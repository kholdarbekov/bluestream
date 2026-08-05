"""Spec §5.7 — unlink releases prepaid reservations that no longer resolve.

Credit is fungible across ONE person's cluster, so a reservation may be funded
by account A and parked on sibling account B's pending order. Once an admin
splits the cluster, that reservation is out of scope and must be released back
to its owner's unapplied balance. Applied allocations are immutable history and
are never rewritten.
"""
from decimal import Decimal

import pytest

from business_app.models.payment import CashCollectionAllocation
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import OrderStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_user,
)


def _reservation_count(payment_id):
    return (
        CashCollectionAllocation.query.filter_by(
            payment_id=payment_id, allocation_mode="prepaid_reservation"
        )
        .filter(CashCollectionAllocation.reversed_at.is_(None))
        .count()
    )


def _reserved_projection(payment):
    return Decimal(
        str((payment.provider_data or {}).get("cod_prepayment_reserved_amount", 0))
    )


@pytest.mark.unit
class TestReleaseOutOfScopeReservations:
    def _seed_cross_reservation(self, db):
        """u1's over-collection reserved against sibling u2's pending order."""
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        own_order, _ = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        _, pending_payment = delivered_cod_order(
            db, u2, total=Decimal("4000.00"), status=OrderStatus.CONFIRMED
        )
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("9000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="overpaid",
        )
        assert _reservation_count(pending_payment.id) == 1
        return u1, u2, event, pending_payment

    def test_releases_cross_account_reservations_both_directions(self, db):
        u1, u2, event, pending_payment = self._seed_cross_reservation(db)
        svc = CashCollectionService()
        released = svc.release_out_of_scope_reservations([u2.id], [u1.id])
        assert released == 1
        assert _reservation_count(pending_payment.id) == 0
        # Credit returned to the source event's unapplied balance.
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("4000.00")
        # Projection resynced on the affected payment.
        db.session.refresh(pending_payment)
        assert _reserved_projection(pending_payment) == Decimal("0.00")

    def test_releases_when_leaving_and_remaining_are_swapped(self, db):
        """Direction-agnostic: the funder may be the one leaving, or the one
        staying — either way the pair straddles the split."""
        u1, u2, event, pending_payment = self._seed_cross_reservation(db)
        released = CashCollectionService().release_out_of_scope_reservations(
            [u1.id], [u2.id]
        )
        assert released == 1
        assert _reservation_count(pending_payment.id) == 0
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("4000.00")

    def test_own_account_reservations_untouched(self, db):
        u, admin = make_user(db), make_user(db)
        own_order, _ = delivered_cod_order(db, u, total=Decimal("5000.00"))
        _, own_pending = delivered_cod_order(
            db, u, total=Decimal("4000.00"), status=OrderStatus.CONFIRMED
        )
        CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("9000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="overpaid",
        )
        assert _reservation_count(own_pending.id) == 1
        db.session.refresh(own_pending)
        projection_before = _reserved_projection(own_pending)
        released = CashCollectionService().release_out_of_scope_reservations(
            [u.id], [999999]
        )
        # Same user on both sides of the event/payment -> stays in scope.
        assert released == 0
        assert _reservation_count(own_pending.id) == 1
        db.session.refresh(own_pending)
        assert _reserved_projection(own_pending) == projection_before
        assert projection_before == Decimal("4000.00")

    def test_reservations_of_an_untouched_cluster_are_not_released(self, db):
        """A third party's cross-account reservation is none of this unlink's
        business — the filter must be scoped to the splitting cluster."""
        u1, u2, _event, pending_payment = self._seed_cross_reservation(db)
        other_a, other_b, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [other_a, other_b])
        other_order, _ = delivered_cod_order(db, other_a, total=Decimal("5000.00"))
        _, other_pending = delivered_cod_order(
            db, other_b, total=Decimal("4000.00"), status=OrderStatus.CONFIRMED
        )
        CashCollectionService().post_collection(
            customer_id=other_a.id,
            amount=Decimal("9000.00"),
            source="standalone_meeting",
            order_id=other_order.id,
            recorded_by_user_id=admin.id,
            notes="overpaid",
        )
        assert _reservation_count(other_pending.id) == 1

        released = CashCollectionService().release_out_of_scope_reservations(
            [u2.id], [u1.id]
        )

        assert released == 1
        assert _reservation_count(pending_payment.id) == 0
        assert _reservation_count(other_pending.id) == 1

    def test_is_idempotent(self, db):
        u1, u2, event, pending_payment = self._seed_cross_reservation(db)
        svc = CashCollectionService()
        assert svc.release_out_of_scope_reservations([u2.id], [u1.id]) == 1
        # Second call finds nothing live -> no double credit-back.
        assert svc.release_out_of_scope_reservations([u2.id], [u1.id]) == 0
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("4000.00")
        assert _reservation_count(pending_payment.id) == 0

    def test_conservation_live_allocations_plus_unapplied_equals_event_amount(self, db):
        u1, u2, event, _ = self._seed_cross_reservation(db)
        CashCollectionService().release_out_of_scope_reservations([u2.id], [u1.id])
        db.session.refresh(event)
        live_total = sum(
            (
                Decimal(str(a.allocated_amount))
                for a in CashCollectionAllocation.query.filter(
                    CashCollectionAllocation.cash_collection_event_id == event.id,
                    CashCollectionAllocation.reversed_at.is_(None),
                ).all()
            ),
            Decimal("0.00"),
        )
        assert live_total + Decimal(str(event.unapplied_amount)) == Decimal(
            str(event.amount)
        )

    def test_applied_allocations_never_touched(self, db):
        u1, u2, event, _ = self._seed_cross_reservation(db)
        applied_before = (
            CashCollectionAllocation.query.filter(
                CashCollectionAllocation.cash_collection_event_id == event.id,
                CashCollectionAllocation.allocation_mode != "prepaid_reservation",
                CashCollectionAllocation.reversed_at.is_(None),
            ).count()
        )
        CashCollectionService().release_out_of_scope_reservations([u2.id], [u1.id])
        applied_after = (
            CashCollectionAllocation.query.filter(
                CashCollectionAllocation.cash_collection_event_id == event.id,
                CashCollectionAllocation.allocation_mode != "prepaid_reservation",
                CashCollectionAllocation.reversed_at.is_(None),
            ).count()
        )
        assert applied_after == applied_before
        assert applied_before >= 1

    def test_unlink_account_releases_cross_reservations_end_to_end(self, db):
        """Wiring pin: the 2a hook must actually delegate — a green
        release_out_of_scope_reservations with a `return 0` hook ships spec
        §5.7 dead."""
        from business_app.services.customer_link_service import CustomerLinkService

        u1, u2, event, pending_payment = self._seed_cross_reservation(db)
        assert _reservation_count(pending_payment.id) == 1

        CustomerLinkService().unlink_account(u2.id, actor_admin_id=None, reason="left")

        assert _reservation_count(pending_payment.id) == 0
        db.session.refresh(event)
        assert Decimal(str(event.unapplied_amount)) == Decimal("4000.00")
        db.session.refresh(pending_payment)
        assert _reserved_projection(pending_payment) == Decimal("0.00")
