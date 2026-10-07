"""
Message formatters for Staff Bot
Format order details, delivery status, addresses, etc. for Telegram messages.
"""
import html
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, List, Optional
from datetime import datetime
from shared.staff_constants import SALES_PAY_PERIOD_STATUSES
from staff_bot.i18n import i18n

# Telegram's own ceiling for one message. Past it `sendMessage` and `editMessageText` raise a 400,
# and a push's retries raise identically, so every long screen and push here fits itself under
# this one number (the earnings summary and lists, the morning digest, the approval push).
TELEGRAM_TEXT_LIMIT = 4096

# One literal key per `FAILED_DELIVERY_REASONS` code, so no key is built at
# runtime. test_every_failure_reason_has_exactly_one_seeded_label keeps the two
# lists equal.
_REASON_KEYS = {
    'customer_unavailable': 'staff.delivery.reason.customer_unavailable',
    'wrong_address': 'staff.delivery.reason.wrong_address',
    'customer_refused': 'staff.delivery.reason.customer_refused',
    'product_damaged': 'staff.delivery.reason.product_damaged',
    'other': 'staff.delivery.reason.other',
}


def escape_html(value: Any) -> str:
    """Escape dynamic text inserted into Telegram HTML-formatted messages."""
    if value is None:
        return ''
    return html.escape(str(value), quote=False)


# Backward-compatible internal alias.
def _escape(value: Any) -> str:
    return escape_html(value)


def format_number(value) -> str:
    """A figure grouped in thousands with no currency ("1,001", "12,500,000"): tier bounds, unit
    counts, a tier's rate, net and amount. Whole, as money is printed; '0' for a missing figure,
    and anything unreadable is echoed, never raised. `format_currency` groups through it, so a
    count and a sum on one card cannot be grouped two ways."""
    if value is None:
        return '0'
    try:
        return f"{float(value):,.0f}"
    except (ValueError, TypeError):
        return str(value)


def format_currency(amount, currency: Optional[str] = None, language: str = 'en') -> str:
    """Format currency amount"""
    if currency is None:
        currency = i18n.get('staff.currency.uzs', language)
    return f"{format_number(amount)} {currency}"


