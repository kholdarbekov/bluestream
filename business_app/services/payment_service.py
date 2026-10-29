"""
Payment service for the Water Business Platform
Supports Payme, Click, Cash, and Loyalty Points payments
"""

import copy
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Any, Optional, List
from flask import current_app
import redis
from sqlalchemy.orm import joinedload

from business_app.models.order import Order
from business_app.models.payment import Payment, PaymentTransaction, CreditCard
from business_app.utils.exceptions import (
    NotFoundError,
    PaymentError,
    PayRefusedError,
    ProviderUnavailableError,
    StateChangedError,
    ValidationError,
)
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
from shared.payment_methods import (
    CUSTOMER_SELECTABLE_METHODS,
    ORDER_PAYMENT_METHODS,
    PAYMENT_METHOD_CATALOG,
    canonical_rail,
)
from business_app.utils.helpers import generate_random_string
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from business_app.utils.payment_projection import (
    FISCALIZED_RAILS,
    get_payment_projection,
    is_online_rail,
    is_settled_prepayment,
    order_is_dead,
    order_is_resolved,
    pay_refusal,
)
from business_app.utils.translations import get_translation
from business_app.utils.state_validators import assert_cash_payment_collector
from business_app.services.card_token_service import CardTokenService
from business_app.services.customer_links import channel_for_order
from business_app.services.customer_order_actions import money_value
from business_app.services.product_fiscal_service import ProductFiscalService
from business_app.services.providers.payme_provider import PaymeProvider, payme_checkout_in_progress
from business_app.services.providers.webhook_signature import WebhookSignatureVerifier
from business_app import db


def _refuse_customer_rail_move(order: Order, payment: Optional[Payment], requested_rail, current_rails) -> None:
    """Raise `ValidationError` unless moving `order` onto `requested_rail` is the one rail move a customer write may
    make: an online order (Click or legacy Payme on both columns) onto Click, with nothing collected, delivered or
    not, but not dead (spec 2026-10-27 §4.3, D2, D4, D5). Every `create_payment` caller is a customer route or
    Payme CreateTransaction; staff move rails with their own tools (`OrderPaymentMethodEditService`, and the
    door's `CashCollectionService.convert_electronic_order_to_cash`). The first check that matches refuses:

    1. any other move, cash -> Click and online -> cash among them: RAIL_MOVE_NOT_ALLOWED. The online rails are
       `payment_projection.is_online_rail`'s, the same ones `pay_refusal` publishes Pay for;
    2. money already collected on the row;
    3. a dead order (cancelled, returned). `retry-payment` never gets here (`pay_refusal` refuses it first), but
       `POST /payments/create` checks only `is_paid`, so this is that route's only lifecycle guard;
    4. a marking-code pool too short for the order on Click: MARKING_CODES_POOL_SHORT. Payme draws no code,
       so the move makes the order need them; refused here rather than at Click's PREPARE, which answers the
       webhook's generic 503 and shows the customer nothing. Its sentence names no other way to pay: the
       customer cannot move the order to cash (D5), and it is usually delivered already.

    The move is not re-priced: Click is charged the online price the order was shown on its legacy rail."""
    if requested_rail is not PaymentMethod.CLICK or not all(is_online_rail(rail) for rail in current_rails):
        raise ValidationError(
            get_translation("api.payments.error.rail_move_not_allowed"), error_code="RAIL_MOVE_NOT_ALLOWED"
        )
    if payment is not None and Decimal(str(payment.amount_collected or 0)) > Decimal("0.00"):
        raise ValidationError("Payment method cannot be changed after cash has been collected")
    if order_is_dead(order):
        raise ValidationError("Payment method cannot be changed once the order has left delivery")
    covered, short_product = ProductFiscalService().pool_covers_order(order, PaymentMethod.CLICK)
    if not covered:
        # get_translation applies **kwargs itself and swallows a bad placeholder.
        raise ValidationError(
            get_translation("api.payments.error.marking_codes_pool_short", product=short_product),
            error_code="MARKING_CODES_POOL_SHORT",
        )


