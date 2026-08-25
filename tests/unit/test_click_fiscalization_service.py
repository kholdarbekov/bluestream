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
    OrderStatus,
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

    @pytest.mark.parametrize(
        "order_status,expected_payment_status,expected_code_status",
        [
            (OrderStatus.CANCELLED, PaymentStatus.CANCELLED, MarkingCodeStatus.AVAILABLE),
            (OrderStatus.PENDING, PaymentStatus.PENDING, MarkingCodeStatus.RESERVED),
        ],
        ids=["order_resolved_releases", "order_live_keeps"],
    )
    def test_click_complete_cancellation_releases_reserved_codes(
        self,
        app,
        db,
        payment_service,
        pending_marked_click_payment,
        order_status,
        expected_payment_status,
        expected_code_status,
    ):
        """B1 fix round 2 — THIS CELL WAS ASSERTING ONLY THE CANCEL-ON-LIVE ANSWER.

        ``pending_marked_click_payment``'s order is PENDING, i.e. UNRESOLVED, so
        the original single-case version pinned exactly the defect: a declined
        Click COMPLETE killing a live order's payment and freeing its codes,
        which permanently locks the customer out of retrying the same link. It
        sat ~50 lines from the keep-on-live-order cells, so this file asserted
        both answers to one question.

        Parametrized rather than deleted: the release genuinely must still fire
        once the ORDER has resolved, and that is what this cell was really for.
        """
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

        # Set the order state only AFTER prepare: the Phase 4A PREPARE guard
        # refuses a resolved order outright (-9), so flipping it earlier would
        # mean no codes were ever reserved and the cell would prove nothing.
        payment.order.status = order_status
        db.session.commit()

        response = provider.handle_complete(complete_payload)

        db.session.refresh(payment)
        db.session.refresh(marking_code)
        # The protocol answer is the same either way — the transaction really
        # was declined. Only OUR payment lifecycle differs.
        assert response['error'] == -9
        assert payment.status == expected_payment_status
        assert marking_code.status == expected_code_status
        if expected_payment_status == PaymentStatus.CANCELLED:
            assert payment.failure_reason == 'Cancelled by Click'
        assert (payment.provider_data or {}).get('click', {}).get('click_paydoc_id') == '9988776655'

    def _cancel_via_provider_status(self, db, payment_service, payment, marking_code):
        """Reserve real codes, then drive update_payment_status with a gateway cancel."""
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

    def test_update_payment_status_keeps_a_LIVE_order_payment_and_its_codes(
        self,
        db,
        payment_service,
        pending_marked_click_payment,
    ):
        """B1 (2026-08-25) — THIS CELL'S EXPECTATION WAS INVERTED.

        It was ``test_update_payment_status_marks_cancelled_and_releases_reserved_codes``
        and asserted CANCELLED + AVAILABLE unconditionally. Its order
        (``sample_order``) is PENDING, i.e. LIVE, so it was asserting exactly the
        defect: a gateway cancel describes ONE abandoned Click attempt, and
        writing a terminal status makes the Phase 4A PREPARE guard refuse every
        future attempt on an order the customer still owes for. The release half
        is still covered — by the dead-order sibling below.
        """
        payment, marking_code = pending_marked_click_payment
        assert payment.order.status == OrderStatus.PENDING, 'fixture must be a LIVE order'

        self._cancel_via_provider_status(db, payment_service, payment, marking_code)

        assert payment.status == PaymentStatus.PENDING
        assert marking_code.status == MarkingCodeStatus.RESERVED

    def test_update_payment_status_marks_cancelled_and_releases_reserved_codes(
        self,
        db,
        payment_service,
        pending_marked_click_payment,
    ):
        """The release still fires once the ORDER itself has resolved.

        This is the original cell's real subject — that a gateway cancel returns
        the codes to the pool — re-scoped to the state where that is correct.
        """
        payment, marking_code = pending_marked_click_payment
        payment.order.status = OrderStatus.CANCELLED
        db.session.commit()

        self._cancel_via_provider_status(db, payment_service, payment, marking_code)

        assert payment.status == PaymentStatus.CANCELLED
        assert payment.failure_reason == 'Timed out'
        assert marking_code.status == MarkingCodeStatus.AVAILABLE

    def test_normalize_error_code_strict_rejects_none_empty_whitespace(self):
        with pytest.raises(ValidationError, match='missing'):
            ClickPaymentProviderService._normalize_error_code(None)
        with pytest.raises(ValidationError, match='missing'):
            ClickPaymentProviderService._normalize_error_code('')
        with pytest.raises(ValidationError, match='missing'):
            ClickPaymentProviderService._normalize_error_code('   ')
        with pytest.raises(ValidationError, match='Invalid'):
            ClickPaymentProviderService._normalize_error_code('abc')
        assert ClickPaymentProviderService._normalize_error_code('0') == 0
        assert ClickPaymentProviderService._normalize_error_code(0) == 0
        assert ClickPaymentProviderService._normalize_error_code('-9') == -9
        assert ClickPaymentProviderService._normalize_error_code(' -1 ') == -1

    def _build_pending_click_payment(self, db, sample_order):
        sample_order.payment_method = PaymentMethod.CLICK
        payment = Payment(
            order_id=sample_order.id,
            user_id=sample_order.user_id,
            payment_method=PaymentMethod.CLICK,
            amount=sample_order.total_amount,
            currency='UZS',
            status=PaymentStatus.PENDING,
            payment_id=f'click-cb-{sample_order.id}',
        )
        db.session.add(payment)
        db.session.commit()
        return payment

    def test_click_complete_missing_error_field_does_not_promote(
        self, app, db, payment_service, sample_order,
    ):
        _configure_click_fiscal_context(app)
        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True
        payment = self._build_pending_click_payment(db, sample_order)

        provider = ClickPaymentProviderService(payment_service=payment_service)
        payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-missing-err',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': sample_order.order_number,
            'merchant_prepare_id': payment.id,
            'click_paydoc_id': '1234567890',
            'amount': str(payment.amount),
            'action': '1',
            'sign_time': '1700000010',
            'error': '0',
            'error_note': '',
        })
        # Drop the error field after signing — Click's signature does not cover it.
        payload.pop('error')

        with patch.object(payment_service, '_handle_successful_payment') as handle_success, patch.object(
            payment_service, 'queue_click_fiscalization',
        ) as queue_fiscalization:
            response = provider.handle_complete(payload)

        db.session.refresh(payment)
        assert response['error'] == -8
        assert response['error_note'] == 'Error in request'
        # B1 fix round 2: the subject of this cell is "does NOT promote", and
        # that still holds — handle_success is never called and we answer -8.
        # What changed is the payment's own status: `sample_order` is PENDING,
        # i.e. UNRESOLVED, and a payload we could not parse is LESS evidence than
        # an affirmative cancel, so it may not end a live order's payment either.
        # See tests/unit/test_update_payment_status_live_order_guard.py for the
        # full ruling and its resolved-order boundary.
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None
        handle_success.assert_not_called()
        queue_fiscalization.assert_not_called()

    def test_click_complete_empty_error_field_does_not_promote(
        self, app, db, payment_service, sample_order,
    ):
        _configure_click_fiscal_context(app)
        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True
        payment = self._build_pending_click_payment(db, sample_order)

        provider = ClickPaymentProviderService(payment_service=payment_service)
        payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-empty-err',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': sample_order.order_number,
            'merchant_prepare_id': payment.id,
            'click_paydoc_id': '1234567890',
            'amount': str(payment.amount),
            'action': '1',
            'sign_time': '1700000011',
            'error': '0',
            'error_note': '',
        })
        payload['error'] = ''

        with patch.object(payment_service, '_handle_successful_payment') as handle_success, patch.object(
            payment_service, 'queue_click_fiscalization',
        ) as queue_fiscalization:
            response = provider.handle_complete(payload)

        db.session.refresh(payment)
        assert response['error'] == -8
        # B1 fix round 2: the subject of this cell is "does NOT promote", and
        # that still holds — handle_success is never called and we answer -8.
        # What changed is the payment's own status: `sample_order` is PENDING,
        # i.e. UNRESOLVED, and a payload we could not parse is LESS evidence than
        # an affirmative cancel, so it may not end a live order's payment either.
        # See tests/unit/test_update_payment_status_live_order_guard.py for the
        # full ruling and its resolved-order boundary.
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None
        handle_success.assert_not_called()
        queue_fiscalization.assert_not_called()

    def test_click_complete_zero_error_missing_click_trans_id_does_not_promote(
        self, app, db, payment_service, sample_order,
    ):
        _configure_click_fiscal_context(app)
        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True
        payment = self._build_pending_click_payment(db, sample_order)

        provider = ClickPaymentProviderService(payment_service=payment_service)
        payload = _sign_click_payload(provider, {
            'click_trans_id': '',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': sample_order.order_number,
            'merchant_prepare_id': payment.id,
            'click_paydoc_id': '1234567890',
            'amount': str(payment.amount),
            'action': '1',
            'sign_time': '1700000012',
            'error': '0',
            'error_note': 'Success',
        })

        with patch.object(payment_service, '_handle_successful_payment') as handle_success, patch.object(
            payment_service, 'queue_click_fiscalization',
        ) as queue_fiscalization:
            response = provider.handle_complete(payload)

        db.session.refresh(payment)
        assert response['error'] == -8
        # B1 fix round 2: the subject of this cell is "does NOT promote", and
        # that still holds — handle_success is never called and we answer -8.
        # What changed is the payment's own status: `sample_order` is PENDING,
        # i.e. UNRESOLVED, and a payload we could not parse is LESS evidence than
        # an affirmative cancel, so it may not end a live order's payment either.
        # See tests/unit/test_update_payment_status_live_order_guard.py for the
        # full ruling and its resolved-order boundary.
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None
        handle_success.assert_not_called()
        queue_fiscalization.assert_not_called()

    def test_click_complete_zero_error_missing_click_paydoc_id_does_not_promote(
        self, app, db, payment_service, sample_order,
    ):
        _configure_click_fiscal_context(app)
        app.config['CLICK_SHOP_SECRET_KEY'] = 'click-secret'
        app.config['CLICK_TEST_MODE'] = True
        payment = self._build_pending_click_payment(db, sample_order)

        provider = ClickPaymentProviderService(payment_service=payment_service)
        payload = _sign_click_payload(provider, {
            'click_trans_id': 'txn-no-paydoc',
            'service_id': provider.service_id or '1',
            'merchant_trans_id': sample_order.order_number,
            'merchant_prepare_id': payment.id,
            'click_paydoc_id': '',
            'amount': str(payment.amount),
            'action': '1',
            'sign_time': '1700000013',
            'error': '0',
            'error_note': 'Success',
        })

        with patch.object(payment_service, '_handle_successful_payment') as handle_success, patch.object(
            payment_service, 'queue_click_fiscalization',
        ) as queue_fiscalization:
            response = provider.handle_complete(payload)

        db.session.refresh(payment)
        assert response['error'] == -8
        # B1 fix round 2: the subject of this cell is "does NOT promote", and
        # that still holds — handle_success is never called and we answer -8.
        # What changed is the payment's own status: `sample_order` is PENDING,
        # i.e. UNRESOLVED, and a payload we could not parse is LESS evidence than
        # an affirmative cancel, so it may not end a live order's payment either.
        # See tests/unit/test_update_payment_status_live_order_guard.py for the
        # full ruling and its resolved-order boundary.
        assert payment.status == PaymentStatus.PENDING
        assert payment.failure_reason is None
        handle_success.assert_not_called()
        queue_fiscalization.assert_not_called()

    def test_update_payment_status_leaves_pending_when_provider_raises(
        self, db, payment_service, pending_marked_click_payment,
    ):
        payment, _marking_code = pending_marked_click_payment
        original_status = payment.status

        with patch.object(
            payment_service,
            '_get_click_provider_service',
            return_value=Mock(check_payment_status=Mock(side_effect=PaymentError('boom'))),
        ):
            result = payment_service.update_payment_status(payment)

        db.session.refresh(payment)
        assert payment.status == original_status == PaymentStatus.PENDING
        assert result is payment

    def test_update_payment_status_does_not_promote_without_provider_transaction_id(
        self, db, payment_service, pending_marked_click_payment,
    ):
        payment, _marking_code = pending_marked_click_payment

        with patch.object(
            payment_service,
            '_get_click_provider_service',
            return_value=Mock(check_payment_status=Mock(return_value={
                'status': 'completed',
                'provider_transaction_id': None,
                'raw': {'payment_status': 2},
            })),
        ), patch.object(payment_service, '_handle_successful_payment') as handle_success, patch.object(
            payment_service, 'queue_click_fiscalization',
        ) as queue_fiscalization:
            payment_service.update_payment_status(payment)

        db.session.refresh(payment)
        assert payment.status == PaymentStatus.PENDING
        handle_success.assert_not_called()
        queue_fiscalization.assert_not_called()

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

        # payment_status=2 is Click's verified "success" code (2026-07-09 live
        # verification); the mapping heals a poll to COMPLETED only on this
        # affirmative code, not on 1 ("processing").
        with patch.object(provider, 'merchant_request', return_value={'error_code': 0, 'payment_status': 2}) as merchant_request:
            payload = provider.check_payment_status(payment)

        assert payload['status'] == PaymentStatus.COMPLETED.value
        kwargs = merchant_request.call_args.kwargs
        assert kwargs['method'] == 'GET'
        assert kwargs['fallback_path'].endswith('/payment/status/98060/22110099')

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

    def test_build_payload_excludes_reward_item(self, app, db, fiscalized_click_payment, sample_product):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 555
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        # Free bonus unit of the SAME marked product (the 6th-bottle scenario).
        # The fixture has exactly 2 marking codes for 2 paid units; if the reward
        # line were NOT excluded, reservation would need a 3rd code and raise.
        db.session.add(OrderItem(
            order_id=payment.order_id, product_id=sample_product.id,
            quantity=1, unit_price=Decimal('0.00'), total_price=Decimal('0.00'),
            is_reward_item=True,
        ))
        db.session.commit()

        service.reserve_required_marking_codes(payment)
        payload = service.build_click_fiscalization_payload(payment)

        assert len(payload['items']) == 1            # reward line is not on the receipt
        assert payload['items'][0]['Amount'] == 2000  # only the 2 paid units
        assert payload['items'][0]['Labels'] == ['MARK-001', 'MARK-002']

    def test_reserve_skips_reward_item(self, app, db, fiscalized_click_payment, sample_product):
        _configure_click_fiscal_context(app)
        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)
        db.session.add(OrderItem(
            order_id=payment.order_id, product_id=sample_product.id,
            quantity=1, unit_price=Decimal('0.00'), total_price=Decimal('0.00'),
            is_reward_item=True,
        ))
        db.session.commit()

        # Must NOT raise "Not enough marking codes" (would, if it tried to reserve 3).
        result = service.reserve_required_marking_codes(payment)
        assert result['reserved'] == 2
        reserved = [oi for (oi, _code) in service._codes_currently_held(payment)]
        assert all(oi.is_reward_item is False for oi in reserved)
        assert len(reserved) == 2

    def test_requires_fiscalization_false_when_only_reward_item_is_fiscalized(
        self, app, db, sample_order, sample_product
    ):
        _configure_click_fiscal_context(app)
        sample_order.payment_method = PaymentMethod.CLICK
        sample_order.subtotal = Decimal('0.00')
        sample_order.total_amount = Decimal('0.00')
        db.session.add(ProductFiscalProfile(
            product_id=sample_product.id, spic='SPIC-X', package_code='PK-X',
            units='1213733', vat_percent=Decimal('12.00'),
            fiscalization_enabled=True, requires_marking_codes=True,
        ))
        db.session.flush()
        db.session.add(OrderItem(
            order_id=sample_order.id, product_id=sample_product.id,
            quantity=1, unit_price=Decimal('0.00'), total_price=Decimal('0.00'),
            is_reward_item=True,
        ))
        payment = Payment(
            order_id=sample_order.id, user_id=sample_order.user_id,
            payment_method=PaymentMethod.CLICK, amount=Decimal('0.00'),
            currency='UZS', status=PaymentStatus.COMPLETED, payment_id='click-reward-only',
        )
        db.session.add(payment)
        db.session.commit()

        service = PaymentFiscalizationService(click_provider_service=Mock())
        assert service.payment_requires_click_fiscalization(payment) is False

    def test_discount_single_line_sets_discount_and_net_vat(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 600
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        order = payment.order
        order.delivery_fee = Decimal('0.00')       # delivery is always free in prod
        order.loyalty_discount = Decimal('5000.00')
        order.total_amount = Decimal('25000.00')   # 30000 subtotal - 5000 discount
        payment.amount = Decimal('25000.00')
        db.session.flush()
        service.reserve_required_marking_codes(payment)

        payload = service.build_click_fiscalization_payload(payment)
        item = payload['items'][0]

        assert item['Price'] == 3000000            # gross line total (tiyin)
        assert item['Discount'] == 500000          # 5000 UZS off
        assert item['VAT'] == 300000               # VAT on net 25000 -> 3000.00
        assert payload['received_card'] == 2500000
        assert item['Price'] - item['Discount'] == payload['received_card']

    def test_discount_zero_omits_discount_key(self, app, db, fiscalized_click_payment):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 601
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)
        payment = Payment.query.get(fiscalized_click_payment.id)
        service.reserve_required_marking_codes(payment)

        payload = service.build_click_fiscalization_payload(payment)
        item = payload['items'][0]

        assert 'Discount' not in item              # no loyalty discount -> key omitted
        assert item['VAT'] == 360000               # unchanged: VAT on full 30000

    def test_discount_spills_across_lines(self, app, db, sample_order, sample_category):
        _configure_click_fiscal_context(app)
        provider = Mock()
        provider.resolve_click_payment_id.return_value = 602
        provider.service_id = '98060'
        service = PaymentFiscalizationService(click_provider_service=provider)

        def _fiscal_product(name, price):
            p = Product(name=name, description='d', category_id=sample_category.id,
                        size='19L', volume=19.0, volume_unit='L', base_price=Decimal(str(price)),
                        stock_quantity=100, min_stock_level=1, max_stock_level=200, is_active=True)
            db.session.add(p); db.session.flush()
            db.session.add(ProductFiscalProfile(
                product_id=p.id, spic=f'SPIC-{name}', package_code=f'PK-{name}', units='1213733',
                vat_percent=Decimal('12.00'), fiscalization_enabled=True, requires_marking_codes=False,
            ))
            db.session.flush()
            return p

        p1 = _fiscal_product('P1', 50000)
        p2 = _fiscal_product('P2', 40000)
        sample_order.payment_method = PaymentMethod.CLICK
        sample_order.delivery_fee = Decimal('0.00')
        sample_order.subtotal = Decimal('90000.00')
        sample_order.loyalty_discount = Decimal('60000.00')
        sample_order.total_amount = Decimal('30000.00')
        db.session.add_all([
            OrderItem(order_id=sample_order.id, product_id=p1.id, quantity=1,
                      unit_price=Decimal('50000.00'), total_price=Decimal('50000.00')),
            OrderItem(order_id=sample_order.id, product_id=p2.id, quantity=1,
                      unit_price=Decimal('40000.00'), total_price=Decimal('40000.00')),
        ])
        payment = Payment(order_id=sample_order.id, user_id=sample_order.user_id,
                          payment_method=PaymentMethod.CLICK, amount=Decimal('30000.00'),
                          currency='UZS', status=PaymentStatus.COMPLETED, payment_id='click-spill')
        db.session.add(payment); db.session.commit()

        payload = service.build_click_fiscalization_payload(payment)
        items = payload['items']

        assert items[0]['Price'] == 5000000 and items[0]['Discount'] == 5000000  # line1 fully discounted
        assert items[0]['VAT'] == 0
        assert items[1]['Price'] == 4000000 and items[1]['Discount'] == 1000000  # remainder spills
        assert items[1]['VAT'] == 360000                                          # VAT on net 30000
        assert payload['received_card'] == 3000000
        assert sum(i['Price'] - i.get('Discount', 0) for i in items) == payload['received_card']


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

    def test_precheck_replacement_code_is_owned_by_the_order(self, app, db, fiscalized_click_payment):
        """A substituted replacement must carry order_id, exactly like a normally
        reserved code. Without it the ownership conjunct in _codes_currently_held
        drops the code and build_click_fiscalization_payload raises
        "Expected N labels ... got N-1"."""
        _configure_click_fiscal_context(app)
        sample_product = fiscalized_click_payment.order.order_items[0].product
        db.session.add(ProductMarkingCode(
            product_id=sample_product.id, code='MARK-003\x1dVERIFY-003', status=MarkingCodeStatus.AVAILABLE,
        ))
        db.session.flush()

        service = PaymentFiscalizationService(click_provider_service=Mock())
        payment = Payment.query.get(fiscalized_click_payment.id)
        service.reserve_required_marking_codes(payment)
        db.session.flush()

        check_responses = [
            {'MARK-001': 'WITHDRAWN', 'MARK-002': 'RECEIVED'},
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},
            {'MARK-003': 'RECEIVED', 'MARK-002': 'RECEIVED'},
        ]
        with patch.object(service.tax_committee_service, 'check_marking_code_statuses',
                          Mock(side_effect=check_responses)), \
             patch.object(service.tax_committee_service, 'utilise_marking_codes',
                          Mock(return_value={'reportId': 'RPT-REPLACED'})):
            service.utilise_marking_codes_with_tax_committee(payment)

        repl = ProductMarkingCode.query.filter_by(code='MARK-003\x1dVERIFY-003').first()
        assert repl.order_id == payment.order_id, (
            'a replacement code must be owned by the order it was substituted into'
        )

        # And it must survive the ownership filter, so the payload stays complete.
        held_ids = {code.id for _item, code in service._codes_currently_held(payment)}
        assert repl.id in held_ids

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


