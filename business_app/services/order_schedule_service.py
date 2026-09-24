"""Scheduled (future-dated) orders — when a delivery is allowed to exist.

The load-bearing idea: an order that is not yet due is invisible to drivers.
It has NO `Delivery` row, or a row held in `rescheduled` (released once, then
moved to a later day). Every driver-facing surface keys off a claimable
`Delivery`, so a held order is invisible by construction.
`ensure_delivery_if_due` is the only place that releases one.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import current_app, g, has_app_context

# `Delivery` is imported alongside `DeliveryPerson` (same module, already on
# this file's import path) for the `ensure_delivery_if_due` return annotation
# and the reschedule's row lock; `DeliveryStatusHistory` for the width of its
# `reason` column. No new import-cycle risk: business_app/__init__.py's
# create_app() path already pulls this module in, and
# business_app.models.delivery does not import order_schedule_service back.
from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User
from business_app.utils.state_validators import ACTIVE_ORDER_STATUSES
from business_app.utils.timezone_utils import get_utc_now
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus

logger = logging.getLogger(__name__)

# The statuses whose orders are real enough to hand a driver. PENDING is
# excluded deliberately: an unpaid card order is cancelled by
# cancel_abandoned_orders at 24h, exactly as it is today.
RELEASABLE_ORDER_STATUSES = (OrderStatus.CONFIRMED, OrderStatus.PREPARING)

# A reschedule's reason is stored on the delivery's history row (R9), whose
# `reason` column is String(100); a longer one is refused, never cut (R24).
# Read from the model, not restated, so a migration that widens the column
# widens this with it.
RESCHEDULE_REASON_MAX_LENGTH = DeliveryStatusHistory.__table__.c.reason.type.length


@dataclass(frozen=True)
class RescheduleOutcome:
    """One committed reschedule, as its post-commit side effects need it.

    Captured under the locks and BEFORE the write: who held the stop and what
    their card showed are exactly what the commit replaces, and the notices go
    out after it.
    """

    order_id: int
    delivery_id: Optional[int]
    new_date: Optional[date]
    pre_status: Optional[DeliveryStatus]
    landed_status: Optional[DeliveryStatus]
    old_driver_user_id: Optional[int]
    old_driver_telegram_id: Optional[str]
    old_driver_order_info: Optional[dict]
    notify_customer: bool
    audit: dict


# `flask.g` attribute name for the request/app-context-scoped
# `earliest_shift_start()` cache. See that method's docstring for why.
_EARLIEST_SHIFT_START_CACHE_ATTR = "_order_schedule_earliest_shift_start"


class OrderScheduleService:
    # R2 (docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md):
    # a delivery can be moved from anywhere short of an end state. Pool rows,
    # a driver's live stops, a failed attempt and a row already waiting for its
    # new day all take the same pull-back.
    RESCHEDULABLE_DELIVERY_STATUSES: FrozenSet[DeliveryStatus] = frozenset(
        {
            DeliveryStatus.SCHEDULED,
            DeliveryStatus.PENDING,
            DeliveryStatus.ASSIGNED,
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
            DeliveryStatus.FAILED,
            DeliveryStatus.RESCHEDULED,
        }
    )
    # R13: the driver was already on the way, so the customer is expecting them
    # today and must hear the new date.
    CUSTOMER_NOTICE_DELIVERY_STATUSES: FrozenSet[DeliveryStatus] = frozenset(
        {DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED}
    )
    # R16: a driver holds the stop in these, so a reschedule takes it off their
    # route and tells them why.
    DRIVER_NOTICE_DELIVERY_STATUSES: FrozenSet[DeliveryStatus] = frozenset(
        {DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED}
    )

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
        """True when this order must stay invisible to drivers for now.

        Held means one of two shapes: no `Delivery` row yet, or a row parked in
        `rescheduled`, which was released once and then moved to a later day
        (R3/R5 of docs/superpowers/specs/2026-09-23-admin-order-reschedule-
        design.md). This stays the ONE place that answers, so the release
        gate, the Dispatch board's `scheduled` bucket and the Orders page's
        "Scheduled" tag cover a held row without a second copy of the rule.
        """
        if order.delivery_date is None:
            return False
        delivery = order.delivery
        if delivery is not None and delivery.status != DeliveryStatus.RESCHEDULED:
            return False
        if order.status not in RELEASABLE_ORDER_STATUSES:
            return False
        release_at = cls.release_at(order)
        return release_at is not None and release_at > get_utc_now()

    @classmethod
    def published_release_at(cls, order: Order) -> Optional[datetime]:
        """The release instant a payload may show: set only while the order is held.

        The ONE answer behind every published `release_at`: the admin order
        payloads (`order_schedule_fields`) and both re-dispatch responses
        (`StaffService.redispatch_release_at`). Once the order is released, or
        when nothing will release it at a known instant (a PENDING order waits
        for its confirmation), there is no "drivers will see it at" to quote.
        """
        return cls.release_at(order) if cls.is_awaiting_release(order) else None

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
    def reschedule_block_code(cls, order: Order) -> Optional[str]:
        """Why this order's delivery cannot be moved, or None when it can (R1, R2).

        The ONE answer to "may this be rescheduled". The admin list row, the
        detail payload and `reschedule` itself all ask here, so the button an
        admin sees and the write it sends cannot disagree. The order is checked
        first: for a delivered order, "this order is delivered" is the reason
        an admin can act on.

        Reads `order.status` and `order.delivery` only. The admin order list
        eager-loads the delivery, so answering this for a whole page costs no
        query.
        """
        if order.status not in ACTIVE_ORDER_STATUSES:
            return "ORDER_NOT_RESCHEDULABLE"
        delivery = order.delivery
        if delivery is not None and delivery.status not in cls.RESCHEDULABLE_DELIVERY_STATUSES:
            return "DELIVERY_NOT_RESCHEDULABLE"
        return None

    @classmethod
    def reschedule_date_bounds(cls, order: Order) -> Tuple[date, date]:
        """The first and last day this order may be moved to (R10, R11).

        Local today through the booking horizon (`schedule_date_bounds`, the
        same range create-order validates against), cut short at a grocery
        store's AMOUNT-contract end. The delivery charges that contract, and
        the charge refuses once it has ended. The end is a UTC instant; its
        LOCAL date is the last day allowed, which is also the date the admin
        sees on the contract. The active-contract lookup already drops a
        contract that has ended, so the cut never falls before today. A store
        with two active AMOUNT contracts gets no cut: the charge refuses on
        that same ambiguity, so it is fixed on the contracts, not here.
        """
        from business_app.utils.delivery_window import schedule_date_bounds
        from business_app.utils.local_windows import local_date

        min_date, max_date = schedule_date_bounds()
        customer = order.user
        if customer is not None and customer.is_grocery_store:
            from business_app.services.corporate_contract_service import get_corporate_contract_service
            from business_app.utils.exceptions import ValidationError

            try:
                contract = get_corporate_contract_service().get_active_amount_contract_for_user(customer.id)
            except ValidationError:
                # Raised only for more than one active AMOUNT contract. Failing
                # here would take the whole admin order detail down with it.
                logger.warning(
                    "Reschedule bounds for order %s: customer %s has more than one active AMOUNT contract; "
                    "no contract cap applied",
                    order.id,
                    customer.id,
                )
                contract = None
            if contract is not None and contract.end_date is not None:
                max_date = min(max_date, local_date(contract.end_date))
        return min_date, max_date

    @classmethod
    def customer_already_notified(cls, order: Order) -> bool:
        """Has this order's customer already been sent a `delivery_rescheduled` notice (R13)?

        Keyed on `is_sent`, never on `delivery_status`. When the customer opens
        their notifications, `mark_all_notifications_read` flips that one to
        'read', and a notice they have read is still a notice they got.
        """
        from business_app import db
        from business_app.models.notification import Notification
        from business_app.utils.constants import NotificationType

        return (
            db.session.query(Notification.id)
            .filter(
                Notification.notification_type == NotificationType.DELIVERY_RESCHEDULED.value,
                Notification.order_id == order.id,
                Notification.is_sent.is_(True),
            )
            .first()
            is not None
        )

    @classmethod
    def will_notify_customer(cls, order: Order) -> bool:
        """R13: a reschedule tells the customer when the driver was already on
        the way, or when they were told about an earlier reschedule and are
        waiting on the date we gave them.

        Published as `reschedule_notifies_customer` AND read by `reschedule`
        itself, so the modal's "the customer will be notified" and the notice
        that is actually queued are one expression. The status test runs first
        and short-circuits, so a delivery that is on its way costs no query.
        """
        delivery = order.delivery
        if delivery is not None and delivery.status in cls.CUSTOMER_NOTICE_DELIVERY_STATUSES:
            return True
        return cls.customer_already_notified(order)

    @staticmethod
    def _customer_notice_channel(order: Order) -> Optional[str]:
        """The notice's channel: "telegram", "email", or None when the customer cannot be reached (R14).

        Resolved by the notification service's own rule, the one the send
        uses, so the modal never promises a channel the notice will not take.
        A staticmethod, called on the class: building a `NotificationService`
        would build its SMS client for a read-only answer.
        """
        from business_app.services.notification_service import NotificationService

        if order.user is None:
            return None
        channels = NotificationService._resolve_telegram_else_email(order.user)
        return channels[0].value if channels else None

    @classmethod
    def get_reschedule_metadata(cls, order: Order, *, detail: bool) -> Dict[str, Any]:
        """The reschedule answer the admin UI renders, published so no client re-derives it.

        List rows (`detail=False`) get the yes/no and its reason, read off the
        row: no query (see `reschedule_block_code`). The detail payload and the
        Reschedule modal also get what the reschedule would do. Each of those
        costs a lookup (notifications, the contract, the driver's user row), so
        only a single order pays for them.

        A blocked order promises nothing: no notice, no driver losing a stop.
        """
        block_code = cls.reschedule_block_code(order)
        metadata: Dict[str, Any] = {"can_reschedule": block_code is None, "reschedule_block_code": block_code}
        if not detail:
            return metadata

        reschedulable = block_code is None
        delivery = order.delivery
        losing_stop = None
        if (
            reschedulable
            and delivery is not None
            and delivery.status in cls.DRIVER_NOTICE_DELIVERY_STATUSES
            and delivery.delivery_person_id is not None
        ):
            # `delivery_person_id` is a users.id; the name comes from that user.
            driver = delivery.delivery_person
            losing_stop = {"id": delivery.delivery_person_id, "name": driver.full_name if driver is not None else ""}
        min_date, max_date = cls.reschedule_date_bounds(order)
        metadata.update(
            {
                "reschedule_notifies_customer": reschedulable and cls.will_notify_customer(order),
                "reschedule_customer_channel": cls._customer_notice_channel(order),
                "reschedule_driver_losing_stop": losing_stop,
                "reschedule_min_date": min_date.isoformat(),
                "reschedule_max_date": max_date.isoformat(),
            }
        )
        return metadata

    @classmethod
    def ensure_delivery_if_due(cls, order: Order) -> Optional[Delivery]:
        """The ONLY place that decides whether an order's delivery may exist yet.

        Every path that used to call `DeliveryService.create_delivery` directly
        goes through here instead. `create_delivery`'s behaviour is unchanged, so
        release fires exactly today's fan-out: pool row, diversion evaluator,
        broadcast, auto-assign timer. It has no order-status guard of its own,
        which is precisely why the status precondition lives here.

        It is also the one way out of `rescheduled`: a held row is flipped
        back into the pool here once its new day's release moment has come
        (`_release_held_delivery`).

        Returns the delivery, or None when the order may not have one yet.
        That includes a held row that is still waiting, so the release sweep
        counts it as `awaiting`, not `released`.
        """
        from business_app import db
        from business_app.services.delivery_service import DeliveryService

        # FIRST, and before the status check: an order that was released and
        # later cancelled still owns a real Delivery row, and every caller
        # expects it back rather than a None that pretends otherwise. The one
        # exception is a row held in `rescheduled`: it waits for its day
        # exactly like an order with no row, so it takes the same decision.
        delivery = order.delivery
        if delivery is not None and delivery.status != DeliveryStatus.RESCHEDULED:
            return delivery
        reason = cls.withhold_reason(order)
        if reason is not None:
            logger.info("Order %s not offered to drivers: %s", order.id, reason)
            return None
        if delivery is not None:
            return cls._release_held_delivery(order.id, delivery.id)

        # About to CREATE the Delivery. (A held row never gets here: it went to
        # `_release_held_delivery` above, which re-decides under its own locks.)
        # This is the branch that matters for the race between
        # `release_due_scheduled_orders` (whose candidate-select is a plain
        # unlocked query, order_tasks.py) and `OrderScheduleService.reschedule`
        # (which, for an order with no Delivery yet, holds the Order lock while
        # it commits a new `delivery_date`). `order` above may be exactly such
        # a stale, unlocked instance -- one queried before a concurrent
        # `reschedule()` committed.
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
        # No deadlock against `reschedule`: on this branch there is no Delivery
        # row to lock, so the Order is the only row either side locks. Do not
        # "optimise" this lock away: it is the entire fix for the
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
    def _release_held_delivery(cls, order_id: int, delivery_id: int) -> Optional[Delivery]:
        """Flip a held (`rescheduled`) row back into the pool on its new day.

        Re-decided under lock, because the caller's "due" came from unlocked
        reads: the sweep's candidate select, or any caller's stale instance.
        The Delivery is locked first, then the Order. That is the order every
        other writer of a delivery takes (`update_delivery_status`,
        `return_delivery_to_pool`, `assign_driver`, `reschedule`). One caller
        breaks it: confirming a PENDING order reaches here through
        `update_order_status`, whose compare-and-set UPDATE already holds the
        Order lock, so a confirm racing a reschedule of the same order is
        Order-then-Delivery against Delivery-then-Order. Postgres aborts one
        side rather than hanging -- the same shape as the order-cancel
        inversion in spec §10. Both reads use `populate_existing`, for
        the reason spelled out in `ensure_delivery_if_due`: without it the
        lock is taken and the decision is still made on the stale instances
        handed in.

        The status is written directly, the way `_pull_back_delivery` writes
        it. RESCHEDULED's only edge in DELIVERY_STATUS_TRANSITIONS is
        CANCELLED, so no status edit by a person can release a held row early
        (R3, R22). This is the system's one exit.
        """
        from business_app import db
        from business_app.services.delivery_service import DeliveryService
        from business_app.utils.state_validators import assert_unassigned_for_pool_status
        from business_app.utils.transactions import atomic_transaction

        delivery = Delivery.query.filter_by(id=delivery_id).with_for_update().populate_existing().one_or_none()
        order = db.session.get(Order, order_id, with_for_update=True, populate_existing=True)
        if delivery is None or order is None:
            return None
        if delivery.status != DeliveryStatus.RESCHEDULED:
            # Moved while we were unlocked. A reschedule to today already put it
            # back in the pool, or the order-cancel cascade ended it. Either
            # way it is an ordinary row now, handed back like any other.
            return delivery
        reason = cls.withhold_reason(order)
        if reason is not None:
            logger.info("Order %s not offered to drivers: %s (caught under lock)", order_id, reason)
            return None

        now = get_utc_now()
        delivery_service = DeliveryService()
        with atomic_transaction():
            delivery.status = DeliveryStatus.SCHEDULED
            assert_unassigned_for_pool_status(delivery, DeliveryStatus.SCHEDULED)
            db.session.add(
                DeliveryStatusHistory(
                    delivery_id=delivery.id,
                    old_status=DeliveryStatus.RESCHEDULED,
                    new_status=DeliveryStatus.SCHEDULED,
                    changed_by=None,
                    changed_at=now,
                    automatic=True,
                    notes="Released to drivers on its rescheduled date",
                )
            )
            # After the status: `stamp_schedule` gives a `scheduled` row its
            # ETA, which a held row went without (no countdown for a held order).
            delivery_service.stamp_schedule(delivery, order, now=now)
        logger.info("Held delivery %s for order %s released to drivers", delivery.id, order_id)
        delivery_service.offer_to_drivers(delivery)
        return delivery

    @classmethod
    def reschedule(
        cls,
        order_id: int,
        *,
        delivery_date: Optional[date],
        window_start: Optional[time] = None,
        window_end: Optional[time] = None,
        actor_user_id: int,
        reason: Optional[str] = None,
        expected_delivery_status: Optional[DeliveryStatus] = None,
    ) -> Order:
        """Move an order's delivery to another date and window. The ONE re-dater.

        The admin PATCH, the admin Delivery-page re-dispatch and the staff-bot
        re-dispatch all come here (spec 2026-09-23 §3.4), so eligibility, the
        unassign, the landing status and the notices cannot differ by screen.

        * No `Delivery` row yet: only the order's fields move, then the release
          gate runs, exactly as before.
        * A row in `RESCHEDULABLE_DELIVERY_STATUSES`: the driver, if any, is
          taken off it. It lands `scheduled` when its new release instant has
          passed (back in today's pool) or `rescheduled` (held until
          `release_due_scheduled_orders` offers it on the day). The row is
          never deleted: a delivery a driver touched has history children, and
          all five FKs into `deliveries` are NO ACTION.
        * The order's status moves only out_for_delivery -> confirmed (R6). A
          reschedule never confirms an order (R23), so a PENDING one stays
          PENDING, and its row is held even on a due date until the order is
          confirmed.

        `reason` is stripped; longer than 100 characters is refused, never cut
        (R24). `expected_delivery_status` is the re-dispatch guard. The caller saw a
        FAILED row on an unlocked read; this re-checks it on the locked one.

        Raises NotFoundError (ORDER_NOT_FOUND), or ValidationError with
        ORDER_NOT_RESCHEDULABLE, DELIVERY_NOT_RESCHEDULABLE,
        DELIVERY_DATE_REQUIRED, ORDER_RESCHEDULE_PAST_CONTRACT_END,
        ORDER_RESCHEDULE_REASON_TOO_LONG or STAFF_DELIVERY_NOT_REDISPATCHABLE.
        Nothing is written when it raises.
        """
        from business_app import db

        outcome = cls._apply_reschedule(
            order_id,
            delivery_date=delivery_date,
            window_start=window_start,
            window_end=window_end,
            actor_user_id=actor_user_id,
            reason=cls._normalize_reason(reason),
            expected_delivery_status=expected_delivery_status,
        )
        logger.info(
            "order_rescheduled order=%s from=%s to=%s delivery_before=%s landed=%s driver_before=%s actor=%s",
            outcome.order_id,
            outcome.audit["from_delivery_date"],
            outcome.audit["to_delivery_date"],
            outcome.audit["delivery_status_before"],
            outcome.audit["landed_status"],
            outcome.old_driver_user_id,
            actor_user_id,
        )
        cls._run_post_commit(outcome)
        return db.session.get(Order, order_id)

    @staticmethod
    def _normalize_reason(reason: Optional[str]) -> Optional[str]:
        """Blank is no reason. Longer than the history column is refused, not cut:
        the audit event keeps the whole text, and a history row quietly holding a
        prefix of it would be the same reason recorded two ways."""
        from business_app.utils.exceptions import ValidationError

        reason = (reason or "").strip() or None
        if reason is not None and len(reason) > RESCHEDULE_REASON_MAX_LENGTH:
            raise ValidationError(
                f"The reason can be at most {RESCHEDULE_REASON_MAX_LENGTH} characters",
                error_code="ORDER_RESCHEDULE_REASON_TOO_LONG",
            )
        return reason

    @staticmethod
    def _lock_delivery(order_id: int) -> Optional[Delivery]:
        """The order's delivery row, locked and re-read from the database.

        `populate_existing` is load-bearing for the reason spelled out in
        `ensure_delivery_if_due`: without it a copy already in the session comes
        back with its pre-lock values, and a driver's tap committed a moment ago
        would be invisible to the decision made under the lock.
        """
        return Delivery.query.filter_by(order_id=order_id).with_for_update().populate_existing().one_or_none()

    @classmethod
    def _apply_reschedule(
        cls,
        order_id: int,
        *,
        delivery_date: Optional[date],
        window_start: Optional[time],
        window_end: Optional[time],
        actor_user_id: int,
        reason: Optional[str],
        expected_delivery_status: Optional[DeliveryStatus],
    ) -> RescheduleOutcome:
        """Lock, validate, write and commit one reschedule (spec §3.4 steps 1-5).

        Delivery first, then Order: the order `update_delivery_status`,
        `return_delivery_to_pool` and `assign_driver` take them in, so a
        driver's tap and a reschedule queue on the row instead of deadlocking.
        Everything after the locks reads only the two locked instances.

        The whole body is one transaction, so a refusal rolls back, lets go of
        both locks at once, and leaves nothing half-written.
        """
        from business_app import db
        from business_app.services.delivery_service import DeliveryService
        from business_app.services.staff_service import StaffService
        from business_app.utils.delivery_window import format_delivery_window
        from business_app.utils.exceptions import NotFoundError, ValidationError
        from business_app.utils.staff_order_info import build_staff_order_info
        from business_app.utils.transactions import atomic_transaction

        with atomic_transaction():
            delivery = cls._lock_delivery(order_id)
            order = db.session.get(Order, order_id, with_for_update=True, populate_existing=True)
            if order is None:
                raise NotFoundError("Order not found", error_code="ORDER_NOT_FOUND")
            if delivery is None:
                # A release may have committed after the first read and before
                # the Order lock. With the Order held, nothing else can create
                # the row, so this read is final. It takes the row lock after
                # the Order's -- the reverse order, but only for a row created
                # an instant ago, and Postgres aborts one side of a deadlock
                # rather than hanging.
                delivery = cls._lock_delivery(order_id)
            # `order.delivery` may still hold what an earlier read saw (None,
            # before that release). Expire it so the eligibility helpers below,
            # which read it, get the row locked above.
            db.session.expire(order, ["delivery"])

            block_code = cls.reschedule_block_code(order)
            if block_code == "ORDER_NOT_RESCHEDULABLE":
                raise ValidationError(
                    f"Order {order.order_number} is {order.status.value}; only an active order can be rescheduled",
                    error_code=block_code,
                )
            if block_code is not None:
                raise ValidationError(
                    f"This order's delivery is {delivery.status.value} and can no longer be rescheduled",
                    error_code=block_code,
                )
            pre_status = delivery.status if delivery is not None else None
            if expected_delivery_status is not None and pre_status != expected_delivery_status:
                current = pre_status.value if pre_status is not None else "none"
                raise ValidationError(
                    f"Only {expected_delivery_status.value} deliveries can be re-dispatched "
                    f"(current status: {current})",
                    error_code="STAFF_DELIVERY_NOT_REDISPATCHABLE",
                )
            if delivery is not None and delivery_date is None:
                raise ValidationError(
                    "A delivery date is required once the order has been released to drivers",
                    error_code="DELIVERY_DATE_REQUIRED",
                )
            if delivery_date is not None and delivery_date > cls.reschedule_date_bounds(order)[1]:
                raise ValidationError(
                    "The new delivery date is after the customer's contract ends",
                    error_code="ORDER_RESCHEDULE_PAST_CONTRACT_END",
                )

            # The pre-write picture the post-commit notices need (step 4).
            old_driver_user_id = delivery.delivery_person_id if delivery is not None else None
            driver_loses_stop = pre_status in cls.DRIVER_NOTICE_DELIVERY_STATUSES
            old_driver = db.session.get(User, old_driver_user_id) if driver_loses_stop else None
            old_driver_telegram_id = old_driver.telegram_id if old_driver is not None else None
            old_driver_order_info = build_staff_order_info(delivery) if driver_loses_stop else None
            # R13, through the same call the detail payload publishes as
            # `reschedule_notifies_customer`, so the modal's promise and the
            # notice queued after the commit are one expression. Safe here:
            # `order.delivery` was expired above and resolves to the locked row,
            # still in its pre-reschedule status.
            notify_customer = cls.will_notify_customer(order)
            from_date = order.delivery_date
            from_start, from_end = order.delivery_window_start, order.delivery_window_end

            order.delivery_date = delivery_date
            order.delivery_window_start = window_start
            order.delivery_window_end = window_end

            landed_status = None
            if delivery is not None:
                # R23: a reschedule never confirms an order. Its only order
                # move is R6's out_for_delivery -> confirmed (a direct write
                # plus history, no CONFIRMED side effects). A PENDING order
                # that owns a row stays PENDING: confirming it would go through
                # `update_order_status`, which deducts a non-cash order's stock
                # on CONFIRMED, for an order nobody has paid for.
                post_status = OrderStatus.CONFIRMED if order.status == OrderStatus.OUT_FOR_DELIVERY else order.status
                # R5, read off the NEW date: past its release instant -> back in
                # today's pool; otherwise held until that instant. Decided on
                # the status the order will have after the pull-back below.
                #
                # Whether that status may be offered to drivers is the release
                # gate's own constant, asked here rather than restated. So a
                # PENDING order's row is held even on a due date (R23's
                # corollary): `scheduled` would put an unpaid order in today's
                # pool and broadcast it. Confirming the order releases the held
                # row: CONFIRMED runs `ensure_delivery_if_due`, which hands it
                # to `_release_held_delivery`.
                now = get_utc_now()
                landed_status = (
                    DeliveryStatus.SCHEDULED
                    if post_status in RELEASABLE_ORDER_STATUSES and cls.release_at(order) <= now
                    else DeliveryStatus.RESCHEDULED
                )
                StaffService._pull_back_delivery(
                    delivery,
                    actor_user_id,
                    target_status=landed_status,
                    reason=reason,
                    notes=f"Rescheduled to {delivery_date.isoformat()}",
                    restore_order=(post_status != order.status),
                )
                DeliveryService().stamp_schedule(delivery, order, now=now)
            delivery_id = delivery.id if delivery is not None else None

        return RescheduleOutcome(
            order_id=order_id,
            delivery_id=delivery_id,
            new_date=delivery_date,
            pre_status=pre_status,
            landed_status=landed_status,
            old_driver_user_id=old_driver_user_id,
            old_driver_telegram_id=old_driver_telegram_id,
            old_driver_order_info=old_driver_order_info,
            notify_customer=notify_customer,
            # JSON-safe values only: the audit row's column is JSON, and a
            # `date` in it is dropped with nothing but a log line.
            audit={
                "actor_user_id": actor_user_id,
                "from_delivery_date": from_date.isoformat() if from_date else None,
                "from_window": format_delivery_window(from_start, from_end),
                "to_delivery_date": delivery_date.isoformat() if delivery_date else None,
                "to_window": format_delivery_window(window_start, window_end),
                "delivery_status_before": pre_status.value if pre_status else None,
                "driver_before": old_driver_user_id,
                # The row status THIS transaction committed. An order with no
                # row reports None even when the release gate creates one
                # straight after.
                "landed_status": landed_status.value if landed_status else None,
                "reason": reason,
            },
        )

    @classmethod
    def _run_post_commit(cls, outcome: RescheduleOutcome) -> None:
        """The side effects of a committed reschedule (spec §3.4 step 6).

        Each step is isolated by `_best_effort`, so a broker or webhook outage
        in one cannot swallow the others. The customer notice matters most
        when the driver's does not go out, and the reverse.
        """
        from business_app import db

        if outcome.landed_status == DeliveryStatus.SCHEDULED and outcome.pre_status != DeliveryStatus.SCHEDULED:
            # Newly in today's pool. A same-day edit of a row that was already
            # in the pool is not a new order to anyone, so it is not
            # re-broadcast. A PENDING order's row never lands `scheduled`
            # (R23's corollary), so an unpaid order is never offered here.
            cls._best_effort(outcome.order_id, "offer_to_drivers", cls._offer_to_drivers, outcome.delivery_id)
        if outcome.delivery_id is None:
            # No row: only the order's date moved, so ask the release gate, as
            # before this feature. An undated or due-today order gets its
            # delivery now rather than at the next sweep tick; `create_delivery`
            # makes its own offer, which is why the branch above skips no-row
            # orders.
            cls._best_effort(
                outcome.order_id,
                "release",
                lambda: cls.ensure_delivery_if_due(db.session.get(Order, outcome.order_id)),
            )
        if outcome.notify_customer:
            cls._best_effort(outcome.order_id, "customer_notice", cls._notify_customer, outcome.order_id)
        if outcome.pre_status in cls.DRIVER_NOTICE_DELIVERY_STATUSES:
            cls._best_effort(outcome.order_id, "driver_notice", cls._notify_old_driver, outcome)
            cls._best_effort(
                outcome.order_id, "route_refresh", cls._refresh_old_drivers_route, outcome.old_driver_user_id
            )
        cls._best_effort(outcome.order_id, "audit", cls._log_reschedule_audit, outcome)

    @staticmethod
    def _offer_to_drivers(delivery_id: int) -> None:
        """The same fan-out a brand-new delivery gets: the auto-assign timer,
        then the diversion evaluator, which itself broadcasts."""
        from business_app import db
        from business_app.services.delivery_service import DeliveryService

        DeliveryService().offer_to_drivers(db.session.get(Delivery, delivery_id))

    @staticmethod
    def _notify_customer(order_id: int) -> None:
        """Only the order id travels. The task re-reads the live order when it
        sends, so a quick second correction goes out with the corrected date."""
        from business_app.tasks.notification_tasks import send_delivery_rescheduled_notification_task

        send_delivery_rescheduled_notification_task.delay(order_id)

    @staticmethod
    def _notify_old_driver(outcome: RescheduleOutcome) -> None:
        """The card the driver last saw, plus the new date. `rescheduled_to` is
        what switches the staff bot from "removed from your route" to the
        "rescheduled to <date> by dispatch" copy (spec §4.2)."""
        from business_app.tasks.staff_tasks import notify_staff_order_unassigned

        notify_staff_order_unassigned.delay(
            outcome.old_driver_telegram_id,
            {**outcome.old_driver_order_info, "rescheduled_to": outcome.new_date.isoformat()},
        )

    @staticmethod
    def _refresh_old_drivers_route(driver_user_id: int) -> None:
        """The existing route-updated push, so the driver's route card redraws
        without the stop `_pull_back_delivery` took off their sequence."""
        from business_app.services.route_edit_service import RouteEditService

        RouteEditService._notify_route_updated(driver_user_id)

    @staticmethod
    def _best_effort(order_id: int, step: str, fn: Callable[..., Any], *args: Any) -> None:
        """Run one post-commit side effect of a reschedule, isolated from the rest.

        The reschedule is already committed. Raising here would tell the admin
        it failed when it did not, and would skip every step after this one.
        The rollback clears a session a failed step may have left unusable, so
        the next step starts clean; nothing of the reschedule is left to lose.
        """
        from business_app import db

        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception("order_reschedule_post_commit_failed order=%s step=%s", order_id, step)

    @staticmethod
    def _log_reschedule_audit(outcome: RescheduleOutcome) -> None:
        """After the commit, so only a reschedule that happened is ever recorded."""
        from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger

        audit_logger.log_event(
            event_type=AuditEventType.ORDER_UPDATED,
            action="order_rescheduled",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(outcome.order_id),
            additional_data=outcome.audit,
        )
