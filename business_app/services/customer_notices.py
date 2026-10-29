"""The notices a customer is owed after a commit (spec 2026-10-23 §2.3-2.5, §2.7, §2.8, §4.4, §4.5, §9 Messages).

ONE channel per notice: Telegram through the customer bot's `POST /internal/customer-notice`
(telegram_bot/webhook_server.py::customer_notice_handler, which renders the seeded copy and the buttons) when
the bot is connected; otherwise email (a file template per kind under templates/emails/, named in
`EmailTemplateService.TEMPLATE_MAPPING` / `EMAIL_SUBJECTS`); otherwise nothing. Never SMS. The channel is
`NotificationService._resolve_telegram_else_email`, the rule every Telegram-else-email notice uses.

Only Celery tasks call this, and only tasks published after the causing transaction committed
(`subscription_notices.deliver`, `cancel_abandoned_orders`), so nothing here runs inside it. A send reads the
order as it is NOW: an order created and cancelled before its notice went out is not announced.

The bot payload is an envelope plus the kind's facts (tests pin both sides: tests/integration/
customer_notice_builders.py::CONTRACT_KEYS and tests/telegram_bot/test_webhook_customer_notice.py):
    envelope                      notice_id, kind, telegram_id, language
    subscription_order_created    order_id, order_number, delivery_date, window_start, window_end, time_kind,
                                  items [{name, qty}], not_included [name], total, payment, amount_due,
                                  prepaid_applied, deadline
    subscription_delivery_skipped subscription_name, skipped_dates, reason, next_delivery_date
    subscription_paid_order_kept  order_id, order_number, delivery_date, trigger, next_delivery_date
    order_payment_expired         order_id, order_number, deadline
    subscription_schedule_changed subscription_id, subscription_name, changes, old_window, new_window, old_weekday,
                                  new_weekday, added_weekdays, next_delivery_date, time_kind
Dates are ISO, money a float, a deadline an ISO instant in local time: each channel formats them by its own
rule (the bot's `utils.format_date`, the email's `helpers.format_datetime`). `payment` is the backend's one
answer to "how is this order paid": cash, company_account, online_unpaid (Pay now, the deadline) or
online_paid. `language` is decided here once (`shared.i18n_rendering.reminder_language`); the bot renders in
it and never reads the customer's stored language. `notice_id` is stable across retries: the bot sends one
message per id.
"""

import logging
from datetime import date, datetime
from typing import Any, Dict, Optional, Sequence
from zoneinfo import ZoneInfo

from business_app import db
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.services import subscription_display
from business_app.services.order_service import OrderService
from business_app.services.telegram_reachability_service import TelegramReachabilityService
from business_app.utils.bot_webhook import trigger_bot_webhook
from business_app.utils import display_labels
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.helpers import format_datetime
from business_app.utils.payment_projection import net_open_receivable_amount, paid_amount
from business_app.utils.timezone_utils import get_utc_now
from business_app.utils.translations import get_translation
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
from shared.i18n_rendering import reminder_language

logger = logging.getLogger(__name__)

BOT_ROUTE = "/internal/customer-notice"
# The route's answer when Telegram refused the send because the customer blocked the bot.
_BOT_BLOCKED_STATUS = 410

SUBSCRIPTION_ORDER_CREATED = NotificationType.SUBSCRIPTION_ORDER_CREATED.value
SUBSCRIPTION_DELIVERY_SKIPPED = NotificationType.SUBSCRIPTION_DELIVERY_SKIPPED.value
SUBSCRIPTION_PAID_ORDER_KEPT = NotificationType.SUBSCRIPTION_PAID_ORDER_KEPT.value
ORDER_PAYMENT_EXPIRED = NotificationType.ORDER_PAYMENT_EXPIRED.value
SUBSCRIPTION_SCHEDULE_CHANGED = NotificationType.SUBSCRIPTION_SCHEDULE_CHANGED.value
# The facts a schedule change carries, as `SubscriptionService.reconcile_with_slots` records them.
_SCHEDULE_CHANGED_FACTS = (
    "subscription_id",
    "subscription_name",
    "changes",
    "old_window",
    "new_window",
    "old_weekday",
    "new_weekday",
    "added_weekdays",
    "next_delivery_date",
    "time_kind",
)