# ---------------------------------------------------------------------------
# Marking-code ownership SSOT — regression pins for prod incident TG_000413_26
# (order 1100 / payment 1204, 2026-08-20/21).
#
# The reconcile timeout released payment 1204's three codes; 34 minutes later a
# different order re-reserved, utilised and USED those exact codes. When the
# late Click debit arrived, `reserve_required_marking_codes` logged `reserved=0`
# because the ownership question was answered from the ledger's `reserved` rows
# plus the code's CURRENT status alone -- never checking who owns the code now,
# and never noticing the later `released` row. Fiscalizing then would have put
# the other order's labels on this order's tax receipt.
# ---------------------------------------------------------------------------


@pytest.fixture
def two_click_payments_sharing_a_pool(db, sample_order, sample_product):
    """Two CLICK orders on one product, and a pool of exactly four codes.

    Returns (payment_a, payment_b, [code1..code4]).
    """
    from business_app.models.order import Order
    from shared.enums import OrderStatus

    db.session.add(ProductFiscalProfile(
        product_id=sample_product.id,
        spic='SPIC-19L',
        package_code='PACK-19L',
        units='1213733',
        vat_percent=Decimal('12.00'),
        fiscalization_enabled=True,
        requires_marking_codes=True,
    ))
    db.session.flush()

    sample_order.payment_method = PaymentMethod.CLICK
    order_b = Order(
        user_id=sample_order.user_id,
        order_number='ORD-TEST-002',
        status=OrderStatus.PENDING,
        subtotal=Decimal('15000.00'),
        delivery_fee=Decimal('3000.00'),
        discount_amount=Decimal('0.00'),
        loyalty_discount=Decimal('0.00'),
        total_amount=Decimal('18000.00'),
        payment_method=PaymentMethod.CLICK,
    )
    db.session.add(order_b)
    db.session.flush()

    payments = []
    for order in (sample_order, order_b):
        db.session.add(OrderItem(
            order_id=order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal('15000.00'),
            total_price=Decimal('30000.00'),
        ))
        payment = Payment(
            order_id=order.id,
            user_id=order.user_id,
            payment_method=PaymentMethod.CLICK,
            amount=Decimal('18000.00'),
            currency='UZS',
            status=PaymentStatus.PENDING,
            payment_id=f'click-{order.order_number}',
        )
        db.session.add(payment)
        payments.append(payment)

    codes = [
        ProductMarkingCode(
            product_id=sample_product.id,
            code=f'MARK-{i:03d}\x1dVERIFY-{i:03d}',
            status=MarkingCodeStatus.AVAILABLE,
        )
        for i in range(1, 5)
    ]
    db.session.add_all(codes)
    db.session.commit()
    return payments[0], payments[1], codes


