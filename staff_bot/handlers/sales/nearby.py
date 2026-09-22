"""Nearby: share a pin, get the outlets around you, nearest first.

Two states (320-321) and one working dict under user_data['sales_nearby']
(registered in flow_state, so a menu tap, /start or the timeout clears it).

Nothing here measures anything. `distance_m`, the ordering and how many rows
come back are the backend's (`OutletService.nearby`, `SALES_NEARBY_LIMIT`);
this asks with the agent's pin and draws what comes back, in the order it
comes back. The pin is not zone-checked either: an agent may legitimately be
standing across a boundary, and this is a READ — the new-outlet walk-in
refuses an out-of-zone pin because it is about to WRITE an address.
"""
import logging
from typing import Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.api_client import api_client
from staff_bot.handlers.sales.hub import SalesHubHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.sales import SalesKeyboards
from staff_bot.permissions import require_auth, require_sales_agent

logger = logging.getLogger(__name__)

NB_PIN, NB_LIST = range(320, 322)
FLOW_KEY = 'sales_nearby'


class NearbyHandler(SalesHubHandler):
    """Subclasses the hub so a tapped row opens the card the hub already draws."""

    async def _flow(self, update, context) -> Optional[Dict]:
        return await self._require_flow(update, context, FLOW_KEY)

    def _pin_prompt(self, language: str):
        return CommonKeyboards.location_request(
            language, i18n.get('staff.sales.nearby.pin_button', language), include_cancel=False
        )

    async def _ask_pin(self, update: Update, language: str) -> int:
        """The prompt message: text, plus the only keyboard that can ask for a pin."""
        await update.effective_message.reply_text(
            f"📍 {i18n.get('staff.sales.nearby.pin_prompt', language)}",
            reply_markup=self._pin_prompt(language), parse_mode='HTML', disable_notification=True,
        )
        return NB_PIN

    @require_auth
    @require_sales_agent
    async def start_nearby(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The hub's 🧭 Nearby, and the list's 📍 New pin — one screen for both.

        Two messages, because `request_location` lives only on a REPLY keyboard
        (`CommonKeyboards.location_request`) and a reply keyboard needs its own
        message. The first REPLACES the screen the tap came from — the hub, or
        the previous list — so its buttons cannot be tapped again behind an
        armed conversation.

        Registered as an entry point for BOTH patterns, so a 📍 New pin tapped
        after a bot restart (PTB keeps conversation state in memory) re-opens
        the prompt instead of spinning. The registered pattern is wider than the
        one literal the list draws and no suffix is re-checked, because every
        `staff_sales_nb_*` tap means exactly this: ask me for a pin. Nothing is
        written, so there is no value a wrong suffix could corrupt.

        The draft holds nothing. The pin is answered in one round trip and the
        rows come back with it, so there is no half-built record to keep (the
        new-outlet walk-in has one because it accumulates eight answers). It
        exists so `_require_flow` can tell "still in the conversation" from "a
        menu tap cleared it", and so `flow_state.clear_pending_flows` has a key
        to drop.
        """
        # BOTH entry literals, not just the list's: 🧭 Nearby left on a stale
        # hub message is the same hole one screen further back.
        if await self._refused_by_open_visit(update, context):
            return ConversationHandler.END
        language = await self._get_language(update, context)
        self._drop_other_sales_drafts(context, FLOW_KEY)
        context.user_data[FLOW_KEY] = {}
        await self._render(
            update,
            f"🧭 <b>{i18n.get('staff.sales.nearby.title', language)}</b>",
            SalesKeyboards.back_to_hub(language),
        )
        return await self._ask_pin(update, language)

    @require_auth
    @require_sales_agent
    async def receive_pin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The pin, and the list the backend builds from it."""
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        location = update.effective_message.location
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        async with api_client as client:
            response = await client.sales_list_outlets(
                token, scope='nearby', lat=location.latitude, lng=location.longitude
            )
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            # The one-shot location keyboard collapsed when the pin was sent;
            # without this the agent has no button left to retry with.
            return await self._ask_pin(update, language)
        items = (response.data or {}).get('items', [])
        # The location request replaced the agent's main menu with a one-shot
        # prompt keyboard: one message hands it back, the next draws the list,
        # because a message can carry only one keyboard (`_after_checkin`).
        await update.effective_message.reply_text(
            f"📍 {i18n.get('staff.sales.nearby.found', language)}",
            reply_markup=self._main_menu(context, language),
            parse_mode='HTML', disable_notification=True,
        )
        title = i18n.get('staff.sales.nearby.title', language)
        text = f"🧭 <b>{title}</b>"
        if not items:
            text = f"{text}\n{i18n.get('staff.sales.list.empty', language)}"
        await update.effective_message.reply_text(
            text, reply_markup=SalesKeyboards.nearby_list(language, items),
            parse_mode='HTML', disable_notification=True,
        )
        return NB_LIST

    @require_auth
    @require_sales_agent
    async def open_outlet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A row tapped on the list: leave the flow, open the card.

        Registered in NB_LIST even though the hub registers the same literal at
        group 0. The conversations get first refusal on every `staff_sales_*`
        tap, so without this the card would render from the hub's handler while
        THIS conversation stayed armed behind it for the rest of its five
        minutes — and its LOCATION state would then swallow the next pin the
        agent shared for anything else.
        """
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        try:
            await self._show_card(update, context, outlet_id, language)
        except Exception as e:
            logger.error(f"Error showing outlet {outlet_id} from nearby: {e}", exc_info=True)
            await self._handle_error(update, context)
        return ConversationHandler.END

    @require_auth
    @require_sales_agent
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`/cancel`, and the inline Back to main.

        Hands the MAIN MENU back rather than a bare confirmation: the pin
        prompt replaced it with a one-shot location keyboard, and a staff
        member whose control surface is gone has nothing left to tap.
        """
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        query = update.callback_query
        if query:
            await self._safe_callback_answer(query, None, show_alert=False)
        await update.effective_message.reply_text(
            i18n.get('staff.cancelled', language),
            reply_markup=self._main_menu(context, language), disable_notification=True,
        )
        return ConversationHandler.END
