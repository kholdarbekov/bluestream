"""Plan 2b Task 2: every allocation write carries dual audit stamps.

`cash_collection_allocations.source_customer_id` / `.beneficiary_user_id` are
denormalized at-allocation copies of `event.customer_id` / `payment.user_id`
(spec §4.3/R6). Today those two are always the same account; once allocation
widens to cluster/place scope they legitimately diverge, and without the stamps
a cross-customer settlement is untraceable after the fact (payment.user_id is
mutable). The §11 reconciliation invariant reads these columns.
"""
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.payment import CashCollectionAllocation
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import OrderStatus
from tests.unit._scope_money_helpers import delivered_cod_order, make_user


@pytest.mark.unit
class TestAllocationDualStamps:
    def test_auto_allocation_stamps_source_and_beneficiary(self, db):
        u = make_user(db)
        admin = make_user(db)
        order, payment = delivered_cod_order(db, u)
        CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash meeting",
        )
        allocs = CashCollectionAllocation.query.filter_by(payment_id=payment.id).all()
        assert allocs, "expected an allocation"
        for a in allocs:
            assert a.source_customer_id == u.id
            assert a.beneficiary_user_id == u.id

    def test_reservation_allocation_stamps_both(self, db):
        u = make_user(db)
        admin = make_user(db)
        delivered, _ = delivered_cod_order(db, u)
        pending_order, pending_payment = delivered_cod_order(
            db, u, status=OrderStatus.CONFIRMED
        )
        # Overpay the delivered order -> residual sweeps onto the pending order
        # as a prepaid_reservation allocation.
        CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("20000.00"),
            source="standalone_meeting",
            order_id=delivered.id,
            recorded_by_user_id=admin.id,
            notes="overpaid cash meeting",
        )
        reservation = CashCollectionAllocation.query.filter_by(
            payment_id=pending_payment.id, allocation_mode="prepaid_reservation"
        ).first()
        assert reservation is not None
        assert reservation.source_customer_id == u.id
        assert reservation.beneficiary_user_id == u.id

    def test_prepaid_credit_allocation_stamps_both(self, db):
        """Standing credit applied later (mode='prepaid_credit') stamps too."""
        u = make_user(db)
        admin = make_user(db)
        # Pure credit: no orders yet, so the whole amount stays unapplied.
        CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            recorded_by_user_id=admin.id,
            notes="prepayment on account",
        )
        # A delivered COD debt appears afterwards; settling it from credit is
        # the 'prepaid_credit' path.
        _order, payment = delivered_cod_order(db, u)
        CashCollectionService().settle_payment_from_customer_credit(payment)
        db.session.commit()

        allocs = CashCollectionAllocation.query.filter_by(
            payment_id=payment.id, allocation_mode="prepaid_credit"
        ).all()
        assert allocs, "expected a prepaid_credit allocation"
        for a in allocs:
            assert a.source_customer_id == u.id
            assert a.beneficiary_user_id == u.id

    def test_stamps_are_not_swapped_when_source_and_beneficiary_differ(self, db):
        """DISTINCT accounts on each side: a swapped assignment must fail.

        Every other test runs on today's narrow allocation where
        event.customer_id == payment.user_id, so a transposed pair would still
        pass. This drives `_allocate_to_payment` directly with a payer and a
        beneficiary that are different users — the shape later tasks unlock.
        """
        payer = make_user(db)
        beneficiary = make_user(db)
        admin = make_user(db)
        assert payer.id != beneficiary.id

        service = CashCollectionService()
        event = service.post_collection(
            customer_id=payer.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            recorded_by_user_id=admin.id,
            notes="credit held by the payer",
        )
        _order, other_payment = delivered_cod_order(db, beneficiary)

        service._allocate_to_payment(
            event=event,
            payment=other_payment,
            amount=Decimal("15000.00"),
            allocation_order=service._next_allocation_order(event.id),
            allocation_mode="manual",
            trigger_completion_notification=False,
        )
        _db.session.commit()

        alloc = CashCollectionAllocation.query.filter_by(
            payment_id=other_payment.id
        ).one()
        assert alloc.source_customer_id == payer.id
        assert alloc.beneficiary_user_id == beneficiary.id

    def test_stamps_survive_flush_as_written_not_reread(self, db):
        """The stamps freeze the values as of allocation time.

        `payment.user_id` is mutable; if the row were populated from a later
        re-read, reassigning the payment would silently rewrite history.
        """
        u = make_user(db)
        other = make_user(db)
        admin = make_user(db)
        order, payment = delivered_cod_order(db, u)
        CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash meeting",
        )
        alloc = CashCollectionAllocation.query.filter_by(payment_id=payment.id).one()
        alloc_id = alloc.id

        payment.user_id = other.id
        db.session.commit()
        db.session.expire_all()

        replayed = db.session.get(CashCollectionAllocation, alloc_id)
        assert replayed.source_customer_id == u.id
        assert replayed.beneficiary_user_id == u.id
