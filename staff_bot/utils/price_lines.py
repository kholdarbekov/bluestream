"""Staff-bot lines about an announced price change. HTML-safe: every value is escaped or a formatted figure.

Sits beside `telegram_bot/price_change_lines.py` by design, not as a duplicate: the two bots run as separate
processes with separate i18n namespaces (`staff.*` vs `telegram.*`), escape modes (HTML vs plain) and money
formatters (`format_currency` vs `format_money`). Neither computes money: every figure is a field the server
publishes.
"""

from typing import Any, List, Optional

from staff_bot.i18n import i18n
from staff_bot.utils.formatters import escape_html, format_currency, format_local_date


def upcoming_price_line(upcoming: Any, language: str) -> Optional[str]:
    """"📅 From 01.02.2027: 20,000 UZS each" for a published `upcoming_price` block, else None."""
    if not isinstance(upcoming, dict):
        return None
    new_price = upcoming.get('new_price')
    date = format_local_date(upcoming.get('effective_date'))
    if not new_price or not date:
        return None
    return "📅 " + i18n.get(
        'staff.price_change.upcoming_line', language,
        date=date, price=format_currency(new_price, language=language),
    )


def courtesy_line(line: Any, language: str) -> Optional[str]:
    """The kept-price split of a priced line, else None. Built only from the line's published fields."""
    courtesy_quantity = (line or {}).get('courtesy_quantity')
    if not courtesy_quantity:
        return None
    courtesy_price = format_currency(line.get('courtesy_unit_price'), language=language)
    if not line.get('regular_quantity'):
        return i18n.get(
            'staff.price_change.courtesy_only', language,
            courtesy_qty=courtesy_quantity, courtesy_price=courtesy_price,
        )
    return i18n.get(
        'staff.price_change.courtesy_split', language,
        courtesy_qty=courtesy_quantity, courtesy_price=courtesy_price,
        regular_qty=line['regular_quantity'],
        regular_price=format_currency(line.get('unit_price'), language=language),
    )


def courtesy_status_lines(status: Any, language: str) -> List[str]:
    """The customer's courtesy standing under the cart: the dated heading with one line per capped product
    while it is `available`, the order it went to when `used`, nothing otherwise."""
    if not isinstance(status, dict):
        return []
    state = status.get('state')
    if state == 'available':
        lines = ["🏷 " + i18n.get(
            'staff.price_change.status_available', language,
            date=format_local_date(status.get('expires_on')),
        )]
        for cap in status.get('caps') or []:
            lines.append("   • " + i18n.get(
                'staff.price_change.status_cap_line', language,
                product=escape_html(cap.get('product_name')), cap=cap.get('cap'),
            ))
        return lines
    if state == 'used':
        return ["🏷 " + i18n.get(
            'staff.price_change.status_used', language,
            order_number=escape_html(status.get('used_order_number')),
        )]
    return []


def price_changed_quote(response: Any) -> Optional[dict]:
    """The new quote a refused order carries when its total moved since the screen was drawn, else None.

    The order endpoints answer 409 `STATE_CHANGED` with `details = {"reason": "price_changed", "quote": <the
    estimate>}`, which `StaffAPIClient` surfaces as `.error_code` / `.details`.
    """
    if getattr(response, 'status_code', None) != 409 or getattr(response, 'error_code', None) != 'STATE_CHANGED':
        return None
    details = getattr(response, 'details', None)
    if not isinstance(details, dict) or details.get('reason') != 'price_changed':
        return None
    quote = details.get('quote')
    return quote if isinstance(quote, dict) and quote.get('items') else None
