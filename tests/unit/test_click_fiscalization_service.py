import hashlib
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from business_app import db
from business_app.models.order import OrderItem
from business_app.models.payment import Payment
from business_app.models.product import ProductFiscalProfile, ProductMarkingCode
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import (
    FiscalizationStatus,
    MarkingCodeStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import ValidationError


def _sign_click_payload(provider: ClickPaymentProviderService, payload: dict) -> dict:
    signed_payload = dict(payload)
    sign_source = (
        f"{signed_payload['click_trans_id']}{signed_payload['service_id']}"
        f"{provider.secret_key}{signed_payload['merchant_trans_id']}"
    )
    if str(signed_payload.get('action')) == '1':
        sign_source += f"{signed_payload.get('merchant_prepare_id', '')}"
    sign_source += f"{signed_payload['amount']}{signed_payload['action']}{signed_payload['sign_time']}"
    signed_payload['sign_string'] = hashlib.md5(sign_source.encode('utf-8')).hexdigest()
    return signed_payload


@pytest.fixture
def payment_service(app, mock_redis):
    with app.app_context():
        service = PaymentService()
        service.redis_client = mock_redis
        return service


@pytest.fixture
def fiscalized_click_payment(db, sample_order, sample_product):
    sample_order.payment_method = PaymentMethod.CLICK
    sample_order.delivery_fee = Decimal('3000.00')
    sample_order.subtotal = Decimal('30000.00')
    sample_order.total_amount = Decimal('33000.00')

    sample_product.barcode = '4780012345678'
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        spic='SPIC-19L',
        package_code='PACK-19L',
        units='pcs',
        vat_percent=Decimal('12.00'),
        fiscalization_enabled=True,
        requires_marking_codes=True,
    )
    db.session.add(profile)
    db.session.flush()

    order_item = OrderItem(
        order_id=sample_order.id,
        product_id=sample_product.id,
        quantity=2,
        unit_price=Decimal('15000.00'),
        total_price=Decimal('30000.00'),
    )
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=Decimal('33000.00'),
        currency='UZS',
        status=PaymentStatus.COMPLETED,
        payment_id='click-payment-test',
    )
    db.session.add_all([
        order_item,
        payment,
        ProductMarkingCode(product_id=sample_product.id, code='MARK-001', status=MarkingCodeStatus.AVAILABLE),
        ProductMarkingCode(product_id=sample_product.id, code='MARK-002', status=MarkingCodeStatus.AVAILABLE),
    ])
    db.session.commit()
    return payment


@pytest.fixture
def business_account_payment(db, sample_order, sample_product):
    sample_order.payment_method = PaymentMethod.BUSINESS_ACCOUNT
    sample_order.delivery_fee = Decimal('0.00')
    sample_order.subtotal = Decimal('15000.00')
    sample_order.total_amount = Decimal('15000.00')

    sample_product.barcode = '4780099999999'
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        spic='SPIC-B2B',
        package_code='PACK-B2B',
        units='pcs',
        vat_percent=Decimal('12.00'),
        fiscalization_enabled=True,
        requires_marking_codes=True,
    )
    db.session.add(profile)
    db.session.flush()

    order_item = OrderItem(
        order_id=sample_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal('15000.00'),
        total_price=Decimal('15000.00'),
    )
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.BUSINESS_ACCOUNT,
        amount=Decimal('15000.00'),
        currency='UZS',
        status=PaymentStatus.COMPLETED,
        payment_id='business-account-payment',
        consume_marking_codes=False,
    )
    marking_code = ProductMarkingCode(
        product_id=sample_product.id,
        code='B2B-MARK-001',
        status=MarkingCodeStatus.AVAILABLE,
    )
    db.session.add_all([order_item, payment, marking_code])
    db.session.commit()
    return payment, marking_code


@pytest.fixture
def pending_marked_click_payment(db, sample_order, sample_product):
    sample_order.payment_method = PaymentMethod.CLICK
    sample_order.delivery_fee = Decimal('0.00')
    sample_order.subtotal = Decimal('15000.00')
    sample_order.total_amount = Decimal('15000.00')

    sample_product.barcode = '4780011111111'
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        spic='SPIC-PENDING',
        package_code='PACK-PENDING',
        units='pcs',
        vat_percent=Decimal('12.00'),
        fiscalization_enabled=True,
        requires_marking_codes=True,
    )
    db.session.add(profile)
    db.session.flush()

    order_item = OrderItem(
        order_id=sample_order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=Decimal('15000.00'),
        total_price=Decimal('15000.00'),
    )
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=Decimal('15000.00'),
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id='click-pending-marked',
    )
    marking_code = ProductMarkingCode(
        product_id=sample_product.id,
        code='MARK-PENDING-001',
        status=MarkingCodeStatus.AVAILABLE,
    )
    db.session.add_all([order_item, payment, marking_code])
    db.session.commit()
    return payment, marking_code