PAY_CASH = "cash"
PAY_COMPANY_ACCOUNT = "company_account"
PAY_ONLINE_UNPAID = "online_unpaid"
PAY_ONLINE_PAID = "online_paid"


class RetryableNoticeError(Exception):
    """The bot was unreachable or failed: the task retries. The bot's dedup on `notice_id` keeps it one message."""


# --------------------------------------------------------------------------- the notices


def send_order_created(order: Order, *, notice_id: str, not_included_product_ids: Sequence[int] = ()) -> str:
    """A subscription order was created (§4.4). `not_included_product_ids` are the subscription's products that
    were no longer available and so left out of the order (spec 2026-10-26 §8.5)."""
    if order.status == OrderStatus.CANCELLED:
        return "skipped:order_inactive"
    return _send(
        order.user,
        SUBSCRIPTION_ORDER_CREATED,
        _order_created_facts(order, not_included_product_ids),
        notice_id=notice_id,
        order=order,
    )


def send_delivery_skipped(subscription, facts: Dict[str, Any], *, notice_id: str) -> str:
    """The system could not create a scheduled order and skipped that delivery (§2.8)."""
    user = subscription.user
    reason_code = facts.get("reason_code")
    return _send(
        user,
        SUBSCRIPTION_DELIVERY_SKIPPED,
        {
            "subscription_name": subscription.name,
            "skipped_dates": list(facts.get("skipped_dates") or []),
            "reason": get_translation(f"api.subscriptions.skip_reason.{reason_code}", _language(user)),
            "next_delivery_date": facts.get("next_delivery_date"),
        },
        notice_id=notice_id,
    )


def send_paid_order_kept(order: Order, facts: Dict[str, Any], *, notice_id: str) -> str:
    """A paid upcoming order stays after skip, pause or cancel; the action applies from the next cycle (§2.7)."""
    return _send(
        order.user,
        SUBSCRIPTION_PAID_ORDER_KEPT,
        {
            "order_id": order.id,
            "order_number": order.order_number,
            "delivery_date": order.delivery_date.isoformat() if order.delivery_date else facts.get("delivery_date"),
            "trigger": facts.get("trigger"),
            "next_delivery_date": facts.get("next_delivery_date"),
        },
        notice_id=notice_id,
    )


def send_schedule_changed(subscription, facts: Dict[str, Any], *, notice_id: str) -> str:
    """A slot write changed the subscription's delivery time or day (spec 2026-10-27 §6.5): the facts recorded with
    the change, each a line of the message."""
    return _send(
        subscription.user,
        SUBSCRIPTION_SCHEDULE_CHANGED,
        {key: facts.get(key) for key in _SCHEDULE_CHANGED_FACTS},
        notice_id=notice_id,
    )


def send_payment_expired(order_id: int) -> str:
    """An unpaid order was cancelled at its payment deadline (§4.5): "not paid by <deadline>"."""
    order = db.session.get(Order, order_id)
    if order is None:
        return "skipped:order_missing"
    if order.status != OrderStatus.CANCELLED:
        return "skipped:not_cancelled"
    return _send(
        order.user,
        ORDER_PAYMENT_EXPIRED,
        {
            "order_id": order.id,
            "order_number": order.order_number,
            "deadline": _local_iso(OrderService.payment_deadline(order)),
        },
        notice_id=f"order-payment-expired:{order.id}",
    )


# --------------------------------------------------------------------------- facts