def _click_checkout_in_progress(payment: Optional[Payment]) -> bool:
    """Has Click PREPARE passed for this payment, its COMPLETE still to come? PREPARE leaves the row PENDING and
    records `provider_data.click.merchant_prepare_id` (`ClickPaymentProviderService.handle_prepare`); nothing
    writes PROCESSING."""
    if payment is None or payment.status != PaymentStatus.PENDING:
        return False
    if payment.payment_method not in {PaymentMethod.CLICK, PaymentMethod.CARD}:
        return False
    return bool(((payment.provider_data or {}).get("click") or {}).get("merchant_prepare_id"))


class PaymentContext(str, Enum):
    """Where a payment method is being offered.

    ORDER carries an item list (or None, before a cart exists); SUBSCRIPTION
    never has line items, so business_account eligibility always falls back
    to the user-level predicate for it.
    """

    ORDER = "order"
    SUBSCRIPTION = "subscription"


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

    @staticmethod
    def _method_is_configured(method_code: str) -> bool:
        """Derive is_active from configured provider credentials.

        The old catalog (business_app/serializers/payment_serializers.py)
        hardcoded ``"is_active": True``, so an install with no merchant ID
        still advertised the provider. Cash needs no provider and is always
        active.
        """
        if method_code == PaymentMethod.CLICK.value:
            return bool(current_app.config.get("CLICK_MERCHANT_ID") and current_app.config.get("CLICK_SERVICE_ID"))
        if method_code == PaymentMethod.PAYME.value:
            return bool(current_app.config.get("PAYME_MERCHANT_ID"))
        return True

    def get_available_payment_methods(
        self,
        user,
        *,
        context: PaymentContext = PaymentContext.ORDER,
        items: Optional[List[Dict[str, Any]]] = None,
        delivery_address_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Which payment methods may THIS user be offered in THIS context.

        Single source of truth for every surface: both bots, web checkout, the
        web subscription constructor, and the admin UI. Payme and
        loyalty_points are excluded by ``CUSTOMER_SELECTABLE_METHODS`` and
        never appear.

        ``delivery_address_id`` is optional: supply the destination address and
        the COD cap's PLACE arm is evaluated too (spec 5.5), so cash disappears
        from the menu exactly when order creation would refuse it. Omitted (or
        an ungrouped address) ⇒ person arm only, unchanged from before.
        """
        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.corporate_contract_service import CorporateContractService

        selectable_codes = {method.value for method in CUSTOMER_SELECTABLE_METHODS}

        available: List[Dict[str, Any]] = []
        for entry in PAYMENT_METHOD_CATALOG:
            code = entry["method"]
            if code not in selectable_codes:
                continue
            if not self._method_is_configured(code):
                continue
            # deepcopy, not {**entry}: a shallow spread would share the nested
            # `supported_currencies` list with the module-level catalog, so a
            # caller annotating a returned dict would corrupt it process-wide.
            # shared/payment_methods.py declares the catalog read-only.
            available.append({**copy.deepcopy(entry), "is_active": True})

        cod_context = CashCollectionService().get_cod_restriction_context(
            user.id, delivery_address_id=delivery_address_id
        )
        if cod_context.get("cod_restricted"):
            available = [entry for entry in available if entry["method"] != PaymentMethod.CASH.value]

        contract_service = CorporateContractService()
        if items:
            # Item-level mirror of validate_business_account_order: if we
            # offer it, creating the order will succeed.
            business_account_ok = contract_service.order_qualifies_for_business_account(user, items)
        else:
            # No cart yet (menu render) or a context with no line items
            # (e.g. subscriptions). User-level half of the same predicate.
            business_account_ok = contract_service.user_can_use_business_account(user)

        if business_account_ok:
            available.append(
                {
                    "method": PaymentMethod.BUSINESS_ACCOUNT.value,
                    "name": PaymentMethod.BUSINESS_ACCOUNT.value,
                    "display_name": "Business Account",
                    "icon_url": None,
                    "description": "Charge the active corporate prepayment balance",
                    "is_active": True,
                    "is_default": True,
                    "supported_currencies": ["UZS"],
                }
            )

        return available

    @staticmethod
    def open_click_payment_window(order: Order, *, now: Optional[datetime] = None) -> None:
        """The order became payable on Click at `now` (the wall clock when None): its payment deadline counts 24
        hours from here (`OrderService.payment_deadline`, spec 2026-10-27 §4.4, §17 Q3). The caller commits.

        The ONLY writer of `orders.online_payable_since`. Called when an order is placed on an online rail
        (`initialize_order_payment`, once), when a customer's Pay moves it onto Click (`create_payment`) and when
        staff move it there (`OrderPaymentMethodEditService._unwind_to_click`)."""
        order.online_payable_since = now or get_utc_now()

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

        # RAIL SSOT. `orders.payment_method` is what every settlement gate reads;
        # `payments.payment_method` is what the allocator reads. Re-purposing the
        # row without moving the order strands the order in a shape no settlement
        # path handles, so move both or neither — and never move a rail that
        # already absorbed money (a later completion would zero amount_collected).
        # THE RAIL IS TWO COLUMNS, so the guard compares BOTH — and compares
        # CANONICAL rails, not raw enum members.
        #
        # Keyed on `payments.payment_method` alone it was wrong twice, and B3
        # routes a customer tap at each:
        #   * FALSE POSITIVE — `PaymentMethod.CARD` is a legacy ALIAS of CLICK
        #     (`shared/payment_methods.py`: "card always normalizes to CLICK; it
        #     is never written again"). A delivered unpaid CARD row is `is_payable`,
        #     so the bot draws Pay, normalizes to click, and the raw comparison
        #     called that a rail move — a rendered button that could only 400.
        #   * FALSE NEGATIVE — with `order.payment_method == CASH` and
        #     `payment.payment_method == CLICK` (the desync commit 6c2951d
        #     addressed) a requested CLICK read as "no move", the lock was
        #     skipped, and the `order.payment_method` write below then moved a
        #     DELIVERED COD debt onto a gateway rail, hiding it from the
        #     receivable ledger: exactly what the lock forbids.
        #
        # Normalisation is `shared.payment_methods`' own, never re-spelled here.
        # A NULL `orders.payment_method` (legacy subscription rows) records no
        # rail to move away from, so it does not vote; nor does a payment row
        # that does not exist yet, so a row-less order's own rail still counts.
        requested_rail = canonical_rail(payment_method)
        current_rails = {
            canonical_rail(method)
            for method in (getattr(payment, "payment_method", None), order.payment_method)
            if method is not None
        }
        if current_rails - {requested_rail}:
            _refuse_customer_rail_move(order, payment, requested_rail, current_rails)

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

        if order.payment_method != payment_method and payment_method in ORDER_PAYMENT_METHODS:
            order.payment_method = payment_method
            if payment_method == PaymentMethod.CLICK:
                self.open_click_payment_window(order)

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
        notify_confirmation: bool = True,
    ) -> Optional[Payment]:
        """Create or finalize the canonical payment record for an order.

        `trigger_notifications=False` sends no payment confirmation; `notify_confirmation=False` sends no generic
        "confirmed" status notice for the confirmation a completed payment triggers. A subscription order passes
        both: its own creation notice tells the customer instead (spec 2026-10-23 §9)."""
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
            if order.online_payable_since is None:
                self.open_click_payment_window(order)
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
                notify_confirmation=notify_confirmation,
            )

        if payment_method == PaymentMethod.BUSINESS_ACCOUNT and payment.consume_marking_codes:
            self._get_payment_fiscalization_service().consume_marking_codes_for_business_account(
                payment,
                actor_user_id=actor_user_id,
            )

        db.session.commit()
        return payment

    def create_payment_link(self, payment_id: int, *, channel: Optional[str] = None) -> Dict[str, str]:
        """
        Create payment link for external payment gateways

        Args:
            payment_id: Payment ID
            channel: where Pay was tapped (`customer_links.PAY_CHANNELS`); Click sends the customer back there. A
                link opened without a tap (the checkout's first link, the admin resend, a notice) returns to the
                channel the order came from (`customer_links.channel_for_order`).

        Returns:
            Dictionary with payment URL and other details
        """
        payment: Payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        if payment.payment_method == PaymentMethod.PAYME:
            return self._create_payme_link(payment)
        elif payment.payment_method in {PaymentMethod.CLICK, PaymentMethod.CARD}:
            return self._create_click_link(payment, channel=channel or channel_for_order(payment.order))
        else:
            raise PaymentError(get_translation("error.payment.unsupported_method"))

    def pay_order_as_customer(
        self, order_id: int, user_id: int, *, channel: str, expected_amount: Decimal
    ) -> Dict[str, Any]:
        """The customer's Pay on one of their orders: a Click link for it (spec 2026-10-27 §4.2, §4.3, §4.5).

        `POST /orders/<id>/retry-payment` is its only caller, so the bot and the website open a payment the same
        way. Each step runs only if the one before it passed:

        1. the order's payment row, then the order, both locked, in the order the Click and Payme webhooks lock them
           (payment first), so a tap racing a webhook waits for it instead of deadlocking on PostgreSQL. Someone
           else's order or a missing one is `NotFoundError`, and its payment row is never locked;
        2. the one Pay rule (`pay_refusal`), the answer every order payload publishes: `PayRefusedError`;
        3. the amount the screen showed (`expected_amount`) against the order total: `StateChangedError`;
        4. a legacy Payme payment whose Payme checkout is still open (CreateTransaction passed, PerformTransaction
           may still come: owner call 13) is not moved: `StateChangedError`. Moving it would let Payme capture a
           second time;
        5. the payment, on Click whatever rail the order is on (a legacy Payme or card order moves to Click). A
           Click payment whose PREPARE has passed and whose COMPLETE has not arrived is left as it is: rebuilding
           it would wipe what PREPARE recorded, and that COMPLETE must still settle the order (Review Focus 3);
        6. its link, returning to `channel`. The link writes only `payment_link`, `payment_link_expires_at`,
           `callback_url` and `provider_data.click.checkout_*`.

        Returns `{"payment", "payment_link", "payment_url"}` and commits. Nothing is written when a check refuses.
        """
        payment = (
            Payment.query.join(Order, Payment.order_id == Order.id)
            .filter(Order.id == order_id, Order.user_id == user_id)
            .with_for_update(of=Payment)
            .populate_existing()
            .first()
        )
        order = Order.query.filter_by(id=order_id, user_id=user_id).with_for_update().populate_existing().first()
        if order is None:
            raise NotFoundError(get_translation("api.orders.not_found"))
        refusal = pay_refusal(order)
        if refusal is not None:
            raise PayRefusedError(refusal)
        if money_value(expected_amount) != money_value(order.total_amount):
            raise StateChangedError()
        if payme_checkout_in_progress(payment):
            raise StateChangedError()

        if not _click_checkout_in_progress(payment):
            payment = self.create_payment(
                order.id,
                PaymentMethod.CLICK,
                amount=order.total_amount,
                description=f"Payment for order #{order.order_number}",
            )
        payment_link = self.create_payment_link(payment.id, channel=channel)
        db.session.commit()
        return {"payment": payment, "payment_link": payment_link, "payment_url": payment_link.get("payment_url")}

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

    def _detect_card_brand(self, masked_number: str) -> str:
        """Delegate to CardTokenService.detect_card_brand."""
        return self.card_token_service.detect_card_brand(masked_number)

    def _save_or_update_verified_card(self, user_id: int, token: str, card_metadata: Dict[str, Any]) -> CreditCard:
        """Delegate to CardTokenService.save_or_update_verified_card."""
        return self.card_token_service.save_or_update_verified_card(user_id, token, card_metadata)

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
        """Bookkeep a refund. NEVER for a card/Click payment.

        ITS ONE SURVIVING PRODUCTION CALLER IS PAYME `CancelTransaction`
        (``payme_provider.py:643``), which is a merchant-agreement obligation the
        gateway INITIATES: Payme tells us the transaction is cancelled and our
        protocol response must report ``state: REFUNDED``. Refusing it would
        breach that agreement, which is why the guard below is rail-gated rather
        than blanket.

        ``LOYALTY_POINTS`` and ``CASH`` remain reachable only because they share
        Payme's bookkeeping branch. Both are effectively dead in practice —
        points are no longer a payment method and
        ``LoyaltyService.cancel_redemption_for_order`` already returns points on
        cancel — so ``_process_points_refund`` is the next branch to remove; it
        is deliberately left alone here.
        """
        payment: Payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation("error.not_found"))

        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError(get_translation("error.payment.cannot_refund"))

        if amount > payment.amount:
            raise ValidationError(get_translation("error.validation.amount_exceeds_total"))

        # THE OWNER'S RULE, ENFORCED AT THE METHOD (ruling 2026-08-24):
        # a card/Click payment is never reversed, because the fiscal receipt
        # cannot be un-submitted and a cancelled payment row holding real money
        # is the payment-vs-fiscalization chaos the rule exists to prevent. The
        # lawful action is to cancel the ORDER; the money then settles as
        # customer prepaid credit
        # (``CashCollectionService.credit_customer_for_dead_order_prepayment``).
        #
        # A RAIL GATE, NOT A CALLER-SIDE CARVE-OUT. A caller-side check leaves
        # the reversal one ``if`` away from firing; this cannot be reached by
        # Payme even in principle, because ``FISCALIZED_RAILS`` excludes PAYME by
        # construction. It turns the rule from currently-unbroken into
        # unbreakable — and the gateway branch it used to guard, along with
        # ``ClickPaymentProviderService.refund_payment``, is deleted outright so
        # there is nothing left for the next caller to find.
        if payment.payment_method in FISCALIZED_RAILS:
            raise ValidationError(get_translation("error.payment.cannot_refund"))

        # Process refund based on payment method
        if payment.payment_method == PaymentMethod.LOYALTY_POINTS:
            success = self._process_points_refund(payment, amount, reason)
        else:
            # Payme (gateway-initiated cancel) and cash (manual, at the door):
            # bookkeeping only, no outbound call of ours.
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
                # B1 — a provider-reported cancel describes ONE abandoned
                # gateway attempt, NOT the end of the payment. Until the ORDER
                # itself has RESOLVED we must neither write a terminal status nor
                # release the codes:
                #
                #  * the status write locks the customer out permanently. The
                #    Phase 4A PREPARE guard (order_is_payable_online) requires
                #    PENDING/PROCESSING, so once CANCELLED lands here the
                #    customer can NEVER pay that link again — on an order they
                #    still owe for, under a policy (Phase 4D) that explicitly
                #    promises the link stays payable through delivery. The
                #    owner's 2026-08-24 ruling says the same: a payment ends
                #    where the ORDER resolves — cash at the door, payment of the
                #    link, or order cancellation. Nothing else.
                #  * the release hands this order's reserved codes back to the
                #    pool while the order still needs them — either to reach the
                #    door, or (case B) to have its receipt issued when the money
                #    finally lands after an unpaid delivery. In prod
                #    (TG_000413_26 / TG_000414_26) another order re-reserved and
                #    used those exact codes 34 minutes later, so this order was
                #    delivered un-fiscalizable and its labels printed on someone
                #    else's tax receipt.
                #
                # Reachable from a CUSTOMER-FACING GET (api/payments.py:454), so
                # before this guard a customer refreshing their own payment page
                # could strip their own unresolved order's marking codes.
                #
                # The predicate is order_is_resolved — "has this ORDER reached
                # its end state", NOT "is this order pre-delivery". A guard
                # written as order_is_live would stop at OUT_FOR_DELIVERY and so
                # still cancel a DELIVERED-but-unpaid order: policy case B, the
                # exact population Phase 4 exists to serve, whose contract is
                # "link stays payable, codes retained". That would be the same
                # permanent lockout one status further on.
                if not order_is_resolved(payment.order):
                    current_app.logger.info(
                        "Gateway reported %s on an unresolved order; leaving payment PENDING and codes reserved",
                        provider_status,
                        extra={
                            "payment_id": payment.id,
                            "order_id": payment.order_id,
                            "gateway_status": provider_status,
                        },
                    )
                    # Deliberately NOT db.session.rollback(). Nothing in this
                    # branch mutates the payment, but the provider call above may
                    # have persisted a freshly-DISCOVERED Click payment id via
                    # check_payment_status_by_mti -> _persist_click_payment_id.
                    # Rolling back would throw that discovery away and force the
                    # next poll to re-derive it. That gain is real only on the
                    # reconcile path, whose trailing commit persists it; on the
                    # customer-facing GET nothing commits and teardown discards
                    # it either way, so there the choice is simply harmless.
                    return payment
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
            # `.replace(day=1)` alone preserves `now`'s hour/minute/second/microsecond,
            # so on the 1st of a month `month_start` lands AFTER any payment made
            # earlier that same day (or even microseconds after one made this instant),
            # silently excluding it from its own month's bucket. Zero the time
            # component explicitly so `month_start` is always the month's first instant.
            month_start = (now.replace(day=1) - timedelta(days=32 * i)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
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
        """Compatibility wrapper used by background verification tasks.

        READ BEFORE ADDING A SECOND CALLER (B2, 2026-08-24).

        ``success`` here means ONLY "the payment is COMPLETED right now". It does
        NOT mean the gateway rejected anything. A perfectly healthy PENDING Click
        payment on a live order returns::

            {"success": False, "error": "Verification failed", "status": "pending"}

        Acting on that boolean as if it were a failure is the root defect behind
        B1 round 3: ``process_payment_verification`` wrote ``FAILED``, ``FAILED``
        fails ``order_is_payable_online``, and the customer's next Click PREPARE
        answered ``-9`` — a permanent lockout, triggered by their own
        ``POST /api/v1/payments/<id>/verify``.

        The discriminator you want is already in this payload: ``status`` carries
        the real state ("pending" / "failed" / "cancelled" / "completed" / ...).
        Branch on that, and gate any terminal write on ``order_is_resolved``
        (business_app/utils/payment_projection.py) exactly as the existing caller
        does — a payment's life ends where its ORDER resolves, nowhere else.

        The two-valued ``success`` and the always-"Verification failed" ``error``
        are kept deliberately: the only caller writes ``error`` into
        ``payment.failure_reason`` and into the ``PaymentTransaction`` audit row,
        so changing either would change persisted data. Pinned by
        tests/unit/test_verify_payment_return_contract.py.
        """
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
    def _create_click_link(self, payment: Payment, *, channel: str) -> Dict[str, str]:
        """Create Click payment link via dedicated provider service."""
        return self._get_click_provider_service().create_payment_link(payment, channel=channel)

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
        notify_confirmation: bool = True,
    ):
        """Handle successful payment. The two notice flags are `initialize_order_payment`'s."""
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
        from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService

        # C14: a held agent order's payment still completes, but the order stays PENDING. Only the
        # order-approval queue (OA2) confirms it. This covers a business-account order paid at
        # creation, a payment-method edit to the business account, and a Click completion.
        if (
            allow_order_confirmation
            and status_value == "pending"
            and not AgentOrderApprovalService.is_awaiting(order.id)
        ):
            from .order_service import OrderService

            order_service = OrderService()
            order_service.update_order_status(order.id, OrderStatus.CONFIRMED, notify=notify_confirmation)

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
