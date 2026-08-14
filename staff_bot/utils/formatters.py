"""
Message formatters for Staff Bot
Format order details, delivery status, addresses, etc. for Telegram messages.
"""
import html
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional
from datetime import datetime
from staff_bot.i18n import i18n


def escape_html(value: Any) -> str:
    """Escape dynamic text inserted into Telegram HTML-formatted messages."""
    if value is None:
        return ''
    return html.escape(str(value), quote=False)


# Backward-compatible internal alias.
def _escape(value: Any) -> str:
    return escape_html(value)


def format_currency(amount, currency: Optional[str] = None, language: str = 'en') -> str:
    """Format currency amount"""
    if currency is None:
        currency = i18n.get('staff.currency.uzs', language)
    if amount is None:
        return f"0 {currency}"
    try:
        return f"{float(amount):,.0f} {currency}"
    except (ValueError, TypeError):
        return f"{amount} {currency}"


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
    time_slot = _escape(order.get('time_slot', ''))
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
    if time_slot:
        lines.append(f"🕐 {time_slot}")

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

    for item in delivery.get('items') or []:
        name = escape_html(item.get('product_name') or item.get('name') or '')
        if not name:
            continue
        qty = format_quantity(item.get('quantity', 1))
        lines.append(f"📦 {name} ×{qty}")

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