@pytest.mark.unit
@pytest.mark.payment
class TestClickFiscalizationService:
    def test_build_click_fiscalization_payload_includes_item_labels(self, db, fiscalized_click_payment):
        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        payload = service.build_click_fiscalization_payload(payment)

        assert payload['payment_id'] == 'click-payment-test'
        assert payload['received_card'] == 33000.0
        assert payload['items'][0]['Barcode'] == '4780012345678'
        assert payload['items'][0]['Labels'] == ['MARK-001', 'MARK-002']
        assert payload['items'][-1]['Other'] == 'delivery_fee'
        assert payload['items'][-1]['Price'] == 3000.0

    def test_business_account_marking_codes_require_explicit_toggle(self, db, business_account_payment):
        payment, marking_code = business_account_payment
        service = PaymentFiscalizationService(click_provider_service=Mock())

        with pytest.raises(ValidationError):
            service.consume_marking_codes_for_business_account(payment)

        payment.consume_marking_codes = True
        fiscalization = service.consume_marking_codes_for_business_account(payment, actor_user_id=123)
        db.session.commit()
        db.session.refresh(marking_code)

        assert fiscalization.status == FiscalizationStatus.COMPLETED
        assert marking_code.status == MarkingCodeStatus.USED
        assert marking_code.used_at is not None

    def test_click_complete_is_idempotent(self, app, db, payment_service, sample_order):
        sample_order.payment_method = PaymentMethod.CLICK
        payment = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=PaymentMethod.CLICK,
            amount=sample_order.total_amount,
            currency='UZS',
            status=PaymentStatus.PENDING,
            payment_id='click-callback-idempotency',
        )
        db.session.add(payment)
        db.session.commit()

        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True

        provider = ClickPaymentProviderService(payment_service=payment_service)
        payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-001',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': payment.payment_id,
            'merchant_prepare_id': payment.id,
            'amount': str(payment.amount),
            'action': '1',
            'sign_time': '1700000000',
            'error': '0',
            'error_note': 'Success',
        })

        with patch.object(payment_service, '_handle_successful_payment') as handle_success, patch.object(
            payment_service,
            'queue_click_fiscalization',
        ) as queue_fiscalization:
            first = provider.handle_complete(payload)
            second = provider.handle_complete(payload)

        db.session.refresh(payment)
        assert first['error'] == 0
        assert second['error'] == -4
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.provider_transaction_id == 'txn-001'
        handle_success.assert_called_once()
        queue_fiscalization.assert_called_once_with(payment.id)

    def test_click_complete_cancellation_releases_reserved_codes(
        self,
        app,
        db,
        payment_service,
        pending_marked_click_payment,
    ):
        payment, marking_code = pending_marked_click_payment
        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True

        provider = ClickPaymentProviderService(payment_service=payment_service)
        prepare_payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-cancel-001',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': payment.payment_id,
            'amount': str(payment.amount),
            'action': '0',
            'sign_time': '1700000001',
            'error': '0',
            'error_note': 'Success',
        })
        complete_payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-cancel-001',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': payment.payment_id,
            'merchant_prepare_id': payment.id,
            'amount': str(payment.amount),
            'action': '1',
            'sign_time': '1700000002',
            'error': '-1',
            'error_note': 'Cancelled by Click',
        })

        provider.handle_prepare(prepare_payload)
        db.session.refresh(marking_code)
        assert marking_code.status == MarkingCodeStatus.RESERVED

        response = provider.handle_complete(complete_payload)

        db.session.refresh(payment)
        db.session.refresh(marking_code)
        assert response['error'] == -9
        assert payment.status == PaymentStatus.CANCELLED
        assert payment.failure_reason == 'Cancelled by Click'
        assert marking_code.status == MarkingCodeStatus.AVAILABLE

    def test_update_payment_status_marks_cancelled_and_releases_reserved_codes(
        self,
        db,
        payment_service,
        pending_marked_click_payment,
    ):
        payment, marking_code = pending_marked_click_payment
        fiscalization_service = PaymentFiscalizationService(click_provider_service=Mock())
        fiscalization_service.reserve_required_marking_codes(payment)
        db.session.refresh(marking_code)
        assert marking_code.status == MarkingCodeStatus.RESERVED

        with patch.object(
            payment_service,
            '_get_click_provider_service',
            return_value=Mock(check_payment_status=Mock(return_value={
                'status': 'cancelled',
                'error_note': 'Timed out',
                'raw': {'error_note': 'Timed out'},
            })),
        ):
            payment_service.update_payment_status(payment)

        db.session.refresh(payment)
        db.session.refresh(marking_code)
        assert payment.status == PaymentStatus.CANCELLED
        assert payment.failure_reason == 'Timed out'
        assert marking_code.status == MarkingCodeStatus.AVAILABLE
