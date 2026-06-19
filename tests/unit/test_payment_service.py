"""Unit tests for PaymentService aligned with the current implementation."""

from datetime import UTC, datetime
from decimal import Decimal
import hashlib
from unittest.mock import patch

import pytest

from business_app.models.payment import CreditCard, Payment
from business_app.services.payment_service import PaymentService
from shared.enums import PaymentMethod, PaymentStatus
from business_app.utils.exceptions import NotFoundError, PaymentError, ValidationError


@pytest.fixture
def payment_service(app, mock_redis):
    with app.app_context():
        service = PaymentService()
        service.redis_client = mock_redis
        service._webhook_signature_verifier._redis = mock_redis
        return service


@pytest.fixture
def cash_payment(db, sample_order):
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CASH,
        amount=sample_order.total_amount,
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id='cash_payment_1',
    )
    db.session.add(payment)
    db.session.commit()
    return payment


@pytest.mark.unit
@pytest.mark.payment
class TestPaymentCreation:
    def test_create_payment_success(self, payment_service, sample_order, db):
        payment = payment_service.create_payment(
            order_id=sample_order.id,
            payment_method=PaymentMethod.CARD,
            amount=Decimal('17000.00'),
            provider='payme',
        )

        db.session.refresh(payment)
        assert payment.order_id == sample_order.id
        assert payment.payment_method == PaymentMethod.CARD
        assert payment.amount == Decimal('17000.00')
        assert payment.status == PaymentStatus.PENDING

    def test_create_payment_uses_order_total_when_amount_missing(self, payment_service, sample_order):
        payment = payment_service.create_payment(
            order_id=sample_order.id,
            payment_method=PaymentMethod.PAYME,
        )

        assert payment.amount == sample_order.total_amount

    def test_create_payment_order_not_found(self, payment_service, db):
        with pytest.raises(NotFoundError):
            payment_service.create_payment(order_id=999999, payment_method=PaymentMethod.CARD)

    def test_create_payment_rejects_invalid_amounts(self, payment_service, sample_order):
        with pytest.raises(ValidationError):
            payment_service.create_payment(
                order_id=sample_order.id,
                payment_method=PaymentMethod.CARD,
                amount=Decimal('-1.00'),
            )

        with pytest.raises(ValidationError):
            payment_service.create_payment(
                order_id=sample_order.id,
                payment_method=PaymentMethod.CARD,
                amount=sample_order.total_amount + Decimal('1.00'),
            )

    def test_create_payment_link_unsupported_method(self, payment_service, cash_payment):
        with pytest.raises(PaymentError):
            payment_service.create_payment_link(cash_payment.id)


@pytest.mark.unit
@pytest.mark.payment
class TestPaymentProcessing:
    def test_process_cash_payment_success(self, payment_service, cash_payment, db):
        def _mock_post_collection(*args, **kwargs):
            cash_payment.status = PaymentStatus.COMPLETED
            cash_payment.paid_at = datetime.now(UTC)
            db.session.flush()

        with patch(
            'business_app.services.cash_collection_service.CashCollectionService.post_collection',
            side_effect=_mock_post_collection,
        ), patch.object(payment_service, '_handle_successful_payment') as mock_handle:
            processed = payment_service.process_cash_payment(cash_payment.id, collected_by=1)

        db.session.refresh(cash_payment)
        assert processed.id == cash_payment.id
        assert cash_payment.status == PaymentStatus.COMPLETED
        assert cash_payment.paid_at is not None
        mock_handle.assert_called_once()

    def test_process_cash_payment_rejects_non_cash_method(self, payment_service, sample_payment):
        with pytest.raises(ValidationError):
            payment_service.process_cash_payment(sample_payment.id)


