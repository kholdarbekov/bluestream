"""Coverage for ``BottleTrackingService.get_cluster_ledger`` (address-group /
multi-phone customer linking, admin combined ledger view).

Scope:
    * A linked cluster (u1 + u2 sharing one CanonicalCustomer) where BOTH
      members have their own ``BottleLedger`` entries -> ``get_cluster_ledger``
      called with EITHER member's id returns entries from BOTH members,
      newest first, paginated with the same shape as the sibling ledger reads
      (``get_all_ledger_entries`` / ``get_address_ledger``).
    * An unlinked user (singleton cluster, per ``CustomerLinkService.
      get_cluster_user_ids`` returning ``[user_id]``) returns only their own
      entries — no behavior change for the common (unlinked) case.

Each test builds its own users/addresses/group/orders via the function-scoped
``db`` fixture (create_all/drop_all per test), mirroring the sibling
``test_bottle_admin_view_group_gap.py`` / ``test_bottle_group_union_reads.py``
builders.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


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
    """Drive the +qty delivery primitive (writes a BottleLedger row) and persist."""
    BottleTrackingService().record_bottles_delivered(
        order_id=order_id, user_id=user_id, address_id=address_id, quantity=Decimal(str(qty))
    )
    db.session.commit()


@pytest.mark.integration
class TestClusterLedgerLinkedCluster:
    def test_returns_both_members_entries_newest_first(self, db):
        """A linked cluster's ledger combines both members' rows, newest first."""
        u1 = _user(db, "cl_a1@example.com", "+998912000001")
        u2 = _user(db, "cl_a2@example.com", "+998912000002")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)

        o1 = _order(db, u1.id, addr_u1.id, "ORD-CL-A1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 3)   # oldest
        o2 = _order(db, u2.id, addr_u2.id, "ORD-CL-A2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 5)   # newest

        result = BottleTrackingService.get_cluster_ledger(u1.id)

        assert result["total"] == 2
        assert len(result["items"]) == 2
        # Newest first: the u2 delivery (5) precedes the u1 delivery (3).
        assert result["items"][0].user_id == u2.id
        assert float(result["items"][0].quantity) == 5.0
        assert result["items"][1].user_id == u1.id
        assert float(result["items"][1].quantity) == 3.0
        # Pagination shape mirrors get_all_ledger_entries/get_address_ledger.
        assert result["page"] == 1
        assert result["per_page"] == 20
        assert result["pages"] == 1

    def test_symmetric_from_either_members_point_of_view(self, db):
        """Calling with u2's id returns the SAME combined set as u1's id."""
        u1 = _user(db, "cl_b1@example.com", "+998912000011")
        u2 = _user(db, "cl_b2@example.com", "+998912000012")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)

        o1 = _order(db, u1.id, addr_u1.id, "ORD-CL-B1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 4)
        o2 = _order(db, u2.id, addr_u2.id, "ORD-CL-B2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 6)

        from_u1 = BottleTrackingService.get_cluster_ledger(u1.id)
        from_u2 = BottleTrackingService.get_cluster_ledger(u2.id)

        ids_from_u1 = sorted(item.id for item in from_u1["items"])
        ids_from_u2 = sorted(item.id for item in from_u2["items"])
        assert ids_from_u1 == ids_from_u2
        assert from_u1["total"] == from_u2["total"] == 2

    def test_pagination_applies_across_the_combined_set(self, db):
        """per_page limits the combined (both-members) set, not each member separately."""
        u1 = _user(db, "cl_c1@example.com", "+998912000021")
        u2 = _user(db, "cl_c2@example.com", "+998912000022")
        _canonical, group = _link_into_group(db, u1, u2)
        addr_u1 = _addr(db, u1.id, group_id=group.id)
        addr_u2 = _addr(db, u2.id, group_id=group.id)

        # Three ledger-writing events across the two members (delivery + return + delivery).
        o1 = _order(db, u1.id, addr_u1.id, "ORD-CL-C1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 2)
        o2 = _order(db, u2.id, addr_u2.id, "ORD-CL-C2")
        _deliver(db, o2.id, u2.id, addr_u2.id, 3)
        BottleTrackingService().record_bottles_returned(
            u1.id, addr_u1.id, Decimal("1"), order_id=o1.id, delivery_id=None
        )
        db.session.commit()

        page1 = BottleTrackingService.get_cluster_ledger(u1.id, page=1, per_page=2)
        page2 = BottleTrackingService.get_cluster_ledger(u1.id, page=2, per_page=2)

        assert page1["total"] == 3
        assert page1["pages"] == 2
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 1
        # No overlap between the two pages.
        page1_ids = {i.id for i in page1["items"]}
        page2_ids = {i.id for i in page2["items"]}
        assert not (page1_ids & page2_ids)


@pytest.mark.integration
class TestClusterLedgerUnlinkedUser:
    def test_unlinked_user_returns_only_their_own_entries(self, db):
        """An unlinked user (singleton cluster) sees only their own ledger rows,
        even when another unrelated user has entries."""
        u1 = _user(db, "cl_d1@example.com", "+998912000031")
        u_other = _user(db, "cl_d2@example.com", "+998912000032")
        addr_u1 = _addr(db, u1.id)          # ungrouped
        addr_other = _addr(db, u_other.id)  # ungrouped, unrelated

        o1 = _order(db, u1.id, addr_u1.id, "ORD-CL-D1")
        _deliver(db, o1.id, u1.id, addr_u1.id, 4)
        o_other = _order(db, u_other.id, addr_other.id, "ORD-CL-D2")
        _deliver(db, o_other.id, u_other.id, addr_other.id, 9)

        result = BottleTrackingService.get_cluster_ledger(u1.id)

        assert result["total"] == 1
        assert len(result["items"]) == 1
        assert result["items"][0].user_id == u1.id
        assert float(result["items"][0].quantity) == 4.0
