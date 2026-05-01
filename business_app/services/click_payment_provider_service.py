"""Click-specific payment provider integration."""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests
from flask import current_app

from business_app import db
from business_app.models.order import Order
from business_app.models.payment import CreditCard, Payment, PaymentTransaction
from shared.enums import PaymentMethod, PaymentStatus
from business_app.utils.exceptions import PaymentError, ProviderUnavailableError, ValidationError
from business_app.utils.http_client import RetryConfig, request_with_retry


class ClickPaymentProviderService:
    """Encapsulates Click checkout callbacks and Merchant API calls."""

    def __init__(self, payment_service=None):
        self.payment_service = payment_service
        self.merchant_id = current_app.config.get("CLICK_SHOP_MERCHANT_ID") or current_app.config.get(
            "CLICK_MERCHANT_ID"
        )
        self.service_id = current_app.config.get("CLICK_SHOP_SERVICE_ID") or current_app.config.get("CLICK_SERVICE_ID")
        self.secret_key = current_app.config.get("CLICK_SHOP_SECRET_KEY") or current_app.config.get("CLICK_SECRET_KEY")
        self.checkout_url = current_app.config.get("CLICK_CHECKOUT_URL") or "https://my.click.uz/services/pay"
        self.shop_callback_url = current_app.config.get("CLICK_SHOP_CALLBACK_URL")
        self.merchant_api_url = (
            current_app.config.get("CLICK_MERCHANT_API_URL")
            or current_app.config.get("CLICK_ENDPOINT_URL")
            or "https://api.click.uz/v2/merchant"
        ).rstrip("/")
        self.merchant_api_user_id = (
            current_app.config.get("CLICK_MERCHANT_API_USER_ID")
            or current_app.config.get("CLICK_MERCHANT_API_USERNAME")
            or current_app.config.get("CLICK_MERCHANT_API_USER")
            or current_app.config.get("CLICK_MERCHANT_ID")
        )
        self.merchant_api_secret_key = (
            current_app.config.get("CLICK_MERCHANT_API_SECRET_KEY")
            or current_app.config.get("CLICK_MERCHANT_API_SECRET")
            or current_app.config.get("CLICK_SECRET_KEY")
        )
        self.merchant_api_token = current_app.config.get("CLICK_MERCHANT_API_TOKEN")
        # PAY-003: tightened default 15s → 10s, plus retry policy. The previous
        # 15s/no-retry behaviour stranded payments in PENDING on a single
        # transient timeout. Now: 10s per attempt × up to 3 attempts (initial +
        # 2 retries) with jittered exponential backoff (0.5s, 1s, capped 8s).
        self.timeout_seconds = int(current_app.config.get("CLICK_MERCHANT_API_TIMEOUT_SECONDS", 10))
        self._retry_config = RetryConfig(
            max_retries=int(current_app.config.get("CLICK_MERCHANT_API_MAX_RETRIES", 2)),
            backoff_base_seconds=float(current_app.config.get("CLICK_MERCHANT_API_BACKOFF_BASE_SECONDS", 0.5)),
            backoff_max_seconds=float(current_app.config.get("CLICK_MERCHANT_API_BACKOFF_MAX_SECONDS", 8.0)),
        )
        self._circuit_failure_threshold = int(current_app.config.get("CLICK_MERCHANT_API_CIRCUIT_FAILURE_THRESHOLD", 5))
        self._circuit_recovery_seconds = float(
            current_app.config.get("CLICK_MERCHANT_API_CIRCUIT_RECOVERY_SECONDS", 30)
        )
        self.payment_timeout_minutes = int(current_app.config.get("PAYMENT_TIMEOUT_MINUTES", 60) or 60)
        self.test_mode = bool(current_app.config.get("CLICK_TEST_MODE", True))

    @staticmethod
    def _payment_log_context(payment: Optional[Payment]) -> Dict[str, Any]:
        if payment is None:
            return {}
        return {
            "payment_id": payment.id,
            "order_id": payment.order_id,
            "payment_ref": payment.payment_id,
            "payment_status": payment.status.value if hasattr(payment.status, "value") else str(payment.status),
        }

    def _log_flow_step(
        self,
        step: str,
        *,
        level: str = "info",
        payment: Optional[Payment] = None,
        **context: Any,
    ) -> None:
        payload: Dict[str, Any] = {
            "flow": "click_payment",
            "step": step,
            **self._payment_log_context(payment),
            **context,
        }
        log_fn = getattr(current_app.logger, level, current_app.logger.info)
        log_fn("Click payment flow step: %s", step, extra=payload)

    def create_payment_link(self, payment: Payment) -> Dict[str, str]:
        self._log_flow_step(
            "create_payment_link_started",
            payment=payment,
            amount=str(payment.amount),
            service_id=self.service_id,
            merchant_id=self.merchant_id,
        )
        if not self.service_id or not self.merchant_id:
            self._log_flow_step(
                "create_payment_link_configuration_missing",
                level="error",
                payment=payment,
                service_id=self.service_id,
                merchant_id=self.merchant_id,
            )
            raise PaymentError("Click payment service is not configured")

        now = datetime.now(timezone.utc)
        base_url = current_app.config.get("COMPANY_WEBSITE", "http://localhost:5000").rstrip("/")
        success_url = (
            payment.callback_url or self.shop_callback_url or f"{base_url}/payment/success?order_id={payment.order_id}"
        )
        cancel_url = f"{base_url}/payment/cancel?order_id={payment.order_id}"
        amount = self._normalize_amount(payment.amount)

        query_params = {
            "service_id": self.service_id,
            "merchant_id": self.merchant_id,
            "amount": f"{amount}",
            "transaction_param": payment.order.order_number,
            "merchant_trans_id": payment.order.order_number,
            "return_url": success_url,
            "cancel_url": cancel_url,
        }
        payment_url = f"{self.checkout_url}?{urlencode(query_params)}"

        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get("click") or {})
        click_data.update(
            {
                "checkout_url": payment_url,
                "checkout_created_at": now.isoformat(),
                "checkout_return_url": success_url,
                "checkout_cancel_url": cancel_url,
            }
        )
        provider_data["click"] = click_data

        payment.provider_data = provider_data
        payment.payment_link = payment_url
        payment.payment_link_expires_at = now + timedelta(minutes=self.payment_timeout_minutes)
        payment.callback_url = success_url
        db.session.flush()

        self._log_flow_step(
            "create_payment_link_completed",
            payment=payment,
            expires_at=payment.payment_link_expires_at.isoformat() if payment.payment_link_expires_at else None,
        )

        return {
            "payment_url": payment_url,
            "reference": payment.payment_id,
            "expires_at": payment.payment_link_expires_at.isoformat() if payment.payment_link_expires_at else None,
        }

    def verify_signature(self, payload: Dict[str, Any]) -> bool:
        self._log_flow_step(
            "verify_signature_started",
            click_trans_id=str(payload.get("click_trans_id") or ""),
            merchant_trans_id=str(payload.get("merchant_trans_id") or payload.get("transaction_param") or ""),
            action=str(payload.get("action") or ""),
        )
        if not self.secret_key:
            current_app.logger.error("Click secret key not configured")
            self._log_flow_step("verify_signature_failed_secret_missing", level="error")
            return False

        click_trans_id = str(payload.get("click_trans_id") or "")
        service_id = str(payload.get("service_id") or "")
        merchant_trans_id = str(payload.get("merchant_trans_id") or payload.get("transaction_param") or "")
        merchant_prepare_id = str(payload.get("merchant_prepare_id") or "")
        amount = str(payload.get("amount") or "")
        action = str(payload.get("action") or "")
        sign_time = str(payload.get("sign_time") or "")
        sign_string = str(payload.get("sign_string") or "")

        try:
            normalized_action = self._normalize_action(action)
        except ValidationError:
            current_app.logger.warning("Unknown Click action during signature verification: %s", action)
            self._log_flow_step("verify_signature_failed_unknown_action", level="warning", action=action)
            return False

        sign_source = f"{click_trans_id}{service_id}{self.secret_key}{merchant_trans_id}"
        if normalized_action == "complete":
            sign_source += merchant_prepare_id
        sign_source += f"{amount}{action}{sign_time}"
        expected = hashlib.md5(sign_source.encode("utf-8")).hexdigest()
        if expected.lower() != sign_string.lower():
            current_app.logger.warning("Invalid Click signature for merchant_trans_id=%s", merchant_trans_id)
            self._log_flow_step(
                "verify_signature_failed_mismatch",
                level="warning",
                merchant_trans_id=merchant_trans_id,
                click_trans_id=click_trans_id,
                action=normalized_action,
            )
            return False
        self._log_flow_step(
            "verify_signature_succeeded",
            merchant_trans_id=merchant_trans_id,
            click_trans_id=click_trans_id,
            action=normalized_action,
        )
        return True

    @staticmethod
    def _normalize_action(action: Any) -> str:
        value = str(action or "").strip().lower()
        if value in {"0", "prepare"}:
            return "prepare"
        if value in {"1", "complete"}:
            return "complete"
        raise ValidationError("Unknown Click action")

    @staticmethod
    def _normalize_amount(value: Any) -> Decimal:
        try:
            return Decimal(str(value or 0)).quantize(Decimal("0.01"))
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
            "click_trans_id": payload.get("click_trans_id"),
            "merchant_trans_id": payment.order.order_number,
            "merchant_confirm_id": payment.id,
            "error": 0,
            "error_note": "Success",
        }

    @staticmethod
    def _build_error_response(error_code: int, error_note: str) -> Dict[str, Any]:
        return {
            "error": error_code,
            "error_note": error_note,
        }

    def _record_transaction(
        self,
        payment: Payment,
        transaction_type: str,
        payload: Dict[str, Any],
        *,
        success: bool = True,
        status: str = "completed",
    ) -> PaymentTransaction:
        transaction = PaymentTransaction(
            payment_id=payment.id,
            transaction_type=transaction_type,
            amount=payment.amount,
            status=status,
            provider_transaction_id=payload.get("click_trans_id") or payload.get("receipt_id"),
            provider_reference=payload.get("merchant_trans_id") or payment.payment_id,
            provider_response=payload,
            success=success,
            processed_at=datetime.now(timezone.utc),
            failure_reason=None if success else payload.get("error_note"),
        )
        db.session.add(transaction)
        self._log_flow_step(
            "payment_transaction_recorded",
            payment=payment,
            transaction_type=transaction_type,
            transaction_status=status,
            success=success,
            provider_transaction_id=transaction.provider_transaction_id,
        )
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
        click_data = dict(provider_data.get("click") or {})
        callbacks = list(click_data.get("callbacks") or [])
        callbacks.append(
            {
                "stage": stage,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "request": request_payload,
                "response": response_payload,
            }
        )
        click_data["callbacks"] = callbacks[-20:]
        provider_data["click"] = click_data
        payment.provider_data = provider_data

    def _get_payment_fiscalization_service(self):
        if self.payment_service is not None:
            return self.payment_service._get_payment_fiscalization_service()

        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

        return PaymentFiscalizationService(click_provider_service=self)

    def handle_callback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._log_flow_step(
            "handle_callback_started",
            click_trans_id=str(payload.get("click_trans_id") or ""),
            merchant_trans_id=str(payload.get("merchant_trans_id") or payload.get("transaction_param") or ""),
            action=str(payload.get("action") or ""),
            payload=payload,
        )
        if not self.verify_signature(payload):
            self._log_flow_step("handle_callback_signature_invalid", level="warning")
            raise PaymentError("Invalid Click signature")

        action = self._normalize_action(payload.get("action"))
        self._log_flow_step("handle_callback_action_resolved", action=action)
        if action == "prepare":
            return self.handle_prepare(payload)
        return self.handle_complete(payload)

    def handle_prepare(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        merchant_trans_id = str(payload.get("merchant_trans_id") or payload.get("transaction_param") or "")
        self._log_flow_step(
            "prepare_started",
            merchant_trans_id=merchant_trans_id,
            click_trans_id=str(payload.get("click_trans_id") or ""),
        )
        order = Order.query.filter_by(order_number=merchant_trans_id).first()
        payment = Payment.query.filter_by(order_id=order.id).with_for_update().first() if order else None
        if not payment:
            self._log_flow_step(
                "prepare_payment_not_found",
                level="warning",
                merchant_trans_id=merchant_trans_id,
            )
            return {"error": -5, "error_note": "Transaction not found"}

        requested_amount = self._normalize_amount(payload.get("amount"))
        expected_amount = self._normalize_amount(payment.amount)
        if expected_amount != requested_amount:
            self._log_flow_step(
                "prepare_amount_mismatch",
                level="warning",
                payment=payment,
                requested_amount=str(requested_amount),
                expected_amount=str(expected_amount),
            )
            response = {"error": -2, "error_note": "Incorrect amount"}
            self._append_callback_audit(payment, stage="prepare", request_payload=payload, response_payload=response)
            return response

        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get("click") or {})
        click_data["click_trans_id"] = str(payload.get("click_trans_id") or click_data.get("click_trans_id") or "")
        click_paydoc_id = payload.get("click_paydoc_id")
        if click_paydoc_id not in (None, ""):
            click_data["click_paydoc_id"] = str(click_paydoc_id)
        click_data["merchant_prepare_id"] = payment.id
        click_data["last_prepare_at"] = datetime.now(timezone.utc).isoformat()
        click_data["prepare_payload"] = payload
        provider_data["click"] = click_data
        payment.provider_data = provider_data
        payment.webhook_attempts = int(payment.webhook_attempts or 0) + 1

        self._get_payment_fiscalization_service().reserve_required_marking_codes(payment)
        self._record_transaction(payment, "click_prepare", payload)
        self._log_flow_step(
            "prepare_processing_completed",
            payment=payment,
            webhook_attempts=payment.webhook_attempts,
            click_paydoc_id=click_data.get("click_paydoc_id"),
        )

        response = {
            "click_trans_id": payload.get("click_trans_id"),
            "merchant_trans_id": payment.order.order_number,
            "merchant_prepare_id": payment.id,
            "error": 0,
            "error_note": "Success",
        }
        self._append_callback_audit(payment, stage="prepare", request_payload=payload, response_payload=response)
        db.session.flush()
        self._log_flow_step("prepare_response_sent", payment=payment, response_error=response.get("error"))
        return response

    def handle_complete(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        merchant_prepare_id = payload.get("merchant_prepare_id")
        merchant_trans_id = str(payload.get("merchant_trans_id") or payload.get("transaction_param") or "")
        self._log_flow_step(
            "complete_started",
            merchant_prepare_id=merchant_prepare_id,
            merchant_trans_id=merchant_trans_id,
            click_trans_id=str(payload.get("click_trans_id") or ""),
        )
        payment = None

        if merchant_prepare_id:
            payment = Payment.query.filter_by(id=merchant_prepare_id).with_for_update().first()
        if not payment and merchant_trans_id:
            order = Order.query.filter_by(order_number=merchant_trans_id).first()
            payment = Payment.query.filter_by(order_id=order.id).with_for_update().first() if order else None
        if not payment:
            self._log_flow_step(
                "complete_payment_not_found",
                level="warning",
                merchant_prepare_id=merchant_prepare_id,
                merchant_trans_id=merchant_trans_id,
            )
            return {"error": -6, "error_note": "Transaction not found"}

        requested_amount = self._normalize_amount(payload.get("amount"))
        expected_amount = self._normalize_amount(payment.amount)
        if expected_amount != requested_amount:
            self._log_flow_step(
                "complete_amount_mismatch",
                level="warning",
                payment=payment,
                requested_amount=str(requested_amount),
                expected_amount=str(expected_amount),
            )
            response = {"error": -2, "error_note": "Incorrect amount"}
            self._append_callback_audit(payment, stage="complete", request_payload=payload, response_payload=response)
            return response

        click_error = self._normalize_error_code(payload.get("error"))
        click_error_note = str(payload.get("error_note") or "").strip() or "Transaction cancelled"

        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get("click") or {})
        click_data["click_trans_id"] = str(payload.get("click_trans_id") or click_data.get("click_trans_id") or "")
        click_paydoc_id = payload.get("click_paydoc_id")
        if click_paydoc_id not in (None, ""):
            click_data["click_paydoc_id"] = str(click_paydoc_id)
        click_data["merchant_prepare_id"] = merchant_prepare_id or click_data.get("merchant_prepare_id") or payment.id
        click_data["last_complete_at"] = datetime.now(timezone.utc).isoformat()
        click_data["complete_payload"] = payload
        provider_data["click"] = click_data
        payment.provider_data = provider_data
        payment.provider_transaction_id = str(payload.get("click_trans_id") or payment.provider_transaction_id or "")
        payment.webhook_processed = True
        payment.webhook_attempts = int(payment.webhook_attempts or 0) + 1

        if payment.status == PaymentStatus.COMPLETED:
            self._log_flow_step("complete_already_paid", payment=payment, level="info")
            response = self._build_error_response(-4, "Already paid")
            self._append_callback_audit(payment, stage="complete", request_payload=payload, response_payload=response)
            db.session.flush()
            return response

        if payment.status in {PaymentStatus.CANCELLED, PaymentStatus.FAILED}:
            self._log_flow_step("complete_already_cancelled_or_failed", payment=payment, level="info")
            response = self._build_error_response(-9, "Transaction cancelled")
            self._append_callback_audit(payment, stage="complete", request_payload=payload, response_payload=response)
            db.session.flush()
            return response

        if click_error != 0:
            self._log_flow_step(
                "complete_cancelled_by_click",
                payment=payment,
                level="warning",
                click_error=click_error,
                click_error_note=click_error_note,
            )
            payment.status = PaymentStatus.CANCELLED
            payment.failure_reason = click_error_note
            self._record_transaction(
                payment,
                "click_complete_cancelled",
                payload,
                success=False,
                status="cancelled",
            )
            self._get_payment_fiscalization_service().release_reserved_marking_codes(
                payment,
                reason="click_complete_cancelled",
            )
            response = self._build_error_response(-9, "Transaction cancelled")
            self._append_callback_audit(payment, stage="complete", request_payload=payload, response_payload=response)
            db.session.flush()
            self._log_flow_step(
                "complete_response_sent_cancelled", payment=payment, response_error=response.get("error")
            )
            return response

        payment.status = PaymentStatus.COMPLETED
        payment.failure_reason = None
        payment.paid_at = payment.paid_at or datetime.now(timezone.utc)
        self._record_transaction(payment, "click_complete", payload)
        self._log_flow_step(
            "complete_payment_marked_completed",
            payment=payment,
            paid_at=payment.paid_at.isoformat() if payment.paid_at else None,
            click_paydoc_id=click_data.get("click_paydoc_id"),
        )

        if self.payment_service:
            self._log_flow_step("complete_triggering_post_payment_actions", payment=payment)
            self.payment_service._handle_successful_payment(payment)
            self.payment_service.queue_click_fiscalization(payment.id)
            self._log_flow_step("complete_post_payment_actions_done", payment=payment)

        response = self._build_success_response(payment, payload)
        self._append_callback_audit(payment, stage="complete", request_payload=payload, response_payload=response)
        db.session.flush()
        self._log_flow_step("complete_response_sent_success", payment=payment, response_error=response.get("error"))
        return response

    def _build_merchant_headers(self) -> Dict[str, str]:
        self._log_flow_step("build_merchant_headers_started")
        if not self.merchant_api_user_id or not self.merchant_api_secret_key:
            self._log_flow_step(
                "build_merchant_headers_failed_missing_credentials",
                level="error",
                has_user_id=bool(self.merchant_api_user_id),
                has_secret_key=bool(self.merchant_api_secret_key),
            )
            raise PaymentError("Click merchant API credentials are not configured")

        timestamp = str(int(time.time()))
        digest_source = f"{timestamp}{self.merchant_api_secret_key}"
        digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Auth": f"{self.merchant_api_user_id}:{digest}:{timestamp}",
        }
        self._log_flow_step("build_merchant_headers_completed", has_auth_header=bool(headers.get("Auth")))
        return headers

    def _resolve_merchant_url(self, configured_url: Optional[str], fallback_path: str) -> str:
        if configured_url:
            if configured_url.startswith("http://") or configured_url.startswith("https://"):
                return configured_url
            configured_url = configured_url if configured_url.startswith("/") else f"/{configured_url}"
            return f"{self.merchant_api_url}{configured_url}"

        fallback_path = fallback_path if fallback_path.startswith("/") else f"/{fallback_path}"
        return f"{self.merchant_api_url}{fallback_path}"

    @staticmethod
    def _with_payment_path_params(path_template: str, service_id: int, payment_id: int) -> str:
        template = str(path_template or "").strip()
        if not template:
            return template
        if "{service_id}" in template or "{payment_id}" in template:
            return template.format(service_id=service_id, payment_id=payment_id)
        normalized = template.rstrip("/")
        if normalized.endswith("/payment/status"):
            return f"{normalized}/{service_id}/{payment_id}"
        if normalized.endswith("/payment/reversal"):
            return f"{normalized}/{service_id}/{payment_id}"
        if normalized.endswith("/payment/ofd_data"):
            return f"{normalized}/{service_id}/{payment_id}"
        return template

    def _normalize_merchant_response(self, response: Any) -> Dict[str, Any]:
        if not isinstance(response, dict):
            return {"raw": response}
        return response.get("result") or response.get("data") or response

    @staticmethod
    def _extract_error_code(payload: Dict[str, Any]) -> Optional[int]:
        if not isinstance(payload, dict):
            return None
        for key in ("error_code", "errorCode", "error"):
            if key not in payload:
                continue
            value = payload.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_error_note(payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        value = payload.get("error_note") or payload.get("errorNote") or payload.get("message")
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _payload_hash(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        if not payload:
            return None
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _map_payment_status(payment_status: Any) -> str:
        try:
            code = int(payment_status)
        except (TypeError, ValueError):
            return "pending"
        if code == 1:
            return PaymentStatus.COMPLETED.value
        if code in {2, -1, -2}:
            return PaymentStatus.CANCELLED.value
        if code in {3, -3}:
            return PaymentStatus.FAILED.value
        return PaymentStatus.PENDING.value

    def _service_id_as_int(self) -> int:
        if self.service_id in (None, ""):
            raise PaymentError("Click service ID is not configured")
        try:
            return int(str(self.service_id))
        except (TypeError, ValueError) as exc:
            raise PaymentError("Click service ID must be numeric") from exc

    def _resolve_click_payment_id(self, payment: Payment) -> int:
        provider_data = dict(payment.provider_data or {})
        click_data = dict(provider_data.get("click") or {})
        candidates = [
            ("provider_data.click.click_paydoc_id", click_data.get("click_paydoc_id")),
            (
                "provider_data.click.complete_payload.click_paydoc_id",
                (click_data.get("complete_payload") or {}).get("click_paydoc_id"),
            ),
            ("provider_transaction_id", payment.provider_transaction_id),
        ]
        for source, value in candidates:
            if value in (None, ""):
                continue
            normalized = str(value).strip()
            if not normalized.isdigit():
                continue
            click_payment_id = int(normalized)
            current_app.logger.info(
                "Resolved Click payment_id for merchant API",
                extra={
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "click_payment_id": click_payment_id,
                    "source": source,
                },
            )
            self._log_flow_step(
                "resolve_click_payment_id_succeeded",
                payment=payment,
                click_payment_id=click_payment_id,
                source=source,
            )
            return click_payment_id
        self._log_flow_step(
            "resolve_click_payment_id_failed",
            level="error",
            payment=payment,
            checked_sources=[source for source, _ in candidates],
        )
        raise PaymentError("missing_click_payment_id")

    def resolve_click_payment_id(self, payment: Payment) -> int:
        """Public wrapper for canonical Click payment ID resolution."""
        return self._resolve_click_payment_id(payment)

    def merchant_request(
        self,
        payload: Optional[Dict[str, Any]] = None,
        *,
        configured_url: Optional[str] = None,
        fallback_path: str,
        method: str = "POST",
        endpoint_label: Optional[str] = None,
        expect_error_code: bool = True,
    ) -> Dict[str, Any]:
        url = self._resolve_merchant_url(configured_url, fallback_path)
        method = (method or "POST").upper()
        self._log_flow_step(
            "merchant_request_started",
            endpoint=endpoint_label or fallback_path,
            method=method,
            url=url,
            payload_hash=self._payload_hash(payload),
        )

        if self.test_mode:
            self._log_flow_step(
                "merchant_request_test_mode_short_circuit",
                endpoint=endpoint_label or fallback_path,
                method=method,
                url=url,
            )
            return {
                "success": True,
                "status": "completed",
                "echo": payload,
                "url": url,
                "method": method,
            }

        payload_hash = self._payload_hash(payload)
        request_kwargs: Dict[str, Any] = {
            "headers": self._build_merchant_headers(),
        }
        if payload:
            if method in {"GET", "DELETE"}:
                request_kwargs["params"] = payload
            else:
                request_kwargs["json"] = payload

        # PAY-003: retry-aware request with circuit breaker. ProviderUnavailableError
        # surfaces to api/payments.py which maps it to HTTP 503 + Retry-After.
        try:
            response = request_with_retry(
                method=method,
                url=url,
                timeout_seconds=self.timeout_seconds,
                retry_config=self._retry_config,
                circuit_key="click_merchant_api",
                circuit_failure_threshold=self._circuit_failure_threshold,
                circuit_recovery_seconds=self._circuit_recovery_seconds,
                **request_kwargs,
            )
        except ProviderUnavailableError as exc:
            self._log_flow_step(
                "merchant_request_provider_unavailable",
                level="error",
                endpoint=endpoint_label or fallback_path,
                method=method,
                url=url,
                error=str(exc),
            )
            raise
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            response_text = response.text[:2000]
            self._log_flow_step(
                "merchant_request_http_error",
                level="error",
                endpoint=endpoint_label or fallback_path,
                method=method,
                url=url,
                status_code=response.status_code,
                response_preview=response_text,
            )
            raise PaymentError(
                f"Click merchant API HTTP error ({method} {url}): {response.status_code} {response_text}"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            self._log_flow_step(
                "merchant_request_json_parse_failed",
                level="error",
                endpoint=endpoint_label or fallback_path,
                method=method,
                url=url,
                status_code=response.status_code,
            )
            raise PaymentError(f"Click merchant API invalid JSON response for {method} {url}") from exc
        self._log_flow_step(
            "merchant_request_http_response",
            endpoint=endpoint_label or fallback_path,
            method=method,
            url=url,
            status_code=response.status_code,
            response_preview=data,
        )
        normalized = self._normalize_merchant_response(data)
        error_code = self._extract_error_code(normalized) if isinstance(normalized, dict) else None
        error_note = self._extract_error_note(normalized) if isinstance(normalized, dict) else None

        current_app.logger.info(
            "Click merchant API request completed",
            extra={
                "payment_provider": PaymentMethod.CLICK.value,
                "endpoint": endpoint_label or fallback_path,
                "method": method,
                "url": url,
                "payload_hash": payload_hash,
                "error_code": error_code,
                "error_note": error_note,
            },
        )
        if expect_error_code and error_code not in (None, 0):
            self._log_flow_step(
                "merchant_request_business_error",
                level="warning",
                endpoint=endpoint_label or fallback_path,
                method=method,
                url=url,
                error_code=error_code,
                error_note=error_note,
            )
            raise PaymentError(
                f"Click merchant API error for {endpoint_label or fallback_path}: "
                f"error_code={error_code}, error_note={error_note or 'unknown'}"
            )
        self._log_flow_step(
            "merchant_request_succeeded",
            endpoint=endpoint_label or fallback_path,
            method=method,
            url=url,
            error_code=error_code,
        )
        return normalized

    def check_payment_status(self, payment: Payment) -> Dict[str, Any]:
        self._log_flow_step("check_payment_status_started", payment=payment)
        if self.test_mode:
            return {
                "status": payment.status.value if hasattr(payment.status, "value") else payment.status,
                "provider_transaction_id": payment.provider_transaction_id,
            }

        service_id = self._service_id_as_int()
        click_payment_id = self._resolve_click_payment_id(payment)
        response = self.merchant_request(
            method="GET",
            configured_url=current_app.config.get("CLICK_MERCHANT_STATUS_URL"),
            fallback_path=self._with_payment_path_params(
                current_app.config.get("CLICK_MERCHANT_API_STATUS_PATH") or "/payment/status",
                service_id,
                click_payment_id,
            ),
            endpoint_label="payment_status",
            expect_error_code=True,
        )
        result = {
            "status": self._map_payment_status(response.get("payment_status")),
            "payment_status_code": response.get("payment_status"),
            "provider_transaction_id": str(response.get("payment_id") or click_payment_id),
            "raw": response,
        }
        self._log_flow_step(
            "check_payment_status_completed",
            payment=payment,
            click_payment_id=click_payment_id,
            provider_status_code=result["payment_status_code"],
            mapped_status=result["status"],
        )
        return result

    def refund_payment(self, payment: Payment, amount: Decimal, reason: Optional[str] = None) -> Dict[str, Any]:
        self._log_flow_step(
            "refund_payment_started",
            payment=payment,
            amount=str(Decimal(str(amount or 0)).quantize(Decimal("0.01"))),
            reason=reason,
        )
        if self.test_mode:
            return {
                "success": True,
                "status": "refunded",
                "provider_transaction_id": payment.provider_transaction_id,
                "receipt_payload": {
                    "amount": float(Decimal(str(amount or 0)).quantize(Decimal("0.01"))),
                    "reason": reason,
                },
            }

        service_id = self._service_id_as_int()
        click_payment_id = self._resolve_click_payment_id(payment)
        response = self.merchant_request(
            method="DELETE",
            configured_url=current_app.config.get("CLICK_MERCHANT_REFUND_URL"),
            fallback_path=self._with_payment_path_params(
                current_app.config.get("CLICK_MERCHANT_API_REFUND_PATH") or "/payment/reversal",
                service_id,
                click_payment_id,
            ),
            endpoint_label="payment_reversal",
            expect_error_code=True,
        )
        result = {
            "success": True,
            "status": "refunded",
            "provider_transaction_id": str(response.get("payment_id") or click_payment_id),
            "receipt_payload": response,
        }
        self._log_flow_step(
            "refund_payment_completed",
            payment=payment,
            click_payment_id=click_payment_id,
            provider_transaction_id=result["provider_transaction_id"],
        )
        return result

    def fetch_ofd_data(self, payment: Payment) -> Dict[str, Any]:
        self._log_flow_step("fetch_ofd_data_started", payment=payment)
        service_id = self._service_id_as_int()
        click_payment_id = self._resolve_click_payment_id(payment)
        response = self.merchant_request(
            method="GET",
            configured_url=current_app.config.get("CLICK_MERCHANT_OFD_DATA_URL"),
            fallback_path=self._with_payment_path_params(
                current_app.config.get("CLICK_MERCHANT_API_OFD_DATA_PATH") or "/payment/ofd_data",
                service_id,
                click_payment_id,
            ),
            endpoint_label="ofd_data",
            expect_error_code=False,
        )
        result = {
            "payment_id": response.get("paymentId") or response.get("payment_id") or click_payment_id,
            "receipt_url": response.get("qrCodeURL") or response.get("qrcode") or response.get("receipt_url"),
            "response": response,
        }
        self._log_flow_step(
            "fetch_ofd_data_completed",
            payment=payment,
            click_payment_id=click_payment_id,
            receipt_url_present=bool(result.get("receipt_url")),
        )
        return result

    def submit_fiscal_qrcode(self, payment: Payment, qrcode: str) -> Dict[str, Any]:
        self._log_flow_step(
            "submit_fiscal_qrcode_started",
            payment=payment,
            qrcode_present=bool(qrcode),
        )
        service_id = self._service_id_as_int()
        click_payment_id = self._resolve_click_payment_id(payment)
        payload = {
            "service_id": service_id,
            "payment_id": click_payment_id,
            "qrcode": qrcode,
        }
        response = self.merchant_request(
            payload,
            method="POST",
            configured_url=current_app.config.get("CLICK_MERCHANT_SUBMIT_QRCODE_URL"),
            fallback_path=(
                current_app.config.get("CLICK_MERCHANT_API_SUBMIT_QRCODE_PATH") or "/payment/ofd_data/submit_qrcode"
            ),
            endpoint_label="submit_qrcode",
            expect_error_code=True,
        )
        self._log_flow_step(
            "submit_fiscal_qrcode_completed",
            payment=payment,
            click_payment_id=click_payment_id,
            error_code=response.get("error_code"),
        )
        return response

    def fiscalize_payment(self, payment: Payment, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._log_flow_step(
            "fiscalize_payment_started",
            payment=payment,
            payload=payload,
            payload_hash=self._payload_hash(payload),
            items_count=len(payload.get("items") or []),
        )
        if self.test_mode:
            receipt_id = f"click-fiscal-{payment.id}"
            self._log_flow_step(
                "fiscalize_payment_test_mode_short_circuit",
                payment=payment,
                receipt_id=receipt_id,
            )
            return {
                "success": True,
                "status": "completed",
                "receipt_id": receipt_id,
                "receipt_url": f"{current_app.config.get('COMPANY_WEBSITE', '').rstrip('/')}/admin/payments/{payment.id}",  # noqa: E501
                "receipt_payload": payload,
            }

        submit_response = self.merchant_request(
            payload,
            method="POST",
            configured_url=current_app.config.get("CLICK_MERCHANT_FISCALIZATION_URL"),
            fallback_path=(
                current_app.config.get("CLICK_MERCHANT_API_FISCALIZATION_PATH") or "/payment/ofd_data/submit_items"
            ),
            endpoint_label="submit_items",
            expect_error_code=True,
        )
        self._log_flow_step(
            "fiscalize_payment_submit_items_completed",
            payment=payment,
            error_code=submit_response.get("error_code"),
            error_note=submit_response.get("error_note"),
        )
        click_payment_id = self._resolve_click_payment_id(payment)
        provider_status = "submitted"
        receipt_url = None
        ofd_response: Dict[str, Any] = {}
        ofd_error = None
        try:
            ofd_payload = self.fetch_ofd_data(payment)
            receipt_url = ofd_payload.get("receipt_url")
            ofd_response = ofd_payload.get("response") or {}
            if not receipt_url:
                provider_status = "submitted_no_qr"
                ofd_error = "missing_qrcode_url"
                self._log_flow_step(
                    "fiscalize_payment_ofd_missing_qr",
                    level="warning",
                    payment=payment,
                    click_payment_id=click_payment_id,
                )
        except Exception as exc:  # noqa: BLE001
            provider_status = "submitted_no_qr"
            ofd_error = str(exc)
            self._log_flow_step(
                "fiscalize_payment_ofd_fetch_failed",
                level="warning",
                payment=payment,
                click_payment_id=click_payment_id,
                error_message=str(exc),
            )

        receipt_payload = {
            "submit_items": submit_response,
            "ofd_data": ofd_response,
        }
        if ofd_error:
            receipt_payload["ofd_data_error"] = ofd_error

        result = {
            "success": True,
            "status": provider_status,
            "receipt_id": str(click_payment_id),
            "receipt_url": receipt_url,
            "receipt_payload": receipt_payload,
            "submit_items": submit_response,
            "ofd_data": ofd_response,
            "click_paydoc_id": click_payment_id,
            "error_note": ofd_error,
        }
        self._log_flow_step(
            "fiscalize_payment_completed",
            payment=payment,
            click_payment_id=click_payment_id,
            provider_status=provider_status,
            receipt_url_present=bool(receipt_url),
        )
        return result

    def process_card_payment(self, payment: Payment, card: CreditCard) -> Dict[str, Any]:
        """
        Process card payment through Click gateway.

        Placeholder — test mode returns a synthetic transaction; production path
        raises until the real Click card API integration lands.
        """
        try:
            current_app.logger.info(f"Processing Click card payment for payment {payment.id}")

            if self.test_mode:
                return {
                    "success": True,
                    "transaction_id": f"click_{int(datetime.now(timezone.utc).timestamp())}",
                    "amount": payment.amount,
                    "card_token": card.card_token,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            raise NotImplementedError("Click card payment API integration required")
        except Exception as e:
            current_app.logger.exception("Click card payment error")
            return {"success": False, "error_message": str(e)}
