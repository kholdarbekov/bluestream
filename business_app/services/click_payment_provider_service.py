"""Click-specific payment provider integration."""

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests
from flask import current_app

from business_app import db
from business_app.models.payment import Payment, PaymentTransaction
from business_app.utils.constants import PaymentMethod, PaymentStatus
from business_app.utils.exceptions import PaymentError, ValidationError


class ClickPaymentProviderService:
    """Encapsulates Click checkout callbacks and Merchant API calls."""

    def __init__(self, payment_service=None):
        self.payment_service = payment_service
        self.merchant_id = (
            current_app.config.get('CLICK_SHOP_MERCHANT_ID')
            or current_app.config.get('CLICK_MERCHANT_ID')
        )
        self.service_id = (
            current_app.config.get('CLICK_SHOP_SERVICE_ID')
            or current_app.config.get('CLICK_SERVICE_ID')
        )
        self.secret_key = (
            current_app.config.get('CLICK_SHOP_SECRET_KEY')
            or current_app.config.get('CLICK_SECRET_KEY')
        )
        self.checkout_url = (
            current_app.config.get('CLICK_CHECKOUT_URL')
            or 'https://my.click.uz/services/pay'
        )
        self.shop_callback_url = current_app.config.get('CLICK_SHOP_CALLBACK_URL')
        self.merchant_api_url = (
            current_app.config.get('CLICK_MERCHANT_API_URL')
            or current_app.config.get('CLICK_ENDPOINT_URL')
            or 'https://api.click.uz/v2/merchant'
        ).rstrip('/')
        self.merchant_api_username = (
            current_app.config.get('CLICK_MERCHANT_API_USERNAME')
            or current_app.config.get('CLICK_MERCHANT_API_USER')
            or current_app.config.get('CLICK_MERCHANT_ID')
        )
        self.merchant_api_password = (
            current_app.config.get('CLICK_MERCHANT_API_PASSWORD')
            or current_app.config.get('CLICK_MERCHANT_API_SECRET')
            or current_app.config.get('CLICK_SECRET_KEY')
        )
        self.merchant_api_token = current_app.config.get('CLICK_MERCHANT_API_TOKEN')
        self.timeout_seconds = int(current_app.config.get('CLICK_MERCHANT_API_TIMEOUT_SECONDS', 15))
        self.payment_timeout_minutes = int(current_app.config.get('PAYMENT_TIMEOUT_MINUTES', 60) or 60)
        self.test_mode = bool(current_app.config.get('CLICK_TEST_MODE', True))

    def create_payment_link(self, payment: Payment) -> Dict[str, str]:
        if not self.service_id or not self.merchant_id:
            raise PaymentError("Click payment service is not configured")

        now = datetime.now(timezone.utc)
        base_url = current_app.config.get('COMPANY_WEBSITE', 'http://localhost:5000').rstrip('/')
        success_url = payment.callback_url or self.shop_callback_url or f"{base_url}/payment/success?order_id={payment.order_id}"
        cancel_url = f"{base_url}/payment/cancel?order_id={payment.order_id}"
        amount = self._normalize_amount(payment.amount)

        query_params = {
            'service_id': self.service_id,
            'merchant_id': self.merchant_id,
            'amount': f"{amount}",
            'transaction_param': payment.payment_id,
            'merchant_trans_id': payment.payment_id,
            'return_url': success_url,
            'cancel_url': cancel_url,
        }
        payment_url = f"{self.checkout_url}?{urlencode(query_params)}"

        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get('click') or {})
        click_data.update({
            'checkout_url': payment_url,
            'checkout_created_at': now.isoformat(),
            'checkout_return_url': success_url,
            'checkout_cancel_url': cancel_url,
        })
        provider_data['click'] = click_data

        payment.provider_data = provider_data
        payment.payment_link = payment_url
        payment.payment_link_expires_at = now + timedelta(minutes=self.payment_timeout_minutes)
        payment.callback_url = success_url
        db.session.flush()

        return {
            'payment_url': payment_url,
            'reference': payment.payment_id,
            'expires_at': payment.payment_link_expires_at.isoformat() if payment.payment_link_expires_at else None,
        }

    def verify_signature(self, payload: Dict[str, Any]) -> bool:
        if not self.secret_key:
            current_app.logger.error("Click secret key not configured")
            return False

        click_trans_id = str(payload.get('click_trans_id') or '')
        service_id = str(payload.get('service_id') or '')
        merchant_trans_id = str(
            payload.get('merchant_trans_id')
            or payload.get('transaction_param')
            or ''
        )
        merchant_prepare_id = str(payload.get('merchant_prepare_id') or '')
        amount = str(payload.get('amount') or '')
        action = str(payload.get('action') or '')
        sign_time = str(payload.get('sign_time') or '')
        sign_string = str(payload.get('sign_string') or '')

        try:
            normalized_action = self._normalize_action(action)
        except ValidationError:
            current_app.logger.warning("Unknown Click action during signature verification: %s", action)
            return False

        sign_source = f"{click_trans_id}{service_id}{self.secret_key}{merchant_trans_id}"
        if normalized_action == 'complete':
            sign_source += merchant_prepare_id
        sign_source += f"{amount}{action}{sign_time}"
        expected = hashlib.md5(sign_source.encode('utf-8')).hexdigest()
        if expected.lower() != sign_string.lower():
            current_app.logger.warning("Invalid Click signature for merchant_trans_id=%s", merchant_trans_id)
            return False
        return True

    @staticmethod
    def _normalize_action(action: Any) -> str:
        value = str(action or '').strip().lower()
        if value in {'0', 'prepare'}:
            return 'prepare'
        if value in {'1', 'complete'}:
            return 'complete'
        raise ValidationError("Unknown Click action")

    @staticmethod
    def _normalize_amount(value: Any) -> Decimal:
        try:
            return Decimal(str(value or 0)).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError("Invalid Click amount") from exc

    @staticmethod
    def _normalize_error_code(value: Any) -> int:
        try:
            return int(str(value or 0).strip())
        except (TypeError, ValueError):
            raise ValidationError("Invalid Click error code")

    @staticmethod
    def _build_success_response(
        payment: Payment,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            'click_trans_id': payload.get('click_trans_id'),
            'merchant_trans_id': payment.payment_id,
            'merchant_confirm_id': payment.id,
            'error': 0,
            'error_note': 'Success',
        }

    @staticmethod
    def _build_error_response(error_code: int, error_note: str) -> Dict[str, Any]:
        return {
            'error': error_code,
            'error_note': error_note,
        }

    def _record_transaction(
        self,
        payment: Payment,
        transaction_type: str,
        payload: Dict[str, Any],
        *,
        success: bool = True,
        status: str = 'completed',
    ) -> PaymentTransaction:
        transaction = PaymentTransaction(
            payment_id=payment.id,
            transaction_type=transaction_type,
            amount=payment.amount,
            status=status,
            provider_transaction_id=payload.get('click_trans_id') or payload.get('receipt_id'),
            provider_reference=payload.get('merchant_trans_id') or payment.payment_id,
            provider_response=payload,
            success=success,
            processed_at=datetime.now(timezone.utc),
            failure_reason=None if success else payload.get('error_note'),
        )
        db.session.add(transaction)
        return transaction

    def _append_callback_audit(
        self,
        payment: Payment,
        *,
        stage: str,
        request_payload: Dict[str, Any],
        response_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get('click') or {})
        callbacks = list(click_data.get('callbacks') or [])
        callbacks.append({
            'stage': stage,
            'received_at': datetime.now(timezone.utc).isoformat(),
            'request': request_payload,
            'response': response_payload,
        })
        click_data['callbacks'] = callbacks[-20:]
        provider_data['click'] = click_data
        payment.provider_data = provider_data

    def _get_payment_fiscalization_service(self):
        if self.payment_service is not None:
            return self.payment_service._get_payment_fiscalization_service()

        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

        return PaymentFiscalizationService(click_provider_service=self)

    def handle_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.verify_signature(payload):
            raise PaymentError("Invalid Click signature")

        action = self._normalize_action(payload.get('action'))
        if action == 'prepare':
            return self.handle_prepare(payload)
        return self.handle_complete(payload)

    def handle_prepare(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        merchant_trans_id = str(payload.get('merchant_trans_id') or payload.get('transaction_param') or '')
        payment = Payment.query.filter_by(payment_id=merchant_trans_id).with_for_update().first()
        if not payment:
            return {'error': -5, 'error_note': 'Transaction not found'}

        requested_amount = self._normalize_amount(payload.get('amount'))
        expected_amount = self._normalize_amount(payment.amount)
        if expected_amount != requested_amount:
            response = {'error': -2, 'error_note': 'Incorrect amount'}
            self._append_callback_audit(payment, stage='prepare', request_payload=payload, response_payload=response)
            return response

        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get('click') or {})
        click_data['click_trans_id'] = str(payload.get('click_trans_id') or click_data.get('click_trans_id') or '')
        click_data['merchant_prepare_id'] = payment.id
        click_data['last_prepare_at'] = datetime.now(timezone.utc).isoformat()
        click_data['prepare_payload'] = payload
        provider_data['click'] = click_data
        payment.provider_data = provider_data
        payment.webhook_attempts = int(payment.webhook_attempts or 0) + 1

        self._get_payment_fiscalization_service().reserve_required_marking_codes(payment)
        self._record_transaction(payment, 'click_prepare', payload)

        response = {
            'click_trans_id': payload.get('click_trans_id'),
            'merchant_trans_id': payment.payment_id,
            'merchant_prepare_id': payment.id,
            'error': 0,
            'error_note': 'Success',
        }
        self._append_callback_audit(payment, stage='prepare', request_payload=payload, response_payload=response)
        db.session.flush()
        return response

    def handle_complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        merchant_prepare_id = payload.get('merchant_prepare_id')
        merchant_trans_id = str(payload.get('merchant_trans_id') or payload.get('transaction_param') or '')
        payment = None

        if merchant_prepare_id:
            payment = Payment.query.filter_by(id=merchant_prepare_id).with_for_update().first()
        if not payment and merchant_trans_id:
            payment = Payment.query.filter_by(payment_id=merchant_trans_id).with_for_update().first()
        if not payment:
            return {'error': -6, 'error_note': 'Transaction not found'}

        requested_amount = self._normalize_amount(payload.get('amount'))
        expected_amount = self._normalize_amount(payment.amount)
        if expected_amount != requested_amount:
            response = {'error': -2, 'error_note': 'Incorrect amount'}
            self._append_callback_audit(payment, stage='complete', request_payload=payload, response_payload=response)
            return response

        click_error = self._normalize_error_code(payload.get('error'))
        click_error_note = str(payload.get('error_note') or '').strip() or 'Transaction cancelled'

        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get('click') or {})
        click_data['click_trans_id'] = str(payload.get('click_trans_id') or click_data.get('click_trans_id') or '')
        click_data['merchant_prepare_id'] = merchant_prepare_id or click_data.get('merchant_prepare_id') or payment.id
        click_data['last_complete_at'] = datetime.now(timezone.utc).isoformat()
        click_data['complete_payload'] = payload
        provider_data['click'] = click_data
        payment.provider_data = provider_data
        payment.provider_transaction_id = str(payload.get('click_trans_id') or payment.provider_transaction_id or '')
        payment.webhook_processed = True
        payment.webhook_attempts = int(payment.webhook_attempts or 0) + 1

        if payment.status == PaymentStatus.COMPLETED:
            response = self._build_error_response(-4, 'Already paid')
            self._append_callback_audit(payment, stage='complete', request_payload=payload, response_payload=response)
            db.session.flush()
            return response

        if payment.status in {PaymentStatus.CANCELLED, PaymentStatus.FAILED}:
            response = self._build_error_response(-9, 'Transaction cancelled')
            self._append_callback_audit(payment, stage='complete', request_payload=payload, response_payload=response)
            db.session.flush()
            return response

        if click_error != 0:
            payment.status = PaymentStatus.CANCELLED
            payment.failure_reason = click_error_note
            self._record_transaction(
                payment,
                'click_complete_cancelled',
                payload,
                success=False,
                status='cancelled',
            )
            self._get_payment_fiscalization_service().release_reserved_marking_codes(
                payment,
                reason='click_complete_cancelled',
            )
            response = self._build_error_response(-9, 'Transaction cancelled')
            self._append_callback_audit(payment, stage='complete', request_payload=payload, response_payload=response)
            db.session.flush()
            return response

        payment.status = PaymentStatus.COMPLETED
        payment.failure_reason = None
        payment.paid_at = payment.paid_at or datetime.now(timezone.utc)
        self._record_transaction(payment, 'click_complete', payload)

        if self.payment_service:
            self.payment_service._handle_successful_payment(payment)
            self.payment_service.queue_click_fiscalization(payment.id)

        response = self._build_success_response(payment, payload)
        self._append_callback_audit(payment, stage='complete', request_payload=payload, response_payload=response)
        db.session.flush()
        return response

    def _build_merchant_headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if self.merchant_api_token:
            headers['Authorization'] = f"Bearer {self.merchant_api_token}"
        return headers

    def _resolve_merchant_url(self, configured_url: Optional[str], fallback_path: str) -> str:
        if configured_url:
            if configured_url.startswith('http://') or configured_url.startswith('https://'):
                return configured_url
            configured_url = configured_url if configured_url.startswith('/') else f'/{configured_url}'
            return f"{self.merchant_api_url}{configured_url}"

        fallback_path = fallback_path if fallback_path.startswith('/') else f'/{fallback_path}'
        return f"{self.merchant_api_url}{fallback_path}"

    def _normalize_merchant_response(self, response: Any) -> Dict[str, Any]:
        if not isinstance(response, dict):
            return {'raw': response}
        return response.get('result') or response.get('data') or response

    def merchant_request(self, payload: Dict[str, Any], *, configured_url: Optional[str] = None, fallback_path: str) -> Dict[str, Any]:
        url = self._resolve_merchant_url(configured_url, fallback_path)

        if self.test_mode:
            return {
                'success': True,
                'status': 'completed',
                'echo': payload,
                'url': url,
            }

        request_kwargs: Dict[str, Any] = {
            'url': url,
            'json': payload,
            'headers': self._build_merchant_headers(),
            'timeout': self.timeout_seconds,
        }
        if not self.merchant_api_token and self.merchant_api_username and self.merchant_api_password:
            request_kwargs['auth'] = (self.merchant_api_username, self.merchant_api_password)

        response = requests.post(**request_kwargs)
        response.raise_for_status()
        data = response.json()
        normalized = self._normalize_merchant_response(data)
        if isinstance(normalized, dict) and normalized.get('error'):
            raise PaymentError(str(normalized.get('error')))
        return normalized

    def check_payment_status(self, payment: Payment) -> Dict[str, Any]:
        if self.test_mode:
            return {
                'status': payment.status.value if hasattr(payment.status, 'value') else payment.status,
                'provider_transaction_id': payment.provider_transaction_id,
            }

        payload = {
            'payment_id': payment.payment_id,
            'provider_transaction_id': payment.provider_transaction_id,
        }
        response = self.merchant_request(
            payload,
            configured_url=current_app.config.get('CLICK_MERCHANT_STATUS_URL'),
            fallback_path=current_app.config.get('CLICK_MERCHANT_API_STATUS_PATH', '/payment/status'),
        )
        return {
            'status': response.get('status') or response.get('state') or response.get('payment_status'),
            'provider_transaction_id': response.get('provider_transaction_id') or response.get('transaction_id') or payment.provider_transaction_id,
            'raw': response,
        }

    def refund_payment(self, payment: Payment, amount: Decimal, reason: Optional[str] = None) -> Dict[str, Any]:
        payload = {
            'payment_id': payment.payment_id,
            'provider_transaction_id': payment.provider_transaction_id,
            'amount': float(Decimal(str(amount or 0)).quantize(Decimal('0.01'))),
            'reason': reason,
        }

        if self.test_mode:
            return {
                'success': True,
                'status': 'refunded',
                'provider_transaction_id': payment.provider_transaction_id,
                'receipt_payload': payload,
            }

        response = self.merchant_request(
            payload,
            configured_url=current_app.config.get('CLICK_MERCHANT_REFUND_URL'),
            fallback_path=current_app.config.get('CLICK_MERCHANT_API_REFUND_PATH', '/payment/reverse'),
        )
        return {
            'success': True,
            'status': response.get('status') or response.get('state') or 'refunded',
            'provider_transaction_id': response.get('provider_transaction_id') or payment.provider_transaction_id,
            'receipt_payload': response,
        }

    def fiscalize_payment(self, payment: Payment, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.test_mode:
            receipt_id = f"click-fiscal-{payment.id}"
            return {
                'success': True,
                'status': 'completed',
                'receipt_id': receipt_id,
                'receipt_url': f"{current_app.config.get('COMPANY_WEBSITE', '').rstrip('/')}/admin/payments/{payment.id}",
                'receipt_payload': payload,
            }

        response = self.merchant_request(
            payload,
            configured_url=current_app.config.get('CLICK_MERCHANT_FISCALIZATION_URL'),
            fallback_path=current_app.config.get('CLICK_MERCHANT_API_FISCALIZATION_PATH', '/fiscalization'),
        )
        return {
            'success': True,
            'status': response.get('status') or response.get('state') or 'completed',
            'receipt_id': response.get('receipt_id') or response.get('id'),
            'receipt_url': response.get('receipt_url') or response.get('url'),
            'receipt_payload': response,
            'receipt': response.get('receipt') if isinstance(response, dict) else None,
            'data': response.get('data') if isinstance(response, dict) else None,
        }

    def submit_fiscalization(self, payment: Payment, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility wrapper used by the fiscalization service."""
        return self.fiscalize_payment(payment, payload)