def format_quantity(value: Any) -> str:
    """Format integer-like quantities without a trailing decimal part."""
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)

    text = format(quantity.normalize(), 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def get_cod_cash_projection(payload: Dict[str, Any]) -> Dict[str, float]:
    """Extract reserved COD prepayment and expected cash-to-collect values from payload."""
    reserved_prepayment = float(payload.get('cod_reserved_prepayment_amount') or 0)

    # Keep explicit zero values from API payloads. Using chained `or` would
    # treat 0 as falsy and incorrectly fall back to total/outstanding amounts.
    expected_cash_to_collect_raw = payload.get('expected_cash_to_collect')
    if expected_cash_to_collect_raw is None:
        expected_cash_to_collect_raw = payload.get('outstanding_amount')
    if expected_cash_to_collect_raw is None:
        expected_cash_to_collect_raw = payload.get('total_amount')
    expected_cash_to_collect = float(expected_cash_to_collect_raw or 0)
    if reserved_prepayment < 0:
        reserved_prepayment = 0.0
    if expected_cash_to_collect < 0:
        expected_cash_to_collect = 0.0
    return {
        'cod_reserved_prepayment_amount': reserved_prepayment,
        'expected_cash_to_collect': expected_cash_to_collect,
    }


def has_cash_due(payload: Dict[str, Any]) -> bool:
    """True when the driver must collect cash at this door — ANY payment rail.

    SSOT shared by the order-card formatters, the orders pool and the
    delivery-completion cash prompt (``status_update``).

    Reads the server-computed ``expected_cash_to_collect``, which
    ``StaffService.get_cod_collection_projection`` makes truthful for every rail
    (plan 2026-08-08-open-receivable-ssot).

    🔴 THIS REPLACED ``is_unsettled_electronic``, WHICH RE-DERIVED THE DECISION
    BOT-SIDE from a hardcoded status set ``{'completed', 'paid',
    'partially_paid'}``. Classifying ``partially_paid`` as settled is what hid
    the unpaid delta of an order edited upward at the door: prod order 961 had
    30 000 outstanding on a Click payment and this module printed
    "To collect now: 0 (no cash)" over it. The set had also drifted from the
    backend's own ``_OFFLINE_SETTLEABLE_STATUSES`` — ``'paid'`` existed only here.

    Do not reintroduce a bot-side status set. The backend owns this decision.
    """
    try:
        return float(payload.get('expected_cash_to_collect') or 0) > 0
    except (TypeError, ValueError):
        return False


def format_place_cod_lines(payload: Dict[str, Any], language: str) -> list:
    """Place-group COD block for a delivery payload (spec 8), or [].

    A "place" is a grouped delivery address — one physical workplace reached
    from several phone numbers. When this order ships to one, the driver must
    see the WHOLE place's open COD total, not just this customer's slice, so
    they know what is collectable at the door. SSOT shared by the order card
    and the at-door cash prompt (``status_update``).

    Returns an empty list for ungrouped addresses (``is_place_grouped`` false /
    absent) and for grouped places with nothing outstanding, so an ungrouped
    customer's card is byte-identical to today's.
    """
    if not payload.get('is_place_grouped'):
        return []
    try:
        place_total = float(payload.get('place_outstanding_cod_total') or 0)
    except (TypeError, ValueError):
        return []
    if place_total <= 0:
        return []

    label = payload.get('place_group_label') or ''
    line = (
        f"🏢 {i18n.get('staff.delivery.place_cod_total', language)}: "
        f"{format_currency(place_total, language=language)}"
        f" ({payload.get('place_active_cod_debt_count') or 0})"
    )
    if label:
        line += f" — {escape_html(label)}"
    return [line]


def format_money_block(
    order: Dict[str, Any],
    language: str,
    *,
    include_place_lines: bool = False,
) -> list:
    """Outstanding / reserved / to-collect lines for an order card, or [].

    SSOT for the order-card money block, shared by :func:`format_order_card` and
    the orders-pool renderer. The pool used to carry a THIRD hand-rolled copy of
    these lines, so widening the formatter silently left the pool on the old
    `payment == 'cash'` gate (plan 2026-08-08-open-receivable-ssot).

    Gated on `has_cash_due` — the server-computed figure — rather than on the
    payment rail. `or payment == 'cash'` is retained so a fully-collected COD
    order still shows its block with the "already collected" flag, which is
    existing behaviour drivers rely on.
    """
    payment = order.get('payment_method', '')
    if not (has_cash_due(order) or payment == 'cash'):
        return []

    cod_projection = get_cod_cash_projection(order)
    lines = [
        f"💸 {i18n.get('staff.delivery.cash_outstanding_label', language)}: "
        f"{format_currency(order.get('outstanding_amount'), language=language)}"
    ]
    if cod_projection['cod_reserved_prepayment_amount'] > 0:
        lines.append(
            f"💳 {i18n.get('staff.delivery.cod_prepaid_reserved', language)}: "
            f"{format_currency(cod_projection['cod_reserved_prepayment_amount'], language=language)}"
        )
    lines.append(
        f"💵 {i18n.get('staff.delivery.cash_to_collect_now', language)}: "
        f"{format_currency(cod_projection['expected_cash_to_collect'], language=language)}"
    )
    if include_place_lines:
        lines.extend(format_place_cod_lines(order, language))
    payment_status = str(order.get('payment_status') or '').lower()
    if payment_status == 'completed' or cod_projection['expected_cash_to_collect'] <= 0:
        lines.append(f"✅ {i18n.get('staff.delivery.cash_already_collected', language)}")
    elif payment_status == 'partially_paid':
        lines.append(f"ℹ️ {i18n.get('staff.delivery.cash_partially_collected', language)}")
    return lines


def format_delivery_window_line(order: Dict[str, Any], language: str) -> str:
    """The `🕐 ...` delivery-window line for an order card, or `''`.

    SSOT for turning the backend's `delivery_window` payload
    (``{start, end, kind, label}`` — see
    ``business_app/utils/delivery_window.py``) into a localized driver-facing
    line. Shared by :func:`format_order_card`,
    ``StaffWebhookServer._format_new_order_message``, and
    ``OrdersPoolHandler.view_order_details`` — all three used to render the
    legacy `time_slot` string raw and unlocalized, which is exactly the
    English-leak class this project keeps rediscovering (see
    `has_cash_due`'s docstring for the same failure mode in the money block).

    MUST branch on `kind`, never on the backend's English `label` — `label`
    is a log/fallback string, and shipping it here is how English leaks into
    a driver's Uzbek or Russian card. Returns `''` for an `anytime` window
    (no line at all) and for a payload without a `delivery_window`.
    """
    window = order.get('delivery_window') or {}
    window_kind = window.get('kind')
    if not window_kind or window_kind == 'anytime':
        return ''

    if window_kind == 'until':
        window_time = window.get('end')
    elif window_kind == 'between':
        window_time = f"{window.get('start')}-{window.get('end')}"
    else:
        window_time = window.get('start')

    return f"🕐 {i18n.get(f'staff.delivery.window.{window_kind}', language, time=window_time)}"


def format_order_card(order: Dict[str, Any], language: str) -> str:
    """
    Format order details as a compact card for Telegram message.
    Used in order pool, active deliveries, and history.
    """
    number = order.get('order_number') or i18n.get('staff.common.not_available', language)
    customer_name = _escape(order.get('customer_name', ''))
    customer_phone = _escape(order.get('customer_phone', ''))
    district = _escape(order.get('district', ''))
    address = _escape(order.get('address', ''))
    window_line = format_delivery_window_line(order, language)
    total = format_currency(order.get('total_amount'), language=language)
    payment = order.get('payment_method', '')
    item_count = order.get('item_count', 0)
    delivery_notes = _escape(order.get('delivery_notes', ''))

    lines = [
        f"📦 <b>#{number}</b>",
    ]

    if customer_name:
        lines.append(f"👤 {customer_name}")
    if customer_phone:
        lines.append(f"📞 {customer_phone}")
    if district:
        lines.append(f"📍 {district}")
    if address:
        lines.append(f"    {address}")
    if window_line:
        lines.append(window_line)

    payment_label = i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
    if payment_label:
        lines.append(f"💰 {total} ({payment_label})")
    else:
        lines.append(f"💰 {total}")
    lines.extend(format_money_block(order, language, include_place_lines=True))
    lines.append(f"📝 {item_count} {i18n.get('staff.items', language)}")

    if delivery_notes:
        lines.append(f"💬 {delivery_notes}")

    return '\n'.join(lines)


def format_delivery_item_labels(delivery: Dict[str, Any]) -> List[str]:
    """`["19 litrlik suv ×3", "Stakan ×1"]` — the ONE rule for how an order
    line reads on a driver's screen.

    Two surfaces render these: the compact order card (one label per line,
    :func:`format_active_delivery_summary`) and the route card's all-stops
    list (comma-joined onto a single line). Both needed to agree on which
    payload field holds the name (`product_name`, or `name` on the cached
    `current_delivery` snapshot), that a nameless line is dropped rather
    than shown as a bare quantity, and that the quantity goes through
    :func:`format_quantity` — `int()` would truncate a 1.5-unit line to 1.
    Written out twice those rules drift silently, so they live here.

    Labels come back HTML-escaped, ready to drop into a Telegram
    HTML-parse-mode message.
    """
    labels = []
    for item in delivery.get('items') or []:
        name = escape_html(item.get('product_name') or item.get('name') or '')
        if not name:
            continue
        labels.append(f"{name} ×{format_quantity(item.get('quantity', 1))}")
    return labels


def format_active_delivery_summary(
    delivery: Dict[str, Any],
    language: str,
    *,
    include_money: bool = True,
    position: Optional[int] = None,
) -> str:
    """Compact order card shared by the active-delivery list, detail view, and
    the status-change confirm/updated briefs.

    Field order: order# — status, customer name, phone, address (+instructions),
    items, then (when include_money) the money block; optional delivery notes
    trail on every surface. Missing fields are skipped, so the same function
    renders a full card or a partial `current_delivery` snapshot without error.

    Args:
        delivery: delivery/order dict (from get_active_deliveries or the cached
            current_delivery snapshot).
        language: UI language code.
        include_money: when False, omit the total/collected/to-collect block
            (the status-change brief).
        position: optional 0-based route position; when an int, prefixes the
            header with "{position+1}. " (list view only).
    """
    lines = []

    order_num = escape_html(
        delivery.get('order_number') or i18n.get('staff.common.not_available', language)
    )
    status_text = format_delivery_status(delivery.get('status', ''), language)
    position_prefix = f"{position + 1}. " if isinstance(position, int) else ""
    lines.append(f"🚚 <b>{position_prefix}#{order_num}</b> — {status_text}")

    customer_name = escape_html(delivery.get('customer_name', ''))
    if customer_name:
        lines.append(f"👤 {customer_name}")
    customer_phone = escape_html(delivery.get('customer_phone', ''))
    if customer_phone:
        lines.append(f"📞 {customer_phone}")

    district = escape_html(delivery.get('district', ''))
    if district:
        lines.append(f"📍 {district}")
    address = escape_html(delivery.get('address', ''))
    if address:
        lines.append(f"    {address}")
    # Structured door details from the customer bot. Most addresses carry
    # neither, so both collapse into ONE line that is omitted entirely when
    # empty — drivers read this card on a phone.
    door_parts = []
    apartment = escape_html(delivery.get('apartment_number', ''))
    if apartment:
        door_parts.append(f"{i18n.get('staff.delivery.apartment_label', language)} {apartment}")
    floor = escape_html(delivery.get('floor_number', ''))
    if floor:
        door_parts.append(f"{i18n.get('staff.delivery.floor_label', language)} {floor}")
    if door_parts:
        lines.append(f"    🏢 {', '.join(door_parts)}")

    instructions = escape_html(delivery.get('delivery_instructions', ''))
    if instructions:
        lines.append(f"    📝 {instructions}")

    for label in format_delivery_item_labels(delivery):
        lines.append(f"📦 {label}")

    if include_money:
        total = format_currency(delivery.get('total_amount'), language=language)
        payment = delivery.get('payment_method', '')
        payment_label = (
            i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
        )
        total_line = f"💰 {i18n.get('staff.delivery.total_label', language)}: {total}"
        if payment_label:
            total_line += f" ({payment_label})"
        lines.append(total_line)

        # One money block for every rail (plan 2026-08-08-open-receivable-ssot).
        # There used to be three arms — cash / unsettled-electronic / nothing —
        # and a part-paid card order fell into the third and was told
        # "To collect now: 0 (no cash)" over a real debt. `payment == 'cash'` is
        # retained so a fully-collected COD order still shows its collected and
        # to-collect lines, which drivers rely on.
        if has_cash_due(delivery) or payment == 'cash':
            cod = get_cod_cash_projection(delivery)
            # The collected line is what EXPLAINS a part-paid balance ("90,000
            # total, 60,000 already paid, 30,000 due"), so it must appear
            # whenever money has actually landed. For an order with nothing
            # collected it is pure noise, and omitting it keeps the
            # unsettled-electronic card byte-identical to before this change.
            try:
                already_collected = float(delivery.get('amount_collected') or 0)
            except (TypeError, ValueError):
                already_collected = 0.0
            if already_collected > 0 or payment == 'cash':
                lines.append(
                    f"🧾 {i18n.get('staff.delivery.cash_collected_label', language)}: "
                    f"{format_currency(delivery.get('amount_collected'), language=language)}"
                )
            if cod['cod_reserved_prepayment_amount'] > 0:
                lines.append(
                    f"💳 {i18n.get('staff.delivery.cod_prepaid_reserved', language)}: "
                    f"{format_currency(cod['cod_reserved_prepayment_amount'], language=language)}"
                )
            lines.append(
                f"💵 {i18n.get('staff.delivery.cash_to_collect_now', language)}: "
                f"{format_currency(cod['expected_cash_to_collect'], language=language)}"
            )
        else:
            lines.append(
                f"💵 {i18n.get('staff.delivery.cash_to_collect_now', language)}: "
                f"{format_currency(0, language=language)} "
                f"({i18n.get('staff.delivery.no_cash_note', language)})"
            )

    notes = escape_html(delivery.get('delivery_notes', ''))
    if notes:
        lines.append(f"💬 {notes}")

    return '\n'.join(lines)


def format_delivery_status(status: str, language: str) -> str:
    """Format delivery status with emoji"""
    status_map = {
        'assigned': ('📋', 'staff.delivery.status.assigned'),
        'picked_up': ('📦', 'staff.delivery.status.picked_up'),
        'in_transit': ('🚚', 'staff.delivery.status.in_transit'),
        'arrived': ('📍', 'staff.delivery.status.arrived'),
        'delivered': ('✅', 'staff.delivery.status.delivered'),
        'failed': ('❌', 'staff.delivery.status.failed'),
    }

    emoji, key = status_map.get(status, ('❓', f'staff.delivery.status.{status}'))
    return f"{emoji} {i18n.get(key, language)}"


def format_fail_reason(reason: str, language: str) -> str:
    """A failure reason in the reader's language (F9).

    One wording for the operator's failed-deliveries list and the failure
    alert, so both name a reason the same way. The backend accepts only
    the codes `_REASON_KEYS` covers. A row written before that check is
    shown as it was stored.
    """
    key = _REASON_KEYS.get(reason)
    if key is not None:
        return i18n.get(key, language)
    return escape_html(reason)


def format_delivery_stats(stats: Dict[str, Any], language: str) -> str:
    """Format delivery performance stats"""
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    total = int(_to_float(stats.get('total_deliveries', 0), 0))
    completed = int(_to_float(stats.get('completed_deliveries', stats.get('delivered', 0)), 0))
    failed = int(_to_float(stats.get('failed_deliveries', stats.get('failed', 0)), 0))
    avg_time_val = stats.get('avg_delivery_time_minutes')
    avg_time = _to_float(avg_time_val, 0.0)
    rating = _to_float(stats.get('avg_rating', 0), 0.0)
    cash_collected = format_currency(stats.get('total_cash_collected', 0), language=language)

    lines = [
        f"📊 <b>{i18n.get('staff.stats.title', language)}</b>",
        "",
        f"📦 {i18n.get('staff.stats.total', language)}: {total}",
        f"✅ {i18n.get('staff.stats.completed', language)}: {completed}",
        f"❌ {i18n.get('staff.stats.failed', language)}: {failed}",
        f"⏱ {i18n.get('staff.stats.avg_time', language)}: {avg_time:.0f} {i18n.get('staff.unit.minutes', language)}",
    ]

    if rating > 0:
        lines.append(f"⭐ {i18n.get('staff.stats.rating', language)}: {rating:.1f}/5")

    lines.append(f"💵 {i18n.get('staff.stats.cash', language)}: {cash_collected}")

    return '\n'.join(lines)


def format_local_time(dt: Optional[datetime] = None, with_seconds: bool = False) -> str:
    """HH:MM (or HH:MM:SS) in the business display timezone (Asia/Tashkent).

    The route card stamps this so freshness is visible without being
    announced (route-UX spec §6.3).

    `with_seconds` exists for DRIVER-TAP renders only (tap-feedback spec
    §4.2). A minute-granular stamp is byte-identical for a full minute, so
    every repeat tap hashed to the same render signature and
    `render_route_card` returned early having made no Telegram call at all
    -- the bot looked frozen. Seconds make a tap's edit genuinely different
    content. The default stays False so every existing caller is
    byte-identical, and in particular so the WEBHOOK path keeps its
    signature idempotence: duplicate silent pushes must remain free.
    """
    from zoneinfo import ZoneInfo

    from shared.constants import DISPLAY_TIMEZONE

    from datetime import timezone as _tz
    moment = dt or datetime.now(_tz.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_tz.utc)
    fmt = "%H:%M:%S" if with_seconds else "%H:%M"
    return moment.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).strftime(fmt)


