"""Unit tests for Fix A6: PaymentService.get_user_payment_statistics.

Extracted from the ~90-line inline aggregation previously in
business_app/api/payments.py::get_payment_statistics (violates
service-layer-first). Mirrors OrderService.get_user_order_statistics.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.payment import Payment
from business_app.services.payment_service import PaymentService
from shared.enums import PaymentMethod, PaymentStatus


@pytest.fixture
def payment_service(app, mock_redis):
    with app.app_context():
        service = PaymentService()
        service.redis_client = mock_redis
        service._webhook_signature_verifier._redis = mock_redis
        return service


def _create_payment(db, user_id, method, status, amount, created_at):
    payment = Payment(
        user_id=user_id,
        payment_method=method,
        amount=Decimal(str(amount)),
        currency="UZS",
        status=status,
    )
    db.session.add(payment)
    db.session.commit()
    if created_at is not None:
        payment.created_at = created_at
        db.session.commit()
    return payment


@pytest.mark.unit
@pytest.mark.payment
class TestGetUserPaymentStatistics:
    def test_returns_expected_aggregation_structure(self, payment_service, db, sample_user):
        now = datetime.now(UTC)
        _create_payment(db, sample_user.id, PaymentMethod.CARD, PaymentStatus.COMPLETED, "10000.00", now)
        _create_payment(db, sample_user.id, PaymentMethod.PAYME, PaymentStatus.COMPLETED, "5000.00", now)
        _create_payment(db, sample_user.id, PaymentMethod.CARD, PaymentStatus.FAILED, "2000.00", now)

        result = payment_service.get_user_payment_statistics(sample_user.id, period="all")

        assert result["period"] == "all"
        stats = result["statistics"]
        assert stats["total_payments"] == 3
        assert stats["successful_payments"] == 2
        assert stats["failed_payments"] == 1
        assert stats["success_rate"] == pytest.approx(66.67, rel=1e-3)
        assert stats["total_amount"] == Decimal("15000.00")
        assert stats["average_payment"] == Decimal("7500.00")
        assert set(stats["payment_methods"].keys()) == {
            "instant",
            "card_payment",
            "digital_wallet",
            "points",
            "account_balance",
        }
        for method_stat in stats["payment_methods"].values():
            assert set(method_stat.keys()) == {"count", "total_amount", "success_rate"}

        month_key = now.strftime("%Y-%m")
        assert stats["monthly_spending_trend"][month_key] == Decimal("15000.00")

    def test_month_period_excludes_older_payments(self, payment_service, db, sample_user):
        now = datetime.now(UTC)
        old = now.replace(year=now.year - 1)
        _create_payment(db, sample_user.id, PaymentMethod.CARD, PaymentStatus.COMPLETED, "10000.00", now)
        _create_payment(db, sample_user.id, PaymentMethod.CARD, PaymentStatus.COMPLETED, "3000.00", old)

        result = payment_service.get_user_payment_statistics(sample_user.id, period="month")

        assert result["statistics"]["total_payments"] == 1
        assert result["statistics"]["total_amount"] == Decimal("10000.00")

    def test_zero_payments_returns_zeroed_structure(self, payment_service, sample_user):
        result = payment_service.get_user_payment_statistics(sample_user.id, period="year")

        stats = result["statistics"]
        assert stats["total_payments"] == 0
        assert stats["success_rate"] == 0
        assert stats["average_payment"] == 0
        assert stats["total_amount"] == 0
