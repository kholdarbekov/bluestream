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
                                  prepaid_applied, deadline, subscription_id, actions
    subscription_delivery_skipped subscription_id, subscription_name, skipped_dates, reason, next_delivery_date
    subscription_paid_order_kept  order_id, order_number, delivery_date, trigger, next_delivery_date,
                                  subscription_id, actions
    order_payment_expired         order_id, order_number, deadline, subscription_id, actions
    subscription_schedule_changed subscription_id, subscription_name, changes, old_window, new_window, old_weekday,
                                  new_weekday, added_weekdays, removed_weekdays, next_delivery_date,
                                  kept_order_note, time_kind
    prepayment_returned           payout_id, amount, balance (money floats; the balance is read when sent)
    price_change_announcement     price_change_id, effective_date, prices [{name, new_price}], is_subscriber
    price_change_reminder         (the same facts as the announcement)
`subscription_id` is None for an ordinary order. `actions` is `customer_actions(order)`, the backend's published
answers (pay, cancel, confirm): the bot draws Pay from `actions["pay"]` and the email links the website page, and
neither re-derives the rule.
Dates are ISO, money a float, a deadline an ISO instant in local time: each channel formats them by its own
rule (the bot's `utils.format_date`, the email's `helpers.format_datetime`). `payment` is the backend's one
answer to "how is this order paid": cash, company_account, online_unpaid (the published Pay is allowed; `deadline`
is when it closes) or online_paid, and None (no payment line) for an unpaid online order whose Pay is refused or has
no deadline. `language` is decided here once (`shared.i18n_rendering.reminder_language`); the bot renders in
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
from business_app.services.customer_links import order_page_url_for, subscription_page_url, tracking_url
from business_app.services.customer_order_actions import customer_actions, pay_answer
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
PREPAYMENT_RETURNED = NotificationType.PREPAYMENT_RETURNED.value
PRICE_CHANGE_ANNOUNCEMENT = NotificationType.PRICE_CHANGE_ANNOUNCEMENT.value
PRICE_CHANGE_REMINDER = NotificationType.PRICE_CHANGE_REMINDER.value
PRICE_CHANGE_KINDS = frozenset({PRICE_CHANGE_ANNOUNCEMENT, PRICE_CHANGE_REMINDER})
# The facts a schedule change carries, as `SubscriptionService.reconcile_with_slots` records them. The next delivery
# and its kept-order note are read when the notice is sent (`send_schedule_changed`).
_SCHEDULE_CHANGED_FACTS = (
    "subscription_id",
    "subscription_name",
    "changes",
    "old_window",
    "new_window",
    "old_weekday",
    "new_weekday",
    "added_weekdays",
    "time_kind",
)

NOTICE_KINDS = frozenset(
    {
        SUBSCRIPTION_ORDER_CREATED,
        SUBSCRIPTION_DELIVERY_SKIPPED,
        SUBSCRIPTION_PAID_ORDER_KEPT,
        ORDER_PAYMENT_EXPIRED,
        SUBSCRIPTION_SCHEDULE_CHANGED,
        PREPAYMENT_RETURNED,
        PRICE_CHANGE_ANNOUNCEMENT,
        PRICE_CHANGE_REMINDER,
    }
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
    )


def send_delivery_skipped(subscription, facts: Dict[str, Any], *, notice_id: str) -> str:
    """The system could not create a scheduled order and skipped that delivery (§2.8)."""
    user = subscription.user
    reason_code = facts.get("reason_code")
    return _send(
        user,
        SUBSCRIPTION_DELIVERY_SKIPPED,
        {
            "subscription_id": subscription.id,
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
            **_order_facts(order, _language(order.user)),
        },
        notice_id=notice_id,
    )


