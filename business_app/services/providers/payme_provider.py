"""
Payme JSON-RPC and Subscribe API provider.

Extracted from ``business_app.services.payment_service`` as ARCH-002 PR 2.
Owns:
    - Payme Merchant API webhook handlers (JSON-RPC 2.0 transaction lifecycle).
    - Payme Subscribe API request helpers shared with card-token code.
    - Refusal of every new Payme payment (spec 2026-10-27 §4.2); transactions
      opened earlier still complete and refund.
    - Payme payment-link (redirect) generation.
    - Payme webhook Basic-auth credential verification.

PaymentService keeps thin delegates so the public API and the small number
of private helpers shared with card-token code (``_payme_request``,
``_extract_payme_error_message``) remain callable on PaymentService. Those
helper delegates go away in PR 3 when CardTokenService moves out.
"""

from __future__ import annotations

import base64
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import requests
from flask import current_app, request

from business_app import db
from business_app.models.payment import Payment, PaymentTransaction
from business_app.utils.constants import (
    PaymeErrors,
    PaymeState,
)
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.helpers import to_ms

# The transaction type CreateTransaction records a Payme Merchant API charge as; it stays "pending" until
# PerformTransaction or CancelTransaction (or Payme's window) closes it.
PAYME_CHARGE = "charge"


