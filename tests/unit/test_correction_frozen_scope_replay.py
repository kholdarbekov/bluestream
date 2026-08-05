"""Corrections replay the FROZEN scope, except ring 3 (Plan 2b, spec §5.6).

``adjust_event_amount`` corrects a collection by voiding the original event and
re-posting a replacement. Two money-safety properties are pinned here:

1. **Frozen-scope replay (widening).** The replacement must allocate under the
   ORIGINAL event's frozen scope. Re-resolving from current topology fails OPEN:
   a link created between the collection and the correction silently re-routes
   the cash to a sibling's older debt, and the scope-membership guard cannot see
   it (the order's owner IS inside the widened scope).
2. **Ring-3 carve-out (narrowing).** The residual reservation sweep is carved
   out of that replay rule and always resolves the CURRENT cluster. Reservations
   are releasable, forward-looking state (§5.7), not history, so a correction
   made after an unlink must never re-create reservations on a departed
   sibling's pending orders.

Spec: docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md
"""
from datetime import datetime, timedelta, UTC
from decimal import Decimal

import pytest

from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import CashCollectionSource, OrderStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_user,
)


def _outstanding(db, payment):
    db.session.refresh(payment)
    return Decimal(str(payment.outstanding_amount))


def _live_reservations_for(payment_id):
    return (
        CashCollectionAllocation.query.filter(
            CashCollectionAllocation.payment_id == payment_id,
            CashCollectionAllocation.reversed_at.is_(None),
            CashCollectionAllocation.allocation_mode == "prepaid_reservation",
        ).all()
    )


def _assert_conserved(db, event):
    """SUM(live allocations) + unapplied == event.amount (global constraint)."""
    db.session.refresh(event)
    allocated = sum(
        (Decimal(str(a.allocated_amount)) for a in event.allocations if a.reversed_at is None),
        Decimal("0.00"),
    )
    assert allocated + Decimal(str(event.unapplied_amount)) == Decimal(str(event.amount))


