"""The outlet card's 📞 Add phone / Change phone: one question, one PUT (D31).

The prompt takes the number as text, checks it with the shared phone rule
(`staff_bot.utils.validators.validate_phone`, i.e. `shared.validators`) and
sends the normalised E.164 value to `PUT /outlets/<id>/primary-phone`. The
working draft is `{'outlet_id': ...}` under `SALES_SET_PHONE_FLOW_KEY`
(registered in flow_state, so a menu tap, /start or the timeout clears it).
Nothing is written until a valid number is sent.

Nothing here re-decides a backend rule. Whether the phone may be set at all is
the card's published `can_set_phone`; which contact the number lands on, and
whether the outlet may then request activation, are the PUT's answers, and the
screen renders the card that reply carries.
"""
import logging
from typing import Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.api_client import api_client
from staff_bot.handlers.sales.hub import SalesHubHandler, format_outlet_card
from staff_bot.i18n import i18n
from staff_bot.keyboards.sales import SalesKeyboards
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils.flow_state import SALES_SET_PHONE_FLOW_KEY
from staff_bot.utils.validators import validate_phone

logger = logging.getLogger(__name__)

# 300-326 are taken: new outlet 300-307, visit 310-319 and 326, Nearby 320-321,
# try-out 322-325.
SP_PHONE = 327
FLOW_KEY = SALES_SET_PHONE_FLOW_KEY

# The backend refusing the NUMBER (`OutletService._require_primary_phone`). The
# outlet is still there to be fixed, so the agent stays on the prompt and types
# it again; any other refusal means the outlet itself moved on, and ends it.
NUMBER_ERROR_CODES = ('SALES_CONTACT_PHONE_INVALID', 'SALES_OUTLET_PHONE_REQUIRED')


def _stays_on_prompt(response) -> bool:
    """Does this failed PUT leave the agent on the prompt, draft and all?

    Yes for a refusal of the number, and for a failure that refused nothing: no
    HTTP status (the client's retries ran out), a 429 or a 5xx. None of those
    says the outlet moved on, so the agent retypes and the PUT, which is
    idempotent, is simply sent again. Only an answer about the outlet itself (a
    400 for its stage, a 403 or 404) ends the screen.
    """
    status = getattr(response, 'status_code', None)
    return (
        getattr(response, 'error_code', None) in NUMBER_ERROR_CODES
        or status is None or status == 429 or status >= 500
    )


class SetPhoneHandler(SalesHubHandler):
    """Subclasses the hub so every way off the screen can land on the outlet card."""

    # ---- helpers -----------------------------------------------------------------
    async def _flow(self, update, context) -> Optional[Dict]:
        return await self._require_flow(update, context, FLOW_KEY)

    async def _say(self, update: Update, text: str, keyboard=None) -> None:
        """Edit the tapped screen, or reply to the agent's message.

        The try-out's `_say`: the tap is answered through
        `_safe_callback_answer`, and a reply goes to `effective_message`. The
        try-out needs the latter because its notes state also receives EDITED
        messages; this screen's text state takes new messages only (bot.py).
        """
        query = update.callback_query
        if query:
            await self._safe_callback_answer(query, None, show_alert=False)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode='HTML', disable_notification=True
        )

    async def _end_on_card(self, update: Update, context, outlet_id: int, language: str) -> int:
        """Leave the screen and draw the outlet card (Cancel, an older card's row, a 400).

        The draft goes FIRST, so a card that fails to render still leaves no
        armed prompt behind to read the agent's next text as a phone number.
        """
        context.user_data.pop(FLOW_KEY, None)
        try:
            await self._show_card(update, context, outlet_id, language)
        except Exception as e:
            logger.error(f"Error showing outlet {outlet_id} after the phone screen: {e}", exc_info=True)
            await self._handle_error(update, context)
        return ConversationHandler.END

    # ---- entry -------------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📞 on the outlet card: ask for the number.

        No read first: the PUT is the ownership and stage check, and its
        refusal is rendered when it comes. The open-visit guard runs FIRST and
        changes nothing, for the reason `_refused_by_open_visit` records.
        """
        if await self._refused_by_open_visit(update, context):
            return ConversationHandler.END
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        self._drop_other_sales_drafts(context, FLOW_KEY)
        # Replaced, never merged: 📞 on another card while this prompt is open
        # (allow_reentry) aims the screen at that outlet instead.
        context.user_data[FLOW_KEY] = {'outlet_id': outlet_id}
        await self._say(update, i18n.get('staff.sales.phone.enter', language),
                        SalesKeyboards.phone_prompt(language))
        return SP_PHONE

    @require_auth
    @require_sales_agent
    async def stale_tap(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The prompt's Cancel tapped when the conversation no longer holds it.

        The screen has ended while the button stayed on it: after a save, the
        timeout, or a restart that emptied PTB's in-memory state. The try-out's
        "Nothing was saved" is false after the first, right under "✅ Phone
        saved.", so the answer is the one true in all three.
        """
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        await self._notify_user(update, i18n.get('staff.sales.phone.closed', language), show_alert=True)
        return ConversationHandler.END

    # ---- the number --------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def receive_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check the number with the shared rule, then PUT the normalised value."""
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        is_valid, phone = validate_phone((update.effective_message.text or '').strip())
        if not is_valid:
            await self._say(update, i18n.get('staff.sales.error.mobile_required', language),
                            SalesKeyboards.phone_prompt(language))
            return SP_PHONE
        token = await self._get_auth_token(update, context)
        if not token:
            context.user_data.pop(FLOW_KEY, None)
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        outlet_id = flow['outlet_id']
        async with api_client as client:
            response = await client.sales_set_primary_phone(token, outlet_id, phone)
        if not response.success:
            if _stays_on_prompt(response):
                text = f"❌ {self._resolve_response_error(language, response, html=True)}"
                await self._say(update, text, SalesKeyboards.phone_prompt(language))
                return SP_PHONE
            # The outlet moved on (a 400: no longer a prospect or on trial) or is
            # not this agent's (403/404), and the PUT wrote nothing. Only a 400
            # has a card worth drawing again -- the outlet is still theirs, at its
            # new stage -- while a GET after a 403/404 would only be refused too.
            context.user_data.pop(FLOW_KEY, None)
            await self._handle_api_response_error(update, response, language)
            if getattr(response, 'status_code', None) == 400:
                return await self._end_on_card(update, context, outlet_id, language)
            return ConversationHandler.END
        outlet = (response.data or {}).get('outlet') or {}
        context.user_data.pop(FLOW_KEY, None)
        await self._say(
            update,
            f"✅ {i18n.get('staff.sales.phone.saved', language)}\n\n{format_outlet_card(outlet, language)}",
            SalesKeyboards.outlet_card(language, outlet),
        )
        return ConversationHandler.END

    # ---- leaving -----------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The prompt's ❌ Cancel and /cancel: back to the card the 📞 was tapped on."""
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        return await self._end_on_card(update, context, flow['outlet_id'], language)

    @require_auth
    @require_sales_agent
    async def open_outlet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """An older card's `staff_sales_outlet_<id>` tapped while the prompt is open.

        Registered in SP_PHONE for `NearbyHandler.open_outlet`'s reason: the
        hub's group-0 handler would draw the card with this state still armed
        behind it, and the agent's next text would be saved as the phone of the
        outlet they walked away from.
        """
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        return await self._end_on_card(update, context, outlet_id, language)
