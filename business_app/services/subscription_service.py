"""
Subscription service for the Water Business Platform
Handles recurring water delivery subscriptions
"""

from datetime import UTC, date, datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Any, Iterable, Optional, Tuple
from flask import current_app

from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.user import User, UserAddress
from business_app.models.product import Product
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.delivery import DeliveryTimeSlot
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError
from business_app.utils.local_windows import local_date
from business_app.utils.order_totals import compute_order_total
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from business_app.services import subscription_notices, subscription_schedule
from shared.enums import SubscriptionStatus, SubscriptionFrequency, PaymentMethod
from business_app.utils.translations import get_translation
from business_app import db

_CENT = Decimal("0.01")
# One run creates every delivery that is due, normally one. A bound, so no bug can loop forever.
_MAX_ORDERS_PER_RUN = 31


def _hhmm(value) -> Optional[str]:
    return value.strftime("%H:%M") if value is not None else None


def _order_ref(order: Order) -> Dict[str, Any]:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
    }


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
        self.max_items = current_app.config.get("MAX_SUBSCRIPTION_ITEMS", 10)

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
            payment_method=self._validated_payment_method(subscription_data.get("payment_method", "cash")),
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
                unit_price=product.base_price,
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
        if subscription.status != SubscriptionStatus.ACTIVE:
            db.session.commit()  # release the row lock before raising
            raise ValidationError("Cannot create an order for an inactive subscription")

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
            subscription.next_delivery_date = subscription_schedule.advance(subscription, day)
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

        try:
            order = OrderService().create_order(
                subscription.user_id, self._order_data(subscription, day), subscription=subscription
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
        log = self._order_created_log(subscription, order, day)
        subscription.last_delivery_date = day
        subscription.next_delivery_date = subscription_schedule.advance(subscription, day)
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
    def _order_data(subscription: Subscription, day: date) -> Dict[str, Any]:
        """The order for `day`, in the slot's window when the slot is offered that day (§4.2). The slot's
        own fees are never applied: `create_order` keeps its normal delivery-fee rule."""
        address = subscription.delivery_address
        window = subscription_schedule.window_for(subscription.delivery_time_slot, day)
        return {
            "items": [
                {"product_id": item.product_id, "quantity": item.quantity} for item in subscription.subscription_items
            ],
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

    def _order_created_log(self, subscription: Subscription, order: Order, day: date) -> SubscriptionLog:
        """The ORDER_CREATED row an order the subscription created owes, added in the transaction that moves
        the clock; its notice is sent from it after the commit (§9)."""
        log = SubscriptionLog(
            subscription_id=subscription.id,
            action=subscription_notices.ORDER_CREATED,
            details=get_translation(
                "api.subscriptions.log.order_created",
                self._owner_language(subscription),
                order_number=order.order_number,
                date=day.isoformat(),
            ),
            extra_data={"order_id": order.id, "order_number": order.order_number, "delivery_date": day.isoformat()},
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
        next_day = subscription_schedule.advance(subscription, day)
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
    def _upcoming_order(cls, subscription_id: int) -> Optional[Order]:
        """§9: this subscription's created order that is neither cancelled nor dispatched, the soonest
        first. Dispatched means the driver has it (`customer_cancel_block_code` WITH_DRIVER)."""
        from .order_service import CUSTOMER_CANCEL_PAID, OrderService

        for order in cls._orders_in_play(subscription_id):
            if order.delivery_date is not None and OrderService.customer_cancel_block_code(order) in (
                None,
                CUSTOMER_CANCEL_PAID,
            ):
                return order
        return None

    @classmethod
    def upcoming_delivery(cls, subscription: Subscription) -> Optional[Dict[str, Any]]:
        """The "upcoming delivery" the bot, the web and admin show (§9): the created order that is neither
        cancelled nor dispatched, otherwise `next_delivery_date` in the slot's window. None for a paused or
        cancelled subscription with no such order."""
        order = cls._upcoming_order(subscription.id) if subscription.id is not None else None
        if order is not None:
            return {
                "date": order.delivery_date.isoformat(),
                "window_start": _hhmm(order.delivery_window_start),
                "window_end": _hhmm(order.delivery_window_end),
                "order_id": order.id,
            }
        if subscription.status != SubscriptionStatus.ACTIVE or subscription.next_delivery_date is None:
            return None
        window = subscription_schedule.window_for(subscription.delivery_time_slot, subscription.next_delivery_date)
        return {
            "date": subscription.next_delivery_date.isoformat(),
            "window_start": _hhmm(window[0]) if window else None,
            "window_end": _hhmm(window[1]) if window else None,
            "order_id": None,
        }

    @classmethod
    def published_schedule(cls, subscription: Subscription) -> Dict[str, Any]:
        """The schedule fields every subscription payload carries, decided here once (§9)."""
        frequency = subscription.frequency
        return {
            "frequency": getattr(frequency, "value", frequency),
            "next_delivery_date": (
                subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None
            ),
            "last_delivery_date": (
                subscription.last_delivery_date.isoformat() if subscription.last_delivery_date else None
            ),
            "deliveries_per_month": float(subscription_schedule.deliveries_per_month(frequency)),
            "upcoming_delivery": cls.upcoming_delivery(subscription),
        }

    @staticmethod
    def _monthly_amount(amount, frequency) -> Decimal:
        """A per-delivery amount over a month, by the one calendar-accurate count (§4.2)."""
        return (Decimal(str(amount or 0)) * subscription_schedule.deliveries_per_month(frequency)).quantize(_CENT)

    @classmethod
    def monthly_value(cls, subscription: Subscription) -> Decimal:
        return cls._monthly_amount(subscription.billing_amount, subscription.frequency)

    @classmethod
    def monthly_recurring_revenue(cls, subscriptions: Optional[Iterable[Subscription]] = None) -> Decimal:
        """MRR: what the ACTIVE subscriptions bring in a month. The admin dashboard, the subscription
        report and the subscription analytics all ask this one function (§4.2, §5.6)."""
        if subscriptions is None:
            subscriptions = Subscription.query.filter_by(status=SubscriptionStatus.ACTIVE).all()
        return sum(
            (cls.monthly_value(s) for s in subscriptions if s.status == SubscriptionStatus.ACTIVE),
            Decimal("0.00"),
        )

    # ------------------------------------------------------------------ actions, one rule each (§4.6, §2.7)

    def _settle_orders_in_play(
        self, subscription: Subscription, *, reason: str, actor_user_id: Optional[int]
    ) -> Tuple[List[Order], List[Tuple[Order, str]]]:
        """Pause and cancel (§4.6 with §2.7): cancel this subscription's unpaid orders the driver does not
        have; every other live order stays and is delivered. The customer's own cancel rule decides,
        on locked rows, so a pickup or a payment that commits first wins."""
        from .order_service import OrderService

        order_service = OrderService()
        cancelled: List[Order] = []
        kept: List[Tuple[Order, str]] = []
        for order in self._orders_in_play(subscription.id):
            code = OrderService.customer_cancel_block_code(order)
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

    def _reschedule_after_change(self, subscription: Subscription) -> None:
        """The frequency, day or slot changed: the next delivery by the rule (§4.6, §9). The last created
        delivery (`last_delivery_date`, the newest order's date) stays as it is; while its order is still live
        (not delivered, cancelled or returned, the driver may already have it) the next one waits a full cycle
        after it, so a change never gives two deliveries back to back."""
        last = subscription.last_delivery_date
        subscription.next_delivery_date = subscription_schedule.delivery_after_change(
            subscription,
            local_date(),
            last,
            order_created=last is not None and self._live_order_for(subscription.id, last, live_only=True) is not None,
        )

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
        recent_orders = (
            Order.query.filter_by(subscription_id=subscription_id).order_by(Order.created_at.desc()).limit(10).all()
        )
        billing_info = self.get_billing_info(subscription_id)

        return {
            "subscription": subscription,
            "recent_orders": recent_orders,
            "billing_info": billing_info,
        }

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
            time_slot = DeliveryTimeSlot.query.get(validated_data.delivery_time_slot_id)
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
    ) -> Subscription:
        """Update a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)

        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_update_cancelled")

        changes = {}
        schedule_changed = False
        for field, new_value in update_data.items():
            if not hasattr(subscription, field):
                continue

            old_value = getattr(subscription, field)
            if field == "delivery_address_id":
                address = UserAddress.query.filter_by(id=new_value, user_id=user_id).first()
                if not address:
                    raise NotFoundError("api.subscriptions.error.invalid_delivery_address")

            if field == "delivery_time_slot_id" and new_value is not None:
                time_slot = DeliveryTimeSlot.query.get(new_value)
                if not time_slot or not time_slot.is_active:
                    raise NotFoundError("api.subscriptions.error.invalid_or_inactive_time_slot")

            if field == "payment_method":
                new_value = self._validated_payment_method(new_value)

            if field in self._SCHEDULE_FIELDS:
                if field == "frequency":
                    new_value = SubscriptionFrequency(new_value)
                schedule_changed = schedule_changed or new_value != old_value
                self._set_schedule_field(subscription, field, new_value)
            else:
                setattr(subscription, field, new_value)
            changes[field] = {
                "old": self._serialize_for_log(old_value),
                "new": self._serialize_for_log(new_value),
            }

        if schedule_changed:
            self._reschedule_after_change(subscription)
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
        if schedule_changed:
            self._create_first_order_if_due(subscription, local_date())
        return subscription

    def pause_subscription_for_user(self, subscription_id: int, user_id: int, reason: str) -> Dict[str, Any]:
        """Pause a user-owned subscription (§4.6, §2.7): its unpaid upcoming orders are cancelled, a paid
        one stays. Returns the subscription and the orders cancelled and kept."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("api.subscriptions.error.only_active_pause")
        return self._pause(
            subscription,
            reason=reason,
            details=get_translation("api.subscriptions.log.reason", reason=reason),
            actor_user_id=subscription.user_id,
        )

    def resume_subscription_for_user(self, subscription_id: int, user_id: int) -> Subscription:
        """Resume a paused user-owned subscription (§4.6)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        if subscription.status != SubscriptionStatus.PAUSED:
            raise ValidationError("api.subscriptions.error.only_paused_resume")
        return self._resume(
            subscription, details=get_translation("api.subscriptions.log.resumed"), actor_user_id=subscription.user_id
        )

    def cancel_subscription_for_user(self, subscription_id: int, user_id: int, reason: str) -> Dict[str, Any]:
        """Cancel a user-owned subscription, immediately (§4.6). A paid upcoming order stays (§2.7)."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        if subscription.status == SubscriptionStatus.CANCELLED:
            raise ValidationError("api.subscriptions.error.already_cancelled")
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
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_modify_cancelled")

        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            raise NotFoundError("api.subscriptions.error.product_not_found")
        self._ensure_meets_min_order_quantity(product, quantity)

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
            unit_price=product.calculate_price(),
            special_instructions=special_instructions,
        )
        item.calculate_total()
        db.session.add(item)
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
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_modify_cancelled")

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
        """Remove an item from a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_modify_cancelled")

        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id,
        ).first()
        if not item:
            raise NotFoundError("api.subscriptions.error.item_not_found")

        remaining_items = (
            SubscriptionItem.query.filter_by(subscription_id=subscription_id)
            .filter(SubscriptionItem.id != item_id)
            .count()
        )
        if remaining_items == 0:
            raise ValidationError("api.subscriptions.error.cannot_remove_last_item")

        product_name = item.product.name if item.product else get_translation("api.subscriptions.unknown_product")
        db.session.delete(item)
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

    def get_subscription_billing_history_for_user(self, subscription_id: int, user_id: int) -> Dict[str, Any]:
        """Get billing history payload for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        payments = Payment.query.filter_by(subscription_id=subscription_id).order_by(Payment.created_at.desc()).all()

        total_paid = sum(
            float(payment.amount)
            for payment in payments
            if getattr(payment.status, "value", payment.status) == "completed"
        )
        failed_payments = len(
            [payment for payment in payments if getattr(payment.status, "value", payment.status) == "failed"]
        )

        return {
            "billing_history": [
                {
                    "payment_id": payment.payment_id,
                    "amount": payment.amount,
                    "status": payment.status.value,
                    "payment_method": payment.payment_method.value,
                    "created_at": payment.created_at.isoformat() if payment.created_at else None,
                    "failure_reason": payment.failure_reason,
                }
                for payment in payments
            ],
            "summary": {
                "total_paid": total_paid,
                "total_payments": len(payments),
                "failed_payments": failed_payments,
                # The next delivery is published once, as the subscription's `upcoming_delivery` (§9).
                "next_billing_amount": subscription.billing_amount,
            },
        }

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
        for i in range(12):
            month_start = (datetime.now(UTC).replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            month_key = month_start.strftime("%Y-%m")

            month_total = Decimal("0.00")
            for subscription in subscriptions:
                if subscription.created_at.date() <= month_start.date() and (
                    not subscription.end_date or subscription.end_date.date() >= month_start.date()
                ):
                    month_total += self.monthly_value(subscription)

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
        reason: str,
    ) -> Dict[str, Any]:
        """Skip the upcoming delivery (§9).

        An unpaid created order is cancelled: that skips its delivery, and the clock (already one cycle on
        since the order was created) stays. A paid one stays and is delivered (§2.7), so the clock moves one
        cycle instead, as it does when no order is created yet. `new_next_delivery_date` is the upcoming
        delivery after the skip.
        """
        from .order_service import OrderService

        subscription = self._get_user_subscription_or_raise(subscription_id, user_id, lock=True)
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("api.subscriptions.error.only_active_skip")

        upcoming = self._upcoming_order(subscription_id)
        cancelled: List[Order] = []
        kept: List[Tuple[Order, str]] = []
        if upcoming is not None:
            code = OrderService.customer_cancel_block_code(upcoming)
            if code is None:
                try:
                    OrderService().cancel_order_as_customer(upcoming.id, subscription.user_id, reason=reason)
                    cancelled.append(upcoming)
                except (ValidationError, ConflictError):  # a pickup or a payment committed first
                    upcoming = db.session.get(Order, upcoming.id, populate_existing=True)
                    code = OrderService.customer_cancel_block_code(upcoming)
            if not cancelled:
                kept.append((upcoming, code))

        if cancelled:
            skipped_date = cancelled[0].delivery_date
        else:
            skipped_date = subscription.next_delivery_date or subscription_schedule.first_delivery(
                subscription, local_date()
            )
            subscription.next_delivery_date = subscription_schedule.advance(subscription, skipped_date)
        subscription.updated_at = datetime.now(UTC)
        db.session.flush()
        upcoming_after = self.upcoming_delivery(subscription)
        new_next = date.fromisoformat(upcoming_after["date"]) if upcoming_after else None

        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="delivery_skipped",
                details=get_translation(
                    "api.subscriptions.log.delivery_skipped",
                    date=skipped_date.isoformat(),
                    reason=reason,
                ),
                user_id=subscription.user_id,
                extra_data={
                    "skipped_by": "customer",
                    "original_delivery_date": skipped_date.isoformat(),
                    "new_delivery_date": new_next.isoformat() if new_next else None,
                    "reason": reason,
                    "cancelled_order_ids": [order.id for order in cancelled],
                },
            )
        )
        notices = self._paid_order_kept_logs(subscription, kept, "skip", subscription.next_delivery_date)
        db.session.commit()
        self._deliver(notices)

        return {
            **self._action_outcome(subscription, cancelled, kept),
            "original_delivery_date": skipped_date,
            "new_next_delivery_date": new_next,
        }

    def change_payment_method_for_user(
        self,
        subscription_id: int,
        user_id: int,
        payment_method: str,
    ) -> Dict[str, Any]:
        """Change payment method for a user-owned subscription."""
        subscription = self._get_user_subscription_or_raise(subscription_id, user_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("api.subscriptions.error.cannot_change_payment_cancelled")

        new_payment_method = self._validated_payment_method(payment_method)

        old_payment_method = subscription.payment_method
        subscription.payment_method = new_payment_method
        subscription.updated_at = datetime.now(UTC)

        db.session.add(
            SubscriptionLog(
                subscription_id=subscription_id,
                action="payment_method_changed",
                details=get_translation(
                    "api.subscriptions.log.payment_method_changed",
                    old_method=old_payment_method.value,
                    new_method=new_payment_method.value,
                ),
                user_id=user_id,
            )
        )
        db.session.commit()

        return {
            "subscription": subscription,
            "old_payment_method": old_payment_method,
            "new_payment_method": new_payment_method,
        }

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
            time_slot = DeliveryTimeSlot.query.get(validated_data.delivery_time_slot_id)
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

        if not overrides.get("edit_any_status") and subscription.status not in [
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.PAUSED,
        ]:
            raise ValidationError(
                "Cannot edit a cancelled or expired subscription without the edit-any-status override"
            )

        status_override_used = bool(
            overrides.get("edit_any_status")
            and subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]
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

            if field == "delivery_time_slot_id" and new_value is not None:
                time_slot = DeliveryTimeSlot.query.get(new_value)
                if not time_slot or not time_slot.is_active:
                    raise NotFoundError("Delivery time slot not found or inactive")

            if field == "payment_method":
                new_value = self._validated_payment_method(new_value)

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
            self._reschedule_after_change(subscription)

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
        subscription = self._get_subscription_or_raise(subscription_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("Cannot modify a cancelled or expired subscription")

        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            raise NotFoundError("Product not found or inactive")
        self._ensure_meets_min_order_quantity(product, quantity)

        existing = SubscriptionItem.query.filter_by(subscription_id=subscription_id, product_id=product_id).first()
        if existing:
            raise ConflictError("This product is already in the subscription")

        item = SubscriptionItem(
            subscription_id=subscription_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=product.calculate_price(),
            special_instructions=special_instructions,
        )
        item.calculate_total()
        db.session.add(item)
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
        subscription = self._get_subscription_or_raise(subscription_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("Cannot modify a cancelled or expired subscription")

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
        """Admin: remove a subscription item (cannot remove the last one)."""
        subscription = self._get_subscription_or_raise(subscription_id)
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            raise ValidationError("Cannot modify a cancelled or expired subscription")

        item = SubscriptionItem.query.filter_by(id=item_id, subscription_id=subscription_id).first()
        if not item:
            raise NotFoundError("Subscription item not found")

        remaining = (
            SubscriptionItem.query.filter_by(subscription_id=subscription_id)
            .filter(SubscriptionItem.id != item_id)
            .count()
        )
        if remaining == 0:
            raise ValidationError("Cannot remove the last item in a subscription")

        product_name = item.product.name if item.product else "item"
        db.session.delete(item)
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

    def admin_pause_subscription(self, subscription_id: int, actor_user_id: int, reason: str) -> Dict[str, Any]:
        """Admin: pause an ACTIVE subscription, by the customer's rule (§4.6): unpaid upcoming orders are
        cancelled, a paid one stays. Audit-logged with the acting admin."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError("Only active subscriptions can be paused")
        return self._pause(
            subscription, reason=reason, details=f"Paused by admin: {reason}", actor_user_id=actor_user_id
        )

    def admin_resume_subscription(self, subscription_id: int, actor_user_id: int) -> Subscription:
        """Admin: resume a PAUSED subscription, by the customer's rule (§4.6)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        if subscription.status != SubscriptionStatus.PAUSED:
            raise ValidationError("Only paused subscriptions can be resumed")
        return self._resume(subscription, details="Resumed by admin", actor_user_id=actor_user_id)

    def admin_cancel_subscription(self, subscription_id: int, actor_user_id: int, reason: str) -> Dict[str, Any]:
        """Admin: cancel a subscription, immediately, by the customer's rule (§4.6, §2.7)."""
        subscription = self._get_subscription_or_raise(subscription_id, lock=True)
        if subscription.status == SubscriptionStatus.CANCELLED:
            raise ValidationError("Subscription is already cancelled")
        return self._cancel(
            subscription, reason=reason, details=f"Cancelled by admin: {reason}", actor_user_id=actor_user_id
        )

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
            raise ValidationError(f"Maximum {self.max_items} items allowed per subscription")

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

            total += float(product.base_price * item["quantity"])

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
    ) -> Dict[str, Any]:
        """
        Calculate preview of subscription before creation. Subscriptions give no discount,
        so the preview quotes none (the same decision creation makes).

        Args:
            user_id: User ID
            frequency: The one frequency (daily, weekly, biweekly, monthly)
            items: List of items with product_id and quantity

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
            item_total = product.base_price * quantity
            subtotal += item_total

            item_details.append(
                {
                    "product_id": product.id,
                    "product_name": product.name,
                    "unit_price": float(product.base_price),
                    "quantity": quantity,
                    "item_total": float(item_total),
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
        monthly_cost = self._monthly_amount(total_amount, frequency)

        return {
            "items": item_details,
            "subtotal": float(subtotal),
            "discount_percentage": 0,
            "discount_amount": float(discount_amount),
            "delivery_fee": float(delivery_fee),
            "total_amount": float(total_amount),
            "frequency": frequency.value,
            "deliveries_per_month": float(subscription_schedule.deliveries_per_month(frequency)),
            "estimated_monthly_cost": float(monthly_cost),
            "estimated_annual_cost": float(monthly_cost * 12),
        }

    def _get_frequency_breakdown(self, subscriptions: List[Subscription]) -> Dict[str, int]:
        """Get subscription frequency breakdown"""
        breakdown = {}
        for subscription in subscriptions:
            freq = subscription.frequency.value
            breakdown[freq] = breakdown.get(freq, 0) + 1
        return breakdown
