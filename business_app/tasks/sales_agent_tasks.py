"""Celery tasks for the sales module: pushes to the staff bot and approver alerts.

Registered in tasks/celery_app.py `include`; unrouted (default queue).
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from celery import shared_task

from business_app.tasks.staff_tasks import _send_staff_webhook
from business_app.utils.bot_webhook import trigger_bot_webhook
from shared.staff_constants import SALES_EVENT_MORNING_DIGEST

logger = logging.getLogger(__name__)


@shared_task(name="sales.push_sales_event", bind=True, max_retries=3, default_retry_delay=60)
def push_sales_event(self, telegram_id: int, event: str, payload: dict):
    """Push one sales event to one staff member's Telegram chat.

    Retried like its sibling `push_agent_order_confirmation`: this is the only channel that
    carries `agent_order_confirmed` / `agent_order_declined` back to the agent who is standing
    in the shop, and a single unreachable moment used to drop that answer for good (M29).

    A retry must not speak twice. `_send_staff_webhook` mints a fresh uuid `event_id` per CALL,
    and the staff bot's dedup is a Redis SET NX on that id — so only replays carrying the SAME
    id collapse. Celery keeps `request.id` across `self.retry()`, which makes the task's own id
    the one stable key available here; called directly (a test, a synchronous caller) there is
    no id and the sender's uuid stands, exactly as before.
    """
    data = {"telegram_id": telegram_id, "event": event, "payload": payload}
    if self.request.id:
        data["event_id"] = f"sales-event:{self.request.id}"
    if not _send_staff_webhook("/internal/sales-event", data):
        raise self.retry(exc=RuntimeError(f"sales-event webhook failed for {event}"))
    return {"success": True, "event": event, "telegram_id": telegram_id}


@shared_task(name="sales.push_agent_order_confirmation", bind=True, max_retries=3, default_retry_delay=60)
def push_agent_order_confirmation(self, order_id: int):
    """Ask a store to confirm the order its sales agent just placed for it.

    `request_id` is the confirmation row's own id and is passed to
    `trigger_bot_webhook` as the X-Request-ID: a retried task collapses to one
    message in the store's chat instead of two sets of live buttons.
    """
    from business_app.models.order import Order
    from business_app.models.sales_visits import OrderConfirmationRequest
    from business_app.models.user import User

    request_row = (
        OrderConfirmationRequest.query.filter_by(order_id=order_id, status="pending")
        .order_by(OrderConfirmationRequest.id.desc())
        .first()
    )
    if request_row is None:
        # Already answered or expired between enqueue and run — nothing to ask.
        return {"success": False, "reason": "no_pending_request", "order_id": order_id}

    order = Order.query.get(order_id)
    if order is None:
        return {"success": False, "reason": "order_not_found", "order_id": order_id}

    outlet = request_row.outlet
    customer = outlet.user if outlet is not None else None
    if customer is None or not customer.telegram_id:
        return {"success": False, "reason": "no_telegram_id", "order_id": order_id}

    agent = User.query.get(order.created_by_staff_id) if order.created_by_staff_id else None
    payload = {
        "telegram_id": int(customer.telegram_id),
        "order_id": order.id,
        "order_number": order.order_number,
        "request_id": request_row.request_id,
        "agent_name": agent.full_name if agent is not None else None,
        # Raw numbers cross the wire; the customer bot formats money and dates
        # with its own locale helpers (no pre-rendered strings).
        "items": [
            {"name": item.product.name if item.product else None, "qty": int(item.quantity)}
            for item in order.order_items
        ],
        "total": float(order.total_amount or 0),
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
    }

    result = trigger_bot_webhook("/internal/agent-order-proposed", payload, request_id=request_row.request_id)
    if result.get("success") is False:
        message = str(result.get("message") or "")
        if "not configured" in message:
            # Permanent in this environment: retrying burns the queue. The row stays
            # pending and `sales.expire_agent_order_confirmations` still confirms the
            # order, so no order is ever stranded (spec, Failure modes).
            logger.warning("Bot webhook unconfigured — agent order %s not pushed", order_id)
            return {"success": False, "reason": "bot_webhook_unconfigured", "order_id": order_id}
        raise self.retry(exc=RuntimeError(message or "agent-order-proposed webhook failed"))
    return {"success": True, "order_id": order_id, "request_id": request_row.request_id}


@shared_task(name="sales.expire_agent_order_confirmations")
def expire_agent_order_confirmations():
    """Stores that never answered: expire the request, confirm the order, let the
    delivery be the acceptance (D5)."""
    from business_app.services.sales.agent_order_confirmation_service import AgentOrderConfirmationService

    return {"success": True, "expired": AgentOrderConfirmationService.expire_due()}


@shared_task(name="sales.abandon_stale_visits")
def abandon_stale_visits():
    """Close visits an agent left open — dead phone, end of day."""
    from business_app.services.sales.visit_service import VisitService

    return {"success": True, "abandoned": VisitService.abandon_stale()}


@shared_task(name="sales.recompute_all_outlets")
def recompute_all_outlets():
    """01:00 local: republish `next_visit_due_at` for every non-lost outlet (D7).

    The due list only READS that column, so an outlet whose stock-out projection moved
    overnight — or one nothing happened to at all — is invisible until this runs. It is
    also the job that owns the prod backfill step the 2a handover asks for by hand.
    """
    from business_app.services.sales.replenishment_service import ReplenishmentService

    return {"success": True, "outlets": ReplenishmentService.recompute_all()}


@shared_task(name="sales.update_outlet_stages")
def update_outlet_stages():
    """01:10 local: at-risk / dormant / reactivation / converted-try-out stage moves."""
    from business_app.services.sales.outlet_service import OutletService

    return {"success": True, **OutletService.update_stages()}


@shared_task(name="sales.snapshot_agent_day_plans")
def snapshot_agent_day_plans():
    """01:20 local: store what each agent's day was supposed to be (R1).

    Ten minutes behind the stage sweep and twenty behind the recompute, because it
    READS the due date both of them rewrite — snapshotting earlier would file a
    plan against a column that is still moving.
    """
    from business_app.services.sales.day_plan_service import AgentDayPlanService

    return {"success": True, "agents": AgentDayPlanService.snapshot_all()}


@shared_task(name="sales.notify_managers_activation_requested", bind=True)
def notify_managers_activation_requested(self, outlet_id: int):
    """IN_APP alert to admins/managers + staff-bot push to operators for a new activation request."""
    from business_app.models.sales import Outlet
    from business_app.models.user import User
    from business_app.services.notification_service import NotificationService
    from business_app.services.staff_service import StaffService
    from business_app.utils.constants import NotificationChannel, NotificationType
    from business_app.utils.translations import get_translation
    from shared.enums import UserRole
    from shared.staff_constants import SALES_EVENT_ACTIVATION_REQUESTED

    outlet = Outlet.query.get(outlet_id)
    if outlet is None:
        return {"success": False, "reason": "outlet_not_found"}
    payload = {"outlet_id": outlet.id, "outlet_name": outlet.name, "reason": None}

    managers = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == "active").all()
    notified = 0
    for manager in managers:
        try:
            language = getattr(manager, "preferred_language", None) or "en"

            def _get_translated(field_name, lang, _name=outlet.name, _language=language):
                target = lang or _language
                if field_name == "subject":
                    return get_translation("staff.notification.subject.outlet_activation_requested", language=target)
                # Markup-FREE key: the admin UI renders notification `content` as plain text, so the
                # <b>...</b> in `staff.sales.notify.activation_requested` would ship as literal tags.
                # That HTML key stays reserved for the staff-bot push (parse_mode='HTML').
                return get_translation(
                    "staff.notification.content.outlet_activation_requested", language=target, outlet_name=_name
                )

            NotificationService().send_notification(
                user_id=manager.id,
                notification_type=NotificationType.SYSTEM_ALERT,
                channels=[NotificationChannel.IN_APP],
                template_data={"outlet_id": outlet.id, "outlet_name": outlet.name},
                template_override=SimpleNamespace(
                    subject=_get_translated("subject", language),
                    content=_get_translated("content", language),
                    get_translated=_get_translated,
                ),
            )
            notified += 1
        except Exception:  # noqa: BLE001 — one bad recipient must not block the rest
            logger.exception("Failed to notify manager %s about outlet %s", manager.id, outlet.id)

    operators = User.query.filter(
        StaffService.staff_role_member_filter(UserRole.OPERATOR.value),
        User.status == "active",
        User.telegram_id.isnot(None),
    ).all()
    for operator in operators:
        push_sales_event.delay(int(operator.telegram_id), SALES_EVENT_ACTIVATION_REQUESTED, payload)
    return {"success": True, "managers": notified, "operators": len(operators)}


@shared_task(name="sales.send_agent_morning_digest")
def send_agent_morning_digest():
    """The agent's day, at `SALES_DIGEST_LOCAL_TIME` (spec § *Stages, due dates and alerts*).

    Beat runs in `DISPLAY_TIMEZONE` (`celery_app.py:381`), so the crontab that fires this IS
    the local time the owner configured — there is nothing to convert here.

    ONE `now` for the whole run: every agent's payload is built from the same instant, so
    two agents cannot straddle a day boundary and be told about different days by the same
    job. An agent with nothing to report is SKIPPED rather than sent an empty message — this
    is the only sales push that arrives with a sound, and a silent-Sunday habit is exactly
    what makes the Monday one invisible.

    One bad agent must not cost the rest their morning: a failure to build is logged and the
    loop continues, the `notify_managers_activation_requested` precedent.
    """
    from business_app.models.user import User
    from business_app.services.sales.digest_service import AgentDigestService
    from business_app.services.staff_service import StaffService
    from business_app.utils.exceptions import ForbiddenError
    from shared.enums import UserRole

    now = datetime.now(timezone.utc)
    agents = User.query.filter(
        StaffService.staff_role_member_filter(UserRole.SALES_AGENT.value),
        User.status == "active",
        User.telegram_id.isnot(None),
    ).all()

    sent = 0
    for agent in agents:
        try:
            # The staff bot's own door: a deactivated agent is refused by every
            # /staff/sales route, so a digest they cannot act on is noise with a sound.
            StaffService.assert_staff_active(agent)
        except ForbiddenError:
            continue
        try:
            payload = AgentDigestService.build(agent.id, now=now)
        except Exception:  # noqa: BLE001 — one bad estate must not silence every agent
            logger.exception("Failed to build the morning digest for agent %s", agent.id)
            continue
        if payload is None:
            continue
        push_sales_event.delay(int(agent.telegram_id), SALES_EVENT_MORNING_DIGEST, payload)
        sent += 1
    return {"success": True, "agents": len(agents), "sent": sent}


@shared_task(name="sales.notify_managers_exception_summary")
def notify_managers_exception_summary():
    """08:00 local: yesterday's exception count, once, to every admin and manager (R9).

    Beat runs in `DISPLAY_TIMEZONE` (`celery_app.py:423`), so the crontab that fires this IS
    08:00 Tashkent and there is nothing to convert. The DAY is yesterday's local calendar
    day: at 08:00 today is four hours old and is not yet a day anyone can act on.

    COUNT ONLY, and only the DATED types (`ExceptionFeedService.count_for_day`) — the spec's
    shape, and the honest one: `unvisited` and `duplicate_open_tryout` are still true this morning and
    every morning after, so including them would make the number grow while the estate stood
    still. The feed itself is a query, not a push; this message exists to point at it, which is
    why `EXCEPTIONS_FEED_PATH` is published as data rather than typed into the copy.

    Silent at zero: this is the only sales alert an admin receives on a schedule, and a
    "0 exceptions" line every morning is how the one that says 11 becomes invisible.

    One bad recipient must not cost the rest their morning — the
    `notify_managers_activation_requested` precedent, same per-recipient try/except.
    """
    from business_app.models.user import User
    from business_app.services.notification_service import NotificationService
    from business_app.services.sales.exception_feed_service import EXCEPTIONS_FEED_PATH, ExceptionFeedService
    from business_app.utils.constants import NotificationChannel, NotificationType
    from business_app.utils.local_windows import local_date
    from business_app.utils.translations import get_translation
    from shared.enums import UserRole

    day = local_date() - timedelta(days=1)
    count = ExceptionFeedService.count_for_day(day)
    if count == 0:
        return {"success": True, "day": day.isoformat(), "count": 0, "managers": 0}

    managers = User.query.filter(User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == "active").all()
    notified = 0
    for manager in managers:
        try:
            language = getattr(manager, "preferred_language", None) or "en"

            def _get_translated(field_name, lang, _count=count, _day=day.isoformat(), _language=language):
                target = lang or _language
                if field_name == "subject":
                    return get_translation("staff.notification.subject.sales_exception_summary", language=target)
                # Markup-FREE, like its activation-request twin: the admin UI renders
                # notification `content` as plain text, so any <b> would ship as literal tags.
                return get_translation(
                    "staff.notification.content.sales_exception_summary",
                    language=target,
                    count=_count,
                    day=_day,
                    path=EXCEPTIONS_FEED_PATH,
                )

            NotificationService().send_notification(
                user_id=manager.id,
                notification_type=NotificationType.SYSTEM_ALERT,
                channels=[NotificationChannel.IN_APP],
                template_data={"day": day.isoformat(), "count": count, "path": EXCEPTIONS_FEED_PATH},
                template_override=SimpleNamespace(
                    subject=_get_translated("subject", language),
                    content=_get_translated("content", language),
                    get_translated=_get_translated,
                ),
            )
            notified += 1
        except Exception:  # noqa: BLE001 — one bad recipient must not block the rest
            logger.exception("Failed to send manager %s the %s exception summary", manager.id, day)
    return {"success": True, "day": day.isoformat(), "count": count, "managers": notified}
