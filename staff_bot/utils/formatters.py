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


# Online payment methods that settle at the gateway. When such a payment has
# NOT settled, the driver collects the full amount in cash at the door — so it
# behaves like a cash order for "cash to collect" purposes. SSOT shared by the
# order-card formatter and the delivery-completion cash prompt (status_update).
_ELECTRONIC_METHODS = {'click', 'payme', 'card'}
_SETTLED_PAYMENT_STATUSES = {'completed', 'paid', 'partially_paid'}


def is_unsettled_electronic(payload: Dict[str, Any]) -> bool:
    """True when an order's online payment (click/payme/card) has not settled,
    so the full amount is due in cash at the door."""
    method = payload.get('payment_method', '')
    status = str(payload.get('payment_status') or '').lower()
    return method in _ELECTRONIC_METHODS and status not in _SETTLED_PAYMENT_STATUSES


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
    if payment == 'cash':
        cod_projection = get_cod_cash_projection(order)
        lines.append(
            f"💸 {i18n.get('staff.delivery.cash_outstanding_label', language)}: "
            f"{format_currency(order.get('outstanding_amount'), language=language)}"
        )
        if cod_projection['cod_reserved_prepayment_amount'] > 0:
            lines.append(
                f"💳 {i18n.get('staff.delivery.cod_prepaid_reserved', language)}: "
                f"{format_currency(cod_projection['cod_reserved_prepayment_amount'], language=language)}"
            )
        lines.append(
            f"💵 {i18n.get('staff.delivery.cash_to_collect_now', language)}: "
            f"{format_currency(cod_projection['expected_cash_to_collect'], language=language)}"
        )
        payment_status = str(order.get('payment_status') or '').lower()
        if payment_status == 'completed' or cod_projection['expected_cash_to_collect'] <= 0:
            lines.append(f"✅ {i18n.get('staff.delivery.cash_already_collected', language)}")
        elif payment_status == 'partially_paid':
            lines.append(f"ℹ️ {i18n.get('staff.delivery.cash_partially_collected', language)}")
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

        if payment == 'cash':
            cod = get_cod_cash_projection(delivery)
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
        elif is_unsettled_electronic(delivery):
            # Online payment not settled → full amount due in cash at the door
            # (mirrors the delivery-completion prompt in status_update.py).
            lines.append(
                f"💵 {i18n.get('staff.delivery.cash_to_collect_now', language)}: "
                f"{format_currency(delivery.get('total_amount'), language=language)}"
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