class TestMarkingCodeOwnershipSSOT:
    def test_released_codes_retaken_by_another_order_are_not_still_held(
        self, db, two_click_payments_sharing_a_pool
    ):
        """The exact TG_000413_26 shape: reserve -> release -> another order takes
        them -> the original payment must NOT believe it still holds them."""
        payment_a, payment_b, _codes = two_click_payments_sharing_a_pool
        service = PaymentFiscalizationService(click_provider_service=Mock())

        assert service.reserve_required_marking_codes(payment_a)['reserved'] == 2
        service.release_reserved_marking_codes(payment_a, reason='payment_timeout')
        db.session.commit()

        # Payment B now takes the very codes A released (oldest-first draw order).
        assert service.reserve_required_marking_codes(payment_b)['reserved'] == 2
        db.session.commit()

        b_code_ids = {
            c.id for c in ProductMarkingCode.query.filter_by(order_id=payment_b.order_id).all()
        }
        assert len(b_code_ids) == 2

        # THE BUG: A's stale `reserved` ledger rows still point at those codes, and
        # the codes are RESERVED again -- so A believed it already held 2.
        result = service.reserve_required_marking_codes(payment_a)
        db.session.commit()

        assert result['reserved'] == 2, (
            'payment A must draw two FRESH codes; it no longer owns the ones it released'
        )
        a_code_ids = {
            c.id for c in ProductMarkingCode.query.filter_by(order_id=payment_a.order_id).all()
        }
        assert len(a_code_ids) == 2
        assert a_code_ids.isdisjoint(b_code_ids), 'payment A must not re-claim order B codes'

    def test_reserved_code_lookup_never_returns_another_orders_codes(
        self, db, two_click_payments_sharing_a_pool
    ):
        """A fiscal receipt must never carry a label belonging to another order."""
        payment_a, payment_b, _codes = two_click_payments_sharing_a_pool
        service = PaymentFiscalizationService(click_provider_service=Mock())

        service.reserve_required_marking_codes(payment_a)
        service.release_reserved_marking_codes(payment_a, reason='payment_timeout')
        db.session.commit()
        service.reserve_required_marking_codes(payment_b)
        db.session.commit()

        held = service._codes_currently_held(payment_a)

        b_code_ids = {
            c.id for c in ProductMarkingCode.query.filter_by(order_id=payment_b.order_id).all()
        }
        assert not ({code.id for _item, code in held} & b_code_ids), (
            'payment A must not surface order B codes into its fiscal payload'
        )

    def test_release_does_not_touch_a_code_another_order_now_owns(
        self, db, two_click_payments_sharing_a_pool
    ):
        """Releasing A a second time must not free B's codes out from under it."""
        payment_a, payment_b, _codes = two_click_payments_sharing_a_pool
        service = PaymentFiscalizationService(click_provider_service=Mock())

        service.reserve_required_marking_codes(payment_a)
        service.release_reserved_marking_codes(payment_a, reason='payment_timeout')
        db.session.commit()
        service.reserve_required_marking_codes(payment_b)
        db.session.commit()

        service.release_reserved_marking_codes(payment_a, reason='payment_timeout')
        db.session.commit()

        still_b = ProductMarkingCode.query.filter_by(order_id=payment_b.order_id).all()
        assert len(still_b) == 2
        assert all(c.status == MarkingCodeStatus.RESERVED for c in still_b)


