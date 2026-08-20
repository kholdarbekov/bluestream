"""Scheduled (future-dated) orders — when a delivery is allowed to exist.

The load-bearing idea: an order that is not yet due has NO `Delivery` row.
Every driver-facing surface keys off `Delivery`, so an unreleased order is
invisible to all of them by construction rather than by a filter repeated in
six queries. `ensure_delivery_if_due` is the only place that decides.
"""

import logging
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from flask import current_app, g, has_app_context

# `Delivery` is imported alongside `DeliveryPerson` (same module, already on
# this file's import path) purely for the `ensure_delivery_if_due` return
# annotation below — no new import-cycle risk, since business_app/__init__.py's
# create_app() path already pulls this module in and business_app.models.delivery
# does not import order_schedule_service back.
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User
from business_app.utils.timezone_utils import get_utc_now
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus

logger = logging.getLogger(__name__)

# The statuses whose orders are real enough to hand a driver. PENDING is
# excluded deliberately: an unpaid card order is cancelled by
# cancel_abandoned_orders at 24h, exactly as it is today.
RELEASABLE_ORDER_STATUSES = (OrderStatus.CONFIRMED, OrderStatus.PREPARING)

# `flask.g` attribute name for the request/app-context-scoped
# `earliest_shift_start()` cache. See that method's docstring for why.
_EARLIEST_SHIFT_START_CACHE_ATTR = "_order_schedule_earliest_shift_start"


