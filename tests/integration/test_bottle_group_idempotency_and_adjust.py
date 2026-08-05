"""Coverage for two finance-relevant bottle-tracking gaps flagged in review:

1. ``BottleTrackingService.record_bottles_returned`` idempotency — the ledger
   write key is ``f"return:{order_id}:{delivery_id}"`` (see
   ``_create_ledger_entry``'s idempotency short-circuit). Calling it twice
   with the SAME (order_id, delivery_id) must post only one
   RETURN_ON_DELIVERY ledger row and decrement the balance only once. A
   different delivery_id under the same order_id is a DISTINCT key, so both
   applies land.

2. ``BottleTrackingService.admin_adjust_balance`` writes to the PLACE the
   given address resolves to; this asserts the adjustment is reflected by
   ``get_place_balance`` at EVERY member address of a linked+grouped 2-user
   place, that it mints no second balance row, and that the ADMIN_ADJUSTMENT
   ledger row stays attributed to the member it was posted through.
   (Renegotiated by the 2026-07-27 place re-key: the old per-(user, address)
   write + union read is gone — one place, one pool.)

Money/asset sensitive: every quantity is distinct and sign-bearing so a sign
error or a dropped/duplicated write changes the asserted total. Each test
builds its own users / addresses / group via the function-scoped ``db``
fixture (create_all/drop_all per test). No product code is modified.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType, OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


# --------------------------------------------------------------------------- #
# Builders — mirrors the sibling group/union test file's style.
# --------------------------------------------------------------------------- #

def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id, group_id=None):
    a = UserAddress(user_id=user_id, full_address="home, Tashkent", city="Tashkent",
                    latitude=41.31, longitude=69.28, address_group_id=group_id)
    db.session.add(a)
    db.session.commit()
    return a


def _canonical_group(db, primary, *members):
    """Link ``primary`` + ``members`` under one CanonicalCustomer; return a fresh AddressGroup."""
    canonical = CanonicalCustomer(primary_user_id=primary.id)
    db.session.add(canonical)
    db.session.commit()
    for u in (primary, *members):
        u.canonical_customer_id = canonical.id
    db.session.commit()
    group = AddressGroup(canonical_customer_id=canonical.id, label="home")
    db.session.add(group)
    db.session.commit()
    return group


def _order(db, user_id, number, address_id):
    o = Order(user_id=user_id, order_number=number, status=OrderStatus.DELIVERED,
              subtotal=Decimal("0"), delivery_fee=Decimal("0"), discount_amount=Decimal("0"),
              loyalty_discount=Decimal("0"), total_amount=Decimal("0"),
              delivery_address_id=address_id, created_at=datetime.now(UTC))
    db.session.add(o)
    db.session.commit()
    return o


# --------------------------------------------------------------------------- #
# 1. record_bottles_returned idempotency (double-spend guard)
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestRecordBottlesReturnedIdempotency:
    def test_same_order_and_delivery_id_dedupes_return_and_balance(self, db):
        """Two calls with the SAME (order_id, delivery_id) => ONE ledger row,
        balance decremented ONCE. deliver +6, return -2 twice => balance 4,
        not 2 (which would mean the double-spend guard failed)."""
        u = _user(db, "a@example.com", "+998900004001")
        a = _addr(db, u.id)
        order = _order(db, u.id, "ORD-RI-1", a.id)
        svc = BottleTrackingService()
        svc.record_bottles_delivered(order.id, u.id, a.id, Decimal("6"))

        first = svc.record_bottles_returned(u.id, a.id, Decimal("2"), order_id=order.id, delivery_id=100)
        second = svc.record_bottles_returned(u.id, a.id, Decimal("2"), order_id=order.id, delivery_id=100)

        # The idempotency short-circuit in _create_ledger_entry returns the
        # SAME row on the duplicate call rather than raising.
        assert second.id == first.id

        return_rows = BottleLedger.query.filter_by(
            order_id=order.id, delivery_id=100, event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
        ).all()
        assert len(return_rows) == 1
        assert return_rows[0].quantity == Decimal("-2.00")

        # Ungrouped address => the address IS the place.
        assert BottleTrackingService.get_place_balance(a.id) == Decimal("4.00")  # 6 - 2, once

    def test_same_order_different_delivery_id_is_distinct_and_both_apply(self, db):
        """Same order_id, DIFFERENT delivery_id => distinct idempotency key
        (`return:{order_id}:{delivery_id}`), so both returns post and both
        decrements land: deliver +9, return -2 (delivery 200) and -3
        (delivery 201) => balance 4, two ledger rows."""
        u = _user(db, "b@example.com", "+998900004002")
        a = _addr(db, u.id)
        order = _order(db, u.id, "ORD-RI-2", a.id)
        svc = BottleTrackingService()
        svc.record_bottles_delivered(order.id, u.id, a.id, Decimal("9"))

        first = svc.record_bottles_returned(u.id, a.id, Decimal("2"), order_id=order.id, delivery_id=200)
        second = svc.record_bottles_returned(u.id, a.id, Decimal("3"), order_id=order.id, delivery_id=201)

        assert first.id != second.id  # distinct rows, not deduped

        return_rows = BottleLedger.query.filter_by(
            order_id=order.id, event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
        ).all()
        assert len(return_rows) == 2
        assert {r.delivery_id for r in return_rows} == {200, 201}
        assert {r.quantity for r in return_rows} == {Decimal("-2.00"), Decimal("-3.00")}

        assert BottleTrackingService.get_place_balance(a.id) == Decimal("4.00")  # 9 - 2 - 3, both applied


# --------------------------------------------------------------------------- #
# 2. admin_adjust_balance reflects in the group union (per-pair write, union read)
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestAdminAdjustBalanceGroupUnion:
    def test_adjustment_through_one_member_moves_the_whole_place(self, db):
        """Linked+grouped 2-user scenario: adjust through u2's address with a
        SIGNED amount and assert (a) the PLACE moved by exactly the adjustment,
        read at BOTH member addresses, (b) the write produced no second balance
        row, (c) an ADMIN_ADJUSTMENT ledger row exists attributed to u2/addrB
        and none to u1/addrA.

        Was ``test_adjustment_on_one_pair_moves_union_leaves_other_pair_untouched``:
        (a) asserted u2's own row reached -3 while (b) u1's stayed at 3, with the
        union netting to 0. There are no sibling rows to keep apart now — the
        pool IS the thing being adjusted — so (a)/(b) merge into "the one place
        went 5 -> 0". The signed -5, the 5 before and the 0 after are unchanged,
        and the ledger-attribution half of the old isolation claim is kept in (c).
        """
        u1 = _user(db, "a@example.com", "+998900004003")
        u2 = _user(db, "b@example.com", "+998900004004")
        admin = _user(db, "admin@example.com", "+998900004009")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        svc = BottleTrackingService()
        svc.record_bottles_delivered(1_000_001, u1.id, a1.id, Decimal("3"))
        svc.record_bottles_delivered(1_000_002, u2.id, a2.id, Decimal("2"))

        before_a1 = BottleTrackingService.get_place_balance(a1.id)
        before_a2 = BottleTrackingService.get_place_balance(a2.id)
        assert before_a1 == before_a2 == Decimal("5.00")  # 3 + 2 into ONE pool

        adjustment = Decimal("-5.00")  # signed: a sign error would fail every assert below
        svc.admin_adjust_balance(u2.id, a2.id, adjustment, admin.id, "correcting an overcount")

        # (a) the PLACE moved by exactly the adjustment, read at BOTH addresses.
        after_a1 = BottleTrackingService.get_place_balance(a1.id)
        after_a2 = BottleTrackingService.get_place_balance(a2.id)
        assert after_a1 == before_a1 + adjustment == Decimal("0.00")
        assert after_a2 == before_a2 + adjustment == Decimal("0.00")

        # (b) it stayed ONE pool — no per-member row was minted by the write.
        assert BottleBalance.query.count() == 1

        # (c) an ADMIN_ADJUSTMENT ledger row exists for (u2, addrB) with the signed qty.
        ledger_rows = BottleLedger.query.filter_by(
            user_id=u2.id, address_id=a2.id, event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
        ).all()
        assert len(ledger_rows) == 1
        assert ledger_rows[0].quantity == adjustment
        # No ADMIN_ADJUSTMENT row was attributed to the other member.
        assert BottleLedger.query.filter_by(
            user_id=u1.id, address_id=a1.id, event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
        ).count() == 0
