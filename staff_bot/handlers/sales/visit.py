"""The visit loop, part 1: start/resume → check in → count the shelf → order screen.

One working draft lives under user_data['sales_visit'] (registered in
flow_state, so a menu tap, /start or the timeout clears it). The draft is a
DRAFT: the visit itself lives on the server, and leaving the conversation
never ends it — only the explicit Abandon button does.

Which screen the agent sees is decided by `visit.current_step`, which the
BACKEND owns. That is what makes resume work from a cold chat: the bot asks
`GET /visits/current` and draws whatever step comes back, instead of trusting
a draft that a restart, a second phone or a menu escape may have outlived.

Nothing here re-derives a backend rule. `distance_m` and `in_radius` are read,
never measured; `suggested_qty` and `rate_per_day` are printed, never
computed; the geofence radius is not a constant this file knows.
"""
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.api_client import TRANSPORT_AMBIGUOUS_ERROR_CODE, api_client
from staff_bot.handlers.sales.hub import SalesHubHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.sales import (
    CLOSE_OUTCOMES,
    EMPTIES_CHOICES,
    NEXT_VISIT_CHOICES,
    NO_ORDER_REASONS,
    ORDER_QTY_CHOICES,
    PAY_METHODS,
    PHOTO_KINDS,
    STOCK_QTY_CHOICES,
    SalesKeyboards,
)
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils import flow_state
from staff_bot.utils.formatters import escape_html, format_currency, format_local_date

logger = logging.getLogger(__name__)

V_CHECKIN, V_STOCK, V_STOCK_QTY, V_ORDER, V_ORDER_EDIT, V_PAYMENT, V_DAY, V_NOTES, V_CONFIRM, V_CLOSE = range(310, 320)
# D26: the one-time photo prompt after check-in. Outside 310-319, which is full; 320-321 are
# Nearby and 322-325 the field try-out.
V_PHOTO = 326
FLOW_KEY = flow_state.SALES_VISIT_FLOW_KEY

# The suffixes each screen accepts. The registered patterns are `\w+` / `\d+`
# shapes (an alternation is invisible to the collision check in
# tests/staff_bot/test_staff_wiring_contract.py), so THESE tuples are the
# guard, re-checked inside every handler.
DAY_CHOICES = ('tomorrow', 'today', 'pick')
CLOSE_STEPS = ('outcome', 'reason', 'notes', 'next')
NOTES_STEP = 'notes'
CLOSE_NOTES_STEP = 'closenotes'
# Both formats a human plausibly types; the year is always four digits, so
# neither can be read as the other.
DATE_FORMATS = ('%d.%m.%Y', '%Y-%m-%d')
MAX_NOTES = 500

REASON_PREFIX = 'staff_sales_v_noreason_'
OQTY_PREFIX = 'staff_sales_v_oqty_'
OQTYSET_PREFIX = 'staff_sales_v_oqtyset_'
PAY_PREFIX = 'staff_sales_v_pay_'
DAY_PREFIX = 'staff_sales_v_day_'
SKIP_PREFIX = 'staff_sales_v_skip_'
OUTCOME_PREFIX = 'staff_sales_v_outcome_'
NEXT_PREFIX = 'staff_sales_v_next_'
PHOTO_PREFIX = 'staff_sales_v_photo_'

# The backend's per-product purchase floor and ceiling, refused as a 400 by
# BOTH `order_estimate` and `place_order`. A 400 carries no body through the
# staff client (`data` survives on 409s only), so the code is all the bot
# gets -- which is why both land on the BASKET, the one screen where a
# quantity can be changed and where each line prints its own floor.
MIN_QTY_ERROR_CODE = 'SALES_ORDER_MIN_QTY'
MAX_QTY_ERROR_CODE = 'SALES_ORDER_QTY_INVALID'
BASKET_ERROR_CODES = (MIN_QTY_ERROR_CODE, MAX_QTY_ERROR_CODE)

# The schedule branch of `place_order` (also a code-less 400 until Task 2
# named it). The day is not a field the confirm card can change, so this one
# re-opens the day question instead.
DATE_ERROR_CODE = 'SALES_DELIVERY_DATE_INVALID'

# An admin un-flagged a SKU while the agent was counting it. The stock-check
# list is the backend's, so the recovery is to ask for it again.
STOCK_PRODUCT_ERROR_CODE = 'SALES_STOCK_PRODUCT_INVALID'

# The three answers `POST /visits/<id>/order` can give for
# `confirmation.state` -- `AgentOrderConfirmationService.CONFIRMATION_STATES`
# (D15), and the VOCABULARY `_order_state_line` gates on. A copy, like the
# four in `keyboards/sales.py`, and pinned against the backend tuple by
# `tests/unit/test_sales_visit_bot_plumbing.py` so a fourth state added there
# fails a test instead of printing nothing on the agent's receipt.
ORDER_STATES = ('auto_confirmed', 'pending_confirmation', 'confirmed')