def payment_facts(order: Order) -> Dict[str, Any]:
    """How the order is paid, as the notice says it (§4.4): the rail, what the driver collects and the prepaid
    credit already applied (cash), or the deadline an unpaid online order must be paid by (§4.5)."""
    facts = {"payment": None, "amount_due": None, "prepaid_applied": None, "deadline": None}
    payment = order.payment
    if order.payment_method == PaymentMethod.CASH:
        prepaid = paid_amount(payment) if payment is not None else 0
        due = net_open_receivable_amount(payment) if payment is not None else order.total_amount
        return {**facts, "payment": PAY_CASH, "amount_due": float(due), "prepaid_applied": float(prepaid) or None}
    if order.payment_method == PaymentMethod.BUSINESS_ACCOUNT:
        return {**facts, "payment": PAY_COMPANY_ACCOUNT}
    if order.is_paid or (payment is not None and payment.status == PaymentStatus.COMPLETED):
        return {**facts, "payment": PAY_ONLINE_PAID}
    return {**facts, "payment": PAY_ONLINE_UNPAID, "deadline": _local_iso(OrderService.payment_deadline(order))}


def _order_created_facts(order: Order, not_included_product_ids: Sequence[int] = ()) -> Dict[str, Any]:
    language = _language(order.user)
    has_window = order.delivery_window_start is not None and order.delivery_window_end is not None
    return {
        "order_id": order.id,
        "order_number": order.order_number,
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "window_start": order.delivery_window_start.strftime("%H:%M") if has_window else None,
        "window_end": order.delivery_window_end.strftime("%H:%M") if has_window else None,
        # Which of the three delivery times this is (spec 2026-10-27 §6.3): the window, Any time, or to be confirmed.
        "time_kind": subscription_display.time_kind(order),
        "items": [
            {"name": _product_name(item.product, item.product_id, language), "qty": item.quantity}
            for item in order.order_items
        ],
        "not_included": [
            _product_name(db.session.get(Product, product_id), product_id, language)
            for product_id in not_included_product_ids
        ],
        "total": float(order.total_amount or 0),
        **payment_facts(order),
    }


def _product_name(product: Optional[Product], product_id: int, language: str) -> str:
    if product is None:
        return f"#{product_id}"
    return product.get_translated("name", language) or product.name


def _language(user: User) -> str:
    return reminder_language(getattr(user, "preferred_language", None))


def _local_iso(instant: datetime) -> str:
    return instant.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).isoformat()


# --------------------------------------------------------------------------- the email's words


def email_template_data(
    kind: str, facts: Dict[str, Any], language: str, *, pay_url: Optional[str] = None
) -> Dict[str, Any]:
    """The facts as the email template prints them, in `language`: dates and the deadline by the email's own
    date rule, money with thousands separators, the window as one span, weekdays by name, and the Click link
    (`pay_url`).
    `order_id` stays in, so the email's `Notification` row is linked to its order."""
    data: Dict[str, Any] = dict(facts)
    for key in ("delivery_date", "next_delivery_date"):
        data[key] = _email_date(facts.get(key), language)
    data["skipped_dates"] = ", ".join(_email_date(day, language) for day in facts.get("skipped_dates") or [])
    data["deadline"] = (
        format_datetime(datetime.fromisoformat(facts["deadline"]), "datetime", language)
        if facts.get("deadline")
        else None
    )
    data["time_label"] = _email_time_label(facts, language)
    for key in ("old_weekday", "new_weekday"):
        data[f"{key}_label"] = _email_weekday(facts.get(key), language)
    data["added_weekdays_label"] = ", ".join(_email_weekday(day, language) for day in facts.get("added_weekdays") or [])
    # Nothing due (a prepaid balance covers it) and no prepaid credit both print as absent.
    for key in ("total", "amount_due", "prepaid_applied"):
        data[key] = NotificationService._format_money(facts[key]) if facts.get(key) else None
    data["pay_url"] = pay_url
    return data


def _email_time_label(facts: Dict[str, Any], language: str) -> str:
    """The delivery time the email prints: the window's span, "any time" or "time to be confirmed" in `language`
    (`subscription_display.time_words`). A payload queued before `time_kind` existed has none: the old rule decides,
    a window or "to be confirmed"."""
    has_span = facts.get("window_start") and facts.get("window_end")
    span = f"{facts['window_start']}–{facts['window_end']}" if has_span else None
    kind = facts.get("time_kind") or (subscription_display.TIME_WINDOW if span else subscription_display.TIME_TBC)
    if kind == subscription_display.TIME_WINDOW and not span:
        kind = subscription_display.TIME_TBC
    return subscription_display.time_words(kind, span, language=language, inline=True)