def format_local_date(value: Any, fmt: str = '%d.%m.%Y') -> str:
    """A backend date/datetime rendered in the business display timezone, or ''.

    The sibling of :func:`format_local_time`, and for the same reason: the
    backend answers in UTC (`_iso(...)` in
    ``business_app/serializers/sales_serializers.py``) and the reader is
    standing in Tashkent. A visit closed at 00:30 local is stamped 19:30Z the
    day before, so a date sliced off the ISO string — or formatted without
    converting — is wrong for every evening of every day.

    Accepts the three shapes the sales payloads actually carry: a tz-aware
    datetime string, a bare ``YYYY-MM-DD`` (a ``Date`` column such as
    ``agent_next_visit_at``), and a real ``date``/``datetime`` object. A naive
    datetime is read as UTC, matching every other timestamp this bot receives.

    Returns ``''`` for None and for anything unparseable, so the caller gates
    its whole line on the result instead of guarding the parse itself.
    """
    from datetime import date as _date, timezone as _tz
    from zoneinfo import ZoneInfo

    from shared.constants import DISPLAY_TIMEZONE

    if value is None or value == '':
        return ''

    moment = value
    if isinstance(moment, str):
        try:
            # A bare `YYYY-MM-DD` is parsed as a DATE first. Read as a datetime it
            # becomes midnight UTC and is then converted, which moves it to the day
            # before under any display timezone behind UTC -- correct today only
            # because Tashkent is ahead of it.
            moment = (
                _date.fromisoformat(moment)
                if 'T' not in moment and ' ' not in moment
                else datetime.fromisoformat(moment)
            )
        except ValueError:
            return ''
    # `datetime` subclasses `date`, so it must be tested first or every
    # timestamp would take the naive branch and skip the conversion.
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_tz.utc)
        return moment.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).strftime(fmt)
    if isinstance(moment, _date):
        return moment.strftime(fmt)
    return ''


