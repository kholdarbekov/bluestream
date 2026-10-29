"""
Subscription service for the Water Business Platform
Handles recurring water delivery subscriptions
"""

import hashlib
import json
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, date, datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Any, FrozenSet, Iterable, Iterator, Optional, Sequence, Set, Tuple
from flask import current_app

from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.user import User, UserAddress
from business_app.models.product import Product
from business_app.models.order import Order
from business_app.models.delivery import DeliveryTimeSlot
from business_app.utils.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    StateChangedError,
    ValidationError,
)
from business_app.utils.local_windows import local_date
from business_app.utils.order_totals import compute_order_total
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from business_app.services import subscription_display, subscription_notices, subscription_schedule
from business_app.services.customer_order_actions import customer_actions, pay_action
from business_app.utils import display_labels
from shared.enums import LIVE_SUBSCRIPTION_STATUSES, SubscriptionStatus, SubscriptionFrequency, PaymentMethod
from business_app.utils.service_factory import get_notification_service
from business_app.utils.translations import get_translation
from shared.i18n_rendering import plural_key
from business_app import db

_CENT = Decimal("0.01")
# One run creates every delivery that is due, normally one. A bound, so no bug can loop forever.
_MAX_ORDERS_PER_RUN = 31

# Who acts (spec 2026-10-26 §5 B1). The admin may edit any status behind its override; the rules are otherwise
# the same for both.
CUSTOMER = "customer"
ADMIN = "admin"
# What each actor is published: the customer has no "Create next order now"; admin has no Pay or Skip route.
CUSTOMER_ACTIONS = ("pay", "skip", "pause", "resume", "cancel", "edit", "remove_item")
ADMIN_ACTIONS = ("pause", "resume", "cancel", "edit", "remove_item", "create_next_order")
# The customer actions that are buttons: each publishes `label` (None while refused).
LABELLED_ACTIONS = ("pay", "skip", "pause", "resume", "cancel")

# Why an action is refused: `allowed_actions.<action>.block_reason`, and the refused write's message.
_REFUSED = {
    "pay": "api.subscriptions.action.pay.nothing_to_pay",
    "skip": "api.subscriptions.action.skip.not_active",
    "pause": "api.subscriptions.action.pause.not_active",
    "resume": "api.subscriptions.action.resume.not_paused",
    "cancel": "api.subscriptions.action.cancel.already_cancelled",
    "edit": "api.subscriptions.action.edit.closed",
    "create_next_order": "api.subscriptions.action.create_next_order.not_active",
}
_LAST_ITEM = "api.subscriptions.action.remove_item.last_item"
_METHOD_NOT_OFFERED = "api.subscriptions.payment.method_not_offered"
# An address change whose new address does not offer the method asks for one (spec 2026-10-27 §7, B8 re-pick).
_METHOD_REQUIRED = "api.subscriptions.address.payment_method_required"
# The card's warning that the subscription's method is no longer offered at its address (B8).
_METHOD_WARNING = "api.subscriptions.page.payment.not_offered"
# The reason code of an order whose move after an address change failed for a reason that is not a refusal (a
# Postgres deadlock abort, a fail-closed guard): the order stays where it was, and the answer says so.
_MOVE_FAILED = "MOVE_FAILED"

# The state of a delivery the page shows (spec 2026-10-26 §5 B2, B3, §12): `upcoming_dates[].state` and
# `upcoming_delivery.state`.
PLACED = "placed"  # its order is created
PLANNED = "planned"  # scheduled; its order is not created yet
SKIPPED = "skipped"  # skipped by the customer or the system
ON_THE_WAY = "on_the_way"  # the driver has its order
PAID_KEPT = "paid_kept"  # a paused or cancelled subscription's order that is still delivered
# The waterline: the next deliveries the page draws.
UPCOMING_DATES_SHOWN = 4


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _order_ref(order: Order) -> Dict[str, Any]:
    """An order as an action's consequences and its response name it, with the labels the page words it in."""
    return {
        "id": order.id,
        "order_number": order.order_number,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        **subscription_display.order_labels(order),
    }


def _kept_ref(order: Order, code: str) -> Dict[str, Any]:
    return {**_order_ref(order), "reason_code": code}


@dataclass(frozen=True)
class ActionCheck:
    """One action's answer (spec 2026-10-26 §5 B1): whether it is allowed now, the translation key of why
    not, and what it will do (its consequences: the orders it cancels and keeps, the dates it gives)."""

    allowed: bool
    block_reason_key: Optional[str] = None
    consequences: Dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class _SkipPlan:
    """What a skip does now (§9): the date it skips, the unpaid upcoming order it cancels or the paid one it
    keeps, the clock after it and the upcoming delivery after it."""

    date: date
    cancel: Tuple[Order, ...]
    kept: Tuple[Tuple[Order, str], ...]
    next_delivery: Optional[date]
    next_after: Optional[date]


@dataclass(frozen=True)
class _AddressPlan:
    """What a customer's address change does (spec 2026-10-27 §7, §17 Q6), read before anything is written: the
    orders already placed that move to `address` too, the ones that stay where they are with
    `OrderAddressChangeService.customer_move_refusal`'s code, and whether the method (the one named, else the
    subscription's) is on the new address's payment menu, `payment_methods`. An order already at `address` is in
    neither list."""

    address: UserAddress
    moves: Tuple[Order, ...]
    stays: Tuple[Tuple[Order, str], ...]
    payment_method_offered: bool
    payment_methods: List[Dict[str, Any]]


@dataclass
class _ScheduleDraft:
    """A subscription's schedule, detached from the session: what a preview applies a change to, so the
    preview runs the save's own rule and still writes nothing (spec 2026-10-26 §5 B4)."""

    id: int
    status: SubscriptionStatus
    frequency: SubscriptionFrequency
    delivery_day_of_week: Optional[int]
    delivery_day_of_month: Optional[int]
    delivery_time_slot_id: Optional[int]
    delivery_time_slot: Any
    next_delivery_date: Optional[date]
    last_delivery_date: Optional[date]

    @classmethod
    def of(cls, subscription: Subscription) -> "_ScheduleDraft":
        return cls(
            id=subscription.id,
            status=subscription.status,
            frequency=subscription.frequency,
            delivery_day_of_week=subscription.delivery_day_of_week,
            delivery_day_of_month=subscription.delivery_day_of_month,
            delivery_time_slot_id=subscription.delivery_time_slot_id,
            delivery_time_slot=subscription.delivery_time_slot,
            next_delivery_date=subscription.next_delivery_date,
            last_delivery_date=subscription.last_delivery_date,
        )


