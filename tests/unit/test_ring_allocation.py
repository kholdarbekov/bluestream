"""Ring allocation (Plan 2b Task 4) — place ring 1 + orderer-cluster ring 2.

Spec: docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md §5.2/§5.3.

The money assertions here are the point: several of these paths already stamped the
right ``scope_type`` before this task while quietly leaving the target's debt
outstanding and parking the cash as the poster's prepaid credit. Every test below
asserts where the MONEY landed, not merely which scope was resolved.
"""
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent, Payment
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


def _outstanding(db, payment):
    db.session.refresh(payment)
    return Decimal(str(payment.outstanding_amount))


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


def _live_allocations(db, event):
    db.session.refresh(event)
    return [a for a in event.allocations if a.reversed_at is None]


def _assert_conserved(db, event):
    """SUM(live allocations) + unapplied == event.amount (global constraint)."""
    db.session.refresh(event)
    allocated = sum(
        (Decimal(str(a.allocated_amount)) for a in _live_allocations(db, event)),
        Decimal("0.00"),
    )
    assert allocated + Decimal(str(event.unapplied_amount)) == Decimal(str(event.amount))


@pytest.mark.unit
class TestPlaceRingAllocation:
    def test_place_scope_settles_pure_oldest_first_across_owners(self, db):
        """Decision 6: no 'own order' at a workplace — the just-delivered order
        participates by age with no special ranking."""
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        t0 = datetime.now(UTC) - timedelta(days=3)
        old_order, old_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("10000.00"), created_at=t0
        )
        new_order, new_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("10000.00"), created_at=t0 + timedelta(days=1)
        )
        # Collect exactly the NEW order's total, posted for its orderer u1:
        # the OLDER coworker debt settles first (pure oldest-first).
        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=new_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert _outstanding(db, old_payment) == Decimal("0.00")
        assert _outstanding(db, new_payment) == Decimal("10000.00")

    def test_place_surplus_spills_to_orderer_cluster_debt_then_credit(self, db):
        u1, u2, sibling, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, sibling])
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        home = make_address(db, sibling)
        place_order, place_payment = delivered_cod_order(db, u1, address=a1, total=Decimal("10000.00"))
        _, sibling_payment = delivered_cod_order(db, sibling, address=home, total=Decimal("5000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("18000.00"),
            source="standalone_meeting",
            order_id=place_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash overpaid",
        )
        # Ring 1: place debt settles; ring 2: orderer-cluster sibling home debt;
        # remainder = orderer's prepaid credit (place groups never pool credit).
        assert _outstanding(db, place_payment) == Decimal("0.00")
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("3000.00")

    def test_place_surplus_never_pays_unrelated_coworker_home_debt(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        coworker_home = make_address(db, u2)
        place_order, place_payment = delivered_cod_order(db, u1, address=a1, total=Decimal("10000.00"))
        _, coworker_home_payment = delivered_cod_order(db, u2, address=coworker_home, total=Decimal("5000.00"))
        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("14000.00"),
            source="standalone_meeting",
            order_id=place_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        # Coworker's HOME debt is neither place ring 1 nor u1's cluster ring 2.
        assert _outstanding(db, place_payment) == Decimal("0.00")
        assert _outstanding(db, coworker_home_payment) == Decimal("5000.00")

    def test_ring1_place_debt_settles_before_an_older_ring2_cluster_debt(self, db):
        """Ring ORDER, not just ring membership.

        Rings are a priority list, NOT one flat oldest-first list: a place debt
        outranks an older cluster debt. Without ring precedence this test's cash
        would settle the 30-day-old sibling debt instead of the office debt.
        """
        u1, u2, sibling, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, sibling])
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        home = make_address(db, sibling)
        very_old = datetime.now(UTC) - timedelta(days=30)
        _, old_sibling_payment = delivered_cod_order(
            db, sibling, address=home, total=Decimal("7000.00"), created_at=very_old
        )
        place_order, place_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("7000.00"), created_at=datetime.now(UTC)
        )
        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("7000.00"),
            source="standalone_meeting",
            order_id=place_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert _outstanding(db, place_payment) == Decimal("0.00")
        assert _outstanding(db, old_sibling_payment) == Decimal("7000.00")

    def test_place_allocation_stamps_source_and_beneficiary(self, db):
        """Cross-owner allocations are the only record of who paid for whom."""
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("6000.00")
        )
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("6000.00"),
            source="standalone_meeting",
            order_id=coworker_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert _outstanding(db, coworker_payment) == Decimal("0.00")
        allocs = _live_allocations(db, event)
        assert len(allocs) == 1
        assert allocs[0].source_customer_id == u1.id
        assert allocs[0].beneficiary_user_id == u2.id
        _assert_conserved(db, event)


