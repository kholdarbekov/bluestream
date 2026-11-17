"""Customer-bot lines about an announced price change. Plain text: callers escape for their parse mode.

The bot prints the two fields the server publishes and decides nothing.
"""

from typing import Any, Optional

from i18n import i18n
from utils import format_date, format_money


def upcoming_price_line(upcoming: Any, language: str) -> Optional[str]:
    """"📅 From 01.02.2027: 20,000 so'm each" for a published `upcoming_price` block, else None."""
    if not isinstance(upcoming, dict):
        return None
    new_price = upcoming.get('new_price')
    date = format_date(upcoming.get('effective_date'))
    if not new_price or not date:
        return None
    return "📅 " + i18n.get(
        'telegram.price_change.upcoming_line', language,
        date=date, price=format_money(new_price, language),
    )


def courtesy_line(line: Any, language: str) -> Optional[str]:
    """The kept-price split of a priced line ("2 × 18,000 so'm (your price, kept for you) + 2 × 20,000 so'm"), else None.

    Every figure is a field the server publishes on the line; nothing here subtracts or multiplies.
    """
    courtesy_quantity = (line or {}).get('courtesy_quantity')
    if not courtesy_quantity:
        return None
    courtesy_price = format_money(line.get('courtesy_unit_price'), language)
    if not line.get('regular_quantity'):
        return i18n.get(
            'telegram.price_change.courtesy_only', language,
            courtesy_qty=courtesy_quantity, courtesy_price=courtesy_price,
        )
    return i18n.get(
        'telegram.price_change.courtesy_split', language,
        courtesy_qty=courtesy_quantity, courtesy_price=courtesy_price,
        regular_qty=line['regular_quantity'], regular_price=format_money(line.get('unit_price'), language),
    )


def courtesy_thanks(price_courtesy: Any, language: str) -> Optional[str]:
    """The one thank-you sentence when the quote's `price_courtesy` block says the kept price was applied."""
    if not isinstance(price_courtesy, dict) or not price_courtesy.get('applied'):
        return None
    return i18n.get('telegram.price_change.courtesy_thanks', language)


def price_line_sub_lines(line: Any, language: str) -> list:
    """The indented sub-lines under a line, in order: the kept-price split, then the dated coming price."""
    subs = (courtesy_line(line, language), upcoming_price_line((line or {}).get('upcoming_price'), language))
    return [f"   {sub}" for sub in subs if sub]


def price_changed_quote(response: Any) -> Optional[dict]:
    """The new quote a refused order carries when its total moved since the screen was drawn, else None.

    `POST /orders` answers 409 with `data.reason_code == "STATE_CHANGED"`, `data.reason == "price_changed"` and
    `data.quote` (the estimate's `data`). `_make_request` hands the whole error body back as `response.data`, so
    the fields sit one level down, the depth `BaseHandler._response_detail` reads.
    """
    if getattr(response, 'status_code', None) != 409:
        return None
    body = getattr(response, 'data', None)
    detail = body.get('data') if isinstance(body, dict) else None
    if not isinstance(detail, dict):
        return None
    if detail.get('reason_code') != 'STATE_CHANGED' or detail.get('reason') != 'price_changed':
        return None
    quote = detail.get('quote')
    return quote if isinstance(quote, dict) and quote.get('pricing') else None
