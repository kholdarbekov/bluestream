"""Edge / finance cases for returnable-bottle balances at a shared PLACE.

Renegotiated by the 2026-07-27 place re-key. This module used to pin the
per-pair balance model unioned across an ``AddressGroup``: one
``BottleBalance`` row per ``(user_id, address_id)``, summed at read time by
``get_group_union_balance``. Bottles at a shared place are now ONE pool — a
single balance row keyed to the address group (or, when ungrouped, to the
address) — so per-pair isolation is deleted by design and the union has nothing
left to sum.

What this module pins now:

  * a place's balance is ONE row, reachable from every member address, and can
    go NEGATIVE (over-collection);
  * every operational surface (driver return anchor, delivered-summary webhook,
    staff ``_customer_bottle_balance``) consults
    ``BottleTrackingService.get_place_balance``;
  * a cross-user grouped net-neutral delivery leaves the place balance and the
    other member's LEDGER unchanged — still CORRECT, still pinned as
    ``test_ng_regression_*`` (bottle *attribution* stays per-user on the ledger
    even though the *pool* does not);
  * ``get_customer_summary`` / ``get_dashboard_stats`` are now PLACE-keyed —
    the documented Phase-1 per-user gap is CLOSED, and the test that locked the
    gap open now locks the fix in.

All balances/quantities are asserted with EXACT distinct Decimal values so a
sign error or a wrong-scope write is caught. Every asserted TOTAL is carried
over unchanged from the union era.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order, OrderItem
from business_app.models.bottle import BottleBalance, BottleLedger, BottleFine
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, OrderStatus, PaymentMethod, UserRole, UserType


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Helpers — every test builds its own users/addresses/group via the db fixture.
# --------------------------------------------------------------------------- #

def _user(db, email, phone):
    u = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name="U",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id, *, group_id=None, full_address="home, Tashkent"):
    a = UserAddress(
        user_id=user_id,
        full_address=full_address,
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        address_group_id=group_id,
    )
    db.session.add(a)
    db.session.commit()
    return a


def _link(db, *users):
    """Create a CanonicalCustomer and point every user at it. Returns the canonical."""
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def _group(db, canonical_id, *addrs, label="home"):
    """Create an AddressGroup under canonical and attach the given addresses."""
    group = AddressGroup(canonical_customer_id=canonical_id, label=label)
    db.session.add(group)
    db.session.commit()
    for a in addrs:
        a.address_group_id = group.id
    db.session.commit()
    return group


def _set_balance(db, address, amount):
    """Seed the PLACE's single balance row for ``address``.

    Group-keyed when grouped, address-keyed otherwise — the
    ``(address_group_id IS NULL) <> (address_id IS NULL)`` CHECK admits nothing
    else, and the two UNIQUEs make a second row for the same place impossible.
    """
    b = BottleBalance(
        address_group_id=address.address_group_id,
        address_id=None if address.address_group_id is not None else address.id,
        balance=Decimal(str(amount)),
    )
    db.session.add(b)
    db.session.commit()
    return b


def _order(db, user_id, address_id, *, status=OrderStatus.DELIVERED, payment_method=None):
    o = Order(
        user_id=user_id,
        status=status,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        delivery_address_id=address_id,
        payment_method=payment_method,
        created_at=datetime.now(UTC),
    )
    db.session.add(o)
    db.session.commit()
    return o


# --------------------------------------------------------------------------- #
# 1. Ledger attribution stays per-user; the POOL moves on a cross-user write.
# --------------------------------------------------------------------------- #

def test_cross_user_grouped_delivery_pools_at_the_place_but_attributes_to_the_actor(db):
    """+N delivered through u2's grouped address moves the shared place by N.

    Was ``test_cross_user_grouped_delivery_isolates_pair_but_moves_union``, whose
    first half asserted (u1,addrA) kept its own 5 while (u2,addrB) got its own 3.
    That per-pair isolation is deleted by design — one place, one pool of 8. The
    isolation that DOES survive is on the ledger: only u2 wrote a row. The
    replacement assertion (exactly one balance row exists) is strictly stronger
    than the old "u1's row is untouched".
    """
    u1 = _user(db, "iso1@example.com", "+998900010001")
    u2 = _user(db, "iso2@example.com", "+998900010002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_a, 5)  # the shared place already holds 5 empties

    BottleTrackingService().record_bottles_delivered(
        order_id=901, user_id=u2.id, address_id=addr_b.id, quantity=Decimal("3")
    )
    db.session.commit()

    # u1 — ledger untouched. Attribution is still per-person.
    assert BottleLedger.query.filter_by(user_id=u1.id).count() == 0

    # u2 — the actor, with exactly one DELIVERY row.
    u2_rows = BottleLedger.query.filter_by(user_id=u2.id).all()
    assert len(u2_rows) == 1
    assert u2_rows[0].event_type == BottleLedgerEventType.DELIVERY
    assert u2_rows[0].quantity == Decimal("3.00")

    # The place reflects the cross-user write from EITHER address: 5 + 3 = 8...
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("8.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("8.00")
    # ...and it is genuinely ONE pool, not two rows that happen to sum to 8.
    assert BottleBalance.query.count() == 1


# --------------------------------------------------------------------------- #
# 2. THE USER-REPORTED SCENARIO — cross-user grouped net-neutral delivery.
# --------------------------------------------------------------------------- #

def test_ng_regression_cross_user_grouped_net_neutral_delivery_leaves_other_member_untouched(db):
    """Regression pin: u2 delivers +3 then returns -3 at a SHARED (grouped) address.

    u1's ledger stays completely unchanged and the place balance is exactly what
    it was before u2's activity. This is CORRECT behaviour — a net-neutral
    cross-user delivery moves nothing net. (The old "u2's own pair nets to 0"
    assertion is subsumed: there is one pool, and it is unchanged.)
    """
    u1 = _user(db, "ng1@example.com", "+998900020001")
    u2 = _user(db, "ng2@example.com", "+998900020002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_a, 5)

    balance_before = BottleTrackingService.get_place_balance(addr_a.id)
    assert balance_before == Decimal("5.00")

    svc = BottleTrackingService()
    svc.record_bottles_delivered(order_id=777, user_id=u2.id, address_id=addr_b.id, quantity=Decimal("3"))
    svc.record_bottles_returned(u2.id, addr_b.id, Decimal("3"), order_id=777, delivery_id=None)
    db.session.commit()

    # u1's ledger: untouched.
    assert BottleLedger.query.filter_by(user_id=u1.id).count() == 0
    # u2 wrote both legs.
    assert BottleLedger.query.filter_by(user_id=u2.id).count() == 2

    # Place unchanged at both grouped addresses — CORRECT.
    assert BottleTrackingService.get_place_balance(addr_a.id) == balance_before == Decimal("5.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")


# --------------------------------------------------------------------------- #
# 3. Returning more than delivered nets DOWN the place, staying positive.
# --------------------------------------------------------------------------- #

def test_return_more_than_delivered_nets_within_the_place(db):
    """+3 then -5 through addrB against a place already holding 5 => 3.

    Was ``test_return_more_than_delivered_pair_goes_negative_union_nets``, which
    asserted (u2,addrB) reached -2 in its own row before the union netted it
    against u1's +5. There is no sibling row to strand the -2 in now; the
    arithmetic (5 + 3 - 5 == 3) and the driver-facing 3.0 are unchanged.
    """
    u1 = _user(db, "neg1@example.com", "+998900030001")
    u2 = _user(db, "neg2@example.com", "+998900030002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_a, 5)

    svc = BottleTrackingService()
    svc.record_bottles_delivered(order_id=880, user_id=u2.id, address_id=addr_b.id, quantity=Decimal("3"))
    svc.record_bottles_returned(u2.id, addr_b.id, Decimal("5"), order_id=880, delivery_id=None)
    db.session.commit()

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("3.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("3.00")

    # Positive place => staff read shows the true 3.
    from business_app.api.staff import _customer_bottle_balance

    order_at_b = _order(db, u2.id, addr_b.id)
    assert _customer_bottle_balance(order_at_b) == 3.0


def test_negative_place_clamps_to_zero_in_customer_bottle_balance(db):
    """A NEGATIVE place balance reads as 0 via staff ``_customer_bottle_balance``
    (clamp), while ``get_place_balance`` / ``get_order_bottle_summary`` expose
    the raw negative."""
    u1 = _user(db, "clamp1@example.com", "+998900040001")
    u2 = _user(db, "clamp2@example.com", "+998900040002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_a, 1)  # small positive to start

    svc = BottleTrackingService()
    svc.record_bottles_delivered(order_id=881, user_id=u2.id, address_id=addr_b.id, quantity=Decimal("3"))
    svc.record_bottles_returned(u2.id, addr_b.id, Decimal("6"), order_id=881, delivery_id=None)
    db.session.commit()

    # 1 + 3 - 6 = -2 (negative).
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("-2.00")

    order_at_b = _order(db, u2.id, addr_b.id)

    # Staff customer view clamps the negative to 0.
    from business_app.api.staff import _customer_bottle_balance

    assert _customer_bottle_balance(order_at_b) == 0.0

    # The read-only order summary balance is the RAW (negative) figure, not clamped.
    summary = BottleTrackingService.get_order_bottle_summary(order_at_b)
    assert summary["balance"] == Decimal("-2.00")


# --------------------------------------------------------------------------- #
# 4. Extra member addresses do not multiply the pool.
# --------------------------------------------------------------------------- #

def test_extra_grouped_member_addresses_do_not_change_the_place_balance(db):
    """Was ``test_grouped_zero_and_missing_balance_addresses_contribute_zero``,
    which checked that an explicit 0 row and a row-less address each ADDED
    nothing to the union.

    Nothing "contributes" any more — membership is not additive. The property
    that carries over is the one that mattered: adding member addresses to a
    place must not change its balance, and every member reads the same 4.
    """
    u1 = _user(db, "zero1@example.com", "+998900050001")
    u2 = _user(db, "zero2@example.com", "+998900050002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    group = _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_a, 4)

    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")
    # addr_b holds no row of its own and still reads the place's 4.
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("4.00")

    # A third grouped address of u1 changes nothing either.
    addr_c = _addr(db, u1.id, group_id=group.id, full_address="C")
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")
    assert BottleTrackingService.get_place_balance(addr_c.id) == Decimal("4.00")
    assert BottleBalance.query.count() == 1


# --------------------------------------------------------------------------- #
# 5. Grouping two addresses that ALREADY hold balances.
# --------------------------------------------------------------------------- #

def test_marking_addresses_with_existing_balances_yields_immediate_sum_union(db):
    """CustomerLinkService.create_place_group over two pre-funded places =>
    the new place holds their combined total at once.

    Was `xfail(strict=True)` until Plan C task 3 (spec §7.2) taught the join to
    re-scope each joiner's own history — ledger rows re-stamped, own-scope
    balance folded into the place's single row — so the 4 and the 3 are no
    longer stranded where no place-scoped read can reach them.
    """
    u1 = _user(db, "mark1@example.com", "+998900060001")
    u2 = _user(db, "mark2@example.com", "+998900060002")
    admin = _user(db, "markadm@example.com", "+998900060009")
    _link(db, u1, u2)  # linked, but place grouping no longer depends on the cluster
    addr_a = _addr(db, u1.id, full_address="A")  # ungrouped
    addr_b = _addr(db, u2.id, full_address="B")  # ungrouped
    _set_balance(db, addr_a, 4)
    _set_balance(db, addr_b, 3)

    # Ungrouped: each address is its own place.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("3.00")

    CustomerLinkService().create_place_group(
        [addr_a.id, addr_b.id], acting_admin_id=admin.id, reason="same physical place"
    )
    db.session.refresh(addr_a)
    db.session.refresh(addr_b)

    # Immediately after grouping, the place holds the combined total.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("7.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("7.00")


# --------------------------------------------------------------------------- #
# 6. record_bottles_returned with qty <= 0 raises ValidationError (no write).
# --------------------------------------------------------------------------- #

def test_record_bottles_returned_non_positive_raises_and_writes_nothing(db):
    u = _user(db, "val1@example.com", "+998900070001")
    a = _addr(db, u.id)
    svc = BottleTrackingService()

    with pytest.raises(ValidationError):
        svc.record_bottles_returned(u.id, a.id, Decimal("0"), order_id=1, delivery_id=None)
    with pytest.raises(ValidationError):
        svc.record_bottles_returned(u.id, a.id, Decimal("-3"), order_id=2, delivery_id=None)

    db.session.rollback()

    # Validation fires before any balance/ledger row is created.
    assert BottleTrackingService.get_place_balance_row(a.id) is None
    assert BottleLedger.query.filter_by(user_id=u.id).count() == 0


# --------------------------------------------------------------------------- #
# 7. A fine does not move the place balance.
# --------------------------------------------------------------------------- #

def test_fine_issued_does_not_corrupt_the_place_balance(db):
    """issue_fine writes a 0-quantity FINE_ISSUED ledger row — the place balance
    is unchanged.

    Was ``test_fine_issued_on_one_pair_does_not_corrupt_union``. ``issue_fine``
    is keyed by ``address_id`` now (its old ``bottle_balance_id`` argument is
    gone with the per-pair row), and the fine freezes the scope it was issued
    at, so this also pins that the frozen group id matches the place.
    """
    u1 = _user(db, "fine1@example.com", "+998900080001")
    u2 = _user(db, "fine2@example.com", "+998900080002")
    admin = _user(db, "fineadm@example.com", "+998900080009")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    group = _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_a, 5)

    svc = BottleTrackingService()
    svc.issue_fine(
        user_id=u1.id,
        address_id=addr_a.id,
        quantity=Decimal("2"),
        fine_amount=Decimal("10000"),
        actor_user_id=admin.id,
        notes="missing bottles",
    )
    db.session.commit()

    fine = BottleFine.query.filter_by(user_id=u1.id).one()
    assert fine.address_id == addr_a.id
    assert fine.address_group_id == group.id  # scope frozen at the PLACE
    # A fine does NOT move the bottle balance (only mark_fine_paid does).
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")


# --------------------------------------------------------------------------- #
# 8. Delivering to a member's OTHER (ungrouped) address doesn't touch the place.
# --------------------------------------------------------------------------- #

def test_delivery_to_members_other_ungrouped_address_does_not_touch_group(db):
    u1 = _user(db, "other1@example.com", "+998900090001")
    u2 = _user(db, "other2@example.com", "+998900090002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")  # grouped
    addr_b = _addr(db, u2.id, full_address="B")  # grouped
    _group(db, canonical.id, addr_a, addr_b)
    addr_c = _addr(db, u1.id, full_address="C")  # u1's OTHER address — ungrouped
    _set_balance(db, addr_a, 6)

    BottleTrackingService().record_bottles_delivered(
        order_id=950, user_id=u1.id, address_id=addr_c.id, quantity=Decimal("7")
    )
    db.session.commit()

    # The shared place is untouched by the ungrouped-address delivery.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("6.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("6.00")
    # The ungrouped address is its own place.
    assert BottleTrackingService.get_place_balance(addr_c.id) == Decimal("7.00")


# --------------------------------------------------------------------------- #
# 9. A 3-phone cluster where only 2 addresses are grouped.
# --------------------------------------------------------------------------- #

def test_three_phone_cluster_only_two_addresses_grouped(db):
    """Three linked users; only addrA & addrB share a place. addrC (u3) is its own."""
    u1 = _user(db, "tri1@example.com", "+998900100001")
    u2 = _user(db, "tri2@example.com", "+998900100002")
    u3 = _user(db, "tri3@example.com", "+998900100003")
    canonical = _link(db, u1, u2, u3)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    addr_c = _addr(db, u3.id, full_address="C")
    _group(db, canonical.id, addr_a, addr_b)  # only A & B grouped
    _set_balance(db, addr_a, 5)
    _set_balance(db, addr_c, 10)

    # The 2-address place excludes the ungrouped third phone.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")
    # addrC is its own place even though u3 is in the same cluster.
    assert BottleTrackingService.get_place_balance(addr_c.id) == Decimal("10.00")

    # A delivery to the ungrouped third phone does not move the A/B place.
    BottleTrackingService().record_bottles_delivered(
        order_id=960, user_id=u3.id, address_id=addr_c.id, quantity=Decimal("4")
    )
    db.session.commit()
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")
    assert BottleTrackingService.get_place_balance(addr_c.id) == Decimal("14.00")


# --------------------------------------------------------------------------- #
# 10. The Phase-1 per-user gap is CLOSED — both summaries are PLACE-keyed.
# --------------------------------------------------------------------------- #

def test_get_customer_summary_and_dashboard_stats_are_place_keyed(db):
    """Was ``test_get_customer_summary_and_dashboard_stats_are_per_user_known_phase1_gap``.

    That test LOCKED THE GAP OPEN: it asserted ``get_customer_summary(u1)`` read
    ``total_balance == 0`` while its grouped coworker u2 held 4 at the same
    place, and that ``top_debtors`` listed u2 but not u1. This plan is what
    closed that gap, so the assertion is INVERTED rather than dropped —
    ``total_balance`` no longer exists (summing a shared pool per member would
    double-count it), ``cluster_scopes`` lists each distinct place, and
    ``top_debtors`` are places rather than people. The quantity under test (the
    4 delivered through phone-2) is unchanged.
    """
    u1 = _user(db, "sum1@example.com", "+998900110001")
    u2 = _user(db, "sum2@example.com", "+998900110002")
    canonical = _link(db, u1, u2)
    addr_a = _addr(db, u1.id, full_address="A")
    addr_b = _addr(db, u2.id, full_address="B")
    group = _group(db, canonical.id, addr_a, addr_b)

    svc = BottleTrackingService()
    svc.record_bottles_delivered(order_id=970, user_id=u2.id, address_id=addr_b.id, quantity=Decimal("4"))
    db.session.commit()

    # GAP CLOSED: u1 sees the shared place's 4 even though u1 personally never
    # took a delivery — the pool belongs to the place, not to whoever signed.
    for uid in (u1.id, u2.id):
        scopes = svc.get_customer_summary(uid)["cluster_scopes"]
        assert scopes == [
            {"address_group_id": group.id, "address_id": None, "balance": 4.0, "is_shared": True}
        ]
    # And it is reported ONCE per place, not once per member.
    assert "total_balance" not in svc.get_customer_summary(u1.id)

    # GAP CLOSED: dashboard stats count PLACES. Two coworkers at one office are
    # ONE debtor holding one pool, not two rows.
    stats = BottleTrackingService.get_dashboard_stats()
    assert stats["total_bottles_out"] == 4.0
    assert stats["places_with_balance"] == 1
    assert [d["address_group_id"] for d in stats["top_debtors"]] == [group.id]

    # Read from either member address, it is the same place.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("4.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("4.00")


# --------------------------------------------------------------------------- #
# 11. TRUE e2e — OrderService DELIVERED transition drives the bottle primitives.
# --------------------------------------------------------------------------- #

def test_order_delivered_transition_records_bottles_and_place_balance_e2e(db, sample_user, sample_product):
    """Drive the real OrderService.update_order_status(DELIVERED) path.

    The DELIVERED branch of ``_handle_status_change_actions`` calls
    record_bottles_delivered / record_bottles_returned against
    (order.user_id, order.delivery_address_id). Assert the ledger rows and the
    place balance reflect it — the delivery address is grouped with a second
    phone's address, so both read the same pool.
    """
    from business_app.services.order_service import OrderService

    # sample_user (u1) is linked to a second phone (u2) whose address shares the
    # same physical group; the place already holds 2 empties.
    u2 = _user(db, "e2e2@example.com", "+998900120002")
    admin = _user(db, "e2eadm@example.com", "+998900120009")
    canonical = _link(db, sample_user, u2)
    addr_a = _addr(db, sample_user.id, full_address="A e2e")
    addr_b = _addr(db, u2.id, full_address="B e2e")
    _group(db, canonical.id, addr_a, addr_b)
    _set_balance(db, addr_b, 2)

    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("2.00")
    db.session.commit()

    order = _order(db, sample_user.id, addr_a.id, status=OrderStatus.OUT_FOR_DELIVERY, payment_method=PaymentMethod.CARD)
    item = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        quantity=2,  # 2 units * 2 bottles/unit = 4 delivered
        unit_price=Decimal("15000.00"),
        total_price=Decimal("30000.00"),
        discount_amount=Decimal("0.00"),
    )
    db.session.add(item)
    db.session.commit()

    OrderService().update_order_status(
        order.id, OrderStatus.DELIVERED, updated_by=admin.id, bottles_returned=1
    )

    # DELIVERY ledger row: +4, keyed by the order idempotency key.
    delivery_row = BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").first()
    assert delivery_row is not None
    assert delivery_row.event_type == BottleLedgerEventType.DELIVERY
    assert delivery_row.quantity == Decimal("4.00")
    assert delivery_row.user_id == sample_user.id
    assert delivery_row.address_id == addr_a.id

    # RETURN_ON_DELIVERY row: -1.
    return_row = BottleLedger.query.filter_by(
        order_id=order.id, event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
    ).first()
    assert return_row is not None
    assert return_row.quantity == Decimal("-1.00")

    # The shared place: 2 (pre-existing) + 4 delivered - 1 returned = 5, read
    # identically at both member addresses.
    assert BottleTrackingService.get_place_balance(addr_a.id) == Decimal("5.00")
    assert BottleTrackingService.get_place_balance(addr_b.id) == Decimal("5.00")