# --------------------------------------------------------------------------- #
# Reservation is ALL-OR-NOTHING (Task 9 fix, 2026-08-24).
#
# `reserve_required_marking_codes` used to loop `order.order_items`
# sequentially, RESERVE each line's codes as it succeeded, and raise
# ValidationError from INSIDE the loop on the first short line. Every caller
# that answers a protocol code instead of re-raising -- `handle_prepare`
# (-9), `_restore_click_rail_after_offline_settlement`, `_accept_late_complete`
# -- then reached `PaymentService.handle_click_webhook`'s UNCONDITIONAL commit,
# so earlier lines' codes were durably stuck RESERVED against an attempt the
# gateway had been told was cancelled.
#
# The fix plans under FOR UPDATE first and mutates only when every line is
# covered. These tests pin both halves: the all-or-nothing guarantee, and the
# same-product duplicate-line hazard the restructure could otherwise introduce.
# --------------------------------------------------------------------------- #

@pytest.fixture
def order_with_two_lines_of_one_product(db, sample_order, sample_product):
    """ONE order, TWO OrderItem rows carrying the SAME product (qty 2 + qty 1).

    `order_items` has NO unique constraint on `(order_id, product_id)` and real
    orders exercise it (dev DB order 141 = items 159 + 160, both product 2).
    The old sequential loop kept the two lines apart only by accident of
    autoflush; a planner that reads without writing must exclude codes it has
    already planned or it hands the same physical code to both lines.

    Returns ``(payment, [code1, code2, code3])``.
    """
    db.session.add(ProductFiscalProfile(
        product_id=sample_product.id,
        spic='SPIC-DUP',
        package_code='PACK-DUP',
        units='1213733',
        vat_percent=Decimal('12.00'),
        fiscalization_enabled=True,
        requires_marking_codes=True,
    ))
    sample_order.payment_method = PaymentMethod.CLICK
    db.session.add_all([
        OrderItem(
            order_id=sample_order.id, product_id=sample_product.id, quantity=2,
            unit_price=Decimal('15000.00'), total_price=Decimal('30000.00'),
        ),
        OrderItem(
            order_id=sample_order.id, product_id=sample_product.id, quantity=1,
            unit_price=Decimal('15000.00'), total_price=Decimal('15000.00'),
        ),
    ])
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=Decimal('45000.00'),
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id='click-dup-lines',
    )
    db.session.add(payment)
    codes = [
        ProductMarkingCode(
            product_id=sample_product.id,
            code=f'DUP-{i:03d}\x1dVERIFY-DUP-{i:03d}',
            status=MarkingCodeStatus.AVAILABLE,
        )
        for i in range(1, 4)
    ]
    db.session.add_all(codes)
    db.session.commit()
    return payment, codes