# ---- Sales-agent pay (compensation spec §7.2, §7.5) ---------------------------------------
#
# One home for how pay money reads, shared by the My earnings screens and the two pay pushes,
# so a figure cannot look one way on the summary and another in the push that links to it.
# Every figure is the backend's: nothing below adds one or compares it against a rule. Pay
# amounts arrive whole; a tier's net is order money and prints to the whole UZS, as every sum here.

# U+2212, the true minus. A hyphen-minus in front of a grouped figure reads as a dash.
MINUS_SIGN = '−'


def signed_currency(value, language: str) -> str:
    """`+` or `−` (U+2212), then `format_currency` of the absolute amount.

    For money whose sign is the news: a correction from an earlier month, a bonus, an
    adjustment, a carried shortfall. Zero reads `+0`.
    """
    amount = float(value or 0)
    sign = MINUS_SIGN if amount < 0 else '+'
    return f"{sign}{format_currency(abs(amount), language=language)}"


def signed_deduction(amount, language: str) -> str:
    """A deduction the backend publishes as a positive amount (a penalty), with its minus."""
    return signed_currency(-float(amount or 0), language)


def format_pay_amount(value, language: str) -> str:
    """A figure that is normally positive (base, commission, variable, total): plain, or
    through `signed_currency` once it is below zero. Nothing below the paid total is floored
    (C1), so a negative variable pay is printed with its minus, never hidden."""
    if value is not None and float(value) < 0:
        return signed_currency(value, language)
    return format_currency(value, language=language)


