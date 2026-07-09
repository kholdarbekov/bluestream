"""
Payment service for the Water Business Platform
Supports Payme, Click, Cash, and Loyalty Points payments
"""

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from flask import current_app
import redis
from sqlalchemy.orm import joinedload

from business_app.models.order import Order
from business_app.models.payment import Payment, PaymentTransaction, CreditCard
from business_app.utils.exceptions import PaymentError, ProviderUnavailableError, ValidationError, NotFoundError
from business_app.utils.constants import (
    PaymeErrors,
    PaymentMethodType,
)
from shared.enums import (
    OrderStatus,
    PaymentStatus,
    PaymentMethod,
    FiscalizationStatus,
)
from business_app.utils.helpers import generate_random_string
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.payment_projection import (
    get_payment_projection,
    is_settled_prepayment,
)
from business_app.utils.translations import get_translation
from business_app.utils.state_validators import assert_cash_payment_collector
from business_app.services.card_token_service import CardTokenService
from business_app.services.providers.payme_provider import PaymeProvider
from business_app.services.providers.webhook_signature import WebhookSignatureVerifier
from business_app import db


class PaymentService:
    """Service for handling payment processing"""

    def __init__(self):
        # Payme configuration + handlers are owned by PaymeProvider (ARCH-002 PR 2).
        self.payme_provider = PaymeProvider(payment_service=self)

        # Click configuration + handlers are owned by ClickPaymentProviderService (ARCH-002 PR 4).

        # Webhook replay protection configuration
        self.webhook_tolerance_seconds = current_app.config.get("WEBHOOK_TIMESTAMP_TOLERANCE", 300)  # 5 minutes
        self.webhook_nonce_ttl = current_app.config.get("WEBHOOK_NONCE_TTL", 3600)  # 1 hour

        # Initialize Redis for nonce tracking
        try:
            redis_url = current_app.config.get("REDIS_URL", "redis://localhost:6379/0")
            # Use a specific DB for webhook/verification tracking (db 3)
            # Parse the URL and replace the db number
            if "/0" in redis_url:
                redis_url = redis_url.replace("/0", "/3")
            elif redis_url.endswith(":6379"):
                redis_url = redis_url + "/3"
            self.redis_client = redis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            current_app.logger.warning(f"Redis not available for webhook nonce tracking: {e}")
            self.redis_client = None

        self._click_provider_service = None
        self._payment_fiscalization_service = None

        self._webhook_signature_verifier = WebhookSignatureVerifier(
            redis_client=self.redis_client,
            verify_payme_signature=self.payme_provider.verify_payme_signature,
            verify_click_signature=self._verify_click_signature,
            tolerance_seconds=self.webhook_tolerance_seconds,
            nonce_ttl_seconds=self.webhook_nonce_ttl,
        )

        # Card tokenization + saved-card CRUD owned by CardTokenService (ARCH-002 PR 3).
        self.card_token_service = CardTokenService(
            payme_provider=self.payme_provider,
            redis_client=self.redis_client,
        )

    def _get_click_provider_service(self):
        if self._click_provider_service is None:
            from business_app.services.click_payment_provider_service import ClickPaymentProviderService

            self._click_provider_service = ClickPaymentProviderService(payment_service=self)
        return self._click_provider_service

    def _get_payment_fiscalization_service(self):
        if self._payment_fiscalization_service is None:
            from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

            self._payment_fiscalization_service = PaymentFiscalizationService(
                click_provider_service=self._get_click_provider_service(),
            )
        return self._payment_fiscalization_service

    def create_payment(self, order_id: int, payment_method: PaymentMethod, amount: int = None, **kwargs) -> Payment:
        """
        Create or update the canonical payment record for an order.

        Args:
            order_id: Order ID
            payment_method: Payment method
            amount: Payment amount (defaults to order total)
            **kwargs: Additional payment data

        Returns:
            Payment object

        Raises:
            NotFoundError: If order not found
            ValidationError: If payment data invalid
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError(get_translation("error.not_found"))

        # Use order total if amount not specified
        if amount is None:
            amount = order.total_amount

        # Validate amount
        if amount <= 0:
            raise ValidationError(get_translation("error.validation.invalid_amount"))

        if amount > order.total_amount:
            raise ValidationError(get_translation("error.validation.amount_exceeds_total"))

        # PAY-005: short-circuit duplicate creation requests via the
        # per-attempt idempotency key BEFORE falling back to order_id lookup.
        # The order_id branch still wins for cases where caller is updating
        # an existing payment (different amount/method on retry); idempotency
        # only matches when (order, user, amount, method) all align.
        idempotency_key = Payment.compute_idempotency_key(
            order_id=order_id,
            user_id=order.user_id,
            amount=amount,
            payment_method=payment_method,
        )
        payment = Payment.query.filter_by(idempotency_key=idempotency_key).first()
        if payment is None:
            payment = Payment.query.filter_by(order_id=order_id).first()
        provider_data = dict(kwargs)
        consume_marking_codes = bool(
            provider_data.pop(
                "consume_marking_codes",
                payment_method in {PaymentMethod.CLICK, PaymentMethod.CARD},
            )
        )

        if payment:
            payment.user_id = order.user_id
            payment.payment_method = payment_method
            payment.amount = amount
            payment.currency = provider_data.pop("currency", payment.currency or "UZS")
            payment.description = provider_data.pop("description", payment.description)
            payment.callback_url = provider_data.pop("return_url", payment.callback_url)
            payment.failure_reason = None
            payment.provider_data = provider_data
            payment.consume_marking_codes = consume_marking_codes
            # PAY-005: refresh idempotency_key when amount/method changes —
            # the row's identity for de-duping shifts with the new attempt scope.
            payment.idempotency_key = idempotency_key
            if payment_method != PaymentMethod.CASH:
                payment.status = PaymentStatus.PENDING
        else:
            payment = Payment(
                order_id=order_id,
                user_id=order.user_id,
                payment_method=payment_method,
                amount=amount,
                status=PaymentStatus.PENDING,
                currency=provider_data.pop("currency", "UZS"),
                description=provider_data.pop("description", None),
                callback_url=provider_data.pop("return_url", None),
                provider_data=provider_data,
                consume_marking_codes=consume_marking_codes,
                idempotency_key=idempotency_key,
            )
            db.session.add(payment)

        if payment_method == PaymentMethod.CASH:
            from business_app.services.cash_collection_service import CashCollectionService

            payment.amount_collected = payment.amount_collected or Decimal("0.00")
            payment.outstanding_amount = amount
            CashCollectionService().sync_payment_projection(payment)

        db.session.commit()

        return payment

    def initialize_order_payment(
        self,
        order_id: int,
        actor_user_id: Optional[int] = None,
        paid_at: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        trigger_notifications: bool = True,
        allow_order_confirmation: bool = True,
        commit: bool = True,
    ) -> Optional[Payment]:
        """Create or finalize the canonical payment record for an order."""
        order = Order.query.options(
            joinedload(Order.payment),
            joinedload(Order.order_items),
        ).get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        payment_method = order.payment_method
        if isinstance(payment_method, str):
            try:
                payment_method = PaymentMethod(payment_method)
            except ValueError:
                return order.payment
        if payment_method not in {
            PaymentMethod.PAYME,
            PaymentMethod.CLICK,
            PaymentMethod.CARD,
            PaymentMethod.BUSINESS_ACCOUNT,
            PaymentMethod.CASH,
        }:
            return order.payment

        if payment_method == PaymentMethod.CASH:
            from business_app.services.cash_collection_service import CashCollectionService

            payment = CashCollectionService().ensure_cod_payment_for_order(
                order,
                actor_user_id=actor_user_id,
                metadata=metadata,
            )
            # ARCH-008: when caller manages an outer transaction (e.g. order_service.create_order),
            # they pass commit=False so a single DB rollback can undo everything.
            if commit:
                db.session.commit()
            return payment

        payment = order.payment
        if not payment:
            payment = Payment(
                order_id=order.id,
                user_id=order.user_id,
                amount=order.total_amount,
                currency="UZS",
                payment_method=payment_method,
                status=PaymentStatus.PENDING,
                description=f"Payment for order #{order.order_number}",
                provider_data={},
                consume_marking_codes=payment_method in {PaymentMethod.CLICK, PaymentMethod.CARD},
            )
            db.session.add(payment)
            db.session.flush()
        elif payment.payment_method != payment_method:
            payment.payment_method = payment_method

        if payment_method == PaymentMethod.BUSINESS_ACCOUNT and metadata and "consume_marking_codes" in metadata:
            payment.consume_marking_codes = bool(metadata.get("consume_marking_codes"))
        elif payment_method in {PaymentMethod.CLICK, PaymentMethod.CARD}:
            payment.consume_marking_codes = True

        if payment_method in {PaymentMethod.PAYME, PaymentMethod.CLICK, PaymentMethod.CARD}:
            # ARCH-008: respect outer-transaction commit ownership.
            if commit:
                db.session.commit()
            return payment

        provider_data = dict(payment.provider_data or {})
        provider_data.update(self._build_business_account_payment_metadata(order, metadata))
        if actor_user_id is not None:
            provider_data["actor_user_id"] = actor_user_id
        payment.provider_data = provider_data

        completed_at = paid_at or payment.paid_at or datetime.now(timezone.utc)
        if completed_at and completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        current_status_value = payment.status.value if hasattr(payment.status, "value") else str(payment.status)
        status_was_completed = current_status_value == PaymentStatus.COMPLETED.value
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = completed_at
        self._sync_order_paid_projection(order, payment.status, completed_at)

        if not status_was_completed:
            self._handle_successful_payment(
                payment,
                trigger_notifications=trigger_notifications,
                allow_order_confirmation=allow_order_confirmation,
            )

        if payment_method == PaymentMethod.BUSINESS_ACCOUNT and payment.consume_marking_codes:
            self._get_payment_fiscalization_service().consume_marking_codes_for_business_account(
                payment,
                actor_user_id=actor_user_id,
            )

        db.session.commit()
        return payment

    def create_payment_link(self, payment_id: int) -> Dict[str, str]:
        """
        Create payment link for external payment gateways

        Args:
            payment_id: Payment ID

        Returns:
            Dictionary with payment URL and other details
        """
        payment: Payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        if payment.payment_method == PaymentMethod.PAYME:
            return self._create_payme_link(payment)
        elif payment.payment_method in {PaymentMethod.CLICK, PaymentMethod.CARD}:
            return self._create_click_link(payment)
        else:
            raise PaymentError(get_translation("error.payment.unsupported_method"))

    def process_cash_payment(self, payment_id: int, collected_by: int = None) -> Payment:
        """Process cash payment"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        if payment.payment_method != PaymentMethod.CASH:
            raise ValidationError(get_translation("error.payment.invalid_method"))

        # ARCH-006: a cash payment cannot be processed without a collector identity.
        assert_cash_payment_collector(
            payment,
            PaymentStatus.COMPLETED,
            collected_by=collected_by,
        )

        from business_app.services.cash_collection_service import CashCollectionService

        payment.collected_by = collected_by
        CashCollectionService().post_collection(
            customer_id=payment.user_id,
            amount=payment.amount,
            source="admin_adjustment",
            collector_user_id=collected_by,
            recorded_by_user_id=collected_by,
            order_id=payment.order_id,
            notes="Cash payment processed via payment service",
            manual_allocations=[
                {
                    "payment_id": payment.id,
                    "amount": payment.amount,
                }
            ],
            allocation_mode="manual",
        )
        db.session.refresh(payment)

        if payment.status == PaymentStatus.COMPLETED:
            self._handle_successful_payment(payment)

        self._create_transaction(
            payment,
            "payment_completed",
            {
                "collected_by": collected_by,
                "collection_method": "cash_on_delivery",
            },
        )
        db.session.commit()

        return payment

    # NOTE: process_loyalty_points_payment() removed — loyalty points are spent
    # only on rewards (LoyaltyReward.points_cost), never as a direct payment method.
    # The PaymentMethod.LOYALTY_POINTS enum value and the historical refund path
    # (_process_points_refund) are retained so existing points-paid orders still
    # deserialize and can be refunded.

    def _payme_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider. Kept for card-token helpers that still call it.
        Moves out in PR 3 when CardTokenService is extracted."""
        return self.payme_provider._payme_request(method, params)

    # ============================================================================
    # PAYME SUBSCRIBE API METHODS (cards.create, cards.verify, receipts.pay, etc.)
    # ============================================================================

    def _extract_payme_error_message(self, error: Dict) -> str:
        """Delegate to PaymeProvider. Shared with card-token helpers."""
        return self.payme_provider._extract_payme_error_message(error)

    def create_card_token_with_verification(self, card_number: str, expiry: str, save: bool = True) -> Dict[str, Any]:
        """Delegate to CardTokenService.create_card_token_with_verification."""
        return self.card_token_service.create_card_token_with_verification(card_number, expiry, save=save)

    def request_card_verification_code(self, token: str) -> Dict[str, Any]:
        """Delegate to CardTokenService.request_card_verification_code."""
        return self.card_token_service.request_card_verification_code(token)

    def get_verification_attempts_remaining(self, token: str) -> int:
        """Delegate to CardTokenService.get_verification_attempts_remaining."""
        return self.card_token_service.get_verification_attempts_remaining(token)

    def increment_verification_attempts(self, token: str) -> int:
        """Delegate to CardTokenService.increment_verification_attempts."""
        return self.card_token_service.increment_verification_attempts(token)

    def reset_verification_attempts(self, token: str) -> None:
        """Delegate to CardTokenService.reset_verification_attempts."""
        return self.card_token_service.reset_verification_attempts(token)

    def verify_card(self, token: str, code: str) -> Dict[str, Any]:
        """Delegate to CardTokenService.verify_card."""
        return self.card_token_service.verify_card(token, code)

    def create_payme_receipt(self, order: Order, description: str = None) -> Dict[str, Any]:
        """Delegate to PaymeProvider.create_payme_receipt."""
        return self.payme_provider.create_payme_receipt(order, description=description)

    def pay_payme_receipt(self, receipt_id: str, token: str, payer: Dict[str, Any] = None) -> Dict[str, Any]:
        """Delegate to PaymeProvider.pay_payme_receipt."""
        return self.payme_provider.pay_payme_receipt(receipt_id, token, payer=payer)

    def _detect_card_brand(self, masked_number: str) -> str:
        """Delegate to CardTokenService.detect_card_brand."""
        return self.card_token_service.detect_card_brand(masked_number)

    def _save_or_update_verified_card(self, user_id: int, token: str, card_metadata: Dict[str, Any]) -> CreditCard:
        """Delegate to CardTokenService.save_or_update_verified_card."""
        return self.card_token_service.save_or_update_verified_card(user_id, token, card_metadata)

    def process_payme_payment_full(
        self, order_id: int, card_token: str, user_id: int, save_card: bool = True, card_metadata: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Delegate to PaymeProvider.process_payme_payment_full."""
        return self.payme_provider.process_payme_payment_full(
            order_id,
            card_token,
            user_id,
            save_card=save_card,
            card_metadata=card_metadata,
        )

    # ============================================================================
    # END OF PAYME SUBSCRIBE API METHODS
    # ============================================================================

    def _process_click_card_payment(self, payment: Payment, card: CreditCard) -> Dict[str, Any]:
        """Delegate to ClickPaymentProviderService.process_card_payment."""
        return self._get_click_provider_service().process_card_payment(payment, card)

    def handle_payme_webhook(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle Payme Merchant API requests.
        Dispatcher for JSON-RPC 2.0 methods.
        """
        if not isinstance(webhook_data, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        request_id = webhook_data.get("id")
        try:
            # Note: Signature verification is already done by the API endpoint via validate_webhook_signature
            # We skip redundant verification here as requested.

            method = webhook_data.get("method")
            params = webhook_data.get("params", {})

            handlers = {
                "CheckPerformTransaction": self._payme_check_perform_transaction,
                "CreateTransaction": self._payme_create_transaction,
                "PerformTransaction": self._payme_perform_transaction,
                "CancelTransaction": self._payme_cancel_transaction,
                "CheckTransaction": self._payme_check_transaction,
                "GetStatement": self._payme_get_statement,
            }

            handler = handlers.get(method)

            response = None
            if not handler:
                response = {"error": {"code": PaymeErrors.METHOD_NOT_FOUND, "message": "Method not found"}}
            else:
                response = handler(params)

            # Ensure JSON-RPC 2.0 compliance
            response["jsonrpc"] = "2.0"
            response["id"] = request_id

            return response

        except Exception:
            current_app.logger.exception("Payme webhook error")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": PaymeErrors.INTERNAL_ERROR, "message": "Internal system error"},
            }

    def handle_click_webhook(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Click Shop API callbacks synchronously.

        Terminal protocol outcomes are RETURN VALUES from handle_callback.
        Anything raised here is a transient/unexpected failure: roll back and
        re-raise so the route releases the idempotency claim and answers 503 —
        Click's retry then re-enters processing (idempotent under the row
        lock + status guards). Swallowing to {"error": -1} used to get cached
        for 24h and permanently poisoned the gateway's retries.
        """
        try:
            response = self._get_click_provider_service().handle_callback(webhook_data)
            db.session.commit()
            return response
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Click webhook error")
            raise

    def process_refund(self, payment_id: int, amount: int, reason: str = None) -> bool:
        """Process payment refund"""
        payment: Payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError(get_translation("error.payment.cannot_refund"))

        if amount > payment.amount:
            raise ValidationError(get_translation("error.validation.amount_exceeds_total"))

        # Process refund based on payment method
        if payment.payment_method == PaymentMethod.LOYALTY_POINTS:
            success = self._process_points_refund(payment, amount, reason)
        elif payment.payment_method in {PaymentMethod.CLICK, PaymentMethod.CARD}:
            response = self._get_click_provider_service().refund_payment(payment, amount, reason)
            success = bool(response.get("success", True))
            if success:
                provider_data = dict(payment.provider_data or {})
                provider_data["refunded_amount"] = float(amount)
                provider_data["refunded_at"] = datetime.now(timezone.utc).isoformat()
                provider_data["click_refund_response"] = response
                payment.provider_data = provider_data
                fiscalization = getattr(payment, "fiscalization", None)
                fiscalization_status = (
                    fiscalization.status.value
                    if fiscalization and hasattr(fiscalization.status, "value")
                    else fiscalization
                )
                if fiscalization_status != FiscalizationStatus.COMPLETED.value:
                    self._get_payment_fiscalization_service().release_reserved_marking_codes(
                        payment,
                        reason="payment_refunded_before_fiscalization",
                    )
        else:
            # Cash refund - manual process
            success = True

        if success:
            # Update payment status
            if amount == payment.amount:
                payment.status = PaymentStatus.CANCELLED
            else:
                payment.status = PaymentStatus.PARTIALLY_REFUNDED
            self._sync_order_paid_projection(payment.order, payment.status)
            db.session.commit()

        return success

    def request_refund(self, payment: Payment, reason: Optional[str] = None) -> PaymentTransaction:
        """Create and process a refund request for a completed payment."""
        refund_reason = reason or "Refund requested"
        refund_success = self.process_refund(payment.id, payment.amount, refund_reason)
        transaction = PaymentTransaction(
            payment_id=payment.id,
            transaction_type="refund",
            amount=payment.amount,
            currency=payment.currency,
            status="completed" if refund_success else "failed",
            provider_transaction_id=payment.provider_transaction_id,
            provider_reference=payment.payment_id,
            provider_response=dict(payment.provider_data or {}),
            success=refund_success,
            failure_reason=None if refund_success else refund_reason,
            processed_at=datetime.now(timezone.utc),
            notes=refund_reason,
        )
        db.session.add(transaction)
        db.session.commit()
        return transaction

    def queue_click_fiscalization(self, payment_id: int):
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError("Payment not found")
        fiscalization = self._get_payment_fiscalization_service().queue_click_fiscalization(payment.id)
        db.session.commit()

        if current_app.config.get("TESTING"):
            fiscalization = self._get_payment_fiscalization_service().process_click_fiscalization(payment.id)
            db.session.commit()
            return fiscalization

        try:
            from business_app.tasks.payment_tasks import process_click_fiscalization_task

            process_click_fiscalization_task.delay(payment.id)
        except Exception:
            current_app.logger.exception("Failed to enqueue Click fiscalization for payment %s", payment.id)
        return fiscalization

    def update_payment_status(self, payment: Payment) -> Payment:
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        if payment.status != PaymentStatus.PENDING:
            return payment

        provider = payment.payment_provider
        if provider == PaymentMethod.CLICK.value:
            try:
                status_data = self._get_click_provider_service().check_payment_status(payment)
            except (PaymentError, ProviderUnavailableError) as exc:
                # H1: transport errors must NOT masquerade as gateway evidence
                # (e.g. as an affirmative not_found) downstream.
                payment._last_gateway_status = None
                current_app.logger.warning(
                    "Click status check failed; leaving payment PENDING",
                    extra={"payment_id": payment.id, "order_id": payment.order_id, "error": str(exc)},
                )
                db.session.rollback()
                return payment
            provider_status = str(status_data.get("status") or "").lower()
            # H1 bridge: record the gateway-reported status for ALL outcomes as a
            # NON-PERSISTENT instance attribute so check_payment_status() can
            # surface affirmative evidence (e.g. "not_found") that this method
            # intentionally does not act on. Never written to the DB.
            payment._last_gateway_status = provider_status
            if provider_status in {"completed", PaymentStatus.COMPLETED.value, "success"}:
                provider_txn_id = status_data.get("provider_transaction_id")
                if not provider_txn_id:
                    current_app.logger.warning(
                        "Click reported completed without provider_transaction_id; leaving PENDING",
                        extra={
                            "payment_id": payment.id,
                            "order_id": payment.order_id,
                            "raw": status_data.get("raw"),
                        },
                    )
                    db.session.rollback()
                    return payment
                payment.status = PaymentStatus.COMPLETED
                payment.paid_at = payment.paid_at or datetime.now(timezone.utc)
                payment.provider_transaction_id = provider_txn_id
                self._handle_successful_payment(payment)
                self.queue_click_fiscalization(payment.id)
                db.session.commit()
            elif provider_status in {
                "cancelled",
                "canceled",
                PaymentStatus.CANCELLED.value,
                "failed",
                PaymentStatus.FAILED.value,
                "error",
            }:
                payment.status = (
                    PaymentStatus.CANCELLED
                    if provider_status in {"cancelled", "canceled", PaymentStatus.CANCELLED.value}
                    else PaymentStatus.FAILED
                )
                payment.failure_reason = (
                    str(status_data.get("error_note") or "")
                    or str((status_data.get("raw") or {}).get("error_note") or "")
                    or str((status_data.get("raw") or {}).get("error") or "")
                    or "Provider reported payment failure"
                )
                self._get_payment_fiscalization_service().release_reserved_marking_codes(
                    payment,
                    reason="provider_status_cancelled",
                )
                db.session.commit()
        return payment

    def check_payment_status(self, payment_id: int) -> Dict[str, Any]:
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        self.update_payment_status(payment)
        payload = self.get_payment_status(payment_id)
        # H1 bridge: surface the gateway's affirmative "not_found" evidence that
        # update_payment_status intentionally does not persist. Only while the
        # payment is still PENDING — nothing is written here; the reconcile
        # task's PAY-007 order-state gate still governs any cancel decision.
        gateway_status = getattr(payment, "_last_gateway_status", None)
        if gateway_status == "not_found" and payment.status == PaymentStatus.PENDING:
            payload["status"] = "not_found"
            payload["not_found"] = True
        return payload

    def get_user_payment_statistics(self, user_id: int, period: str = "year") -> Dict[str, Any]:
        """Get aggregated payment statistics for a user.

        Extracted verbatim from the previous inline implementation in
        ``api/payments.py::get_payment_statistics`` (ARCH: service-layer-first).
        """
        now = datetime.now(timezone.utc)
        if period == "month":
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "quarter":
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:  # all time
            start_date = None

        query = Payment.query.filter_by(user_id=user_id)
        if start_date:
            query = query.filter(Payment.created_at >= start_date)

        payments = query.all()

        total_payments = len(payments)
        successful_payments = len([p for p in payments if p.status == PaymentStatus.COMPLETED])
        failed_payments = len([p for p in payments if p.status == PaymentStatus.FAILED])
        total_amount = sum(p.amount for p in payments if p.status == PaymentStatus.COMPLETED)

        method_stats = {}
        for method in PaymentMethodType:
            method_payments = [p for p in payments if p.payment_method == method]
            method_stats[method.value] = {
                "count": len(method_payments),
                "total_amount": sum(p.amount for p in method_payments if p.status == PaymentStatus.COMPLETED),
                "success_rate": (
                    (
                        len([p for p in method_payments if p.status == PaymentStatus.COMPLETED])
                        / len(method_payments)
                        * 100
                    )
                    if method_payments
                    else 0
                ),
            }

        monthly_spending = {}
        for i in range(12):
            month_start = (now.replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            month_end = (
                month_start.replace(month=month_start.month % 12 + 1)
                if month_start.month < 12
                else month_start.replace(year=month_start.year + 1, month=1)
            )

            month_payments = [
                p
                for p in payments
                if month_start <= ensure_utc(p.created_at) < month_end and p.status == PaymentStatus.COMPLETED
            ]
            month_total = sum(p.amount for p in month_payments)

            monthly_spending[month_start.strftime("%Y-%m")] = month_total

        return {
            "period": period,
            "statistics": {
                "total_payments": total_payments,
                "successful_payments": successful_payments,
                "failed_payments": failed_payments,
                "success_rate": round((successful_payments / total_payments * 100), 2) if total_payments > 0 else 0,
                "total_amount": total_amount,
                "average_payment": round(total_amount / successful_payments, 2) if successful_payments > 0 else 0,
                "payment_methods": method_stats,
                "monthly_spending_trend": monthly_spending,
            },
        }

    def verify_payment(self, payment_id: int, verification_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Compatibility wrapper used by background verification tasks."""
        verification_data = verification_data or {}
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        status_payload = self.check_payment_status(payment_id)
        status_value = str(status_payload.get("status") or "").lower()
        if status_value in {PaymentStatus.COMPLETED.value, "completed", "success"}:
            return {
                "success": True,
                "transaction_id": verification_data.get("transaction_id") or payment.provider_transaction_id,
                "status": PaymentStatus.COMPLETED.value,
            }
        return {
            "success": False,
            "error": verification_data.get("error") or "Verification failed",
            "status": status_value or PaymentStatus.PENDING.value,
        }

    def validate_webhook_signature(self, provider: str, request) -> bool:
        """Validate webhook signature + replay protection via the verifier.

        Public API preserved for existing consumers (``api/payments.py``,
        tests). Low-level HMAC/credential checks still live in
        ``_verify_payme_signature`` / ``_verify_click_signature`` — the
        verifier calls them through injected callables.
        """
        return self._webhook_signature_verifier.validate(provider, request)

    def get_payment_status(self, payment_id: int) -> Dict[str, Any]:
        """Get payment status and details"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        fiscalization_data = (
            payment.fiscalization.to_dict()
            if getattr(payment, "fiscalization", None)
            else {
                "status": (
                    FiscalizationStatus.PENDING.value
                    if payment.payment_provider == PaymentMethod.CLICK.value
                    else FiscalizationStatus.NOT_REQUIRED.value
                )
            }
        )

        return {
            "id": payment.id,
            "status": payment.status.value,
            "method": payment.payment_method.value,
            "payment_provider": payment.payment_provider,
            "amount": payment.amount,
            "currency": payment.currency,
            "payment_id": payment.payment_id,
            "created_at": payment.created_at.isoformat(),
            "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
            "gateway_reference": payment.gateway_reference,
            "gateway_response": payment.gateway_response,
            "refunded_amount": payment.refunded_amount or 0,
            "refunded_at": payment.refunded_at.isoformat() if payment.refunded_at else None,
            "payment_link": payment.payment_link,
            "provider_transaction_id": payment.provider_transaction_id,
            "fiscalization": fiscalization_data,
            "fiscalization_status": fiscalization_data.get("status"),
            "transactions": [
                {
                    "id": tx.id,
                    "type": tx.transaction_type,
                    "amount": tx.amount,
                    "status": tx.status,
                    "created_at": tx.created_at.isoformat(),
                    "gateway_response": tx.gateway_response,
                }
                for tx in payment.transactions
            ],
        }

    # Private methods for Payme integration — all delegate to PaymeProvider
    def _create_payme_link(self, payment: Payment) -> Dict[str, str]:
        """Delegate to PaymeProvider.create_payme_link."""
        return self.payme_provider.create_payme_link(payment)

    def _verify_payme_signature(self, data: Dict[str, Any]) -> bool:
        """Delegate to PaymeProvider.verify_payme_signature."""
        return self.payme_provider.verify_payme_signature(data)

    def _payme_check_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider.check_perform_transaction."""
        return self.payme_provider.check_perform_transaction(params)

    def _payme_create_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider.create_transaction."""
        return self.payme_provider.create_transaction(params)

    def _payme_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider.perform_transaction."""
        return self.payme_provider.perform_transaction(params)

    def _payme_cancel_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider.cancel_transaction."""
        return self.payme_provider.cancel_transaction(params)

    def _payme_check_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider.check_transaction."""
        return self.payme_provider.check_transaction(params)

    def _payme_get_statement(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Delegate to PaymeProvider.get_statement."""
        return self.payme_provider.get_statement(params)

    # Private methods for Click integration
    def _create_click_link(self, payment: Payment) -> Dict[str, str]:
        """Create Click payment link via dedicated provider service."""
        return self._get_click_provider_service().create_payment_link(payment)

    def _verify_click_signature(self, data: Dict[str, Any]) -> bool:
        """Verify Click webhook signature."""
        return self._get_click_provider_service().verify_signature(data)

    def _click_prepare(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Click prepare request."""
        return self._get_click_provider_service().handle_prepare(data)

    def _click_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Click complete request."""
        return self._get_click_provider_service().handle_complete(data)

    # Helper methods
    def _generate_payment_id(self) -> str:
        """Generate unique payment reference"""
        return f"PAY_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{generate_random_string(6).upper()}"

    def _create_transaction(self, payment: Payment, transaction_type: str, data: Dict[str, Any]) -> PaymentTransaction:
        """Create payment transaction record"""
        transaction = PaymentTransaction(
            payment_id=payment.id,
            transaction_type=transaction_type,
            amount=data.get("amount", payment.amount),
            status="completed" if transaction_type == "completed" else "pending",
            provider_transaction_id=data.get("receipt_id"),
            provider_response=data,
        )

        db.session.add(transaction)
        return transaction

    def _build_business_account_payment_metadata(
        self,
        order: Order,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        from business_app.models.corporate import CorporateContract

        contract_ids = sorted(
            {int(item.contract_id) for item in (order.order_items or []) if getattr(item, "contract_id", None)}
        )
        debt_contract_ids = set()
        if contract_ids:
            debt_contract_ids = {
                contract_id
                for contract_id, allows_debt in db.session.query(
                    CorporateContract.id,
                    CorporateContract.allows_debt,
                )
                .filter(CorporateContract.id.in_(contract_ids))
                .all()
                if allows_debt
            }

        payload = {
            "settlement_mode": "corporate_contract",
            "contract_ids": contract_ids,
            "has_debt_enabled_contract": bool(debt_contract_ids),
            "debt_enabled_contract_ids": sorted(debt_contract_ids),
        }
        if metadata:
            payload.update(metadata)
        return payload

    def _sync_order_paid_projection(
        self,
        order: Optional[Order],
        payment_status: Any,
        paid_at: Optional[datetime] = None,
    ) -> None:
        if not order:
            return

        status_value = payment_status.value if hasattr(payment_status, "value") else str(payment_status)
        is_completed = status_value == PaymentStatus.COMPLETED.value
        order.is_paid = is_completed
        order.paid_at = paid_at if is_completed else None

    @staticmethod
    def _sync_completed_prepayment_projection(payment: Optional[Payment]) -> None:
        """Keep persisted projection fields aligned for completed prepaid payments."""
        if not payment or not is_settled_prepayment(payment):
            return

        projection = get_payment_projection(payment)
        payment.amount_collected = projection["amount_collected"]
        payment.outstanding_amount = projection["outstanding_amount"]

    def _handle_successful_payment(
        self,
        payment: Payment,
        *,
        trigger_notifications: bool = True,
        allow_order_confirmation: bool = True,
    ):
        """Handle successful payment"""
        self._sync_completed_prepayment_projection(payment)

        # Update order status
        order = payment.order
        if not order:
            return

        # Sync order payment method to the actual method used (e.g. cash → click)
        if order.payment_method != payment.payment_method:
            order.payment_method = payment.payment_method

        self._sync_order_paid_projection(order, payment.status, payment.paid_at)

        # Handle both Enum and string status values
        status_value = order.status.value if hasattr(order.status, "value") else order.status
        if allow_order_confirmation and status_value == "pending":
            from .order_service import OrderService

            order_service = OrderService()
            order_service.update_order_status(order.id, OrderStatus.CONFIRMED)

        # Award purchase AquaCoins if this payment completes an order that was
        # already delivered (e.g. a prepaid payment settled after delivery). The
        # guard self-checks (delivered AND paid) and is idempotent, so for the
        # normal prepaid flow (paid before delivery) this is a no-op — the award
        # then fires on the DELIVERED transition instead.
        from .order_service import OrderService

        OrderService().maybe_award_purchase_points(order, commit=False)

        # Settle the corporate contract for grocery-store customers paying
        # electronically (Click/Payme/Card). Cash & personal-card already settle via
        # CashCollectionService.post_collection; gating to electronic methods here
        # prevents double-settlement (process_cash_payment also calls this hook).
        # Runs in the completion transaction (owner-confirmed transactional+retryable);
        # idempotent per payment_id so a retried webhook re-settles at most once.
        if (
            order.user
            and order.user.is_grocery_store
            and payment.payment_method in {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
            and Decimal(str(payment.amount or 0)) > Decimal("0")
        ):
            from business_app.services.corporate_contract_service import CorporateContractService

            CorporateContractService().settle_order_collection(
                user=order.user,
                order_id=order.id,
                collected_amount=Decimal(str(payment.amount)),
                source=payment.payment_method.value,
                payment_id=payment.id,
                actor_user_id=(payment.provider_data or {}).get("actor_user_id"),
                notes="Electronic payment settled against grocery contract",
            )

        if not trigger_notifications:
            return

        self.dispatch_payment_confirmation(payment)

    def dispatch_payment_confirmation(self, payment: Payment) -> bool:
        """Best-effort dispatch of the customer payment confirmation.

        Email/SMS via Celery for non-Telegram orders; bot push for Telegram
        users (stable request_id keyed on payment.id, so re-dispatch is safe).
        On success stamps provider_data.post_payment.confirmation_enqueued_at —
        the side-effect sweep re-fires unmarked COMPLETED payments. NEVER raises:
        this runs after the money commit and must not abort the webhook chain.
        """
        order = payment.order
        try:
            order_source = getattr(order, "order_source", None) if order is not None else None
            if order_source != "telegram":
                from ..tasks.notification_tasks import send_payment_confirmation_task

                send_payment_confirmation_task.delay(payment.id)

            user = payment.user
            if user and user.telegram_id:
                from business_app.utils.bot_webhook import trigger_bot_webhook

                # BOT-008: stable request_id keyed on payment.id so Celery
                # retries or re-dispatches collapse to one bot notification.
                trigger_bot_webhook(
                    "/internal/payment-success",
                    {
                        "user_id": user.id,
                        "telegram_id": user.telegram_id,
                        "order_id": order.id if order else None,
                        "order_number": order.order_number if order else None,
                        "amount": float(order.total_amount) if order else float(payment.amount),
                        "currency": "UZS",
                    },
                    request_id=f"payment-confirm:{payment.id}",
                )
        except Exception:  # noqa: BLE001
            current_app.logger.exception("Failed to dispatch payment confirmation for payment %s", payment.id)
            return False

        provider_data = dict(payment.provider_data or {})
        post_payment = dict(provider_data.get("post_payment") or {})
        post_payment["confirmation_enqueued_at"] = datetime.now(timezone.utc).isoformat()
        provider_data["post_payment"] = post_payment
        payment.provider_data = provider_data
        return True

    def _process_points_refund(self, payment: Payment, amount: int, reason: str) -> bool:
        """Refund a points-paid order by RETURNING the redeemed points.

        Loyalty is rewards-only: there is no UZS↔points conversion. This path
        only runs for orders paid with loyalty points, so the refund returns the
        points the customer actually spent (``order.loyalty_points_used``),
        proportional to the refunded fraction. The credit is a non-qualifying
        ADJUSTMENT (via ``reverse_earnings``) — no tier multiplier, and it does
        not count toward tier qualification.
        """
        from decimal import Decimal

        from .loyalty_service import LoyaltyService

        order = payment.order
        redeemed = int(getattr(order, "loyalty_points_used", 0) or 0)
        payment_amount = Decimal(str(payment.amount or 0))
        if redeemed <= 0 or payment_amount <= 0:
            return True

        # Proportional to the refunded fraction (full refund -> all points back).
        fraction = min(Decimal("1"), Decimal(str(amount)) / payment_amount)
        points_to_refund = int(redeemed * fraction)
        if points_to_refund <= 0:
            return True

        LoyaltyService().reverse_earnings(
            user_id=payment.user_id,
            order_id=order.id,
            old_points_earned=0,
            new_points_earned=points_to_refund,
            clamp=False,
            description=f"Refund of redeemed points for order #{order.order_number}",
        )

        return True

    def save_card(self, card_data: Dict[str, Any]) -> CreditCard:
        """Delegate to CardTokenService.save_card."""
        return self.card_token_service.save_card(card_data)

    def get_user_cards(self, user_id: int, include_expired: bool = False) -> List[CreditCard]:
        """Delegate to CardTokenService.get_user_cards."""
        return self.card_token_service.get_user_cards(user_id, include_expired=include_expired)

    def get_default_card(self, user_id: int) -> Optional[CreditCard]:
        """Delegate to CardTokenService.get_default_card."""
        return self.card_token_service.get_default_card(user_id)

    def set_default_card(self, card_id: int, user_id: int) -> CreditCard:
        """Delegate to CardTokenService.set_default_card."""
        return self.card_token_service.set_default_card(card_id, user_id)

    def delete_card(self, card_id: int, user_id: int) -> bool:
        """Delegate to CardTokenService.delete_card."""
        return self.card_token_service.delete_card(card_id, user_id)

    def create_card_token(self, number: str, expire: str, save: bool = False) -> Dict[str, Any]:
        """Delegate to CardTokenService.create_card_token."""
        return self.card_token_service.create_card_token(number, expire, save=save)

    def _get_provider_for_brand(self, card_brand: str) -> str:
        """Delegate to CardTokenService.get_provider_for_brand."""
        return self.card_token_service.get_provider_for_brand(card_brand)