@pytest.mark.unit
class TestCorrectionReplaysFrozenScope:
    def test_link_after_collection_does_not_reroute_the_correction(self, db):
        """Widening pin: personal collection + later LINK + correction.

        u1 pays cash against u1's own delivered order. An admin later links u1
        with u2 (who carries an OLDER open COD debt), then corrects the amount.
        The replacement must still land on u1's own order — u2's older debt is
        NOT part of the universe the cash was collected in.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        t0 = datetime.now(UTC) - timedelta(days=3)

        # u2's debt is OLDER, so an oldest-first cluster walk would take it first.
        _sibling_order, sibling_payment = delivered_cod_order(
            db, u2, total=Decimal("6000.00"), created_at=t0
        )
        own_order, own_payment = delivered_cod_order(
            db, u1, total=Decimal("10000.00"), created_at=t0 + timedelta(days=1)
        )

        service = CashCollectionService()
        event = service.post_collection(
            customer_id=u1.id,
            amount=Decimal("4000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="cash handed over at meeting",
        )
        assert event.scope_type == "personal"
        assert event.scope_snapshot is None
        assert _outstanding(db, own_payment) == Decimal("6000.00")
        assert _outstanding(db, sibling_payment) == Decimal("6000.00")

        # An admin links the two accounts AFTER the cash changed hands.
        link_users(db, [u1, u2])
        assert CustomerLinkService().get_cluster_user_ids(u1.id) == sorted([u1.id, u2.id])

        replacement = service.adjust_event_amount(
            event.id,
            new_amount=Decimal("6000.00"),
            adjusted_by_user_id=admin.id,
            reason="driver miscounted the notes",
        )

        # Money first: 10000 - 6000, i.e. the corrected amount landed on the
        # order it was collected for...
        assert _outstanding(db, own_payment) == Decimal("4000.00")
        # ...and the freshly-linked sibling's older debt is untouched.
        assert _outstanding(db, sibling_payment) == Decimal("6000.00")
        # The replacement replays the FROZEN personal scope, not the new cluster.
        assert replacement.scope_type == "personal"
        assert replacement.scope_snapshot is None
        assert Decimal(str(replacement.unapplied_amount)) == Decimal("0.00")
        _assert_conserved(db, replacement)

    def test_correction_replays_frozen_cluster_after_unlink(self, db):
        """Narrowing companion: the frozen scope survives an unlink too.

        Rings 1-2 are history — the sibling debt that was inside the cluster when
        the cash was collected still settles on correction.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=2)

        _sibling_order, sibling_payment = delivered_cod_order(
            db, u2, total=Decimal("5000.00"), created_at=t0
        )
        own_order, own_payment = delivered_cod_order(
            db, u1, total=Decimal("5000.00"), created_at=t0 + timedelta(days=1)
        )

        service = CashCollectionService()
        event = service.post_collection(
            customer_id=u1.id,
            amount=Decimal("5000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="one person, two phones",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, sibling_payment) == Decimal("0.00")

        CustomerLinkService().unlink_account(u2.id, actor_admin_id=admin.id, reason="separate people")
        db.session.commit()

        replacement = service.adjust_event_amount(
            event.id,
            new_amount=Decimal("9000.00"),
            adjusted_by_user_id=admin.id,
            reason="recount",
        )

        assert replacement.scope_type == "cluster"
        assert replacement.scope_snapshot == {"user_ids": sorted([u1.id, u2.id])}
        # Both debts inside the frozen cluster still settle: 5000 + 4000 of 9000.
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        assert _outstanding(db, own_payment) == Decimal("1000.00")
        _assert_conserved(db, replacement)


@pytest.mark.unit
class TestRingThreeCarveOut:
    def test_correction_after_unlink_creates_no_reservation_on_departed_sibling(self, db):
        """Ring-3 carve-out pin (spec §5.6).

        A cluster collection leaves a residual. The sibling is then unlinked and
        acquires their own credit plus a pending order. Correcting the original
        event replays the FROZEN cluster for debt settlement (rings 1-2) but the
        residual reservation sweep must resolve the CURRENT cluster — so nothing
        may be reserved against the departed sibling's pending order.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=4)

        own_order, own_payment = delivered_cod_order(
            db, u1, total=Decimal("5000.00"), created_at=t0
        )

        service = CashCollectionService()
        event = service.post_collection(
            customer_id=u1.id,
            amount=Decimal("8000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="cluster cash with change left over",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert Decimal(str(event.unapplied_amount)) == Decimal("3000.00")

        # The sibling leaves the cluster, then acquires their own credit and a
        # pending order — state that only the sibling's own sweep may touch.
        CustomerLinkService().unlink_account(u2.id, actor_admin_id=admin.id, reason="separate people")
        db.session.commit()
        assert CustomerLinkService().get_cluster_user_ids(u1.id) == [u1.id]

        sibling_credit = CashCollectionEvent(
            customer_id=u2.id,
            recorded_by_user_id=admin.id,
            amount=Decimal("2000.00"),
            currency="UZS",
            source=CashCollectionSource.ADMIN_ADJUSTMENT,
            occurred_at=t0 + timedelta(days=1),
            unapplied_amount=Decimal("2000.00"),
            notes="sibling's own prepaid credit",
        )
        db.session.add(sibling_credit)
        db.session.commit()
        _pending_order, sibling_pending_payment = delivered_cod_order(
            db,
            u2,
            total=Decimal("4000.00"),
            status=OrderStatus.PENDING,
            created_at=t0 + timedelta(days=2),
        )
        assert _live_reservations_for(sibling_pending_payment.id) == []

        replacement = service.adjust_event_amount(
            event.id,
            new_amount=Decimal("9000.00"),
            adjusted_by_user_id=admin.id,
            reason="recount at handover",
        )

        # Ring 3 resolved the CURRENT cluster: the departed sibling's pending
        # order has no reservation, and their own credit was never consumed.
        assert _live_reservations_for(sibling_pending_payment.id) == []
        db.session.refresh(sibling_credit)
        assert Decimal(str(sibling_credit.unapplied_amount)) == Decimal("2000.00")
        db.session.refresh(sibling_pending_payment)
        reserved_projection = (sibling_pending_payment.provider_data or {}).get(
            "cod_prepayment_reserved_amount"
        )
        assert not reserved_projection or Decimal(str(reserved_projection)) == Decimal("0.00")

        # Rings 1-2 replayed the frozen cluster: u1's own debt settled again.
        assert replacement.scope_type == "cluster"
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert Decimal(str(replacement.unapplied_amount)) == Decimal("4000.00")
        _assert_conserved(db, replacement)

    def test_sweep_still_reserves_within_the_current_cluster(self, db):
        """The carve-out narrows to CURRENT topology — it does not disable ring 3.

        A still-linked SIBLING's pending order is a legitimate ring-3 target: the
        pending order below belongs to u2 while u1 posts and funds the residual.
        Building it for u1 instead would be pre-fix-identical (a poster's own
        pending order is reserved against with or without the cluster widening),
        so the widening would go unpinned.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        t0 = datetime.now(UTC) - timedelta(days=4)

        own_order, own_payment = delivered_cod_order(
            db, u1, total=Decimal("5000.00"), created_at=t0
        )
        _pending_order, sibling_pending_payment = delivered_cod_order(
            db,
            u2,
            total=Decimal("4000.00"),
            status=OrderStatus.PENDING,
            created_at=t0 + timedelta(days=1),
        )

        service = CashCollectionService()
        event = service.post_collection(
            customer_id=u1.id,
            amount=Decimal("8000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="cluster cash with change left over",
        )

        assert _outstanding(db, own_payment) == Decimal("0.00")
        reservations = _live_reservations_for(sibling_pending_payment.id)
        assert [Decimal(str(a.allocated_amount)) for a in reservations] == [Decimal("3000.00")]
        assert {a.cash_collection_event_id for a in reservations} == {event.id}