@pytest.mark.unit
class TestClusterRingAllocation:
    def test_cluster_scope_keeps_current_order_last_convention(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=2)
        _, old_sibling_payment = delivered_cod_order(
            db, u2, total=Decimal("8000.00"), created_at=t0
        )
        current_order, current_payment = delivered_cod_order(
            db, u1, total=Decimal("8000.00"), created_at=t0 + timedelta(days=1)
        )
        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("8000.00"),
            source="standalone_meeting",
            order_id=current_order.id,
            recorded_by_user_id=admin.id,
            notes="cash",
        )
        # As-built convention preserved: sibling's OLDER debt settles first
        # (oldest-first over the cluster; the current order is newest).
        assert _outstanding(db, old_sibling_payment) == Decimal("0.00")
        assert _outstanding(db, current_payment) == Decimal("8000.00")

    def test_personal_scope_unchanged_single_allocation(self, db):
        u, admin = make_user(db), make_user(db)
        order, payment = delivered_cod_order(db, u)
        CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash",
        )
        assert _outstanding(db, payment) == Decimal("0.00")
        allocs = CashCollectionAllocation.query.filter_by(payment_id=payment.id).all()
        assert len(allocs) == 1

    def test_personal_scope_never_touches_an_unlinked_strangers_debt(self, db):
        """The unlinked/ungrouped regression baseline: surplus becomes credit,
        it does not wander onto anybody else's debt."""
        u, stranger, admin = make_user(db), make_user(db), make_user(db)
        order, payment = delivered_cod_order(db, u, total=Decimal("10000.00"))
        _, stranger_payment = delivered_cod_order(db, stranger, total=Decimal("5000.00"))
        event = CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash",
        )
        assert _outstanding(db, payment) == Decimal("0.00")
        assert _outstanding(db, stranger_payment) == Decimal("5000.00")
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("5000.00")

    def test_cluster_surplus_settles_sibling_then_credits_the_poster(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=2)
        current_order, current_payment = delivered_cod_order(
            db, u1, total=Decimal("6000.00"), created_at=t0 + timedelta(days=1)
        )
        _, sibling_payment = delivered_cod_order(db, u2, total=Decimal("4000.00"), created_at=t0)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("12000.00"),
            source="standalone_meeting",
            order_id=current_order.id,
            recorded_by_user_id=admin.id,
            notes="cash",
        )
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        assert _outstanding(db, current_payment) == Decimal("0.00")
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("2000.00")
        _assert_conserved(db, event)


