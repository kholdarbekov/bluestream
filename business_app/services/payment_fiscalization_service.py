"""Click fiscalization and marking-code lifecycle workflows."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.models.order import Order, OrderItem, OrderItemMarkingCodeAllocation
from business_app.models.payment import Payment, PaymentFiscalization
from business_app.models.product import Product, ProductMarkingCode
from business_app.services.product_fiscal_service import ProductFiscalService
from business_app.utils.audit_logger import audit_logger
from business_app.utils.constants import (
    FiscalizationStatus,
    MarkingCodeLedgerEventType,
    MarkingCodeStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import NotFoundError, ValidationError


class PaymentFiscalizationService:
    """Own Click fiscalization execution and marking-code accounting."""

    OFD_RETRY_DELAY_SECONDS = 300
    OFD_RETRY_MAX_ATTEMPTS = 5

    def __init__(self, click_provider_service=None):
        self._click_provider_service = click_provider_service
        self._product_fiscal_service = ProductFiscalService()

    @staticmethod
    def _payment_log_context(payment: Optional[Payment]) -> Dict[str, Any]:
        if payment is None:
            return {}
        return {
            'payment_id': payment.id,
            'order_id': payment.order_id,
            'payment_ref': payment.payment_id,
            'payment_status': payment.status.value if hasattr(payment.status, 'value') else str(payment.status),
        }

    @staticmethod
    def _fiscalization_log_context(fiscalization: Optional[PaymentFiscalization]) -> Dict[str, Any]:
        if fiscalization is None:
            return {}
        return {
            'fiscalization_id': fiscalization.id,
            'fiscalization_status': (
                fiscalization.status.value if hasattr(fiscalization.status, 'value') else str(fiscalization.status)
            ),
            'fiscalization_attempts': int(fiscalization.attempts or 0),
            'provider_status': fiscalization.provider_status,
            'payload': fiscalization.request_payload if hasattr(fiscalization, 'request_payload') else ''
        }

    def _log_fiscal_step(
        self,
        step: str,
        *,
        level: str = 'info',
        payment: Optional[Payment] = None,
        fiscalization: Optional[PaymentFiscalization] = None,
        **context: Any,
    ) -> None:
        payload: Dict[str, Any] = {
            'flow': 'click_fiscalization',
            'step': step,
            **self._payment_log_context(payment),
            **self._fiscalization_log_context(fiscalization),
            **context,
        }
        log_fn = getattr(current_app.logger, level, current_app.logger.info)
        log_fn("Click fiscalization flow step: %s", step, extra=payload)

    @property
    def click_provider_service(self):
        if self._click_provider_service is None:
            from business_app.services.click_provider_service import ClickProviderService

            self._click_provider_service = ClickProviderService()
        return self._click_provider_service

    def _append_payment_audit_trail(
        self,
        payment: Payment,
        *,
        action: str,
        actor_user_id: Optional[int] = None,
        fiscalization: Optional[PaymentFiscalization] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        provider_data = dict(payment.provider_data or {})
        trail = list(provider_data.get('fiscalization_audit_trail') or [])
        trail.append({
            'action': action,
            'occurred_at': datetime.now(timezone.utc).isoformat(),
            'actor_user_id': actor_user_id,
            'payment_id': payment.id,
            'order_id': payment.order_id,
            'fiscalization_id': fiscalization.id if fiscalization else None,
            'fiscalization_status': (
                fiscalization.status.value if fiscalization and hasattr(fiscalization.status, 'value') else None
            ),
            'success': bool(success),
            'error_message': error_message,
            'additional_data': additional_data or {},
        })
        provider_data['fiscalization_audit_trail'] = trail[-50:]
        payment.provider_data = provider_data

    def _log_payment_activity(
        self,
        payment: Payment,
        *,
        action: str,
        description: str,
        event_type: AuditEventType,
        severity: AuditSeverity = AuditSeverity.MEDIUM,
        actor_user_id: Optional[int] = None,
        fiscalization: Optional[PaymentFiscalization] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        additional_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = dict(additional_data or {})
        payload.update({
            'payment_id': payment.id,
            'order_id': payment.order_id,
            'payment_method': (
                payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
            ),
            'payment_provider': payment.payment_provider,
            'actor_user_id': actor_user_id,
        })
        if fiscalization is not None:
            payload.update({
                'fiscalization_id': fiscalization.id,
                'fiscalization_status': (
                    fiscalization.status.value if hasattr(fiscalization.status, 'value') else fiscalization.status
                ),
                'fiscalization_attempts': int(fiscalization.attempts or 0),
                'provider_receipt_id': fiscalization.provider_receipt_id,
            })

        self._append_payment_audit_trail(
            payment,
            action=action,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
            success=success,
            error_message=error_message,
            additional_data=payload,
        )
        audit_logger.log_event(
            event_type=event_type,
            action=action,
            severity=severity,
            resource_type='payment',
            resource_id=str(payment.id),
            description=description,
            success=success,
            error_message=error_message,
            additional_data=payload,
        )

    def ensure_fiscalization_record(self, payment: Payment) -> PaymentFiscalization:
        fiscalization = payment.fiscalization
        if fiscalization:
            self._log_fiscal_step('ensure_fiscalization_record_existing', payment=payment, fiscalization=fiscalization)
            return fiscalization

        fiscalization = PaymentFiscalization(
            payment_id=payment.id,
            provider_name=PaymentMethod.CLICK.value,
            status=FiscalizationStatus.PENDING,
        )
        db.session.add(fiscalization)
        db.session.flush()
        self._log_fiscal_step('ensure_fiscalization_record_created', payment=payment, fiscalization=fiscalization)
        return fiscalization

    def payment_requires_click_fiscalization(self, payment: Payment) -> bool:
        method_value = payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
        if method_value not in {PaymentMethod.CLICK.value, PaymentMethod.CARD.value}:
            return False
        if payment.status != PaymentStatus.COMPLETED:
            return False
        order = payment.order
        if not order:
            return False
        return any(
            item.product and item.product.fiscalization_enabled
            for item in (order.order_items or [])
        )

    def queue_click_fiscalization(
        self,
        payment_id: int,
        *,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        self._log_fiscal_step('queue_click_fiscalization_started', payment=None, target_payment_id=payment_id)
        payment = self._get_payment(payment_id)
        fiscalization = self.ensure_fiscalization_record(payment)

        if not self.payment_requires_click_fiscalization(payment):
            self._log_fiscal_step(
                'queue_click_fiscalization_not_required',
                payment=payment,
                fiscalization=fiscalization,
            )
            fiscalization.status = FiscalizationStatus.NOT_REQUIRED
            fiscalization.queued_at = datetime.now(timezone.utc)
            self._log_payment_activity(
                payment,
                action='payment_fiscalization_not_required',
                description=f'Fiscalization is not required for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.LOW,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
            )
            return fiscalization

        fiscalization.status = FiscalizationStatus.PENDING
        fiscalization.queued_at = datetime.now(timezone.utc)
        self._log_fiscal_step('queue_click_fiscalization_marked_pending', payment=payment, fiscalization=fiscalization)
        self._log_payment_activity(
            payment,
            action='payment_fiscalization_queued',
            description=f'Queued Click fiscalization for payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.LOW,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
        )
        return fiscalization

    def process_click_fiscalization(
        self,
        payment_id: int,
        *,
        force: bool = False,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        self._log_fiscal_step('process_click_fiscalization_started', payment=None, target_payment_id=payment_id, force=force)
        payment = self._get_payment(payment_id)
        if payment.status != PaymentStatus.COMPLETED:
            self._log_fiscal_step(
                'process_click_fiscalization_invalid_payment_status',
                level='warning',
                payment=payment,
                expected_status=PaymentStatus.COMPLETED.value,
            )
            raise ValidationError("Only completed payments can be fiscalized")

        fiscalization = self.ensure_fiscalization_record(payment)
        if fiscalization.status == FiscalizationStatus.COMPLETED and not force:
            self._log_fiscal_step(
                'process_click_fiscalization_already_completed',
                payment=payment,
                fiscalization=fiscalization,
                force=force,
            )
            if fiscalization.provider_status == 'submitted_no_qr' and not fiscalization.provider_receipt_url:
                self._log_fiscal_step(
                    'process_click_fiscalization_refreshing_ofd_for_partial',
                    payment=payment,
                    fiscalization=fiscalization,
                )
                return self._refresh_ofd_receipt_data(
                    payment,
                    fiscalization,
                    actor_user_id=actor_user_id,
                )
            return fiscalization

        if not self.payment_requires_click_fiscalization(payment):
            self._log_fiscal_step(
                'process_click_fiscalization_not_required',
                payment=payment,
                fiscalization=fiscalization,
            )
            fiscalization.status = FiscalizationStatus.NOT_REQUIRED
            fiscalization.completed_at = datetime.now(timezone.utc)
            return fiscalization

        fiscalization.status = FiscalizationStatus.PROCESSING
        fiscalization.last_attempt_at = datetime.now(timezone.utc)
        fiscalization.attempts = int(fiscalization.attempts or 0) + 1
        db.session.flush()
        self._log_fiscal_step(
            'process_click_fiscalization_marked_processing',
            payment=payment,
            fiscalization=fiscalization,
        )

        self._log_payment_activity(
            payment,
            action='payment_fiscalization_processing_started',
            description=f'Started Click fiscalization attempt {fiscalization.attempts} for payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.MEDIUM,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
        )

        self.reserve_required_marking_codes(payment, actor_user_id=actor_user_id)
        self._log_fiscal_step(
            'process_click_fiscalization_marking_codes_reserved',
            payment=payment,
            fiscalization=fiscalization,
        )
        payload = self.build_click_fiscalization_payload(payment)
        fiscalization.request_payload = payload
        self._log_fiscal_step(
            'process_click_fiscalization_payload_built',
            payment=payment,
            fiscalization=fiscalization,
            items_count=len(payload.get('items') or []),
            received_card=payload.get('received_card'),
        )

        try:
            self._log_fiscal_step('process_click_fiscalization_submit_started', payment=payment, fiscalization=fiscalization)
            response = self.click_provider_service.submit_fiscalization(payment, payload)
            self._log_fiscal_step(
                'process_click_fiscalization_submit_completed',
                payment=payment,
                fiscalization=fiscalization,
                provider_status=response.get('status'),
                click_paydoc_id=response.get('click_paydoc_id'),
            )
        except Exception as exc:
            self._log_fiscal_step(
                'process_click_fiscalization_submit_failed',
                level='error',
                payment=payment,
                fiscalization=fiscalization,
                error_message=str(exc),
            )
            fiscalization.status = FiscalizationStatus.FAILED
            fiscalization.failure_reason = str(exc)
            fiscalization.response_payload = {'error': str(exc)}
            fiscalization.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            self.release_reserved_marking_codes(
                payment,
                reason='click_fiscalization_failed',
                actor_user_id=actor_user_id,
            )
            self._log_payment_activity(
                payment,
                action='payment_fiscalization_failed',
                description=f'Click fiscalization failed for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_FAILED,
                severity=AuditSeverity.HIGH,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
                success=False,
                error_message=str(exc),
                additional_data={
                    'click_paydoc_id': self._safe_click_payment_id(payment),
                },
            )
            raise

        response_payload = response or {}
        provider_status = str(
            response_payload.get('status')
            or response_payload.get('state')
            or 'submitted'
        ).lower()
        receipt_payload = (
            response_payload.get('receipt_payload')
            or response_payload.get('receipt')
            or response_payload.get('data')
            or response_payload
        )
        fiscalization.response_payload = response_payload
        fiscalization.receipt_payload = receipt_payload
        fiscalization.provider_receipt_id = str(
            response_payload.get('receipt_id')
            or response_payload.get('receiptId')
            or response_payload.get('click_paydoc_id')
            or (response_payload.get('receipt') or {}).get('id')
            or (response_payload.get('data') or {}).get('id')
            or payload.get('payment_id')
            or ''
        )
        fiscalization.provider_receipt_url = (
            response_payload.get('receipt_url')
            or response_payload.get('receiptUrl')
            or (response_payload.get('receipt') or {}).get('url')
        )
        fiscalization.provider_status = provider_status
        fiscalization.status = FiscalizationStatus.COMPLETED
        fiscalization.failure_reason = None
        fiscalization.completed_at = datetime.now(timezone.utc)
        self._log_fiscal_step(
            'process_click_fiscalization_marked_completed',
            payment=payment,
            fiscalization=fiscalization,
            provider_status=provider_status,
            provider_receipt_url_present=bool(fiscalization.provider_receipt_url),
        )
        if provider_status == 'submitted_no_qr':
            fiscalization.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=self.OFD_RETRY_DELAY_SECONDS)
            self._schedule_ofd_retry(payment.id, int(fiscalization.attempts or 0))
        else:
            fiscalization.next_retry_at = None

        used_codes = self.mark_reserved_codes_used(payment, fiscalization, actor_user_id=actor_user_id)
        self._log_fiscal_step(
            'process_click_fiscalization_marking_codes_marked_used',
            payment=payment,
            fiscalization=fiscalization,
            used_codes=used_codes,
        )
        self._log_payment_activity(
            payment,
            action='payment_fiscalization_completed',
            description=f'Completed Click fiscalization for payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.MEDIUM,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
            additional_data={
                'used_marking_codes': used_codes,
                'click_paydoc_id': response_payload.get('click_paydoc_id') or payload.get('payment_id'),
                'provider_status': provider_status,
                'merchant_api_result': {
                    'error_code': response_payload.get('error_code'),
                    'error_note': response_payload.get('error_note'),
                },
            },
        )
        return fiscalization

    def _refresh_ofd_receipt_data(
        self,
        payment: Payment,
        fiscalization: PaymentFiscalization,
        *,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        self._log_fiscal_step(
            'refresh_ofd_receipt_data_started',
            payment=payment,
            fiscalization=fiscalization,
        )
        fiscalization.last_attempt_at = datetime.now(timezone.utc)
        fiscalization.attempts = int(fiscalization.attempts or 0) + 1
        try:
            ofd_payload = self.click_provider_service.fetch_ofd_data(payment)
            receipt_url = ofd_payload.get('receipt_url')
            provider_response = dict(fiscalization.response_payload or {})
            provider_response['ofd_data'] = ofd_payload.get('response') or {}
            fiscalization.response_payload = provider_response
            fiscalization.receipt_payload = provider_response.get('receipt_payload') or provider_response
            if receipt_url:
                fiscalization.provider_receipt_url = receipt_url
                fiscalization.provider_status = 'submitted'
                fiscalization.next_retry_at = None
                self._log_fiscal_step(
                    'refresh_ofd_receipt_data_completed_with_qr',
                    payment=payment,
                    fiscalization=fiscalization,
                    provider_receipt_url=receipt_url,
                )
                self._log_payment_activity(
                    payment,
                    action='payment_fiscalization_receipt_data_refreshed',
                    description=f'Refreshed OFD receipt data for payment {payment.id}',
                    event_type=AuditEventType.PAYMENT_PROCESSED,
                    severity=AuditSeverity.LOW,
                    actor_user_id=actor_user_id,
                    fiscalization=fiscalization,
                    additional_data={
                        'click_paydoc_id': self._safe_click_payment_id(payment),
                        'provider_receipt_url': receipt_url,
                    },
                )
                return fiscalization

            fiscalization.provider_status = 'submitted_no_qr'
            fiscalization.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=self.OFD_RETRY_DELAY_SECONDS)
            self._log_fiscal_step(
                'refresh_ofd_receipt_data_missing_qr',
                level='warning',
                payment=payment,
                fiscalization=fiscalization,
            )
            self._log_payment_activity(
                payment,
                action='payment_fiscalization_receipt_data_missing_qr',
                description=f'OFD data response had no QR URL for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_FAILED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
                success=False,
                error_message='missing_qrcode_url',
            )
        except Exception as exc:  # noqa: BLE001
            fiscalization.provider_status = 'submitted_no_qr'
            fiscalization.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=self.OFD_RETRY_DELAY_SECONDS)
            self._log_fiscal_step(
                'refresh_ofd_receipt_data_failed',
                level='warning',
                payment=payment,
                fiscalization=fiscalization,
                error_message=str(exc),
            )
            self._log_payment_activity(
                payment,
                action='payment_fiscalization_receipt_data_refresh_failed',
                description=f'Failed refreshing OFD receipt data for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_FAILED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
                success=False,
                error_message=str(exc),
                additional_data={
                    'click_paydoc_id': self._safe_click_payment_id(payment),
                },
            )

        self._schedule_ofd_retry(payment.id, int(fiscalization.attempts or 0))
        return fiscalization

    def _schedule_ofd_retry(self, payment_id: int, attempts: int) -> None:
        self._log_fiscal_step('schedule_ofd_retry_started', payment=None, target_payment_id=payment_id, attempts=attempts)
        retry_limit = int(current_app.config.get('CLICK_FISCALIZATION_OFD_RETRY_MAX_ATTEMPTS', self.OFD_RETRY_MAX_ATTEMPTS) or self.OFD_RETRY_MAX_ATTEMPTS)
        if attempts >= retry_limit:
            self._log_fiscal_step(
                'schedule_ofd_retry_skipped_limit_reached',
                level='warning',
                payment=None,
                target_payment_id=payment_id,
                attempts=attempts,
                retry_limit=retry_limit,
            )
            current_app.logger.warning(
                "Skipping OFD retry for payment %s because attempts=%s reached limit=%s",
                payment_id,
                attempts,
                retry_limit,
            )
            return
        if current_app.config.get('TESTING'):
            self._log_fiscal_step(
                'schedule_ofd_retry_skipped_testing',
                payment=None,
                target_payment_id=payment_id,
            )
            return
        try:
            from business_app.tasks.payment_tasks import process_click_fiscalization_task

            process_click_fiscalization_task.apply_async(
                args=[payment_id],
                kwargs={'force': False},
                countdown=int(current_app.config.get('CLICK_FISCALIZATION_OFD_RETRY_DELAY_SECONDS', self.OFD_RETRY_DELAY_SECONDS) or self.OFD_RETRY_DELAY_SECONDS),
            )
            self._log_fiscal_step(
                'schedule_ofd_retry_enqueued',
                payment=None,
                target_payment_id=payment_id,
                attempts=attempts,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_fiscal_step(
                'schedule_ofd_retry_failed',
                level='error',
                payment=None,
                target_payment_id=payment_id,
                error_message=str(exc),
            )
            current_app.logger.error("Failed to schedule OFD data retry for payment %s: %s", payment_id, exc)

    def _safe_click_payment_id(self, payment: Payment) -> Optional[int]:
        try:
            return self.click_provider_service.resolve_click_payment_id(payment)
        except Exception:  # noqa: BLE001
            return None

    def consume_marking_codes_for_business_account(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> PaymentFiscalization:
        method_value = payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
        if method_value != PaymentMethod.BUSINESS_ACCOUNT.value or not payment.consume_marking_codes:
            raise ValidationError("Payment is not eligible for manual marking-code consumption")
        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError("Only completed business account payments can consume marking codes")

        fiscalization = self.ensure_fiscalization_record(payment)
        fiscalization.provider_name = PaymentMethod.BUSINESS_ACCOUNT.value
        fiscalization.status = FiscalizationStatus.PROCESSING
        fiscalization.last_attempt_at = datetime.now(timezone.utc)
        fiscalization.attempts = int(fiscalization.attempts or 0) + 1

        self._reserve_codes_for_manual_consumption(payment, actor_user_id=actor_user_id)
        fiscalization.request_payload = {'mode': 'manual_business_account_consumption'}
        fiscalization.response_payload = {'status': 'completed'}
        fiscalization.receipt_payload = {'mode': 'manual_business_account_consumption'}
        fiscalization.provider_status = 'completed'
        fiscalization.status = FiscalizationStatus.COMPLETED
        fiscalization.completed_at = datetime.now(timezone.utc)

        used_codes = self.mark_reserved_codes_used(payment, fiscalization, actor_user_id=actor_user_id)
        self._log_payment_activity(
            payment,
            action='business_account_marking_codes_consumed',
            description=f'Consumed marking codes for business account payment {payment.id}',
            event_type=AuditEventType.PAYMENT_PROCESSED,
            severity=AuditSeverity.MEDIUM,
            actor_user_id=actor_user_id,
            fiscalization=fiscalization,
            additional_data={
                'used_marking_codes': used_codes,
            },
        )
        return fiscalization

    def build_click_fiscalization_payload(self, payment: Payment) -> Dict[str, Any]:
        self._log_fiscal_step('build_payload_started', payment=payment)
        order = payment.order
        if not order:
            self._log_fiscal_step('build_payload_failed_missing_order', level='error', payment=payment)
            raise ValidationError("Payment has no order")

        click_payment_id = self.click_provider_service.resolve_click_payment_id(payment)
        self._log_fiscal_step('build_payload_click_payment_id_resolved', payment=payment, click_payment_id=click_payment_id)
        reserved_lookup = self._reserved_code_lookup(payment)
        items_payload: List[Dict[str, Any]] = []
        for order_item in order.order_items or []:
            product = order_item.product
            if not product:
                self._log_fiscal_step(
                    'build_payload_failed_missing_product',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                )
                raise ValidationError(f"Order item {order_item.id} has no product reference")
            if not product.fiscalization_enabled:
                self._log_fiscal_step(
                    'build_payload_failed_product_not_fiscalized',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                    product_id=product.id,
                )
                raise ValidationError(
                    f"Order item {order_item.id} product {product.id} is not fiscalization enabled"
                )
            product_name = str(product.name or '').strip()
            if not product_name:
                self._log_fiscal_step(
                    'build_payload_failed_missing_name',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                    product_id=product.id,
                )
                raise ValidationError(
                    f"Order item {order_item.id} product {product.id} is missing Name for Click fiscalization"
                )
            spic = str(product.spic or '').strip()
            if not spic:
                self._log_fiscal_step(
                    'build_payload_failed_missing_spic',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                    product_id=product.id,
                )
                raise ValidationError(
                    f"Order item {order_item.id} product {product.id} is missing SPIC for Click fiscalization"
                )
            package_code = str(product.package_code or '').strip()

            quantity = int(order_item.quantity or 0)
            if quantity <= 0:
                self._log_fiscal_step(
                    'build_payload_failed_invalid_quantity',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                    quantity=order_item.quantity,
                )
                raise ValidationError(f"Order item {order_item.id} has invalid quantity: {order_item.quantity}")
            total_price = Decimal(str(order_item.total_price or 0)).quantize(Decimal('0.01'))
            if total_price <= Decimal('0'):
                self._log_fiscal_step(
                    'build_payload_failed_invalid_total_price',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                    total_price=str(order_item.total_price),
                )
                raise ValidationError(f"Order item {order_item.id} has invalid total price: {order_item.total_price}")
            vat_percent = self._normalize_vat_percent(product.vat_percent)
            vat_amount = (total_price * vat_percent / Decimal('100')).quantize(Decimal('0.01'))
            item_payload = {
                'Name': product_name,
                'SPIC': spic,
                'PackageCode': package_code,
                'Price': self._to_tiyin(total_price),
                'Amount': quantity,
                'VAT': self._to_tiyin(vat_amount),
                'VATPercent': int(vat_percent),
            }
            labels = reserved_lookup.get(order_item.id, [])
            if product.requires_marking_codes:
                if len(labels) != quantity:
                    self._log_fiscal_step(
                        'build_payload_failed_marking_labels_mismatch',
                        level='error',
                        payment=payment,
                        order_item_id=order_item.id,
                        expected_labels=quantity,
                        actual_labels=len(labels),
                    )
                    raise ValidationError(
                        f"Expected {order_item.quantity} labels for product {product.name}, got {len(labels)}"
                    )
                item_payload['Labels'] = labels

            commission_info = self._build_commission_info(payment)
            if commission_info:
                item_payload['CommissionInfo'] = commission_info
            items_payload.append(item_payload)
            self._log_fiscal_step(
                'build_payload_item_added',
                payment=payment,
                order_item_id=order_item.id,
                product_id=product.id,
                amount=item_payload['Amount'],
                price=item_payload['Price'],
                has_labels=bool(item_payload.get('Labels')),
            )

        if not items_payload:
            self._log_fiscal_step('build_payload_failed_no_items', level='error', payment=payment)
            raise ValidationError("No fiscalized items found for Click fiscalization request")

        received_card_tiyin = self._to_tiyin(Decimal(str(order.total_amount or payment.amount or 0)))

        try:
            service_id = int(str(self.click_provider_service.service_id))
        except (TypeError, ValueError) as exc:
            self._log_fiscal_step(
                'build_payload_failed_invalid_service_id',
                level='error',
                payment=payment,
                service_id=self.click_provider_service.service_id,
            )
            raise ValidationError("Click service ID must be numeric") from exc

        payload: Dict[str, Any] = {
            'service_id': service_id,
            'payment_id': click_payment_id,
            'received_cash': 0,
            'received_ecash': received_card_tiyin,
            # Card payments in this flow charge the full order amount from card.
            'received_card': 0,
            'items': items_payload,
        }
        self._log_fiscal_step(
            'build_payload_completed',
            payment=payment,
            click_payment_id=click_payment_id,
            service_id=service_id,
            items_count=len(items_payload),
            received_card=received_card_tiyin,
        )
        return payload

    def marking_code_allocation_summary(self, order: Order) -> Dict[str, Any]:
        summary = defaultdict(int)
        codes_by_item: Dict[int, List[str]] = defaultdict(list)
        for allocation in order.marking_code_allocations or []:
            action_value = allocation.action.value if hasattr(allocation.action, 'value') else str(allocation.action)
            summary[action_value] += 1
            if allocation.marking_code:
                codes_by_item[allocation.order_item_id].append(allocation.marking_code.code)
        return {
            'events': dict(summary),
            'codes_by_order_item': dict(codes_by_item),
        }

    def reserve_required_marking_codes(self, payment: Payment, *, actor_user_id: Optional[int] = None) -> Dict[str, Any]:
        self._log_fiscal_step('reserve_required_marking_codes_started', payment=payment)
        order = payment.order
        if not order:
            self._log_fiscal_step('reserve_required_marking_codes_skipped_no_order', payment=payment)
            return {'reserved': 0, 'skipped': True}

        method_value = payment.payment_method.value if hasattr(payment.payment_method, 'value') else payment.payment_method
        if method_value not in {PaymentMethod.CLICK.value, PaymentMethod.CARD.value}:
            self._log_fiscal_step(
                'reserve_required_marking_codes_skipped_payment_method',
                payment=payment,
                payment_method=method_value,
            )
            return {'reserved': 0, 'skipped': True}

        reserved_count = 0
        for order_item in order.order_items or []:
            product = order_item.product
            if not product or not product.fiscalization_enabled or not product.requires_marking_codes:
                continue

            already_allocated = self._get_active_allocated_codes(payment, order_item)
            if len(already_allocated) >= int(order_item.quantity or 0):
                continue

            missing_count = int(order_item.quantity or 0) - len(already_allocated)
            available_codes = (
                ProductMarkingCode.query.filter_by(
                    product_id=order_item.product_id,
                    status=MarkingCodeStatus.AVAILABLE,
                )
                .order_by(ProductMarkingCode.created_at.asc(), ProductMarkingCode.id.asc())
                .with_for_update()
                .limit(missing_count)
                .all()
            )
            if len(available_codes) != missing_count:
                self._log_fiscal_step(
                    'reserve_required_marking_codes_failed_insufficient',
                    level='error',
                    payment=payment,
                    order_item_id=order_item.id,
                    required=missing_count,
                    available=len(available_codes),
                    product_id=order_item.product_id,
                )
                raise ValidationError(
                    f"Not enough marking codes for product {product.name}. Required: {missing_count}, available: {len(available_codes)}"
                )

            for code in available_codes:
                code.status = MarkingCodeStatus.RESERVED
                code.reserved_at = datetime.now(timezone.utc)
                db.session.add(
                    OrderItemMarkingCodeAllocation(
                        order_item_id=order_item.id,
                        order_id=order.id,
                        payment_id=payment.id,
                        product_marking_code_id=code.id,
                        action=MarkingCodeLedgerEventType.RESERVED,
                        actor_user_id=actor_user_id,
                        event_metadata={
                            'payment_method': method_value,
                            'order_number': order.order_number,
                        },
                    )
                )
                reserved_count += 1

            self._log_fiscal_step(
                'reserve_required_marking_codes_item_reserved',
                payment=payment,
                order_item_id=order_item.id,
                reserved_for_item=missing_count,
                product_id=order_item.product_id,
            )

        if reserved_count:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_reserved',
                description=f'Reserved {reserved_count} marking codes for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                additional_data={'reserved_marking_codes': reserved_count},
            )

        self._log_fiscal_step(
            'reserve_required_marking_codes_completed',
            payment=payment,
            reserved=reserved_count,
        )
        return {'reserved': reserved_count}

    def release_reserved_marking_codes(
        self,
        payment: Payment,
        *,
        reason: str,
        actor_user_id: Optional[int] = None,
    ) -> int:
        self._log_fiscal_step('release_reserved_marking_codes_started', payment=payment, reason=reason)
        reserved_codes = self._get_reserved_codes(payment)
        released = 0
        for order_item, code in reserved_codes:
            if code.status != MarkingCodeStatus.RESERVED:
                continue
            code.status = MarkingCodeStatus.AVAILABLE
            code.reserved_at = None
            db.session.add(
                OrderItemMarkingCodeAllocation(
                    order_item_id=order_item.id,
                    order_id=order_item.order_id,
                    payment_id=payment.id,
                    product_marking_code_id=code.id,
                    action=MarkingCodeLedgerEventType.RELEASED,
                    actor_user_id=actor_user_id,
                    notes=reason,
                    event_metadata={'reason': reason},
                )
            )
            released += 1
        if released:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_released',
                description=f'Released {released} marking codes for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_FAILED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                additional_data={
                    'released_marking_codes': released,
                    'reason': reason,
                },
            )
        self._log_fiscal_step(
            'release_reserved_marking_codes_completed',
            payment=payment,
            released=released,
            reason=reason,
        )
        return released

    def _reserve_codes_for_manual_consumption(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> None:
        order = payment.order
        if not order:
            return

        reserved_count = 0
        for order_item in order.order_items or []:
            product = order_item.product
            if not product or not product.requires_marking_codes:
                continue

            existing_codes = self._get_active_allocated_codes(payment, order_item)
            missing_count = int(order_item.quantity or 0) - len(existing_codes)
            if missing_count <= 0:
                continue

            available_codes = (
                ProductMarkingCode.query.filter_by(
                    product_id=order_item.product_id,
                    status=MarkingCodeStatus.AVAILABLE,
                )
                .order_by(ProductMarkingCode.created_at.asc(), ProductMarkingCode.id.asc())
                .with_for_update()
                .limit(missing_count)
                .all()
            )
            if len(available_codes) != missing_count:
                raise ValidationError(
                    f"Not enough marking codes for product {product.name}. Required: {missing_count}, available: {len(available_codes)}"
                )

            for code in available_codes:
                code.status = MarkingCodeStatus.RESERVED
                code.reserved_at = datetime.now(timezone.utc)
                db.session.add(
                    OrderItemMarkingCodeAllocation(
                        order_item_id=order_item.id,
                        order_id=order.id,
                        payment_id=payment.id,
                        product_marking_code_id=code.id,
                        action=MarkingCodeLedgerEventType.RESERVED,
                        actor_user_id=actor_user_id,
                        event_metadata={'mode': 'manual_business_account_consumption'},
                    )
                )
                reserved_count += 1

        if reserved_count:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_reserved',
                description=f'Reserved {reserved_count} marking codes for business account payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                additional_data={
                    'reserved_marking_codes': reserved_count,
                    'mode': 'manual_business_account_consumption',
                },
            )

    def mark_reserved_codes_used(
        self,
        payment: Payment,
        fiscalization: PaymentFiscalization,
        *,
        actor_user_id: Optional[int] = None,
    ) -> int:
        self._log_fiscal_step('mark_reserved_codes_used_started', payment=payment, fiscalization=fiscalization)
        used = 0
        for order_item, code in self._get_reserved_codes(payment):
            if code.status == MarkingCodeStatus.USED:
                continue
            code.status = MarkingCodeStatus.USED
            code.used_at = datetime.now(timezone.utc)
            db.session.add(
                OrderItemMarkingCodeAllocation(
                    order_item_id=order_item.id,
                    order_id=order_item.order_id,
                    payment_id=payment.id,
                    product_marking_code_id=code.id,
                    payment_fiscalization_id=fiscalization.id,
                    action=MarkingCodeLedgerEventType.USED,
                    actor_user_id=actor_user_id,
                    event_metadata={
                        'provider_name': fiscalization.provider_name,
                        'provider_receipt_id': fiscalization.provider_receipt_id,
                    },
                )
            )
            used += 1
        if used:
            self._log_payment_activity(
                payment,
                action='payment_marking_codes_used',
                description=f'Marked {used} marking codes as used for payment {payment.id}',
                event_type=AuditEventType.PAYMENT_PROCESSED,
                severity=AuditSeverity.MEDIUM,
                actor_user_id=actor_user_id,
                fiscalization=fiscalization,
                additional_data={'used_marking_codes': used},
            )
        self._log_fiscal_step(
            'mark_reserved_codes_used_completed',
            payment=payment,
            fiscalization=fiscalization,
            used=used,
        )
        return used

    def _reserved_code_lookup(self, payment: Payment) -> Dict[int, List[str]]:
        lookup: Dict[int, List[str]] = defaultdict(list)
        for order_item, code in self._get_reserved_codes(payment):
            lookup[order_item.id].append(code.code)
        return lookup

    def _get_reserved_codes(self, payment: Payment) -> List[Tuple[OrderItem, ProductMarkingCode]]:
        allocations = (
            OrderItemMarkingCodeAllocation.query.options(
                joinedload(OrderItemMarkingCodeAllocation.marking_code),
                joinedload(OrderItemMarkingCodeAllocation.order_item),
            )
            .filter(
                OrderItemMarkingCodeAllocation.payment_id == payment.id,
                OrderItemMarkingCodeAllocation.action == MarkingCodeLedgerEventType.RESERVED,
            )
            .order_by(OrderItemMarkingCodeAllocation.created_at.asc(), OrderItemMarkingCodeAllocation.id.asc())
            .all()
        )
        reserved_items: List[Tuple[OrderItem, ProductMarkingCode]] = []
        seen = set()
        for allocation in allocations:
            code = allocation.marking_code
            order_item = allocation.order_item
            if not code or not order_item:
                continue
            key = (order_item.id, code.id)
            if key in seen:
                continue
            if code.status in {MarkingCodeStatus.RESERVED, MarkingCodeStatus.USED}:
                reserved_items.append((order_item, code))
                seen.add(key)
        return reserved_items

    def _get_active_allocated_codes(self, payment: Payment, order_item: OrderItem) -> List[ProductMarkingCode]:
        allocations = (
            OrderItemMarkingCodeAllocation.query.options(joinedload(OrderItemMarkingCodeAllocation.marking_code))
            .filter(
                OrderItemMarkingCodeAllocation.payment_id == payment.id,
                OrderItemMarkingCodeAllocation.order_item_id == order_item.id,
                OrderItemMarkingCodeAllocation.action.in_(
                    [MarkingCodeLedgerEventType.RESERVED, MarkingCodeLedgerEventType.USED]
                ),
            )
            .order_by(OrderItemMarkingCodeAllocation.created_at.asc(), OrderItemMarkingCodeAllocation.id.asc())
            .all()
        )
        result: List[ProductMarkingCode] = []
        seen = set()
        for allocation in allocations:
            code = allocation.marking_code
            if not code or code.id in seen:
                continue
            if code.status in {MarkingCodeStatus.RESERVED, MarkingCodeStatus.USED}:
                result.append(code)
                seen.add(code.id)
        return result

    def _get_payment(self, payment_id: int) -> Payment:
        self._log_fiscal_step('get_payment_started', payment=None, target_payment_id=payment_id)
        payment = Payment.query.options(
            joinedload(Payment.user),
            joinedload(Payment.fiscalization),
            joinedload(Payment.order)
            .joinedload(Order.order_items)
            .joinedload(OrderItem.product)
            .joinedload(Product.fiscal_profile),
        ).get(payment_id)
        if not payment:
            self._log_fiscal_step('get_payment_failed_not_found', level='warning', payment=None, target_payment_id=payment_id)
            raise NotFoundError("Payment not found")
        self._log_fiscal_step('get_payment_completed', payment=payment)
        return payment

    @staticmethod
    def _to_tiyin(value: Any) -> int:
        amount = Decimal(str(value or 0)).quantize(Decimal('0.01'))
        return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _normalize_vat_percent(vat_value: Any) -> Decimal:
        vat_percent = Decimal(str(vat_value or 0)).quantize(Decimal('0.01'))
        if vat_percent < Decimal('0') or vat_percent > Decimal('100'):
            raise ValidationError("VATPercent must be between 0 and 100")
        if vat_percent != vat_percent.to_integral_value():
            raise ValidationError("VATPercent must be an integer value")
        return vat_percent

    @staticmethod
    def _format_amount(value: Any) -> str:
        return f"{Decimal(str(value or 0)).quantize(Decimal('0.01'))}"

    @staticmethod
    def _build_commission_info(payment: Payment) -> Dict[str, Any]:
        tin = str(current_app.config.get('COMPANY_TIN') or '').strip()
        if not tin:
            current_app.logger.error(
                "Click fiscalization commission info is missing COMPANY_TIN",
                extra={
                    'flow': 'click_fiscalization',
                    'step': 'build_commission_info_failed_missing_tin',
                    'payment_id': payment.id,
                    'order_id': payment.order_id,
                },
            )
            raise ValidationError("CommissionInfo.TIN requires COMPANY_TIN configuration")
        if not tin.isdigit() or len(tin) != 9:
            current_app.logger.error(
                "Click fiscalization commission info has invalid COMPANY_TIN format",
                extra={
                    'flow': 'click_fiscalization',
                    'step': 'build_commission_info_failed_invalid_tin',
                    'payment_id': payment.id,
                    'order_id': payment.order_id,
                    'tin_length': len(tin),
                },
            )
            raise ValidationError("CommissionInfo.TIN must be a 9-digit value")
        current_app.logger.info(
            "Click fiscalization commission info prepared",
            extra={
                'flow': 'click_fiscalization',
                'step': 'build_commission_info_completed',
                'payment_id': payment.id,
                'order_id': payment.order_id,
            },
        )
        return {'TIN': tin}

    def diagnose_fiscalization_gap(self, payment: Payment) -> Dict[str, Any]:
        self._log_fiscal_step('diagnose_fiscalization_gap_started', payment=payment)
        diagnosis: Dict[str, Any] = {
            'payment_id': payment.id,
            'order_id': payment.order_id,
            'requires_click_fiscalization': self.payment_requires_click_fiscalization(payment),
            'payment_status': payment.status.value if hasattr(payment.status, 'value') else str(payment.status),
            'fiscalization_status': (
                payment.fiscalization.status.value
                if getattr(payment, 'fiscalization', None) and hasattr(payment.fiscalization.status, 'value')
                else None
            ),
            'issues': [],
        }

        if diagnosis['requires_click_fiscalization'] and not getattr(payment, 'fiscalization', None):
            diagnosis['issues'].append('missing_fiscalization_record')

        try:
            click_payment_id = self.click_provider_service.resolve_click_payment_id(payment)
            diagnosis['click_payment_id'] = click_payment_id
        except Exception:
            diagnosis['issues'].append('missing_click_payment_id')

        company_tin = str(current_app.config.get('COMPANY_TIN') or '').strip()
        if not company_tin:
            diagnosis['issues'].append('missing_company_tin')
        elif not company_tin.isdigit() or len(company_tin) != 9:
            diagnosis['issues'].append('invalid_company_tin')

        fiscalization = getattr(payment, 'fiscalization', None)
        if fiscalization and getattr(fiscalization, 'failure_reason', None):
            diagnosis['last_failure_reason'] = fiscalization.failure_reason

        diagnosis['is_ready'] = len(diagnosis['issues']) == 0
        self._log_fiscal_step(
            'diagnose_fiscalization_gap_completed',
            payment=payment,
            is_ready=diagnosis['is_ready'],
            issues_count=len(diagnosis['issues']),
        )
        return diagnosis