@pytest.mark.unit
@pytest.mark.payment
class TestRefunds:
    def test_process_refund_rejects_pending_payment(self, payment_service, cash_payment):
        with pytest.raises(ValidationError):
            payment_service.process_refund(cash_payment.id, amount=Decimal('1000.00'))

    def test_process_refund_full_sets_cancelled(self, payment_service, cash_payment, db):
        cash_payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        success = payment_service.process_refund(cash_payment.id, amount=cash_payment.amount)

        db.session.refresh(cash_payment)
        assert success is True
        assert cash_payment.status == PaymentStatus.CANCELLED

    def test_process_refund_partial_sets_partially_refunded(self, payment_service, cash_payment, db):
        cash_payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        success = payment_service.process_refund(cash_payment.id, amount=Decimal('5000.00'))

        db.session.refresh(cash_payment)
        assert success is True
        assert cash_payment.status == PaymentStatus.PARTIALLY_REFUNDED

    def test_points_refund_returns_redeemed_points_proportionally(self, payment_service, monkeypatch):
        """Loyalty is rewards-only: refunding a points-paid order returns the
        points the customer actually redeemed (proportional to the refund),
        credited as a non-qualifying ADJUSTMENT — NOT a UZS->points conversion
        and NOT with the earning tier multiplier."""
        from types import SimpleNamespace

        from business_app.services.loyalty_service import LoyaltyService

        captured = {}

        def fake_reverse(self, user_id, order_id, old_points_earned, new_points_earned, **kwargs):
            captured.update(
                user_id=user_id, order_id=order_id, old=old_points_earned, new=new_points_earned
            )
            return {"transaction_id": 1}

        monkeypatch.setattr(LoyaltyService, "reverse_earnings", fake_reverse)

        def _no_conversion(self, *args, **kwargs):
            raise AssertionError("refund must not convert UZS->points")

        monkeypatch.setattr(LoyaltyService, "calculate_points_for_purchase", _no_conversion)

        payment = SimpleNamespace(
            user_id=42,
            amount=1000,
            order=SimpleNamespace(id=7, order_number="N-1", loyalty_points_used=200),
        )
        # Half the order is refunded -> half the redeemed points returned.
        result = payment_service._process_points_refund(payment, amount=500, reason="test")

        assert result is True
        assert captured["user_id"] == 42
        assert captured["old"] == 0
        assert captured["new"] == 100  # 200 redeemed * (500 / 1000)


@pytest.mark.unit
@pytest.mark.payment
class TestCardManagement:
    def test_get_user_cards_excludes_expired_by_default(self, payment_service, sample_user, db):
        current_year = datetime.now(UTC).year

        active_card = CreditCard(
            user_id=sample_user.id,
            card_token='tok_active',
            card_brand='visa',
            last_four_digits='4242',
            expiry_month=12,
            expiry_year=current_year + 1,
            cardholder_name='Test User',
            provider='payme',
            is_default=True,
            is_active=True,
        )
        expired_card = CreditCard(
            user_id=sample_user.id,
            card_token='tok_expired',
            card_brand='visa',
            last_four_digits='1111',
            expiry_month=1,
            expiry_year=current_year - 1,
            cardholder_name='Test User',
            provider='payme',
            is_active=True,
        )
        db.session.add_all([active_card, expired_card])
        db.session.commit()

        cards = payment_service.get_user_cards(sample_user.id)

        assert len(cards) == 1
        assert cards[0].card_token == 'tok_active'

    def test_get_default_card_falls_back_to_latest_active(self, payment_service, sample_user, db):
        current_year = datetime.now(UTC).year

        older_card = CreditCard(
            user_id=sample_user.id,
            card_token='tok_old',
            card_brand='visa',
            last_four_digits='1111',
            expiry_month=12,
            expiry_year=current_year + 1,
            cardholder_name='Test User',
            provider='payme',
            is_default=False,
            is_active=True,
        )
        db.session.add(older_card)
        db.session.commit()

        newer_card = CreditCard(
            user_id=sample_user.id,
            card_token='tok_new',
            card_brand='visa',
            last_four_digits='2222',
            expiry_month=12,
            expiry_year=current_year + 2,
            cardholder_name='Test User',
            provider='payme',
            is_default=False,
            is_active=True,
        )
        db.session.add(newer_card)
        db.session.commit()

        default_card = payment_service.get_default_card(sample_user.id)

        assert default_card is not None
        assert default_card.card_token == 'tok_new'

    def test_set_default_card_rejects_expired(self, payment_service, sample_user, db):
        expired_card = CreditCard(
            user_id=sample_user.id,
            card_token='tok_set_default_expired',
            card_brand='visa',
            last_four_digits='3333',
            expiry_month=1,
            expiry_year=2020,
            cardholder_name='Test User',
            provider='payme',
            is_default=False,
            is_active=True,
        )
        db.session.add(expired_card)
        db.session.commit()

        with pytest.raises(ValidationError):
            payment_service.set_default_card(expired_card.id, sample_user.id)

    def test_delete_card_rejects_only_default_card(self, payment_service, sample_user, db):
        only_card = CreditCard(
            user_id=sample_user.id,
            card_token='tok_only_default',
            card_brand='visa',
            last_four_digits='4444',
            expiry_month=12,
            expiry_year=datetime.now(UTC).year + 2,
            cardholder_name='Test User',
            provider='payme',
            is_default=True,
            is_active=True,
        )
        db.session.add(only_card)
        db.session.commit()

        with pytest.raises(ValidationError):
            payment_service.delete_card(only_card.id, sample_user.id)


