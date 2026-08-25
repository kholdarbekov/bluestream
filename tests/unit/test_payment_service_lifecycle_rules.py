"""Business-rule unit tests for PaymentService lifecycle transitions."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from business_app.models.payment import Payment, PaymentTransaction
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import PaymeState
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
from business_app.utils.exceptions import ValidationError


@pytest.fixture
def payment_service(app, mock_redis):
    with app.app_context():
        service = PaymentService()
        service.redis_client = mock_redis
        return service


@pytest.fixture
def loyalty_payment(db, sample_order):
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.LOYALTY_POINTS,
        amount=sample_order.total_amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id="loyalty_payment_1",
        provider_data={},
    )
    db.session.add(payment)
    db.session.commit()
    return payment


@pytest.mark.unit
@pytest.mark.payment
class TestPaymentLifecycleRules:
    def test_payment_order_id_unique_constraint_rejects_duplicates(
        self,
        db,
        sample_order,
    ):
        first = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=PaymentMethod.PAYME,
            amount=sample_order.total_amount,
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id="unique-payment-1",
            provider_data={},
        )
        second = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=PaymentMethod.PAYME,
            amount=sample_order.total_amount,
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id="unique-payment-2",
            provider_data={},
        )

        db.session.add(first)
        db.session.commit()

        db.session.add(second)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_initialize_order_payment_creates_pending_payme_record_idempotently(
        self,
        payment_service,
        sample_order,
        db,
    ):
        sample_order.payment_method = PaymentMethod.PAYME
        db.session.commit()

        first = payment_service.initialize_order_payment(sample_order.id)
        second = payment_service.initialize_order_payment(sample_order.id)

        payments = Payment.query.filter_by(order_id=sample_order.id).all()
        assert len(payments) == 1
        assert first.id == second.id
        assert first.status == PaymentStatus.PENDING

    def test_initialize_order_payment_completes_business_account_idempotently(
        self,
        payment_service,
        sample_order,
        db,
    ):
        sample_order.payment_method = PaymentMethod.BUSINESS_ACCOUNT
        db.session.commit()

        with patch.object(payment_service, "_handle_successful_payment") as handle_success:
            first = payment_service.initialize_order_payment(
                sample_order.id,
                metadata={"backfill_applied": False},
            )
            second = payment_service.initialize_order_payment(sample_order.id)

        db.session.refresh(sample_order)
        db.session.refresh(first)
        assert first.id == second.id
        assert first.status == PaymentStatus.COMPLETED
        assert first.provider_data["settlement_mode"] == "corporate_contract"
        assert sample_order.is_paid is True
        assert sample_order.paid_at is not None
        handle_success.assert_called_once()

    # NOTE: tests for process_loyalty_points_payment removed — loyalty points are
    # spent only on rewards, never as a direct payment method. The historical
    # points-refund path (_process_points_refund) is retained and tested below.

    def test_process_refund_rejects_amount_exceeding_payment(self, payment_service, loyalty_payment, db):
        loyalty_payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        with pytest.raises(ValidationError):
            payment_service.process_refund(
                payment_id=loyalty_payment.id,
                amount=Decimal(loyalty_payment.amount) + Decimal("1.00"),
            )

    def test_process_refund_loyalty_points_full_sets_cancelled(self, payment_service, loyalty_payment, db):
        loyalty_payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        with patch.object(payment_service, "_process_points_refund", return_value=True) as process_points:
            success = payment_service.process_refund(
                payment_id=loyalty_payment.id,
                amount=Decimal(loyalty_payment.amount),
                reason="customer request",
            )

        db.session.refresh(loyalty_payment)
        assert success is True
        assert loyalty_payment.status == PaymentStatus.CANCELLED
        process_points.assert_called_once()

    def test_process_refund_when_points_refund_fails_does_not_change_status(self, payment_service, loyalty_payment, db):
        loyalty_payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        with patch.object(payment_service, "_process_points_refund", return_value=False):
            success = payment_service.process_refund(
                payment_id=loyalty_payment.id,
                amount=Decimal("1000.00"),
                reason="test",
            )

        db.session.refresh(loyalty_payment)
        assert success is False
        assert loyalty_payment.status == PaymentStatus.COMPLETED

    def test_handle_successful_payment_confirms_pending_order_only(self, payment_service, sample_payment, db):
        sample_payment.order.status = OrderStatus.PENDING
        db.session.commit()

        with patch("business_app.services.order_service.OrderService") as order_service_cls, patch(
            "business_app.tasks.notification_tasks.send_payment_confirmation_task.delay"
        ) as notify_delay:
            payment_service._handle_successful_payment(sample_payment)

        order_service_cls.return_value.update_order_status.assert_called_once_with(
            sample_payment.order.id, OrderStatus.CONFIRMED
        )
        notify_delay.assert_called_once_with(sample_payment.id)

    def test_handle_successful_payment_does_not_reconfirm_non_pending_order(self, payment_service, sample_payment, db):
        sample_payment.order.status = OrderStatus.CONFIRMED
        db.session.commit()

        with patch("business_app.services.order_service.OrderService") as order_service_cls, patch(
            "business_app.tasks.notification_tasks.send_payment_confirmation_task.delay"
        ) as notify_delay:
            payment_service._handle_successful_payment(sample_payment)

        order_service_cls.return_value.update_order_status.assert_not_called()
        notify_delay.assert_called_once_with(sample_payment.id)

    def test_payme_cancel_transaction_uses_order_service_cancellation(self, payment_service, sample_payment, db):
        sample_payment.status = PaymentStatus.COMPLETED
        sample_payment.order.status = OrderStatus.CONFIRMED
        transaction = PaymentTransaction(
            payment_id=sample_payment.id,
            transaction_type="charge",
            amount=sample_payment.amount,
            currency="UZS",
            status="completed",
            provider_transaction_id="payme-tx-1",
            success=True,
        )
        db.session.add(transaction)
        db.session.commit()

        with patch("business_app.services.order_service.OrderService") as order_service_cls, patch.object(
            payment_service,
            "process_refund",
            return_value=True,
        ) as process_refund:
            result = payment_service._payme_cancel_transaction({"id": "payme-tx-1", "reason": 42})

        db.session.refresh(transaction)
        order_service_cls.return_value.cancel_order.assert_called_once_with(
            sample_payment.order.id,
            reason="Payme Cancel: 42",
            process_payment_refund=False,
        )
        process_refund.assert_called_once_with(sample_payment.id, sample_payment.amount, "Payme Cancel: 42")
        assert transaction.status == "refunded"
        assert result["result"]["state"] == PaymeState.REFUNDED.value

    def test_payme_cancel_transaction_end_to_end_is_unaffected_by_the_card_refund_ban(
        self, payment_service, sample_payment, db
    ):
        """B4a §6.1 — PAYME MUST NOT CHANGE, and this drives it UNPATCHED.

        The cell above patches `process_refund`, so it would keep passing even if
        the real method started refusing every rail. This one runs the real
        method end to end on a real PAYME payment and pins the three things
        Payme's protocol response depends on: `success is True`, the payment
        flipped to CANCELLED, and `_sync_order_paid_projection` ran
        (`order.is_paid` back to False). Payme `CancelTransaction` is a
        merchant-agreement obligation the GATEWAY initiates — refusing it would
        breach that agreement, which is why the B4a guard is rail-gated on
        {CLICK, CARD} rather than blanket.

        And no `CashCollectionEvent`: Payme money really does go back to the
        customer's card, so it must not ALSO become customer prepaid credit.
        """
        from business_app.models.payment import CashCollectionEvent

        sample_payment.payment_method = PaymentMethod.PAYME
        sample_payment.status = PaymentStatus.COMPLETED
        sample_payment.amount_collected = sample_payment.amount
        sample_payment.order.payment_method = PaymentMethod.PAYME
        sample_payment.order.status = OrderStatus.CONFIRMED
        sample_payment.order.is_paid = True
        transaction = PaymentTransaction(
            payment_id=sample_payment.id,
            transaction_type="charge",
            amount=sample_payment.amount,
            currency="UZS",
            status="completed",
            provider_transaction_id="payme-tx-b4a",
            success=True,
        )
        db.session.add(transaction)
        db.session.commit()

        with patch("business_app.services.order_service.OrderService"):
            result = payment_service._payme_cancel_transaction({"id": "payme-tx-b4a", "reason": 5})

        db.session.refresh(sample_payment)
        assert result["result"]["state"] == PaymeState.REFUNDED.value
        assert sample_payment.status == PaymentStatus.CANCELLED
        assert sample_payment.order.is_paid is False
        assert CashCollectionEvent.query.filter_by(customer_id=sample_payment.user_id).count() == 0
