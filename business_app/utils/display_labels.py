"""Display labels for the website: the ONE helper per language (spec 2026-10-26 §5 B11).

Dates, delivery windows, money, "today"/"tomorrow"/"in N days" and payment-method names, in the
request's language, from DB-backed copy (`web.display_labels.*`, seeded en/uz/ru). The website reads
these as published labels and never formats a date or an amount itself (no `new Date('YYYY-MM-DD')`,
no en-US `toLocaleString`). B11 is website-only (spec §12): the bot and the emails keep their formats.

Every date label is in the nominative (Russian and Uzbek weekday names are lower case), so a copy
template that embeds one puts it after a colon or a dash, never after a preposition that needs a case.
Money groups thousands with a no-break space and keeps the unit on the same line: `36 000 so'm`,
`36 000 сум`.
"""

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from shared.constants import DISPLAY_TIMEZONE
from shared.i18n_rendering import plural_key

KEY = "web.display_labels"
NBSP = "\u00a0"
EN_DASH = "\u2013"


def _t(key: str, language: Optional[str], **values) -> str:
    return get_translation(f"{KEY}.{key}", language, **values)


def weekday_name(weekday: int, language: Optional[str] = None) -> str:
    """A weekday's name: "Thursday" / "четверг" / "payshanba"; 0 = Monday (Python's `weekday()`)."""
    return _t(f"weekday.{weekday}", language)


def date_long(day: date, language: Optional[str] = None) -> str:
    """The long date: "Thursday, 30 October" / "четверг, 30 октября" / "30-oktabr, payshanba"."""
    return _t(
        "date_long",
        language,
        weekday=weekday_name(day.weekday(), language),
        day=day.day,
        month=_t(f"month.{day.month}", language),
    )


def date_short(day: date, language: Optional[str] = None) -> str:
    """The short date: "Thu 30 Oct" / "чт, 30 окт." / "30-okt, Pa"."""
    return _t(
        "date_short",
        language,
        weekday=_t(f"weekday_short.{day.weekday()}", language),
        day=day.day,
        month=_t(f"month_short.{day.month}", language),
    )


def day_month(day: date, language: Optional[str] = None) -> str:
    """The day and month: "30 October" / "30 октября" / "30-oktabr"."""
    return _t("day_month", language, day=day.day, month=_t(f"month.{day.month}", language))


def window(start: Optional[time], end: Optional[time]) -> Optional[str]:
    """The delivery window, "10:00–13:00" (24h, en dash), or None when it has none: the time is then "Any time" or
    "Time to be confirmed" (`subscription_display.time_label`, the one wording)."""
    if start is None or end is None:
        return None
    return f"{start.strftime('%H:%M')}{EN_DASH}{end.strftime('%H:%M')}"


def money(amount, language: Optional[str] = None) -> str:
    """An amount: "36 000 so'm" / "36 000 сум", whole so'm, thousands grouped by a no-break space."""
    whole = Decimal(str(amount or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    grouped = f"{int(whole):,}".replace(",", NBSP)
    return _t("money", language, amount=grouped)


def days_away(day: date, today: date, language: Optional[str] = None) -> Optional[str]:
    """How far off a day is: "today" / "tomorrow" / "in 4 days" (each language's plural form); None when past."""
    count = (day - today).days
    if count < 0:
        return None
    if count == 0:
        return _t("today", language)
    if count == 1:
        return _t("tomorrow", language)
    language = language or get_current_language()
    return get_translation(plural_key(f"{KEY}.in_days", language, count), language, count=count)


def deadline(moment: datetime, today: date, language: Optional[str] = None) -> str:
    """A cut-off instant as the customer reads it, in local time: "13:00 today", "13:00 tomorrow",
    "13:00 on Thursday" within the week, "13:00 on 6 November" beyond it."""
    local = moment.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    clock = local.strftime("%H:%M")
    count = (local.date() - today).days
    if count == 0:
        return _t("deadline.today", language, time=clock)
    if count == 1:
        return _t("deadline.tomorrow", language, time=clock)
    if 1 < count < 7:
        return _t("deadline.weekday", language, time=clock, weekday=weekday_name(local.weekday(), language))
    return _t("deadline.date", language, time=clock, date=day_month(local.date(), language))


def payment_method(method, language: Optional[str] = None) -> str:
    """The method's name for customers: "Click", "Cash", "Bank transfer"."""
    value = getattr(method, "value", method)
    return _t(f"payment_method.{value}", language)


def order_detail(order, language: Optional[str] = None) -> Dict[str, Any]:
    """The shared order modal's labels (spec 2026-10-26 §12): the day it was placed, its amounts and each
    line's total. A discount, reward or tax line that is zero is None, so the modal draws no row for it."""
    from business_app.utils.local_windows import local_date

    def optional(amount) -> Optional[str]:
        return money(amount, language) if Decimal(str(amount or 0)) > 0 else None

    return {
        "placed_on": date_long(local_date(order.created_at), language) if order.created_at else None,
        "subtotal": money(order.subtotal, language),
        "delivery_fee": money(order.delivery_fee, language),
        "tier_discount": optional(order.tier_discount),
        "subscription_discount": optional(order.discount_amount),
        "reward_discount": optional(order.loyalty_discount),
        "tax": optional(getattr(order, "tax_amount", 0)),
        "total": money(order.total_amount, language),
        "items": {str(item.id): money(item.total_price, language) for item in order.order_items},
    }