@pytest.mark.unit
class TestDeliveryPathMoneyMoves:
    """Task-3 handoff #1 — the delivery path had no current-order tail, so a
    delivery-only post against a linked/coworker order settled NOTHING and parked
    the cash as the poster's prepaid credit. Scope-only assertions never saw it.
    """

    def test_cluster_delivery_post_settles_the_siblings_debt(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, sibling_payment = delivered_cod_order(db, u2, total=Decimal("15000.00"))
        delivery = _attach_delivery(db, order)
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="delivery_completion",
            delivery_id=delivery.id,
            recorded_by_user_id=admin.id,
            notes="cash at the sibling's door",
        )
        assert event.scope_type == "cluster"
        # THE MONEY: the sibling's debt is gone and nothing was parked as credit.
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("0.00")
        allocs = _live_allocations(db, event)
        assert [(a.source_customer_id, a.beneficiary_user_id) for a in allocs] == [(u1.id, u2.id)]
        _assert_conserved(db, event)

    def test_place_delivery_post_settles_the_coworkers_debt(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("9000.00")
        )
        delivery = _attach_delivery(db, order)
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("9000.00"),
            source="delivery_completion",
            delivery_id=delivery.id,
            recorded_by_user_id=admin.id,
            notes="office cash at the door",
        )
        assert event.scope_type == "place"
        assert _outstanding(db, coworker_payment) == Decimal("0.00")
        assert svc.get_customer_prepaid_balance(u1.id) == Decimal("0.00")
        _assert_conserved(db, event)

    def test_delivery_post_never_collects_against_a_not_yet_delivered_order(self, db):
        """The deliberate boundary of the delivery-path fix.

        Both rings filter ``Order.status == DELIVERED``, so a delivery-only post
        whose order has NOT been delivered yet is settled by nobody. That is
        intentional: pre-delivery money is *reservable* forward-looking state
        (`RESERVABLE_ORDER_STATUSES`), never a collection. Hard-allocating it
        here would strand the cash on the payment if the order is later
        cancelled. Task 5 widens the residual RESERVATION sweep to the cluster —
        a reservation leaves `outstanding_amount` alone, so this pin survives it.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, pending_payment = delivered_cod_order(
            db, u2, total=Decimal("5000.00"), status=OrderStatus.CONFIRMED
        )
        delivery = _attach_delivery(db, order)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("5000.00"),
            source="next_delivery",
            delivery_id=delivery.id,
            recorded_by_user_id=admin.id,
            notes="cash for future delivery",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, pending_payment) == Decimal("5000.00")
        projecting = [
            a
            for a in _live_allocations(db, event)
            if a.payment_id == pending_payment.id
            and CashCollectionService._allocation_affects_payment_projection(a)
        ]
        assert projecting == []

    def test_personal_delivery_post_is_unchanged(self, db):
        """The same shape for an unlinked, ungrouped customer keeps behaving
        exactly as before: their own delivered debt settles."""
        u, admin = make_user(db), make_user(db)
        order, payment = delivered_cod_order(db, u, total=Decimal("15000.00"))
        delivery = _attach_delivery(db, order)
        event = CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="delivery_completion",
            delivery_id=delivery.id,
            recorded_by_user_id=admin.id,
            notes="cash",
        )
        assert event.scope_type == "personal"
        assert _outstanding(db, payment) == Decimal("0.00")


@pytest.mark.unit
class TestClusterSpillSemanticsPinned:
    """Task-3 handoff #2 — ADMIN_ADJUSTMENT, BACKFILL and PERSONAL_CARD_TRANSFER
    all stamp CLUSTER scope for a linked payer, so with ring 2 live their
    residuals now spill onto SIBLING accounts' debts.

    Per spec §5.1 (one wallet per real person) and UC1/UC2 that is the INTENDED
    one-wallet behaviour, not an accident of widening the allocator. These tests
    exist so a future reader sees it was a decision. PERSONAL scope keeps the
    per-account behaviour, pinned alongside each case.
    """

    def test_admin_adjustment_residual_spills_onto_a_sibling_debt(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _, own_payment = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        _, sibling_payment = delivered_cod_order(db, u2, total=Decimal("5000.00"))
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="admin_adjustment",
            recorded_by_user_id=admin.id,
            notes="book correction",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        _assert_conserved(db, event)

    def test_admin_adjustment_stays_per_account_when_unlinked(self, db):
        u, stranger, admin = make_user(db), make_user(db), make_user(db)
        _, own_payment = delivered_cod_order(db, u, total=Decimal("5000.00"))
        _, stranger_payment = delivered_cod_order(db, stranger, total=Decimal("5000.00"))
        event = CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="admin_adjustment",
            recorded_by_user_id=admin.id,
            notes="book correction",
        )
        assert event.scope_type == "personal"
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert _outstanding(db, stranger_payment) == Decimal("5000.00")
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("5000.00")

    def test_backfill_residual_spills_onto_a_sibling_debt(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _, own_payment = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        _, sibling_payment = delivered_cod_order(db, u2, total=Decimal("5000.00"))
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="backfill",
            recorded_by_user_id=admin.id,
            notes="historic cash backfill",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        _assert_conserved(db, event)

    def test_personal_card_transfer_target_first_then_sibling_spill(self, db):
        """The PCT target-first branch is untouched: the TARGET settles first even
        though an older sibling debt exists; only the residual spills."""
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=5)
        _, old_sibling_payment = delivered_cod_order(
            db, u2, total=Decimal("4000.00"), created_at=t0
        )
        target_order, target_payment = delivered_cod_order(
            db, u1, total=Decimal("6000.00"), created_at=t0 + timedelta(days=1)
        )
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="personal_card_transfer",
            order_id=target_order.id,
            recorded_by_user_id=admin.id,
            notes="card transfer",
        )
        assert event.scope_type == "cluster"
        allocs = sorted(_live_allocations(db, event), key=lambda a: a.allocation_order)
        # Target first (allocation_order 1), sibling second — never the reverse.
        assert allocs[0].payment_id == target_payment.id
        assert allocs[0].beneficiary_user_id == u1.id
        assert allocs[1].payment_id == old_sibling_payment.id
        assert allocs[1].beneficiary_user_id == u2.id
        assert _outstanding(db, target_payment) == Decimal("0.00")
        assert _outstanding(db, old_sibling_payment) == Decimal("0.00")
        _assert_conserved(db, event)

    def test_personal_card_transfer_unlinked_surplus_still_becomes_credit(self, db):
        u, stranger, admin = make_user(db), make_user(db), make_user(db)
        _, stranger_payment = delivered_cod_order(db, stranger, total=Decimal("4000.00"))
        target_order, target_payment = delivered_cod_order(db, u, total=Decimal("6000.00"))
        event = CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="personal_card_transfer",
            order_id=target_order.id,
            recorded_by_user_id=admin.id,
            notes="card transfer",
        )
        assert event.scope_type == "personal"
        assert _outstanding(db, target_payment) == Decimal("0.00")
        assert _outstanding(db, stranger_payment) == Decimal("4000.00")
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("4000.00")

    def test_grocery_customer_never_spills_onto_a_linked_account(self, db):
        """Grocery backstop (spec §5.8 layer 3) survives ring allocation."""
        grocery, sibling, admin = make_user(db, grocery=True), make_user(db), make_user(db)
        link_users(db, [grocery, sibling])
        _, own_payment = delivered_cod_order(db, grocery, total=Decimal("5000.00"))
        _, sibling_payment = delivered_cod_order(db, sibling, total=Decimal("5000.00"))
        event = CashCollectionService().post_collection(
            customer_id=grocery.id,
            amount=Decimal("10000.00"),
            source="admin_adjustment",
            recorded_by_user_id=admin.id,
            notes="grocery cash",
        )
        assert event.scope_type == "personal"
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert _outstanding(db, sibling_payment) == Decimal("5000.00")


@pytest.mark.unit
class TestScopedDebtQueryHelpers:
    def test_get_active_cod_payments_for_scope_orders_ring1_before_ring2(self, db):
        from business_app.services.allocation_scope import AllocationScope

        u1, u2, sibling = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        home = make_address(db, sibling)
        very_old = datetime.now(UTC) - timedelta(days=30)
        _, old_sibling_payment = delivered_cod_order(
            db, sibling, address=home, total=Decimal("7000.00"), created_at=very_old
        )
        _, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("3000.00"), created_at=datetime.now(UTC) - timedelta(days=2)
        )
        _, own_place_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("3000.00"), created_at=datetime.now(UTC) - timedelta(days=1)
        )
        scope = AllocationScope.place(
            group_id=a1.address_group_id,
            address_ids=[a1.id, a2.id],
            place_user_ids=[u1.id, u2.id],
            orderer_cluster_user_ids=[u1.id, sibling.id],
        )
        payments = CashCollectionService().get_active_cod_payments_for_scope(scope)
        assert [p.id for p in payments] == [
            coworker_payment.id,
            own_place_payment.id,
            old_sibling_payment.id,
        ]

    def test_get_active_cod_payments_for_scope_cluster_is_oldest_first(self, db):
        from business_app.services.allocation_scope import AllocationScope

        u1, u2 = make_user(db), make_user(db)
        t0 = datetime.now(UTC) - timedelta(days=4)
        _, older = delivered_cod_order(db, u2, total=Decimal("1000.00"), created_at=t0)
        _, newer = delivered_cod_order(db, u1, total=Decimal("1000.00"), created_at=t0 + timedelta(days=1))
        scope = AllocationScope.cluster([u1.id, u2.id])
        payments = CashCollectionService().get_active_cod_payments_for_scope(scope)
        assert [p.id for p in payments] == [older.id, newer.id]

    def test_scoped_debt_query_excludes_settled_and_non_delivered(self, db):
        from business_app.services.allocation_scope import AllocationScope

        u = make_user(db)
        _, settled = delivered_cod_order(db, u, total=Decimal("1000.00"), outstanding=Decimal("0.00"))
        _, pending = delivered_cod_order(
            db, u, total=Decimal("1000.00"), status=OrderStatus.CONFIRMED
        )
        _, live = delivered_cod_order(db, u, total=Decimal("1000.00"))
        payments = CashCollectionService().get_active_cod_payments_for_scope(
            AllocationScope.personal(u.id)
        )
        assert [p.id for p in payments] == [live.id]
        assert settled.id not in {p.id for p in payments}
        assert pending.id not in {p.id for p in payments}


@pytest.mark.unit
class TestMixedAwarenessOrdering:
    """The ring ordering must be derived in SQL, never by sorting DateTime
    columns in Python.

    Within one session those values are mixed-awareness: a row flushed but not
    reloaded keeps its tz-AWARE ``datetime.now(UTC)``, while a row reloaded from
    SQLite comes back NAIVE. A Python sort over Order.created_at therefore raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` on a
    live money path. This test builds exactly that session shape.
    """

    def test_ordering_survives_a_flushed_but_uncommitted_candidate(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        # Committed => reloaded NAIVE on access under SQLite.
        _, old_sibling_payment = delivered_cod_order(
            db, u2, total=Decimal("5000.00"), created_at=datetime.now(UTC) - timedelta(days=3)
        )
        # Flushed but NOT committed => stays tz-AWARE in the identity map.
        ts = datetime.now(UTC)
        order = Order(
            user_id=u1.id,
            order_number="ORD-2B-TZMIX",
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("5000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("5000.00"),
            payment_method=PaymentMethod.CASH,
            created_at=ts,
        )
        db.session.add(order)
        db.session.flush()
        fresh_payment = Payment(
            order_id=order.id,
            user_id=u1.id,
            payment_method=PaymentMethod.CASH,
            amount=Decimal("5000.00"),
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id="pay-2b-tzmix",
            amount_collected=Decimal("0.00"),
            outstanding_amount=Decimal("5000.00"),
            created_at=ts,
        )
        db.session.add(fresh_payment)
        db.session.flush()

        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            recorded_by_user_id=admin.id,
            notes="cash",
        )
        allocs = sorted(_live_allocations(db, event), key=lambda a: a.allocation_order)
        assert [a.payment_id for a in allocs] == [old_sibling_payment.id, fresh_payment.id]
        assert _outstanding(db, old_sibling_payment) == Decimal("0.00")
        assert _outstanding(db, fresh_payment) == Decimal("0.00")


@pytest.mark.unit
class TestRingAllocationConservation:
    def test_no_allocation_exceeds_the_event_or_the_payment(self, db):
        """Conservation across a 3-ring-1 + 1-ring-2 fan-out."""
        u1, u2, sibling, admin = make_user(db), make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, sibling])
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        home = make_address(db, sibling)
        t0 = datetime.now(UTC) - timedelta(days=6)
        payments = [
            delivered_cod_order(
                db, u2, address=a2, total=Decimal("3000.00"), created_at=t0
            )[1],
            delivered_cod_order(
                db, u1, address=a1, total=Decimal("3000.00"), created_at=t0 + timedelta(days=1)
            )[1],
            delivered_cod_order(
                db, u2, address=a2, total=Decimal("3000.00"), created_at=t0 + timedelta(days=2)
            )[1],
            delivered_cod_order(
                db, sibling, address=home, total=Decimal("3000.00"), created_at=t0 + timedelta(days=3)
            )[1],
        ]
        order = payments[1].order
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        db.session.refresh(event)
        allocs = _live_allocations(db, event)
        # 3 full ring-1 settlements + a 1000 partial on the ring-2 sibling debt.
        assert [Decimal(str(a.allocated_amount)) for a in allocs] == [
            Decimal("3000.00"),
            Decimal("3000.00"),
            Decimal("3000.00"),
            Decimal("1000.00"),
        ]
        assert [a.allocation_order for a in allocs] == [1, 2, 3, 4]
        for alloc, payment in zip(allocs, payments):
            assert alloc.payment_id == payment.id
        assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
        assert _outstanding(db, payments[3]) == Decimal("2000.00")
        _assert_conserved(db, event)

    def test_zero_amount_post_allocates_nothing(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _, sibling_payment = delivered_cod_order(db, u2, total=Decimal("5000.00"))
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("0.00"),
            source="standalone_meeting",
            recorded_by_user_id=admin.id,
            notes="nothing collected",
        )
        assert _live_allocations(db, event) == []
        assert _outstanding(db, sibling_payment) == Decimal("5000.00")
        assert CashCollectionEvent.query.get(event.id).unapplied_amount == Decimal("0.00")