def send_schedule_changed(subscription, facts: Dict[str, Any], *, notice_id: str) -> str:
    """A slot write changed the subscription's delivery time or day (spec 2026-10-27 §6.5): the facts recorded with
    the change, each a line of the message, and the next delivery as the card shows it now, with the note that an
    order already placed stays as it is (`SubscriptionService.notice_next_delivery`, in the notice's language)."""
    from business_app.services.subscription_service import SubscriptionService

    return _send(
        subscription.user,
        SUBSCRIPTION_SCHEDULE_CHANGED,
        {
            **{key: facts.get(key) for key in _SCHEDULE_CHANGED_FACTS},
            "removed_weekdays": facts.get("removed_weekdays") or [],  # a row recorded before lost days were told
            **SubscriptionService.notice_next_delivery(subscription, language=_language(subscription.user)),
        },
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
            **_order_facts(order, _language(order.user)),
        },
        notice_id=f"order-payment-expired:{order.id}",
    )


def send_prepayment_returned(payout_id: int) -> str:
    """An admin returned part of the customer's prepaid balance (spec 2026-11-10 P8). It goes to the payout's
    customer, the account the admin acted on. The balance is read now, from the one source
    (`CashCollectionService.get_customer_prepaid_balance`), so it is right even if the credit moved since the
    commit. A payout undone before this ran is not announced."""
    from business_app.models.payment import CustomerCreditPayout
    from business_app.services.cash_collection_service import CashCollectionService

    payout = db.session.get(CustomerCreditPayout, payout_id)
    if payout is None:
        return "skipped:payout_missing"
    if not payout.is_live:
        return "skipped:payout_undone"
    return _send(
        payout.customer,
        PREPAYMENT_RETURNED,
        {
            "payout_id": payout.id,
            "amount": float(payout.amount),
            "balance": float(CashCollectionService().get_customer_prepaid_balance(payout.customer_id)),
        },
        notice_id=f"prepayment_returned:{payout_id}",
    )


def send_price_change(price_change, user: User, kind: str, *, is_subscriber: bool, notice_id: str) -> str:
    """The announcement or the reminder of a price change (spec 2026-11-16 §5.3), to one person. The prices are the
    change's items in id order, each product named in the notice's language; `is_subscriber` is the caller's answer
    and only adds the subscription sentence. `notice_id` is stable across retries (the bot sends one message per id)."""
    if kind not in PRICE_CHANGE_KINDS:
        raise ValueError(f"{kind!r} is not a price-change notice kind")
    language = _language(user)
    return _send(
        user,
        kind,
        {
            "price_change_id": price_change.id,
            "effective_date": price_change.change_on.isoformat(),
            "prices": [
                {
                    "name": _product_name(item.product, item.product_id, language),
                    "new_price": float(item.new_price),
                }
                for item in price_change.items
            ],
            "is_subscriber": is_subscriber,
        },
        notice_id=notice_id,
    )


# --------------------------------------------------------------------------- facts


def payment_facts(order: Order) -> Dict[str, Any]:
    """How the order is paid, as the notice says it (§4.4): the rail, what the driver collects and the prepaid
    credit already applied (cash), or the deadline an unpaid online order must be paid by (§4.5).

    "Pay by" is the published Pay's own answer (`customer_order_actions.pay_answer`, which `customer_actions.pay` and
    the notice's `actions` read): the line is there only when that Pay is allowed and closes at a deadline, and its
    deadline is that instant, the one `customer_actions.pay`'s `deadline_label` is worded from. An online order whose
    Pay is refused (partly paid, nothing owed, its payment row on an offline rail, past its deadline) gets no payment
    line, so a notice never says "please pay by" beside a Pay that is not there; nor does one whose Pay has no
    deadline (the driver has it), which no notice could truthfully date."""
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
    refusal, closes_at = pay_answer(order)
    if refusal is not None or closes_at is None:
        return facts
    return {**facts, "payment": PAY_ONLINE_UNPAID, "deadline": _local_iso(closes_at)}


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
        **_order_facts(order, language),
    }


def _order_facts(order: Order, language: str) -> Dict[str, Any]:
    """What every order notice carries about the order's place on the customer's surfaces: the subscription it
    belongs to (None for an ordinary order) and the answers the backend publishes for it (Pay above all)."""
    return {"subscription_id": order.subscription_id, "actions": customer_actions(order, language=language)}


def _product_name(product: Optional[Product], product_id: int, language: str) -> str:
    if product is None:
        return f"#{product_id}"
    return product.get_translated("name", language) or product.name


