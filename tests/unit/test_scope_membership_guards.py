from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.services.allocation_scope import AllocationScope
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.exceptions import ValidationError
from shared.enums import DeliveryStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


def _attach_delivery(db, order):
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(UTC),
        delivered_at=datetime.now(UTC),
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
class TestScopeMembershipGuards:
    def test_personal_scope_rejects_foreign_order_same_message(self, db):
        u, stranger, admin = make_user(db), make_user(db), make_user(db)
        order, _ = delivered_cod_order(db, stranger)
        with pytest.raises(ValidationError, match="Order does not belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u.id,
                amount=Decimal("15000.00"),
                source="standalone_meeting",
                order_id=order.id,
                recorded_by_user_id=admin.id,
                notes="cash",
            )

    def test_cluster_scope_accepts_sibling_account_order(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, payment = delivered_cod_order(db, u2)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash from sibling phone",
        )
        assert event.scope_type == "cluster"
        db.session.refresh(payment)
        assert Decimal(str(payment.outstanding_amount)) == Decimal("0.00")

    def test_place_scope_accepts_coworker_order_at_grouped_address(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        # Coworker u2's delivered order at the grouped place; posting customer u1.
        order, payment = delivered_cod_order(db, u2, address=a2)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert event.scope_type == "place"

    def test_non_member_customer_rejected_for_grouped_stranger_order(self, db):
        """Circular-authorisation pin: PLACE scope must NOT be resolvable for a
        customer who is not at the place. Otherwise the scope-membership guard
        authorises itself and ANY stranger's order delivered to a grouped
        address becomes spendable with an unrelated customer's cash."""
        stranger, coworker, outsider, admin = (
            make_user(db),
            make_user(db),
            make_user(db),
            make_user(db),
        )
        a1, a2 = make_address(db, stranger), make_address(db, coworker)
        make_place_group(db, a1, a2)
        # `outsider` owns no address in the group.
        order, _ = delivered_cod_order(db, stranger, address=a1)
        with pytest.raises(ValidationError, match="Order does not belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=outsider.id,
                amount=Decimal("15000.00"),
                source="standalone_meeting",
                order_id=order.id,
                recorded_by_user_id=admin.id,
                notes="not my office",
            )

    def test_cluster_scope_still_rejects_stranger_order(self, db):
        """The order guard must still REJECT under a non-personal scope — a
        cluster widens the universe to the siblings, not to everyone."""
        u1, u2, stranger, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, payment = delivered_cod_order(db, stranger)
        with pytest.raises(ValidationError, match="Order does not belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u1.id,
                amount=Decimal("15000.00"),
                source="standalone_meeting",
                order_id=order.id,
                recorded_by_user_id=admin.id,
                notes="not my cluster",
            )
        db.session.refresh(payment)
        assert Decimal(str(payment.outstanding_amount)) == Decimal("15000.00")

    def test_place_scope_still_rejects_order_owned_outside_the_place(self, db):
        """PLACE scope is not a licence to touch anyone: the payer holds a real
        membership (so the scope mints), but the target order belongs to a
        stranger AND was delivered nowhere near the group's addresses."""
        u1, coworker, stranger, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, coworker)
        make_place_group(db, a1, a2)
        foreign_address = make_address(db, stranger)  # ungrouped
        order, foreign_payment = delivered_cod_order(db, stranger, address=foreign_address)
        # Mint a genuine PLACE scope for u1, then aim the guard at the stranger's
        # order — the frozen-scope replay path (spec 5.6) can present exactly this
        # pairing, so the guard, not the resolver, must be the one that refuses.
        scope = CashCollectionService().resolve_allocation_scope(
            u1.id, delivery_address_id=a1.id, source="standalone_meeting"
        )
        assert scope.scope_type == "place"
        with pytest.raises(ValidationError, match="Order does not belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u1.id,
                amount=Decimal("15000.00"),
                source="standalone_meeting",
                order_id=order.id,
                recorded_by_user_id=admin.id,
                notes="stranger order, stranger address",
                replay_scope=scope,
            )
        db.session.refresh(foreign_payment)
        assert Decimal(str(foreign_payment.outstanding_amount)) == Decimal("15000.00")

    def test_cluster_scope_still_rejects_stranger_manual_allocation(self, db):
        u1, u2, stranger, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _, foreign_payment = delivered_cod_order(db, stranger)
        with pytest.raises(ValidationError, match="Manual allocations must belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u1.id,
                amount=Decimal("15000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin.id,
                notes="manual",
                manual_allocations=[{"payment_id": foreign_payment.id, "amount": Decimal("15000.00")}],
            )

    def test_personal_scope_rejects_foreign_manual_allocation_same_message(self, db):
        u, stranger, admin = make_user(db), make_user(db), make_user(db)
        _, foreign_payment = delivered_cod_order(db, stranger)
        with pytest.raises(ValidationError, match="Manual allocations must belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u.id,
                amount=Decimal("15000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin.id,
                notes="manual",
                manual_allocations=[{"payment_id": foreign_payment.id, "amount": Decimal("15000.00")}],
            )
        db.session.refresh(foreign_payment)
        assert Decimal(str(foreign_payment.outstanding_amount)) == Decimal("15000.00")

    def test_cluster_scope_accepts_sibling_manual_allocation(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _, sibling_payment = delivered_cod_order(db, u2)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="admin_adjustment",
            recorded_by_user_id=admin.id,
            notes="settle the sibling phone's debt",
            manual_allocations=[{"payment_id": sibling_payment.id, "amount": Decimal("15000.00")}],
        )
        assert event.scope_type == "cluster"
        db.session.refresh(sibling_payment)
        assert Decimal(str(sibling_payment.outstanding_amount)) == Decimal("0.00")

    def test_personal_scope_rejects_foreign_delivery_same_message(self, db):
        u, stranger, admin = make_user(db), make_user(db), make_user(db)
        order, _ = delivered_cod_order(db, stranger)
        delivery = _attach_delivery(db, order)
        with pytest.raises(ValidationError, match="Delivery does not belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u.id,
                amount=Decimal("15000.00"),
                source="delivery_completion",
                delivery_id=delivery.id,
                recorded_by_user_id=admin.id,
                notes="cash",
            )

    def test_cluster_scope_still_rejects_stranger_delivery(self, db):
        u1, u2, stranger, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, _ = delivered_cod_order(db, stranger)
        delivery = _attach_delivery(db, order)
        with pytest.raises(ValidationError, match="Delivery does not belong to the selected customer"):
            CashCollectionService().post_collection(
                customer_id=u1.id,
                amount=Decimal("15000.00"),
                source="delivery_completion",
                delivery_id=delivery.id,
                recorded_by_user_id=admin.id,
                notes="cash",
            )

    def test_cluster_scope_accepts_sibling_delivery(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, _ = delivered_cod_order(db, u2)
        delivery = _attach_delivery(db, order)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="delivery_completion",
            delivery_id=delivery.id,
            recorded_by_user_id=admin.id,
            notes="cash at the sibling's door",
        )
        assert event.scope_type == "cluster"
        assert event.delivery_id == delivery.id


@pytest.mark.unit
class TestScopeCoversOrderHelper:
    """Direct unit coverage of `_scope_covers_order` — Task 7 reuses it, and each
    arm must be able to answer BOTH True and False (an always-true arm is worse
    than no guard at all)."""

    def test_personal_scope_covers_only_its_own_order(self, db):
        owner, stranger = make_user(db), make_user(db)
        own_order, _ = delivered_cod_order(db, owner)
        foreign_order, _ = delivered_cod_order(db, stranger)
        scope = AllocationScope.personal(owner.id)
        assert CashCollectionService._scope_covers_order(scope, own_order) is True
        assert CashCollectionService._scope_covers_order(scope, foreign_order) is False

    def test_cluster_scope_covers_siblings_only(self, db):
        u1, u2, stranger = make_user(db), make_user(db), make_user(db)
        sibling_order, _ = delivered_cod_order(db, u2)
        foreign_order, _ = delivered_cod_order(db, stranger)
        scope = AllocationScope.cluster([u1.id, u2.id])
        assert CashCollectionService._scope_covers_order(scope, sibling_order) is True
        assert CashCollectionService._scope_covers_order(scope, foreign_order) is False

    def test_place_scope_address_arm_covers_member_addresses_only(self, db):
        member, coworker, stranger = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, member), make_address(db, coworker)
        group = make_place_group(db, a1, a2)
        outside_address = make_address(db, stranger)
        at_place, _ = delivered_cod_order(db, stranger, address=a2)
        elsewhere, _ = delivered_cod_order(db, stranger, address=outside_address)
        addressless, _ = delivered_cod_order(db, stranger)
        scope = AllocationScope.place(
            group_id=group.id,
            address_ids=[a1.id, a2.id],
            place_user_ids=[member.id, coworker.id],
            orderer_cluster_user_ids=[member.id],
        )
        # Address arm: a stranger's order delivered INTO the place is covered...
        assert CashCollectionService._scope_covers_order(scope, at_place) is True
        # ...but the same stranger's order delivered anywhere else is not,
        # and neither is an order with no delivery address at all.
        assert CashCollectionService._scope_covers_order(scope, elsewhere) is False
        assert CashCollectionService._scope_covers_order(scope, addressless) is False

    def test_cluster_arm_of_place_scope_is_the_orderer_cluster_not_the_place(self, db):
        """PLACE freezes two different member lists; the cluster arm must read
        `orderer_cluster_user_ids`. A place co-member who is NOT in the payer's
        cluster is reachable only through the address arm."""
        member, coworker = make_user(db), make_user(db)
        a1, a2 = make_address(db, member), make_address(db, coworker)
        group = make_place_group(db, a1, a2)
        # Coworker's order delivered to their OWN, non-grouped second address.
        coworker_other_address = make_address(db, coworker)
        order, _ = delivered_cod_order(db, coworker, address=coworker_other_address)
        scope = AllocationScope.place(
            group_id=group.id,
            address_ids=[a1.id, a2.id],
            place_user_ids=[member.id, coworker.id],
            orderer_cluster_user_ids=[member.id],
        )
        assert CashCollectionService._scope_covers_order(scope, order) is False
