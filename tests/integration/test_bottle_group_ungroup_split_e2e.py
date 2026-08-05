"""E2E: what a place-group ADDRESS REMOVAL does to the place's bottles (§7.1).

The answer, by default, is nothing: the bottles stay with the PLACE and the
departing address opens a fresh scope at 0. The conserve-total netting this
file used to prove is deleted (spec §8) — `bottle_balances` has one row per
place since migration a3e7d1f9c204, so there is no donor pair to net against,
no clamp and no shortfall.

What still makes this suite E2E rather than a second copy of
``tests/unit/test_place_group_ungroup_split.py``:

  * Every starting balance — including the negative one — is produced by the
    GENUINE bottle primitives (``record_bottles_delivered`` /
    ``record_bottles_returned``) against real ``Order`` rows, so the ledger +
    materialized-balance machinery is exercised on the way IN.
  * ``BottleLedger.balance_after`` is asserted on those rows, so a change that
    writes a correct balance but a WRONG running ledger snapshot is caught.
  * The operational read (``get_place_balance`` — the driver return anchor and
    the delivered-summary webhook) is asserted before and after the removal.

Note the substantive change the re-key made, which
``test_place_keeps_its_balance_when_a_member_leaves`` now pins: over-returning
8 against a 3-bottle delivery drives the **place** to -5, netting against the
coworker's +9 inside ONE row. The old premise — -5 sitting on one person while
+9 sat on another — is not representable any more.

The DELIBERATE alternative — the admin names a `bottles_leaving` quantity that
departs WITH the address — is ``TestBottlesLeavingWithTheAddress`` at the bottom
of this file. It does not weaken the default above: `bottles_leaving` is opt-in,
defaults to 0, and every impossible value is REJECTED rather than clamped.

All quantities are distinct so a sign error fails loudly. A failure here is a
SERVICE bug and must be fixed there, never patched away in the test.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.bottle import BottleLedger
from business_app.models.customer_link import CustomerLinkEvent
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, OrderStatus, UserRole, UserStatus, UserType


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _user(db, email, phone):
    u = User(
        email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
        first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER, status=UserStatus.ACTIVE, is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id):
    a = UserAddress(user_id=user_id, full_address="x, Tashkent", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    db.session.add(a)
    db.session.commit()
    return a


def _order(db, user, address, order_number):
    order = Order(
        user_id=user.id, order_number=order_number, status=OrderStatus.DELIVERED,
        subtotal=Decimal("0.00"), delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"), delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _grouped_coworkers(db):
    """THREE DISTINCT (never linked) customers, one place group over their addresses.

    Three, not two, because §7.3 dissolves a place the moment a removal would
    leave it with exactly ONE member — and every test in this file is about
    §7.1's removal semantics with the place still standing (they all assert
    ``addr_b.address_group_id == group_id`` afterwards). The third member never
    moves a bottle, so no figure below changes; it only keeps the place alive
    past the first removal. The dissolve has its own file,
    ``tests/integration/test_place_dissolve_and_delete_fence.py``.
    """
    u1 = _user(db, "leave@example.com", "+998900000101")
    u2 = _user(db, "stay@example.com", "+998900000102")
    u3 = _user(db, "quiet@example.com", "+998900000103")
    admin = _user(db, "admin@example.com", "+998900000109")
    svc = CustomerLinkService()
    addr_a = _addr(db, u1.id)   # will be removed
    addr_b = _addr(db, u2.id)   # stays
    addr_c = _addr(db, u3.id)   # stays, and never moves a bottle
    group = svc.create_place_group(
        [addr_a.id, addr_b.id, addr_c.id], acting_admin_id=admin.id,
        reason="same office", label="office",
    )
    return svc, admin, u1, addr_a, u2, addr_b, group.id


def _place(address_id):
    """The balance of the PLACE this address resolves to — the operational read."""
    return BottleTrackingService.get_place_balance(address_id)


def _admin_adj_rows(address_id):
    return BottleLedger.query.filter_by(
        address_id=address_id, event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
    ).order_by(BottleLedger.id.asc()).all()


def _rows_of_type(address_id, event_type):
    return BottleLedger.query.filter_by(
        address_id=address_id, event_type=event_type,
    ).order_by(BottleLedger.id.asc()).all()


@pytest.mark.integration
@pytest.mark.e2e
class TestUngroupLeavesThePlaceWhole:
    def test_place_keeps_its_balance_when_a_member_leaves(self, db):
        """addr_a: deliver +3 then return 8 => the PLACE goes to -5, not addr_a.
        addr_b: +9. Place total 4. Removing addr_a leaves all 4 at the place."""
        svc, admin, u1, addr_a, u2, addr_b, group_id = _grouped_coworkers(db)
        bottles = BottleTrackingService()
        o_a1, o_a2 = _order(db, u1, addr_a, "ORD-A1"), _order(db, u1, addr_a, "ORD-A2")
        bottles.record_bottles_delivered(o_a1.id, u1.id, addr_a.id, Decimal("3"))
        bottles.record_bottles_returned(u1.id, addr_a.id, Decimal("8"), order_id=o_a2.id)
        o_b = _order(db, u2, addr_b, "ORD-B1")
        bottles.record_bottles_delivered(o_b.id, u2.id, addr_b.id, Decimal("9"))
        db.session.commit()
        assert _place(addr_a.id) == Decimal("4.00")     # ONE pool, both members

        # The numbers really came out of the genuine primitives, and the running
        # snapshots walk the ONE place row: +3, then -5, then +4 as u2 delivers.
        assert [(r.quantity, r.balance_after)
                for r in _rows_of_type(addr_a.id, BottleLedgerEventType.DELIVERY)] == [
            (Decimal("3.00"), Decimal("3.00"))]
        assert [(r.quantity, r.balance_after)
                for r in _rows_of_type(addr_a.id, BottleLedgerEventType.RETURN_ON_DELIVERY)] == [
            (Decimal("-8.00"), Decimal("-5.00"))]
        assert [(r.quantity, r.balance_after)
                for r in _rows_of_type(addr_b.id, BottleLedgerEventType.DELIVERY)] == [
            (Decimal("9.00"), Decimal("4.00"))]

        result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                               reason="left")

        # Exact shape, so a surprise key still fails here. `bottles_leaving` is
        # the §7.1 opt-in split and its DEFAULT is what this test is about: zero.
        # `dissolved` is §7.3's flag and stays False while the place still has
        # two members.
        assert result == {"group_id": group_id, "bottles_leaving": Decimal("0.00"), "dissolved": False}
        assert "netting" not in result
        assert _place(addr_b.id) == Decimal("4.00")
        assert _place(addr_a.id) == Decimal("0.00")
        db.session.refresh(addr_a); db.session.refresh(addr_b)
        assert addr_a.address_group_id is None
        assert addr_b.address_group_id == group_id

        event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        assert event.member_user_ids == [u1.id]
        assert event.reason.startswith(f"[group {group_id}]")
        # No donor, no clamp: the audit reason carries no shortfall marker.
        assert "shortfall" not in (event.reason or "").lower()

    def test_removal_posts_no_adjustments_by_default(self, db):
        """Removal is a membership edit, not a bottle movement: zero
        ADMIN_ADJUSTMENT rows, and the place figure does not move."""
        svc, admin, u1, addr_a, u2, addr_b, group_id = _grouped_coworkers(db)
        bottles = BottleTrackingService()

        o_a = _order(db, u1, addr_a, "ORD-4A1")
        bottles.record_bottles_delivered(o_a.id, u1.id, addr_a.id, Decimal("5"))
        o_b = _order(db, u2, addr_b, "ORD-4B1")
        bottles.record_bottles_delivered(o_b.id, u2.id, addr_b.id, Decimal("1"))
        db.session.commit()
        assert _place(addr_a.id) == Decimal("6.00")

        result = svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="moved")

        # Exact shape, so a surprise key still fails here. Nothing left with the
        # address because nothing was asked to (spec §7.1's default), and the
        # place still has two members so §7.3 did not fire.
        assert result == {"group_id": group_id, "bottles_leaving": Decimal("0.00"), "dissolved": False}
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 0
        assert _admin_adj_rows(addr_a.id) == [] and _admin_adj_rows(addr_b.id) == []
        # The whole 6 stayed with the place; the departed address is a clean 0
        # with no balance row of its own at all.
        assert _place(addr_b.id) == Decimal("6.00")
        assert _place(addr_a.id) == Decimal("0.00")
        assert BottleTrackingService.get_place_balance_row(addr_a.id) is None
        db.session.refresh(addr_a); db.session.refresh(addr_b)
        assert addr_a.address_group_id is None
        assert addr_b.address_group_id == group_id

    def test_remove_readd_remove_leaves_the_place_whole(self, db):
        """Groups are long-lived; remove -> re-add -> remove-again is routine.
        Both episodes must audit, and neither may move a bottle unasked — while
        genuine deliveries/returns in between still land on the place."""
        svc, admin, u1, addr_a, u2, addr_b, group_id = _grouped_coworkers(db)
        bottles = BottleTrackingService()

        # Episode 1: addr_a delivers 4 then over-returns 10, addr_b delivers 8.
        # One pool: 4 - 10 + 8 = 2.
        o_a1, o_a2 = _order(db, u1, addr_a, "ORD-5A1"), _order(db, u1, addr_a, "ORD-5A2")
        bottles.record_bottles_delivered(o_a1.id, u1.id, addr_a.id, Decimal("4"))
        bottles.record_bottles_returned(u1.id, addr_a.id, Decimal("10"), order_id=o_a2.id)
        o_b = _order(db, u2, addr_b, "ORD-5B1")
        bottles.record_bottles_delivered(o_b.id, u2.id, addr_b.id, Decimal("8"))
        db.session.commit()
        assert _place(addr_b.id) == Decimal("2.00")

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="first episode")
        assert _place(addr_b.id) == Decimal("2.00")   # untouched by the removal
        assert _place(addr_a.id) == Decimal("0.00")

        # Re-add, then more REAL traffic through the place.
        svc.add_addresses_to_group(group_id, [addr_a.id], acting_admin_id=admin.id, reason="re-add")
        assert _place(addr_a.id) == Decimal("2.00")   # rejoined the same pool
        o_a3, o_a4 = _order(db, u1, addr_a, "ORD-5A3"), _order(db, u1, addr_a, "ORD-5A4")
        bottles.record_bottles_delivered(o_a3.id, u1.id, addr_a.id, Decimal("1"))
        bottles.record_bottles_returned(u1.id, addr_a.id, Decimal("3"), order_id=o_a4.id)
        db.session.commit()
        assert _place(addr_b.id) == Decimal("0.00")   # 2 + 1 - 3

        svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id, reason="second episode")

        assert _place(addr_b.id) == Decimal("0.00")
        assert _place(addr_a.id) == Decimal("0.00")
        events = (CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group")
                  .order_by(CustomerLinkEvent.id.asc()).all())
        assert len(events) == 2
        assert [e.reason.startswith(f"[group {group_id}]") for e in events] == [True, True]
        # Neither episode moved a bottle.
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 0


@pytest.mark.integration
@pytest.mark.e2e
class TestBottlesLeavingWithTheAddress:
    """The opt-in split (spec §7.1) against balances built by the GENUINE
    primitives, with the running `balance_after` snapshots asserted on the way
    out — a change that writes the right balance but a wrong ledger snapshot is
    caught here and nowhere else.
    """

    def test_the_split_conserves_the_place_across_real_deliveries(self, db):
        """addr_a: +6 delivered then 2 returned => 4 attributed to it.
        addr_b: +5. The place holds 9. Three bottles leave with addr_a, so the
        place must end on 6 and the departing address on 3 — the pair, not one
        side of it."""
        svc, admin, u1, addr_a, u2, addr_b, group_id = _grouped_coworkers(db)
        bottles = BottleTrackingService()
        o_a1, o_a2 = _order(db, u1, addr_a, "ORD-S1"), _order(db, u1, addr_a, "ORD-S2")
        bottles.record_bottles_delivered(o_a1.id, u1.id, addr_a.id, Decimal("6"))
        bottles.record_bottles_returned(u1.id, addr_a.id, Decimal("2"), order_id=o_a2.id)
        o_b = _order(db, u2, addr_b, "ORD-S3")
        bottles.record_bottles_delivered(o_b.id, u2.id, addr_b.id, Decimal("5"))
        db.session.commit()

        place_before = _place(addr_a.id)
        assert place_before == Decimal("9.00")
        # The prefill derives 4 from addr_a's OWN attributed entries (+6 -2),
        # which is under the place total of 9 and so is offered unclamped.
        assert BottleTrackingService.suggested_bottles_leaving(group_id, addr_a.id) == Decimal("4.00")

        result = svc.remove_address_from_group(
            addr_a.id, acting_admin_id=admin.id, reason="took three", bottles_leaving=3)

        assert result["group_id"] == group_id
        assert result["bottles_leaving"] == Decimal("3.00")
        place_after, departed = _place(addr_b.id), _place(addr_a.id)
        assert place_after == Decimal("6.00")
        assert departed == Decimal("3.00")
        # CONSERVATION, as a pair: the place lost exactly what left with it.
        assert place_before == place_after + result["bottles_leaving"]
        assert place_after + departed == place_before

        # The two halves, with their running snapshots: the place walks 9 -> 6
        # and the departing address's brand-new scope opens at 3.
        event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        adjustments = _admin_adj_rows(addr_a.id)
        assert [(r.quantity, r.balance_after, r.address_group_id) for r in adjustments] == [
            (Decimal("-3.00"), Decimal("6.00"), group_id),
            (Decimal("3.00"), Decimal("3.00"), None),
        ]
        assert [r.idempotency_key for r in adjustments] == [
            f"place_leave:{group_id}:{event.id}:{addr_a.id}:out",
            f"place_leave:{group_id}:{event.id}:{addr_a.id}:in",
        ]
        # The coworker who stayed was not touched by the split.
        assert _admin_adj_rows(addr_b.id) == []
        db.session.refresh(addr_a); db.session.refresh(addr_b)
        assert addr_a.address_group_id is None
        assert addr_b.address_group_id == group_id

    def test_a_split_above_the_place_total_is_refused_and_moves_nothing(self, db):
        """The place holds 4 even though addr_a alone was delivered 7 — the
        coworker over-returned. Asking for 7 is impossible and is REJECTED, not
        silently clamped, and nothing is written on the way out.

        DO NOT add a `db.session.rollback()` after the raise. Its ABSENCE pins
        the write ordering end-to-end: `bottles_leaving` is validated BEFORE the
        `CustomerLinkEvent` is created, so the refused attempt leaves nothing in
        the session, and the legal retry further down commits ONE removal event
        rather than inheriting a phantom from the attempt that failed.
        """
        svc, admin, u1, addr_a, u2, addr_b, group_id = _grouped_coworkers(db)
        bottles = BottleTrackingService()
        o_a = _order(db, u1, addr_a, "ORD-S4")
        bottles.record_bottles_delivered(o_a.id, u1.id, addr_a.id, Decimal("7"))
        o_b1, o_b2 = _order(db, u2, addr_b, "ORD-S5"), _order(db, u2, addr_b, "ORD-S6")
        bottles.record_bottles_delivered(o_b1.id, u2.id, addr_b.id, Decimal("1"))
        bottles.record_bottles_returned(u2.id, addr_b.id, Decimal("4"), order_id=o_b2.id)
        db.session.commit()
        assert _place(addr_a.id) == Decimal("4.00")
        ledger_before = BottleLedger.query.count()

        with pytest.raises(ValidationError) as exc:
            svc.remove_address_from_group(addr_a.id, acting_admin_id=admin.id,
                                          reason="all of mine", bottles_leaving=7)
        assert exc.value.error_code == "PLACE_SPLIT_INVALID"

        # No rollback — see the docstring. This is the ordering pin.
        assert CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").count() == 0
        assert BottleLedger.query.count() == ledger_before
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT).count() == 0
        assert _place(addr_a.id) == Decimal("4.00")
        assert addr_a.address_group_id == group_id

        # The prefill is what the admin should have accepted: 4, not 7.
        assert BottleTrackingService.suggested_bottles_leaving(group_id, addr_a.id) == Decimal("4.00")
        result = svc.remove_address_from_group(
            addr_a.id, acting_admin_id=admin.id, reason="all the place has",
            bottles_leaving=Decimal("4"))
        assert result["bottles_leaving"] == Decimal("4.00")
        assert _place(addr_b.id) == Decimal("0.00")
        assert _place(addr_a.id) == Decimal("4.00")
        # The refused attempt left no trace on the committed audit trail.
        assert CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").count() == 1
