"""
Customer-facing bottle balance handler.
Shows the customer their returnable bottle balance and ledger history per address.
"""

import html
from datetime import datetime
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from keyboards import KeyboardBuilder, MenuKeyboards
from utils import user_middleware, get_auth_token


def _to_decimal(value) -> Decimal:
    """Coerce a float/str/int/Decimal bottle quantity to Decimal, defaulting to 0."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _normalize_qty(value) -> str:
    """Render a bottle quantity with trailing zeros stripped ("4", "1.5", "0").

    Decimal-safe; never int()-truncates fractional bottle counts. ``format(_, 'f')``
    keeps integers out of exponent form (e.g. Decimal('1E+1') -> '10')."""
    return format(_to_decimal(value).normalize(), 'f')


def _format_ledger_date(value) -> str:
    """Format an ISO occurred_at timestamp as 'dd.mm.yyyy'; '' when absent/unparseable."""
    if not value:
        return ''
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return ''
    return dt.strftime('%d.%m.%Y')


# Event types that represent one physical order visit and are collapsed into a
# single ``#order (date): Collected: n, Delivered: m`` line when order_id is set.
_ORDER_EVENT_TYPES = ('delivery', 'return_on_delivery')
# Non-order rows whose quantity moves bottles up or down: render a signed glyph.
_SIGNED_EVENT_TYPES = ('admin_adjustment', 'fine_paid')
# Fine bookkeeping rows carry 0 bottles — no quantity fragment at all.
_NO_QTY_EVENT_TYPES = ('fine_issued', 'fine_reversed')


def _render_non_order_line(row: dict, language: str) -> str:
    """One ledger line for a non-order (or order_id-less) row: ``{label} ({date})``
    optionally followed by ``: {qty}``.

    - ``admin_adjustment`` / ``fine_paid`` carry a SIGNED quantity (＋/−); a 0
      quantity drops the fragment.
    - ``fine_issued`` / ``fine_reversed`` move money not bottles — label + date only.
    - everything else (standalone_collection, initial_balance, and defensive
      delivery/return rows without an order_id) shows an UNSIGNED quantity."""
    event_type = row.get('event_type') or ''
    label = i18n.get(f'telegram.bottles.event.{event_type}', language)
    date_str = _format_ledger_date(row.get('occurred_at'))
    prefix = f"{label} ({date_str})"

    if event_type in _NO_QTY_EVENT_TYPES:
        return prefix

    qty = _to_decimal(row.get('quantity', 0))
    if event_type in _SIGNED_EVENT_TYPES:
        if qty == 0:
            return prefix
        sign = '＋' if qty > 0 else '−'
        return f"{prefix}: {sign}{_normalize_qty(abs(qty))}"

    return f"{prefix}: {_normalize_qty(abs(qty))}"


def _render_order_group_line(group: dict, language: str) -> str:
    """One collapsed line for an order's delivery/return activity on this page:
    ``#{order} ({date}): {Collected}: n, {Delivered}: m``.

    Collected (return) fragment comes first, Delivered second; a fragment is
    omitted when that side of the pair is absent on the page. The date is taken
    from the delivery row when present, otherwise the return row. Quantities are
    UNSIGNED (abs)."""
    return_label = i18n.get('telegram.bottles.event.return_on_delivery', language)
    delivery_label = i18n.get('telegram.bottles.event.delivery', language)

    # order_number is user-influenced free text rendered into an HTML
    # (parse_mode='HTML') message — escape so a value like "<x>" doesn't make
    # Telegram reject the whole message. Fall back to the numeric order_id.
    order_ref = group['order_number'] if group['order_number'] is not None else group['order_id']
    date_src = group['delivery_date'] if group['has_delivery'] else group['return_date']
    date_str = _format_ledger_date(date_src)

    fragments = []
    if group['has_return']:
        fragments.append(f"{return_label}: {_normalize_qty(abs(group['return_qty']))}")
    if group['has_delivery']:
        fragments.append(f"{delivery_label}: {_normalize_qty(abs(group['delivery_qty']))}")

    return f"#{html.escape(str(order_ref))} ({date_str}): {', '.join(fragments)}"


def _render_ledger_lines(items: list, language: str) -> list:
    """Render a fetched ledger page into display lines.

    delivery + return_on_delivery rows that share an order_id collapse into a
    single per-order line, positioned at the group's first occurrence in the
    (occurred_at desc) item order. Every other row renders one line each."""
    groups = {}
    # Sequence of ('line', str) or ('group', order_id) preserving item order,
    # with each order appearing once at its first occurrence.
    sequence = []

    for row in items:
        event_type = row.get('event_type') or ''
        order_id = row.get('order_id')
        if event_type in _ORDER_EVENT_TYPES and order_id is not None:
            group = groups.get(order_id)
            if group is None:
                group = {
                    'order_id': order_id,
                    'order_number': None,
                    'has_delivery': False,
                    'has_return': False,
                    'delivery_qty': Decimal(0),
                    'return_qty': Decimal(0),
                    'delivery_date': None,
                    'return_date': None,
                }
                groups[order_id] = group
                sequence.append(('group', order_id))

            qty = _to_decimal(row.get('quantity', 0))
            if event_type == 'delivery':
                group['has_delivery'] = True
                group['delivery_qty'] += qty
                if group['delivery_date'] is None:
                    group['delivery_date'] = row.get('occurred_at')
            else:
                group['has_return'] = True
                group['return_qty'] += qty
                if group['return_date'] is None:
                    group['return_date'] = row.get('occurred_at')
            if group['order_number'] is None and row.get('order_number') is not None:
                group['order_number'] = row.get('order_number')
        else:
            sequence.append(('line', _render_non_order_line(row, language)))

    lines = []
    for kind, value in sequence:
        if kind == 'line':
            lines.append(value)
        else:
            lines.append(_render_order_group_line(groups[value], language))
    return lines


def _history_keyboard(address_id: int, page: int, per_page: int, total: int, language: str):
    """Prev/next pagination (page-relative, next hidden when the page is the
    last one) + Back to the balances screen."""
    nav_row = []
    if page > 1:
        nav_row.append({
            'text': i18n.get('telegram.pagination.previous', language),
            'callback_data': f'bottle_history_{address_id}_{page - 1}',
        })
    if page * per_page < total:
        nav_row.append({
            'text': i18n.get('telegram.pagination.next', language),
            'callback_data': f'bottle_history_{address_id}_{page + 1}',
        })

    buttons = []
    if nav_row:
        buttons.append(nav_row)
    buttons.append([
        {'text': i18n.get('telegram.back', language), 'callback_data': 'my_bottles'}
    ])
    return KeyboardBuilder.build_inline_keyboard(buttons)


class BottleBalanceHandler(BaseHandler):
    """Show customer their bottle balances and per-address ledger history."""

    async def show_bottle_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display bottle balances across ALL addresses (including 0/negative),
        each with its own History button. Empty state applies only when the
        customer has no bottle activity at all."""
        query = update.callback_query
        if query:
            await query.answer()

        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_my_bottle_balances(user_token)

            if not response.success:
                await self._handle_api_error(
                    update,
                    i18n.get('telegram.bottles.load_error', language),
                    language,
                )
                return

            payload = response.data
            if isinstance(payload, dict):
                balances = payload.get('data', []) or []
            elif isinstance(payload, list):
                balances = payload
            else:
                balances = []

            title = i18n.get('telegram.bottles.title', language)
            if not balances:
                text = (
                    f"📦 <b>{title}</b>\n\n"
                    f"{i18n.get('telegram.bottles.no_balance', language)}"
                )
                keyboard = MenuKeyboards.back_button(language)
            else:
                lines = [f"📦 <b>{title}</b>\n"]
                buttons = []
                history_label = i18n.get('telegram.bottles.history_button', language)
                for b in balances:
                    address_id = b.get('address_id')
                    addr_title = (
                        b.get('address_title')
                        or b.get('address_label')
                        or f"Address #{address_id}"
                    )
                    balance = _normalize_qty(b.get('balance', 0))
                    # The message body is sent with parse_mode='HTML', so the
                    # user-controlled title must be escaped there (a title like
                    # "Home <3" would otherwise make Telegram reject the whole
                    # message). Button text is NOT HTML-parsed, so it keeps the
                    # raw title.
                    lines.append(f"• {html.escape(addr_title)}: <b>{balance}</b>")
                    buttons.append([{
                        'text': f"{history_label}: {addr_title}",
                        'callback_data': f'bottle_history_{address_id}_1',
                    }])
                buttons.append([{
                    'text': i18n.get('telegram.back', language),
                    'callback_data': 'back_to_main',
                }])
                text = '\n'.join(lines)
                keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            if query:
                await self._edit_or_replace_callback_message(
                    query, text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as exc:
            await self._handle_error(update, context, exc=exc, operation="show_bottle_balance")

    async def show_bottle_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Paginated per-address ledger history (callback ^bottle_history_\\d+_\\d+$).

        callback_data layout: ``bottle_history_{address_id}_{page}`` — split on '_'
        so the trailing two segments are always address_id and page."""
        query = update.callback_query
        if query:
            await query.answer()

        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            parts = query.data.split('_')
            address_id = int(parts[-2])
            page = int(parts[-1])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_my_bottle_ledger(user_token, address_id, page)

            if not response or not response.success:
                await self._handle_api_error(
                    update,
                    i18n.get('telegram.bottles.load_error', language),
                    language,
                )
                return

            # `.data` is the success envelope {'success': ..., 'data': {items,...}};
            # fall back to the envelope itself if a caller returns the inner dict.
            envelope = response.data if isinstance(response.data, dict) else {}
            inner = envelope.get('data', envelope) or {}
            items = inner.get('items', []) or []
            total = int(inner.get('total', 0) or 0)
            per_page = int(inner.get('per_page', 10) or 10)
            page = int(inner.get('page', page) or page)

            # The seeded title already begins with the 📜 emoji — do NOT prepend
            # another one here or users see a doubled scroll glyph.
            title = i18n.get('telegram.bottles.history_title', language)
            if not items:
                text = (
                    f"<b>{title}</b>\n\n"
                    f"{i18n.get('telegram.bottles.history_empty', language)}"
                )
            else:
                lines = [f"<b>{title}</b>\n"]
                lines.extend(_render_ledger_lines(items, language))
                text = '\n'.join(lines)

            keyboard = _history_keyboard(address_id, page, per_page, total, language)
            await self._edit_or_replace_callback_message(
                query, text, reply_markup=keyboard, parse_mode='HTML'
            )

        except Exception as exc:
            await self._handle_error(update, context, exc=exc, operation="show_bottle_history")