def format_pay_month(month: Optional[str]) -> str:
    """A pay month as the wire carries it ("2026-10") as the agent reads it ("10.2026").

    Through `format_local_date`, the date SSOT: the first of the month is a bare date, so no
    timezone can move it. '' for a missing month, so the caller gates its line on the result.
    """
    if not month:
        return ''
    return format_local_date(f"{month}-01", '%m.%Y')


def format_pay_status(status, language: str) -> str:
    """A pay month's published status in the agent's words ("Under review", "Paid"); '' for a
    value the family lacks. One reading for the My earnings screens and their buttons."""
    if status not in SALES_PAY_PERIOD_STATUSES:
        return ''
    return i18n.get(f'staff.sales.earnings.status.{status}', language)


def format_pay_carry_in(carry_in: Optional[Dict[str, Any]], language: str) -> Optional[str]:
    """The carried-in row, labelled by its published `source`, or None when nothing came in.

    `owed` is a balance the agent owed after leaving, netted here (I-28); anything else is
    last month's shortfall carried forward (C1). Which one it is was decided by the backend's
    `carry_in_view` and is never re-derived from amounts here.
    """
    carry_in = carry_in or {}
    amount = carry_in.get('amount')
    if amount is None or float(amount) >= 0:
        return None
    month = format_pay_month(carry_in.get('from_month'))
    if carry_in.get('source') == 'owed':
        label = i18n.get('staff.sales.earnings.owed_in', language, month=month)
    else:
        label = i18n.get('staff.sales.earnings.carry_in', language, month=month)
    return f"{label}: {signed_currency(amount, language)}"


