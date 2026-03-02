"""Business-rule unit tests for PaymentService lifecycle transitions."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.payment import Payment, PaymentTransaction
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import OrderStatus, PaymentMethod, PaymentStatus, PaymeState
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

    def test_process_loyalty_points_payment_rejects_when_points_insufficient(
        self,
        payment_service,
        loyalty_payment,
    ):
        with patch("business_app.services.loyalty_service.LoyaltyService") as loyalty_cls:
            loyalty = loyalty_cls.return_value
            loyalty.get_user_points.return_value = 10

            with pytest.raises(ValidationError):
                payment_service.process_loyalty_points_payment(loyalty_payment.id, points_used=100)

    def test_process_loyalty_points_payment_rejects_when_points_value_under_amount(
        self,
        payment_service,
        loyalty_payment,
    ):
        with patch("business_app.services.loyalty_service.LoyaltyService") as loyalty_cls, patch(
            "business_app.utils.helpers.calculate_discount_from_points", return_value=Decimal("1000.00")
        ):
            loyalty = loyalty_cls.return_value
            loyalty.get_user_points.return_value = 10000

            with pytest.raises(ValidationError):
                payment_service.process_loyalty_points_payment(loyalty_payment.id, points_used=5000)

    def test_process_loyalty_points_payment_success_updates_status_and_provider_data(
        self,
        payment_service,
        loyalty_payment,
        db,
    ):
        with patch("business_app.services.loyalty_service.LoyaltyService") as loyalty_cls, patch(
            "business_app.utils.helpers.calculate_discount_from_points",
            return_value=Decimal(loyalty_payment.amount),
        ), patch.object(payment_service, "_create_transaction") as create_tx, patch.object(
            payment_service, "_handle_successful_payment"
        ) as handle_success:
            loyalty = loyalty_cls.return_value
            loyalty.get_user_points.return_value = 999999

            updated = payment_service.process_loyalty_points_payment(loyalty_payment.id, points_used=18000)

        db.session.refresh(loyalty_payment)
        assert updated.status == PaymentStatus.COMPLETED
        assert loyalty_payment.status == PaymentStatus.COMPLETED
        assert loyalty_payment.provider_data["points_used"] == 18000
        create_tx.assert_called_once()
        handle_success.assert_called_once()
        loyalty.deduct_points.assert_called_once()

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