def payme_transaction_timed_out(transaction: PaymentTransaction) -> bool:
    """Has Payme's window (`PAYME_TIMEOUT_MS` from CreateTransaction) closed on this transaction? Past it, a
    PerformTransaction can no longer complete it. The one expression of that rule: CreateTransaction,
    PerformTransaction and `payme_checkout_in_progress` all read it."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return (now_ms - to_ms(transaction.created_at)) > current_app.config.get("PAYME_TIMEOUT_MS", 43200000)


def payme_checkout_in_progress(payment) -> bool:
    """Can a Payme PerformTransaction still capture this payment? It is a Payme payment with a charge that
    CreateTransaction opened and that neither Payme's window nor a cancel has closed. Moving such a payment to
    another rail would let Payme capture a second time (spec 2026-10-27 §4.3, owner call 13)."""
    if payment is None or payment.id is None or payment.payment_method != PaymentMethod.PAYME:
        return False
    open_charges = PaymentTransaction.query.filter_by(
        payment_id=payment.id, transaction_type=PAYME_CHARGE, status="pending"
    ).all()
    return any(not payme_transaction_timed_out(charge) for charge in open_charges)


class PaymeProvider:
    """Payme Merchant API + Subscribe API integration."""

    def __init__(self, *, payment_service):
        self._payment_service = payment_service
        self.payme_merchant_id = current_app.config.get("PAYME_MERCHANT_ID")
        self.payme_merchant_id_with_billing = current_app.config.get("PAYME_MERCHANT_ID_WITH_BILLING")
        self.payme_secret_key = current_app.config.get("PAYME_SECRET_KEY")
        self.payme_secret_key_with_billing = current_app.config.get("PAYME_SECRET_KEY_WITH_BILLING")
        self.payme_endpoint = current_app.config.get("PAYME_ENDPOINT_URL")
        self.payme_test_mode = current_app.config.get("PAYME_TEST_MODE", True)

    # ------------------------------------------------------------------
    # Low-level JSON-RPC client
    # ------------------------------------------------------------------

    def _payme_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Send request to Payme Subscribe API."""
        try:
            current_app.logger.debug(f"Payme: method: {method}, url: {self.payme_endpoint}")
            headers = {
                "X-Auth": (
                    f"{self.payme_merchant_id}:{self.payme_secret_key}"
                    if method.startswith("receipts.")
                    else self.payme_merchant_id
                ),
                "Content-Type": "application/json",
            }

            payload = {"method": method, "params": params, "id": random.randint(1, 1000000), "jsonrpc": "2.0"}

            response = requests.post(self.payme_endpoint, json=payload, headers=headers, timeout=30)

            return response.json()
        except Exception as e:
            current_app.logger.exception("Payme API request error (%s)", method)
            return {"error": {"code": -1, "message": str(e)}}

    @staticmethod
    def _extract_payme_error_message(error: Dict) -> str:
        """Extract human-readable error message from Payme error response."""
        message = error.get("message", "Unknown error")
        if isinstance(message, dict):
            return message.get("en") or message.get("ru") or message.get("uz") or str(message)
        return str(message)

    # ------------------------------------------------------------------
    # Payment link (redirect method)
    # ------------------------------------------------------------------

    def create_payme_link(self, payment: Payment) -> Dict[str, str]:
        """Create Payme payment link (Redirect Method)."""
        try:
            from business_app.utils.helpers import get_current_language

            lang = get_current_language() or "en"
        except ImportError:
            lang = "en"

        bot_username = current_app.config.get("TELEGRAM_BOT_USERNAME", "BlueStreamWaterBot")
        return_url = f"https://t.me/{bot_username}"

        amount_tiyin = int(payment.amount * 100)

        params = f"m={self.payme_merchant_id_with_billing};ac.order_id={payment.order_id};a={amount_tiyin};l={lang};c={return_url}"  # noqa: E501
        encoded_params = base64.b64encode(params.encode("utf-8")).decode("utf-8")

        base_url = self.payme_endpoint.replace("/api", "")
        if base_url.endswith("/"):
            base_url = base_url[:-1]

        payment_url = f"{base_url}/{encoded_params}"

        payment.payment_link = payment_url
        payment.payment_link_expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        payment.callback_url = return_url
        db.session.commit()

        return {
            "payment_url": payment_url,
            "reference": payment.payment_id,
            "expires_at": payment.payment_link_expires_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Webhook signature (Basic auth)
    # ------------------------------------------------------------------

    def verify_payme_signature(self, data: Dict[str, Any]) -> bool:
        """Verify Payme webhook signature (Basic auth header)."""
        if not self.payme_secret_key_with_billing:
            current_app.logger.error("Payme secret key not configured")
            return False

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            current_app.logger.warning("Missing or invalid Authorization header for Payme webhook")
            return False

        try:
            encoded_credentials = auth_header[6:]
            decoded_credentials = base64.b64decode(encoded_credentials).decode("utf-8")
            username, password = decoded_credentials.split(":", 1)

            expected_username = "Paycom"
            expected_password = self.payme_secret_key_with_billing

            if username != expected_username or password != expected_password:
                current_app.logger.warning(
                    f"Invalid Payme webhook credentials. Username: {username}, Maxfiy_soz: {password}, Expected Username: {expected_username}, Expected Maxfiy_soz: {expected_password}"  # noqa: E501
                )
                return False

            return True

        except Exception:
            current_app.logger.exception("Failed to verify Payme signature")
            return False

    # ------------------------------------------------------------------
    # Merchant API — JSON-RPC 2.0 transaction handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _new_payments_refused() -> Dict[str, Any]:
        """The one answer for a Payme payment that would start now. Customers pay online through Click only
        (spec 2026-10-27 §4.2, §17 Q8); a Payme transaction opened before that still completes and refunds."""
        return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "New Payme payments are not accepted"}}

    def check_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CheckPerformTransaction: no new Payme payment is accepted, for any order."""
        return self._new_payments_refused()

    def create_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CreateTransaction. Payme replays it on a timeout, so a transaction opened before deploy still
        answers; a new id is refused."""
        payme_trans_id = params.get("id")

        transaction = PaymentTransaction.query.filter_by(provider_transaction_id=payme_trans_id).first()

        if transaction:
            if transaction.status != "pending":
                return {
                    "error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction already processed"}
                }

            if payme_transaction_timed_out(transaction):
                transaction.status = "cancelled"
                transaction.failure_reason = "Payme timeout"
                transaction.processed_at = datetime.now(timezone.utc)
                db.session.commit()

                return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction timed out"}}

            return {
                "result": {
                    "create_time": to_ms(transaction.created_at),
                    "transaction": str(transaction.id),
                    "state": PaymeState.CREATED.value,
                }
            }

        return self._new_payments_refused()

    @staticmethod
    def _performed_answer(transaction: PaymentTransaction) -> Dict[str, Any]:
        """PerformTransaction's success answer for a completed charge. Every Perform for the same id, first or
        repeated, answers this, with the perform_time recorded when the charge completed."""
        return {
            "result": {
                "transaction": str(transaction.id),
                "perform_time": to_ms(transaction.processed_at),
                "state": PaymeState.COMPLETED.value,
            }
        }

    def perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme PerformTransaction."""
        payme_trans_id = params.get("id")

        transaction: PaymentTransaction = PaymentTransaction.query.filter_by(
            provider_transaction_id=payme_trans_id
        ).first()
        if not transaction:
            return {"error": {"code": PaymeErrors.TRANSACTION_NOT_FOUND, "message": "Transaction not found"}}

        payment = None
        if transaction.status == "pending":
            # Read under the row lock every other payment writer takes first (Click PREPARE/COMPLETE, the
            # customer's Pay tap), so a rail move cannot land between this check and the capture. Then re-read the
            # charge under it: a duplicate Perform for this id that held the lock first may have closed it, and
            # writing from the unlocked read above would overwrite that answer.
            payment = Payment.query.filter_by(id=transaction.payment_id).with_for_update().populate_existing().one()
            transaction = (
                PaymentTransaction.query.filter_by(id=transaction.id).with_for_update().populate_existing().one()
            )

        if transaction.status == "pending":
            if payme_transaction_timed_out(transaction):
                transaction.status = "cancelled"
                transaction.failure_reason = "Payme timeout during perform"
                db.session.commit()
                return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction timed out"}}

            if (
                payment.payment_method == PaymentMethod.PAYME
                and payment.status == PaymentStatus.COMPLETED
                and payment.provider_transaction_id == payme_trans_id
            ):
                # This charge already settled the payment, and an earlier Perform for it failed after that commit
                # (a side effect of `_handle_successful_payment` raised once `update_order_status` had committed).
                # Payme's retry is that same Perform: finish the charge and answer success, so Payme captures the
                # money the order already counts as paid.
                transaction.status = "completed"
                transaction.processed_at = datetime.now(timezone.utc)
                db.session.commit()
                return self._performed_answer(transaction)

            if payment.payment_method != PaymentMethod.PAYME or payment.status == PaymentStatus.COMPLETED:
                # The payment left Payme while this charge waited (the customer paid it through Click: spec
                # 2026-10-27 §4.3), or something other than this charge settled it. Capturing now would take the
                # money twice, and on a Click row it would settle a fiscalized rail with Payme money. Cancel the
                # charge instead, so Payme releases the payer's hold; CheckTransaction and CancelTransaction then
                # answer "cancelled".
                transaction.status = "cancelled"
                transaction.failure_reason = "Payme perform refused: the payment is no longer open on Payme"
                db.session.commit()
                return {
                    "error": {
                        "code": PaymeErrors.OPERATION_NOT_ALLOWED,
                        "message": "The payment is no longer open on Payme",
                    }
                }

            payment.status = PaymentStatus.COMPLETED
            payment.paid_at = datetime.now(timezone.utc)
            payment.provider_transaction_id = payme_trans_id

            self._payment_service._handle_successful_payment(payment)

            transaction.status = "completed"
            transaction.processed_at = datetime.now(timezone.utc)
            db.session.commit()

            return self._performed_answer(transaction)

        if transaction.status == "completed":
            return self._performed_answer(transaction)

        return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction cancelled"}}

    def cancel_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CancelTransaction."""
        payme_trans_id = params.get("id")
        reason = params.get("reason")

        transaction: PaymentTransaction = PaymentTransaction.query.filter_by(
            provider_transaction_id=payme_trans_id
        ).first()
        if not transaction:
            return {"error": {"code": PaymeErrors.TRANSACTION_NOT_FOUND, "message": "Transaction not found"}}

        if transaction.status == "pending":
            transaction.status = "cancelled"
            transaction.failure_reason = f"Payme Cancel: Reason {reason}"
            db.session.commit()

            return {
                "result": {
                    "transaction": str(transaction.id),
                    "cancel_time": to_ms(transaction.updated_at),
                    "state": PaymeState.CANCELLED.value,
                }
            }

        if transaction.status == "completed":
            payment: Payment = transaction.payment
            if payment.order.status in [OrderStatus.DELIVERED, OrderStatus.OUT_FOR_DELIVERY]:
                return {
                    "error": {
                        "code": PaymeErrors.UNABLE_TO_CANCEL,
                        "message": "Order delivered or being delivered, cannot cancel",
                    }
                }
            else:
                from business_app.services.order_service import OrderService

                try:
                    OrderService().cancel_order(
                        payment.order.id,
                        reason=f"Payme Cancel: {reason}",
                        process_payment_refund=False,
                    )
                except (ConflictError, ValidationError) as exc:
                    return {
                        "error": {
                            "code": PaymeErrors.UNABLE_TO_CANCEL,
                            "message": str(exc),
                        }
                    }

            if not self._payment_service.process_refund(payment.id, payment.amount, f"Payme Cancel: {reason}"):
                return {"error": {"code": PaymeErrors.UNABLE_TO_CANCEL, "message": "Refund failed"}}

            transaction.status = "refunded"
            db.session.commit()

            return {
                "result": {
                    "transaction": str(transaction.id),
                    "cancel_time": to_ms(transaction.updated_at),
                    "state": PaymeState.REFUNDED.value,
                }
            }

        if transaction.status in ["cancelled", "refunded"]:
            return {
                "result": {
                    "transaction": str(transaction.id),
                    "cancel_time": to_ms(transaction.updated_at),
                    "state": (
                        PaymeState.CANCELLED.value if transaction.status == "cancelled" else PaymeState.REFUNDED.value
                    ),
                }
            }

        return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Unknown state"}}

    def check_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CheckTransaction."""
        payme_trans_id = params.get("id")
        transaction: PaymentTransaction = PaymentTransaction.query.filter_by(
            provider_transaction_id=payme_trans_id
        ).first()

        if not transaction:
            return {"error": {"code": PaymeErrors.TRANSACTION_NOT_FOUND, "message": "Transaction not found"}}

        state = 0
        create_time = to_ms(transaction.created_at)
        perform_time = 0
        cancel_time = 0
        reason = None

        if transaction.status == "pending":
            state = PaymeState.CREATED.value
        elif transaction.status == "completed":
            state = PaymeState.COMPLETED.value
            perform_time = to_ms(transaction.processed_at)
        elif transaction.status == "cancelled":
            state = PaymeState.CANCELLED.value
            cancel_time = to_ms(transaction.updated_at)
            perform_time = to_ms(transaction.processed_at) if transaction.processed_at else 0
            reason_str = transaction.failure_reason.split("Reason")[-1].strip() if transaction.failure_reason else None
            reason = int(reason_str) if reason_str and reason_str.isdigit() else 5
        elif transaction.status == "refunded":
            state = PaymeState.REFUNDED.value
            cancel_time = to_ms(transaction.updated_at)
            perform_time = to_ms(transaction.processed_at) if transaction.processed_at else 0
            reason_str = transaction.failure_reason.split("Reason")[-1].strip() if transaction.failure_reason else None
            reason = int(reason_str) if reason_str and reason_str.isdigit() else 5

        return {
            "result": {
                "create_time": create_time,
                "perform_time": perform_time,
                "cancel_time": cancel_time,
                "transaction": str(transaction.id),
                "state": state,
                "reason": reason,
            }
        }

    def get_statement(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme GetStatement — returns transactions for a time window."""
        from_time_ms = params.get("from")
        to_time_ms = params.get("to")

        if from_time_ms is None or to_time_ms is None:
            return {
                "error": {"code": PaymeErrors.JSON_VALIDATION_ERROR, "message": "from and to parameters are required"}
            }

        try:
            from_time_ms = int(from_time_ms)
            to_time_ms = int(to_time_ms)
        except (ValueError, TypeError):
            return {
                "error": {"code": PaymeErrors.JSON_VALIDATION_ERROR, "message": "from and to must be valid timestamps"}
            }

        from_dt = datetime.fromtimestamp(from_time_ms / 1000, tz=timezone.utc)
        to_dt = datetime.fromtimestamp(to_time_ms / 1000, tz=timezone.utc)

        transactions = (
            PaymentTransaction.query.join(Payment)
            .filter(
                Payment.payment_method == PaymentMethod.PAYME,
                PaymentTransaction.provider_transaction_id.isnot(None),
                PaymentTransaction.created_at >= from_dt,
                PaymentTransaction.created_at <= to_dt,
            )
            .order_by(PaymentTransaction.created_at.asc())
            .all()
        )

        transaction_list = []
        for tx in transactions:
            if tx.status == "pending":
                state = PaymeState.CREATED.value
            elif tx.status == "completed":
                state = PaymeState.COMPLETED.value
            elif tx.status == "cancelled":
                state = PaymeState.CANCELLED.value
            elif tx.status == "refunded":
                state = PaymeState.REFUNDED.value
            else:
                state = PaymeState.CREATED.value

            create_time_ms = to_ms(tx.created_at) if tx.created_at else 0
            perform_time_ms = to_ms(tx.processed_at) if tx.processed_at else 0
            cancel_time_ms = to_ms(tx.updated_at) if tx.status in ["cancelled", "refunded"] and tx.updated_at else 0

            reason = None
            if tx.status in ["cancelled", "refunded"]:
                if tx.failure_reason and "Reason" in tx.failure_reason:
                    try:
                        reason_str = tx.failure_reason.split("Reason")[-1].strip()
                        reason = int(reason_str)
                    except (ValueError, IndexError):
                        reason = 5
                else:
                    reason = 5

            order_id = tx.payment.order_id if tx.payment else None
            account = {"order_id": str(order_id)} if order_id else {}

            tx_data = {
                "id": tx.provider_transaction_id,
                "time": create_time_ms,
                "amount": int(float(tx.amount) * 100),
                "account": account,
                "create_time": create_time_ms,
                "perform_time": perform_time_ms,
                "cancel_time": cancel_time_ms,
                "transaction": str(tx.id),
                "state": state,
                "reason": reason,
            }

            transaction_list.append(tx_data)

        current_app.logger.info(
            f"Payme GetStatement: Returned {len(transaction_list)} transactions "
            f"from {from_dt.isoformat()} to {to_dt.isoformat()}"
        )

        return {"result": {"transactions": transaction_list}}
