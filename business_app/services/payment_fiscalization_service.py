"""Click fiscalization and marking-code lifecycle workflows."""

import time
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
from business_app.utils.exceptions import NotFoundError, TaxCommitteeUnavailableError, ValidationError


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

    @property
    def tax_committee_service(self):
        if not hasattr(self, '_tax_committee_service') or self._tax_committee_service is None:
            from business_app.services.tax_committee_service import TaxCommitteeService

            self._tax_committee_service = TaxCommitteeService()
        return self._tax_committee_service

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

    def pre_utilise_marking_codes_for_payment(
        self,
        payment: Payment,
        max_retries: int = 3,
    ) -> Optional[datetime]:
        """Pre-utilise marking codes at order-confirmation time for card/click payments.

        Called synchronously during order creation before the payment link is surfaced
        to the user.  Guarantees the Tax Committee utilisation happens at least
        PRE_PAYMENT_UTILISATION_WAIT_SECONDS before the customer actually pays.

        Steps:
          1. Create / fetch the PaymentFiscalization record.
          2. Reserve marking codes (idempotent — skips already-allocated codes).
          3. Send utilisation request to Tax Committee with up to *max_retries* attempts.
          4. Record tax_committee_utilised_at on the fiscal record.

        Returns:
            The tax_committee_utilised_at datetime (UTC) if utilisation happened or was
            already recorded; None if the order has no marking-code products.

        Raises:
            TaxCommitteeUnavailableError: after *max_retries* exhausted.
            ValidationError: if there are insufficient available marking codes.
        """
        self._log_fiscal_step('pre_utilise_marking_codes_started', payment=payment)

        fiscalization = self.ensure_fiscalization_record(payment)
        self.reserve_required_marking_codes(payment)

        # Retry Tax Committee utilisation up to max_retries times
        last_error: Optional[Exception] = None
        utilisation_result: Optional[Dict[str, Any]] = None
        for attempt in range(max_retries):
            try:
                utilisation_result = self.utilise_marking_codes_with_tax_committee(payment)
                break
            except Exception as exc:
                last_error = exc
                self._log_fiscal_step(
                    'pre_utilise_marking_codes_tc_retry',
                    level='warning',
                    payment=payment,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    error=str(exc),
                )
                if attempt < max_retries - 1:
                    time.sleep(1)
        else:
            self._log_fiscal_step(
                'pre_utilise_marking_codes_tc_failed',
                level='error',
                payment=payment,
                max_retries=max_retries,
                error=str(last_error),
            )
            raise TaxCommitteeUnavailableError(str(last_error))

        # Record utilised_at the first time codes are newly utilised
        if utilisation_result and utilisation_result.get('utilised', 0) > 0 and fiscalization.tax_committee_utilised_at is None:
            fiscalization.tax_committee_utilised_at = datetime.now(timezone.utc)

        db.session.commit()
        self._log_fiscal_step(
            'pre_utilise_marking_codes_completed',
            payment=payment,
            fiscalization=fiscalization,
            utilised_at=fiscalization.tax_committee_utilised_at,
        )
        return fiscalization.tax_committee_utilised_at

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

        try:
            # Preprocessing: change marking codes from "Received" to "Applied" in Tax Committee
            utilisation_result = self.utilise_marking_codes_with_tax_committee(payment, actor_user_id=actor_user_id)
            self._log_fiscal_step(
                'process_click_fiscalization_marking_codes_utilised',
                payment=payment,
                fiscalization=fiscalization,
            )

            newly_utilised = utilisation_result.get('utilised', 0) > 0

            # Record timestamp only when we just utilised codes in this Celery run.
            # If tax_committee_utilised_at is already set it means pre_utilise_marking_codes_for_payment
            # ran at order-confirmation time — do NOT overwrite that earlier timestamp.
            if newly_utilised and fiscalization.tax_committee_utilised_at is None:
                fiscalization.tax_committee_utilised_at = datetime.now(timezone.utc)

            # Enforce delay only when codes were utilised in THIS Celery run (not pre-utilised).
            # Pre-utilised payments already waited PRE_PAYMENT_UTILISATION_WAIT_SECONDS before
            # the user even saw the payment link, so the Tax Committee requirement is satisfied.
            if newly_utilised and fiscalization.tax_committee_utilised_at is not None:
                delay_seconds = int(
                    current_app.config.get('TAX_COMMITTEE_UTILISATION_DELAY_SECONDS', 120) or 120
                )
                utilised_at = fiscalization.tax_committee_utilised_at
                # SQLite returns naive datetimes; treat them as UTC for comparison
                if utilised_at.tzinfo is None:
                    utilised_at = utilised_at.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - utilised_at).total_seconds()
                if elapsed < delay_seconds:
                    remaining = delay_seconds - elapsed
                    self._log_fiscal_step(
                        'process_click_fiscalization_tc_delay_waiting',
                        payment=payment,
                        fiscalization=fiscalization,
                        elapsed_seconds=int(elapsed),
                        remaining_seconds=int(remaining),
                    )
                    fiscalization.status = FiscalizationStatus.PENDING
                    fiscalization.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=remaining)
                    self._schedule_tc_delay_retry(payment.id, int(remaining))
                    return fiscalization

            payload = self.build_click_fiscalization_payload(payment)
            fiscalization.request_payload = payload
            self._log_fiscal_step(
                'process_click_fiscalization_payload_built',
                payment=payment,
                fiscalization=fiscalization,
                items_count=len(payload.get('items') or []),
                received_card=payload.get('received_card'),
            )

            self._log_fiscal_step('process_click_fiscalization_submit_started', payment=payment, fiscalization=fiscalization)
            response = self.click_provider_service.fiscalize_payment(payment, payload)
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

    def _schedule_tc_delay_retry(self, payment_id: int, countdown_seconds: int) -> None:
        """Schedule fiscalization retry after Tax Committee utilisation delay."""
        self._log_fiscal_step(
            'schedule_tc_delay_retry_started',
            payment=None,
            target_payment_id=payment_id,
            countdown_seconds=countdown_seconds,
        )
        if current_app.config.get('TESTING'):
            return
        try:
            from business_app.tasks.payment_tasks import process_click_fiscalization_task

            process_click_fiscalization_task.apply_async(
                args=[payment_id],
                kwargs={'force': False},
                countdown=countdown_seconds,
            )
            self._log_fiscal_step(
                'schedule_tc_delay_retry_enqueued',
                payment=None,
                target_payment_id=payment_id,
                countdown_seconds=countdown_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_fiscal_step(
                'schedule_tc_delay_retry_failed',
                level='error',
                payment=None,
                target_payment_id=payment_id,
                error_message=str(exc),
            )
            current_app.logger.error(
                "Failed to schedule TC delay retry for payment %s: %s", payment_id, exc
            )

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

    MAX_PRECHECK_ROUNDS = 3

    def _precheck_and_replace_invalid_codes(
        self,
        payment: Payment,
        codes_by_product: Dict[int, List[Tuple[OrderItem, ProductMarkingCode]]],
        *,
        actor_user_id: Optional[int] = None,
    ) -> Dict[int, List[Tuple[OrderItem, ProductMarkingCode]]]:
        """Check Tax Committee statuses and replace WITHDRAWN/WRITTEN_OFF codes.

        For each product batch:
        - RECEIVED codes are kept (need utilisation).
        - APPLIED/INTRODUCED codes are kept (already utilised, skip utilisation later).
        - WITHDRAWN/WRITTEN_OFF codes are archived and replaced with fresh AVAILABLE codes.

        Replacement codes are re-checked in subsequent rounds (up to MAX_PRECHECK_ROUNDS)
        to avoid picking another invalid code.

        Returns the updated codes_by_product dict with all codes valid.
        """
        from business_app.services.tax_committee_service import TaxCommitteeService

        order = payment.order

        for round_num in range(1, self.MAX_PRECHECK_ROUNDS + 1):
            # Collect all identification codes across products for a single API call
            all_id_codes: List[str] = []
            code_to_full: Dict[str, Tuple[int, int]] = {}  # id_code -> (product_id, index in list)
            for product_id, items in codes_by_product.items():
                for idx, (order_item, code) in enumerate(items):
                    if not order_item.product or not order_item.product.requires_marking_codes:
                        continue
                    id_code = self._extract_identification_code(code.code)
                    all_id_codes.append(id_code)
                    code_to_full[id_code] = (product_id, idx)

            if not all_id_codes:
                break

            status_map = self.tax_committee_service.check_marking_code_statuses(all_id_codes)

            self._log_fiscal_step(
                'precheck_round_completed',
                payment=payment,
                round_num=round_num,
                statuses={code: status for code, status in status_map.items()},
            )

            # Identify codes that need replacement
            codes_to_replace: List[Tuple[int, int, str, str]] = []  # (product_id, idx, id_code, tc_status)
            for id_code, (product_id, idx) in code_to_full.items():
                tc_status = status_map.get(id_code, '')
                if tc_status in TaxCommitteeService.INVALID_STATUSES:
                    codes_to_replace.append((product_id, idx, id_code, tc_status))

            if not codes_to_replace:
                # All codes are valid (RECEIVED, APPLIED, or INTRODUCED)
                break

            self._log_fiscal_step(
                'precheck_replacing_invalid_codes',
                payment=payment,
                round_num=round_num,
                invalid_count=len(codes_to_replace),
                invalid_codes=[
                    {'code': c[2], 'status': c[3]} for c in codes_to_replace
                ],
            )

            # Archive invalid codes and reserve replacements
            for product_id, idx, id_code, tc_status in codes_to_replace:
                order_item, bad_code = codes_by_product[product_id][idx]

                # Archive the invalid code
                bad_code.status = MarkingCodeStatus.ARCHIVED
                bad_code.archived_at = datetime.now(timezone.utc)
                db.session.add(
                    OrderItemMarkingCodeAllocation(
                        order_item_id=order_item.id,
                        order_id=order.id,
                        payment_id=payment.id,
                        product_marking_code_id=bad_code.id,
                        action=MarkingCodeLedgerEventType.ARCHIVED,
                        actor_user_id=actor_user_id,
                        event_metadata={
                            'reason': f'Tax Committee status: {tc_status}',
                            'tax_committee_status': tc_status,
                        },
                    )
                )

                # Reserve a replacement
                replacement = (
                    ProductMarkingCode.query.filter_by(
                        product_id=product_id,
                        status=MarkingCodeStatus.AVAILABLE,
                    )
                    .order_by(ProductMarkingCode.created_at.asc(), ProductMarkingCode.id.asc())
                    .with_for_update()
                    .first()
                )
                if not replacement:
                    product_name = order_item.product.name if order_item.product else product_id
                    raise ValidationError(
                        f"No replacement marking code available for product {product_name}. "
                        f"Code {id_code} is {tc_status} in Tax Committee."
                    )

                replacement.status = MarkingCodeStatus.RESERVED
                replacement.reserved_at = datetime.now(timezone.utc)
                db.session.add(
                    OrderItemMarkingCodeAllocation(
                        order_item_id=order_item.id,
                        order_id=order.id,
                        payment_id=payment.id,
                        product_marking_code_id=replacement.id,
                        action=MarkingCodeLedgerEventType.RESERVED,
                        actor_user_id=actor_user_id,
                        event_metadata={
                            'reason': f'Replacement for {tc_status} code',
                            'replaced_code_id': bad_code.id,
                        },
                    )
                )

                # Swap in the replacement
                codes_by_product[product_id][idx] = (order_item, replacement)

            db.session.flush()

            # Sync stock for affected products
            replaced_product_ids = {pid for pid, _, _, _ in codes_to_replace}
            for product_id in replaced_product_ids:
                items = codes_by_product[product_id]
                product = items[0][0].product
                if product and product.requires_marking_codes:
                    self._product_fiscal_service.sync_stock_from_marking_codes(product)

            self._log_fiscal_step(
                'precheck_replacements_completed',
                payment=payment,
                round_num=round_num,
                replaced_count=len(codes_to_replace),
            )
            # Loop back to re-check the replacement codes
        else:
            # Exhausted all rounds — still have invalid codes
            self._log_fiscal_step(
                'precheck_max_rounds_exceeded',
                level='error',
                payment=payment,
                max_rounds=self.MAX_PRECHECK_ROUNDS,
            )
            raise ValidationError(
                f"Could not find valid replacement marking codes after {self.MAX_PRECHECK_ROUNDS} attempts"
            )

        return codes_by_product

    def utilise_marking_codes_with_tax_committee(
        self,
        payment: Payment,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Preprocessing: check Tax Committee statuses, replace invalid codes, then utilise RECEIVED ones."""
        from business_app.services.tax_committee_service import TaxCommitteeService

        if not current_app.config.get('TAX_COMMITTEE_UTILISATION_ENABLED', True):
            self._log_fiscal_step('tax_committee_utilisation_skipped_disabled', payment=payment)
            return {'utilised': 0, 'skipped': True}

        self._log_fiscal_step('tax_committee_utilisation_started', payment=payment)
        order = payment.order
        if not order:
            return {'utilised': 0, 'skipped': True}

        reserved_codes = self._get_reserved_codes(payment)
        if not reserved_codes:
            self._log_fiscal_step('tax_committee_utilisation_skipped_no_codes', payment=payment)
            return {'utilised': 0, 'skipped': True}

        # Group codes by product for per-product expire_days
        codes_by_product: Dict[int, List[Tuple[OrderItem, ProductMarkingCode]]] = defaultdict(list)
        for order_item, code in reserved_codes:
            codes_by_product[order_item.product_id].append((order_item, code))

        # Pre-check statuses and replace WITHDRAWN/WRITTEN_OFF codes
        codes_by_product = self._precheck_and_replace_invalid_codes(
            payment, codes_by_product, actor_user_id=actor_user_id,
        )

        total_utilised = 0
        total_already_applied = 0
        for product_id, items in codes_by_product.items():
            product = items[0][0].product
            if not product or not product.requires_marking_codes:
                continue

            # Separate codes by Tax Committee status: only utilise RECEIVED ones
            codes_to_utilise: List[Tuple[OrderItem, ProductMarkingCode]] = []
            codes_already_applied: List[Tuple[OrderItem, ProductMarkingCode]] = []

            # Get current statuses for this batch
            id_codes = [self._extract_identification_code(code.code) for _, code in items]
            status_map = self.tax_committee_service.check_marking_code_statuses(id_codes)

            for order_item, code in items:
                id_code = self._extract_identification_code(code.code)
                tc_status = status_map.get(id_code, TaxCommitteeService.STATUS_RECEIVED)
                if tc_status in TaxCommitteeService.ALREADY_UTILISED_STATUSES:
                    codes_already_applied.append((order_item, code))
                else:
                    codes_to_utilise.append((order_item, code))

            # Utilise only RECEIVED codes
            if codes_to_utilise:
                full_codes = [code.code for _, code in codes_to_utilise]
                result = self.tax_committee_service.utilise_marking_codes(full_codes, product)
                report_id = result.get('reportId')
                self._log_fiscal_step(
                    'tax_committee_utilisation_batch_completed',
                    payment=payment,
                    product_id=product_id,
                    codes_count=len(full_codes),
                    report_id=report_id,
                )

                for order_item, code in codes_to_utilise:
                    db.session.add(
                        OrderItemMarkingCodeAllocation(
                            order_item_id=order_item.id,
                            order_id=order.id,
                            payment_id=payment.id,
                            product_marking_code_id=code.id,
                            action=MarkingCodeLedgerEventType.UTILISED,
                            actor_user_id=actor_user_id,
                            event_metadata={
                                'report_id': report_id,
                                'tax_committee_api': True,
                            },
                        )
                    )
                total_utilised += len(codes_to_utilise)

            # Log already-applied codes (no utilisation needed, but still record ledger event)
            if codes_already_applied:
                self._log_fiscal_step(
                    'tax_committee_codes_already_applied',
                    payment=payment,
                    product_id=product_id,
                    codes_count=len(codes_already_applied),
                )
                for order_item, code in codes_already_applied:
                    id_code = self._extract_identification_code(code.code)
                    tc_status = status_map.get(id_code, 'APPLIED')
                    db.session.add(
                        OrderItemMarkingCodeAllocation(
                            order_item_id=order_item.id,
                            order_id=order.id,
                            payment_id=payment.id,
                            product_marking_code_id=code.id,
                            action=MarkingCodeLedgerEventType.UTILISED,
                            actor_user_id=actor_user_id,
                            event_metadata={
                                'tax_committee_status': tc_status,
                                'already_applied': True,
                            },
                        )
                    )
                total_already_applied += len(codes_already_applied)

        self._log_fiscal_step(
            'tax_committee_utilisation_completed',
            payment=payment,
            total_utilised=total_utilised,
            total_already_applied=total_already_applied,
        )
        return {
            'utilised': total_utilised,
            'already_applied': total_already_applied,
        }

    def build_click_fiscalization_payload(self, payment: Payment) -> Dict[str, Any]:
        self._log_fiscal_step('build_payload_started', payment=payment)
        order = payment.order
        if not order:
            self._log_fiscal_step('build_payload_failed_missing_order', level='error', payment=payment)
            raise ValidationError("Payment has no order")

        click_payment_id = self.click_provider_service.resolve_click_payment_id(payment)
        self._log_fiscal_step('build_payload_click_payment_id_resolved', payment=payment, click_payment_id=click_payment_id)
        reserved_lookup = self._reserved_code_lookup(payment)
        commission_info = self._build_commission_info(payment)
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
                'Amount': quantity * 1000, # Click developers informed that 'Amount' field should be multiplied by 1000
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
            'received_ecash': 0,
            'received_card': received_card_tiyin,
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
                code.order_id = order.id
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
            # Sync stock for all affected products
            for order_item in order.order_items or []:
                product = order_item.product
                if product and product.requires_marking_codes:
                    self._product_fiscal_service.sync_stock_from_marking_codes(product)
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
            code.order_id = None
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
            # Sync stock for affected products (codes returned to AVAILABLE)
            synced_products = set()
            for order_item, code in reserved_codes:
                if order_item.product_id not in synced_products and order_item.product:
                    if order_item.product.requires_marking_codes:
                        self._product_fiscal_service.sync_stock_from_marking_codes(order_item.product)
                        synced_products.add(order_item.product_id)
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
                code.order_id = order.id
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
            # Sync stock for affected products
            for order_item in order.order_items or []:
                product = order_item.product
                if product and product.requires_marking_codes:
                    self._product_fiscal_service.sync_stock_from_marking_codes(product)
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
            # Sync stock for affected products
            synced_products = set()
            for order_item, code in self._get_reserved_codes(payment):
                if order_item.product_id not in synced_products and order_item.product:
                    if order_item.product.requires_marking_codes:
                        self._product_fiscal_service.sync_stock_from_marking_codes(order_item.product)
                        synced_products.add(order_item.product_id)
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

    @staticmethod
    def _extract_identification_code(full_code: str) -> str:
        """Extract the identification code portion (before the first ASCII 29 / GS character)."""
        gs_char = '\x1d'  # ASCII 29 (Group Separator)
        idx = full_code.find(gs_char)
        if idx == -1:
            return full_code
        return full_code[:idx]

    def _reserved_code_lookup(self, payment: Payment) -> Dict[int, List[str]]:
        """Return {order_item_id: [identification_code, ...]} for Click Labels.

        Only the identification code (before ASCII 29) is sent in fiscalization receipts.
        """
        lookup: Dict[int, List[str]] = defaultdict(list)
        for order_item, code in self._get_reserved_codes(payment):
            lookup[order_item.id].append(self._extract_identification_code(code.code))
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
