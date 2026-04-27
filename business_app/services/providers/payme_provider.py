"""
Payme JSON-RPC and Subscribe API provider.

Extracted from ``business_app.services.payment_service`` as ARCH-002 PR 2.
Owns:
    - Payme Merchant API webhook handlers (JSON-RPC 2.0 transaction lifecycle).
    - Payme Subscribe API: card tokenization helpers, receipts.create/pay, full
      payment orchestration.
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
from business_app.models.order import Order
from business_app.models.payment import Payment, PaymentTransaction
from business_app.models.user import User
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from business_app.utils.constants import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    PaymeErrors,
    PaymeState,
)
from business_app.utils.exceptions import ConflictError, NotFoundError, PaymentError, ValidationError
from business_app.utils.helpers import to_ms


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
    # Subscribe API — receipts.create / receipts.pay and orchestration
    # ------------------------------------------------------------------

    def create_payme_receipt(self, order: Order, description: str = None) -> Dict[str, Any]:
        """Create a Payme receipt with full fiscal details via receipts.create."""
        if not order:
            raise ValidationError("Order is required")

        if not order.order_items or len(order.order_items) == 0:
            raise ValidationError("Order must have at least one item")

        amount_tiyin = int(float(order.total_amount) * 100)

        items = []
        for item in order.order_items:
            item_data = {
                "title": item.product.name if item.product else f"Product #{item.product_id}",
                "price": int(float(item.unit_price) * 100),
                "count": item.quantity,
                "code": getattr(item.product, "ikpu_code", None) or "02201001001000000",
                "vat_percent": 0,
            }

            if hasattr(item.product, "package_code") and item.product.package_code:
                item_data["package_code"] = item.product.package_code

            items.append(item_data)

        params = {
            "amount": amount_tiyin,
            "account": {"charge_id": str(order.id)},
            "description": description or f"Water delivery order #{order.order_number}",
        }

        detail = {"receipt_type": 0, "items": items}

        delivery_fee = getattr(order, "delivery_fee", None) or getattr(order, "shipping_fee", None)
        if delivery_fee and float(delivery_fee) > 0:
            detail["shipping"] = {"title": "Delivery Fee", "price": int(float(delivery_fee) * 100)}

        params["detail"] = detail

        response = self._payme_request("receipts.create", params)

        if "error" in response:
            error_msg = self._extract_payme_error_message(response["error"])
            current_app.logger.error(f"Payme receipts.create failed: {error_msg}")
            raise PaymentError(f"Failed to create receipt: {error_msg}")

        receipt = response.get("result", {}).get("receipt", {})

        if not receipt.get("_id"):
            raise PaymentError("Payme did not return a receipt ID")

        current_app.logger.info(f"Payme receipt created: {receipt['_id']} for order {order.id}")

        return {
            "receipt_id": receipt["_id"],
            "state": receipt.get("state", 0),
            "amount": receipt.get("amount", amount_tiyin),
            "create_time": receipt.get("create_time"),
        }

    def pay_payme_receipt(self, receipt_id: str, token: str, payer: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute payment on a Payme receipt via receipts.pay."""
        if not receipt_id:
            raise ValidationError("Receipt ID is required")

        if not token:
            raise ValidationError("Card token is required")

        params = {"id": receipt_id, "token": token}

        if payer:
            payer_info = {}
            if payer.get("phone"):
                payer_info["phone"] = payer["phone"]
            if payer.get("email"):
                payer_info["email"] = payer["email"]
            if payer.get("name"):
                payer_info["name"] = payer["name"]
            if payer.get("ip"):
                payer_info["ip"] = payer["ip"]

            if payer_info:
                params["payer"] = payer_info

        response = self._payme_request("receipts.pay", params)

        if "error" in response:
            error_msg = self._extract_payme_error_message(response["error"])
            current_app.logger.error(f"Payme receipts.pay failed: {error_msg}")
            raise PaymentError(f"Payment failed: {error_msg}")

        receipt = response.get("result", {}).get("receipt", {})
        state = receipt.get("state")

        if state != 4:
            state_messages = {
                0: "Receipt awaiting payment",
                1: "Transaction verification in progress",
                2: "Funds deducted, processing",
                3: "Transaction closing in progress",
                5: "Receipt archived",
                6: "Payment on hold - contact Payme support",
                20: "Payment paused for manual review",
                21: "Payment queued for cancellation",
                50: "Payment cancelled",
            }
            msg = state_messages.get(state, f"Unexpected receipt state: {state}")
            current_app.logger.error(f"Payme payment not completed. State: {state} - {msg}")
            raise PaymentError(f"Payment not completed: {msg}")

        current_app.logger.info(f"Payme payment successful. Receipt: {receipt_id}, State: {state}")

        return {
            "success": True,
            "receipt_id": receipt.get("_id", receipt_id),
            "state": state,
            "pay_time": receipt.get("pay_time"),
            "amount": receipt.get("amount"),
            "card": receipt.get("card", {}),
        }

    def process_payme_payment_full(
        self, order_id: int, card_token: str, user_id: int, save_card: bool = True, card_metadata: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Complete Payme payment flow: create receipt and pay."""
        order: Order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        if order.user_id != int(user_id):
            raise ValidationError("Order does not belong to this user")

        if hasattr(order, "is_paid") and order.is_paid:
            raise ValidationError("Order is already paid")

        payment: Payment = Payment(
            order_id=order_id,
            user_id=user_id,
            amount=order.total_amount,
            currency="UZS",
            payment_method=PaymentMethod.PAYME,
            status=PaymentStatus.PENDING,
            description=f"Payment for order #{order.order_number}",
        )
        db.session.add(payment)
        db.session.flush()

        receipt_id = None

        try:
            current_app.logger.info(f"Creating Payme receipt for order {order_id}")
            receipt_result = self.create_payme_receipt(order, description=f"Water delivery order #{order.order_number}")
            receipt_id = receipt_result["receipt_id"]

            self._payment_service._create_transaction(
                payment,
                "receipt_created",
                {"receipt_id": receipt_id, "amount": receipt_result["amount"], "state": receipt_result["state"]},
            )

            current_app.logger.info(f"Paying Payme receipt {receipt_id}")
            user = User.query.get(user_id)
            payer_info = {
                "phone": user.phone if user else None,
                "email": user.email if user else None,
                "name": getattr(user, "full_name", None) if user else None,
            }
            try:
                payer_info["ip"] = request.remote_addr
            except RuntimeError:
                pass

            pay_result = self.pay_payme_receipt(receipt_id, card_token, payer_info)

            payment.status = PaymentStatus.COMPLETED
            payment.paid_at = datetime.now(timezone.utc)
            payment.provider_transaction_id = receipt_id
            payment.provider_data = {
                "receipt_id": receipt_id,
                "state": pay_result["state"],
                "pay_time": pay_result["pay_time"],
                "card_last_four": pay_result["card"].get("number", "")[-4:] if pay_result.get("card") else None,
            }

            self._payment_service._create_transaction(
                payment,
                "payment_completed",
                {"receipt_id": receipt_id, "pay_time": pay_result["pay_time"], "state": pay_result["state"]},
            )

            self._payment_service._handle_successful_payment(payment)

            if save_card and card_metadata:
                self._payment_service._save_or_update_verified_card(user_id, card_token, card_metadata)

            db.session.commit()

            audit_logger.log_event(
                event_type=AuditEventType.PAYMENT_PROCESSED,
                action="payme_payment_completed",
                severity=AuditSeverity.MEDIUM,
                resource_type="payment",
                resource_id=str(payment.id),
                description=f"Payme payment completed for order {order_id}",
                additional_data={
                    "order_id": order_id,
                    "amount": float(order.total_amount),
                    "receipt_id": receipt_id,
                    "user_id": user_id,
                },
            )

            return {
                "success": True,
                "payment_id": payment.id,
                "order_id": order_id,
                "receipt_id": receipt_id,
                "amount": float(order.total_amount),
                "redirect_url": f"/my-orders?order_id={order_id}&payment=success",
            }

        except Exception as e:
            current_app.logger.exception("Payme payment failed for order %s", order_id)
            payment.status = PaymentStatus.FAILED
            payment.failure_reason = str(e)

            if receipt_id:
                payment.provider_transaction_id = receipt_id

            db.session.commit()

            audit_logger.log_event(
                event_type=AuditEventType.PAYMENT_FAILED,
                action="payme_payment_failed",
                severity=AuditSeverity.HIGH,
                resource_type="payment",
                resource_id=str(payment.id),
                description=f"Payme payment failed for order {order_id}: {str(e)}",
                additional_data={"order_id": order_id, "error": str(e), "receipt_id": receipt_id, "user_id": user_id},
            )

            raise

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

    def check_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CheckPerformTransaction."""
        account = params.get("account", {})
        order_id = account.get("order_id")

        amount_tiyin = params.get("amount")

        if not order_id:
            return {"error": {"code": PaymeErrors.ORDER_NOT_FOUND, "message": "Order ID not provided"}}

        order = Order.query.get(order_id)
        if not order:
            return {"error": {"code": PaymeErrors.ORDER_NOT_FOUND, "message": "Order not found"}}

        if int(order.total_amount * 100) != amount_tiyin:
            return {"error": {"code": PaymeErrors.INVALID_AMOUNT, "message": "Incorrect amount"}}

        if order.is_paid:
            return {"error": {"code": PaymeErrors.ORDER_ALREADY_PAID, "message": "Order already paid"}}

        if order.status == OrderStatus.CANCELLED:
            return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Order cancelled"}}

        return {"result": {"allow": True}}

    def create_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CreateTransaction."""
        account = params.get("account", {})
        order_id = account.get("order_id")
        payme_trans_id = params.get("id")
        time_ms = params.get("time")
        params.get("amount")

        transaction = PaymentTransaction.query.filter_by(provider_transaction_id=payme_trans_id).first()

        if transaction:
            if transaction.status != "pending":
                return {
                    "error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction already processed"}
                }

            create_time_ms = to_ms(transaction.created_at)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            timeout_ms = current_app.config.get("PAYME_TIMEOUT_MS", 43200000)

            if (now_ms - create_time_ms) > timeout_ms:
                transaction.status = "cancelled"
                transaction.failure_reason = "Payme timeout"
                transaction.processed_at = datetime.now(timezone.utc)
                db.session.commit()

                return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction timed out"}}

            return {
                "result": {
                    "create_time": create_time_ms,
                    "transaction": str(transaction.id),
                    "state": PaymeState.CREATED.value,
                }
            }

        existing_pending = (
            PaymentTransaction.query.join(Payment)
            .filter(
                Payment.order_id == order_id,
                Payment.payment_method == PaymentMethod.PAYME,
                PaymentTransaction.status == "pending",
            )
            .first()
        )

        if existing_pending:
            return {
                "error": {"code": PaymeErrors.ORDER_HAS_PENDING_PAYMENT, "message": "Order has pending transaction"}
            }

        check_result = self.check_perform_transaction(params)
        if "error" in check_result:
            return check_result

        order = Order.query.get(order_id)

        payment = Payment.query.filter_by(order_id=order_id, payment_method=PaymentMethod.PAYME).first()
        if not payment:
            payment = self._payment_service.create_payment(
                **{
                    "order_id": order_id,
                    "amount": order.total_amount,
                    "payment_method": PaymentMethod.PAYME,
                    "user_id": order.user_id,
                    "currency": "UZS",
                }
            )

        payme_create_time = (
            datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc) if time_ms else datetime.now(timezone.utc)
        )

        new_transaction = PaymentTransaction(
            payment_id=payment.id,
            transaction_type="charge",
            amount=order.total_amount,
            currency="UZS",
            status="pending",
            provider_transaction_id=payme_trans_id,
            provider_response=params,
            created_at=payme_create_time,
        )
        db.session.add(new_transaction)
        db.session.commit()

        return {
            "result": {
                "create_time": time_ms if time_ms else to_ms(new_transaction.created_at),
                "transaction": str(new_transaction.id),
                "state": PaymeState.CREATED.value,
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

        if transaction.status == "pending":
            create_time_ms = to_ms(transaction.created_at)
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

            timeout_ms = current_app.config.get("PAYME_TIMEOUT_MS", 43200000)

            if (now_ms - create_time_ms) > timeout_ms:
                transaction.status = "cancelled"
                transaction.failure_reason = "Payme timeout during perform"
                db.session.commit()
                return {"error": {"code": PaymeErrors.OPERATION_NOT_ALLOWED, "message": "Transaction timed out"}}

            payment: Payment = transaction.payment

            if payment.status == PaymentStatus.COMPLETED:
                pass
            else:
                payment.status = PaymentStatus.COMPLETED
                payment.paid_at = datetime.now(timezone.utc)
                payment.provider_transaction_id = payme_trans_id

                self._payment_service._handle_successful_payment(payment)

            transaction.status = "completed"
            transaction.processed_at = datetime.now(timezone.utc)
            db.session.commit()

            return {
                "result": {
                    "transaction": str(transaction.id),
                    "perform_time": to_ms(transaction.processed_at),
                    "state": PaymeState.COMPLETED.value,
                }
            }

        if transaction.status == "completed":
            return {
                "result": {
                    "transaction": str(transaction.id),
                    "perform_time": to_ms(transaction.processed_at),
                    "state": PaymeState.COMPLETED.value,
                }
            }

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