def format_pay_balance_note(carry_out, owed, language: str) -> Optional[str]:
    """The sentence under a total that a paid 0 would otherwise hide, or None.

    A negative `carry_out` is a shortfall approval moves into next month; a positive `owed`
    is one the agent owes because employment does not continue (C1, I-26). The backend never
    publishes both; `{amount}` is the absolute value, as the copy reads.
    """
    if carry_out is not None and float(carry_out) < 0:
        return i18n.get(
            'staff.sales.earnings.carry_note', language,
            amount=format_currency(abs(float(carry_out)), language=language),
        )
    if owed is not None and float(owed) > 0:
        return i18n.get(
            'staff.sales.earnings.owed_note', language,
            amount=format_currency(owed, language=language),
        )
    return None


def format_pay_product_name(names: Optional[Dict[str, Any]], language: str) -> str:
    """A product's published name (`product_name {en, uz, ru}`) in the agent's language, else
    English, escaped for the HTML message (§7.2). The one reading of it for the tier rows, the
    late rows and the credited-order units."""
    names = names or {}
    return escape_html(names.get(language) or names.get('en'))


def _tier_range(tier: Dict[str, Any]) -> str:
    """"501–1,000", or "1,001+" for the open-ended top tier: built from the published bounds,
    the one string §5.1 leaves to the clients. The `–` and the `+` are glyphs (§8.5)."""
    start = format_number(tier.get('from_unit'))
    end = tier.get('to_unit')
    return f"{start}+" if end is None else f"{start}–{format_number(end)}"


