"""The admin per-customer bottle view and dashboard, keyed by PLACE.

RENEGOTIATED — this file used to characterize a GAP, and the 2026-07-27 place
re-key CLOSED it. Every assertion here is therefore inverted rather than
deleted: the numbers under test are carried over verbatim, and what changed is
which answer is correct.

What it pinned before:
  * balances were stored PER ``(user_id, address_id)``; operational reads
    unioned across an ``AddressGroup`` while ``get_customer_summary`` stayed
    PER-USER — ``total_balance`` summed only that one user's rows, with the
    combined figure bolted on beside it as ``cluster_total_balance`` /
    ``group_union_balance``;
  * ``get_dashboard_stats`` was the STILL-OPEN half: ``total_bottles_out`` summed
    raw rows globally and ``top_debtors`` grouped by ``user_id``, so two
    coworkers at one office showed up as two debtors.

What it pins now:
  * a PLACE owns one balance row, so ``get_customer_summary`` reports
    ``place_balance`` per address and ``cluster_scopes`` per distinct place.
    ``total_balance`` and ``cluster_total_balance`` are GONE — summing a shared
    pool once per member reports the same bottles twice;
  * ``get_dashboard_stats`` counts ``places_with_balance`` and lists ``top_debtors``
    as places (``address_group_id`` / ``address_id``), never people.
  * the per-address list is still restricted to the viewer's OWN addresses —
    that property survived the re-key untouched.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


# --------------------------------------------------------------------------- #
# Builders — each test constructs its own users/addresses/group via the
# function-scoped ``db`` fixture (create_all/drop_all per test).
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


def _link_into_group(db, u1, u2):
    """Put u1 (primary) and u2 under one CanonicalCustomer + one AddressGroup.

    Returns (canonical, group). Mirrors the direct-model setup used by the
    existing customer_link / place-scope tests.
    """
    canonical = CanonicalCustomer(primary_user_id=u1.id)
    db.session.add(canonical)
    db.session.commit()
    u1.canonical_customer_id = canonical.id
    u2.canonical_customer_id = canonical.id
    db.session.commit()
    group = AddressGroup(canonical_customer_id=canonical.id, label="home")
    db.session.add(group)
    db.session.commit()
    return canonical, group


def _addr(db, user_id, group_id=None):
    a = UserAddress(
        user_id=user_id,
        full_address="x, Tashkent",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        address_group_id=group_id,
    )
    db.session.add(a)
    db.session.commit()
    return a


def _order(db, user_id, address_id, number):
    o = Order(
        user_id=user_id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("0"),
        delivery_fee=Decimal("0"),
        discount_amount=Decimal("0"),
        loyalty_discount=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=address_id,
        created_at=datetime.now(UTC),
    )
    db.session.add(o)
    db.session.commit()
    return o


def _deliver(db, order_id, user_id, address_id, qty):
    """Drive the +qty delivery primitive and persist (the wrapper only flushes)."""
    BottleTrackingService().record_bottles_delivered(
        order_id=order_id, user_id=user_id, address_id=address_id, quantity=Decimal(str(qty))
    )
    db.session.commit()


@pytest.mark.integration
class TestCustomerSummaryPerUserGap:
    def test_summary_place_balance_includes_the_grouped_coworkers_delivery(self, db):
        """INVERTED from ``test_summary_total_balance_excludes_grouped_other_user``.

        That test asserted u1's ``total_balance`` stayed at 3.0 after a +7
        delivery landed on the grouped coworker u2 — the gap. The bottles are at
        ONE place, so u1's view of it is now the full 10. ``total_balance`` and
        ``cluster_total_balance`` are gone; ``place_balance`` and
        ``cluster_scopes`` replace them. Quantities (3, +7, 10) unchanged.
        """
        u1 = _user(db, "gap_a1@example.com", "+998911000001")
        u2 = _user(db, "gap_a2@example.com", "+998911000002")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)  # same physical place, phone-2

        # The place already holds 3 empties (delivered on u1's order).
        o1 = _order(db, u1.id, addr_u1.id, "ORD-GAP-A1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 3)

        svc = BottleTrackingService()
        summary_before = svc.get_customer_summary(u1.id)
        assert summary_before["addresses"][0]["place_balance"] == 3.0
        assert "total_balance" not in summary_before
        assert "cluster_total_balance" not in summary_before

        # A NEW delivery of 7 lands on u2 at the grouped address.
        o2 = _order(db, u2.id, addr_u2.id, "ORD-GAP-A2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 7)

        # GAP CLOSED: u1's summary now reports the shared place's full 10.
        summary_after = svc.get_customer_summary(u1.id)
        grouped_entry = next(row for row in summary_after["addresses"] if row["address_id"] == addr_u1.id)
        assert grouped_entry["place_balance"] == 10.0
        assert grouped_entry["is_grouped"] is True
        assert grouped_entry["address_group_id"] == group.id

        # The place read agrees from either address (3 + 7 = 10).
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("10.00")
        assert BottleTrackingService.get_place_balance(addr_u2.id) == Decimal("10.00")

        # Cluster context is still on the same summary — as places, counted once.
        assert summary_after["is_linked"] is True
        assert summary_after["cluster_member_ids"] == sorted([u1.id, u2.id])
        assert summary_after["cluster_scopes"] == [
            {"address_group_id": group.id, "address_id": None, "balance": 10.0, "is_shared": True}
        ]

    def test_summary_addresses_list_shows_only_own_addresses(self, db):
        """UNCHANGED PROPERTY: the per-address breakdown lists only u1's OWN
        address rows, never the grouped coworker's.

        Only the balance figure was renegotiated — ``balance`` /
        ``group_union_balance`` / ``total_balance`` collapse into one
        ``place_balance``, which is the 13 the union used to report separately.
        """
        u1 = _user(db, "gap_b1@example.com", "+998911000011")
        u2 = _user(db, "gap_b2@example.com", "+998911000012")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)

        o1 = _order(db, u1.id, addr_u1.id, "ORD-GAP-B1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 4)
        o2 = _order(db, u2.id, addr_u2.id, "ORD-GAP-B2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 9)

        summary = BottleTrackingService().get_customer_summary(u1.id)

        listed_address_ids = {row["address_id"] for row in summary["addresses"]}
        assert listed_address_ids == {addr_u1.id}      # only u1's own address
        assert addr_u2.id not in listed_address_ids    # grouped other-user addr absent
        assert len(summary["addresses"]) == 1
        assert summary["addresses"][0]["place_balance"] == 13.0   # 4 + 9, one pool
        assert summary["cluster_scopes"] == [
            {"address_group_id": group.id, "address_id": None, "balance": 13.0, "is_shared": True}
        ]
        assert summary["is_linked"] is True

    def test_summary_is_symmetric_across_place_members(self, db):
        """INVERTED from ``test_summary_symmetric_gap_for_the_other_member``.

        Symmetry used to mean "each member sees only its own slice" (6 and 2).
        It now means the stronger thing: both members see the SAME place, 8.
        """
        u1 = _user(db, "gap_c1@example.com", "+998911000021")
        u2 = _user(db, "gap_c2@example.com", "+998911000022")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)

        o1 = _order(db, u1.id, addr_u1.id, "ORD-GAP-C1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 6)
        o2 = _order(db, u2.id, addr_u2.id, "ORD-GAP-C2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 2)

        svc = BottleTrackingService()
        # Each member's own address row reports the shared place, 6 + 2 = 8.
        assert svc.get_customer_summary(u1.id)["addresses"][0]["place_balance"] == 8.0
        assert svc.get_customer_summary(u2.id)["addresses"][0]["place_balance"] == 8.0
        assert BottleTrackingService.get_place_balance(addr_u1.id) == Decimal("8.00")
        # Both members' cluster_scopes agree, and each lists the place ONCE.
        expected = [{"address_group_id": group.id, "address_id": None, "balance": 8.0, "is_shared": True}]
        assert svc.get_customer_summary(u1.id)["cluster_scopes"] == expected
        assert svc.get_customer_summary(u2.id)["cluster_scopes"] == expected


@pytest.mark.integration
class TestDashboardStatsPerUserGap:
    def test_total_bottles_out_sums_places_not_rows_per_person(self, db):
        """INVERTED from ``test_total_bottles_out_sums_raw_positive_rows_globally``.

        The global TOTAL is unchanged at 15 — bottles do not appear or vanish.
        What changed is the row COUNT: the two grouped members used to be two
        positive rows (``customers_with_balance == 3``) and are now ONE place
        holding 13 (``places_with_balance == 2``). That collapse is spec §1.1.
        """
        u1 = _user(db, "gap_d1@example.com", "+998911000031")
        u2 = _user(db, "gap_d2@example.com", "+998911000032")
        u3 = _user(db, "gap_d3@example.com", "+998911000033")  # unrelated, ungrouped
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)
        addr_u3 = _addr(db, u3.id)  # ungrouped

        o1 = _order(db, u1.id, addr_u1.id, "ORD-GAP-D1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 5)
        o2 = _order(db, u2.id, addr_u2.id, "ORD-GAP-D2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 8)
        o3 = _order(db, u3.id, addr_u3.id, "ORD-GAP-D3")
        _deliver(db, o3.id, u3.id, addr_u3.id, 2)

        stats = BottleTrackingService.get_dashboard_stats()

        # (5 + 8) + 2 = 15 — same total, now over TWO places instead of three rows.
        assert stats["total_bottles_out"] == 15.0
        assert stats["places_with_balance"] == 2
        assert "customers_with_balance" not in stats
        assert BottleBalance.query.count() == 2

    def test_top_debtors_are_places_not_people(self, db):
        """INVERTED from ``test_top_debtors_grouped_by_user_not_by_cluster``.

        Two members of ONE place used to appear as two debtor entries of 5 and
        8, and the test explicitly asserted no entry carried the merged 13. The
        merged 13 is now the correct and only answer — a driver chasing empties
        goes to one door, not two — and debtor entries no longer carry
        ``user_id`` at all, because a place has no owner.
        """
        u1 = _user(db, "gap_e1@example.com", "+998911000041")
        u2 = _user(db, "gap_e2@example.com", "+998911000042")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)

        o1 = _order(db, u1.id, addr_u1.id, "ORD-GAP-E1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 5)
        o2 = _order(db, u2.id, addr_u2.id, "ORD-GAP-E2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 8)

        stats = BottleTrackingService.get_dashboard_stats()

        assert len(stats["top_debtors"]) == 1
        debtor = stats["top_debtors"][0]
        assert debtor["address_group_id"] == group.id
        assert debtor["address_id"] is None
        assert debtor["total_balance"] == 13.0   # the merged place figure
        assert "user_id" not in debtor           # a place has no owner