class OrderScheduleService:
    @staticmethod
    def _local_tz() -> ZoneInfo:
        return ZoneInfo(current_app.config.get("DISPLAY_TIMEZONE", DISPLAY_TIMEZONE))

    @staticmethod
    def earliest_shift_start() -> time:
        """The moment the working day opens, as the earliest rostered shift.

        Filtered on `is_active` + `User.status == 'active'` only. `is_available`
        and `notifications_muted` are momentary toggles — one driver flipping
        unavailable at 07:00 must not move the release for every scheduled order
        that day. `working_days` is deliberately ignored: nothing else in the
        codebase reads it (`DeliveryPerson.is_working_now` does not), so making
        release its first consumer would let unvalidated data silently shift
        release times.

        Request/app-context-scoped cache: `is_awaiting_release`/`release_at`
        both resolve through here, and `serialize_order_admin` calls one or
        both **per order** in the paginated admin order-list's per-order loop
        (business_app/api/admin.py::get_orders) — an uncached call was one
        fresh `DeliveryPerson` roster query PER ORDER on that endpoint. The
        roster cannot meaningfully change mid-request and the release instant
        must not vary within a single response anyway, so caching the result
        on `flask.g` is both a real fix and semantically correct: `g` is torn
        down and rebuilt fresh on every new application context (one per
        HTTP request, and one per Celery task under `ContextTask` — see
        `celery_app.py`), so a stale roster can never leak from one request/
        task into the next. Outside any app context at all (there is none —
        a bare script, say) this simply recomputes on every call, exactly the
        old, uncached behaviour.
        """
        if has_app_context() and hasattr(g, _EARLIEST_SHIFT_START_CACHE_ATTR):
            return getattr(g, _EARLIEST_SHIFT_START_CACHE_ATTR)

        rows = (
            DeliveryPerson.query.join(User, User.id == DeliveryPerson.user_id)
            .filter(DeliveryPerson.is_active.is_(True), User.status == "active")
            .with_entities(DeliveryPerson.working_hours_start)
            .all()
        )
        starts = []
        for (raw,) in rows:
            if not raw:
                continue
            try:
                starts.append(time.fromisoformat(raw))
            except ValueError:
                logger.warning("Ignoring unparseable working_hours_start %r", raw)
        if starts:
            result = min(starts)
        else:
            # No driver rostered at all — fall back so an order can never strand.
            result = time.fromisoformat(current_app.config["DEFAULT_DISPATCH_OPEN_TIME"])

        if has_app_context():
            setattr(g, _EARLIEST_SHIFT_START_CACHE_ATTR, result)
        return result

    @classmethod
    def release_at(cls, order: Order) -> Optional[datetime]:
        """When this order becomes visible to drivers, in UTC.

        `None` means "no schedule" — release is immediate, i.e. today's
        behaviour for every order that carries no `delivery_date`.

        Tashkent has no DST, so combining the date with a local wall-clock time
        is unambiguous.
        """
        if order.delivery_date is None:
            return None
        local = datetime.combine(order.delivery_date, cls.earliest_shift_start(), tzinfo=cls._local_tz())
        return local.astimezone(timezone.utc)

    @classmethod
    def is_awaiting_release(cls, order: Order) -> bool:
        """True when this order must stay invisible to drivers for now."""
        if order.delivery_date is None:
            return False
        if order.delivery is not None:
            return False
        if order.status not in RELEASABLE_ORDER_STATUSES:
            return False
        release_at = cls.release_at(order)
        return release_at is not None and release_at > get_utc_now()

    @classmethod
    def withhold_reason(cls, order: Order) -> Optional[str]:
        """Why this order must NOT get a `Delivery` row yet, or None if it may.

        Two independent reasons, deliberately answered in ONE place so the
        gate's pre-lock and under-lock checks can never drift apart:

        * the order is not real enough to hand a driver at all
          (`RELEASABLE_ORDER_STATUSES`), whatever its date says;
        * the order is real, but its release morning has not arrived yet.

        `is_awaiting_release` cannot answer the first one, and must not be made
        to: its question is "is this order being HELD BACK?", and a PENDING or
        CANCELLED order is not held back -- it is simply not a release
        candidate, so False is the right answer there. The gate, however, asks
        the opposite question ("may I create the Delivery now?"), and reading
        that False as "yes, due now" is what let `reschedule` and the
        `assign_delivery` bulk action -- the two callers that pass unfiltered
        statuses -- fire `create_delivery` for an unpaid or outright cancelled
        order, putting an Accept button in front of every on-shift driver.
        """
        if order.status not in RELEASABLE_ORDER_STATUSES:
            status = getattr(order.status, "value", order.status)
            return f"status {status} is not releasable"
        if cls.is_awaiting_release(order):
            return f"held until {cls.release_at(order)}"
        return None

    @classmethod
    def ensure_delivery_if_due(cls, order: Order) -> Optional[Delivery]:
        """The ONLY place that decides whether an order's delivery may exist yet.

        Every path that used to call `DeliveryService.create_delivery` directly
        goes through here instead. `create_delivery`'s behaviour is unchanged, so
        release fires exactly today's fan-out: pool row, diversion evaluator,
        broadcast, auto-assign timer. It has no order-status guard of its own,
        which is precisely why the status precondition lives here.

        Returns the delivery, or None when the order may not have one yet.
        """
        from business_app import db
        from business_app.services.delivery_service import DeliveryService

        # FIRST, and before the status check: an order that was released and
        # later cancelled still owns a real Delivery row, and every caller
        # expects it back rather than a None that pretends otherwise.
        if order.delivery is not None:
            return order.delivery
        reason = cls.withhold_reason(order)
        if reason is not None:
            logger.info("Order %s not offered to drivers: %s", order.id, reason)
            return None

        # About to WRITE (create the Delivery) -- this is the only branch that
        # matters for the race between `release_due_scheduled_orders` (whose
        # candidate-select is a plain unlocked query, order_tasks.py) and
        # `OrderScheduleService.reschedule` (which locks the Order first, then
        # may delete its Delivery and commit a new future `delivery_date`).
        # `order` above may be exactly such a stale, unlocked instance -- one
        # queried before a concurrent `reschedule()` committed.
        #
        # `populate_existing=True` is load-bearing, NOT decoration.
        # `with_for_update` alone does two things and only two: it skips the
        # identity-map shortcut so SQL is really emitted, and it appends FOR
        # UPDATE. It does NOT set `_populate_existing`, so when the loader
        # finds `order` already in the session it returns that same instance
        # with its STALE attribute values -- the row is locked, the decision
        # is still made on pre-lock data. Measured against the installed
        # SQLAlchemy 2.0.43: `with_for_update()` alone reports the old column
        # value and a `delivery` of None; `get(..., populate_existing=True)`
        # reports the committed value and the committed one-to-one. Autoflush
        # still runs first, so pending in-memory changes are flushed rather
        # than clobbered.
        #
        # Lock ordering is Order-then-Delivery on both sides -- `reschedule`
        # already locks the Order first too -- so this cannot deadlock against
        # it. Do not "optimise" this lock away: it is the entire fix for the
        # premature-Delivery race, not a defensive extra. Re-acquiring a lock
        # this transaction already holds is a harmless no-op in Postgres, so
        # no reentrancy guard is needed either.
        locked_order = db.session.get(Order, order.id, with_for_update=True, populate_existing=True)
        if locked_order is None:
            return None
        if locked_order.delivery is not None:
            return locked_order.delivery
        reason = cls.withhold_reason(locked_order)
        if reason is not None:
            logger.info(
                "Order %s not offered to drivers: %s (caught under lock)",
                locked_order.id,
                reason,
            )
            return None
        return DeliveryService().create_delivery(locked_order.id)

    @classmethod
    def reschedule(
        cls,
        order_id: int,
        *,
        delivery_date,
        window_start: Optional[time] = None,
        window_end: Optional[time] = None,
        actor_user_id: int,
    ) -> Order:
        """Move an order to a different day/window.

        Three cases:
          * awaiting release -> just update the fields
          * released, delivery NEVER claimed -> delete the delivery row so the
            order returns to awaiting release
          * delivery has ANY status history (or already carries a driver) ->
            refuse

        "Never claimed" is "has no `DeliveryStatusHistory`". `create_delivery`
        writes no history, so a pool row nobody touched is childless and
        deletes cleanly; every ever-assigned delivery has history. The five FKs
        that reference `deliveries` are all NO ACTION, so a non-childless
        delete would fail loudly rather than corrupt anything — but we refuse
        first, to give the operator an actionable message instead of an
        IntegrityError.

        The delete deliberately does NOT route through the cancel-delivery
        path: no driver ever owned this row, so `notify_staff_order_cancelled`
        / `notify_staff_order_unassigned` must stay silent.
        """
        from business_app import db
        from business_app.models.delivery import DeliveryStatusHistory
        from business_app.utils.exceptions import NotFoundError, ValidationError
        from business_app.utils.transactions import atomic_transaction

        order = Order.query.with_for_update().get(order_id)
        if not order:
            raise NotFoundError("Order not found", error_code="ORDER_NOT_FOUND")

        delivery = Delivery.query.filter_by(order_id=order_id).first()
        if delivery is not None:
            has_history = (
                db.session.query(DeliveryStatusHistory.id)
                .filter(DeliveryStatusHistory.delivery_id == delivery.id)
                .first()
                is not None
            )
            if has_history or delivery.delivery_person_id is not None:
                raise ValidationError(
                    "This order has already been accepted by a driver. "
                    "Unassign the driver before changing its delivery date.",
                    error_code="ORDER_SCHEDULE_LOCKED_BY_DRIVER",
                )

        with atomic_transaction():
            if delivery is not None:
                db.session.delete(delivery)
            order.delivery_date = delivery_date
            order.delivery_window_start = window_start
            order.delivery_window_end = window_end

        logger.info("Order %s rescheduled to %s by user %s", order_id, delivery_date, actor_user_id)

        # If the new schedule is already due (or cleared), release immediately
        # so the order does not sit invisible until the next sweep tick. No
        # status precondition here on purpose: the gate owns that decision (it
        # declines for anything outside RELEASABLE_ORDER_STATUSES), so re-dating
        # a PENDING or CANCELLED order updates its fields and stops there.
        cls.ensure_delivery_if_due(order)
        return order
