"""Integration/e2e coverage for the PLACE-scoped bottle read surfaces.

Renegotiated by the 2026-07-27 place re-key. This module used to pin
``BottleTrackingService.get_group_union_balance``, which SUMMED one
``BottleBalance`` row per ``(user_id, address_id)`` pair across every address
sharing an ``address_group_id``. There are no pairs any more: a physical PLACE
— the address group when the address is grouped, else the address itself — owns
exactly ONE balance row, so the union has nothing left to compute and the
helper is deleted.

Scope now:
    * ``BottleTrackingService.get_place_balance(address_id)`` — the SSOT that
      resolves an address to its place and returns that place's single balance.
    * The two place-aware read surfaces built on top of it:
        - ``business_app.api.staff._customer_bottle_balance(order)`` ==
          ``max(0, place balance at order.delivery_address_id)`` — driver-facing,
          so a negative/over-credited place clamps to 0.
        - ``BottleTrackingService.get_order_bottle_summary(order)["balance"]`` ==
          the place balance, Decimal preserved and NOT clamped (the raw physical
          figure for the delivered-summary webhook).

Every asserted TOTAL is unchanged from the union era — 7, 5, 1, -3, 8, 4, 9 —
only the vehicle that produces it changed. Each test builds its own users /
addresses / group via the function-scoped ``db`` fixture (create_all/drop_all
per test).

These assert the SSOT that backs cross-phone ("same physical place") bottle
accounting for two linked customers.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.api.staff import _customer_bottle_balance
from shared.enums import OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


# --------------------------------------------------------------------------- #
# Builders — each test composes its own graph so they stay independent.
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


def _bal(db, address, amount):
    """Write the PLACE's single balance row for ``address`` (may be negative).

    Group-keyed when the address is grouped, address-keyed otherwise — the
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
# get_place_balance — read edge cases
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestGroupUnionBalanceReads:
    def test_ungrouped_address_returns_single_pair(self, db):
        """Ungrouped (address_group_id is None) => the address IS the place, Decimal."""
        u = _user(db, "a@example.com", "+998900001001")
        a = _addr(db, u.id)  # no group
        _bal(db, a, "7.00")

        balance = BottleTrackingService.get_place_balance(a.id)

        assert balance == Decimal("7.00")
        assert isinstance(balance, Decimal)

    def test_ungrouped_address_with_no_balance_row_is_zero(self, db):
        """No BottleBalance row yet => coalesces to Decimal 0 (never None)."""
        u = _user(db, "a@example.com", "+998900001002")
        a = _addr(db, u.id)

        balance = BottleTrackingService.get_place_balance(a.id)

        assert balance == Decimal("0")
        assert isinstance(balance, Decimal)

    def test_grouped_two_users_read_the_same_place_at_either_address(self, db):
        """Was `test_grouped_two_users_sum_at_either_address` (3 + 2). The two
        linked users' addresses now resolve to ONE pooled row; the total read at
        either address is still 5."""
        u1 = _user(db, "a@example.com", "+998900001003")
        u2 = _user(db, "b@example.com", "+998900001004")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)  # same physical home via phone-2
        _bal(db, a1, "5.00")

        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("5.00")

    def test_grouped_three_addresses_all_resolve_to_one_place(self, db):
        """Was `test_grouped_three_addresses_sum` (4 + 2 + 1). Summing three
        member rows is not the mechanism any more, but the property that
        mattered survives: EVERY member address — including a member's second
        address at the same place — resolves to the one pooled row, and the
        group holds exactly one row."""
        u1 = _user(db, "a@example.com", "+998900001005")
        u2 = _user(db, "b@example.com", "+998900001006")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)  # u1, home
        a2 = _addr(db, u2.id, group_id=group.id)  # u2, home (phone-2)
        a3 = _addr(db, u1.id, group_id=group.id)  # u1, second address at same place
        _bal(db, a1, "7.00")

        for addr in (a1, a2, a3):
            assert BottleTrackingService.get_place_balance(addr.id) == Decimal("7.00")
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 1
        # No per-member row was minted alongside the pool.
        assert BottleBalance.query.count() == 1

    def test_over_collection_nets_within_the_place(self, db):
        """Was `test_negative_pair_nets_within_group`: 6 on one pair, -5 on the
        other, netted to 1 at read time. One row per place means the netting now
        happens IN the row via the ledger. Same arithmetic (6 - 5 == 1), same
        invariant: a return is SUMMED, never abs'd or dropped — if the sign were
        mishandled this would read 11."""
        u1 = _user(db, "a@example.com", "+998900001007")
        u2 = _user(db, "b@example.com", "+998900001008")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        order = _order(db, u1.id, "ORD-NET-1", a1.id)

        svc = BottleTrackingService()
        svc.record_bottles_delivered(order.id, u1.id, a1.id, Decimal("6"))
        # Over-collected via the SECOND phone's address — same pool.
        svc.record_bottles_returned(u2.id, a2.id, Decimal("5"), order_id=order.id, delivery_id=None)
        db.session.commit()

        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("1.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("1.00")

    def test_address_joining_group_after_it_has_a_balance_is_included_immediately(self, db):
        """A place's balance is derived from membership: adding an address that
        ALREADY holds a balance to the group must make the place reflect it.

        Was `xfail(strict=True)` until Plan C task 3 (spec §7.2). The xfail
        reason claimed it would flip to XPASS on its own, but it could not: it
        attached a2 with a bare ``a2.address_group_id = group.id`` COLUMN write,
        which no service hook can observe, so a2's 3 stayed on its own
        address-keyed row no matter what the join path learned to do. Joining is
        a SERVICE operation — it moves bottle history — so the test now goes
        through ``add_addresses_to_group``, which is the surface the admin API
        and every caller actually use. The assertions (8 from both addresses)
        are unchanged.
        """
        u1 = _user(db, "a@example.com", "+998900001009")
        u2 = _user(db, "b@example.com", "+998900001010")
        admin = _user(db, "adm@example.com", "+998900001019")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)     # in the group from the start
        a2 = _addr(db, u2.id, group_id=None)         # ungrouped, but already has a balance
        _bal(db, a1, "5.00")
        _bal(db, a2, "3.00")

        # Before joining: two independent places.
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("3.00")

        # Attach a2 (with its pre-existing 3) to the group.
        CustomerLinkService().add_addresses_to_group(
            group.id, [a2.id], acting_admin_id=admin.id, reason="joined the office"
        )
        db.session.refresh(a2)

        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("8.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("8.00")

    def test_ungrouped_address_of_grouped_member_is_excluded(self, db):
        """A grouped member's OWN ungrouped address is a DIFFERENT place."""
        u1 = _user(db, "a@example.com", "+998900001011")
        group = _canonical_group(db, u1)
        a_grouped = _addr(db, u1.id, group_id=group.id)
        a_ungrouped = _addr(db, u1.id, group_id=None)  # same user, different place, no group
        _bal(db, a_grouped, "4.00")
        _bal(db, a_ungrouped, "9.00")

        # The shared place sees only its own pool.
        assert BottleTrackingService.get_place_balance(a_grouped.id) == Decimal("4.00")
        # The ungrouped address is its own place.
        assert BottleTrackingService.get_place_balance(a_ungrouped.id) == Decimal("9.00")


# --------------------------------------------------------------------------- #
# The place balance reflects grouped WRITES via the primitive (item 2) +
# ungrouped isolation (item 5), driven through the real ledger write path.
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestUnionReflectsGroupedWritesViaPrimitive:
    def test_cross_user_grouped_delivery_raises_the_place_at_both_addresses(self, db):
        """+N delivered to a2 — a grouped, cross-user address — lifts the place
        balance by N, read at BOTH grouped addresses.

        Was `..._raises_union_at_both_addresses`, which additionally asserted the
        WRITE stayed per-pair (u1's row 2, u2's row 5). That half is deleted by
        design: one place, one pool. Its replacement asserts the stronger
        property — the write produced NO second row — while keeping 2 / +5 / 7.
        """
        u1 = _user(db, "a@example.com", "+998900001012")
        u2 = _user(db, "b@example.com", "+998900001013")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "2.00")  # the place already holds 2 empties
        order = _order(db, u2.id, "ORD-XU-1", a2.id)

        BottleTrackingService().record_bottles_delivered(order.id, u2.id, a2.id, Decimal("5"))

        # Rose by exactly the delivered 5 at BOTH addresses (2 + 5 == 7).
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("7.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("7.00")
        # The delivery through phone-2 did NOT mint a second, per-member row.
        assert BottleBalance.query.count() == 1
        # Attribution is still per-user on the LEDGER: only u2 moved a bottle.
        from business_app.models.bottle import BottleLedger

        assert BottleLedger.query.filter_by(user_id=u1.id).count() == 0
        assert BottleLedger.query.filter_by(user_id=u2.id).count() == 1

    def test_delivery_to_ungrouped_address_of_grouped_member_leaves_place_unchanged(self, db):
        """Item 5: delivering to a grouped member's UNGROUPED address does not
        touch the shared place — only that separate place moves."""
        u1 = _user(db, "a@example.com", "+998900001014")
        group = _canonical_group(db, u1)
        a_grouped = _addr(db, u1.id, group_id=group.id)
        a_ungrouped = _addr(db, u1.id, group_id=None)
        _bal(db, a_grouped, "3.00")
        order = _order(db, u1.id, "ORD-UG-1", a_ungrouped.id)

        BottleTrackingService().record_bottles_delivered(order.id, u1.id, a_ungrouped.id, Decimal("8"))

        # The shared place is unchanged; the ungrouped place reflects the +8.
        assert BottleTrackingService.get_place_balance(a_grouped.id) == Decimal("3.00")
        assert BottleTrackingService.get_place_balance(a_ungrouped.id) == Decimal("8.00")


# --------------------------------------------------------------------------- #
# Place-aware surface #1: staff API _customer_bottle_balance (driver-facing,
# clamps to >= 0, returns float).
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestCustomerBottleBalanceSurface:
    def test_equals_positive_place_balance_as_float(self, db):
        """_customer_bottle_balance == float(place balance) when positive."""
        u1 = _user(db, "a@example.com", "+998900001015")
        u2 = _user(db, "b@example.com", "+998900001016")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "5.00")
        order = _order(db, u2.id, "ORD-CB-1", a2.id)  # ordered from phone-2

        place = BottleTrackingService.get_place_balance(a2.id)
        result = _customer_bottle_balance(order)

        assert result == 5.0
        assert result == max(0.0, float(place))
        assert isinstance(result, float)

    def test_negative_place_balance_clamps_to_zero(self, db):
        """A negative (over-credited) place reads as 0.0 to the driver — never negative."""
        u1 = _user(db, "a@example.com", "+998900001017")
        u2 = _user(db, "b@example.com", "+998900001018")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "-3.00")
        order = _order(db, u2.id, "ORD-CB-2", a2.id)

        # Raw place balance is genuinely negative; the surface clamps it.
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("-3.00")
        assert _customer_bottle_balance(order) == 0.0

    def test_no_delivery_address_returns_zero(self, db):
        """Order without a delivery address => 0.0 (no place to read)."""
        u = _user(db, "a@example.com", "+998900001019")
        order = _order(db, u.id, "ORD-CB-3", None)

        assert _customer_bottle_balance(order) == 0.0


