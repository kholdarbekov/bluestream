"""Try-out from the field: pick products and quantities, note, confirm, POST.

One working draft lives under user_data['sales_tryout'] (registered in
flow_state, so a menu tap, /start or the timeout clears it). Unlike the visit,
NOTHING exists server-side until Confirm -- so leaving really is leaving, and
the shared `staff.flow_timed_out` copy ("nothing was saved") is true here.

Nothing in this file re-decides a backend rule. Who the contact is, which
address the try-out goes to, whether the outlet's stage moves to `trial` and
whether a product may be lent at all are all `POST /outlets/<id>/tryouts`'s
answers; this file renders them.
"""
import logging
from typing import Dict, List, Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.api_client import api_client
from staff_bot.handlers.sales.hub import SalesHubHandler, format_outlet_card
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.sales import TRYOUT_QTY_CHOICES, SalesKeyboards
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils.formatters import escape_html

logger = logging.getLogger(__name__)

T_PRODUCTS, T_QTY, T_NOTES, T_CONFIRM = range(322, 326)
FLOW_KEY = 'sales_tryout'

MAX_NOTES = 500
PRODUCT_PREFIX = 'staff_sales_t_p_'
QTY_PREFIX = 'staff_sales_t_qty_'

# A try-out for an outlet with no contact phone. `TryoutService` upserts the
# trial contact BY PHONE, so there is nothing to attach the loan to -- and the
# remedy is on the outlet CARD, not on this screen, which is why this one ends
# the conversation and points there instead of leaving a Confirm button whose
# only possible outcome is the same refusal.
PHONE_ERROR_CODE = 'SALES_TRYOUT_PHONE_REQUIRED'
# An empty basket, or a product the backend will not lend. The basket is the
# only screen a quantity can be changed on, so that is where this lands.
ITEMS_ERROR_CODE = 'SALES_TRYOUT_ITEMS_INVALID'


def tryout_eligible(product: Dict) -> bool:
    """Is this `serialize_product` row lendable?

    Reads the two booleans the backend PUBLISHES and nothing else. It decides
    what is OFFERED; `TryoutService._validate_and_build_items` is what refuses,
    and the two are allowed to disagree for exactly as long as it takes an
    admin's edit to reach the next fetch -- the same arrangement
    `keyboards/sales.py::VISITABLE_STAGES` documents.

    Both flags live under their serializer's OWN section
    (`business_app/serializers/product_serializers.py`: `flags.is_active`,
    `inventory.is_tryout_eligible`). Read at the TOP level they are always
    absent, and a `.get(key, True)` default then waves every product through --
    which is a filter that filters nothing.
    """
    flags = product.get('flags') or {}
    inventory = product.get('inventory') or {}
    return flags.get('is_active') is not False and inventory.get('is_tryout_eligible') is not False