def _email_weekday(value: Optional[int], language: str) -> Optional[str]:
    return display_labels.weekday_name(value, language) if value is not None else None


def _email_date(value: Optional[str], language: str) -> Optional[str]:
    return format_datetime(date.fromisoformat(value), "date", language) if value else None


# --------------------------------------------------------------------------- one channel


def _send(user: User, kind: str, facts: Dict[str, Any], *, notice_id: str, order: Optional[Order] = None) -> str:
    """Telegram when the bot is connected, else email, else nothing. A bot the customer blocked marks the
    account blocked and falls through to email in the same attempt."""
    language = _language(user)
    channels = NotificationService._resolve_telegram_else_email(user)
    if NotificationChannel.TELEGRAM in channels:
        outcome = _send_telegram(user, kind, facts, language, notice_id)
        if outcome is not None:
            return outcome
        channels = NotificationService._resolve_telegram_else_email(user)
    if NotificationChannel.EMAIL in channels:
        return _send_email(user, kind, facts, language, order)
    logger.info("Customer notice %s not sent: user %s has no deliverable channel", notice_id, user.id)
    return "no_channel"


def _send_telegram(user: User, kind: str, facts: Dict[str, Any], language: str, notice_id: str) -> Optional[str]:
    """POST the notice to the customer bot. None when the customer blocked the bot (the caller emails instead)."""
    payload = {
        "notice_id": notice_id,
        "kind": kind,
        "telegram_id": str(user.telegram_id),
        "language": language,
        **facts,
    }
    result = trigger_bot_webhook(BOT_ROUTE, payload, request_id=notice_id)
    if result.get("success"):
        return "telegram"
    status = result.get("status_code")
    if status == _BOT_BLOCKED_STATUS:
        TelegramReachabilityService.mark_blocked(user, source="send_410", at=get_utc_now())
        db.session.commit()
        logger.info("Customer notice %s: user %s blocked the bot; trying email", notice_id, user.id)
        return None
    if status is None and "not configured" in str(result.get("message", "")).lower():
        # A deployment fault, not a blip: a retry cannot fix it.
        logger.error("Customer notice %s not sent: %s", notice_id, result.get("message"))
        return "telegram_unconfigured"
    raise RetryableNoticeError(f"customer notice {notice_id}: bot answered {status or result.get('message')}")


def _send_email(user: User, kind: str, facts: Dict[str, Any], language: str, order: Optional[Order]) -> str:
    """One attempt through the shared Brevo path, never retried (a retry could mail the customer twice)."""
    pay_url = _pay_url(order) if facts.get("payment") == PAY_ONLINE_UNPAID else None
    results = NotificationService().send_notification(
        user.id,
        NotificationType(kind),
        channels=[NotificationChannel.EMAIL],
        template_data=email_template_data(kind, facts, language, pay_url=pay_url),
        language=language,
    )
    outcome = (results or {}).get(NotificationChannel.EMAIL.value) or {}
    if outcome.get("success"):
        return "email"
    logger.warning("Customer notice email (%s) to user %s failed: %s", kind, user.id, outcome)
    return "email_failed"


def _pay_url(order: Optional[Order]) -> Optional[str]:
    """The Click link the email carries (§4.4). `create_payment_link` only flushes the link onto the payment, so
    it is committed here. No link (Click not configured, a gateway fault) still sends the email without one."""
    from business_app.services.payment_service import PaymentService

    if order is None or order.payment is None:
        return None
    try:
        url = PaymentService().create_payment_link(order.payment.id)["payment_url"]
        db.session.commit()
        return url
    except Exception:  # noqa: BLE001 - the notice still goes out
        db.session.rollback()
        logger.exception("No payment link for the notice of order %s", order.id)
        return None
