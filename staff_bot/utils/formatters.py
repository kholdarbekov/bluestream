"""
Message formatters for Staff Bot
Format order details, delivery status, addresses, etc. for Telegram messages.
"""
import html
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional
from datetime import datetime
from i18n import i18n


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
        f"\U0001f4e6 <b>#{number}</b>",
    ]

    if customer_name:
        lines.append(f"\U0001f464 {customer_name}")
    if customer_phone:
        lines.append(f"\U0001f4de {customer_phone}")
    if district:
        lines.append(f"\U0001f4cd {district}")
    if address:
        lines.append(f"    {address}")
    if time_slot:
        lines.append(f"\U0001f550 {time_slot}")

    payment_label = i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
    if payment_label:
        lines.append(f"\U0001f4b0 {total} ({payment_label})")
    else:
        lines.append(f"\U0001f4b0 {total}")
    lines.append(f"\U0001f4dd {item_count} {i18n.get('staff.items', language)}")

    if delivery_notes:
        lines.append(f"\U0001f4ac {delivery_notes}")

    return '\n'.join(lines)


def format_delivery_status(status: str, language: str) -> str:
    """Format delivery status with emoji"""
    status_map = {
        'assigned': ('\U0001f4cb', 'staff.delivery.status.assigned'),
        'picked_up': ('\U0001f4e6', 'staff.delivery.status.picked_up'),
        'in_transit': ('\U0001f69a', 'staff.delivery.status.in_transit'),
        'arrived': ('\U0001f4cd', 'staff.delivery.status.arrived'),
        'delivered': ('\u2705', 'staff.delivery.status.delivered'),
        'failed': ('\u274c', 'staff.delivery.status.failed'),
    }

    emoji, key = status_map.get(status, ('\u2753', f'staff.delivery.status.{status}'))
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
        f"\U0001f4ca <b>{i18n.get('staff.stats.title', language)}</b>",
        "",
        f"\U0001f4e6 {i18n.get('staff.stats.total', language)}: {total}",
        f"\u2705 {i18n.get('staff.stats.completed', language)}: {completed}",
        f"\u274c {i18n.get('staff.stats.failed', language)}: {failed}",
        f"\u23f1 {i18n.get('staff.stats.avg_time', language)}: {avg_time:.0f} {i18n.get('staff.unit.minutes', language)}",
    ]

    if rating > 0:
        lines.append(f"\u2b50 {i18n.get('staff.stats.rating', language)}: {rating:.1f}/5")

    lines.append(f"\U0001f4b5 {i18n.get('staff.stats.cash', language)}: {cash_collected}")

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
        f"\U0001f464 <b>{name}</b>",
        f"\U0001f4de {phone}",
        f"\U0001f4cd {address_count} {i18n.get('staff.addresses', language)}",
        f"\U0001f4e6 {order_count} {i18n.get('staff.orders', language)}",
    ]

    return '\n'.join(lines)