@pytest.mark.unit
@pytest.mark.payment
class TestWebhookSignatures:
    def test_verify_click_signature_valid_and_invalid(self, app, payment_service):
        app.config['CLICK_SHOP_SECRET_KEY'] = 'test-click-secret'

        prepare_data = {
            'click_trans_id': '123',
            'service_id': '55',
            'merchant_trans_id': 'PAY_1',
            'amount': '18000.00',
            'action': '0',
            'sign_time': '1700000000',
        }
        signature_payload = (
            f"{prepare_data['click_trans_id']}{prepare_data['service_id']}"
            f"{app.config['CLICK_SHOP_SECRET_KEY']}{prepare_data['merchant_trans_id']}"
            f"{prepare_data['amount']}{prepare_data['action']}{prepare_data['sign_time']}"
        )
        valid_signature = hashlib.md5(signature_payload.encode('utf-8')).hexdigest()

        signed_data = dict(prepare_data)
        signed_data['sign_string'] = valid_signature

        assert payment_service._verify_click_signature(signed_data) is True

        signed_data['sign_string'] = 'bad-signature'
        assert payment_service._verify_click_signature(signed_data) is False

        complete_data = {
            'click_trans_id': '123',
            'service_id': '55',
            'merchant_trans_id': 'PAY_1',
            'merchant_prepare_id': '91',
            'amount': '18000.00',
            'action': '1',
            'sign_time': '1700000000',
        }
        complete_signature_payload = (
            f"{complete_data['click_trans_id']}{complete_data['service_id']}"
            f"{app.config['CLICK_SHOP_SECRET_KEY']}{complete_data['merchant_trans_id']}"
            f"{complete_data['merchant_prepare_id']}{complete_data['amount']}"
            f"{complete_data['action']}{complete_data['sign_time']}"
        )
        complete_signature = hashlib.md5(complete_signature_payload.encode('utf-8')).hexdigest()
        signed_complete_data = dict(complete_data, sign_string=complete_signature)

        assert payment_service._verify_click_signature(signed_complete_data) is True

        signed_complete_data['merchant_prepare_id'] = '92'
        assert payment_service._verify_click_signature(signed_complete_data) is False

    def test_validate_click_webhook_signature_uses_callback_allowlist(self, app, payment_service):
        app.config['CLICK_SHOP_SECRET_KEY'] = 'test-click-secret'
        app.config['CLICK_CALLBACK_ALLOWLIST'] = ['10.10.10.10']

        payload = {
            'click_trans_id': '123',
            'service_id': '55',
            'merchant_trans_id': 'PAY_1',
            'amount': '18000.00',
            'action': '0',
            'sign_time': '1700000000',
        }
        signature_payload = (
            f"{payload['click_trans_id']}{payload['service_id']}"
            f"{app.config['CLICK_SHOP_SECRET_KEY']}{payload['merchant_trans_id']}"
            f"{payload['amount']}{payload['action']}{payload['sign_time']}"
        )
        payload['sign_string'] = hashlib.md5(signature_payload.encode('utf-8')).hexdigest()

        class DummyRequest:
            content_type = 'application/x-www-form-urlencoded'
            remote_addr = '10.10.10.10'
            form = payload
            headers = {}

            @staticmethod
            def get_json():
                return None

        assert payment_service.validate_webhook_signature('click', DummyRequest()) is True

        class UnauthorizedDummyRequest(DummyRequest):
            remote_addr = '10.10.10.11'

        assert payment_service.validate_webhook_signature('click', UnauthorizedDummyRequest()) is False

    def test_validate_webhook_signature_unknown_provider(self, payment_service):
        class DummyRequest:
            headers = {}
            form = {}
            content_type = 'application/json'
            remote_addr = '127.0.0.1'

            @staticmethod
            def get_json():
                return {}

        assert payment_service.validate_webhook_signature('unknown', DummyRequest()) is False