def _tier_rate(mode: Optional[str], value, language: str) -> str:
    """A next tier's rate in words: "2,000 UZS per unit", or "2.5%" (the published value)."""
    if mode == 'percent':
        return f"{format_quantity(value)}%"
    return i18n.get('staff.sales.earnings.per_unit', language, amount=format_currency(value, language=language))


def format_pay_tiers(product: Dict[str, Any], language: str, *, with_next: bool) -> List[str]:
    """One product's commission as the summary and the approval push print it (§7.2, §7.5):

        "   19L: 700 units · 800,000 UZS"
        "      501–1,000: 200 × 1,500 = 300,000"       (a per-unit tier)
        "      1+: 2% of 12,500,000 = 250,000"          (a percent tier)
        "      🎯 next tier from unit 1,001 (2,000 UZS per unit): 301 more units"

    One row per published tier, all of which hold units. A tier whose `amount` differs from its
    `amount_full` holds an order paid on less money (D-Q3), so the row keeps "units × rate =
    amount_full" true and adds what it counted (V5-B10). The 🎯 row is drawn only `with_next`
    and only when the backend published a next tier (an open estimate's, §5.2). Every figure is
    a field; the range string is the one thing built here. The first row is always the product
    line, which is all a card short of room keeps (§7.2 "Length").
    """
    name = format_pay_product_name(product.get('product_name'), language)
    units = i18n.get('staff.sales.earnings.units', language, count=format_number(product.get('units')))
    lines = [f"   {name}: {units} · {format_currency(product.get('total'), language=language)}"]
    for tier in product.get('tiers') or []:
        if tier.get('mode') == 'percent':
            rate = i18n.get('staff.sales.earnings.tier_percent', language,
                            value=format_quantity(tier.get('value')), net=format_number(tier.get('net')))
        else:
            rate = f"{format_number(tier.get('units'))} × {format_number(tier.get('value'))}"
        line = f"      {_tier_range(tier)}: {rate} = {format_number(tier.get('amount_full'))}"
        if tier.get('amount') != tier.get('amount_full'):
            counted = i18n.get('staff.sales.earnings.tier_scaled', language, amount=format_number(tier.get('amount')))
            line = f"{line} · {counted}"
        lines.append(line)
    next_tier = product.get('next_tier') if with_next else None
    if next_tier:
        hint = i18n.get('staff.sales.earnings.next_tier', language,
                        from_unit=format_number(next_tier.get('from_unit')),
                        rate=_tier_rate(next_tier.get('mode'), next_tier.get('value'), language),
                        count=format_number(next_tier.get('units_to_go')))
        lines.append(f"      🎯 {hint}")
    return lines


def format_user_card(user: Dict[str, Any], language: str) -> str:
    """Format user details card (for operator)"""
    name = _escape(f"{user.get('first_name', '')} {user.get('last_name', '')}".strip())
    if not name:
        name = i18n.get('staff.common.not_available', language)
    phone = _escape(user.get('phone', ''))
    address_count = user.get('address_count', 0)
    order_count = user.get('order_count', 0)

    lines = [
        f"👤 <b>{name}</b>",
        f"📞 {phone}",
        f"📍 {address_count} {i18n.get('staff.addresses', language)}",
        f"📦 {order_count} {i18n.get('staff.orders', language)}",
    ]

    return '\n'.join(lines)
