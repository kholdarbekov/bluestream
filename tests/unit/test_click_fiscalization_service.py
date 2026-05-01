import hashlib
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from business_app import db
from business_app.models.order import OrderItem, OrderItemMarkingCodeAllocation
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from business_app.services.payment_service import PaymentService
from shared.enums import (
    FiscalizationStatus,
    MarkingCodeLedgerEventType,
    MarkingCodeStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import PaymentError, ValidationError


def _configure_click_fiscal_context(app):
    app.config['CLICK_SERVICE_ID'] = '98060'
    app.config['CLICK_SHOP_SERVICE_ID'] = '98060'
    app.config['CLICK_MERCHANT_ID'] = '58228'
    app.config['CLICK_SHOP_MERCHANT_ID'] = '58228'
    app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
    app.config['COMPANY_TIN'] = '306522134'
    app.config['COMPANY_PINFL'] = ''
    app.config['CLICK_MERCHANT_API_USER_ID'] = 'merchant-user'
    app.config['CLICK_MERCHANT_API_SECRET_KEY'] = 'merchant-secret'
    # Tax Committee config (needed by fiscalization flow preprocessing)
    app.config['TAX_COMMITTEE_API_URL'] = 'https://xtrace.test.uz'
    app.config['TAX_COMMITTEE_BUSINESS_PLACE_ID'] = '101380'
    app.config['TAX_COMMITTEE_API_TOKEN'] = 'test-tax-token'
    app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True


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
        units='1213733',
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
        ProductMarkingCode(product_id=sample_product.id, code='MARK-001\x1dVERIFY-001', status=MarkingCodeStatus.AVAILABLE),
        ProductMarkingCode(product_id=sample_product.id, code='MARK-002\x1dVERIFY-002', status=MarkingCodeStatus.AVAILABLE),
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
        units='1213733',
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
        units='1213733',
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
    def test_payment_requires_click_fiscalization_ignores_delivery_fee_only(self, db, sample_order):
        sample_order.payment_method = PaymentMethod.CLICK
        sample_order.delivery_fee = Decimal('3000.00')
        sample_order.subtotal = Decimal('0.00')
        sample_order.total_amount = Decimal('3000.00')
        payment = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal('3000.00'),
            currency='UZS',
            status=PaymentStatus.COMPLETED,
            payment_id='click-delivery-only',
        )
        db.session.add(payment)
        db.session.commit()

        service = PaymentFiscalizationService(click_provider_service=Mock())
        assert service.payment_requires_click_fiscalization(payment) is False

    def test_build_click_fiscalization_payload_includes_item_labels(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 987654321
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        payload = service.build_click_fiscalization_payload(payment)

        assert payload['payment_id'] == 987654321
        assert payload['received_card'] == 3300000
        item = payload['items'][0]
        assert item['Name'] == 'Pure Water 19L'
        assert item['SPIC'] == 'SPIC-19L'
        assert item['PackageCode'] == 'PACK-19L'
        assert item['Price'] == 3000000
        assert item['Amount'] == 2000
        assert item['VAT'] == 360000
        assert item['VATPercent'] == 12
        assert item['CommissionInfo'] == {'TIN': '306522134'}
        assert item['Labels'] == ['MARK-001', 'MARK-002']
        assert set(item.keys()) == {
            'Name',
            'SPIC',
            'PackageCode',
            'Price',
            'Amount',
            'VAT',
            'VATPercent',
            'CommissionInfo',
            'Labels',
        }
        assert len(payload['items']) == 1

    def test_build_click_fiscalization_payload_uses_order_total_for_received_card(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 777
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        service.reserve_required_marking_codes(payment)
        payload = service.build_click_fiscalization_payload(payment)

        assert len(payload['items']) == 1
        assert payload['items'][0]['Price'] == 3000000
        assert payload['received_card'] == 3300000

    def test_build_click_fiscalization_payload_uses_line_total_price_and_vat(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 999
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        order_item = payment.order.order_items[0]
        order_item.discount_amount = Decimal('1000.00')
        order_item.total_price = Decimal('29000.00')
        payment.order.subtotal = Decimal('29000.00')
        payment.order.total_amount = Decimal('32000.00')
        payment.amount = Decimal('32000.00')
        db.session.flush()
        service.reserve_required_marking_codes(payment)

        payload = service.build_click_fiscalization_payload(payment)

        assert payload['received_card'] == 3200000
        assert payload['items'][0]['Price'] == 2900000
        assert payload['items'][0]['VAT'] == 348000

    def test_build_click_fiscalization_payload_requires_company_tin(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['COMPANY_TIN'] = ''
        app.config['COMPANY_PINFL'] = '12345678901234'
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 888
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        service.reserve_required_marking_codes(payment)

        with pytest.raises(ValidationError, match='CommissionInfo.TIN requires COMPANY_TIN'):
            service.build_click_fiscalization_payload(payment)

    def test_build_click_fiscalization_payload_fails_when_order_has_non_fiscalized_item(
        self,
        app,
        db,
        fiscalized_click_payment,
        sample_product,
    ):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 1001
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        non_fiscal_product = Product(
            name='Non Fiscal Item',
            description='No fiscal profile',
            category_id=sample_product.category_id,
            size='10L',
            volume=10.0,
            volume_unit='L',
            base_price=Decimal('1000.00'),
            stock_quantity=10,
            min_stock_level=1,
            max_stock_level=100,
            is_active=True,
        )
        db.session.add(non_fiscal_product)
        db.session.flush()
        non_fiscal_item = OrderItem(
            order_id=payment.order_id,
            product_id=non_fiscal_product.id,
            quantity=1,
            unit_price=Decimal('1000.00'),
            total_price=Decimal('1000.00'),
        )
        db.session.add(non_fiscal_item)
        db.session.flush()

        service.reserve_required_marking_codes(payment)
        with pytest.raises(ValidationError, match='is not fiscalization enabled'):
            service.build_click_fiscalization_payload(payment)

    def test_build_click_fiscalization_payload_requires_non_empty_name(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 1002
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.order.order_items[0].product.name = '   '
        db.session.flush()

        service.reserve_required_marking_codes(payment)
        with pytest.raises(ValidationError, match='is missing Name'):
            service.build_click_fiscalization_payload(payment)

    def test_build_click_fiscalization_payload_allows_empty_package_code(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 1003
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.order.order_items[0].product.fiscal_profile.package_code = None
        db.session.flush()

        service.reserve_required_marking_codes(payment)
        payload = service.build_click_fiscalization_payload(payment)

        assert payload['items'][0]['PackageCode'] == ''

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
        _configure_click_fiscal_context(app)
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
            'merchant_trans_id': sample_order.order_number,
            'merchant_prepare_id': payment.id,
            'click_paydoc_id': '1234567890',
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
        assert (payment.provider_data or {}).get('click', {}).get('click_paydoc_id') == '1234567890'
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
        _configure_click_fiscal_context(app)
        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True

        provider = ClickPaymentProviderService(payment_service=payment_service)
        prepare_payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-cancel-001',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': payment.order.order_number,
            'amount': str(payment.amount),
            'action': '0',
            'sign_time': '1700000001',
            'error': '0',
            'error_note': 'Success',
        })
        complete_payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-cancel-001',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': payment.order.order_number,
            'merchant_prepare_id': payment.id,
            'click_paydoc_id': '9988776655',
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
        assert (payment.provider_data or {}).get('click', {}).get('click_paydoc_id') == '9988776655'

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

    def test_build_merchant_headers_uses_auth_digest_format(self, app):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        app.config['CLICK_MERCHANT_API_USER_ID'] = 'merchant-42'
        app.config['CLICK_MERCHANT_API_SECRET_KEY'] = 'super-secret'
        provider = ClickPaymentProviderService()
        with patch('business_app.services.click_payment_provider_service.time.time', return_value=1700000000):
            headers = provider._build_merchant_headers()

        expected_digest = hashlib.sha1('1700000000super-secret'.encode('utf-8')).hexdigest()
        assert headers['Auth'] == f"merchant-42:{expected_digest}:1700000000"

    def test_resolve_click_payment_id_fallbacks_to_complete_payload(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = ClickPaymentProviderService()
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.provider_data = {
            'click': {
                'complete_payload': {
                    'click_paydoc_id': '44556677',
                },
            },
        }
        db.session.flush()

        assert provider.resolve_click_payment_id(payment) == 44556677

    def test_check_payment_status_uses_docs_get_path(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        provider = ClickPaymentProviderService()
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.provider_data = {'click': {'click_paydoc_id': '22110099'}}
        db.session.flush()

        with patch.object(provider, 'merchant_request', return_value={'error_code': 0, 'payment_status': 1}) as merchant_request:
            payload = provider.check_payment_status(payment)

        assert payload['status'] == PaymentStatus.COMPLETED.value
        kwargs = merchant_request.call_args.kwargs
        assert kwargs['method'] == 'GET'
        assert kwargs['fallback_path'].endswith('/payment/status/98060/22110099')

    def test_refund_payment_uses_docs_delete_path(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        provider = ClickPaymentProviderService()
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.provider_data = {'click': {'click_paydoc_id': '77665544'}}
        db.session.flush()

        with patch.object(provider, 'merchant_request', return_value={'error_code': 0, 'payment_id': 77665544}) as merchant_request:
            result = provider.refund_payment(payment, Decimal('33000.00'), reason='test')

        assert result['success'] is True
        kwargs = merchant_request.call_args.kwargs
        assert kwargs['method'] == 'DELETE'
        assert kwargs['fallback_path'].endswith('/payment/reversal/98060/77665544')

    def test_fiscalize_payment_uses_submit_items_endpoint(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        provider = ClickPaymentProviderService()
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.provider_data = {'click': {'click_paydoc_id': '77665544'}}
        db.session.flush()

        with patch.object(provider, 'merchant_request', return_value={'error_code': 0}) as merchant_request, patch.object(
            provider,
            'fetch_ofd_data',
            return_value={'receipt_url': 'https://example.com/qr', 'response': {}},
        ):
            response = provider.fiscalize_payment(
                payment,
                {
                    'service_id': 98060,
                    'payment_id': 77665544,
                    'received_card': 3300000,
                    'items': [],
                },
            )

        assert response['status'] == 'submitted'
        kwargs = merchant_request.call_args.kwargs
        assert kwargs['method'] == 'POST'
        assert kwargs['fallback_path'] == '/payment/ofd_data/submit_items'

    def test_fetch_ofd_data_uses_docs_get_path(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        provider = ClickPaymentProviderService()
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.provider_data = {'click': {'click_paydoc_id': '55443322'}}
        db.session.flush()

        with patch.object(provider, 'merchant_request', return_value={'error_code': 0, 'qrCodeURL': 'https://ofd/qr'}) as merchant_request:
            response = provider.fetch_ofd_data(payment)

        assert response['receipt_url'] == 'https://ofd/qr'
        kwargs = merchant_request.call_args.kwargs
        assert kwargs['method'] == 'GET'
        assert kwargs['fallback_path'].endswith('/payment/ofd_data/98060/55443322')

    def test_submit_fiscal_qrcode_uses_docs_post_path(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        provider = ClickPaymentProviderService()
        payment = Payment.query.get(fiscalized_click_payment.id)
        payment.provider_data = {'click': {'click_paydoc_id': '66778899'}}
        db.session.flush()

        with patch.object(provider, 'merchant_request', return_value={'error_code': 0}) as merchant_request:
            provider.submit_fiscal_qrcode(payment, 'https://ofd/qr')

        args, kwargs = merchant_request.call_args
        assert args[0]['service_id'] == 98060
        assert args[0]['payment_id'] == 66778899
        assert args[0]['qrcode'] == 'https://ofd/qr'
        assert kwargs['method'] == 'POST'
        assert kwargs['fallback_path'] == '/payment/ofd_data/submit_qrcode'

    def test_process_fiscalization_marks_completed_when_qr_is_missing(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        payment = Payment.query.get(fiscalized_click_payment.id)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 1234567
        provider.service_id = '98060'
        provider.fiscalize_payment.return_value = {
            'status': 'submitted_no_qr',
            'receipt_id': '1234567',
            'receipt_url': None,
            'click_paydoc_id': 1234567,
            'error_note': 'missing_qrcode_url',
        }
        service = PaymentFiscalizationService(click_provider_service=provider)

        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 0}):
            fiscalization = service.process_click_fiscalization(payment.id, force=True)
        db.session.flush()

        assert fiscalization.status == FiscalizationStatus.COMPLETED
        assert fiscalization.provider_status == 'submitted_no_qr'
        assert fiscalization.next_retry_at is not None

    def test_process_fiscalization_failure_releases_reserved_codes(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        payment = Payment.query.get(fiscalized_click_payment.id)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 1234567
        provider.service_id = '98060'
        provider.fiscalize_payment.side_effect = PaymentError('error_code=-9')
        service = PaymentFiscalizationService(click_provider_service=provider)

        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 0}):
            with pytest.raises(PaymentError, match='error_code=-9'):
                service.process_click_fiscalization(payment.id, force=True)

        db.session.refresh(payment)
        for code in ProductMarkingCode.query.filter_by(product_id=payment.order.order_items[0].product_id).all():
            assert code.status == MarkingCodeStatus.AVAILABLE
        assert payment.fiscalization is not None
        assert payment.fiscalization.status == FiscalizationStatus.FAILED
        assert 'error_code=-9' in (payment.fiscalization.failure_reason or '')

    def test_merchant_request_raises_on_nonzero_error_code(self, app):
        _configure_click_fiscal_context(app)
        app.config['CLICK_TEST_MODE'] = False
        provider = ClickPaymentProviderService()

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'error_code': -9,
            'error_note': 'bad request',
        }

        with patch('business_app.services.click_payment_provider_service.requests.request', return_value=response):
            with pytest.raises(PaymentError, match='error_code=-9'):
                provider.merchant_request(
                    {'service_id': 1},
                    fallback_path='/payment/ofd_data/submit_items',
                    method='POST',
                    endpoint_label='submit_items',
                )


@pytest.mark.unit
@pytest.mark.payment
class TestIdentificationCodeExtraction:
    """Tests for ASCII 29 marking code handling in fiscalization Labels."""

    def test_extract_identification_code_with_ascii29(self):
        service = PaymentFiscalizationService(click_provider_service=Mock())
        full_code = 'IDENT-CODE-123\x1dVERIFICATION-456'
        assert service._extract_identification_code(full_code) == 'IDENT-CODE-123'

    def test_extract_identification_code_without_ascii29(self):
        service = PaymentFiscalizationService(click_provider_service=Mock())
        full_code = 'SIMPLE-CODE-789'
        assert service._extract_identification_code(full_code) == 'SIMPLE-CODE-789'

    def test_extract_identification_code_multiple_ascii29(self):
        service = PaymentFiscalizationService(click_provider_service=Mock())
        full_code = 'IDENT\x1dVERIFY\x1dEXTRA'
        assert service._extract_identification_code(full_code) == 'IDENT'

    def test_extract_identification_code_empty_string(self):
        service = PaymentFiscalizationService(click_provider_service=Mock())
        assert service._extract_identification_code('') == ''

    def test_reserved_code_lookup_returns_identification_codes(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)

        # Update marking codes to include ASCII 29 separators
        codes = ProductMarkingCode.query.filter_by(product_id=payment.order.order_items[0].product_id).all()
        codes[0].code = 'ID-001\x1dVERIFY-001'
        codes[1].code = 'ID-002\x1dVERIFY-002'
        db.session.flush()

        service.reserve_required_marking_codes(payment)
        lookup = service._reserved_code_lookup(payment)

        order_item_id = payment.order.order_items[0].id
        labels = lookup[order_item_id]
        assert labels == ['ID-001', 'ID-002']

    def test_build_payload_labels_use_identification_codes(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 111222
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)

        codes = ProductMarkingCode.query.filter_by(product_id=payment.order.order_items[0].product_id).all()
        codes[0].code = 'IDENT-A\x1dVERIFY-A'
        codes[1].code = 'IDENT-B\x1dVERIFY-B'
        db.session.flush()

        service.reserve_required_marking_codes(payment)
        payload = service.build_click_fiscalization_payload(payment)

        assert payload['items'][0]['Labels'] == ['IDENT-A', 'IDENT-B']


@pytest.mark.unit
@pytest.mark.payment
class TestTaxCommitteeUtilisationIntegration:
    """Tests for Tax Committee utilisation within the fiscalization flow."""

    def test_utilisation_called_during_process_click_fiscalization(self, app, db, fiscalized_click_payment):
        """Verify utilise_marking_codes_with_tax_committee is called during fiscalization.

        Mocks already_applied codes (utilised=0) so no delay timestamp is set and fiscalization completes.
        """
        _configure_click_fiscal_context(app)
        app.config['TAX_COMMITTEE_API_URL'] = 'https://xtrace.test.uz'
        app.config['TAX_COMMITTEE_BUSINESS_PLACE_ID'] = '101380'
        app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True
        app.config['TAX_COMMITTEE_API_TOKEN'] = 'test-token'
        app.config['TAX_COMMITTEE_UTILISATION_DELAY_SECONDS'] = 120

        provider = Mock()
        provider.resolve_click_payment_id.return_value = 333444
        provider.service_id = '98060'
        provider.fiscalize_payment.return_value = {
            'status': 'submitted',
            'click_paydoc_id': '333444',
        }

        service = PaymentFiscalizationService(click_provider_service=provider)

        # utilised=0: codes were already applied from a previous run; no new timestamp set → no delay
        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 0, 'already_applied': 2}) as mock_utilise:
            fiscalization = service.process_click_fiscalization(fiscalized_click_payment.id)

        mock_utilise.assert_called_once()
        assert fiscalization.status == FiscalizationStatus.COMPLETED

    def test_tc_delay_pending_when_codes_just_utilised(self, app, db, fiscalized_click_payment):
        """When codes are newly utilised and delay has not elapsed, status must be PENDING."""
        _configure_click_fiscal_context(app)
        app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True
        app.config['TAX_COMMITTEE_UTILISATION_DELAY_SECONDS'] = 120

        provider = Mock()
        provider.resolve_click_payment_id.return_value = 111222
        provider.service_id = '98060'

        service = PaymentFiscalizationService(click_provider_service=provider)

        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 2}):
            fiscalization = service.process_click_fiscalization(fiscalized_click_payment.id)

        assert fiscalization.status == FiscalizationStatus.PENDING
        assert fiscalization.tax_committee_utilised_at is not None
        assert fiscalization.next_retry_at is not None
        # submit_items must NOT have been called
        provider.fiscalize_payment.assert_not_called()

    def test_tc_delay_proceeds_when_elapsed(self, app, db, fiscalized_click_payment):
        """When delay has already elapsed, submit_items proceeds on the retry call."""
        from datetime import datetime, timedelta, timezone
        _configure_click_fiscal_context(app)
        app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True
        app.config['TAX_COMMITTEE_UTILISATION_DELAY_SECONDS'] = 120

        provider = Mock()
        provider.resolve_click_payment_id.return_value = 222333
        provider.service_id = '98060'
        provider.fiscalize_payment.return_value = {'status': 'submitted', 'click_paydoc_id': '222333'}

        service = PaymentFiscalizationService(click_provider_service=provider)

        # First attempt: codes newly utilised → PENDING + timestamp recorded
        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 2}):
            fiscalization = service.process_click_fiscalization(fiscalized_click_payment.id)

        assert fiscalization.status == FiscalizationStatus.PENDING
        provider.fiscalize_payment.assert_not_called()

        # Simulate time passing: backdate the utilised_at timestamp
        fiscalization.tax_committee_utilised_at = datetime.now(timezone.utc) - timedelta(minutes=6)
        db.session.flush()

        # Retry: codes now already_applied, delay elapsed → COMPLETED
        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 0, 'already_applied': 2}):
            fiscalization = service.process_click_fiscalization(fiscalized_click_payment.id)

        assert fiscalization.status == FiscalizationStatus.COMPLETED
        provider.fiscalize_payment.assert_called_once()

    def test_tc_delay_not_applied_when_no_codes_utilised(self, app, db, fiscalized_click_payment):
        """When no codes are newly utilised (skipped), no delay is applied."""
        _configure_click_fiscal_context(app)
        app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True
        app.config['TAX_COMMITTEE_UTILISATION_DELAY_SECONDS'] = 120

        provider = Mock()
        provider.resolve_click_payment_id.return_value = 444555
        provider.service_id = '98060'
        provider.fiscalize_payment.return_value = {'status': 'submitted', 'click_paydoc_id': '444555'}

        service = PaymentFiscalizationService(click_provider_service=provider)

        # utilised=0, skipped=True (no marking codes for this payment)
        with patch.object(service, 'utilise_marking_codes_with_tax_committee', return_value={'utilised': 0, 'skipped': True}):
            fiscalization = service.process_click_fiscalization(fiscalized_click_payment.id)

        assert fiscalization.status == FiscalizationStatus.COMPLETED
        assert fiscalization.tax_committee_utilised_at is None
        provider.fiscalize_payment.assert_called_once()

    def test_utilisation_failure_releases_reserved_codes(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = True

        provider = Mock()
        provider.resolve_click_payment_id.return_value = 999
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        product_id = payment.order.order_items[0].product_id

        with patch.object(
            service,
            'utilise_marking_codes_with_tax_committee',
            side_effect=ValidationError('Tax Committee API down'),
        ):
            with pytest.raises(ValidationError, match='Tax Committee API down'):
                service.process_click_fiscalization(payment.id)

        # Verify codes were released
        codes = ProductMarkingCode.query.filter_by(product_id=product_id).all()
        for code in codes:
            assert code.status == MarkingCodeStatus.AVAILABLE

    def test_utilisation_skipped_when_disabled(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        app.config['TAX_COMMITTEE_UTILISATION_ENABLED'] = False

        provider = Mock()
        provider.resolve_click_payment_id.return_value = 555666
        provider.service_id = '98060'
        provider.fiscalize_payment.return_value = {'status': 'submitted', 'click_paydoc_id': '555666'}

        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)

        # Tax committee service should never be accessed when disabled
        fiscalization = service.process_click_fiscalization(payment.id)

        assert fiscalization.status == FiscalizationStatus.COMPLETED
        # Verify Click submission was still called (utilisation skipped, but fiscalization proceeds)
        provider.fiscalize_payment.assert_called_once()


class TestPrecheckAndReplaceInvalidCodes:
    """Tests for the pre-check flow that verifies Tax Committee statuses
    and replaces WITHDRAWN/WRITTEN_OFF codes before utilisation."""

    def test_precheck_all_received_no_replacement(self, app, db, fiscalized_click_payment):
        """All codes are RECEIVED — no replacement needed, all sent to utilisation."""
        _configure_click_fiscal_context(app)
        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        # Reserve codes first
        service.reserve_required_marking_codes(payment)
        db.session.flush()

        mock_check = Mock(return_value={'MARK-001': 'RECEIVED', 'MARK-002': 'RECEIVED'})
        mock_utilise = Mock(return_value={'reportId': 'RPT-ALL-RECEIVED'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes', mock_utilise):
            result = service.utilise_marking_codes_with_tax_committee(payment)

        assert result['utilised'] == 2
        assert result['already_applied'] == 0
        mock_utilise.assert_called_once()
        # Both full codes (including verification and group separator) should be passed to utilisation
        call_codes = mock_utilise.call_args[0][0]
        assert len(call_codes) == 2
        assert 'MARK-001\x1dVERIFY-001' in call_codes
        assert 'MARK-002\x1dVERIFY-002' in call_codes

    def test_precheck_codes_already_applied_skips_utilisation(self, app, db, fiscalized_click_payment):
        """Codes already APPLIED/INTRODUCED skip utilisation but still get ledger events."""
        _configure_click_fiscal_context(app)
        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        # Pre-check returns all as INTRODUCED (already in circulation)
        mock_check = Mock(return_value={'MARK-001': 'INTRODUCED', 'MARK-002': 'APPLIED'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check):
            result = service.utilise_marking_codes_with_tax_committee(payment)

        assert result['utilised'] == 0
        assert result['already_applied'] == 2
        # Verify UTILISED ledger events with already_applied metadata
        utilised_events = (
            OrderItemMarkingCodeAllocation.query
            .filter_by(payment_id=payment.id, action=MarkingCodeLedgerEventType.UTILISED)
            .all()
        )
        assert len(utilised_events) == 2
        assert all(e.event_metadata.get('already_applied') is True for e in utilised_events)

    def test_precheck_mixed_received_and_applied(self, app, db, fiscalized_click_payment):
        """Mix of RECEIVED and INTRODUCED — only RECEIVED codes are utilised."""
        _configure_click_fiscal_context(app)
        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        # First call is from precheck, second is from utilise method splitting by status
        mock_check = Mock(return_value={'MARK-001': 'RECEIVED', 'MARK-002': 'INTRODUCED'})
        mock_utilise = Mock(return_value={'reportId': 'RPT-PARTIAL'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes', mock_utilise):
            result = service.utilise_marking_codes_with_tax_committee(payment)

        assert result['utilised'] == 1
        assert result['already_applied'] == 1
        # Only RECEIVED code should be sent to utilisation — full form including verification and group separator
        call_codes = mock_utilise.call_args[0][0]
        assert len(call_codes) == 1
        assert call_codes[0] == 'MARK-001\x1dVERIFY-001'

    def test_precheck_withdrawn_code_replaced(self, app, db, fiscalized_click_payment):
        """WITHDRAWN code is archived and replaced with a fresh available code."""
        _configure_click_fiscal_context(app)
        sample_product = fiscalized_click_payment.order.order_items[0].product

        # Add a third code as replacement candidate
        replacement_code = ProductMarkingCode(
            product_id=sample_product.id, code='MARK-003\x1dVERIFY-003', status=MarkingCodeStatus.AVAILABLE,
        )
        db.session.add(replacement_code)
        db.session.flush()

        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        # Pre-check: MARK-001 is WITHDRAWN, MARK-002 is RECEIVED
        # After replacement, re-check sees MARK-003 as RECEIVED
        check_responses = [
            {'MARK-001': 'WITHDRAWN', 'MARK-002': 'RECEIVED'},  # Round 1 precheck
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},  # Round 2 precheck (replacement re-check)
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},  # Status check for utilise split
        ]
        mock_check = Mock(side_effect=check_responses)
        mock_utilise = Mock(return_value={'reportId': 'RPT-REPLACED'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes', mock_utilise):
            result = service.utilise_marking_codes_with_tax_committee(payment)

        assert result['utilised'] == 2

        # MARK-001 should be ARCHIVED
        bad_code = ProductMarkingCode.query.filter_by(code='MARK-001\x1dVERIFY-001').first()
        assert bad_code.status == MarkingCodeStatus.ARCHIVED
        assert bad_code.archived_at is not None

        # MARK-003 should now be RESERVED (used as replacement)
        repl_code = ProductMarkingCode.query.filter_by(code='MARK-003\x1dVERIFY-003').first()
        assert repl_code.status in {MarkingCodeStatus.RESERVED, MarkingCodeStatus.USED}

        # Verify ARCHIVED ledger event
        archived_events = (
            OrderItemMarkingCodeAllocation.query
            .filter_by(payment_id=payment.id, action=MarkingCodeLedgerEventType.ARCHIVED)
            .all()
        )
        assert len(archived_events) == 1
        assert archived_events[0].event_metadata['tax_committee_status'] == 'WITHDRAWN'

    def test_precheck_written_off_code_replaced(self, app, db, fiscalized_click_payment):
        """WRITTEN_OFF code is also archived and replaced."""
        _configure_click_fiscal_context(app)
        sample_product = fiscalized_click_payment.order.order_items[0].product

        replacement_code = ProductMarkingCode(
            product_id=sample_product.id, code='MARK-003\x1dVERIFY-003', status=MarkingCodeStatus.AVAILABLE,
        )
        db.session.add(replacement_code)
        db.session.flush()

        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        check_responses = [
            {'MARK-001': 'WRITTEN_OFF', 'MARK-002': 'RECEIVED'},
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},
        ]
        mock_check = Mock(side_effect=check_responses)
        mock_utilise = Mock(return_value={'reportId': 'RPT-WO'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes', mock_utilise):
            result = service.utilise_marking_codes_with_tax_committee(payment)

        assert result['utilised'] == 2

        bad_code = ProductMarkingCode.query.filter_by(code='MARK-001\x1dVERIFY-001').first()
        assert bad_code.status == MarkingCodeStatus.ARCHIVED

        archived_events = (
            OrderItemMarkingCodeAllocation.query
            .filter_by(payment_id=payment.id, action=MarkingCodeLedgerEventType.ARCHIVED)
            .all()
        )
        assert len(archived_events) == 1
        assert archived_events[0].event_metadata['tax_committee_status'] == 'WRITTEN_OFF'

    def test_precheck_no_replacement_available_raises(self, app, db, fiscalized_click_payment):
        """If a code is WITHDRAWN and no replacement is available, raise ValidationError."""
        _configure_click_fiscal_context(app)
        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        # Both codes are reserved; no AVAILABLE codes left for replacement
        mock_check = Mock(return_value={'MARK-001': 'WITHDRAWN', 'MARK-002': 'RECEIVED'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check):
            with pytest.raises(ValidationError, match='No replacement marking code available'):
                service.utilise_marking_codes_with_tax_committee(payment)

    def test_precheck_replacement_also_invalid_triggers_second_round(self, app, db, fiscalized_click_payment):
        """If replacement code is also WITHDRAWN, a second round replaces it again."""
        _configure_click_fiscal_context(app)
        sample_product = fiscalized_click_payment.order.order_items[0].product

        # Two replacement candidates
        db.session.add_all([
            ProductMarkingCode(product_id=sample_product.id, code='MARK-003\x1dVERIFY-003', status=MarkingCodeStatus.AVAILABLE),
            ProductMarkingCode(product_id=sample_product.id, code='MARK-004\x1dVERIFY-004', status=MarkingCodeStatus.AVAILABLE),
        ])
        db.session.flush()

        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        check_responses = [
            {'MARK-001': 'WITHDRAWN', 'MARK-002': 'RECEIVED'},       # Round 1: MARK-001 bad
            {'MARK-003': 'WRITTEN_OFF', 'MARK-002': 'RECEIVED'},     # Round 2: replacement MARK-003 also bad
            {'MARK-004': 'RECEIVED', 'MARK-002': 'RECEIVED'},        # Round 3: second replacement OK
            {'MARK-004': 'RECEIVED', 'MARK-002': 'RECEIVED'},        # Status check for utilise split
        ]
        mock_check = Mock(side_effect=check_responses)
        mock_utilise = Mock(return_value={'reportId': 'RPT-DOUBLE-REPLACE'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes', mock_utilise):
            result = service.utilise_marking_codes_with_tax_committee(payment)

        assert result['utilised'] == 2

        # Both MARK-001 and MARK-003 should be archived
        for code_str in ['MARK-001\x1dVERIFY-001', 'MARK-003\x1dVERIFY-003']:
            code = ProductMarkingCode.query.filter_by(code=code_str).first()
            assert code.status == MarkingCodeStatus.ARCHIVED

        # MARK-004 used as final replacement
        final = ProductMarkingCode.query.filter_by(code='MARK-004\x1dVERIFY-004').first()
        assert final.status in {MarkingCodeStatus.RESERVED, MarkingCodeStatus.USED}

    def test_precheck_stock_synced_after_replacement(self, app, db, fiscalized_click_payment):
        """Stock quantity is synced after codes are replaced."""
        _configure_click_fiscal_context(app)
        sample_product = fiscalized_click_payment.order.order_items[0].product

        db.session.add(
            ProductMarkingCode(product_id=sample_product.id, code='MARK-003\x1dVERIFY-003', status=MarkingCodeStatus.AVAILABLE),
        )
        db.session.flush()
        # Before: 3 codes total (MARK-001, MARK-002 available, MARK-003 available)
        # After reserve: 2 reserved, 1 available -> stock = 1
        # After replace MARK-001 (archived) with MARK-003 (reserved) -> stock = 0

        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)

        service.reserve_required_marking_codes(payment)
        db.session.flush()

        check_responses = [
            {'MARK-001': 'WITHDRAWN', 'MARK-002': 'RECEIVED'},
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},
        ]
        mock_check = Mock(side_effect=check_responses)
        mock_utilise = Mock(return_value={'reportId': 'RPT-STOCK'})

        with patch.object(service.tax_committee_service, 'check_marking_code_statuses', mock_check), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes', mock_utilise):
            service.utilise_marking_codes_with_tax_committee(payment)

        # All available codes used up: MARK-001 archived, MARK-002 reserved, MARK-003 reserved
        assert sample_product.stock_quantity == 0