def _language(user: User) -> str:
    return reminder_language(getattr(user, "preferred_language", None))


def _local_iso(instant: datetime) -> str:
    return instant.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).isoformat()


# --------------------------------------------------------------------------- the email's words


def email_template_data(kind: str, facts: Dict[str, Any], language: str) -> Dict[str, Any]:
    """The facts as the email template prints them, in `language`: dates and the deadline by the email's own
    date rule, money with thousands separators, the window as one span, weekdays by name, and the website links
    (never a payment link: a Pay opens the order's page, where the customer taps it). `pay_url` is there only
    when the published Pay is allowed.
    `order_id` stays in, so the email's `Notification` row is linked to its order."""
    data: Dict[str, Any] = dict(facts)
    for key in ("delivery_date", "next_delivery_date"):
        data[key] = _email_date(facts.get(key), language)
    data["effective_date"] = _email_date_in_words(facts.get("effective_date"), language)
    data["prices"] = [
        {"name": price["name"], "new_price": NotificationService._format_money(price["new_price"])}
        for price in facts.get("prices") or []
    ]
    data["skipped_dates"] = ", ".join(_email_date(day, language) for day in facts.get("skipped_dates") or [])
    data["deadline"] = (
        format_datetime(datetime.fromisoformat(facts["deadline"]), "datetime", language)
        if facts.get("deadline")
        else None
    )
    data["time_label"] = _email_time_label(facts, language)
    for key in ("old_weekday", "new_weekday"):
        data[f"{key}_label"] = _email_weekday(facts.get(key), language)
    for key in ("added_weekdays", "removed_weekdays"):
        data[f"{key}_label"] = ", ".join(_email_weekday(day, language) for day in facts.get(key) or [])
    # Nothing due (a prepaid balance covers it) and no prepaid credit both print as absent.
    for key in ("total", "amount_due", "prepaid_applied"):
        data[key] = NotificationService._format_money(facts[key]) if facts.get(key) else None
    # A balance of 0 is a fact, not an absence: both print.
    for key in ("amount", "balance"):
        if key in facts:
            data[key] = NotificationService._format_money(facts[key])
    order_id, subscription_id = facts.get("order_id"), facts.get("subscription_id")
    pay_allowed = (facts.get("actions") or {}).get("pay", {}).get("allowed")
    data["order_url"] = tracking_url(order_id) if order_id else None
    data["subscription_url"] = subscription_page_url(subscription_id) if subscription_id else None
    data["pay_url"] = order_page_url_for(order_id, subscription_id) if order_id and pay_allowed else None
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


def _email_date_in_words(value: Optional[str], language: str) -> Optional[str]:
    """A date the reader must not misread, as words with the year: "1 February 2027" / "1 февраля 2027" /
    "1-fevral 2027" (`display_labels.day_month`, the website's own wording). The price notices state a date the
    customer plans around, and the en email's numeric form (02/01/2027) reads as 2 January in a day-first country."""
    if not value:
        return None
    day = date.fromisoformat(value)
    return f"{display_labels.day_month(day, language)} {day.year}"


def _email_date(value: Optional[str], language: str) -> Optional[str]:
    return format_datetime(date.fromisoformat(value), "date", language) if value else None


# --------------------------------------------------------------------------- one channel


def _send(user: User, kind: str, facts: Dict[str, Any], *, notice_id: str) -> str:
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
        return _send_email(user, kind, facts, language)
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


def _send_email(user: User, kind: str, facts: Dict[str, Any], language: str) -> str:
    """One attempt through the shared Brevo path, never retried (a retry could mail the customer twice)."""
    results = NotificationService().send_notification(
        user.id,
        NotificationType(kind),
        channels=[NotificationChannel.EMAIL],
        template_data=email_template_data(kind, facts, language),
        language=language,
    )
    outcome = (results or {}).get(NotificationChannel.EMAIL.value) or {}
    if outcome.get("success"):
        return "email"
    logger.warning("Customer notice email (%s) to user %s failed: %s", kind, user.id, outcome)
    return "email_failed"
