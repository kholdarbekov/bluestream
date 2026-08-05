from decimal import Decimal

from business_app.models.customer_link import CanonicalCustomer
from business_app.models.order import Order
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType, OrderStatus


def _link(db, *users):
    """Put every user in one canonical cluster (primary = the first)."""
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def _seed(db, place, sample_user, second_sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("1"))
    db.session.flush()


def test_both_coworkers_see_the_same_place_number(
    app, db, place, sample_user, second_sample_user
):
    _seed(db, place, sample_user, second_sample_user)
    svc = BottleTrackingService()
    a = svc.get_customer_bottle_overview(sample_user.id)
    b = svc.get_customer_bottle_overview(second_sample_user.id)
    assert a["balances"][0]["place_balance"] == 7.0
    assert b["balances"][0]["place_balance"] == 7.0


def test_no_per_member_balance_is_exposed(app, db, place, sample_user, second_sample_user):
    _seed(db, place, sample_user, second_sample_user)
    row = BottleTrackingService().get_customer_bottle_overview(sample_user.id)["balances"][0]
    assert row["place_members"], "members must still be named"
    for member in row["place_members"]:
        assert "balance" not in member
        assert member["member_name"]


def test_no_scalar_cluster_total(app, db, place, sample_user, second_sample_user):
    _seed(db, place, sample_user, second_sample_user)
    payload = BottleTrackingService().get_customer_bottle_overview(sample_user.id)
    assert "cluster_total_balance" not in payload


def test_member_with_no_delivery_still_sees_the_place(
    app, db, place, sample_user, second_sample_user
):
    """Membership must come from addresses, not from owning a balance row —
    otherwise a coworker who never took a delivery at their own door sees the
    empty state while the driver at that door is offered the place total."""
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("7"))
    db.session.flush()
    payload = svc.get_customer_bottle_overview(second_sample_user.id)
    assert payload["balances"], "second member must see the place"
    assert payload["balances"][0]["place_balance"] == 7.0


def test_get_order_bottle_summary_uses_place_balance_for_grouped_address(
    app, db, place, sample_user, second_sample_user
):
    """`get_order_bottle_summary` was still calling the deleted
    `get_group_union_balance`; it must be re-pointed at `get_place_balance` and
    keep returning the PLACE total for an order delivered to a grouped address,
    not just the pair balance at the delivery address."""
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("1"))
    db.session.flush()

    order = Order(
        user_id=sample_user.id,
        order_number="ORD-SUM-PLACE-001",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=place["a1"].id,
    )
    db.session.add(order)
    db.session.flush()

    summary = BottleTrackingService.get_order_bottle_summary(order)

    assert summary["balance"] == Decimal("7")
    assert summary["balance"] == BottleTrackingService.get_place_balance(place["a1"].id)


def test_dedup_prefers_the_viewers_own_address_as_scope_representative(
    app, db, place, sample_user, second_sample_user
):
    """Two LINKED accounts each own an address at the same place — the exact
    multi-phone-linking scenario this re-key exists to serve. `UserAddress`
    rows come back in no guaranteed order, and the dedup loop must not just
    keep whichever one it meets first: the CALLER's own address has to win, or
    the caller sees `is_own: False` / a sibling's `owner_user_id` on a place
    they themselves own an address at (and it mis-sorts into the siblings
    tier). Called from BOTH accounts in turn so neither direction can pass by
    accident of row order.
    """
    _link(db, sample_user, second_sample_user)
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("14"))
    db.session.flush()

    for caller in (sample_user, second_sample_user):
        payload = BottleTrackingService().get_customer_bottle_overview(caller.id)
        assert len(payload["balances"]) == 1, "one shared place must dedup to one row"
        row = payload["balances"][0]
        assert row["is_own"] is True, f"caller {caller.id} must see their own place as own"
        assert row["owner_user_id"] == caller.id
        assert row["place_balance"] == 14.0