# --------------------------------------------------------------------------- #
# Place-aware surface #2: get_order_bottle_summary["balance"] (webhook-facing,
# raw Decimal, NOT clamped).
# --------------------------------------------------------------------------- #

@pytest.mark.integration
class TestOrderBottleSummaryBalanceSurface:
    def test_balance_equals_place_balance_decimal(self, db):
        """summary['balance'] == the place balance at the order's address, Decimal."""
        u1 = _user(db, "a@example.com", "+998900001020")
        u2 = _user(db, "b@example.com", "+998900001021")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "5.00")
        order = _order(db, u2.id, "ORD-OS-1", a2.id)

        summary = BottleTrackingService.get_order_bottle_summary(order)

        assert summary["balance"] == Decimal("5.00")
        assert summary["balance"] == BottleTrackingService.get_place_balance(a2.id)
        assert isinstance(summary["balance"], Decimal)

    def test_balance_preserves_negative_place_balance_unclamped(self, db):
        """Unlike the driver surface, the summary keeps a negative place verbatim."""
        u1 = _user(db, "a@example.com", "+998900001022")
        u2 = _user(db, "b@example.com", "+998900001023")
        group = _canonical_group(db, u1, u2)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "-3.00")
        order = _order(db, u2.id, "ORD-OS-2", a2.id)

        summary = BottleTrackingService.get_order_bottle_summary(order)

        assert summary["balance"] == Decimal("-3.00")  # NOT clamped to 0
        assert isinstance(summary["balance"], Decimal)

    def test_balance_is_the_address_place_when_ungrouped(self, db):
        """Ungrouped order address => the address IS the place."""
        u = _user(db, "a@example.com", "+998900001024")
        a = _addr(db, u.id)  # ungrouped
        _bal(db, a, "4.00")
        order = _order(db, u.id, "ORD-OS-3", a.id)

        summary = BottleTrackingService.get_order_bottle_summary(order)

        assert summary["balance"] == Decimal("4.00")
        assert isinstance(summary["balance"], Decimal)