class SubscriptionService:
    """Service for managing subscriptions"""

    # Fields an admin may set only behind an explicit override flag.
    _ADMIN_OVERRIDE_FIELDS = {
        "billing_amount": "manual_billing_amount",
        "next_delivery_date": "manual_billing_dates",
    }

    # A change to any of these moves the next delivery by the rule (spec 2026-10-23 §4.6, §9).
    _SCHEDULE_FIELDS = ("frequency", "delivery_day_of_week", "delivery_day_of_month", "delivery_time_slot_id")

    # Never editable through admin_update_subscription: status transitions go
    # through the lifecycle actions (pause/resume/cancel) and identity columns
    # are immutable. Enforced at the service boundary (defense-in-depth — the
    # serializer omits these too, but the service must own its invariant).
    _ADMIN_PROTECTED_FIELDS = {"status", "id", "user_id", "subscription_number"}

    @staticmethod
    def user_has_subscription_using_address(user_id: int, address_id: int) -> bool:
        """Check whether ``user_id`` has any subscription bound to ``address_id``.

        Used by the addresses API to block deletion of an address still
        referenced by a subscription. Encapsulating the query here keeps
        ``business_app/api/addresses.py`` free of direct ``Subscription``
        model access (boundary rule enforced by
        ``test_api_boundary_coupling_scores_do_not_regress``).
        """
        return (
            Subscription.query.filter_by(
                user_id=user_id,
                delivery_address_id=address_id,
            ).first()
            is not None
        )

    def __init__(self):
        self.billing_day = current_app.config.get("SUBSCRIPTION_BILLING_DAY", 1)

    @property
    def max_items(self) -> int:
        """MAX_SUBSCRIPTION_ITEMS, read when it is needed (the instance is cached for the request)."""
        return current_app.config.get("MAX_SUBSCRIPTION_ITEMS", 10)

    @staticmethod
    def _validated_payment_method(raw) -> PaymentMethod:
        """Normalize and enforce the customer-selectable set.

        Previously ``PaymentMethod(raw)`` accepted all six enum members, so a
        subscription could be created with ``loyalty_points`` or ``payme`` —
        methods ``create_order`` rejects at billing time, so an unvalidated
        subscription would fail every single billing cycle forever.
        """
        from shared.payment_methods import (
            UnknownPaymentMethodError,
            UnsupportedPaymentMethodError,
            assert_customer_selectable,
            normalize_payment_method,
        )

        try:
            method = normalize_payment_method(raw)
            assert_customer_selectable(method)
        except (UnknownPaymentMethodError, UnsupportedPaymentMethodError) as exc:
            raise ValidationError("api.subscriptions.error.invalid_payment_method") from exc
        return method

    @staticmethod
    def offered_payment_methods(user, delivery_address_id: Optional[int]) -> List[Dict[str, Any]]:
        """THE subscription payment menu of this customer at this address (spec 2026-10-26 §8.1, B8):
        `PaymentService.get_available_payment_methods` in the subscription context, which the website and the
        bot ask through `/payments/methods?context=subscription` and the admin form through
        `/admin/users/<id>/payment-methods?context=subscription`. The write guard below checks this list."""
        from business_app.services.payment_service import PaymentContext
        from business_app.utils.service_factory import get_payment_service

        return get_payment_service().get_available_payment_methods(
            user, context=PaymentContext.SUBSCRIPTION, delivery_address_id=delivery_address_id
        )

    @staticmethod
    def admin_payment_menu(user_id: int, delivery_address_id: Optional[int]) -> Dict[str, Any]:
        """The admin subscription form's payment menu: the CUSTOMER's (`offered_payment_methods`), never the
        signed-in admin's, so the form offers exactly what an admin create or update will accept (B8)."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")
        if (
            delivery_address_id is not None
            and not UserAddress.query.filter_by(id=delivery_address_id, user_id=user_id).first()
        ):
            raise NotFoundError("Delivery address not found for this user")
        return {
            "customer_id": user_id,
            "available_methods": SubscriptionService.offered_payment_methods(user, delivery_address_id),
        }

    @staticmethod
    def method_offered(user, method: PaymentMethod, delivery_address_id: Optional[int]) -> bool:
        """B8's ONE answer (spec 2026-10-26 §8.1): whether this customer's payment menu at this address
        (`offered_payment_methods`) offers `method`. The write guard below, the card's `payment_method_warning` and
        an address change's `payment_method_offered` all ask it."""
        return SubscriptionService.menu_offers(
            SubscriptionService.offered_payment_methods(user, delivery_address_id), method
        )

    @staticmethod
    def menu_offers(menu: List[Dict[str, Any]], method: PaymentMethod) -> bool:
        """Whether an already-read payment menu offers `method` (`method_offered`'s membership test), for a caller
        that also publishes the menu and so reads it once."""
        return method.value in {entry["method"] for entry in menu}

    @staticmethod
    def _ensure_payment_method_offered(user, method: PaymentMethod, delivery_address_id: Optional[int]) -> None:
        """B8 (spec 2026-10-26 §8.1): a subscription may use only a method its payment menu offers THIS
        customer for THIS address. The menu (`offered_payment_methods`, the one every bot, web and admin
        surface reads) IS the write guard (`method_offered`), so nothing is accepted that the menu would not show:
        Bank Transfer for a customer without a workplace contract, or cash past the cash-debt limit. Either one
        would otherwise be saved, and then no order would ever be produced."""
        if not SubscriptionService.method_offered(user, method, delivery_address_id):
            raise ValidationError(
                _METHOD_NOT_OFFERED,
                details={"payment_method": method.value},
                error_code="SUBSCRIPTION_PAYMENT_METHOD_NOT_OFFERED",
            )

    def create_subscription(
        self,
        subscription_data: Dict[str, Any],
        items: List[Dict[str, Any]],
        discount_percentage: float = 0.0,
    ) -> Subscription:
        """
        Create a new subscription

        Args:
            subscription_data: Dictionary containing subscription configuration
            items: List of subscription items with product_id and quantity
            discount_percentage: Subscription discount. Subscriptions give none, so only the
                admin path passes it; it is deliberately NOT read from subscription_data,
                which is built from customer-reachable requests.

        Returns:
            Created Subscription object
        """
        user_id = subscription_data["user_id"]

        # Validate user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        # Note: Users can have multiple subscriptions with the same name
        # This allows flexibility in naming (e.g., "Weekly Water - Home" for different products)

        # Validate items
        if not items or len(items) == 0:
            raise ValidationError("Subscription must have at least one item")

        self._validate_subscription_items(items)

        # Calculate pricing
        total_amount = self._calculate_items_total(items)

        # Apply discount if provided
        if discount_percentage > 0:
            total_amount = total_amount * (1 - discount_percentage / 100)

        # Set start date
        start_date = subscription_data.get("start_date")
        if start_date is None:
            start_date = datetime.now(timezone.utc)

        try:
            frequency = SubscriptionFrequency(subscription_data["frequency"])
        except (KeyError, ValueError) as e:
            raise ValidationError(f"Invalid frequency value: {e}")

        # B8: only a method this customer's payment menu offers for this address (customer and admin alike).
        payment_method = self._validated_payment_method(subscription_data.get("payment_method", "cash"))
        self._ensure_payment_method_offered(user, payment_method, subscription_data.get("delivery_address_id"))

        # Create subscription
        slot_id = subscription_data.get("delivery_time_slot_id")
        subscription = Subscription(
            user_id=user_id,
            name=subscription_data.get("name", "Water Delivery Subscription"),
            description=subscription_data.get("description", ""),
            status=SubscriptionStatus.ACTIVE,
            delivery_day_of_week=subscription_data.get("delivery_day_of_week"),
            delivery_day_of_month=subscription_data.get("delivery_day_of_month"),
            # The object, not only the id: the schedule below reads the slot before the row is flushed.
            delivery_time_slot=db.session.get(DeliveryTimeSlot, slot_id) if slot_id else None,
            delivery_address_id=subscription_data.get("delivery_address_id"),
            payment_method=payment_method,
            auto_renew=subscription_data.get("auto_renew", True),
            discount_percentage=discount_percentage,
            billing_amount=total_amount,
            start_date=start_date,
            end_date=subscription_data.get("end_date"),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        # One frequency (spec 2026-10-23 §2.1); the first delivery by the rule (§4.3).
        subscription.frequency = frequency
        today = local_date()
        subscription.next_delivery_date = subscription_schedule.first_delivery(
            subscription, today, start=local_date(start_date)
        )
        # A weekly, biweekly or monthly subscription created without its day keeps the first delivery's (§2.9).
        for field, value in subscription_schedule.missing_anchor_day(
            subscription, subscription.next_delivery_date
        ).items():
            setattr(subscription, field, value)
        # B9 (spec 2026-10-26 §8.2) on create too, after the day is fixed: the schedule PUT's own guard and its
        # translated refusal, so create never saves what an edit would refuse (a monthly slot not offered on
        # every day, a weekday the slot is not offered on). Customer and admin creates both come through here.
        self._ensure_schedule_valid(subscription)

        db.session.add(subscription)
        db.session.flush()  # Get subscription ID

        current_app.logger.info(f"Subscription object CREATED SUCCESSFULLY")  # noqa: F541

        # Add subscription items
        for item_data in items:
            product: Product = Product.query.get(item_data["product_id"])
            if not product:
                raise NotFoundError(f"Product {item_data['product_id']} not found")

            subscription_item = SubscriptionItem(
                subscription_id=subscription.id,
                product_id=item_data["product_id"],
                quantity=item_data["quantity"],
                unit_price=self.customer_unit_price(product),
                special_instructions=item_data.get("special_instructions"),
            )
            subscription_item.calculate_total()
            db.session.add(subscription_item)
            current_app.logger.info(f"SubscriptionItem object CREATED SUCCESSFULLY")  # noqa: F541

        db.session.commit()
        current_app.logger.info(f"SubscriptionItem object CREATED SUCCESSFULLY with db commit")  # noqa: F541

        # A first delivery inside the lead window is created now, otherwise by the daily job (§4.3).
        self._create_first_order_if_due(subscription, today)

        return {**subscription.to_dict(), **self.published_schedule(subscription)}

    # ------------------------------------------------------------------ the engine (spec §4.3, §2.8)

    def _create_first_order_if_due(self, subscription: Subscription, today: date) -> None:
        """At creation, at resume and after the next delivery moved (a frequency, day or slot change, or the
        admin's "Set next delivery date"): a next delivery inside the lead window gets its order now, through
        the worker (as before this change); a later one is left to the daily job (§4.3, §4.6)."""
        if (
            subscription.status != SubscriptionStatus.ACTIVE
            or subscription.next_delivery_date is None
            or subscription.next_delivery_date > subscription_schedule.due_through(today)
        ):
            return
        try:
            from business_app.tasks.subscription_tasks import create_subscription_delivery_task

            create_subscription_delivery_task.delay(subscription.id)
        except Exception:
            # Celery being down must not fail the action; the 09:00 job still creates the order.
            current_app.logger.exception("Failed to enqueue the first order of subscription %s", subscription.id)

    def create_due_orders(self, today: Optional[date] = None) -> Dict[str, Any]:
        """The daily job (§4.3): every ACTIVE subscription whose next delivery is due by `today`'s lead
        gets its order(s). One subscription can have more than one due after downtime."""
        today = today or local_date()
        due_ids = [
            row.id
            for row in Subscription.query.with_entities(Subscription.id)
            .filter(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.next_delivery_date <= subscription_schedule.due_through(today),
            )
            .order_by(Subscription.id)
            .all()
        ]
        summary = {"total_processed": 0, "successful": 0, "skipped": 0, "failed": 0, "errors": []}
        for subscription_id in due_ids:
            summary["total_processed"] += 1
            for _ in range(_MAX_ORDERS_PER_RUN):
                try:
                    result = self.create_next_order(subscription_id, today=today)
                except Exception as exc:  # one subscription must never stop the run
                    db.session.rollback()
                    current_app.logger.exception("Subscription %s: creating its next order failed", subscription_id)
                    summary["failed"] += 1
                    summary["errors"].append({"subscription_id": subscription_id, "error": str(exc)})
                    break
                summary["successful"] += len(result.get("created_order_ids", []))
                summary["skipped"] += result.get("skipped_deliveries", 0)
                if not result["success"] and not result.get("skipped"):
                    summary["failed"] += 1
                    summary["errors"].append({"subscription_id": subscription_id, "error": result.get("error")})
                if not result.get("advanced"):
                    break
        return summary

    def create_next_order(
        self, subscription_id: int, *, today: Optional[date] = None, due_only: bool = True
    ) -> Dict[str, Any]:
        """Create the order for the subscription's next delivery, dated and windowed, then advance (§4.3).

        `due_only` (the job, creation and resume) leaves a delivery outside the lead window alone; the
        admin's "Create next order now" passes False. Never back-dated (§2.8): a next delivery that has
        already passed is skipped, with every other passed one, and the order is made for the first
        scheduled date from today. A refused order (one of the §2.8 refusals) skips that one delivery and
        advances; it is never retried. Any other failure moves nothing, and the next run tries again. The
        admin's run for a delivery not yet due changes nothing on a refusal either: it only reports it.

        The result always says whether the clock `advanced`, so the daily job knows whether to look again.
        """
        today = today or local_date()
        subscription = self._fetch(Subscription.query.filter_by(id=subscription_id), lock=True)
        if not subscription:
            raise NotFoundError("Subscription not found")
        try:
            self._require(subscription, "create_next_order", actor=ADMIN)
        except ValidationError:
            db.session.commit()  # release the row lock before raising
            raise

        result: Dict[str, Any] = {
            "success": True,
            "subscription_id": subscription_id,
            "advanced": False,
            "created_order_ids": [],
            "skipped_deliveries": 0,
        }
        if subscription.next_delivery_date is None:
            subscription.next_delivery_date = subscription_schedule.first_delivery(subscription, today)
        day, passed = subscription_schedule.catch_up(subscription, subscription.next_delivery_date, today)
        if passed:
            # A forward move (M1): it steps over the dates the customer skipped, and so does not report them as
            # passed a second time. The clock's own date is reported even when the customer once skipped it: a
            # resume or the admin put it back, so it was due and genuinely passed.
            customer_skipped = self._customer_skipped_dates(subscription.id)
            passed = passed[:1] + [passed_day for passed_day in passed[1:] if passed_day not in customer_skipped]
            day = self._step_over_skips(subscription, day)
            subscription.next_delivery_date = day
            log = self._delivery_skipped_log(subscription, passed, subscription_notices.SKIP_DATE_PASSED, day, None)
            db.session.commit()
            self._deliver([log])
            result.update(advanced=True, skipped_deliveries=len(passed))
            subscription = self._fetch(Subscription.query.filter_by(id=subscription_id), lock=True)

        not_due = day > subscription_schedule.due_through(today)
        if due_only and not_due:
            db.session.commit()
            return {**result, "skipped": True, "reason": "not_due", **self._clock_fields(subscription)}

        existing = self._live_order_for(subscription_id, day)
        if existing is not None:
            # The crash window (order committed, the clock and its ORDER_CREATED row not): heal the clock and
            # write the ORDER_CREATED row the normal path writes, so the customer still gets its notice (a
            # Click subscriber's only Pay now). Never a second order, and never a second notice: an order that
            # already has its row is only healed.
            subscription.last_delivery_date = day
            subscription.next_delivery_date = self._advance_clock(subscription, day)
            notices = [] if self._order_announced(existing) else [self._order_created_log(subscription, existing, day)]
            db.session.commit()
            self._deliver(notices)
            return {
                **result,
                "advanced": True,
                "skipped": True,
                "reason": "already_created",
                "order_id": existing.id,
                "delivery_date": day.isoformat(),
                **self._clock_fields(subscription),
            }

        from .order_service import OrderService

        refusal = OrderService.customer_order_refusal(subscription.user)
        if refusal is not None:
            if not_due:
                db.session.rollback()  # release the row lock: nothing changes
                return self._refused_before_due(
                    subscription_id, subscription_notices.SKIP_CUSTOMER_BARRED, refusal, result
                )
            return self._skip_refused_delivery(
                subscription_id, day, subscription_notices.SKIP_CUSTOMER_BARRED, refusal, result
            )

        # A product that is inactive or gone is left out of the order and told to the customer (spec §8.5);
        # when none is left the delivery is skipped as out of stock (§2.8).
        lines = [item for item in subscription.subscription_items if self.product_available(item.product)]
        left_out = [item for item in subscription.subscription_items if item not in lines]
        if not lines:
            detail = "no product of the subscription is available"
            if not_due:
                db.session.rollback()  # release the row lock: nothing changes
                return self._refused_before_due(subscription_id, subscription_notices.SKIP_OUT_OF_STOCK, detail, result)
            return self._skip_refused_delivery(
                subscription_id, day, subscription_notices.SKIP_OUT_OF_STOCK, detail, result
            )

        try:
            order = OrderService().create_order(
                subscription.user_id, self._order_data(subscription, day, lines), subscription=subscription
            )
        except Exception as exc:
            db.session.rollback()
            reason = subscription_notices.refusal_reason(exc)
            if reason is None:
                # Not a §2.8 refusal. `create_order` reports an unexpected failure (a Redis or database
                # outage) as a ValidationError with no code. Nothing moves and the next run tries again; a
                # date that passes meanwhile is skipped by `catch_up`.
                current_app.logger.exception("Subscription %s: creating the order for %s failed", subscription_id, day)
                return {**result, "success": False, "error": str(exc)}
            current_app.logger.warning("Subscription %s: the order for %s was refused: %s", subscription_id, day, exc)
            if not_due:
                return self._refused_before_due(subscription_id, reason, str(exc), result)
            return self._skip_refused_delivery(subscription_id, day, reason, str(exc), result)

        # The Order is authoritative: billing_amount records what was actually charged.
        subscription.billing_amount = order.total_amount
        subscription.total_amount_billed = (subscription.total_amount_billed or Decimal("0.00")) + order.total_amount
        subscription.total_orders_generated = (subscription.total_orders_generated or 0) + 1
        log = self._order_created_log(subscription, order, day, not_included=[item.product_id for item in left_out])
        subscription.last_delivery_date = day
        subscription.next_delivery_date = self._advance_clock(subscription, day)
        db.session.commit()
        self._deliver([log])

        return {
            **result,
            "advanced": True,
            "created_order_ids": [order.id],
            "order_id": order.id,
            "amount": float(order.total_amount),
            "delivery_date": day.isoformat(),
            **self._clock_fields(subscription),
        }

    @staticmethod
    def _order_data(subscription: Subscription, day: date, lines: Sequence[SubscriptionItem]) -> Dict[str, Any]:
        """The order for `day` with the given item lines, in the slot's window when the slot is offered that
        day (§4.2). The slot's own fees are never applied: `create_order` keeps its normal delivery-fee rule."""
        address = subscription.delivery_address
        window = subscription_schedule.window_for(subscription.delivery_time_slot, day)
        return {
            "items": [{"product_id": item.product_id, "quantity": item.quantity} for item in lines],
            # Address lives on the UserAddress relationship; create_order requires delivery_address_id.
            "delivery_address": {
                "delivery_address_id": subscription.delivery_address_id,
                "street": address.street_address if address else None,
                "city": address.city if address else None,
                "latitude": address.latitude if address else None,
                "longitude": address.longitude if address else None,
            },
            "delivery_notes": address.delivery_instructions if address else None,
            "payment_method": subscription.payment_method.value,
            "delivery_date": day,
            "delivery_window_start": window[0] if window else None,
            "delivery_window_end": window[1] if window else None,
        }

    def _order_created_log(
        self, subscription: Subscription, order: Order, day: date, *, not_included: Sequence[int] = ()
    ) -> SubscriptionLog:
        """The ORDER_CREATED row an order the subscription created owes, added in the transaction that moves
        the clock; its notice is sent from it after the commit (§9). `not_included` are the products left out
        of the order because they are no longer available: the notice names them."""
        log = SubscriptionLog(
            subscription_id=subscription.id,
            action=subscription_notices.ORDER_CREATED,
            details=get_translation(
                "api.subscriptions.log.order_created",
                self._owner_language(subscription),
                order_number=order.order_number,
                date=day.isoformat(),
            ),
            extra_data={
                "order_id": order.id,
                "order_number": order.order_number,
                "delivery_date": day.isoformat(),
                **({"not_included_product_ids": list(not_included)} if not_included else {}),
            },
        )
        db.session.add(log)
        return log

    @staticmethod
    def _order_announced(order: Order) -> bool:
        """Whether this order already has its ORDER_CREATED row (and so its notice). Read only on the rare
        crash-heal path, so the rows are filtered here rather than by a JSON query each database spells
        differently."""
        return any(
            (log.extra_data or {}).get("order_id") == order.id
            for log in SubscriptionLog.query.filter_by(
                subscription_id=order.subscription_id, action=subscription_notices.ORDER_CREATED
            )
        )

    def _refused_before_due(
        self, subscription_id: int, reason: str, detail: str, result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """The admin's "Create next order now" for a delivery not yet due was refused (controller ruling, final
        review I3). Nothing changes: no skip, no clock move, no notice. The refusal may clear by the delivery
        day, when the daily job creates the order or, still refused, skips it (§2.8)."""
        subscription = db.session.get(Subscription, subscription_id)
        return {**result, "success": False, "reason": reason, "error": detail, **self._clock_fields(subscription)}

    def _skip_refused_delivery(
        self, subscription_id: int, day: date, reason: str, detail: str, result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """§2.8: the order for `day` cannot be created. That one delivery is skipped, the clock moves to the
        next scheduled date and the customer is owed one notice. Not a payment failure: the subscription
        stays ACTIVE and no failure counter moves.

        The row is locked again and re-read first: after `create_order` rolled back, the lock was gone, and a
        skip, pause, cancel or change that committed meanwhile wins. Then nothing is skipped here."""
        subscription = self._fetch(Subscription.query.filter_by(id=subscription_id), lock=True)
        if (
            subscription is None
            or subscription.status != SubscriptionStatus.ACTIVE
            or subscription.next_delivery_date != day
        ):
            db.session.commit()  # release the row lock
            return {**result, "success": False, "skipped": True, "reason": "changed_meanwhile", "error": detail}
        next_day = self._advance_clock(subscription, day)
        subscription.next_delivery_date = next_day
        log = self._delivery_skipped_log(subscription, [day], reason, next_day, detail)
        db.session.commit()
        self._deliver([log])
        return {
            **result,
            "success": False,
            "advanced": True,
            "skipped": True,
            "reason": reason,
            "error": detail,
            "skipped_deliveries": result["skipped_deliveries"] + 1,
            "skipped_date": day.isoformat(),
            **self._clock_fields(subscription),
        }

    def _delivery_skipped_log(
        self, subscription: Subscription, skipped: List[date], reason: str, next_day: date, detail: Optional[str]
    ) -> SubscriptionLog:
        language = self._owner_language(subscription)
        log = SubscriptionLog(
            subscription_id=subscription.id,
            action=subscription_notices.DELIVERY_SKIPPED,
            details=get_translation(
                "api.subscriptions.log.delivery_skipped",
                language,
                date=", ".join(day.isoformat() for day in skipped),
                reason=get_translation(f"api.subscriptions.skip_reason.{reason}", language),
            ),
            extra_data={
                "skipped_by": "system",
                "reason_code": reason,
                "skipped_dates": [day.isoformat() for day in skipped],
                "next_delivery_date": next_day.isoformat(),
                "detail": detail,
            },
        )
        db.session.add(log)
        return log

    @staticmethod
    def _live_order_for(subscription_id: int, day: date, *, live_only: bool = False) -> Optional[Order]:
        """The idempotency guard (§4.3): a non-cancelled order of this subscription for this date. With
        `live_only`, one still in play (not delivered, cancelled or returned)."""
        from shared.enums import OrderStatus

        status = Order.status.in_(ACTIVE_ORDER_STATUSES) if live_only else Order.status != OrderStatus.CANCELLED
        return Order.query.filter(
            Order.subscription_id == subscription_id,
            Order.delivery_date == day,
            status,
        ).first()

    @staticmethod
    def _clock_fields(subscription: Subscription) -> Dict[str, Any]:
        return {
            "next_delivery_date": (
                subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
            ),
        }

    @staticmethod
    def _owner_language(subscription: Subscription) -> Optional[str]:
        """System-written log lines are in the customer's language: the bot shows them as they are."""
        return getattr(subscription.user, "preferred_language", None) or None

    @staticmethod
    def _deliver(notices: Iterable[SubscriptionLog]) -> None:
        """Hand committed notice rows to the Task 3 hook. Only ever called after the commit."""
        for log in notices:
            subscription_notices.deliver(log)

    # ------------------------------------------------------------------ the one published schedule (§9)

    @staticmethod
    def _orders_in_play(subscription_id: int) -> List[Order]:
        """This subscription's orders still alive (not delivered, cancelled or returned), soonest first."""
        return (
            Order.query.filter(
                Order.subscription_id == subscription_id,
                Order.status.in_(ACTIVE_ORDER_STATUSES),
            )
            .order_by(Order.delivery_date.is_(None), Order.delivery_date, Order.id)
            .all()
        )

    @classmethod
    def _upcoming_order(cls, subscription_id: int, *, orders: Optional[Sequence[Order]] = None) -> Optional[Order]:
        """§9: this subscription's created order that is neither cancelled nor dispatched, the soonest
        first. Dispatched means the driver has it (`customer_cancel_block_code` WITH_DRIVER). `orders` are
        `_orders_in_play` already read."""
        from .order_service import CUSTOMER_CANCEL_PAID, OrderService

        for order in cls._orders_in_play(subscription_id) if orders is None else orders:
            if order.delivery_date is not None and OrderService.customer_cancel_block_code(order) in (
                None,
                CUSTOMER_CANCEL_PAID,
            ):
                return order
        return None

    @classmethod
    def next_delivery_after_skip(
        cls, subscription: Optional[Subscription], skipped, *, orders: Optional[Sequence[Order]] = None
    ) -> Optional[date]:
        """The ONE answer to "the next delivery after skipping D" (spec 2026-10-27 §5.2, §16 M3): a skip's
        "Next delivery: …" and "Your next delivery is …". `skipped` is the order skipped (dated D) or the date D.

        The soonest of: the dates after D of the orders the page shows coming (`_shown_orders`, the one the driver
        has included), the skipped order left out; and the clock when it is after D, otherwise the clock's next
        move from D (`_advance_clock`, which steps over the dates the customer skipped). None unless the
        subscription is ACTIVE. `orders` are this subscription's `_orders_in_play`, when already read."""
        if subscription is None or subscription.status != SubscriptionStatus.ACTIVE:
            return None
        if isinstance(skipped, Order):
            day, skipped_id = skipped.delivery_date, skipped.id
        else:
            day, skipped_id = skipped, None
        later = [
            order.delivery_date
            for order, _state in cls._shown_orders(cls._orders_of(subscription, orders))
            if order.id != skipped_id and order.delivery_date > day
        ]
        clock = subscription.next_delivery_date
        later.append(clock if clock is not None and clock > day else cls._advance_clock(subscription, day))
        return min(later)

    @classmethod
    def _shown_orders(cls, orders: Sequence[Order]) -> Iterator[Tuple[Order, str]]:
        """The live orders the page shows coming, soonest first, each `PLACED` or `ON_THE_WAY` (spec
        2026-10-26 §12: the order the driver has is shown "On the way"). An order that waits for a new date
        after a failed delivery, or a leftover the customer's cancel rule fails closed on, is not coming."""
        from .order_service import CUSTOMER_CANCEL_PAID, CUSTOMER_CANCEL_WITH_DRIVER, OrderService

        for order in orders:
            if order.delivery_date is None:
                continue
            code = OrderService.customer_cancel_block_code(order)
            if code not in (None, CUSTOMER_CANCEL_PAID, CUSTOMER_CANCEL_WITH_DRIVER):
                continue
            yield order, (ON_THE_WAY if OrderService.order_is_with_driver(order) else PLACED)

    @classmethod
    def kept_order_note(cls, orders: Sequence[Order]) -> Optional[str]:
        """The note "Your order for 30 October stays as it is.": a schedule, items or payment-method change
        leaves the order already placed (the first one the page shows coming) as it was placed. ONE answer for the
        preview (`kept_order_note`, spec 2026-10-26 §5 B4) and the subscription payload, which the payment
        editor shows before and after a save (UX brief "SCHEDULE AND PAYMENT SAVE")."""
        shown = next(iter(cls._shown_orders(orders)), None)
        return subscription_display.kept_order_note(shown[0]) if shown is not None else None

    @classmethod
    def _orders_of(cls, subscription, orders: Optional[Sequence[Order]]) -> Sequence[Order]:
        if orders is not None:
            return orders
        return cls._orders_in_play(subscription.id) if subscription.id is not None else []

    @classmethod
    def upcoming_delivery(
        cls, subscription: Subscription, *, orders: Optional[Sequence[Order]] = None
    ) -> Optional[Dict[str, Any]]:
        """The "upcoming delivery" the bot, the web and admin show (§9, spec 2026-10-26 §5 B3, §12): the
        soonest order still coming, the one the driver has included (`on_the_way`); a paused or cancelled
        subscription's kept order is `paid_kept` only when it is paid (with `kept_line`, the sentence for it).
        Otherwise an ACTIVE subscription's `next_delivery_date` in
        the slot's window (`planned`, with the day its order will be placed). None for a paused or cancelled
        subscription with no order coming. Labelled for the page (`subscription_display.upcoming_delivery`)."""
        shown = next(iter(cls._shown_orders(cls._orders_of(subscription, orders))), None)
        today = local_date()
        if shown is not None:
            order, state = shown
            kept_line = None
            if subscription.status != SubscriptionStatus.ACTIVE:
                # A kept order is called paid only when it is; the driver's is "on the way", paid or not.
                from .order_service import CUSTOMER_CANCEL_PAID, CUSTOMER_CANCEL_WITH_DRIVER, OrderService

                if state == PLACED and OrderService.customer_cancel_block_code(order) == CUSTOMER_CANCEL_PAID:
                    state = PAID_KEPT
                if state == PAID_KEPT:
                    kept_line = subscription_display.kept_line(order, CUSTOMER_CANCEL_PAID)
                elif state == ON_THE_WAY:
                    kept_line = subscription_display.kept_line(order, CUSTOMER_CANCEL_WITH_DRIVER)
            return subscription_display.upcoming_delivery(subscription, order, state, today, kept_line)
        if subscription.status != SubscriptionStatus.ACTIVE or subscription.next_delivery_date is None:
            return None
        return subscription_display.upcoming_delivery(subscription, None, PLANNED, today)

    @classmethod
    def upcoming_dates(
        cls, subscription, *, orders: Optional[Sequence[Order]] = None, today: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """The waterline (spec 2026-10-26 §5 B2): the next `UPCOMING_DATES_SHOWN` deliveries of an ACTIVE
        subscription, soonest first, each `placed` or `on_the_way` (its order is coming), `skipped` (a skip the
        clock has moved past, or a date the customer skipped that the clock will step over), or `planned` (from the
        clock, by `subscription_schedule`). A paused one shows the one delivery a resume today would give ("If you
        resume today"); a cancelled one shows none. `subscription` may be a `_ScheduleDraft`: the preview's
        waterline is this same answer under the draft settings."""
        today = today or local_date()
        if subscription.status == SubscriptionStatus.PAUSED:
            return [subscription_display.date_item(subscription_schedule.first_delivery(subscription, today), PLANNED)]
        if subscription.status != SubscriptionStatus.ACTIVE:
            return []
        first = subscription.next_delivery_date or subscription_schedule.first_delivery(subscription, today)
        # A skipped date still to come before the clock: the clock moved past it. One on the clock was put back (a
        # resume, or an admin's next delivery date): the clock delivers it, and the clock alone decides it. One the
        # customer skipped after the clock (staff had moved its order ahead): the clock steps over it when it gets
        # there (`_advance_clock`, spec 2026-10-27 §16 M1).
        known: Dict[date, str] = {}
        for log in cls._skip_logs(subscription.id):
            by_customer = cls._skipped_by_customer(log)
            for day in cls._skip_log_dates(log):
                if today <= day < first or (by_customer and day > first):
                    known[day] = SKIPPED
        for order, state in cls._shown_orders(cls._orders_of(subscription, orders)):
            # An order for the date is what is coming, also on a date once skipped (an admin re-dated it).
            if known.get(order.delivery_date) in (None, SKIPPED):
                known[order.delivery_date] = state
        for day in subscription_schedule.next_deliveries(subscription, first, UPCOMING_DATES_SHOWN):
            known.setdefault(day, PLANNED)
        return [
            subscription_display.date_item(day, state) for day, state in sorted(known.items())[:UPCOMING_DATES_SHOWN]
        ]

    @staticmethod
    def _skipped_by_customer(log: SubscriptionLog) -> bool:
        return (log.extra_data or {}).get("skipped_by") == "customer"

    @classmethod
    def _skip_log_dates(cls, log: SubscriptionLog) -> List[date]:
        """The dates one delivery_skipped row skipped: the customer's one date, or the system's dates."""
        extra = log.extra_data or {}
        raw = (
            [extra.get("original_delivery_date")] if cls._skipped_by_customer(log) else extra.get("skipped_dates") or []
        )
        return [date.fromisoformat(day) for day in raw if day]

    @classmethod
    def _skip_logs(cls, subscription_id: Optional[int]) -> List[SubscriptionLog]:
        if subscription_id is None:
            return []
        return SubscriptionLog.query.filter_by(
            subscription_id=subscription_id, action=subscription_notices.DELIVERY_SKIPPED
        ).all()

    @classmethod
    def _customer_skipped_dates(cls, subscription_id: Optional[int]) -> Set[date]:
        """Every date the customer skipped (a customer `delivery_skipped` row's `original_delivery_date`). The only
        dates the clock steps over (spec 2026-10-27 §16 M1): a system skip, a pause cascade or a staff cancel never
        stops a date from being delivered again."""
        return {
            day
            for log in cls._skip_logs(subscription_id)
            if cls._skipped_by_customer(log)
            for day in cls._skip_log_dates(log)
        }

    @classmethod
    def _step_over_skips(cls, subscription, day: date) -> date:
        """`day`, or when the customer skipped it, the first delivery after it the customer did not skip: where a
        forward clock move lands (the daily run, a refused delivery, a skip, the catch-up after missed runs, a
        schedule change). A move that sets the clock (a resume, the admin's next delivery date, a new
        subscription's first delivery) lands where it is told and delivers that date (M1). Bounded by
        `_MAX_ORDERS_PER_RUN` steps, so no data can loop it forever."""
        skipped = cls._customer_skipped_dates(subscription.id)
        for _ in range(_MAX_ORDERS_PER_RUN):
            if day not in skipped:
                break
            day = subscription_schedule.advance(subscription, day)
        return day

    @classmethod
    def _advance_clock(cls, subscription, day: date) -> date:
        """The clock's next delivery after `day`: one cycle on (`subscription_schedule.advance`), stepping over the
        dates the customer skipped (`_step_over_skips`). Every forward move from a delivery goes through here."""
        return cls._step_over_skips(subscription, subscription_schedule.advance(subscription, day))

    @classmethod
    def published_schedule(
        cls,
        subscription: Subscription,
        *,
        orders: Optional[Sequence[Order]] = None,
        delivery_days: Optional[FrozenSet[int]] = None,
    ) -> Dict[str, Any]:
        """The schedule fields every subscription payload carries, decided here once (§9). A caller over many rows
        (the admin list) hands in `delivery_days`, read once (`subscription_schedule.delivery_weekdays`)."""
        frequency = subscription.frequency
        return {
            "frequency": getattr(frequency, "value", frequency),
            "next_delivery_date": (
                subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
            ),
            "last_delivery_date": (
                subscription.last_delivery_date.isoformat() if subscription.last_delivery_date else None
            ),
            "deliveries_per_month": float(
                subscription_schedule.deliveries_per_month(
                    frequency, subscription.delivery_time_slot, delivery_days=delivery_days
                )
            ),
            "upcoming_delivery": cls.upcoming_delivery(subscription, orders=orders),
            # The one delivery-time answer (spec 2026-10-27 §6.3): the window, "Any time" or "Time to be confirmed".
            "time_kind": subscription_display.time_kind(subscription),
            "time_label": subscription_display.time_label(subscription),
        }

    # ------------------------------------------------------------------ the page's published answers (B2-B5)

    @classmethod
    def published_fields(cls, subscription: Subscription, *, actor: str = CUSTOMER) -> Dict[str, Any]:
        """Every published answer of a customer subscription payload, with its live orders read once: the
        schedule (§9), `allowed_actions` (spec 2026-10-26 §5 B1) and the page's fields (B2-B5)."""
        orders = cls._orders_of(subscription, None)
        return {
            **cls.published_schedule(subscription, orders=orders),
            "allowed_actions": cls.allowed_actions(subscription, actor=actor, orders=orders),
            **cls.published_page(subscription, orders=orders),
        }

    @staticmethod
    def product_available(product: Optional[Product]) -> bool:
        """Whether a subscription item's product can still be delivered: it exists and is active. An
        unavailable one is left out of the estimate (spec 2026-10-26 §8.5)."""
        return product is not None and bool(product.is_active)

    @staticmethod
    def customer_unit_price(product: Product) -> Decimal:
        """THE unit price of a subscription item (spec 2026-10-26 §5 B10, §8.3): the product's current
        customer price, for create, add, the replace-all save, the preview and the estimate. Orders are
        still priced when they are created."""
        return product.catalogue_unit_price

    @classmethod
    def line_labels(cls, product: Optional[Product], quantity: int) -> Dict[str, Any]:
        """One item line as the website shows it, saved (`subscription_items[]`) or drafted (the preview's
        `items[]`): the product's name, whether it can still be delivered, its minimum quantity, and "2 ×
        15 000 so'm" / "30 000 so'm" at the one price rule. A deleted product has no name and no price."""
        from business_app.utils.helpers import get_current_language

        if product is None:
            return {
                "product_name": None,
                "available": False,
                "min_order_quantity": 1,
                "unit_price_label": None,
                "line_total_label": None,
            }
        price = cls.customer_unit_price(product)
        return {
            "product_name": product.get_translated("name", get_current_language()),
            "available": cls.product_available(product),
            "min_order_quantity": int(product.min_order_quantity or 1),
            "unit_price_label": display_labels.money(price),
            "line_total_label": display_labels.money(price * quantity),
        }

    @classmethod
    def product_catalog(cls) -> List[Dict[str, Any]]:
        """Every subscription product picker (spec 2026-10-26 B7, B10; UX brief "PRODUCT PICKER"): /my-subscriptions,
        the homepage builder and the customer bot. Every product a subscription may take, the active ones the
        items save accepts, unpaged, each with its name, minimum quantity and unit price label at the one price
        rule (`line_labels`). Not the products API's checkout price, which is a different rule (its `base_price`
        and bulk rules, a contract). Not personalised: nothing here reads the customer."""
        products = Product.query.filter(Product.is_active.is_(True)).order_by(Product.name, Product.id).all()
        catalog = []
        for product in products:
            labels = cls.line_labels(product, 1)
            catalog.append(
                {
                    "id": product.id,
                    "product_name": labels["product_name"],
                    "images": list(product.images or []),
                    "min_order_quantity": labels["min_order_quantity"],
                    "unit_price_label": labels["unit_price_label"],
                }
            )
        return catalog

    @classmethod
    def _estimate(cls, lines: Iterable[Tuple[Optional[Product], int]], frequency, slot=None) -> Tuple[Decimal, Decimal]:
        """`estimated_per_delivery` (the available items at their current customer price) and
        `estimated_per_month` (that times `deliveries_per_month` on `slot`'s days): spec 2026-10-26 §12, spec
        2026-10-27 §6.4."""
        per_delivery = sum(
            (
                cls.customer_unit_price(product) * quantity
                for product, quantity in lines
                if cls.product_available(product)
            ),
            Decimal("0.00"),
        ).quantize(_CENT)
        return per_delivery, cls._monthly_amount(per_delivery, frequency, slot)

    @staticmethod
    def _current_lines(subscription: Subscription) -> List[Tuple[Optional[Product], int]]:
        return [(item.product, item.quantity) for item in subscription.subscription_items]

    @classmethod
    def _estimate_fields(cls, lines, frequency, slot) -> Dict[str, Any]:
        """The estimate as published: `deliveries_per_month` and both amounts, as numbers and labels. `slot` is
        the subscription's (or the draft's): a daily delivery counts its days (spec 2026-10-27 §6.4)."""
        per_delivery, per_month = cls._estimate(lines, frequency, slot)
        return {
            "deliveries_per_month": float(subscription_schedule.deliveries_per_month(frequency, slot)),
            "estimated_per_delivery": float(per_delivery),
            "estimated_per_delivery_label": display_labels.money(per_delivery),
            "estimated_per_month": float(per_month),
            "estimated_per_month_label": display_labels.money(per_month),
        }

    @classmethod
    def published_page(cls, subscription: Subscription, *, orders: Optional[Sequence[Order]] = None) -> Dict[str, Any]:
        """What the website reads besides the schedule and `allowed_actions` (spec 2026-10-26 §5 B2, B5,
        §12): the waterline, the estimate, whether every product can still be delivered, the sentences and
        the date labels. Decided here, worded by `subscription_display`."""
        lines = cls._current_lines(subscription)
        available = sum(1 for product, _ in lines if cls.product_available(product))
        start = local_date(subscription.start_date) if subscription.start_date else None
        paused = subscription.status == SubscriptionStatus.PAUSED and subscription.paused_at
        cancelled = subscription.status == SubscriptionStatus.CANCELLED and subscription.end_date
        return {
            "delivery_address_id": subscription.delivery_address_id,
            "delivery_address_label": subscription_display.address_label(subscription.delivery_address),
            "upcoming_dates": cls.upcoming_dates(subscription, orders=orders),
            **cls._estimate_fields(lines, subscription.frequency, subscription.delivery_time_slot),
            # all: every product is delivered; some: the unavailable ones are left out; none: nothing can be.
            "items_availability": "all" if available == len(lines) else ("none" if available == 0 else "some"),
            "summary_sentence": subscription_display.summary_sentence(subscription, start),
            "schedule_sentence": subscription_display.schedule_sentence(subscription),
            "payment_method_label": display_labels.payment_method(subscription.payment_method),
            # B8's one published answer (spec 2026-10-27 §7): the method is no longer on its address's menu. The
            # cabinet and the bot render it; an address change (even to the same address) asks for one then. Shown
            # only where that write is allowed: it is gated on the write guard's own rule (`edit`, the customer's).
            "payment_method_warning": (
                get_translation(_METHOD_WARNING)
                if cls._action_refusal(subscription, "edit", actor=CUSTOMER, override=False) is None
                and not cls.method_offered(
                    subscription.user, subscription.payment_method, subscription.delivery_address_id
                )
                else None
            ),
            # A change of method (or schedule, or items) leaves the order already placed as it is.
            "kept_order_note": cls.kept_order_note(cls._orders_of(subscription, orders)),
            "paused_at_label": display_labels.day_month(local_date(subscription.paused_at)) if paused else None,
            "cancelled_at_label": display_labels.day_month(local_date(subscription.end_date)) if cancelled else None,
        }

    @staticmethod
    def _monthly_amount(amount, frequency, slot=None, *, delivery_days: Optional[FrozenSet[int]] = None) -> Decimal:
        """A per-delivery amount over a month, by the one calendar-accurate count (§4.2) on `slot`'s days (spec
        2026-10-27 §6.4)."""
        per_month = subscription_schedule.deliveries_per_month(frequency, slot, delivery_days=delivery_days)
        return (Decimal(str(amount or 0)) * per_month).quantize(_CENT)

    @staticmethod
    def _deliveries_per_month_label(per_month) -> str:
        """The builder's per-month hint as a finished sentence in the page language: a whole count (like the
        cabinet's preview) worded with the language's plural form."""
        from business_app.utils.helpers import get_current_language

        count = int(per_month.to_integral_value())
        return get_translation(
            plural_key("landing.custom_subscription.review.per_month_hint", get_current_language(), count),
            count=count,
        )

    @classmethod
    def monthly_value(cls, subscription: Subscription, *, delivery_days: Optional[FrozenSet[int]] = None) -> Decimal:
        """What one subscription brings in a month. A caller over many rows hands in `delivery_days`, read once."""
        return cls._monthly_amount(
            subscription.billing_amount,
            subscription.frequency,
            subscription.delivery_time_slot,
            delivery_days=delivery_days,
        )

    @classmethod
    def monthly_recurring_revenue(cls, subscriptions: Optional[Iterable[Subscription]] = None) -> Decimal:
        """MRR: what the ACTIVE subscriptions bring in a month. The admin dashboard, the subscription
        report and the subscription analytics all ask this one function (§4.2, §5.6)."""
        if subscriptions is None:
            subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.ACTIVE).all()
        delivery_days = subscription_schedule.delivery_weekdays()
        return sum(
            (
                cls.monthly_value(s, delivery_days=delivery_days)
                for s in subscriptions
                if s.status == SubscriptionStatus.ACTIVE
            ),
            Decimal("0.00"),
        )

    # ------------------------------------------------------------------ one guard per action (spec 2026-10-26 B1)

    @classmethod
    def check_action(
        cls,
        subscription: Subscription,
        action: str,
        *,
        actor: str = CUSTOMER,
        override: bool = False,
        orders: Optional[Sequence[Order]] = None,
    ) -> ActionCheck:
        """THE answer to "may this actor do `action` to this subscription now, and what will it do?".

        Actions: pay, skip, pause, resume, cancel, edit, remove_item, create_next_order. `override` is the
        admin's `edit_any_status`. `allowed_actions` publishes this answer and every customer and admin write
        is refused by the same rule (`_require`), so a button the website, the bot or admin shows and the
        write it sends cannot disagree. The rules themselves are the step-2 rules (spec 2026-10-23 §4.6):
        only their home moved. `orders` are this subscription's `_orders_in_play`, when already read."""
        refusal = cls._action_refusal(subscription, action, actor=actor, override=override, orders=orders)
        consequences: Dict[str, Any] = {}
        if action == "edit" and actor == ADMIN:
            # The admin form's "Allow editing any status" lifts a status refusal.
            consequences["override_available"] = (
                cls._action_refusal(subscription, action, actor=actor, override=True) is None
            )
        if refusal is not None:
            if actor == CUSTOMER and action in LABELLED_ACTIONS:
                consequences["label"] = None  # a refused action has no button
            return ActionCheck(False, refusal, consequences)
        return ActionCheck(
            True, None, {**consequences, **cls._consequences(subscription, action, actor=actor, orders=orders)}
        )

    @classmethod
    def allowed_actions(
        cls, subscription: Subscription, *, actor: str = CUSTOMER, orders: Optional[Sequence[Order]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """`allowed_actions` as every subscription payload publishes it: per action `{allowed, block_reason,
        …consequences}`, the block reason translated in the request's language. The customer's also carries
        `expected_state` (`state_token`); the admin's does not, as admin writes take none."""
        orders = cls._orders_of(subscription, orders)
        published = {}
        for action in CUSTOMER_ACTIONS if actor == CUSTOMER else ADMIN_ACTIONS:
            check = cls.check_action(subscription, action, actor=actor, orders=orders)
            published[action] = {
                "allowed": check.allowed,
                "block_reason": get_translation(check.block_reason_key) if check.block_reason_key else None,
                **check.consequences,
            }
        if actor == CUSTOMER:
            # What the customer's pause, cancel, payment-method and address writes send back (spec 2026-10-27 §16
            # M2); the schedule and items saves send their preview's.
            published["expected_state"] = cls.state_token(subscription, orders=orders)
        return published

    @classmethod
    def state_token(cls, subscription: Subscription, *, orders: Optional[Sequence[Order]] = None) -> str:
        """What a customer's confirm was drawn from, as one opaque token (spec 2026-10-27 §16 M2): the
        subscription's schedule, address, method, clock and items, and each order in play (`_orders_in_play`) with
        its status, amount, date, address, method and cancel answer. No labels and no language, so the bot and the
        website send one token. `allowed_actions.expected_state` and the preview publish it; `_require_state`
        compares it on the locked row. 16 lowercase hex characters (a Telegram `callback_data` fits it)."""
        from .order_service import OrderService

        state = {
            "id": subscription.id,
            "status": subscription.status,
            "frequency": subscription.frequency,
            "delivery_day_of_week": subscription.delivery_day_of_week,
            "delivery_day_of_month": subscription.delivery_day_of_month,
            "delivery_time_slot_id": subscription.delivery_time_slot_id,
            "delivery_address_id": subscription.delivery_address_id,
            "payment_method": subscription.payment_method,
            "next_delivery_date": subscription.next_delivery_date,
            "items": sorted((item.product_id, item.quantity) for item in subscription.subscription_items),
            "orders": [
                (
                    order.id,
                    order.status,
                    str(order.total_amount),
                    order.delivery_date,
                    order.delivery_address_id,
                    order.payment_method,
                    OrderService.customer_cancel_block_code(order),
                )
                for order in cls._orders_of(subscription, orders)
            ],
        }
        return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    @classmethod
    def _require(cls, subscription: Subscription, action: str, *, actor: str = CUSTOMER, override: bool = False):
        """The write half of `check_action`: refuse with the very key `allowed_actions` publishes."""
        refusal = cls._action_refusal(subscription, action, actor=actor, override=override)
        if refusal is not None:
            raise ValidationError(refusal, details={"action": action}, error_code="SUBSCRIPTION_ACTION_NOT_ALLOWED")

    @classmethod
    def _require_state(cls, subscription: Subscription, expected_state: Optional[str]) -> None:
        """The screen half of a customer write (spec 2026-10-27 §16 M2): called after the write's own refusals, on
        the locked row. A token other than `state_token` now (the confirm named something that has moved), or none,
        raises StateChangedError: "This changed. Refresh to see the latest." Nothing is written."""
        if expected_state is None or expected_state != cls.state_token(subscription):
            raise StateChangedError()

    @classmethod
    def _action_refusal(
        cls,
        subscription: Subscription,
        action: str,
        *,
        actor: str,
        override: bool,
        orders: Optional[Sequence[Order]] = None,
    ) -> Optional[str]:
        """The rule of each action: the translation key of why it is refused now, or None."""
        status = subscription.status
        if action in ("skip", "pause", "create_next_order"):
            return None if status == SubscriptionStatus.ACTIVE else _REFUSED[action]
        if action == "resume":
            return None if status == SubscriptionStatus.PAUSED else _REFUSED[action]
        if action == "cancel":
            return None if status != SubscriptionStatus.CANCELLED else _REFUSED[action]
        if action in ("edit", "remove_item"):
            if status not in LIVE_SUBSCRIPTION_STATUSES and not (actor == ADMIN and override):
                return _REFUSED["edit"]
            if action == "remove_item" and len(subscription.subscription_items) <= 1:
                return _LAST_ITEM
            return None
        if action == "pay":
            return None if cls._payable_order(subscription, orders) is not None else _REFUSED[action]
        raise ValueError(f"Unknown subscription action {action!r}")

    @classmethod
    def _consequences(
        cls, subscription: Subscription, action: str, *, actor: str = CUSTOMER, orders: Optional[Sequence[Order]] = None
    ) -> Dict[str, Any]:
        """What an allowed action will do, computed by the same plan the action itself follows. The order
        lists have the shapes the action's own response returns (`cancelled_orders`, `kept_orders`). For the
        customer it also carries the finished sentences and labels (`subscription_display`), which clients render
        as they are; the admin payload is unchanged."""
        customer = actor == CUSTOMER
        if action == "pay":
            order = cls._payable_order(subscription, orders)
            pay = pay_action(order)
            published = {
                "order_id": order.id,
                "order_number": order.order_number,
                "date": _iso(order.delivery_date),
                "amount_label": display_labels.money(order.total_amount),
                # What the Pay write sends back: the order's published Pay (`customer_actions.pay`).
                "expected_amount": pay["expected_amount"],
            }
            if customer:
                published.update(label=pay["label"], note=subscription_display.pay_note(order, local_date()))
            return published
        if action == "skip":
            plan = cls._skip_plan(subscription, orders=orders)
            published = {
                "date": plan.date.isoformat(),
                # "Skip 30 October" / "Skip Thursday, 30 October? … Your next delivery will be …"
                "date_label": display_labels.day_month(plan.date),
                "label_long": display_labels.date_long(plan.date),
                "will_cancel_orders": [_order_ref(order) for order in plan.cancel],
                "kept_orders": [_kept_ref(order, code) for order, code in plan.kept],
                "next_date_after": _iso(plan.next_after),
                "next_date_after_label": display_labels.date_long(plan.next_after) if plan.next_after else None,
            }
            if customer:
                shown = next(iter(cls._shown_orders(cls._orders_of(subscription, orders))), None)
                confirm_text, keep_label = subscription_display.skip_confirm(plan, shown[0] if shown else None)
                published.update(
                    label=subscription_display.action_label(action),
                    confirm_text=confirm_text,
                    confirm_label=subscription_display.action_text("skip.do", date=display_labels.day_month(plan.date)),
                    keep_label=keep_label,
                    reasons=subscription_display.skip_reasons(),
                )
            return published
        if action in ("pause", "cancel"):
            plan = cls._settle_plan(subscription, orders=orders)
            published = {
                "will_cancel_orders": [_order_ref(order) for order, code in plan if code is None],
                "kept_orders": [_kept_ref(order, code) for order, code in plan if code is not None],
            }
            if customer:
                kept_lines, cancel_lines = subscription_display.settle_lines(plan)
                published.update(
                    label=subscription_display.action_label(action),
                    confirm_text=subscription_display.action_text(f"{action}.confirm"),
                    cancel_lines=cancel_lines,
                    kept_lines=kept_lines,
                    confirm_label=subscription_display.action_text("pause.do" if action == "pause" else "cancel.label"),
                    keep_label=subscription_display.action_text(f"{action}.keep"),
                )
                if action == "pause":
                    published["needs_confirm"] = bool(cancel_lines)
                else:
                    published["coins_line"] = subscription_display.action_text("cancel.coins")
            return published
        if action == "resume":
            preview = subscription_schedule.first_delivery(subscription, local_date())
            published = {
                "resume_preview": preview.isoformat(),
                "resume_preview_label": display_labels.date_long(preview),
            }
            if customer:
                published["label"] = subscription_display.action_label(action)
            return published
        return {}

    @classmethod
    def _payable_order(cls, subscription: Subscription, orders: Optional[Sequence[Order]] = None) -> Optional[Order]:
        """The order of this subscription the customer can pay now, by the retry-payment route's own rule
        (`payment_projection.can_pay`): the soonest live one first, then a delivered one still unpaid."""
        from business_app.utils.payment_projection import can_pay

        in_play = cls._orders_in_play(subscription.id) if orders is None else orders
        for order in [*in_play, *cls._unpaid_delivered(subscription.id)]:
            if can_pay(order):
                return order
        return None

    @staticmethod
    def _unpaid_delivered(subscription_id: int) -> List[Order]:
        """This subscription's delivered orders nobody has paid yet (a Click payment left waiting after the
        door), soonest first: Pay stays offered for them until they are paid."""
        from shared.enums import OrderStatus

        return (
            Order.query.filter(
                Order.subscription_id == subscription_id,
                Order.status == OrderStatus.DELIVERED,
                Order.is_paid.isnot(True),
            )
            .order_by(Order.delivery_date.is_(None), Order.delivery_date, Order.id)
            .all()
        )

    @classmethod
    def _skip_plan(cls, subscription: Subscription, *, orders: Optional[Sequence[Order]] = None) -> _SkipPlan:
        """What a skip does now (§9). An unpaid created order is cancelled: that skips its delivery, and the
        clock (already one cycle on since the order was created) stays, unless the order is dated on the clock
        (`_clock_after_skipping`). A paid one stays and is delivered (§2.7), so the clock moves on instead
        (`_advance_clock`), as it does when no order is created yet. An order the driver has is not the upcoming
        one: the skip applies from the following delivery (spec 2026-10-26 §12)."""
        from .order_service import OrderService

        upcoming = cls._upcoming_order(subscription.id, orders=orders)
        code = OrderService.customer_cancel_block_code(upcoming) if upcoming is not None else None
        if upcoming is not None and code is None:
            clock = cls._clock_after_skipping(subscription, upcoming.delivery_date)
            next_after = cls.next_delivery_after_skip(subscription, upcoming, orders=orders)
            return _SkipPlan(upcoming.delivery_date, (upcoming,), (), clock, next_after)
        day = subscription.next_delivery_date or subscription_schedule.first_delivery(subscription, local_date())
        clock = cls._advance_clock(subscription, day)
        kept = ((upcoming, code),) if upcoming is not None else ()
        # A kept paid order comes before the date skipped, so it is still the next delivery (spec 2026-10-26 §12).
        next_after = (
            upcoming.delivery_date
            if upcoming is not None
            else cls.next_delivery_after_skip(subscription, day, orders=orders)
        )
        return _SkipPlan(day, (), kept, clock, next_after)

    @classmethod
    def _clock_after_skipping(cls, subscription: Subscription, day: date) -> Optional[date]:
        """The clock after the customer skips the created order dated `day`, for both skips (Skip-next's
        `_skip_plan` and the order's own Skip, `skip_order_for_user`; spec 2026-10-27 §5.2, owner call 1). When
        `day` is the clock (staff re-dated the order onto it, or the clock was not moved past the order when it was
        created), it moves on (`_advance_clock`), so the daily job does not create the skipped date again. Otherwise
        it stays where it is, and steps over the skipped date when it gets there (spec 2026-10-27 §16 M1)."""
        clock = subscription.next_delivery_date
        return cls._advance_clock(subscription, day) if day == clock else clock

    @classmethod
    def _settle_plan(
        cls, subscription: Subscription, *, orders: Optional[Sequence[Order]] = None
    ) -> List[Tuple[Order, Optional[str]]]:
        """Pause and cancel (§4.6 with §2.7): each live order with the customer's own cancel rule's answer.
        None is cancelled (unpaid, the driver does not have it); any other code stays and is delivered."""
        from .order_service import OrderService

        return [
            (order, OrderService.customer_cancel_block_code(order))
            for order in (cls._orders_in_play(subscription.id) if orders is None else orders)
        ]

    @staticmethod
    def _ensure_schedule_valid(subscription: Subscription, *, slot_chosen: bool = True) -> None:
        """B9 (spec 2026-10-26 §8.2), on a real schedule change: the day a weekly, biweekly or monthly delivery
        needs, and a slot that covers it (`subscription_schedule.schedule_refusal`). `slot_chosen`: the change
        carried a slot (or null, Any time); when it did not and the stored slot no longer covers the schedule,
        the customer must choose (spec 2026-10-27 §6.2). Callers pass `"delivery_time_slot_id" in update_data`."""
        code = subscription_schedule.schedule_refusal(subscription, slot_chosen=slot_chosen)
        if code is not None:
            raise ValidationError(f"api.subscriptions.schedule.{code}", error_code="SUBSCRIPTION_SCHEDULE_INVALID")

    # ------------------------------------------------------------------ actions, one rule each (§4.6, §2.7)

    def _settle_orders_in_play(
        self, subscription: Subscription, *, reason: str, actor_user_id: Optional[int]
    ) -> Tuple[List[Order], List[Tuple[Order, str]]]:
        """Pause and cancel (§4.6 with §2.7), by `_settle_plan`: cancel this subscription's unpaid orders the
        driver does not have; every other live order stays and is delivered. The customer's own cancel rule
        decides again on locked rows, so a pickup or a payment that commits first wins."""
        from .order_service import OrderService

        order_service = OrderService()
        cancelled: List[Order] = []
        kept: List[Tuple[Order, str]] = []
        for order, code in self._settle_plan(subscription):
            if code is None:
                try:
                    order_service.cancel_order_as_customer(
                        order.id, subscription.user_id, reason=reason, actor_user_id=actor_user_id
                    )
                    cancelled.append(order)
                    continue
                except (ValidationError, ConflictError):
                    order = db.session.get(Order, order.id, populate_existing=True)
                    code = OrderService.customer_cancel_block_code(order)
            kept.append((order, code))
        return cancelled, kept

    def _paid_order_kept_logs(
        self, subscription: Subscription, kept: List[Tuple[Order, str]], trigger: str, next_day: Optional[date]
    ) -> List[SubscriptionLog]:
        """§2.7: a paid order stays and is delivered; the customer is owed a notice saying so."""
        from .order_service import CUSTOMER_CANCEL_PAID

        language = self._owner_language(subscription)
        logs = []
        for order, code in kept:
            if code != CUSTOMER_CANCEL_PAID:
                continue
            log = SubscriptionLog(
                subscription_id=subscription.id,
                action=subscription_notices.PAID_ORDER_KEPT,
                details=get_translation(
                    "api.subscriptions.log.paid_order_kept",
                    language,
                    order_number=order.order_number,
                    date=order.delivery_date.isoformat() if order.delivery_date else "",
                ),
                extra_data={
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
                    "trigger": trigger,
                    "next_delivery_date": next_day.isoformat() if next_day else None,
                },
            )
            db.session.add(log)
            logs.append(log)
        return logs

    @staticmethod
    def _action_outcome(
        subscription: Subscription, cancelled: List[Order], kept: List[Tuple[Order, str]]
    ) -> Dict[str, Any]:
        return {
            "subscription": subscription,
            "cancelled_orders": [_order_ref(order) for order in cancelled],
            "kept_orders": [{**_order_ref(order), "reason_code": code} for order, code in kept],
        }

    def _pause(
        self, subscription: Subscription, *, reason: str, details: str, actor_user_id: Optional[int]
    ) -> Dict[str, Any]:
        cancelled, kept = self._settle_orders_in_play(subscription, reason=reason, actor_user_id=actor_user_id)
        subscription.pause(reason=reason)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription.id,
                action="paused",
                details=details,
                user_id=actor_user_id,
                extra_data={
                    "cancelled_order_ids": [order.id for order in cancelled],
                    "kept_order_ids": [order.id for order, _ in kept],
                },
            )
        )
        notices = self._paid_order_kept_logs(subscription, kept, "pause", None)
        db.session.commit()
        self._deliver(notices)
        return self._action_outcome(subscription, cancelled, kept)

    def _resume(self, subscription: Subscription, *, details: str, actor_user_id: Optional[int]) -> Subscription:
        """§4.6: the next delivery is the first allowed one after the lead; inside the lead it is created now."""
        today = local_date()
        subscription.resume()
        subscription.pause_end_date = None
        subscription.next_delivery_date = subscription_schedule.first_delivery(subscription, today)
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(subscription_id=subscription.id, action="resumed", details=details, user_id=actor_user_id)
        )
        db.session.commit()
        self._create_first_order_if_due(subscription, today)
        return subscription

    def _cancel(
        self, subscription: Subscription, *, reason: str, details: str, actor_user_id: Optional[int]
    ) -> Dict[str, Any]:
        """§4.6: always immediate. A paid upcoming order still stays (§2.7)."""
        cancelled, kept = self._settle_orders_in_play(subscription, reason=reason, actor_user_id=actor_user_id)
        subscription.cancel(reason=reason)
        subscription.end_date = datetime.now(UTC)
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription.id,
                action="cancelled",
                details=details,
                user_id=actor_user_id,
                extra_data={
                    "cancelled_order_ids": [order.id for order in cancelled],
                    "kept_order_ids": [order.id for order, _ in kept],
                },
            )
        )
        notices = self._paid_order_kept_logs(subscription, kept, "cancel", None)
        db.session.commit()
        self._deliver(notices)
        return self._action_outcome(subscription, cancelled, kept)

    def _reschedule_after_change(self, subscription: Subscription, origin: Optional[date]) -> None:
        """The frequency, day or slot changed: the next delivery by the rule (§4.6, §9). The last created
        delivery (`last_delivery_date`, the newest order's date) stays as it is; while its order is still live
        (not delivered, cancelled or returned, the driver may already have it) the next one waits a full cycle
        after it, so a change never gives two deliveries back to back. `origin` is the clock's
        `subscription_schedule.clock_origin` read BEFORE the change: a date that was skipped never comes back
        (spec 2026-10-26 §12).

        Only a move steps over a date the customer skipped (M1): when the rule leaves the clock where it is, the
        clock stays, so a date a resume or the admin put back is still delivered after a change of slot alone."""
        before = subscription.next_delivery_date
        last = subscription.last_delivery_date
        moved = subscription_schedule.delivery_after_change(
            subscription,
            local_date(),
            last,
            order_created=last is not None and self._live_order_for(subscription.id, last, live_only=True) is not None,
            origin=origin,
        )
        subscription.next_delivery_date = (
            moved if moved == before else self._step_over_skips(subscription, moved)  # a forward move (M1)
        )

    # ------------------------------------------------------------------ slot writes (spec 2026-10-27 §6.5, §17 Q7)

    @classmethod
    def reconcile_with_slots(
        cls,
        *,
        changed: Optional[DeliveryTimeSlot] = None,
        before: Optional[subscription_schedule.SlotState] = None,
        before_delivery_days: Optional[FrozenSet[int]] = None,
        actor_user_id: Optional[int] = None,
    ) -> List[SubscriptionLog]:
        """Bring every live subscription in line with the slots after a slot write (`DeliveryTimeSlotService`), or once
        at deploy (`flask subscriptions reconcile-delivery-days`). Flushes and never commits; the caller commits and
        then hands the returned SCHEDULE_CHANGED rows to `_deliver`, one notice per changed subscription.

        `changed` is the slot the write edited and `before` its state before the write; `before_delivery_days` the
        delivery days before it. With no `changed` (a new slot, a deleted one, the deploy) only the day rules run;
        with no `before_delivery_days` (the deploy) no daily subscription is told of added days. Orders already
        created keep their date and window (§15 call 9).

        The delivery days are read once every live row is locked (`lock_live_subscriptions`), so a slot write
        that took the same locks first has committed and is seen whole."""
        live = cls.lock_live_subscriptions()
        delivery_days = subscription_schedule.delivery_weekdays()
        logs = []
        for subscription in live:
            facts = cls._schedule_after_slots(subscription, changed, before, before_delivery_days, delivery_days)
            if facts is None:
                continue
            subscription.updated_at = datetime.now(UTC)
            log = SubscriptionLog(
                subscription_id=subscription.id,
                action=subscription_notices.SCHEDULE_CHANGED,
                details=get_translation("api.subscriptions.log.schedule_changed", cls._owner_language(subscription)),
                user_id=actor_user_id,
                extra_data=facts,
            )
            db.session.add(log)
            logs.append(log)
        db.session.flush()
        return logs

    @staticmethod
    def lock_live_subscriptions() -> List[Subscription]:
        """Every live subscription, locked FOR UPDATE in ascending id order and read fresh: the first lock a slot
        write takes (`DeliveryTimeSlotService`), before the slot and before it reads the delivery days.

        One lock order, subscription before slot, as a schedule save takes them (the subscription, then the chosen
        slot FOR SHARE, `_chosen_slot`), so a save and a slot write queue instead of deadlocking. Two slot writes
        queue on the same rows, so each one reads the delivery days the other left."""
        return (
            Subscription.query.filter(Subscription.status.in_(LIVE_SUBSCRIPTION_STATUSES))
            .order_by(Subscription.id)
            .with_for_update()
            .populate_existing()
            .all()
        )

    @classmethod
    def _schedule_after_slots(
        cls,
        subscription: Subscription,
        changed: Optional[DeliveryTimeSlot],
        before: Optional[subscription_schedule.SlotState],
        before_delivery_days: Optional[FrozenSet[int]],
        delivery_days: FrozenSet[int],
    ) -> Optional[Dict[str, Any]]:
        """One subscription after a slot write: its slot, day and next delivery moved by §6.5, and the notice's
        facts (`subscription_notices` SCHEDULE_CHANGED), or None when nothing about it changed."""
        changes: Set[str] = set()
        facts: Dict[str, Any] = {
            "old_window": None,
            "new_window": None,
            "old_weekday": None,
            "new_weekday": None,
            "added_weekdays": [],
        }
        frequency = SubscriptionFrequency(getattr(subscription.frequency, "value", subscription.frequency))
        uses_changed = changed is not None and subscription.delivery_time_slot_id == changed.id
        had_no_slot = subscription.delivery_time_slot_id is None

        # 1. Its own slot was written. Off after the write: it becomes Any time (§6.5), even when the slot was off
        # before ("Time to be confirmed" until now; a slot switched off before the cascade existed left its rows
        # there), so every write to an off slot lets go of the rows still on it, once. Narrowed or retimed: only a
        # slot that was on gave it a time that changes.
        if uses_changed:
            after = subscription_schedule.SlotState.of(changed)
            if not after.is_active:
                cls._clear_slot(subscription, before if before is not None else after, changes, facts)
            elif before is not None and before.is_active:
                narrowed = after.days != before.days and not subscription_schedule.slot_serves(
                    subscription, changed, previous_days=before.days
                )
                if narrowed:
                    cls._clear_slot(subscription, before, changes, facts)
                elif (after.start_time, after.end_time) != (before.start_time, before.end_time):
                    changes.add(subscription_notices.TIME_CHANGED)
                    facts.update(old_window=before.window_label, new_window=after.window_label)

        # 2. A weekly or biweekly day that is no longer a delivery day moves to the next one (§6.4); a slot not
        # offered on that day is cleared with it.
        weekday = subscription.delivery_day_of_week
        if (
            frequency in (SubscriptionFrequency.WEEKLY, SubscriptionFrequency.BIWEEKLY)
            and weekday is not None
            and weekday not in delivery_days
        ):
            new_weekday = subscription_schedule.next_delivery_weekday(weekday, delivery_days)
            subscription.delivery_day_of_week = new_weekday
            changes.add(subscription_notices.DAY_MOVED)
            facts.update(old_weekday=weekday, new_weekday=new_weekday)
            slot = subscription.delivery_time_slot
            if slot is not None and not subscription_schedule.slot_offered_for(slot, frequency, new_weekday):
                cls._clear_slot(subscription, subscription_schedule.SlotState.of(slot), changes, facts)

        # 3. The next delivery, never earlier and in its own cycle. Only a move steps over a date the customer
        # skipped (M1).
        if subscription.status == SubscriptionStatus.ACTIVE and subscription.next_delivery_date is not None:
            old_next = subscription.next_delivery_date
            moved = subscription_schedule.delivery_after_days_changed(subscription, old_next)
            new_next = moved if moved == old_next else cls._step_over_skips(subscription, moved)
            if new_next != old_next:
                subscription.next_delivery_date = new_next
                if subscription_notices.DAY_MOVED not in changes:
                    changes.add(subscription_notices.DATE_MOVED)
                    facts["old_weekday"] = old_next.weekday()

        # 3b. A daily subscription delivers on more days: it is told (owner, 2026-10-27). One on another slot keeps
        # its slot's days; the deploy reconcile never tells.
        if (
            before_delivery_days is not None
            and frequency == SubscriptionFrequency.DAILY
            and (uses_changed or had_no_slot)
        ):
            old_days = (before.weekdays if uses_changed and before is not None else None) or before_delivery_days
            slot = subscription.delivery_time_slot
            slot_days = subscription_schedule.SlotState.of(slot).weekdays if slot is not None else None
            added = sorted((slot_days or delivery_days) - old_days)
            if added:
                changes.add(subscription_notices.DAYS_ADDED)
                facts["added_weekdays"] = added

        if not changes:
            return None
        if subscription_notices.TIME_CLEARED in changes:
            changes.discard(subscription_notices.TIME_CHANGED)  # the slot it changed is gone
        active = subscription.status == SubscriptionStatus.ACTIVE
        return {
            "subscription_id": subscription.id,
            "subscription_name": subscription.name,
            "changes": [code for code in subscription_notices.SCHEDULE_CHANGES if code in changes],
            **facts,
            "next_delivery_date": _iso(subscription.next_delivery_date) if active else None,
            "time_kind": subscription_display.time_kind(subscription),
        }

    @staticmethod
    def _clear_slot(
        subscription: Subscription, state: subscription_schedule.SlotState, changes: Set[str], facts: Dict[str, Any]
    ) -> None:
        """The subscription becomes Any time: its slot no longer serves it. The notice names the window it had."""
        subscription.delivery_time_slot = None
        subscription.delivery_time_slot_id = None
        changes.add(subscription_notices.TIME_CLEARED)
        facts.update(old_window=facts["old_window"] or state.window_label, new_window=None)

    @staticmethod
    def _chosen_slot(slot_id: int) -> Optional[DeliveryTimeSlot]:
        """A slot being chosen for a subscription, read FOR SHARE and fresh: a slot write (switch-off, narrowing,
        delete) waits for this save to commit and then sees it, or this save waits for the slot write and reads
        what it left (spec 2026-10-27 §6.5). Read after the subscription's own lock, the order a slot write takes
        them in (`lock_live_subscriptions`), so the two queue instead of deadlocking."""
        return DeliveryTimeSlot.query.filter_by(id=slot_id).with_for_update(read=True).populate_existing().first()

    def _set_schedule_field(self, subscription: Subscription, field: str, value: Any) -> None:
        """Write one schedule field so the rule reads the new value at once (the slot object, not only
        its id)."""
        if field == "delivery_time_slot_id":
            subscription.delivery_time_slot = db.session.get(DeliveryTimeSlot, value) if value else None
            subscription.delivery_time_slot_id = value
        else:
            setattr(subscription, field, value)

    def get_subscription_analytics(self, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
        """Get subscription analytics"""
        query = Subscription.query

        if start_date:
            query = query.filter(Subscription.created_at >= start_date)
        if end_date:
            query = query.filter(Subscription.created_at <= end_date)

        subscriptions = query.all()

        # Calculate metrics
        total_subscriptions = len(subscriptions)
        active_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.ACTIVE])
        trial_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.TRIAL])
        cancelled_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.CANCELLED])

        monthly_revenue = float(self.monthly_recurring_revenue(subscriptions))

        # Churn rate
        churn_rate = (cancelled_subscriptions / total_subscriptions) * 100 if total_subscriptions > 0 else 0

        # Average subscription value
        avg_subscription_value = (
            sum(float(s.billing_amount or 0) for s in subscriptions) / len(subscriptions) if subscriptions else 0
        )

        return {
            "total_subscriptions": total_subscriptions,
            "active_subscriptions": active_subscriptions,
            "trial_subscriptions": trial_subscriptions,
            "cancelled_subscriptions": cancelled_subscriptions,
            "churn_rate": round(churn_rate, 2),
            "monthly_recurring_revenue": monthly_revenue,
            "average_subscription_value": round(avg_subscription_value, 2),
            "frequency_breakdown": self._get_frequency_breakdown(subscriptions),
        }

    @staticmethod
    def frequency_filter(value: Optional[str]) -> Optional[SubscriptionFrequency]:
        """A subscription list's `?frequency=`, for the customer and the admin list alike: None when absent.
        An unknown value is the client's mistake (400), not a database error (500)."""
        if not value:
            return None
        try:
            return SubscriptionFrequency(value)
        except ValueError as exc:
            raise ValidationError("api.subscriptions.error.invalid_frequency_value") from exc

    def get_user_subscriptions_paginated(
        self,
        user_id: int,
        page: int,
        per_page: int,
        status: Optional[str] = None,
        frequency: Optional[str] = None,
    ):
        """Get user subscriptions with filtering and pagination."""
        query = Subscription.query.filter_by(user_id=user_id)

        if status:
            try:
                query = query.filter_by(status=SubscriptionStatus(status))
            except ValueError as exc:
                raise ValidationError("api.subscriptions.error.invalid_status_value") from exc

        frequency = self.frequency_filter(frequency)
        if frequency is not None:
            query = query.filter_by(frequency=frequency)

        return query.order_by(Subscription.created_at.desc()).paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )

    def get_subscription_details_for_user(self, subscription_id: int, user_id: int) -> Dict[str, Any]:
        """Get full subscription details for a specific user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        # This subscription's orders are its `/history` (spec 2026-10-26 §12): no second list here.
        return {"subscription": subscription, "billing_info": self.get_billing_info(subscription_id)}

    def create_subscription_for_user(self, user_id: int, validated_data: Any) -> Dict[str, Any]:
        """Validate ownership/dependencies and create a subscription for a user."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("user_not_found")

        address = UserAddress.query.filter_by(
            id=validated_data.delivery_address_id,
            user_id=user_id,
        ).first()
        if not address:
            raise NotFoundError("api.subscriptions.error.invalid_delivery_address")

        if validated_data.delivery_time_slot_id:
            time_slot = self._chosen_slot(validated_data.delivery_time_slot_id)
            if not time_slot or not time_slot.is_active:
                raise NotFoundError("api.subscriptions.error.invalid_or_inactive_time_slot")

        payment_method = self._validated_payment_method(validated_data.payment_method)

        subscription_data = {
            "user_id": user_id,
            "name": validated_data.name,
            "description": validated_data.description or "",
            "frequency": validated_data.frequency,
            "delivery_day_of_week": validated_data.delivery_day_of_week,
            "delivery_day_of_month": validated_data.delivery_day_of_month,
            "delivery_time_slot_id": validated_data.delivery_time_slot_id,
            "delivery_address_id": validated_data.delivery_address_id,
            "payment_method": payment_method,
            "auto_renew": validated_data.auto_renew,
            "start_date": validated_data.start_date or datetime.now(UTC),
            "end_date": validated_data.end_date,
        }

        return self.create_subscription(subscription_data, validated_data.items)

    def update_subscription_for_user(
        self,
        subscription_id: int,
        user_id: int,
        update_data: Dict[str, Any],
        *,
        expected_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a user-owned subscription. A real frequency, day or slot change must be a valid schedule (B9) and
        moves the next delivery by the rule (`_apply_schedule`, the very function the preview runs). A body that
        changes the schedule or the address carries the `expected_state` its screen showed (spec 2026-10-27 §16 M2);
        a rename needs none.

        The address change (spec 2026-10-27 §7, §17 Q6) is planned before anything is written
        (`_address_change_plan`, the preview's own plan). Its address must be the customer's, and the method (the
        one the body names, else the subscription's) must be on that address's menu (B8): a method rides this PUT
        only with an address, and when the current one is not offered there and none is named, the refusal
        SUBSCRIPTION_PAYMENT_METHOD_REQUIRED carries the new address's menu. After the commit, the orders the plan
        moves go to the new address through the admin address change's write (`_move_orders_to_address`).

        Refusal before staleness: every refusal answers first, in this order: the action, a method with no address,
        the address's owner, the method, the schedule (judged on a `_ScheduleDraft`, as the preview judges it). Only
        then is the token compared, on the untouched locked row.

        Returns the subscription, the orders that moved (`moved_orders`) and the ones that still go to their old
        address with their reason (`orders_at_old_address`)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "edit")
        user = subscription.user
        address, method = self._requested_address(subscription, update_data)
        plan = None
        if address is not None:
            plan = self._address_change_plan(subscription, address, actor=user, payment_method=method)
            if not plan.payment_method_offered:
                raise ValidationError(
                    _METHOD_REQUIRED,
                    details={"payment_methods": plan.payment_methods},
                    error_code="SUBSCRIPTION_PAYMENT_METHOD_REQUIRED",
                )
        schedule_sent = any(field in update_data for field in self._SCHEDULE_FIELDS)
        if schedule_sent:
            self._apply_schedule(_ScheduleDraft.of(subscription), update_data)
        if schedule_sent or "delivery_address_id" in update_data:
            self._require_state(subscription, expected_state)

        changes = {}
        for field, new_value in update_data.items():
            if field in self._SCHEDULE_FIELDS or field == "payment_method" or not hasattr(subscription, field):
                continue
            old_value = getattr(subscription, field)
            setattr(subscription, field, new_value)
            changes[field] = {"old": self._serialize_for_log(old_value), "new": self._serialize_for_log(new_value)}
        old_method = subscription.payment_method
        method_repicked = method is not None and method != old_method
        if method_repicked:
            self._set_payment_method(subscription, method, user_id=user_id)
        schedule_changed, schedule_log = self._apply_schedule(subscription, update_data)
        changes.update(schedule_log)

        subscription.updated_at = datetime.now(UTC)
        if changes:
            log = SubscriptionLog(
                subscription_id=subscription_id,
                action="updated",
                details=get_translation(
                    "api.subscriptions.log.updated_fields",
                    fields=", ".join(changes.keys()),
                ),
                user_id=user_id,
                extra_data={"changes": changes},
            )
            db.session.add(log)

        db.session.commit()
        if method_repicked:
            self._notify_payment_method_changed(subscription, old_method, method)
        moved, kept = self._move_orders_to_address(plan, actor=user) if plan is not None else ([], [])
        if schedule_changed:
            self._create_first_order_if_due(subscription, local_date())
        return {"subscription": subscription, "moved_orders": moved, "orders_at_old_address": kept}

    def _requested_address(
        self, subscription: Subscription, changes: Dict[str, Any]
    ) -> Tuple[Optional[UserAddress], Optional[PaymentMethod]]:
        """The address a customer's PUT or its preview names, and the method that rides with it (spec 2026-10-27 §7),
        refused in the save's order: a method with no address (only an address change re-picks it, B8), an address
        that is not this customer's (404), and a named method that is not selectable or not on the address's menu.
        `(None, None)` when no address is named."""
        if "payment_method" in changes and "delivery_address_id" not in changes:
            raise ValidationError("api.subscriptions.error.validation_failed")
        if "delivery_address_id" not in changes:
            return None, None
        address = UserAddress.query.filter_by(id=changes["delivery_address_id"], user_id=subscription.user_id).first()
        if address is None:
            raise NotFoundError("api.subscriptions.error.invalid_delivery_address")
        if "payment_method" not in changes:
            return address, None
        method = self._validated_payment_method(changes["payment_method"])
        self._ensure_payment_method_offered(subscription.user, method, address.id)
        return address, method

    def _address_change_plan(
        self,
        subscription: Subscription,
        address: UserAddress,
        *,
        actor: User,
        payment_method: Optional[PaymentMethod] = None,
    ) -> _AddressPlan:
        """What moving `subscription` to `address` does, read before anything is written (spec 2026-10-27 §17 Q6):
        THE plan the preview publishes and the PUT carries out. Every order in play with a date is asked
        `OrderAddressChangeService.customer_move_refusal` (a daily subscription can have two): None moves it, CURRENT
        (already there) leaves it out, any other code keeps it at its address. `payment_method_offered` is B8's answer
        (`method_offered`) for the named method, else the subscription's."""
        from .order_address_change_service import OrderAddressChangeService

        moves: List[Order] = []
        stays: List[Tuple[Order, str]] = []
        for order in self._orders_in_play(subscription.id):
            if order.delivery_date is None:
                continue
            code = OrderAddressChangeService.customer_move_refusal(order, address, actor=actor)
            if code is None:
                moves.append(order)
            elif code != "CURRENT":
                stays.append((order, code))
        method = payment_method or subscription.payment_method
        menu = self.offered_payment_methods(subscription.user, address.id)
        return _AddressPlan(
            address=address,
            moves=tuple(moves),
            stays=tuple(stays),
            payment_method_offered=self.menu_offers(menu, method),
            payment_methods=menu,
        )

    @staticmethod
    def _move_orders_to_address(
        plan: _AddressPlan, *, actor: User
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """After the subscription's commit: each order the plan moves goes to the new address through the admin
        address change's own write (`OrderAddressChangeService.change(for_customer=True)`, which owns its transaction,
        re-asks `customer_move_refusal` under its locks and tells the driver who accepted the stop). An order refused
        there (the driver picked it up meanwhile) stays with that function's code. Any other failure of one move (a
        Postgres deadlock abort while it waits on the locks, a fail-closed guard) is logged, and that order stays
        with MOVE_FAILED: the subscription is already saved at the new address, so the PUT still answers what
        happened, and the next moves are still tried. Returns the refs of the orders moved, and of the orders still
        at their old address (the plan's stays first) with their `reason_code`."""
        from .order_address_change_service import OrderAddressChangeService

        kept = [_kept_ref(order, code) for order, code in plan.stays]
        moved: List[Dict[str, Any]] = []
        address_id = plan.address.id
        for order in plan.moves:
            ref = _order_ref(order)
            try:
                OrderAddressChangeService.change(
                    order.id,
                    address_id=address_id,
                    note="subscription address change",
                    actor=actor,
                    for_customer=True,
                )
            except (ValidationError, ConflictError, ForbiddenError, NotFoundError) as refused:
                kept.append({**ref, "reason_code": refused.details.get("reason_code") or refused.error_code})
                continue
            except Exception:  # noqa: BLE001  (the subscription is committed; one order's failure must not hide it)
                # `change` rolled its own transaction back; this clears a session a failure past it left unusable.
                db.session.rollback()
                current_app.logger.exception(
                    "Subscription address change: moving order %s to address %s failed", ref["id"], address_id
                )
                kept.append({**ref, "reason_code": _MOVE_FAILED})
                continue
            moved.append(ref)
        return moved, kept

    def _apply_schedule(self, target, update_data: Dict[str, Any]) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
        """Apply `update_data`'s frequency, day and slot to `target`: THE one "apply a schedule change"
        (spec 2026-10-26 §5 B4). The save runs it on the subscription itself, the preview on a
        `_ScheduleDraft` (so it writes nothing): the preview's answer is the save's. The customer save also runs
        it on a draft first, so its refusals answer before a stale `expected_state` (spec 2026-10-27 §16 M2).

        A slot given must exist and be active; null is Any time (spec 2026-10-27 §6.1). A real change must be a
        valid schedule (B9) and moves the next delivery by the rule, counted from the clock's origin read BEFORE
        the change, so a skipped date never comes back (§12). Returns whether the schedule changed,
        and the log's old/new per field."""
        origin = subscription_schedule.clock_origin(target, target.next_delivery_date)
        changed = False
        log: Dict[str, Dict[str, Any]] = {}
        for field in self._SCHEDULE_FIELDS:
            if field not in update_data:
                continue
            new_value = update_data[field]
            old_value = getattr(target, field)
            if field == "delivery_time_slot_id" and new_value is not None:
                time_slot = self._chosen_slot(new_value)
                if not time_slot or not time_slot.is_active:
                    raise NotFoundError("api.subscriptions.error.invalid_or_inactive_time_slot")
            if field == "frequency":
                new_value = SubscriptionFrequency(new_value)
            changed = changed or new_value != old_value
            self._set_schedule_field(target, field, new_value)
            log[field] = {"old": self._serialize_for_log(old_value), "new": self._serialize_for_log(new_value)}
        if changed:
            self._ensure_schedule_valid(target, slot_chosen="delivery_time_slot_id" in update_data)
            self._reschedule_after_change(target, origin)
        return changed, log

    def _item_lines(self, subscription: Subscription, items: List[Dict[str, Any]]) -> List[Tuple[Product, int]]:
        """A whole replacement item list, validated as adding an item is (spec 2026-10-26 §5 B7): an active
        product at its minimum quantity, at least one item, at most MAX_SUBSCRIPTION_ITEMS, each product
        once. A product already in the subscription may stay after it was discontinued (§12); a new one must
        be active. Shared by the replace-all save and the preview, and refuses before anything changes."""
        if not items:
            raise ValidationError(_LAST_ITEM, error_code="SUBSCRIPTION_ACTION_NOT_ALLOWED")
        if len(items) > self.max_items:
            raise self._too_many_items()
        kept = {item.product_id for item in subscription.subscription_items}
        lines: List[Tuple[Product, int]] = []
        seen = set()
        for line in items:
            product_id, quantity = int(line["product_id"]), int(line["quantity"])
            if product_id in seen:
                raise ValidationError("api.subscriptions.error.product_already_exists")
            seen.add(product_id)
            product = db.session.get(Product, product_id)
            if product is None or not (product.is_active or product_id in kept):
                raise NotFoundError("api.subscriptions.error.product_not_found")
            self._ensure_meets_min_order_quantity(product, quantity)
            lines.append((product, quantity))
        return lines

    def _too_many_items(self) -> ValidationError:
        return ValidationError(get_translation("api.subscriptions.error.too_many_items", maximum=self.max_items))

    def replace_items_for_user(
        self,
        subscription_id: int,
        user_id: int,
        items: List[Dict[str, Any]],
        *,
        expected_state: Optional[str] = None,
    ) -> Subscription:
        """Replace the whole item list in one transaction (spec 2026-10-26 §5 B7: the website's Items save
        bar cannot half-apply). Every line is validated first (`_item_lines`), then the preview's
        `expected_state` (spec 2026-10-27 §16 M2); then a product that stays keeps its row (and its unit price), a
        new one is priced by the one rule and a dropped one is removed."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "edit")
        lines = self._item_lines(subscription, items)
        self._require_state(subscription, expected_state)

        before = {str(item.product_id): item.quantity for item in subscription.subscription_items}
        rows = {item.product_id: item for item in subscription.subscription_items}
        wanted = {product.id for product, _ in lines}
        for item in list(subscription.subscription_items):
            if item.product_id not in wanted:
                subscription.subscription_items.remove(item)
        for product, quantity in lines:
            item = rows.get(product.id)
            if item is None:
                item = SubscriptionItem(product_id=product.id, unit_price=self.customer_unit_price(product))
                subscription.subscription_items.append(item)
            item.quantity = quantity
            item.calculate_total()
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="updated",
                details=get_translation("api.subscriptions.log.updated_fields", fields="items"),
                user_id=user_id,
                extra_data={
                    "changes": {
                        "items": {"old": before, "new": {str(product.id): quantity for product, quantity in lines}}
                    }
                },
            )
        )
        db.session.commit()
        return subscription

    def preview_changes_for_user(self, subscription_id: int, user_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        """What a schedule, items or address change would give, BEFORE it is saved (spec 2026-10-26 §5 B4).

        Runs the save's own functions (`_apply_schedule` on a `_ScheduleDraft`, `_item_lines`, `_requested_address`
        and `_address_change_plan`), refuses what the save would refuse, and writes nothing. Returns the waterline,
        the first delivery with the change, the sentence, the deliveries a month, the estimate and, when a placed
        order stays as it is, the note that says so. With a `delivery_address_id` (spec 2026-10-27 §7, §17 Q6) it
        also returns `address_change` (the orders that move and stay, whether the method is offered there, and that
        address's menu) and `consequences`, the confirm's sentences. A method not offered there is not refused here:
        the confirm asks for one."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        self._require(subscription, "edit")
        address, method = self._requested_address(subscription, changes)
        draft = _ScheduleDraft.of(subscription)
        self._apply_schedule(draft, changes)
        lines = (
            self._item_lines(subscription, changes["items"])
            if "items" in changes
            else self._current_lines(subscription)
        )

        today = local_date()
        orders = self._orders_in_play(subscription.id)
        paused = draft.status != SubscriptionStatus.ACTIVE
        first_new = (
            subscription_schedule.first_delivery(draft, today)
            if paused or draft.next_delivery_date is None
            else draft.next_delivery_date
        )
        first_new_label = subscription_display.date_with_window(
            first_new, subscription_schedule.window_for(draft.delivery_time_slot, first_new)
        )
        kept_note = self.kept_order_note(orders)
        estimate = self._estimate_fields(lines, draft.frequency, draft.delivery_time_slot)
        address_change = {}
        if address is not None:
            plan = self._address_change_plan(subscription, address, actor=subscription.user, payment_method=method)
            address_change = {
                "address_change": {
                    "delivery_address_id": address.id,
                    "address_label": subscription_display.address_label(address),
                    "moves": [_order_ref(order) for order in plan.moves],
                    "stays": [_kept_ref(order, code) for order, code in plan.stays],
                    "payment_method_offered": plan.payment_method_offered,
                    "payment_methods": plan.payment_methods,
                },
                "consequences": subscription_display.address_consequences(plan, method or subscription.payment_method),
            }
        return {
            **address_change,
            "upcoming_dates": self.upcoming_dates(draft, orders=orders, today=today),
            "first_new_date": first_new.isoformat(),
            "first_new_date_label": first_new_label,
            "sentence": subscription_display.preview_sentence(
                first_new_label,
                paused,
                kept_note,
                subscription_schedule.deliveries_per_month(draft.frequency, draft.delivery_time_slot),
                estimate["estimated_per_month_label"],
            ),
            "schedule_sentence": subscription_display.schedule_sentence(draft),
            "time_kind": subscription_display.time_kind(draft),
            "time_label": subscription_display.time_label(draft),
            **estimate,
            "kept_order_note": kept_note,
            # What the save sends back: the state this preview was drawn from (spec 2026-10-27 §16 M2).
            "expected_state": self.state_token(subscription, orders=orders),
            # The drafted item list, each line labelled as a saved one is (the Items editor's rows).
            "items": [
                {
                    "product_id": product.id if product else None,
                    "quantity": quantity,
                    **self.line_labels(product, quantity),
                }
                for product, quantity in lines
            ],
        }

    def pause_subscription_for_user(
        self, subscription_id: int, user_id: int, reason: str, *, expected_state: Optional[str] = None
    ) -> Dict[str, Any]:
        """Pause a user-owned subscription (§4.6, §2.7): its unpaid upcoming orders are cancelled, a paid
        one stays. Returns the subscription and the orders cancelled and kept. `expected_state` is what the
        confirm showed (spec 2026-10-27 §16 M2)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "pause")
        self._require_state(subscription, expected_state)
        return self._pause(
            subscription,
            reason=reason,
            details=get_translation("api.subscriptions.log.reason", reason=reason),
            actor_user_id=subscription.user_id,
        )

    def resume_subscription_for_user(self, subscription_id: int, user_id: int) -> Subscription:
        """Resume a paused user-owned subscription (§4.6)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "resume")
        return self._resume(
            subscription, details=get_translation("api.subscriptions.log.resumed"), actor_user_id=subscription.user_id
        )

    def cancel_subscription_for_user(
        self, subscription_id: int, user_id: int, reason: str, *, expected_state: Optional[str] = None
    ) -> Dict[str, Any]:
        """Cancel a user-owned subscription, immediately (§4.6). A paid upcoming order stays (§2.7).
        `expected_state` is what the confirm showed (spec 2026-10-27 §16 M2)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "cancel")
        self._require_state(subscription, expected_state)
        return self._cancel(
            subscription,
            reason=reason,
            details=get_translation("api.subscriptions.log.cancelled_with_reason", reason=reason),
            actor_user_id=subscription.user_id,
        )

    def get_subscription_items_for_user(self, subscription_id: int, user_id: int) -> List[SubscriptionItem]:
        """Get subscription items for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        return subscription.subscription_items

    def add_subscription_item_for_user(
        self,
        subscription_id: int,
        user_id: int,
        product_id: int,
        quantity: int,
        special_instructions: Optional[str] = None,
        language: str = "en",
    ) -> Dict[str, Any]:
        """Add an item to a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "edit")

        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            raise NotFoundError("api.subscriptions.error.product_not_found")
        self._ensure_meets_min_order_quantity(product, quantity)
        if len(subscription.subscription_items) >= self.max_items:
            raise self._too_many_items()

        existing_item = SubscriptionItem.query.filter_by(
            subscription_id=subscription_id,
            product_id=product_id,
        ).first()
        if existing_item:
            raise ConflictError("api.subscriptions.error.product_already_exists")

        item = SubscriptionItem(
            subscription_id=subscription_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=self.customer_unit_price(product),
            special_instructions=special_instructions,
        )
        item.calculate_total()
        # Through the collection, so the total below counts the new row.
        subscription.subscription_items.append(item)
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)

        product_name = product.get_translated("name", language)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_added",
                details=get_translation(
                    "api.subscriptions.log.item_added",
                    quantity=quantity,
                    product=product_name,
                ),
                user_id=user_id,
            )
        )
        db.session.commit()

        return {
            "item": item,
            "billing_amount": subscription.billing_amount,
        }

    def update_subscription_item_for_user(
        self,
        subscription_id: int,
        item_id: int,
        user_id: int,
        quantity: int,
        special_instructions: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a subscription item for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "edit")

        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id,
        ).first()
        if not item:
            raise NotFoundError("api.subscriptions.error.item_not_found")
        if item.product is not None:
            self._ensure_meets_min_order_quantity(item.product, quantity)

        old_quantity = item.quantity
        item.quantity = quantity
        if special_instructions is not None:
            item.special_instructions = special_instructions

        item.calculate_total()
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)

        product_name = item.product.name if item.product else get_translation("api.subscriptions.unknown_product")
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_updated",
                details=get_translation(
                    "api.subscriptions.log.item_updated",
                    product=product_name,
                    old_quantity=old_quantity,
                    new_quantity=quantity,
                ),
                user_id=user_id,
            )
        )
        db.session.commit()

        return {
            "item": item,
            "billing_amount": subscription.billing_amount,
        }

    def remove_subscription_item_for_user(
        self,
        subscription_id: int,
        item_id: int,
        user_id: int,
    ) -> Subscription:
        """Remove an item from a user-owned subscription. The last item stays (the `remove_item` rule)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "remove_item")

        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id,
        ).first()
        if not item:
            raise NotFoundError("api.subscriptions.error.item_not_found")

        product_name = item.product.name if item.product else get_translation("api.subscriptions.unknown_product")
        # Through the collection (delete-orphan), so the total below no longer counts the removed row.
        subscription.subscription_items.remove(item)
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_removed",
                details=get_translation("api.subscriptions.log.item_removed", product=product_name),
                user_id=user_id,
            )
        )
        db.session.commit()
        return subscription

    def subscription_history_for_user(
        self, subscription_id: int, user_id: int, page: int, per_page: int
    ) -> Tuple[List[Dict[str, Any]], int]:
        """The Orders section (spec 2026-10-26 §5 B6, §12): ONE list, newest first, merged here.

        - Each order is a row with its chip (`subscription_display.history_chip`), amount and the order's
          `customer_actions` (the one pay rule, which the retry-payment route enforces); its `can_pay` is that
          published Pay's `allowed`. A skipped row has no order to act on: `customer_actions` is None.
        - A customer skip is one row per skipped date with its reason; a skipped unpaid order is ONE
          "Skipped" row that carries the cancelled order's number, not a second "Cancelled" row.
        - A system skip (a date passed, out of stock, the cash-debt limit) is one row per date.

        Returns the page's rows and the total of every row."""
        self._get_user_subscription_or_raise(subscription_id, user_id)
        orders = {order.id: order for order in Order.query.filter_by(subscription_id=subscription_id).all()}
        rows: List[Tuple[date, int, int, Dict[str, Any]]] = []
        skipped_order_ids = set()
        for log in self._skip_logs(subscription_id):
            extra = log.extra_data or {}
            cancelled = [orders[i] for i in extra.get("cancelled_order_ids") or () if i in orders]
            skipped_order_ids.update(order.id for order in cancelled)
            reason = self._history_skip_reason(extra)
            for day in self._skip_log_dates(log):
                order = cancelled[0] if cancelled else None
                rows.append(
                    (
                        day,
                        0,
                        log.id,
                        {
                            "date": day.isoformat(),
                            "date_label": display_labels.date_short(day),
                            "kind": "skipped",
                            "order_id": order.id if order else None,
                            "order_number": order.order_number if order else None,
                            "status": subscription_display.chip("skipped"),
                            "amount_label": None,
                            "can_pay": False,
                            "customer_actions": None,
                            "reason_label": reason,
                        },
                    )
                )
        for order in orders.values():
            if order.id in skipped_order_ids:
                continue
            day = order.delivery_date or local_date(order.created_at)
            actions = customer_actions(order)
            rows.append(
                (
                    day,
                    1,
                    order.id,
                    {
                        "date": day.isoformat(),
                        "date_label": display_labels.date_short(day),
                        "kind": "order",
                        "order_id": order.id,
                        "order_number": order.order_number,
                        "status": subscription_display.history_chip(order),
                        "amount_label": display_labels.money(order.total_amount),
                        "can_pay": actions["pay"]["allowed"],
                        "customer_actions": actions,
                        "reason_label": None,
                    },
                )
            )
        rows.sort(key=lambda row: row[:3], reverse=True)
        start = (page - 1) * per_page
        return [row[3] for row in rows[start : start + per_page]], len(rows)

    @staticmethod
    def _history_skip_reason(extra: Dict[str, Any]) -> Optional[str]:
        """Why a delivery was skipped, in the reader's language: the customer's own words for "other", the
        customer's chip, or the system's reason. None when the customer gave none."""
        if extra.get("skipped_by") == "customer" and extra.get("reason_text"):
            return extra["reason_text"]
        code = extra.get("reason_code")
        return get_translation(f"api.subscriptions.skip_reason.{code}") if code else None

    def get_subscription_logs_paginated_for_user(
        self,
        subscription_id: int,
        user_id: int,
        page: int,
        per_page: int,
    ):
        """Get paginated logs for a user-owned subscription."""
        self._get_user_subscription_or_raise(subscription_id, user_id)
        return (
            SubscriptionLog.query.filter_by(subscription_id=subscription_id)
            .order_by(SubscriptionLog.created_at.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )

    def get_subscription_statistics_for_user(self, user_id: int, language: str = "en") -> Dict[str, Any]:
        """Calculate subscription statistics for a user."""
        subscriptions = Subscription.query.filter_by(user_id=user_id).all()

        total_subscriptions = len(subscriptions)
        active_subscriptions = len(
            [subscription for subscription in subscriptions if subscription.status == SubscriptionStatus.ACTIVE]
        )
        paused_subscriptions = len(
            [subscription for subscription in subscriptions if subscription.status == SubscriptionStatus.PAUSED]
        )
        cancelled_subscriptions = len(
            [subscription for subscription in subscriptions if subscription.status == SubscriptionStatus.CANCELLED]
        )

        total_spent = sum((subscription.total_amount_billed or 0) for subscription in subscriptions)
        total_savings = sum(
            (subscription.total_amount_billed or 0) * (subscription.discount_percentage / 100)
            for subscription in subscriptions
            if subscription.discount_percentage and subscription.discount_percentage > 0
        )
        total_deliveries = sum((subscription.total_orders_generated or 0) for subscription in subscriptions)

        average_order_value = 0.0
        if total_deliveries > 0:
            average_order_value = round(float(total_spent) / total_deliveries, 2)

        product_counts = {}
        for subscription in subscriptions:
            for item in subscription.subscription_items:
                if item.product:
                    product_counts[item.product_id] = product_counts.get(item.product_id, 0) + item.quantity

        most_ordered_product = None
        if product_counts:
            most_popular_product_id = max(product_counts, key=product_counts.get)
            for subscription in subscriptions:
                for item in subscription.subscription_items:
                    if item.product_id == most_popular_product_id and item.product:
                        most_ordered_product = item.product.get_translated("name", language)
                        break
                if most_ordered_product:
                    break

        monthly_spending = {}
        delivery_days = subscription_schedule.delivery_weekdays()  # once for every month and row below
        for i in range(12):
            month_start = (datetime.now(UTC).replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            month_key = month_start.strftime("%Y-%m")

            month_total = Decimal("0.00")
            for subscription in subscriptions:
                if subscription.created_at.date() <= month_start.date() and (
                    not subscription.end_date or subscription.end_date.date() >= month_start.date()
                ):
                    month_total += self.monthly_value(subscription, delivery_days=delivery_days)

            monthly_spending[month_key] = float(month_total)

        return {
            "total_subscriptions": total_subscriptions,
            "active_subscriptions": active_subscriptions,
            "paused_subscriptions": paused_subscriptions,
            "cancelled_subscriptions": cancelled_subscriptions,
            "total_spent": float(total_spent),
            "total_savings": float(total_savings),
            "total_deliveries": total_deliveries,
            "average_order_value": float(average_order_value),
            "most_ordered_product": most_ordered_product,
            # Every active subscription has exactly one upcoming delivery.
            "upcoming_deliveries": active_subscriptions,
            "monthly_spending_trend": monthly_spending,
        }

    def skip_next_delivery_for_user(
        self,
        subscription_id: int,
        user_id: int,
        *,
        expected_date: date,
        reason_code: Optional[str] = None,
        reason_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Skip the upcoming delivery (§9), by the plan `allowed_actions.skip` publishes (`_skip_plan`).

        An unpaid created order is cancelled: that skips its delivery, and the clock (already one cycle on
        since the order was created) stays, unless the order is dated on the clock, when it moves on as the
        order's own Skip moves it (`_clock_after_skipping`). A paid one stays and is delivered (§2.7), so the
        clock moves on instead (`_advance_clock`), as it does when no order is created yet. The clock is set
        before the cancel, so the cancel's commit carries it and a refused cancel's rollback discards it.
        `new_next_delivery_date` is the upcoming delivery after the skip.

        `expected_date` is the date the customer's button named (spec 2026-10-26 §12). When the skip would now
        land on another date (the order was paid, picked up or skipped meanwhile), nothing is skipped and a
        StateChangedError says so: "This changed. Refresh to see the latest." (spec 2026-10-27 §16 M2).
        """
        from .order_service import OrderService

        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "skip")
        plan = self._skip_plan(subscription)
        if plan.date != expected_date:
            raise StateChangedError(details={"expected_date": expected_date.isoformat()})
        reason = self._skip_reason_label(reason_code, reason_text)

        subscription.next_delivery_date = plan.next_delivery
        cancelled: List[Order] = list(plan.cancel)
        for order in cancelled:
            try:
                OrderService().cancel_order_as_customer(order.id, subscription.user_id, reason=reason)
            except (ValidationError, ConflictError) as exc:
                # A pickup or a payment committed first: the skip would now land on another date. The refusal
                # rolled back and wrote nothing.
                raise StateChangedError(details={"expected_date": expected_date.isoformat()}) from exc
        kept = list(plan.kept)
        subscription.updated_at = datetime.now(UTC)
        db.session.flush()
        new_next = plan.next_after

        self._record_customer_skip(
            subscription,
            day=plan.date,
            new_next=new_next,
            reason=reason,
            reason_code=reason_code,
            reason_text=reason_text,
            cancelled_order_ids=[order.id for order in cancelled],
        )
        notices = self._paid_order_kept_logs(subscription, kept, "skip", subscription.next_delivery_date)
        db.session.commit()
        self._deliver(notices)

        return {
            **self._action_outcome(subscription, cancelled, kept),
            "original_delivery_date": plan.date,
            "new_next_delivery_date": new_next,
            "reason": reason,
        }

    def skip_order_for_user(
        self,
        order_id: int,
        user_id: int,
        *,
        reason_code: Optional[str] = None,
        reason_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Skip one order of an ACTIVE subscription, the customer's "Skip this delivery" on that order (spec
        2026-10-27 §5.2, D10): the order is cancelled and one customer `delivery_skipped` log records its date and
        reason, so the history shows one "Skipped" row. Any order the driver does not have, not only the soonest.

        The clock moves on only when the order is dated on it (`_clock_after_skipping`, the rule Skip-next follows
        too); otherwise it stays, and steps over the skipped date when it gets there. The log and the clock are
        added to the session before the cancel, so the cancel's commit carries all three and a refusal's rollback
        discards all three. The caller (`OrderService.remove_order_as_customer`) has locked the subscription, the
        delivery and the order and checked the cancel rule. Returns the subscription, the cancelled order, the date
        skipped, the next delivery after it (`next_delivery_after_skip`) and the reason's words."""
        from .order_service import OrderService

        order = db.session.get(Order, order_id)
        if order is None or order.user_id != user_id or order.subscription_id is None:
            raise NotFoundError("Order not found")
        subscription = self._get_user_subscription_or_raise(order.subscription_id, user_id, lock=True)
        self._require(subscription, "skip")
        reason = self._skip_reason_label(reason_code, reason_text)
        day = order.delivery_date
        subscription.next_delivery_date = self._clock_after_skipping(subscription, day)
        new_next = self.next_delivery_after_skip(subscription, order)
        subscription.updated_at = datetime.now(UTC)
        self._record_customer_skip(
            subscription,
            day=day,
            new_next=new_next,
            reason=reason,
            reason_code=reason_code,
            reason_text=reason_text,
            cancelled_order_ids=[order.id],
        )
        order = OrderService().cancel_order_as_customer(order.id, subscription.user_id, reason=reason)
        return {
            "subscription": subscription,
            "order": order,
            "original_delivery_date": day,
            "new_next_delivery_date": new_next,
            "reason": reason,
        }

    def _record_customer_skip(
        self,
        subscription: Subscription,
        *,
        day: date,
        new_next: Optional[date],
        reason: str,
        reason_code: Optional[str],
        reason_text: Optional[str],
        cancelled_order_ids: List[int],
    ) -> SubscriptionLog:
        """The customer's skip of `day`, as the one `delivery_skipped` row the history and the waterline read
        (`_skip_log_dates`) and the clock steps over (`_customer_skipped_dates`), added to the session. The caller
        commits it."""
        log = SubscriptionLog(
            subscription_id=subscription.id,
            action=subscription_notices.DELIVERY_SKIPPED,
            details=get_translation("api.subscriptions.log.delivery_skipped", date=day.isoformat(), reason=reason),
            user_id=subscription.user_id,
            extra_data={
                "skipped_by": "customer",
                "original_delivery_date": day.isoformat(),
                "new_delivery_date": new_next.isoformat() if new_next else None,
                "reason": reason,
                "reason_code": reason_code,
                "reason_text": reason_text,
                "cancelled_order_ids": cancelled_order_ids,
            },
        )
        db.session.add(log)
        return log

    @staticmethod
    def skip_notice_data(
        subscription: Subscription, *, skipped_date: date, next_date: Optional[date], reason: str
    ) -> Dict[str, Any]:
        """The "delivery_skipped" notification's template data, for both skips (Skip-next and the order's own
        Skip), in the request's language."""
        from business_app.utils.helpers import get_current_language

        return {
            "subscription_name": subscription.get_translated("name", get_current_language()),
            "skipped_date": skipped_date.isoformat(),
            "next_delivery_date": next_date.isoformat() if next_date else None,
            "reason": reason,
        }

    @staticmethod
    def _skip_reason_label(reason_code: Optional[str], reason_text: Optional[str]) -> str:
        """What the skip's log line and notification say: the customer's own words for "other", else the
        translated reason chip, else the default."""
        if reason_text:
            return reason_text
        if reason_code:
            return get_translation(f"api.subscriptions.skip_reason.{reason_code}")
        return get_translation("api.subscriptions.default_reason_customer_request")

    def change_payment_method_for_user(
        self,
        subscription_id: int,
        user_id: int,
        payment_method: str,
        *,
        expected_state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Change payment method for a user-owned subscription: the only customer writer of the method, and
        only to one the payment menu offers for this subscription's address (spec 2026-10-26 B8). Then the
        `expected_state` the screen showed (spec 2026-10-27 §16 M2), on the locked row."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        self._require(subscription, "edit")

        new_payment_method = self._validated_payment_method(payment_method)
        self._ensure_payment_method_offered(subscription.user, new_payment_method, subscription.delivery_address_id)
        self._require_state(subscription, expected_state)

        old_payment_method = subscription.payment_method
        self._set_payment_method(subscription, new_payment_method, user_id=user_id)
        subscription.updated_at = datetime.now(UTC)
        db.session.commit()
        self._notify_payment_method_changed(subscription, old_payment_method, new_payment_method)

        return {
            "subscription": subscription,
            "old_payment_method": old_payment_method,
            "new_payment_method": new_payment_method,
        }

    @staticmethod
    def _notify_payment_method_changed(
        subscription: Subscription, old_method: PaymentMethod, new_method: PaymentMethod
    ) -> None:
        """The one `payment_method_changed` notice, in the request's language. Both customer writers of the method
        (`change_payment_method_for_user` and the address PUT's re-pick) call it AFTER their commit, so nothing is
        published from inside the transaction that writes the row."""
        from business_app.utils.helpers import get_current_language

        get_notification_service().send_notification(
            subscription.user_id,
            "payment_method_changed",
            template_data={
                "subscription_name": subscription.get_translated("name", get_current_language()),
                "old_method": old_method.value,
                "new_method": new_method.value,
            },
        )

    @staticmethod
    def _set_payment_method(subscription: Subscription, new_method: PaymentMethod, *, user_id) -> None:
        """Set the customer's new method and log it (`payment_method_changed`): the method change and an address
        change that re-picks it (B8) write it this one way. The caller commits."""
        old_method = subscription.payment_method
        subscription.payment_method = new_method
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription.id,
                action="payment_method_changed",
                details=get_translation(
                    "api.subscriptions.log.payment_method_changed",
                    old_method=old_method.value,
                    new_method=new_method.value,
                ),
                user_id=user_id,
            )
        )

    @staticmethod
    def _fetch(query, *, lock: bool = False) -> Optional[Subscription]:
        """The one way a subscription row is read for a change. `lock` takes it FOR UPDATE, fresh from the
        database, so a skip, pause, resume, cancel or change serialises with the daily run, which locks the
        same row (§4.3), instead of racing it."""
        if lock:
            query = query.with_for_update().populate_existing()
        return query.first()

    def _get_subscription_or_raise(self, subscription_id: int, *, lock: bool = False) -> Subscription:
        """Load a subscription by id without an ownership filter (admin scope)."""
        subscription = self._fetch(Subscription.query.filter_by(id=subscription_id), lock=lock)
        if not subscription:
            raise NotFoundError("Subscription not found")
        return subscription

    @staticmethod
    def _ensure_utc(value: Any) -> Any:
        """Make a naive datetime tz-aware (UTC); pass through anything else."""
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def admin_create_subscription(self, validated_data: Any, actor_user_id: int) -> Dict[str, Any]:
        """Admin: create a subscription for any user.

        Reuses ``create_subscription`` (which validates items, computes the
        first delivery by the schedule rule and commits), then attaches an
        audit-log row stamped with the acting admin. Validates that the
        delivery address belongs to the target user (``create_subscription``
        does not).
        """
        user = User.query.get(validated_data.user_id)
        if not user:
            raise NotFoundError("User not found")

        address = UserAddress.query.filter_by(
            id=validated_data.delivery_address_id,
            user_id=validated_data.user_id,
        ).first()
        if not address:
            raise NotFoundError("Delivery address not found for this user")

        if validated_data.delivery_time_slot_id:
            time_slot = self._chosen_slot(validated_data.delivery_time_slot_id)
            if not time_slot or not time_slot.is_active:
                raise NotFoundError("Delivery time slot not found or inactive")

        payment_method = self._validated_payment_method(validated_data.payment_method)

        subscription_data = {
            "user_id": validated_data.user_id,
            "name": validated_data.name,
            "description": validated_data.description or "",
            "frequency": validated_data.frequency,
            "delivery_day_of_week": validated_data.delivery_day_of_week,
            "delivery_day_of_month": validated_data.delivery_day_of_month,
            "delivery_time_slot_id": validated_data.delivery_time_slot_id,
            "delivery_address_id": validated_data.delivery_address_id,
            "payment_method": payment_method,
            "auto_renew": validated_data.auto_renew,
            "start_date": (
                self._ensure_utc(validated_data.start_date) if validated_data.start_date else datetime.now(UTC)
            ),
            "end_date": self._ensure_utc(validated_data.end_date) if validated_data.end_date else None,
        }

        result = self.create_subscription(
            subscription_data, validated_data.items, discount_percentage=validated_data.discount_percentage
        )

        # create_subscription commits and returns to_dict(); attach audit + any
        # loyalty multiplier override, keyed off the unique subscription number.
        subscription = Subscription.query.filter_by(subscription_number=result["subscription_number"]).first()
        if subscription is not None:
            if getattr(validated_data, "loyalty_points_multiplier", None) is not None:
                subscription.loyalty_points_multiplier = validated_data.loyalty_points_multiplier
            db.session.add(
                SubscriptionLog(
                    subscription_id=subscription.id,
                    action="created",
                    details="Created by admin",
                    user_id=actor_user_id,
                )
            )
            db.session.commit()

        return result

    def admin_update_subscription(
        self,
        subscription_id: int,
        update_data: Dict[str, Any],
        actor_user_id: int,
        overrides: Optional[Dict[str, bool]] = None,
    ) -> Subscription:
        """Admin: update subscription fields with optional warned overrides.

        By default only ACTIVE/PAUSED subscriptions are editable and the
        override-gated fields (billing_amount, the next delivery date) are
        dropped. ``overrides`` re-enables them:
        ``edit_any_status`` / ``manual_billing_amount`` / ``manual_billing_dates``.

        One frequency (spec 2026-10-23 §2.1). A real change of frequency, day or slot moves the next delivery
        by the rule (§4.6, §9); "Set next delivery date" is checked by the rule (§9).
        """
        overrides = overrides or {}
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "edit", actor=ADMIN, override=bool(overrides.get("edit_any_status")))
        origin = subscription_schedule.clock_origin(subscription, subscription.next_delivery_date)

        # The override was needed: without it the edit rule refuses.
        status_override_used = bool(overrides.get("edit_any_status")) and (
            self._action_refusal(subscription, "edit", actor=ADMIN, override=False) is not None
        )

        # Drop override-gated fields whose flag is not set.
        data = dict(update_data)
        for field, flag in self._ADMIN_OVERRIDE_FIELDS.items():
            if field in data and not overrides.get(flag):
                data.pop(field)

        # Never editable through this method (status changes go via lifecycle actions).
        for field in self._ADMIN_PROTECTED_FIELDS:
            data.pop(field, None)

        next_delivery = data.pop("next_delivery_date", None)

        changes: Dict[str, Any] = {}
        override_changes: Dict[str, Any] = {}
        schedule_changed = False
        payment_method_changed = False
        address_changed = False
        if status_override_used:
            override_changes["edit_any_status"] = {"status_at_edit": self._serialize_for_log(subscription.status)}
        for field, new_value in data.items():
            if not hasattr(subscription, field):
                continue
            old_value = getattr(subscription, field)

            if field == "delivery_address_id":
                address = UserAddress.query.filter_by(id=new_value, user_id=subscription.user_id).first()
                if not address:
                    raise NotFoundError("Delivery address not found for this user")
                # The admin form resends the address too: only a real move is re-checked (B8, below).
                address_changed = address_changed or new_value != old_value

            # The admin form resends the stored slot too, and keeps one an admin later deactivated: only a slot
            # being CHOSEN must be active (re-review admin-bot-backend-3), as the address and method checks.
            # Null is Any time: it clears the slot and is not checked.
            if field == "delivery_time_slot_id" and new_value is not None and new_value != old_value:
                time_slot = self._chosen_slot(new_value)
                if not time_slot or not time_slot.is_active:
                    raise NotFoundError("Delivery time slot not found or inactive")

            if field == "payment_method":
                new_value = self._validated_payment_method(new_value)
                # The admin form resends every field: an unchanged method is not a change to guard.
                payment_method_changed = payment_method_changed or new_value != old_value

            new_value = self._ensure_utc(new_value)

            if field in self._SCHEDULE_FIELDS:
                schedule_changed = schedule_changed or new_value != old_value
                self._set_schedule_field(subscription, field, new_value)
            else:
                setattr(subscription, field, new_value)
            entry = {"old": self._serialize_for_log(old_value), "new": self._serialize_for_log(new_value)}
            changes[field] = entry
            if field in self._ADMIN_OVERRIDE_FIELDS:
                override_changes[field] = entry

        # The customer's rules for a changed method or address (B8: the method must be on THIS address's
        # menu) and a changed schedule (B9), on the saved values.
        if payment_method_changed or address_changed:
            self._ensure_payment_method_offered(
                subscription.user, subscription.payment_method, subscription.delivery_address_id
            )
        if schedule_changed:
            self._ensure_schedule_valid(subscription, slot_chosen="delivery_time_slot_id" in data)

        if next_delivery is not None:
            try:
                subscription_schedule.validate_next_delivery(subscription, next_delivery, local_date())
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
            entry = {
                "old": self._serialize_for_log(subscription.next_delivery_date),
                "new": self._serialize_for_log(next_delivery),
            }
            subscription.next_delivery_date = next_delivery
            changes["next_delivery_date"] = override_changes["next_delivery_date"] = entry
            schedule_changed = True
        elif schedule_changed:
            self._reschedule_after_change(subscription, origin)

        subscription.updated_at = datetime.now(UTC)
        if changes:
            db.session.add(
                SubscriptionLog(
                    subscription_id=subscription_id,
                    action="updated",
                    details="Updated fields: " + ", ".join(changes.keys()),
                    user_id=actor_user_id,
                    extra_data={"changes": changes},
                )
            )
        if override_changes:
            db.session.add(
                SubscriptionLog(
                    subscription_id=subscription_id,
                    action="admin_override",
                    details="Admin override applied: " + ", ".join(override_changes.keys()),
                    user_id=actor_user_id,
                    extra_data={"overrides": override_changes},
                )
            )
        db.session.commit()
        if schedule_changed:
            self._create_first_order_if_due(subscription, local_date())
        return subscription

    def admin_add_item(
        self,
        subscription_id: int,
        product_id: int,
        quantity: int,
        special_instructions: Optional[str],
        actor_user_id: int,
    ) -> Dict[str, Any]:
        """Admin: add an item to a subscription (re-derives billing_amount)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "edit", actor=ADMIN)

        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            raise NotFoundError("Product not found or inactive")
        self._ensure_meets_min_order_quantity(product, quantity)
        if len(subscription.subscription_items) >= self.max_items:
            raise self._too_many_items()

        existing = SubscriptionItem.query.filter_by(subscription_id=subscription_id, product_id=product_id).first()
        if existing:
            raise ConflictError("This product is already in the subscription")

        item = SubscriptionItem(
            subscription_id=subscription_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=self.customer_unit_price(product),
            special_instructions=special_instructions,
        )
        item.calculate_total()
        # Through the collection, so the total below counts the new row.
        subscription.subscription_items.append(item)
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_added",
                details=f"Added {quantity} x {product.name}",
                user_id=actor_user_id,
            )
        )
        db.session.commit()
        return {"item": item, "billing_amount": subscription.billing_amount}

    def admin_update_item(
        self,
        subscription_id: int,
        item_id: int,
        quantity: int,
        special_instructions: Optional[str],
        actor_user_id: int,
    ) -> Dict[str, Any]:
        """Admin: update a subscription item (re-derives billing_amount)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "edit", actor=ADMIN)

        item = SubscriptionItem.query.filter_by(id=item_id, subscription_id=subscription_id).first()
        if not item:
            raise NotFoundError("Subscription item not found")
        if item.product is not None:
            self._ensure_meets_min_order_quantity(item.product, quantity)

        old_quantity = item.quantity
        item.quantity = quantity
        if special_instructions is not None:
            item.special_instructions = special_instructions
        item.calculate_total()
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        product_name = item.product.name if item.product else "item"
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_updated",
                details=f"Updated {product_name}: qty {old_quantity} -> {quantity}",
                user_id=actor_user_id,
            )
        )
        db.session.commit()
        return {"item": item, "billing_amount": subscription.billing_amount}

    def admin_remove_item(
        self,
        subscription_id: int,
        item_id: int,
        actor_user_id: int,
    ) -> Subscription:
        """Admin: remove a subscription item (cannot remove the last one: the `remove_item` rule)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "remove_item", actor=ADMIN)

        item = SubscriptionItem.query.filter_by(id=item_id, subscription_id=subscription_id).first()
        if not item:
            raise NotFoundError("Subscription item not found")

        product_name = item.product.name if item.product else "item"
        # Through the collection (delete-orphan), so the total below no longer counts the removed row.
        subscription.subscription_items.remove(item)
        db.session.flush()

        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="item_removed",
                details=f"Removed {product_name}",
                user_id=actor_user_id,
            )
        )
        db.session.commit()
        return subscription

    def admin_pause_subscription(
        self, subscription_id: int, actor_user_id: int, reason: Optional[str]
    ) -> Dict[str, Any]:
        """Admin: pause an ACTIVE subscription, by the customer's rule (§4.6): unpaid upcoming orders are
        cancelled, a paid one stays. Audit-logged with the acting admin."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "pause", actor=ADMIN)
        # The reason is optional (a bulk request may send `"reason": null`): no reason logs none, never "None".
        details = f"Paused by admin: {reason}" if reason else "Paused by admin"
        return self._pause(subscription, reason=reason or None, details=details, actor_user_id=actor_user_id)

    def admin_resume_subscription(self, subscription_id: int, actor_user_id: int) -> Subscription:
        """Admin: resume a PAUSED subscription, by the customer's rule (§4.6)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "resume", actor=ADMIN)
        return self._resume(subscription, details="Resumed by admin", actor_user_id=actor_user_id)

    def admin_cancel_subscription(
        self, subscription_id: int, actor_user_id: int, reason: Optional[str]
    ) -> Dict[str, Any]:
        """Admin: cancel a subscription, immediately, by the customer's rule (§4.6, §2.7)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        self._require(subscription, "cancel", actor=ADMIN)
        details = f"Cancelled by admin: {reason}" if reason else "Cancelled by admin"
        return self._cancel(subscription, reason=reason or None, details=details, actor_user_id=actor_user_id)

    def _get_user_subscription_or_raise(
        self, subscription_id: int, user_id: int, *, lock: bool = False
    ) -> Subscription:
        """Fetch a subscription scoped to a user or raise not found."""
        subscription = self._fetch(Subscription.query.filter_by(id=subscription_id, user_id=user_id), lock=lock)
        if not subscription:
            raise NotFoundError("api.subscriptions.error.not_found")
        return subscription

    @staticmethod
    def _serialize_for_log(value: Any) -> Any:
        """Convert non-JSON values to storage-friendly representations."""
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    # create_subscription_plan removed - users create custom subscriptions instead

    # Private helper methods
    def _validate_subscription_items(self, items: List[Dict[str, Any]]):
        """Validate subscription items"""
        if not items:
            raise ValidationError("Subscription must have at least one item")

        if len(items) > self.max_items:
            raise self._too_many_items()

        for item in items:
            if "product_id" not in item or "quantity" not in item:
                raise ValidationError("Each item must have product_id and quantity")

            product = Product.query.get(item["product_id"])
            if not product or not product.is_active:
                raise ValidationError(f"Product {item['product_id']} not found or inactive")

            if item["quantity"] <= 0:
                raise ValidationError("Item quantity must be positive")

            self._ensure_meets_min_order_quantity(product, item["quantity"])

    @staticmethod
    def _ensure_meets_min_order_quantity(product: Product, quantity: int) -> None:
        """Refuse a subscription line that billing's `create_order` would refuse.

        A subscription item is a standing order line, and `OrderService._process_order_items`
        rejects any quantity under the product's minimum. A subscription saved with one could
        never produce an order: it failed every billing cycle and told no one (prod
        subscription 8, 2026-10-04). Every item write path calls this, before it mutates.
        """
        minimum = int(product.min_order_quantity or 1)
        quantity = int(quantity)
        if quantity >= minimum:
            return
        from business_app.utils.helpers import get_current_language

        language = get_current_language()
        product_name = product.get_translated("name", language)
        raise ValidationError(
            get_translation(
                "api.subscriptions.error.below_min_order_quantity",
                language,
                product=product_name,
                minimum=minimum,
                quantity=quantity,
            ),
            details={
                "product_id": product.id,
                "product_name": product_name,
                "min_order_quantity": minimum,
                "quantity": quantity,
            },
            error_code="SUBSCRIPTION_ITEM_BELOW_MIN_ORDER_QUANTITY",
        )

    # _calculate_subscription_total with plan removed - discounts applied directly to subscriptions

    def _calculate_items_total(self, items: List[Dict[str, Any]]) -> float:
        """Calculate total amount for subscription items"""
        total = 0.0

        for item in items:
            product: Product = Product.query.get(item["product_id"])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")

            total += float(self.customer_unit_price(product) * item["quantity"])

        return total

    def get_billing_info(self, subscription_id: int) -> Dict[str, Any]:
        """
        Get billing information for a subscription

        Args:
            subscription_id: Subscription ID

        Returns:
            Dictionary with billing information
        """
        subscription: Subscription = Subscription.query.get(subscription_id)
        if not subscription:
            raise NotFoundError("Subscription not found")

        # Calculate next billing amount
        next_billing_amount = subscription.billing_amount

        # Get payment method
        payment_method = subscription.payment_method.value or PaymentMethod.CASH

        # The next delivery is published once, as the subscription's `upcoming_delivery` (§9): no second copy.
        return {
            "subscription_id": subscription_id,
            "next_billing_amount": next_billing_amount,
            "payment_method": payment_method.value if hasattr(payment_method, "value") else payment_method,
        }

    def calculate_subscription_preview(
        self,
        user_id: int,
        frequency,
        items: List[Dict[str, Any]],
        delivery_time_slot_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Calculate preview of subscription before creation. Subscriptions give no discount,
        so the preview quotes none (the same decision creation makes).

        Args:
            user_id: User ID
            frequency: The one frequency (daily, weekly, biweekly, monthly)
            items: List of items with product_id and quantity
            delivery_time_slot_id: The chosen slot, whose days a daily delivery is counted on, so the
                per-month figure is the one the saved subscription publishes (spec 2026-10-27 §6.4). An
                inactive or unknown slot counts as none.

        Returns:
            Preview information with pricing breakdown
        """
        # Validate user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        # Validate items
        self._validate_subscription_items(items)

        # Calculate base amount
        subtotal = 0
        item_details = []

        for item in items:
            product = Product.query.get(item["product_id"])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")

            quantity = item.get("quantity", 1)
            unit_price = self.customer_unit_price(product)
            item_total = unit_price * quantity
            subtotal += item_total

            labels = self.line_labels(product, quantity)
            item_details.append(
                {
                    "product_id": product.id,
                    # Translated, as the catalog the pickers read names it (re-review admin-bot-backend-4).
                    "product_name": labels["product_name"],
                    "unit_price": float(unit_price),
                    "quantity": quantity,
                    "item_total": float(item_total),
                    # The website builder's review lines (re-review G6): the labels a saved line carries.
                    "unit_price_label": labels["unit_price_label"],
                    "line_total_label": labels["line_total_label"],
                }
            )

        # Subscriptions give no discount
        discount_amount = 0

        # Calculate delivery fee (if applicable)
        delivery_fee = 0  # Can be calculated based on address/distance

        from business_app.services.loyalty_service import LoyaltyService

        # Calculate final total. A subscription preview is a QUOTE, so it must
        # use the same expression as the order it previews.
        total_amount = compute_order_total(
            subtotal=Decimal(str(subtotal)),
            discount_amount=Decimal(str(discount_amount)),
            delivery_fee=Decimal(str(delivery_fee)),
            # Rewards are redeemed against an existing order; a preview has none.
            loyalty_discount=Decimal("0.00"),
            # The one tier decision; billing stamps subscription orders "web", and the flag makes it 0.
            tier_discount=LoyaltyService()
            .quote_tier_discount(
                user,
                Decimal(str(subtotal)),
                None,
                order_source="web",
                is_subscription_order=True,
            )
            .amount,
        )

        frequency = SubscriptionFrequency(frequency)
        slot = db.session.get(DeliveryTimeSlot, delivery_time_slot_id) if delivery_time_slot_id else None
        per_month = subscription_schedule.deliveries_per_month(frequency, slot)
        monthly_cost = self._monthly_amount(total_amount, frequency, slot)

        return {
            "items": item_details,
            "subtotal": float(subtotal),
            "discount_percentage": 0,
            "discount_amount": float(discount_amount),
            "delivery_fee": float(delivery_fee),
            "total_amount": float(total_amount),
            "total_amount_label": display_labels.money(total_amount),
            "frequency": frequency.value,
            "deliveries_per_month": float(per_month),
            "deliveries_per_month_label": self._deliveries_per_month_label(per_month),
            "estimated_monthly_cost": float(monthly_cost),
            "estimated_monthly_cost_label": display_labels.money(monthly_cost),
            "estimated_annual_cost": float(monthly_cost * 12),
        }

    def _get_frequency_breakdown(self, subscriptions: List[Subscription]) -> Dict[str, int]:
        """Get subscription frequency breakdown"""
        breakdown = {}
        for subscription in subscriptions:
            freq = subscription.frequency.value
            breakdown[freq] = breakdown.get(freq, 0) + 1
        return breakdown