class VisitHandler(SalesHubHandler):
    """Subclasses the hub so a finished visit can fall back onto the outlet card."""

    # ---- helpers -----------------------------------------------------------------
    async def _flow(self, update, context) -> Optional[Dict]:
        return await self._require_flow(update, context, FLOW_KEY)

    async def _say(self, update: Update, text: str, keyboard=None, *, force_new: bool = False) -> Optional[Message]:
        """Edit the screen the agent is looking at, or send a new message.

        Through `_safe_callback_answer`, never a bare `answer()` -- the
        hub's `_render` uses a bare one, and on the paths here that have
        ALREADY answered (an API error is shown as an alert first) a second
        answer is exactly what Telegram rejects with "query is too old or
        invalid". Raised there, the rejection would escape before the EDIT,
        leaving the agent on a screen whose buttons no longer mean anything.

        `force_new` sends INSTEAD of editing, and deliberately answers
        nothing: it is used where the message on screen has to survive (the
        order receipt the close prompt follows, the reply keyboard handed back
        after a check-in), and every one of those paths has already answered
        its callback. Answering twice there is the same rejection again.

        Returns the message it sent or edited, for the one screen that has to
        be found again later (the D26 photo prompt).
        """
        query = update.callback_query
        if query and not force_new:
            await self._safe_callback_answer(query, None, show_alert=False)
            return await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode='HTML', disable_notification=True
        )

    def _checkin_prompt(self, language: str):
        return CommonKeyboards.location_request(
            language, i18n.get('staff.sales.visit.checkin_button', language), include_cancel=False
        )

    async def _ack(self, update: Update, state: int) -> int:
        """Answer the tap and leave the screen exactly as it is."""
        query = update.callback_query
        if query:
            await self._safe_callback_answer(query, None, show_alert=False)
        return state

    async def _stay(self, update: Update, state: int) -> int:
        """A callback whose payload does not belong to the step the agent is on.

        The registered patterns are `\\d+` shapes, so the VALUE is re-checked
        in each handler and anything else lands here: the tap is answered so
        the button stops spinning, and the same state is returned, which
        leaves the prompt and its keyboard where they are. Deliberately not
        re-rendering -- the copy would be identical and Telegram answers
        "message is not modified", so a redraw is noise, not a re-prompt.
        """
        query = update.callback_query
        # INFO, not WARNING: a stale tap on an older message is routine (the
        # grids are re-drawn constantly and Telegram keeps the old keyboards
        # live), and logging every one at warning buries the real ones.
        logger.info("Unroutable visit callback %s in state %s", query.data if query else None, state)
        return await self._ack(update, state)

    @staticmethod
    def _ids(update: Update, count: int) -> Optional[List[int]]:
        """The trailing `count` integers of a callback, or None when it is junk."""
        data = (update.callback_query.data if update.callback_query else '') or ''
        tail = data.split('_')[-count:]
        if len(tail) != count:
            return None
        try:
            return [int(value) for value in tail]
        except ValueError:
            return None

    @staticmethod
    def _header(flow: Dict) -> str:
        name = flow.get('outlet_name')
        return f"🏪 <b>{escape_html(name)}</b>" if name else ''

    @staticmethod
    def _current_visit_id(flow: Dict) -> Optional[int]:
        """The visit every write in this conversation is addressed to.

        One accessor rather than `flow.get('visit_id')` scattered about: the
        order POST, the close POST and the abandon POST all have to refuse to
        fire without it, and Task 12's half asks the same question.
        """
        return (flow or {}).get('visit_id')

    @staticmethod
    def _product(flow: Dict, product_id: int) -> Optional[Dict]:
        return next((p for p in flow.get('products') or [] if p.get('id') == product_id), None)

    @staticmethod
    def _entry(flow: Dict, product_id: int) -> Dict:
        """This product's draft row, created empty on first touch.

        `on_hand is None` means NOT COUNTED YET, which is not the same as a
        counted zero -- only the first is left out of the POST.
        """
        return flow.setdefault('stock', {}).setdefault(
            str(product_id), {'on_hand': None, 'empties': None, 'sold_out': False, 'low': False}
        )

    @staticmethod
    def _stock_rows(flow: Dict) -> Tuple[List[Dict], List[Dict]]:
        """(every product as `stock_overview` reads it, the counted subset).

        The row is the ruling-32 shape -- `serialize_stock_check`'s own key
        names plus the product's two flags -- so the draft, the backend's
        reply and the keyboard all speak ONE dict. Renaming `product_name` to
        `name` here would draw a shelf of buttons with no product on them and
        no test on callback data would notice.

        `on_hand_qty` stays None for a product nobody has counted: the
        keyboard prints no number for it, and `_stock_items` leaves it out of
        the POST.
        """
        rows, counted = [], []
        for product in flow.get('products') or []:
            entry = (flow.get('stock') or {}).get(str(product.get('id'))) or {}
            row = {
                'product_id': product.get('id'),
                'product_name': product.get('name') or '',
                'on_hand_qty': entry.get('on_hand'),
                'empties_qty': entry.get('empties'),
                'is_sold_out': bool(entry.get('sold_out')),
                'is_low': bool(entry.get('low')),
                'suggested_qty': None,
                'accepted_qty': None,
                'rate_per_day': None,
                'rate_source': 'none',
                'is_returnable_bottle': bool(product.get('is_returnable_bottle')),
                'min_order_quantity': product.get('min_order_quantity'),
            }
            rows.append(row)
            if row['on_hand_qty'] is not None:
                counted.append(row)
        return rows, counted

    @staticmethod
    def _stock_items(flow: Dict) -> List[Dict]:
        """The POST body's `items`, in the backend's own product order.

        A product carrying only a FLAG and no count is left out: the payload
        requires `on_hand_qty`, and inventing a zero would teach the rate rule
        a number the agent never gave.
        """
        items = []
        for product in flow.get('products') or []:
            entry = (flow.get('stock') or {}).get(str(product.get('id'))) or {}
            if entry.get('on_hand') is None:
                continue
            items.append({
                'product_id': product.get('id'),
                'on_hand_qty': entry.get('on_hand'),
                'empties_qty': entry.get('empties'),
                'is_sold_out': bool(entry.get('sold_out')),
                'is_low': bool(entry.get('low')),
            })
        return items

    @staticmethod
    def _has_suggestion(flow: Dict) -> bool:
        """Did the backend suggest a quantity for anything on this shelf?"""
        return any(int(row.get('suggested_qty') or 0) > 0 for row in flow.get('suggestions') or [])

    @staticmethod
    def _has_last(flow: Dict) -> bool:
        """Did it tell us what this outlet took last time?

        `last_order_qty` rides on the POST /stock-check reply ONLY; resuming
        reads `serialize_visit.stock_checks`, which has no such key, so a
        resumed visit legitimately offers no "same as last time" button rather
        than one that would repeat a quantity nobody can see.
        """
        return any(int(row.get('last_order_qty') or 0) > 0 for row in flow.get('suggestions') or [])

    def _seed(self, context, visit: Dict, outlet_name: str) -> Dict:
        """Replace the phone-side draft with what the SERVER says the visit is.

        Every key of the flow dict exists from here, including the ones only
        Task 12's half writes (`payment_methods`, `estimate`, `close`), so no
        later screen has to remember to create one.

        `suggestions` holds the backend's rows VERBATIM -- `product_name`,
        `suggested_qty`, `rate_per_day`, `last_order_qty` -- rather than a
        re-keyed copy: the order screen, the basket and the confirm screen
        then read the same names the API answers with.

        The shelf draft is pre-filled (spec *Visit* step 2, controller ruling
        29): from THIS visit's own `stock_checks` when there are any (a
        resume), otherwise from `previous_stock`, the last completed visit's
        counts. Counts only -- `sold_out` / `low` are claims about today and
        start clear.
        """
        context.user_data.pop(FLOW_KEY, None)
        flow = {
            'visit_id': visit.get('id'),
            'outlet_id': visit.get('outlet_id'),
            'outlet_name': outlet_name or '',
            'step': visit.get('current_step') or 'checkin',
            'products': [],
            # Did `GET /stock-check-products` ANSWER? An empty catalogue and a
            # failed fetch both leave `products` empty and mean opposite
            # things: the first is a shelf with nothing to count (post
            # `items: []` and move on), the second is a shelf nobody has seen.
            'products_ok': False,
            'stock': {},
            'suggestions': list(visit.get('stock_checks') or []),
            'order_lines': {},
            'payment_methods': [],
            'payment_method': None,
            'delivery_date': None,
            'delivery_notes': None,
            'estimate': None,
            # `has_order` is the ONE thing the close screen needs before it
            # asks anything: an order makes the outcome the backend's to stamp.
            'close': {'has_order': bool(visit.get('order'))},
        }
        own_rows = visit.get('stock_checks') or []
        for row in own_rows:
            product_id = row.get('product_id')
            if product_id is None:
                continue
            flow['stock'][str(product_id)] = {
                'on_hand': row.get('on_hand_qty'),
                'empties': row.get('empties_qty'),
                'sold_out': bool(row.get('is_sold_out')),
                'low': bool(row.get('is_low')),
            }
        if not own_rows:
            for row in visit.get('previous_stock') or []:
                product_id = row.get('product_id')
                if product_id is None:
                    continue
                flow['stock'][str(product_id)] = {
                    'on_hand': row.get('on_hand_qty'),
                    'empties': row.get('empties_qty'),
                    'sold_out': False,
                    'low': False,
                }
        context.user_data[FLOW_KEY] = flow
        return flow

    async def _load_current(self, update, context, language: str) -> Optional[Dict]:
        """`GET /visits/current`, rebuilt into the draft. None means "already told them why"."""
        token = await self._get_auth_token(update, context)
        if not token:
            # The same verdict as `_session_gone`, in the shape this helper
            # answers with: every caller ENDs on None.
            await self._handle_auth_error(update, language)
            context.user_data.pop(FLOW_KEY, None)
            return None
        async with api_client as client:
            response = await client.sales_current_visit(token)
        if not response.success:
            if getattr(response, 'error_code', None) == 'SALES_VISIT_NOT_FOUND':
                # The server has no open visit for this agent, so the phone
                # must not keep one: `user_data` outlives the conversation,
                # and a draft left here points the stale-tap entry point (and
                # every button still on an old screen) at a closed visit id.
                # Scoped to THIS code, not to every failure: a 503 means "I
                # could not ask", and dropping a counted shelf for a network
                # blink is the expensive half of the visit thrown away.
                context.user_data.pop(FLOW_KEY, None)
            await self._handle_api_response_error(update, response, language)
            return None
        payload = response.data or {}
        visit = payload.get('visit') or {}
        if not visit.get('id'):
            # A 200 with no visit says the same thing as the 404 above.
            context.user_data.pop(FLOW_KEY, None)
            await self._notify_user(
                update, i18n.get('staff.sales.error.visit_not_open', language), show_alert=True
            )
            return None
        return self._seed(context, visit, (payload.get('outlet') or {}).get('name') or '')

    async def _load_products(self, update, context, flow: Dict, language: str) -> bool:
        """The stock-check list, cached on the draft for the rest of the visit.

        Backend-flagged (`products.in_sales_stock_check`), never a list the
        bot keeps: a SKU switched on in the admin UI shows up on the next
        screen an agent opens, with no bot deploy.

        The return value is also recorded on the draft as `products_ok`,
        because the SCREEN has to tell a fetch that failed from a catalogue
        that is genuinely empty -- the two look identical in `products`.
        """
        flow['products_ok'] = False
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return False
        async with api_client as client:
            response = await client.sales_stock_check_products(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return False
        flow['products'] = [
            {
                'id': item.get('id'),
                'name': item.get('name') or '',
                'is_returnable_bottle': bool(item.get('is_returnable_bottle')),
                'min_order_quantity': item.get('min_order_quantity'),
            }
            for item in ((response.data or {}).get('items') or [])
            if item.get('id') is not None
        ]
        flow['products_ok'] = True
        return True

    async def _session_gone(self, update, context, language: str) -> int:
        """Say the session expired, drop the draft, and END.

        The conversation is over either way, and `user_data` outlives it: a draft
        left behind belongs to no live flow, keeps `_refused_by_open_visit`
        refusing Nearby and the try-out on behalf of it, and points every button
        still on the agent's screen at a visit this phone can no longer post to.
        Nothing is lost -- the VISIT is server-side state, and `_load_current`
        rebuilds the draft from it on the way back in.

        One helper rather than the same two lines in ten handlers: "the session
        ended under me" is one fact, and the one that decides it must not be
        able to mean two things depending on which screen it happened on.
        """
        await self._handle_auth_error(update, language)
        context.user_data.pop(FLOW_KEY, None)
        return ConversationHandler.END

    @staticmethod
    def _visit_is_gone(context, response) -> bool:
        """Did the backend just say this visit is no longer open? Then leave.

        `SALES_VISIT_NOT_OPEN` means the row is closed or abandoned -- by the
        stale-visit sweep, by a second device, by an admin -- and `VisitService`
        raises the same ConflictError from `stock_check`, `place_order` and
        `close`. Every button on the screen the agent is looking at posts to
        that id, so staying is a loop: the same refusal to every tap, with no
        way out but Abandon, which posts to the same dead id.

        One helper rather than the same branch in three handlers: "the visit
        ended under me" is one fact and must not mean three different things.
        The alert has already been shown by the caller; this drops the draft so
        the agent's next message does not land in a visit that is over.
        """
        if getattr(response, 'error_code', None) != 'SALES_VISIT_NOT_OPEN':
            return False
        context.user_data.pop(FLOW_KEY, None)
        return True

    @staticmethod
    def _nothing_to_count(flow: Dict) -> bool:
        """The catalogue ANSWERED and is empty, so `items: []` is the truth.

        Deliberately not "products is empty": a failed fetch leaves it empty
        too, and posting an empty count there would record "nothing on this
        shelf" for a shelf nobody managed to look at.
        """
        return bool(flow.get('products_ok')) and not flow.get('products')

    # ---- screens -----------------------------------------------------------------
    async def _render_checkin(self, update, context, flow: Dict, language: str) -> int:
        """Two messages, because `request_location` lives only on a reply keyboard.

        The first replaces the outlet card with the visit header and the
        inline Skip/Abandon pair; the second carries the location button that
        actually answers the prompt.
        """
        started = i18n.get('staff.sales.visit.started', language)
        # The label is seeded BARE (controller ruling 19) and the outlet name
        # is composed around it. Passing `outlet_name=` instead would be
        # dropped silently by `render_translation`, leaving an agent looking
        # at "Visit started." with no way to tell WHICH outlet they opened.
        await self._say(
            update,
            '\n'.join(line for line in (self._header(flow), f"▶️ {started}") if line),
            SalesKeyboards.checkin(language),
        )
        await self._say(
            update,
            f"📍 {i18n.get('staff.sales.visit.checkin_prompt', language)}",
            self._checkin_prompt(language),
            force_new=True,
        )
        flow['step'] = 'checkin'
        return V_CHECKIN

    async def _render_stock(self, update, context, flow: Dict, language: str, *, edit: bool = True) -> int:
        """The shelf overview: one row per product, counts printed above it.

        Three shapes, because "no products" has two opposite causes. The
        catalogue is re-fetched until it ANSWERS (`products_ok`), so the Retry
        button below re-enters here and a transient outage costs one tap:

        * fetched and non-empty -- the hint and one button per product;
        * fetched and EMPTY -- the empty-state line and Continue, because
          nothing is flagged for the stock check (the state of every install
          until an admin ticks `products.in_sales_stock_check`);
        * NOT fetched -- the API error was already shown as an alert, and the
          screen keeps Retry. No empty-state line: nothing is known about this
          shelf, which is not the same as knowing it is empty.
        """
        if not flow.get('products_ok'):
            await self._load_products(update, context, flow, language)
        rows, counted = self._stock_rows(flow)
        shelf_label = i18n.get('staff.sales.visit.stock_row', language)
        lines = [
            self._header(flow),
            f"📦 <b>{i18n.get('staff.sales.visit.stock_title', language)}</b>",
        ]
        if rows:
            lines.append(i18n.get('staff.sales.visit.stock_hint', language))
        elif self._nothing_to_count(flow):
            lines.append(i18n.get('staff.sales.visit.stock_no_products', language))
        for row in rows:
            if row['on_hand_qty'] is None:
                continue
            # Composed, not interpolated (ruling 19): "• Pure Water 19L — On
            # shelf: 3". The label carries no placeholder in any language, so
            # this is the only place the name and the number can appear.
            lines.append(
                f"• {escape_html(row['product_name'])} — {shelf_label}: {row['on_hand_qty']}"
            )
        text = '\n'.join(line for line in lines if line)
        keyboard = SalesKeyboards.stock_overview(
            language, rows, bool(counted), can_retry=not flow.get('products_ok'),
        )
        await self._say(update, text, keyboard, force_new=not edit)
        flow['step'] = 'stock'
        return V_STOCK

    async def _render_stock_quantity(self, update, flow: Dict, language: str, product_id: int) -> int:
        product = self._product(flow, product_id) or {}
        entry = self._entry(flow, product_id)
        is_returnable = bool(product.get('is_returnable_bottle'))
        lines = [
            self._header(flow),
            f"📦 <b>{escape_html(product.get('name') or '')}</b>",
            i18n.get('staff.sales.visit.stock_qty_prompt', language),
        ]
        if is_returnable:
            lines.append(f"♻️ {i18n.get('staff.sales.visit.stock_empties_prompt', language)}")
        await self._say(
            update,
            '\n'.join(line for line in lines if line),
            SalesKeyboards.stock_quantity(
                language, product_id, entry.get('on_hand'), is_returnable,
                entry.get('empties'), bool(entry.get('sold_out')), bool(entry.get('low')),
            ),
        )
        return V_STOCK_QTY

    def _order_text(self, flow: Dict, language: str) -> str:
        """What the backend suggests, and what this outlet took last time.

        Extracted from `_render_order` because Task 12's confirm/back paths
        redraw the same screen: ONE place builds these lines.

        Both figures are the backend's own fields, printed as given -- the bot
        multiplies nothing. Both lines are COMPOSED around a bare label
        (ruling 19): "Suggested: 20 × Pure Water 19L".
        """
        suggested_label = i18n.get('staff.sales.visit.order_suggested_line', language)
        last_label = i18n.get('staff.sales.visit.order_last_line', language)
        lines = [self._header(flow), f"🧾 <b>{i18n.get('staff.sales.visit.order_title', language)}</b>"]
        for row in flow.get('suggestions') or []:
            name = escape_html(row.get('product_name') or '')
            suggested = row.get('suggested_qty')
            if suggested:
                lines.append(f"✅ {suggested_label}: {suggested} × {name}")
            last_qty = row.get('last_order_qty')
            if last_qty:
                lines.append(f"🔁 {last_label}: {last_qty} × {name}")
        if not self._has_suggestion(flow):
            lines.append(i18n.get('staff.sales.visit.order_none_line', language))
        return '\n'.join(line for line in lines if line)

    async def _render_order(self, update, context, flow: Dict, language: str, *, edit: bool = True) -> int:
        """The order screen. Task 12 reuses this exact signature.

        `has_suggestion` / `has_last` gate the buttons on the backend's fields
        rather than on a rule of the bot's, so a suggestion of nothing is
        never offered as one.
        """
        await self._say(
            update,
            self._order_text(flow, language),
            SalesKeyboards.order_options(language, self._has_suggestion(flow), self._has_last(flow)),
            force_new=not edit,
        )
        flow['step'] = 'order'
        return V_ORDER

    async def _render_step(self, update, context, flow: Dict, language: str, *, edit: bool = True) -> int:
        """Draw the screen for the step the SERVER says the visit is on."""
        step = flow.get('step')
        if step == 'stock':
            return await self._render_stock(update, context, flow, language, edit=edit)
        if step == 'order':
            return await self._render_order(update, context, flow, language, edit=edit)
        if step == 'close':
            return await self._render_close(update, context, flow, language, force_new=not edit)
        return await self._render_checkin(update, context, flow, language)

    async def _offer_resume(self, update, context, language: str) -> int:
        """The 409: this agent already has an open visit somewhere.

        `APIResponse` drops the error `details`, so the open visit's id and
        outlet are unknowable from the refusal itself -- `GET /visits/current`
        is the only way to name the outlet on the offer, and the only way
        Abandon knows what to abandon. The refusal IS this screen; showing an
        error alert on top of it would only describe the two buttons below.
        """
        flow = await self._load_current(update, context, language)
        if flow is None:
            return ConversationHandler.END
        # Composed, not interpolated (ruling 19): the offer has to NAME the
        # outlet -- the open visit may be at a different shop entirely, and
        # "you already have an open visit" without saying where is how an
        # agent abandons the wrong one.
        await self._say(
            update,
            '\n'.join(line for line in (
                self._header(flow),
                f"⏯ {i18n.get('staff.sales.visit.resume_or_abandon', language)}",
            ) if line),
            SalesKeyboards.resume_or_abandon(language),
        )
        return V_CHECKIN

    async def _after_checkin(self, update, context, flow: Dict, language: str, result: str, visit: Dict) -> int:
        """Hand the main menu back, then ask for a photo (D26) or draw the shelf screen.

        The location request replaced the agent's main menu with a one-shot
        prompt keyboard (the `_after_pin` shape in new_outlet.py): one message
        restores the menu and reports the result, the next draws the screen,
        because a message can carry only one keyboard. `photo_requested` is the
        backend's answer; the bot never decides which outlets are asked.
        """
        await self._say(update, result, self._main_menu(context, language), force_new=True)
        if visit.get('photo_requested'):
            return await self._render_photo_request(update, flow, language)
        return await self._render_stock(update, context, flow, language, edit=False)

    async def _render_photo_request(self, update, flow: Dict, language: str) -> int:
        """D26: ask once, right after check-in, at a shop or an office.

        Bot-local: the server's step is already `stock`, so a restart or a second
        device resumes on the shelf and never asks twice. `photo_prompt` marks
        that the kind picker, once answered, leads on to the shelf.
        """
        flow['photo_prompt'] = True
        prompt = await self._say(
            update,
            '\n'.join(line for line in (
                self._header(flow),
                f"📷 {i18n.get('staff.sales.visit.photo_request', language)}",
            ) if line),
            SalesKeyboards.photo_request(language),
            force_new=True,
        )
        # A photo answers the prompt with a NEW shelf message, so this one is
        # not replaced and has to be found by id to lose its buttons.
        flow['photo_prompt_message_id'] = getattr(prompt, 'message_id', None)
        return V_PHOTO

    # ---- entry -------------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def start_visit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """▶ Start visit, from the outlet card.

        The POST opens the visit; the follow-up read hydrates the draft from
        the server (the outlet's NAME, which the POST does not carry, and the
        step, which the server owns) so start, resume and the 409 all reach
        the same screens through one path.
        """
        language = await self._get_language(update, context)
        self._drop_other_sales_drafts(context, FLOW_KEY)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        async with api_client as client:
            response = await client.sales_start_visit(token, outlet_id)
        if not response.success:
            if getattr(response, 'error_code', None) == 'SALES_VISIT_ALREADY_OPEN':
                return await self._offer_resume(update, context, language)
            await self._handle_api_response_error(update, response, language)
            return ConversationHandler.END
        flow = await self._load_current(update, context, language)
        if flow is None:
            return ConversationHandler.END
        return await self._render_step(update, context, flow, language)

    async def _resume(self, update, context, language: str) -> int:
        """Re-read the visit from the server and draw whatever step it is on.

        Undecorated, so it can be called from INSIDE the conversation as well
        as from the entry point: Task 12's order and close paths fall back to
        it whenever the draft has lost the visit id (a restart, a second
        device, a timeout), rather than posting to `None`.
        """
        flow = await self._load_current(update, context, language)
        if flow is None:
            return ConversationHandler.END
        return await self._render_step(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def resume_visit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """⏯ Resume visit, from the card or the hub -- including from a cold chat."""
        language = await self._get_language(update, context)
        return await self._resume(update, context, language)

    @require_auth
    @require_sales_agent
    async def stale_tap(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Any visit button tapped when the conversation is no longer holding it.

        PTB keeps conversation state in memory, so a bot RESTART empties it
        while every screen an agent is looking at keeps its buttons. Before
        this, those taps matched no handler at all: the button spun and
        nothing happened, on a visit that is still open on the server.

        Registered as the LAST entry point rather than as a second global
        handler (the `staff_transfer_driver_\\d+` precedent in bot.py): the
        return value has to ARM the conversation, which a top-level handler's
        cannot, or the very next tap lands here again.

        It re-reads `GET /visits/current` and draws whatever step the SERVER
        says the visit is on -- the same `_resume` the card's button uses, so
        the live path and the stale path cannot answer differently -- or the
        `visit_not_open` alert when nothing is open any more.
        """
        language = await self._get_language(update, context)
        return await self._resume(update, context, language)

    @require_auth
    @require_sales_agent
    async def resume_from_conflict(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`Resume` on the 409 offer.

        The draft was already hydrated by the offer, so this re-reads nothing:
        the two screens are one tap apart and a second GET would only invite
        them to disagree.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        return await self._render_step(update, context, flow, language)

    # ---- check-in ----------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def receive_checkin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The pin shared at the shop door.

        Deliberately NOT refused when it falls outside the delivery polygon,
        unlike the new-outlet pin: a check-in is a record of where the agent
        actually stood. The backend measures `distance_m` and decides
        `in_radius`; this only reads them back.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        location = update.effective_message.location if update.effective_message else None
        if not location:
            return V_CHECKIN
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        payload = {
            'latitude': location.latitude,
            'longitude': location.longitude,
            'horizontal_accuracy': getattr(location, 'horizontal_accuracy', None),
        }
        async with api_client as client:
            response = await client.sales_checkin(token, flow.get('visit_id'), payload)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            gone = self._visit_is_gone(context, response)
            if gone:
                # The check-in reply keyboard is the one-shot location prompt
                # (`_checkin_prompt`, no Cancel) -- it replaced the persistent
                # main menu, and ending bare would leave the agent holding a
                # phone with no menu on it. `_leave_to_menu` is the one exit
                # that hands it back.
                return await self._leave_to_menu(
                    update, context, self._resolve_response_error(language, response, html=True), language
                )
            # The one-shot keyboard collapsed when the pin was sent; without
            # this the agent has no button left to retry with.
            await self._say(
                update,
                f"📍 {i18n.get('staff.sales.visit.checkin_prompt', language)}",
                self._checkin_prompt(language),
                force_new=True,
            )
            return V_CHECKIN
        visit = (response.data or {}).get('visit') or {}
        distance = visit.get('distance_m')
        # An outlet with no pin has no distance and no radius to be inside of.
        # Unreachable for a visitable outlet (activation requires a pin), and
        # reported honestly rather than as a fabricated zero if it ever is.
        shown = '—' if distance is None else int(round(float(distance)))
        key = 'staff.sales.visit.checkin_ok' if visit.get('in_radius') is not False else 'staff.sales.visit.checkin_far'
        mark = '✅' if visit.get('in_radius') is not False else '⚠️'
        flow['step'] = visit.get('current_step') or 'stock'
        return await self._after_checkin(
            update, context, flow, language,
            f"{mark} {i18n.get(key, language, distance=shown)}",
            visit,
        )

    @require_auth
    @require_sales_agent
    async def skip_checkin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """No fix, or no time. `{"skipped": true}` alone -- never an invented pin."""
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        async with api_client as client:
            response = await client.sales_checkin(token, flow.get('visit_id'), {'skipped': True})
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            gone = self._visit_is_gone(context, response)
            if gone:
                # Same trap as `receive_checkin`: this posts to the same
                # `/checkin` endpoint from the same one-shot location
                # keyboard, and staying on V_CHECKIN keeps both the dead
                # draft and the menu-less reply keyboard.
                return await self._leave_to_menu(
                    update, context, self._resolve_response_error(language, response, html=True), language
                )
            return V_CHECKIN
        await self._ack(update, V_CHECKIN)
        try:
            # The prompt message keeps its text but loses its buttons: the
            # step has moved on, and a Skip that now answers with a step
            # error is worse than no button at all.
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except TelegramError as error:
            logger.debug("Could not clear the check-in keyboard: %s", error)
        visit = (response.data or {}).get('visit') or {}
        flow['step'] = visit.get('current_step') or 'stock'
        return await self._after_checkin(
            update, context, flow, language,
            f"⏭ {i18n.get('staff.sales.visit.checkin_skipped', language)}",
            visit,
        )

    # ---- the shelf ---------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def open_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        ids = self._ids(update, 1)
        if ids is None or self._product(flow, ids[0]) is None:
            return await self._stay(update, V_STOCK)
        return await self._render_stock_quantity(update, flow, language, ids[0])

    @require_auth
    @require_sales_agent
    async def set_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        ids = self._ids(update, 2)
        if ids is None or self._product(flow, ids[0]) is None or ids[1] not in STOCK_QTY_CHOICES:
            return await self._stay(update, V_STOCK_QTY)
        product_id, value = ids
        entry = self._entry(flow, product_id)
        # An explicit tap makes the number the AGENT's, even when it equals
        # the zero `toggle_sold_out` auto-set — including a re-tap of the
        # same value, which returns early below.
        entry.pop('auto_zero', None)
        if entry.get('on_hand') == value:
            # Same value twice: the redraw would be byte-identical and
            # Telegram answers "message is not modified".
            return await self._ack(update, V_STOCK_QTY)
        entry['on_hand'] = value
        return await self._render_stock_quantity(update, flow, language, product_id)

    @require_auth
    @require_sales_agent
    async def set_empties(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Empty bottles waiting at the outlet -- returnable SKUs only.

        `is_returnable_bottle` is the BACKEND's flag (the `Product` SSOT); the
        grid is not drawn for a SKU it says carries no deposit, and a tap that
        arrives for one anyway is refused here too.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        ids = self._ids(update, 2)
        if ids is None or ids[1] not in EMPTIES_CHOICES:
            return await self._stay(update, V_STOCK_QTY)
        product = self._product(flow, ids[0])
        if product is None or not product.get('is_returnable_bottle'):
            return await self._stay(update, V_STOCK_QTY)
        product_id, value = ids
        entry = self._entry(flow, product_id)
        if entry.get('empties') == value:
            return await self._ack(update, V_STOCK_QTY)
        entry['empties'] = value
        return await self._render_stock_quantity(update, flow, language, product_id)

    @require_auth
    @require_sales_agent
    async def toggle_sold_out(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Sold out IS a count: the shelf holds zero.

        Without the zero the flag never reached `POST /stock-check` at all --
        `_stock_items` leaves out every product with no `on_hand`, so an agent
        who flagged a product they had no number for saw 🚫 on the overview and
        the backend saw no row. It is also the highest-value input to the
        replenishment rate, and the case where it matters most is exactly the
        case where there is nothing on the shelf to count.

        Clearing the flag takes back the zero the flag ITSELF set, and only
        that one: `auto_zero` marks a count the bot made so the agent can
        un-make it with the same tap they made it with. A zero they typed on
        the grid is theirs and stays.

        The Low flag goes with it. Low is a claim ABOUT a count, and `toggle_low`
        can only check that at tap time -- withdrawing the count later would
        otherwise leave a flag on an uncounted product, which `_stock_items`
        drops from the POST while the overview still decorates the button.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        ids = self._ids(update, 1)
        if ids is None or self._product(flow, ids[0]) is None:
            return await self._stay(update, V_STOCK_QTY)
        entry = self._entry(flow, ids[0])
        entry['sold_out'] = not entry.get('sold_out')
        if entry['sold_out']:
            if entry.get('on_hand') is None:
                entry['on_hand'] = 0
                entry['auto_zero'] = True
        elif entry.pop('auto_zero', False):
            entry['on_hand'] = None
            entry['low'] = False
        return await self._render_stock_quantity(update, flow, language, ids[0])

    @require_auth
    @require_sales_agent
    async def toggle_low(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """"Running low" is a claim ABOUT a count, so it needs one.

        `SalesKeyboards.stock_quantity` draws the button only once `on_hand` is
        set; this is the same rule for the stale tap Telegram can still deliver
        from an older message, and it is the guard that keeps the flag from
        being written into a row `_stock_items` would then drop.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        ids = self._ids(update, 1)
        if ids is None or self._product(flow, ids[0]) is None:
            return await self._stay(update, V_STOCK_QTY)
        if self._entry(flow, ids[0]).get('on_hand') is None:
            return await self._stay(update, V_STOCK_QTY)
        entry = self._entry(flow, ids[0])
        entry['low'] = not entry.get('low')
        return await self._render_stock_quantity(update, flow, language, ids[0])

    @require_auth
    @require_sales_agent
    async def stock_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        return await self._render_stock(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def stock_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Re-fetch a catalogue the first GET could not deliver.

        Without it a single failed fetch left the shelf screen with Abandon as
        the only move: `_load_products` is re-attempted on a render, and no
        button on that screen produced one.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if not await self._load_products(update, context, flow, language):
            # The alert already said why, and the screen -- with this same
            # Retry button on it -- is still correct. Re-drawing it would be
            # byte-identical, which Telegram answers "message is not modified".
            return V_STOCK
        return await self._render_stock(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def stock_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Submit the counts and land on the order screen.

        The reply carries the backend's `suggested_qty` / `rate_per_day` /
        `last_order_qty` and the step it moved the visit to -- all four are
        stored as given.

        An EMPTY body is a valid submission when the catalogue answered with an
        empty list: the backend accepts it and advances the visit, which is the
        only way out of the shelf step on an install where nothing is flagged
        for the stock check. It is refused for a catalogue that never arrived.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        items = self._stock_items(flow)
        if not items and not self._nothing_to_count(flow):
            # The button is not drawn without a count; a stale tap can still
            # arrive from an older message.
            return await self._stay(update, V_STOCK)
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        async with api_client as client:
            response = await client.sales_stock_check(token, flow.get('visit_id'), items)
        if not response.success:
            # The overview is still on screen and still correct: the alert
            # explains, and a redraw would be identical.
            await self._handle_api_response_error(update, response, language)
            if self._visit_is_gone(context, response):
                return ConversationHandler.END
            if getattr(response, 'error_code', None) == STOCK_PRODUCT_ERROR_CODE:
                # The catalogue moved under the agent. Re-fetch it and redraw:
                # the product the backend no longer lists then leaves the
                # screen AND the next POST (`_stock_items` walks `products`),
                # which is the only way off this step -- every retry with the
                # stale list is refused exactly the same way.
                await self._load_products(update, context, flow, language)
                return await self._render_stock(update, context, flow, language)
            return V_STOCK
        payload = response.data or {}
        # Stored VERBATIM: the eleven-key rows carry `suggested_qty`,
        # `rate_per_day` and `last_order_qty`, and the order screen reads
        # the backend's own key names off them.
        flow['suggestions'] = list(payload.get('items') or [])
        flow['step'] = (payload.get('visit') or {}).get('current_step') or 'order'
        return await self._render_step(update, context, flow, language)

    # ---- the basket ----------------------------------------------------------------
    # `_has_suggestion` / `_has_last` are Task 11's (the order screen gates its
    # buttons on them); this half only READS them.
    @staticmethod
    def _lines_from(flow: Dict, field: str) -> Dict:
        """`{str(product_id): quantity}` built from one backend field.

        Both "take the suggestion" and "same as last time" are the same move
        over a different column, and the column is the BACKEND's answer in
        both cases -- the bot multiplies nothing and remembers nothing.
        """
        lines = {}
        for item in flow.get('suggestions') or []:
            product_id = item.get('product_id')
            quantity = int(item.get(field) or 0)
            if product_id is not None and quantity > 0:
                lines[str(product_id)] = quantity
        return lines

    @staticmethod
    def _default_lines(flow: Dict) -> Dict:
        """What the edit screen opens on: the BACKEND's published `suggested_qty`.

        Rendered as-is. None and 0 both open the row on 0, and that is the whole
        rule — the bot adds no fallback and makes no judgement about which of
        the two it was handed.

        Every fallback and every refusal behind that number belongs to
        `ReplenishmentService.suggested_qty`: it already means "the suggestion,
        else what they took last time", it answers an explicit 0 for a shelf
        that is covered, and it answers None where an order cannot be placed at
        all — a purchase minimum above the line ceiling publishes None BESIDE a
        non-null `last_order_qty` on purpose. So reaching for `last_order_qty`
        here would not be a harmless second opinion; it would pre-fill last
        month's quantity over the backend's refusal, one tap from being sent,
        which is exactly the defect this method was rewritten to remove.
        """
        lines = {}
        for item in flow.get('suggestions') or []:
            product_id = item.get('product_id')
            if product_id is None:
                continue
            lines[str(product_id)] = int(item.get('suggested_qty') or 0)
        return lines

    @staticmethod
    def _basket_rows(flow: Dict) -> List[Dict]:
        """`SalesKeyboards.order_edit` rows, in the backend's own product order."""
        rows = []
        order_lines = flow.get('order_lines') or {}
        for item in flow.get('suggestions') or []:
            product_id = item.get('product_id')
            if product_id is None:
                continue
            rows.append({
                'product_id': product_id,
                # `product_name`, not `name`: controller ruling 32 fixes the
                # key `SalesKeyboards.order_edit` reads, and a rename here
                # draws a basket of nameless " × 20" buttons that no
                # callback-data assertion would notice.
                'product_name': item.get('product_name') or '',
                'quantity': int(order_lines.get(str(product_id)) or 0),
            })
        return rows

    @classmethod
    def _order_items(cls, flow: Dict) -> List[Dict]:
        """`PlaceOrderPayload.items` -- only the lines with something on them."""
        return [{'product_id': row['product_id'], 'quantity': row['quantity']}
                for row in cls._basket_rows(flow) if row['quantity'] > 0]

    @staticmethod
    def _knows_product(flow: Dict, product_id: int) -> bool:
        return any(item.get('product_id') == product_id for item in flow.get('suggestions') or [])

    @staticmethod
    def _product_name(flow: Dict, product_id: int) -> str:
        for item in flow.get('suggestions') or []:
            if item.get('product_id') == product_id:
                return item.get('product_name') or ''
        return ''

    def _basket_text(self, flow: Dict, language: str) -> str:
        lines = [i18n.get('staff.sales.visit.order_edit_title', language)]
        for row in self._basket_rows(flow):
            lines.append(f"• {escape_html(row['product_name'])} × {row['quantity']}")
        return "\n".join(lines)

    async def _render_order_edit(self, update: Update, flow: Dict, language: str) -> int:
        await self._say(update, self._basket_text(flow, language),
                        SalesKeyboards.order_edit(language, self._basket_rows(flow)))
        return V_ORDER_EDIT

    @require_auth
    @require_sales_agent
    async def take_suggestion(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        lines = self._lines_from(flow, 'suggested_qty')
        if not lines:
            return await self._stay(update, V_ORDER)
        flow['order_lines'] = lines
        return await self._after_lines(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def take_last_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """"Same as last time", per product.

        The card's `last_orders` is a list of ORDER headers, not of lines, so
        the per-product quantity comes from the stock-check reply's
        `last_order_qty` -- the only itemised answer the backend gives.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        lines = self._lines_from(flow, 'last_order_qty')
        if not lines:
            return await self._stay(update, V_ORDER)
        flow['order_lines'] = lines
        return await self._after_lines(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def edit_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if not flow.get('order_lines'):
            flow['order_lines'] = self._default_lines(flow)
        return await self._render_order_edit(update, flow, language)

    @require_auth
    @require_sales_agent
    async def open_order_line(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        suffix = (update.callback_query.data or '').split(OQTY_PREFIX, 1)[-1]
        if not suffix.isdigit() or not self._knows_product(flow, int(suffix)):
            return await self._stay(update, V_ORDER_EDIT)
        product_id = int(suffix)
        current = int((flow.get('order_lines') or {}).get(str(product_id)) or 0)
        line = f"• {escape_html(self._product_name(flow, product_id))} × {current}"
        # The backend's published `min_order_quantity`, printed beside the
        # line it constrains -- composed around a bare label (ruling 19), and
        # only where there IS a floor: "min 1" is not a rule, it is noise.
        minimum = (self._product(flow, product_id) or {}).get('min_order_quantity')
        if minimum and int(minimum) > 1:
            line = f"{line} · {i18n.get('staff.sales.visit.order_min', language)} {int(minimum)}"
        text = f"{i18n.get('staff.sales.visit.order_edit_title', language)}\n{line}"
        await self._say(update, text, SalesKeyboards.order_quantity(language, product_id, current))
        return V_ORDER_EDIT

    @require_auth
    @require_sales_agent
    async def set_order_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        parts = (update.callback_query.data or '').split(OQTYSET_PREFIX, 1)[-1].split('_')
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return await self._stay(update, V_ORDER_EDIT)
        product_id, quantity = int(parts[0]), int(parts[1])
        # ORDER_QTY_CHOICES is the grid `SalesKeyboards.order_quantity`
        # draws; the registered pattern is `\d+_\d+`, wider than the grid,
        # so the value is checked HERE against the same tuple the button
        # came from.
        if quantity not in ORDER_QTY_CHOICES or not self._knows_product(flow, product_id):
            return await self._stay(update, V_ORDER_EDIT)
        flow.setdefault('order_lines', {})[str(product_id)] = quantity
        return await self._render_order_edit(update, flow, language)

    @require_auth
    @require_sales_agent
    async def order_lines_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if not self._order_items(flow):
            # Every line zeroed is not an order; it is the no-order path, and
            # that one records WHY.
            return await self._stay(update, V_ORDER_EDIT)
        return await self._after_lines(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def no_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        await self._say(update, i18n.get('staff.sales.visit.no_order_reason_prompt', language),
                        SalesKeyboards.no_order_reasons(language))
        return V_ORDER

    @staticmethod
    def _reason_state(flow: Dict) -> Optional[int]:
        """Which screen the reason grid is on -- None when it is on NEITHER.

        It is reachable from two: the order screen's *No order*, which draws
        the grid without starting the close (`close['step']` is unset), and the
        close screen's own reason step. Any OTHER close step means the grid is
        not what is on screen and the tap came from an older message -- and
        returning V_ORDER for it (as this used to) silently moved the agent's
        PTB state off the close screen they are looking at, so the next tap on
        it reached no handler at all.
        """
        step = (flow.get('close') or {}).get('step')
        if step == 'reason':
            return V_CLOSE
        if step in CLOSE_STEPS:
            return None
        return V_ORDER

    @require_auth
    @require_sales_agent
    async def choose_no_reason(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The reason IS the outcome: a no-order visit needs no second question."""
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        reason = (update.callback_query.data or '').split(REASON_PREFIX, 1)[-1]
        state = self._reason_state(flow)
        if state is None:
            # The sibling of `skip_close_notes`'s guard: the close screen is on
            # a different question, and taking this tap would stamp the outcome
            # of a visit whose outcome is still being asked for.
            return await self._stay(update, V_CLOSE)
        if reason not in NO_ORDER_REASONS:
            return await self._stay(update, state)
        close = flow.setdefault('close', {})
        close['no_order_reason'] = reason
        close['outcome'] = 'no_order'
        close.pop('step', None)
        return await self._render_close(update, context, flow, language)

    # ---- rails, day, notes, confirm -------------------------------------------------
    async def _after_lines(self, update, context, flow: Dict, language: str) -> int:
        """Lines are chosen: ask the BACKEND which rails this outlet may use.

        Never a list the bot keeps. A cash-only outlet, an outlet whose
        contract has just been signed and an outlet that may not order at all
        are three different answers to this one call, and only the backend
        knows which is which.
        """
        outlet_id = flow.get('outlet_id')
        if outlet_id is None:
            return await self._resume(update, context, language)
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        async with api_client as client:
            response = await client.sales_payment_methods(token, outlet_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return V_ORDER
        offered = (response.data or {}).get('methods') or []
        methods = []
        for entry in offered:
            method = entry.get('method') if isinstance(entry, dict) else entry
            if method in PAY_METHODS and method not in methods:
                methods.append(method)
        if not methods:
            # An outlet with no rail cannot be ordered for at all; saying so
            # here is kinder than a 400 read at the end of the visit. Its OWN
            # key, not the not-active refusal: the commonest cause is an
            # ACTIVE outlet whose COD ceiling is reached or whose contract is
            # unsigned, and "ask an operator to activate it" is an instruction
            # that leads nowhere for that agent. The screen does not move: the
            # basket is still valid for the moment the account is opened
            # (controller ruling 24).
            await self._notify_user(update, i18n.get('staff.sales.visit.no_rails', language),
                                    show_alert=True)
            return V_ORDER
        flow['payment_methods'] = methods
        if len(methods) == 1:
            # One rail is not a choice; asking would be theatre.
            flow['payment_method'] = methods[0]
            return await self._ask_day(update, language)
        # The keyboard takes the GET's rows VERBATIM (controller ruling 32):
        # it filters to PAY_METHODS itself and labels each button from the seeded
        # `staff.sales.visit.pay.<method>` family. `methods` above is the same set flattened to
        # method strings, kept on the flow because that -- not the pattern --
        # is what `choose_payment` re-validates a tap against.
        await self._say(update, i18n.get('staff.sales.visit.payment_prompt', language),
                        SalesKeyboards.payment_methods(language, offered))
        return V_PAYMENT

    @require_auth
    @require_sales_agent
    async def choose_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        method = (update.callback_query.data or '').split(PAY_PREFIX, 1)[-1]
        if method not in PAY_METHODS or method not in (flow.get('payment_methods') or []):
            # The pattern is `\w+`-wide; what this outlet may use is what the
            # backend just said it may use, and nothing else.
            return await self._stay(update, V_PAYMENT)
        flow['payment_method'] = method
        return await self._ask_day(update, language)

    async def _ask_day(self, update: Update, language: str) -> int:
        await self._say(update, i18n.get('staff.sales.visit.day_prompt', language),
                        SalesKeyboards.delivery_day(language))
        return V_DAY

    @require_auth
    @require_sales_agent
    async def day_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """⬅️ from the delivery day, to the last question that was ASKED.

        Two rails means the rail WAS a question, so Back re-asks it; one rail
        was never asked (`_after_lines` skips the screen entirely), so Back is
        the order options, where every other move still lives.

        The rails come off the draft, never from a second
        `GET /payment-methods`: the list cannot change between two taps, and a
        re-fetch is only a chance for the two screens to disagree. The
        keyboard takes method STRINGS as happily as the GET's rows.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        # Backing out of a RE-asked day leaves the draft by another door, so the
        # mark goes with it: a day chosen after an ordinary walk through the
        # screens must still reach the note prompt.
        flow.pop('date_redo', None)
        methods = flow.get('payment_methods') or []
        if len(methods) > 1:
            await self._say(update, i18n.get('staff.sales.visit.payment_prompt', language),
                            SalesKeyboards.payment_methods(language, methods))
            return V_PAYMENT
        return await self._render_order(update, context, flow, language)

    @staticmethod
    def _parse_date(text: str) -> Optional[date]:
        raw = (text or '').strip()
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    @require_auth
    @require_sales_agent
    async def choose_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        choice = (update.callback_query.data or '').split(DAY_PREFIX, 1)[-1]
        if choice not in DAY_CHOICES:
            return await self._stay(update, V_DAY)
        if choice == 'pick':
            # WITH the day keyboard: the prompt used to render bare, so an
            # agent who tapped 🗓 by accident had no button left at all —
            # not even the Abandon every other visit screen carries.
            await self._say(update, i18n.get('staff.sales.visit.day_pick_prompt', language),
                            SalesKeyboards.delivery_day(language))
            return V_DAY
        chosen = date.today() if choice == 'today' else date.today() + timedelta(days=1)
        flow['delivery_date'] = chosen.isoformat()
        return await self._after_day(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def receive_delivery_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A typed date. Yesterday is refused HERE, not read back as a 400.

        The backend validates the schedule too (`parse_and_validate_schedule`);
        this is the same rule stated where the agent can still fix it in one
        message, and it refuses only the past -- how far ahead an order may be
        dated stays the backend's answer.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        message = update.effective_message
        chosen = self._parse_date(message.text if message else '')
        if chosen is None or chosen < date.today():
            await self._say(update, i18n.get('staff.sales.visit.day_invalid', language),
                            SalesKeyboards.delivery_day(language))
            return V_DAY
        flow['delivery_date'] = chosen.isoformat()
        return await self._after_day(update, context, flow, language)

    async def _after_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                         flow: Dict, language: str) -> int:
        """Where a chosen day goes next: the note prompt, or straight back.

        Normally the note is the last question of the draft, so it follows the
        day. On the `SALES_DELIVERY_DATE_INVALID` recovery the draft is already
        complete and the day is the ONLY field being re-asked -- asking the note
        again there offers a Skip that deletes one the agent has already typed.
        """
        if flow.pop('date_redo', False):
            return await self._render_confirm(update, context, flow, language)
        return await self._ask_notes(update, language)

    async def _ask_notes(self, update: Update, language: str) -> int:
        await self._say(update, i18n.get('staff.sales.visit.notes_prompt', language),
                        SalesKeyboards.visit_skip(language, NOTES_STEP))
        return V_NOTES

    @require_auth
    @require_sales_agent
    async def receive_order_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        message = update.effective_message
        text = ((message.text if message else '') or '').strip()[:MAX_NOTES]
        flow['delivery_notes'] = text or None
        return await self._render_confirm(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def skip_order_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        step = (update.callback_query.data or '').split(SKIP_PREFIX, 1)[-1]
        if step != NOTES_STEP:
            # The same `staff_sales_v_skip_\w+` shape is drawn on the close
            # screen too; each state accepts only its own step.
            return await self._stay(update, V_NOTES)
        flow['delivery_notes'] = None
        return await self._render_confirm(update, context, flow, language)

    async def _estimate(self, update, token: str, flow: Dict, language: str) -> Tuple[Optional[Dict], Optional[str]]:
        """`(the backend's quote, the error code it refused with)`.

        The code comes back because the CALLER decides which screen a refusal
        lands on, and the two are not the same: a sub-minimum line belongs on
        the basket, anything else on the order options.
        """
        outlet_id, items = flow.get('outlet_id'), self._order_items(flow)
        if outlet_id is None or not items:
            return None, None
        async with api_client as client:
            response = await client.sales_order_estimate(
                token, outlet_id, items, flow.get('payment_method')
            )
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return None, getattr(response, 'error_code', None)
        return ((response.data or {}).get('estimate') or {}), None

    def _confirm_rows(self, flow: Dict, estimate: Optional[Dict]) -> List[Dict]:
        rows = (estimate or {}).get('items') or []
        if rows:
            return [{'product_name': row.get('product_name') or '', 'quantity': row.get('quantity') or 0}
                    for row in rows]
        return [{'product_name': self._product_name(flow, row['product_id']), 'quantity': row['quantity']}
                for row in self._basket_rows(flow) if row['quantity'] > 0]

    def _confirm_text(self, flow: Dict, estimate: Optional[Dict], language: str) -> str:
        """The last screen before money moves: what, how much, how paid, when.

        The total renders through `format_currency` -- the same helper the
        outlet card's receivable uses -- rather than a new "total" key
        (controller ruling 24), and the day through `format_local_date`, the
        bot's one date renderer.
        """
        lines = [f"<b>{i18n.get('staff.sales.visit.confirm_title', language)}</b>"]
        if flow.get('outlet_name'):
            lines.append(f"🏪 {escape_html(flow['outlet_name'])}")
        for row in self._confirm_rows(flow, estimate):
            lines.append(f"• {escape_html(row['product_name'])} × {row['quantity']}")
        total = (estimate or {}).get('total_amount')
        if total is not None:
            lines.append(f"💰 {format_currency(total, language=language)}")
        method = flow.get('payment_method')
        if method in PAY_METHODS:
            lines.append(f"💳 {i18n.get(f'staff.sales.visit.pay.{method}', language)}")
        if flow.get('delivery_date'):
            lines.append(f"📅 {format_local_date(flow['delivery_date'])}")
        if flow.get('delivery_notes'):
            lines.append(f"📝 {escape_html(flow['delivery_notes'])}")
        return "\n".join(lines)

    async def _show_confirm(self, update: Update, flow: Dict, language: str) -> int:
        """Re-draw the confirm screen from the LAST quote -- no second pricing.

        Used after a refused send: the basket did not change, so asking the
        backend to price it again would be a second call whose only possible
        effect is a different number under an unchanged list.
        """
        await self._say(update, self._confirm_text(flow, flow.get('estimate'), language),
                        SalesKeyboards.order_confirm(language))
        return V_CONFIRM

    async def _render_confirm(self, update, context, flow: Dict, language: str) -> int:
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        estimate, error_code = await self._estimate(update, token, flow, language)
        flow['estimate'] = estimate
        if estimate is None:
            if error_code in BASKET_ERROR_CODES:
                # The basket is what the backend refused, so the basket is the
                # screen: it is the only one a quantity can be changed on, and
                # it prints each line's floor.
                return await self._render_order_edit(update, flow, language)
            # No quote, no confirm screen -- they are the same screen. The
            # confirm card simply omits the 💰 line when the total is missing,
            # which leaves Place order live under a basket with no price on it:
            # the one screen where the agent reads a number out to the
            # shopkeeper, and a send from it commits the store to a total
            # nobody has seen. The order options keep every move alive
            # (re-price, edit, no order, abandon) and the basket is untouched.
            return await self._render_order(update, context, flow, language)
        return await self._show_confirm(update, flow, language)

    @staticmethod
    def _order_state_line(state: str, language: str) -> str:
        """Which of the three result lines the agent reads.

        `confirmation.state` is the backend's (D15) -- whether the store has
        to confirm in its own bot, whether it was confirmed at creation, or
        whether the instant-COD rule already did it. `ORDER_STATES` is the
        gate (and the pin against the backend's own tuple); the three i18n
        KEYS stay written out as literals so staff_bot/i18n.py's key scraper
        sees all three and /health requires them.

        Anything else -- including the 409's `details.order`, which carries no
        state at all -- prints no line rather than a guessed one.
        """
        if state not in ORDER_STATES:
            return ''
        if state == 'pending_confirmation':
            return i18n.get('staff.sales.visit.order_pending_confirmation', language)
        if state == 'confirmed':
            return i18n.get('staff.sales.visit.order_confirmed', language)
        return i18n.get('staff.sales.visit.order_auto_confirmed', language)

    @staticmethod
    def _order_schedule_lines(order: Dict, language: str) -> List[str]:
        """The RESOLVED schedule, read off the reply (controller ruling 35).

        Never the day the agent tapped. `serialize_order_placed` publishes
        what was WRITTEN precisely because the backend may substitute the
        outlet's standing window (`visit_service.py:418-437`) -- or drop it,
        leaving the order "anytime" -- and echoing the request here is how a
        shopkeeper is told a window their order has not got.

        The window is all-or-nothing on the backend's side and printed
        all-or-nothing here, through ONE key that carries both times: an
        unlabelled `09:00-12:00` is the only line on this screen that says
        nothing in any language about what the numbers mean.
        """
        lines = []
        day = format_local_date(order.get('delivery_date'))
        if day:
            lines.append(f"📅 {day}")
        start, end = order.get('delivery_window_start'), order.get('delivery_window_end')
        if start and end:
            lines.append("🕘 " + i18n.get(
                'staff.sales.visit.window_line', language,
                start=escape_html(str(start)), end=escape_html(str(end)),
            ))
        return lines

    def _order_result_text(self, order: Dict, state: str, language: str) -> str:
        lines = [i18n.get('staff.sales.visit.order_created', language,
                          order_number=escape_html(order.get('order_number') or ''))]
        line = self._order_state_line(state, language)
        if line:
            lines.append(line)
        # What the store was CHARGED, not what the quote said: `create_order`
        # is the only call that bills, and a tier discount or a contract price
        # can move the number between the two. `format_currency` is the same
        # helper the confirm card and the outlet card's receivable use, so the
        # agent reads one shape of money everywhere. Absent on the 409's
        # `details.order`? No -- `serialize_order_brief` carries it too, which
        # is why the guard is `is not None` and not a key check.
        total = order.get('total_amount')
        if total is not None:
            lines.append(f"💰 {format_currency(total, language=language)}")
        # Empty for the 409's `details.order`, which is `serialize_order_brief`
        # and carries no schedule: nothing was created now, so there is no
        # resolved day for this reply to report.
        lines.extend(self._order_schedule_lines(order, language))
        return "\n".join(lines)

    @staticmethod
    def _existing_order(response) -> Dict:
        """The order a 409 names, when it names one.

        `_make_request` keeps the error body on `data` for 409s ONLY
        (staff_bot/api_client.py:415-431), so this is the one refusal the bot
        can report by order number instead of by sentence.
        """
        details = (getattr(response, 'data', None) or {}).get('details') or {}
        order = details.get('order') or {}
        return order if order.get('order_number') else {}

    @require_auth
    @require_sales_agent
    async def confirm_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send the order. The ONLY place this bot creates one.

        The body is exactly `PlaceOrderPayload`: no window (a sales visit
        books a day, not a slot), no `order_source`, no `created_by_staff_id`
        -- the backend stamps both from the token, and a bot that sent them
        would be a second place deciding who placed the order.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        visit_id = self._current_visit_id(flow)
        if visit_id is None:
            return await self._resume(update, context, language)
        items = self._order_items(flow)
        if not items:
            return await self._render_order(update, context, flow, language)
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        payload = {
            'items': items,
            'payment_method': flow.get('payment_method'),
            'delivery_date': flow.get('delivery_date'),
            'delivery_window_start': None,
            'delivery_window_end': None,
            'delivery_notes': flow.get('delivery_notes'),
        }
        async with api_client as client:
            response = await client.sales_place_order(token, visit_id, payload)
        if not response.success:
            error_code = getattr(response, 'error_code', None)
            if error_code == TRANSPORT_AMBIGUOUS_ERROR_CODE:
                # An AMBIGUOUS transport failure: the request died in the
                # read/write phase, so the bot cannot know whether the store
                # was just billed. POSTs are never auto-retried (controller
                # ruling 30, spec *Failure modes*) and "it failed" would be a
                # guess in the direction that produces a second order for the
                # same visit. The honest answer is the visit itself: end the
                # conversation and offer the Resume that re-reads the server.
                #
                # Scoped to that phase ALONE, on the code the client already
                # stamps (`_make_request`, AMBIGUOUS_PHASE_ERRORS vs
                # NEVER_DELIVERED_ERRORS). A connect-phase exhaustion comes
                # back with `status_code is None` and NO code, provably never
                # reached the backend, and falls through to the ordinary
                # refusal below -- "this may already have been recorded" would
                # be a lie there, and a warning that cries wolf is one the
                # agent stops reading. Same line as
                # `handlers/delivery/bottle_collection.py:_handle_submit_failure`.
                await self._say(
                    update,
                    f"⚠️ {i18n.get('staff.sales.visit.order_maybe_landed', language)}",
                    SalesKeyboards.visit_resume(language),
                )
                context.user_data.pop(FLOW_KEY, None)
                return ConversationHandler.END
            await self._handle_api_response_error(update, response, language)
            if self._visit_is_gone(context, response):
                return ConversationHandler.END
            if error_code in BASKET_ERROR_CODES:
                # The quote and the charge are two calls and only the second
                # one bills: an admin can move the floor or the ceiling
                # between them. Offering Send again on a basket the backend
                # has already refused is the one move that cannot work.
                return await self._render_order_edit(update, flow, language)
            if error_code == DATE_ERROR_CODE:
                # The day is the only field of this payload the confirm card
                # cannot change, so it is the only thing re-asked: the basket,
                # the rail and the note are all still on the draft, and one
                # answer re-sends. `date_redo` is what makes that true --
                # without it the answer walks on to the note prompt, whose
                # Skip writes `delivery_notes = None` and deletes the
                # instruction the agent had already typed.
                flow['date_redo'] = True
                return await self._ask_day(update, language)
            if error_code != 'SALES_VISIT_ORDER_EXISTS':
                # The alert has already said why, and the confirm card is
                # unchanged — the basket and the cached quote both survived
                # the refusal — so a redraw would be byte-identical and
                # Telegram answers "message is not modified". Both moves are
                # still live on the screen that is already there: send again,
                # or go back and change it. (`stock_retry` and `_stay` draw
                # the same line.)
                return V_CONFIRM
            # The order IS placed -- by an earlier tap of this same button.
            # Offering the send again would invite a third one.
            flow.setdefault('close', {})['has_order'] = True
            existing = self._existing_order(response)
            if existing:
                await self._say(update, self._order_result_text(existing, '', language))
                return await self._render_close(update, context, flow, language, force_new=True)
            return await self._render_close(update, context, flow, language)
        data = response.data or {}
        order = data.get('order') or {}
        state = ((data.get('confirmation') or {}).get('state')) or ''
        flow.setdefault('close', {})['has_order'] = True
        flow['step'] = ((data.get('visit') or {}).get('current_step')) or 'close'
        await self._say(update, self._order_result_text(order, state, language))
        return await self._render_close(update, context, flow, language, force_new=True)

    @require_auth
    @require_sales_agent
    async def order_back(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        return await self._render_order(update, context, flow, language)

    # ---- the close -------------------------------------------------------------------
    @staticmethod
    def _close_step(flow: Dict) -> str:
        """Which question the close screen is on.

        Both shortcuts are BACKEND facts rather than bot memory: an order
        exists (the backend stamps `order_placed` itself, so the bot posts
        `outcome: null`), or a no-order reason was already given (the reason
        IS the outcome). Either way the outcome prompt would be asking a
        question that is already answered.
        """
        close = flow.setdefault('close', {})
        step = close.get('step')
        if step in CLOSE_STEPS:
            return step
        return 'notes' if (close.get('has_order') or close.get('no_order_reason')) else 'outcome'

    async def _render_close(self, update, context, flow: Dict, language: str, *,
                            force_new: bool = False) -> int:
        """Every close screen, drawn from the flow alone.

        `force_new` is for the two-message hand-off after an order is placed:
        the confirm message becomes the receipt, and the close prompt is a
        new message under it rather than an edit that would erase it.
        """
        flow['step'] = 'close'
        close = flow.setdefault('close', {})
        step = self._close_step(flow)
        close['step'] = step
        if step == 'reason':
            text = i18n.get('staff.sales.visit.no_order_reason_prompt', language)
            keyboard = SalesKeyboards.no_order_reasons(language)
        elif step == 'notes':
            text = i18n.get('staff.sales.visit.close_notes_prompt', language)
            keyboard = SalesKeyboards.visit_skip(language, CLOSE_NOTES_STEP)
        elif step == 'next':
            text = i18n.get('staff.sales.visit.next_visit_prompt', language)
            keyboard = SalesKeyboards.next_visit(language)
        else:
            text = i18n.get('staff.sales.visit.close_outcome_prompt', language)
            keyboard = SalesKeyboards.close_outcome(language)
        await self._say(update, text, keyboard, force_new=force_new)
        return V_CLOSE

    @require_auth
    @require_sales_agent
    async def choose_outcome(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        outcome = (update.callback_query.data or '').split(OUTCOME_PREFIX, 1)[-1]
        if outcome not in CLOSE_OUTCOMES:
            # `order_placed` is deliberately not in CLOSE_OUTCOMES: an agent
            # may not claim an order the backend has no row for.
            return await self._stay(update, V_CLOSE)
        close = flow.setdefault('close', {})
        close['outcome'] = outcome
        close['step'] = 'reason' if (outcome == 'no_order' and not close.get('no_order_reason')) else 'notes'
        return await self._render_close(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def receive_close_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        close = flow.setdefault('close', {})
        if self._close_step(flow) != 'notes':
            # Text arrived while a different question was on screen; re-ask
            # the one that is actually open rather than filing the answer
            # under the wrong field.
            return await self._render_close(update, context, flow, language)
        message = update.effective_message
        text = ((message.text if message else '') or '').strip()[:MAX_NOTES]
        close['notes'] = text or None
        close['step'] = 'next'
        return await self._render_close(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def skip_close_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        step = (update.callback_query.data or '').split(SKIP_PREFIX, 1)[-1]
        if step != CLOSE_NOTES_STEP:
            return await self._stay(update, V_CLOSE)
        if self._close_step(flow) != 'notes':
            # The sibling guard in `receive_close_notes`, for the button
            # rather than the typing. Telegram keeps every old keyboard live,
            # so this Skip can arrive while the reason grid is up; taking it
            # as "no note" would move the close to the next-visit question
            # with the reason still missing, and `POST /close` then answers
            # SALES_VISIT_OUTCOME_REQUIRED to every tap after that. `_stay`
            # rather than a redraw: the grid is already on screen.
            return await self._stay(update, V_CLOSE)
        close = flow.setdefault('close', {})
        close['notes'] = None
        close['step'] = 'next'
        return await self._render_close(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def choose_next_visit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """When to come back -- and the last tap of the visit.

        'none' is an answer, not an omission: it clears the agent's date and
        hands the cadence back to the backend's own rule (D7).
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        choice = (update.callback_query.data or '').split(NEXT_PREFIX, 1)[-1]
        if choice not in NEXT_VISIT_CHOICES:
            return await self._stay(update, V_CLOSE)
        if self._close_step(flow) != 'next':
            # The sibling guard in `receive_close_notes` / `skip_close_notes`,
            # for the grid that ENDS the visit. Telegram keeps every old
            # keyboard live, so this tap can arrive while the outcome, the
            # reason or the note question is still on screen; taking it would
            # POST the close from an unanswered screen -- an ordered visit
            # filed with no note and a next-visit date meant for another shop,
            # or an orderless one refused with SALES_VISIT_OUTCOME_REQUIRED.
            # `_stay` rather than a redraw: the real question is already up.
            return await self._stay(update, V_CLOSE)
        next_visit_at = None if choice == 'none' else (date.today() + timedelta(days=int(choice))).isoformat()
        return await self._post_close(update, context, flow, language, next_visit_at)

    def _closed_text(self, outlet: Dict, language: str) -> str:
        """The visit's last screen.

        `{next_due}` is the BACKEND's recomputed due date (the close reply's
        outlet card), not the date just picked: a predicted stock-out can pull
        the next visit earlier than the agent's own answer, and the agent has
        to leave the shop knowing which one won.
        """
        return i18n.get('staff.sales.visit.closed', language,
                        next_due=format_local_date(outlet.get('next_visit_due_at')) or '—')

    async def _leave_to_menu(self, update, context, text: str, language: str) -> int:
        """End the conversation with the agent's MAIN MENU back under their thumb.

        Every way out of a visit goes through here: the close, Abandon and
        "Back to main". The check-in screen replaced the persistent menu with
        a one-shot location prompt (`_render_checkin`), and a reply keyboard
        cannot ride on an edit — so leaving has to be a NEW message carrying
        the menu, exactly as `_after_checkin` does on the way in.

        The tapped message loses its buttons first: they all post to a visit
        this conversation is no longer holding.
        """
        query = update.callback_query
        if query:
            await self._safe_callback_answer(query, None, show_alert=False)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except TelegramError as error:
                # `TelegramError`, not `BadRequest`: it is what this module
                # already imports and what `skip_checkin` swallows for the
                # same "the keyboard is already gone" case. A name this module
                # does not import would be a NameError on the SUCCESS path of
                # every visit that ends.
                logger.debug("Could not clear the visit keyboard: %s", error)
        await update.effective_message.reply_text(
            text, reply_markup=self._main_menu(context, language),
            parse_mode='HTML', disable_notification=True,
        )
        return ConversationHandler.END

    async def _post_close(self, update, context, flow: Dict, language: str,
                          next_visit_at: Optional[str]) -> int:
        visit_id = self._current_visit_id(flow)
        if visit_id is None:
            return await self._resume(update, context, language)
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        close = flow.get('close') or {}
        payload = {
            # None when the visit has an order: `VisitService.close` stamps
            # `order_placed` from the row itself, and a bot that sent the word
            # would be a second place deciding it.
            'outcome': close.get('outcome'),
            'no_order_reason': close.get('no_order_reason'),
            'notes': close.get('notes'),
            'next_visit_at': next_visit_at,
            # Phase 2b asks who was present; the field travels as null so the
            # payload shape does not change when it starts being answered.
            'dm_present': None,
        }
        async with api_client as client:
            response = await client.sales_close_visit(token, visit_id, payload)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            if self._visit_is_gone(context, response):
                return ConversationHandler.END
            return V_CLOSE
        outlet = (response.data or {}).get('outlet') or {}
        context.user_data.pop(FLOW_KEY, None)
        return await self._leave_to_menu(update, context, self._closed_text(outlet, language), language)

    # ---- photos ------------------------------------------------------------------
    @staticmethod
    def _is_forwarded(message) -> bool:
        """Telegram's two forward markers, new shape and old.

        `forward_origin` is Bot API 7.0; `forward_date` is what an older
        client still sets. Either means the picture was taken somewhere
        else -- which is the one thing the duplicate hash can never catch,
        because it only ever sees a REPEAT, not a photo that was never of
        this shop.
        """
        return bool(
            getattr(message, 'forward_origin', None) or getattr(message, 'forward_date', None)
        )

    @require_auth
    @require_sales_agent
    async def receive_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A photo, at any step of the visit.

        Registered with `filters.PHOTO` in every state, and it returns None
        from every in-visit path: PTB reads that as "state unchanged", so the
        step screen the agent is standing on keeps its buttons and its place.
        A picture is an annotation, never a move through the loop.

        The bytes are downloaded here only to be fingerprinted, then dropped:
        the backend keeps this bot's `file_id` and the SHA-256 (D27), and the
        picture stays on Telegram. `file_unique_id` rides along as the stable
        reference.

        A BURST queues. Telegram delivers a media group as separate updates,
        and one picker per photo would leave two dead keyboards behind and
        file the last picture three times.

        A CAPTION is deliberately DISCARDED. `filters.PHOTO` matches a
        captioned photo in every state, V_NOTES included, and in V_NOTES the
        text handler beside it is what files the order note -- so a captioned
        photo sent there would otherwise have to be two things at once. The
        ruling: a photo is a photo. Nothing in 2b reads
        `message.caption`, the visit note keeps its single, explicit screen,
        and an agent who types under a picture is not silently filing a note
        they cannot see afterwards. Pinned by
        `test_a_caption_is_not_a_visit_note`; revisit only by giving the
        caption a VISIBLE destination, never by quietly saving it.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        message = update.effective_message
        if self._is_forwarded(message):
            await self._notify_user(
                update, i18n.get('staff.sales.visit.photo_forwarded', language), show_alert=True
            )
            return None
        visit_id = self._current_visit_id(flow)
        if not visit_id:
            # A draft with no id is a restart or a second device. Re-read the
            # server and draw its step, exactly as every other write path
            # does, rather than posting this photo to `None`.
            return await self._resume(update, context, language)
        sizes = message.photo or []
        if not sizes:
            return None
        # Telegram sends the sizes ascending, so the last is the largest the
        # agent's client uploaded.
        size = sizes[-1]
        try:
            telegram_file = await context.bot.get_file(size.file_id)
            payload = await telegram_file.download_as_bytearray()
        except TelegramError as error:
            logger.warning("Could not download a visit photo: %s", error)
            await self._notify_user(
                update, i18n.get('staff.sales.visit.photo_failed', language), show_alert=True
            )
            return None
        queue = flow.setdefault('pending_photo', [])
        queue.append({
            'file_id': size.file_id,
            'file_unique_id': size.file_unique_id,
            # The digest, not the id, is what the duplicate rule compares: Telegram
            # mints a new file id for every upload of the same picture.
            'sha256': hashlib.sha256(bytes(payload)).hexdigest(),
        })
        if len(queue) > 1:
            # The picker drawn for the first photo of the burst is still on
            # screen and still live; a second one would file the queue twice.
            return None
        await self._say(
            update,
            '\n'.join(line for line in (
                self._header(flow),
                f"📷 {i18n.get('staff.sales.visit.photo_kind_prompt', language)}",
            ) if line),
            SalesKeyboards.photo_kind(language),
            force_new=True,
        )
        return None

    @require_auth
    @require_sales_agent
    async def choose_photo_kind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """File everything queued under the kind the agent tapped.

        The queue is drained BEFORE the posts but AFTER the token is in hand:
        a refusal by the BACKEND loses the picture rather than looping
        (`SALES_PHOTO_INVALID` is a verdict on the FILE, and a retry would
        re-post the same bytes for ever), but an expired session is not a
        verdict on anything -- popping first would throw away a whole burst on
        the way to a login screen, and the agent's only recovery would be to
        photograph the shelf again.

        `is_duplicate` is read, never computed -- the backend compares this
        agent's digests (D27).
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        data = (update.callback_query.data if update.callback_query else '') or ''
        kind = data.split(PHOTO_PREFIX, 1)[-1]
        if kind not in PHOTO_KINDS:
            # None, not a state: the registered pattern is a `\w+` shape, and
            # anything else must leave the agent's step exactly where it is.
            return await self._stay(update, None)
        if not flow.get('pending_photo'):
            # The picker outlived its photos -- a second tap on a keyboard
            # whose queue was already filed. Saying so beats re-posting bytes
            # the backend already has and flagging them as duplicates of
            # themselves.
            await self._notify_user(
                update, i18n.get('staff.sales.visit.photo_failed', language), show_alert=True
            )
            return None
        token = await self._get_auth_token(update, context)
        if not token:
            # Checked BEFORE the queue is drained: an expired session is not a
            # verdict on the photograph, and the agent must still be holding
            # the burst when they come back.
            return await self._session_gone(update, context, language)
        queue = flow.pop('pending_photo', None) or []
        visit_id = self._current_visit_id(flow)
        saved = duplicates = 0
        failure = None
        gone = False
        async with api_client as client:
            for item in queue:
                response = await client.sales_add_photo(
                    token, visit_id, item['file_id'], item['file_unique_id'], item['sha256'], kind,
                )
                if not response.success:
                    failure = response
                    continue
                saved += 1
                if ((response.data or {}).get('photo') or {}).get('is_duplicate'):
                    duplicates += 1
        lines = [self._header(flow)]
        if saved:
            lines.append(f"📷 {i18n.get('staff.sales.visit.photo_saved', language)}")
        if duplicates:
            lines.append(f"♻️ {i18n.get('staff.sales.visit.photo_duplicate', language)}")
        if failure is not None:
            # A visit that closed under the agent invalidates the draft, and
            # it must mean the same thing here as it does on the order and
            # close paths -- one helper, one meaning, INCLUDING the leaving:
            # staying would park the agent on a screen whose every button posts
            # to a visit that is over, with no draft behind any of them.
            gone = self._visit_is_gone(context, failure)
            reason = self._resolve_response_error(language, failure, html=True)
            lines.append(f"⚠️ {reason}")
        await self._say(update, '\n'.join(line for line in lines if line))
        if gone:
            return ConversationHandler.END
        if saved and flow.pop('photo_prompt', False):
            # D26: the prompt has its answer; the visit moves on to the shelf.
            # Its Skip goes first: left live above the shelf, a tap on it from a
            # later step reaches `stale_tap`, which re-seeds the draft and drops
            # the counts or the basket not yet sent.
            prompt_id = flow.pop('photo_prompt_message_id', None)
            if prompt_id:
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=update.effective_chat.id, message_id=prompt_id, reply_markup=None,
                    )
                except TelegramError as error:
                    logger.debug("Could not clear the photo prompt keyboard: %s", error)
            return await self._render_stock(update, context, flow, language, edit=False)
        return None

    @require_auth
    @require_sales_agent
    async def skip_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """⏭ on the one-time photo prompt (D26): straight on to the shelf.

        Also registered on the shelf screen, where it can only be the prompt's
        LEFTOVER button -- a photo already answered the prompt and moved the visit
        on, and `choose_photo_kind` failed to clear it. There it clears its own
        keyboard and nothing else: unregistered, the tap would reach `stale_tap`,
        which re-reads the visit and drops shelf counts the agent has typed but
        not yet sent.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if not flow.pop('photo_prompt', False):
            await self._ack(update, V_STOCK)
            try:
                await update.callback_query.edit_message_reply_markup(reply_markup=None)
            except TelegramError as error:
                logger.debug("Could not clear the photo prompt keyboard: %s", error)
            return V_STOCK
        # The shelf is drawn over the prompt itself, so there is no keyboard left to find.
        flow.pop('photo_prompt_message_id', None)
        return await self._render_stock(update, context, flow, language)

    # ---- leaving -----------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def abandon(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The agent's own way out of a visit, from any screen inside it.

        It is the only tap that ends a visit the agent did not close -- but
        no longer always WITHOUT an outcome: a visit that already carries an
        order is CLOSED by the server instead (ruling 74), because an order
        is the outcome and `close` is the only thing that republishes the
        shop's next-visit date. Which of the two happened is the SERVER's
        answer, read off `visit.status`; the flow cannot tell, because the
        same button posts from the basket and from the close screen alike.

        Returns None on failure, which PTB reads as "state unchanged" -- the
        agent stays on whichever screen they tapped from rather than being
        teleported to a fixed one.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        token = await self._get_auth_token(update, context)
        if not token:
            return await self._session_gone(update, context, language)
        async with api_client as client:
            response = await client.sales_abandon_visit(token, flow.get('visit_id'))
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            gone = self._visit_is_gone(context, response)
            if gone:
                # Abandon posts from EVERY screen, including check-in's own
                # one-shot location keyboard (its inline Skip/Abandon pair).
                # Staying there with `return None` would leave the agent
                # holding a dead draft AND a reply keyboard with no menu on
                # it -- the same trap `receive_checkin`/`skip_checkin` fix.
                return await self._leave_to_menu(
                    update, context, self._resolve_response_error(language, response, html=True), language
                )
            return None
        data = response.data or {}
        visit = data.get('visit') or {}
        context.user_data.pop(FLOW_KEY, None)
        if visit.get('status') == 'completed':
            # The server closed it rather than abandoning it (ruling 74). The agent leaves the
            # shop with the same receipt a deliberate close prints -- the recomputed next-visit
            # date included -- instead of being told the call that placed an order was thrown
            # away. The reply carries the outlet card for exactly this.
            return await self._leave_to_menu(
                update, context, self._closed_text(data.get('outlet') or {}, language), language
            )
        return await self._leave_to_menu(
            update, context, f"🛑 {i18n.get('staff.sales.visit.abandoned', language)}", language
        )

    # Private on purpose (no `@require_auth`): PTB fires TIMEOUT handlers itself with the
    # agent's LAST update, so an auth bounce here would swallow the timeout copy and leave
    # `sales_visit` in user_data — the same exemption `_flow_timeout` relies on.
    async def _timed_out(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The visit conversation's own five-minute expiry.

        The shared `_flow_timeout` copy says the flow was closed and "nothing
        was saved". For a visit that is FALSE and dangerously so: a timeout
        does not touch it. The server still holds it open, the check-in and the
        counts are already recorded, and Resume walks back into it. An agent
        told their visit was thrown away opens a second one, which
        `uq_visits_one_open_per_agent` refuses -- leaving them outside a visit
        they cannot reopen, holding a phone that says it is gone.

        The DRAFT is still dropped, through the same SSOT every other way out
        uses: the conversation really has ended, and its keys are stale the
        moment it does.

        `@require_auth` and not `@require_sales_agent`: SEC-002 requires a
        guard on every handler under `staff_bot/handlers/` (the shared
        `_flow_timeout` escapes the scan only because it is a closure in
        bot.py), and of the two this is the one that does not replace the
        message with "unauthorized" for an agent whose role was revoked while
        they were standing in a shop -- their flow really did end, and that is
        what they need told. Everything else is inside the try for the same
        reason: PTB dispatches TIMEOUT handlers itself, and a raise here is
        silence.
        """
        try:
            language = await self._get_language(update, context)
            await flow_state.clear_pending_flows(context, update)
            text = i18n.get('staff.sales.visit.timeout', language)
            keyboard = SalesKeyboards.visit_resume(language)
            # The timeout update is the agent's LAST real one, so the reply
            # target is derived rather than assumed: a flow abandoned on a
            # button times out on a callback query, one abandoned at a text
            # prompt on a message.
            query = update.callback_query
            target = query.message if query is not None else update.effective_message
            if target is not None:
                await target.reply_text(text, reply_markup=keyboard, disable_notification=True)
            elif update.effective_user is not None:
                await context.bot.send_message(
                    chat_id=update.effective_user.id, text=text,
                    reply_markup=keyboard, disable_notification=True,
                )
        except Exception as exc:  # noqa: BLE001 -- never surface to the agent
            logger.error("Failed to announce the visit timeout: %s", exc, exc_info=True)
        return ConversationHandler.END

    @require_auth
    @require_sales_agent
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/cancel and Back to main: LEAVE the conversation, never abandon.

        The visit is server-side state. An agent who steps out to check their
        cash hub, or whose flow times out, must come back to the same open
        visit -- ending it here would silently throw away a check-in that
        really happened. Only `staff_sales_v_abandon` writes.
        """
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        return await self._leave_to_menu(
            update, context, i18n.get('staff.cancelled', language), language
        )