class TryoutFromFieldHandler(SalesHubHandler):
    """Subclasses the hub so a finished try-out can fall back onto the outlet card."""

    # ---- helpers -----------------------------------------------------------------
    async def _flow(self, update, context) -> Optional[Dict]:
        return await self._require_flow(update, context, FLOW_KEY)

    async def _say(self, update: Update, text: str, keyboard=None) -> None:
        """Edit the screen the agent is looking at, or send a new message.

        Through `_safe_callback_answer`, never a bare `answer()`: the refusal
        paths have ALREADY answered this query (the API error is shown as an
        alert first), and a second answer is exactly what Telegram rejects with
        "query is too old or invalid" -- raised here it would escape before the
        EDIT and leave the agent on a screen whose buttons no longer mean
        anything.
        """
        query = update.callback_query
        if query:
            await self._safe_callback_answer(query, None, show_alert=False)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode='HTML', disable_notification=True
        )

    async def _stay(self, update: Update, state: int) -> int:
        """A callback whose payload does not belong to the step the agent is on.

        The registered patterns are `\\d+` shapes, so the VALUE is re-checked in
        each handler and anything else lands here: the tap is answered so the
        button stops spinning, and the same state is returned, which leaves the
        prompt and its keyboard where they are. Deliberately not re-rendering --
        the copy would be identical and Telegram answers "message is not
        modified", so a redraw is noise, not a re-prompt.
        """
        query = update.callback_query
        logger.info("Unroutable try-out callback %s in state %s", query.data if query else None, state)
        if query:
            await self._safe_callback_answer(query, None, show_alert=False)
        return state

    @staticmethod
    def _rows(flow: Dict) -> List[Dict]:
        """`SalesKeyboards.tryout_products` rows, in the catalogue's own order."""
        items = flow.get('items') or {}
        return [
            {
                'product_id': product.get('id'),
                'product_name': product.get('name') or '',
                'quantity': int(items.get(str(product.get('id'))) or 0),
            }
            for product in flow.get('products') or []
        ]

    @classmethod
    def _basket(cls, flow: Dict) -> List[Dict]:
        return [row for row in cls._rows(flow) if row['quantity'] > 0]

    @staticmethod
    def _knows(flow: Dict, product_id: int) -> bool:
        return any(product.get('id') == product_id for product in flow.get('products') or [])

    @staticmethod
    def _name(flow: Dict, product_id: int) -> str:
        for product in flow.get('products') or []:
            if product.get('id') == product_id:
                return product.get('name') or ''
        return ''

    @staticmethod
    def _header(flow: Dict) -> str:
        name = flow.get('outlet_name')
        return f"🏪 <b>{escape_html(name)}</b>" if name else ''

    # ---- screens -----------------------------------------------------------------
    async def _render_products(self, update: Update, flow: Dict, language: str) -> int:
        lines = [
            self._header(flow),
            f"🧪 <b>{i18n.get('staff.sales.tryout.choose_products', language)}</b>",
        ]
        for row in self._basket(flow):
            # Composed, not interpolated (ruling 19): the label carries no
            # placeholder in any language.
            lines.append(f"• {escape_html(row['product_name'])} × {row['quantity']}")
        await self._say(
            update,
            '\n'.join(line for line in lines if line),
            SalesKeyboards.tryout_products(language, self._rows(flow)),
        )
        return T_PRODUCTS

    def _confirm_text(self, flow: Dict, language: str) -> str:
        lines = [
            f"<b>{i18n.get('staff.sales.tryout.confirm_title', language)}</b>",
            self._header(flow),
        ]
        for row in self._basket(flow):
            lines.append(f"• {escape_html(row['product_name'])} × {row['quantity']}")
        if flow.get('notes'):
            lines.append(f"📝 {escape_html(flow['notes'])}")
        return '\n'.join(line for line in lines if line)

    async def _render_confirm(self, update: Update, flow: Dict, language: str) -> int:
        await self._say(update, self._confirm_text(flow, language),
                        SalesKeyboards.tryout_confirm(language))
        return T_CONFIRM

    def _receipt(self, tryout: Dict, outlet: Dict, language: str) -> str:
        """What was really lent, and the shop as it now stands — both off the 201 reply.

        The ITEMS come back from `serialize_tryout`, never from the draft: the
        backend prices and validates each line, and echoing the request here is
        how an agent tells a shopkeeper about a bottle that never left the van.

        The CARD comes from the same reply's `outlet` block. This route is the
        one place outside the visit loop that moves the stage, and the reply
        carries the moved card exactly as `close`/`abandon` do -- so the receipt
        shows `trial` without a second GET, and the agent's next action is on
        the screen already in front of them rather than one tap away. The
        precedent is `SalesHubHandler.request_activation`, which renders its own
        confirmation line above `format_outlet_card(outlet, language)` from the
        reply it just received.
        """
        lines = [i18n.get('staff.sales.tryout.created', language,
                          tryout_number=escape_html(tryout.get('tryout_number') or ''))]
        for item in tryout.get('items') or []:
            lines.append(
                f"• {escape_html(item.get('product_name') or '')} × {item.get('quantity') or 0}"
            )
        lines.append(i18n.get('staff.sales.tryout.handoff_queued', language))
        if outlet:
            lines.append('')
            lines.append(format_outlet_card(outlet, language))
        return '\n'.join(line for line in lines if line is not None)

    # ---- entry -------------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def start_tryout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """🧪 Try-out on the outlet card.

        Two reads before a single button is drawn, and both are load-bearing:
        `GET /outlets/<id>` is the OWNERSHIP proof (a 403/404 is an alert and
        the conversation never opens) and the only source of the outlet's name,
        and the catalogue decides whether there is anything to lend at all.
        Neither is cached: a SKU switched off in the admin UI is gone from the
        next screen an agent opens, with no bot deploy.

        The open-visit guard comes FIRST, for the reason
        `_refused_by_open_visit` records: this is an entry point, so a 🧪 left
        on an outlet card from before the agent walked into a visit would
        otherwise open a basket ON TOP of the live visit.
        """
        if await self._refused_by_open_visit(update, context):
            return ConversationHandler.END
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        # Unconditionally, and BEFORE the three early returns below (no token, a
        # refused outlet, a refused or empty catalogue). Entering this point at
        # all means the previous basket is over: leaving it behind on a path
        # that renders no basket is how an agent's next Confirm sends products
        # they picked for a different shop. It sits after `_refused_by_open_visit`
        # on purpose -- that guard refuses the tap outright and must change
        # nothing.
        context.user_data.pop(FLOW_KEY, None)
        self._drop_other_sales_drafts(context, FLOW_KEY)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        async with api_client as client:
            outlet_response = await client.sales_get_outlet(token, outlet_id)
        if not outlet_response.success:
            await self._handle_api_response_error(update, outlet_response, language)
            return ConversationHandler.END
        async with api_client as client:
            products_response = await client.sales_tryout_products(token)
        if not products_response.success:
            await self._handle_api_response_error(update, products_response, language)
            return ConversationHandler.END
        products = [
            {'id': item.get('id'), 'name': item.get('name') or ''}
            for item in ((products_response.data or {}).get('items') or [])
            if item.get('id') is not None and tryout_eligible(item)
        ]
        if not products:
            # An empty grid with a live Continue would post a basket the backend
            # refuses. The card the agent tapped from is still on screen with
            # every other move on it, so the alert is the whole answer.
            await self._notify_user(
                update, i18n.get('staff.sales.tryout.no_products', language), show_alert=True
            )
            return ConversationHandler.END
        outlet = (outlet_response.data or {}).get('outlet') or {}
        flow = {
            'outlet_id': outlet_id,
            'outlet_name': outlet.get('name') or '',
            'products': products,
            'items': {},
            'notes': None,
        }
        context.user_data[FLOW_KEY] = flow
        return await self._render_products(update, flow, language)

    @require_auth
    @require_sales_agent
    async def stale_tap(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Any basket button tapped when the conversation is no longer holding it.

        PTB keeps conversation state in memory, so a bot RESTART empties it while
        every screen an agent is looking at keeps its buttons. Those taps matched
        no handler at all: the button spun and nothing happened.

        Registered as the LAST entry point, the `stale_visit_tap` shape in
        bot.py -- but this flow has nothing to re-read. NOTHING exists
        server-side until Confirm, so "nothing was saved" is simply true, and
        the outlet card the agent came from is still on the screen above with
        every move on it.
        """
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        await self._notify_user(update, i18n.get('staff.flow_timed_out', language), show_alert=True)
        return ConversationHandler.END

    # ---- the basket --------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def open_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        suffix = (update.callback_query.data or '').split(PRODUCT_PREFIX, 1)[-1]
        if not suffix.isdigit() or not self._knows(flow, int(suffix)):
            return await self._stay(update, T_PRODUCTS)
        product_id = int(suffix)
        current = int((flow.get('items') or {}).get(str(product_id)) or 0)
        text = (
            f"{i18n.get('staff.sales.tryout.quantity_prompt', language)}\n"
            f"• {escape_html(self._name(flow, product_id))} × {current}"
        )
        await self._say(update, text, SalesKeyboards.tryout_quantity(language, product_id, current))
        return T_QTY

    @require_auth
    @require_sales_agent
    async def set_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        parts = (update.callback_query.data or '').split(QTY_PREFIX, 1)[-1].split('_')
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return await self._stay(update, T_QTY)
        product_id, quantity = int(parts[0]), int(parts[1])
        # TRYOUT_QTY_CHOICES is the grid `SalesKeyboards.tryout_quantity` draws;
        # the registered pattern is `\\d+_\\d+`, wider than the grid, so the
        # value is checked HERE against the same tuple the button came from.
        if quantity not in TRYOUT_QTY_CHOICES or not self._knows(flow, product_id):
            return await self._stay(update, T_QTY)
        flow.setdefault('items', {})[str(product_id)] = quantity
        return await self._render_products(update, flow, language)

    @require_auth
    @require_sales_agent
    async def back_to_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """One button, two states: Back from the grid and Back from the confirm
        card both mean the basket, which is the only screen a quantity can
        change on."""
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        return await self._render_products(update, flow, language)

    @require_auth
    @require_sales_agent
    async def products_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if not self._basket(flow):
            # Continue is not drawn on an empty basket, so this is a tap from an
            # older screen.
            return await self._stay(update, T_PRODUCTS)
        await self._say(update, i18n.get('staff.sales.tryout.notes_prompt', language),
                        SalesKeyboards.tryout_notes(language))
        return T_NOTES

    # ---- note and confirm --------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def receive_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        message = update.effective_message
        flow['notes'] = ((message.text if message else '') or '').strip()[:MAX_NOTES] or None
        return await self._render_confirm(update, flow, language)

    @require_auth
    @require_sales_agent
    async def skip_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        flow['notes'] = None
        return await self._render_confirm(update, flow, language)

    @require_auth
    @require_sales_agent
    async def confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send the try-out. The ONLY place this conversation writes anything.

        The body is exactly what `POST /outlets/<id>/tryouts` reads: no contact,
        no address, no source, no driver -- the backend builds the payload from
        the OUTLET and stamps the source from the token, and a bot that sent
        them would be a second place deciding who the loan is for.
        """
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        items = [{'product_id': row['product_id'], 'quantity': row['quantity']}
                 for row in self._basket(flow)]
        if not items:
            return await self._render_products(update, flow, language)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        outlet_id = flow.get('outlet_id')
        async with api_client as client:
            response = await client.sales_create_tryout(
                token, outlet_id, {'items': items, 'notes': flow.get('notes')}
            )
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            error_code = getattr(response, 'error_code', None)
            if error_code == PHONE_ERROR_CODE:
                # The remedy is a contact on the outlet card, so the flow ENDS
                # here: leaving it armed would keep a Confirm button live whose
                # only possible outcome is this same refusal, and the agent's
                # next tap would have to travel back out through the hub.
                context.user_data.pop(FLOW_KEY, None)
                await self._say(update, i18n.get('staff.sales.tryout.no_phone', language),
                                SalesKeyboards.tryout_outlet_link(language, outlet_id))
                return ConversationHandler.END
            if error_code == ITEMS_ERROR_CODE:
                return await self._render_products(update, flow, language)
            # The alert has already said why, and the confirm card is unchanged
            # -- the basket and the note both survived -- so a redraw would be
            # byte-identical and Telegram answers "message is not modified".
            # Both moves are still live on the screen that is already there.
            return T_CONFIRM
        data = response.data or {}
        tryout = data.get('tryout') or {}
        # The reply's own card, already at `trial`. Falling back to the link
        # keyboard when the block is somehow absent keeps the receipt useful
        # rather than dead-ended — but the backend contract (Task 5) publishes
        # it on every 201, and the journey pins that.
        outlet = data.get('outlet') or {}
        context.user_data.pop(FLOW_KEY, None)
        await self._say(
            update,
            self._receipt(tryout, outlet, language),
            SalesKeyboards.outlet_card(language, outlet) if outlet
            else SalesKeyboards.tryout_outlet_link(language, outlet_id),
        )
        return ConversationHandler.END

    # ---- leaving -----------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/cancel and Back to main. Nothing exists server-side yet, so this
        really does throw the basket away -- which is what the shared
        `staff.cancelled` copy says."""
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        await self._say(update, i18n.get('staff.cancelled', language),
                        CommonKeyboards.back_button(language))
        return ConversationHandler.END