@pytest.fixture
def two_product_click_order(db, sample_order, sample_product):
    """A two-line CLICK order: product A (qty 1) and product B (qty 1).

    Both products require marking codes; the caller decides how deep each pool
    is. Returns ``(payment, product_a, product_b)``.
    """
    product_b = Product(
        name='Product B (second pool)',
        category_id=sample_product.category_id,
        size='19L',
        volume=19.0,
        volume_unit='L',
        base_price=Decimal('15000.00'),
        stock_quantity=0,
        is_active=True,
    )
    db.session.add(product_b)
    db.session.flush()

    db.session.add_all([
        ProductFiscalProfile(
            product_id=sample_product.id, spic='SPIC-A', package_code='PACK-A',
            units='1213733', vat_percent=Decimal('12.00'),
            fiscalization_enabled=True, requires_marking_codes=True,
        ),
        ProductFiscalProfile(
            product_id=product_b.id, spic='SPIC-B', package_code='PACK-B',
            units='1213733', vat_percent=Decimal('12.00'),
            fiscalization_enabled=True, requires_marking_codes=True,
        ),
    ])
    sample_order.payment_method = PaymentMethod.CLICK
    db.session.add_all([
        OrderItem(
            order_id=sample_order.id, product_id=sample_product.id, quantity=1,
            unit_price=Decimal('15000.00'), total_price=Decimal('15000.00'),
        ),
        OrderItem(
            order_id=sample_order.id, product_id=product_b.id, quantity=1,
            unit_price=Decimal('15000.00'), total_price=Decimal('15000.00'),
        ),
    ])
    payment = Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        payment_method=PaymentMethod.CLICK,
        amount=Decimal('30000.00'),
        currency='UZS',
        status=PaymentStatus.PENDING,
        payment_id='click-two-products',
    )
    db.session.add(payment)
    db.session.commit()
    return payment, sample_product, product_b


def _seed_codes(db, product, count, prefix):
    codes = [
        ProductMarkingCode(
            product_id=product.id,
            code=f'{prefix}-{i:03d}\x1dVERIFY-{prefix}-{i:03d}',
            status=MarkingCodeStatus.AVAILABLE,
        )
        for i in range(1, count + 1)
    ]
    db.session.add_all(codes)
    db.session.commit()
    return codes


@pytest.mark.unit
@pytest.mark.payment
class TestReservationIsAllOrNothing:
    def test_two_lines_of_the_same_product_never_share_a_physical_code(
        self, db, order_with_two_lines_of_one_product
    ):
        """T2 -- the regression only the plan-then-mutate restructure can introduce.

        Planning reads without writing, so without an explicit
        `planned_code_ids` exclusion both lines' queries see the same three
        AVAILABLE rows and the qty-1 line is handed a code the qty-2 line
        already holds. That would print one physical label on two receipt
        lines: strictly worse than the leak being fixed.
        """
        payment, codes = order_with_two_lines_of_one_product
        service = PaymentFiscalizationService(click_provider_service=Mock())

        assert service.reserve_required_marking_codes(payment)['reserved'] == 3
        db.session.commit()

        held = service._codes_currently_held(payment)
        assert len(held) == 3, 'three units ordered, three code allocations'
        assert len({code.id for _item, code in held}) == 3, (
            'each order line must hold its OWN physical code -- no code may be '
            'handed to two lines of the same product'
        )
        assert {c.id for c in codes} == {code.id for _item, code in held}

    def test_a_short_pool_across_duplicate_lines_reserves_nothing(
        self, db, order_with_two_lines_of_one_product
    ):
        """Three units ordered, two codes in the pool -> refuse, keep both."""
        payment, codes = order_with_two_lines_of_one_product
        codes[2].status = MarkingCodeStatus.ARCHIVED
        db.session.commit()
        service = PaymentFiscalizationService(click_provider_service=Mock())

        with pytest.raises(ValidationError):
            service.reserve_required_marking_codes(payment)

        # The caller (handle_prepare -> handle_click_webhook) COMMITS after a
        # refusal, because the -9 is a return value and not an exception.
        # Committing here is what makes the leak durable in production.
        db.session.commit()
        db.session.expire_all()

        assert ProductMarkingCode.query.filter_by(status=MarkingCodeStatus.RESERVED).count() == 0
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=payment.order_id).count() == 0

    def test_a_shortfall_on_the_second_product_leaves_the_first_untouched(
        self, db, two_product_click_order
    ):
        """The prod shape: A's pool covers its line, B's is empty."""
        payment, product_a, _product_b = two_product_click_order
        _seed_codes(db, product_a, 1, 'A')
        service = PaymentFiscalizationService(click_provider_service=Mock())

        with pytest.raises(ValidationError):
            service.reserve_required_marking_codes(payment)

        db.session.commit()
        db.session.expire_all()

        assert ProductMarkingCode.query.filter_by(
            product_id=product_a.id, status=MarkingCodeStatus.AVAILABLE
        ).count() == 1, "product A's code must not be spent by a refused attempt"
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=payment.order_id).count() == 0

    def test_a_covered_line_is_skipped_so_a_retry_never_over_releases(
        self, db, two_product_click_order
    ):
        """T3 -- the idempotent-retry shape driven by :429 / :871 / :1015.

        After a successful reservation, B's pool is emptied. A second reserve
        must NOT be short (B's line is already fully covered, so the planner
        skips it), must draw nothing new, and must not release, re-stamp or
        audit anything. The last two assertions are what a compensating-release
        fix would fail.
        """
        payment, product_a, product_b = two_product_click_order
        _seed_codes(db, product_a, 1, 'A')
        b_codes = _seed_codes(db, product_b, 1, 'B')
        service = PaymentFiscalizationService(click_provider_service=Mock())

        assert service.reserve_required_marking_codes(payment)['reserved'] == 2
        db.session.commit()

        held_ids = {code.id for _item, code in service._codes_currently_held(payment)}
        reserved_at_before = {
            c.id: c.reserved_at for c in ProductMarkingCode.query.filter(
                ProductMarkingCode.id.in_(held_ids)
            ).all()
        }
        allocations_before = OrderItemMarkingCodeAllocation.query.filter_by(
            order_id=payment.order_id
        ).count()
        stock_a_before = Product.query.get(product_a.id).stock_quantity

        # Product B's pool is now bone dry: a fresh draw for B would be short.
        assert ProductMarkingCode.query.filter_by(
            product_id=product_b.id, status=MarkingCodeStatus.AVAILABLE
        ).count() == 0
        assert b_codes[0].status == MarkingCodeStatus.RESERVED

        assert service.reserve_required_marking_codes(payment)['reserved'] == 0
        db.session.commit()
        db.session.expire_all()

        assert {code.id for _item, code in service._codes_currently_held(payment)} == held_ids
        held_rows = ProductMarkingCode.query.filter(ProductMarkingCode.id.in_(held_ids)).all()
        assert all(c.status == MarkingCodeStatus.RESERVED for c in held_rows)
        assert {c.id: c.reserved_at for c in held_rows} == reserved_at_before, (
            'a no-op retry must not re-stamp reserved_at'
        )
        assert OrderItemMarkingCodeAllocation.query.filter_by(
            order_id=payment.order_id
        ).count() == allocations_before, 'the append-only ledger must not grow on a no-op'
        assert Product.query.get(product_a.id).stock_quantity == stock_a_before

        trail = (Payment.query.get(payment.id).provider_data or {}).get('fiscalization_audit_trail') or []
        assert not [e for e in trail if e.get('action') == 'payment_marking_codes_released'], (
            'reserve must never release: a compensating-release fix would leave '
            'this audit row and free the TC-pre-utilised codes with it'
        )

    def test_manual_business_account_consumption_is_all_or_nothing(
        self, db, two_product_click_order
    ):
        """T7 -- the sixth, previously undocumented copy of the same defect
        (`_reserve_codes_for_manual_consumption`).

        Pins the CODES half only, which is all the fix delivers: a shortfall
        takes no code and writes no ledger row. The `fiscalization.status =
        PROCESSING` / `attempts` writes its caller made before the raise are
        still protected only by the caller rolling back, so this test
        deliberately asserts nothing about them.
        """
        payment, product_a, _product_b = two_product_click_order
        _seed_codes(db, product_a, 1, 'A')

        payment.payment_method = PaymentMethod.BUSINESS_ACCOUNT
        payment.status = PaymentStatus.COMPLETED
        payment.consume_marking_codes = True
        db.session.commit()

        service = PaymentFiscalizationService(click_provider_service=Mock())

        with pytest.raises(ValidationError):
            service.consume_marking_codes_for_business_account(payment)

        db.session.commit()
        db.session.expire_all()

        assert ProductMarkingCode.query.filter_by(
            product_id=product_a.id, status=MarkingCodeStatus.AVAILABLE
        ).count() == 1
        assert ProductMarkingCode.query.filter_by(status=MarkingCodeStatus.RESERVED).count() == 0
        assert ProductMarkingCode.query.filter_by(status=MarkingCodeStatus.USED).count() == 0
        assert OrderItemMarkingCodeAllocation.query.filter_by(order_id=payment.order_id).count() == 0
