"""Field onboarding of an outlet:
type → name → contact name → contact phone → pin (or typed address) → class → notes → dedupe → confirm.

One working dict lives under user_data['new_outlet'] (registered in flow_state so a
menu tap, /start or the timeout clears it). Every step re-validates the flow.
"""
import logging
from typing import Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from shared.constants import is_within_tashkent
from staff_bot.api_client import api_client
from staff_bot.handlers.sales.hub import SalesHubHandler, format_outlet_card
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.sales import SalesKeyboards
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils.api_errors import geocoder_down
from staff_bot.utils.formatters import escape_html
from staff_bot.utils.validators import validate_phone

logger = logging.getLogger(__name__)

NO_TYPE, NO_NAME, NO_CONTACT_NAME, NO_CONTACT_PHONE, NO_PIN, NO_CLASS, NO_NOTES, NO_CONFIRM = range(300, 308)
FLOW_KEY = 'new_outlet'

# The three `SalesOutlet.outlet_type` values the type keyboard draws and the
# only ones the backend accepts. Checked HERE rather than in the registered
# pattern: an alternation is invisible to the collision check in
# tests/staff_bot/test_staff_wiring_contract.py (it cannot sample a concrete
# callback_data out of one), which is the same reason the hub's list pattern
# is `\w+` — see the note in `bot.py`.
OUTLET_TYPES = ('grocery_store', 'workplace', 'individual')
OUTLET_CLASSES = ('A', 'B', 'C')

# What `_fetch_candidates` came back with. Three answers, because a failed
# lookup, an empty result and a dead session each need a different move.
LOOKUP_OK, LOOKUP_FAILED, LOOKUP_NO_TOKEN = 'ok', 'failed', 'no_token'


class NewOutletHandler(SalesHubHandler):
    # ---- helpers -----------------------------------------------------------------
    async def _flow(self, update, context) -> Optional[Dict]:
        return await self._require_flow(update, context, FLOW_KEY)

    async def _say(self, update: Update, text: str, keyboard=None) -> None:
        if update.callback_query:
            # Through `_safe_callback_answer`, never a bare `answer()`. On the
            # duplicate-refusal path this query has ALREADY been answered once
            # (`_handle_api_response_error` shows the 409 as an alert), and a
            # second answer is exactly what Telegram rejects with "query is too
            # old or invalid". Raised here, that rejection escaped BEFORE the
            # edit below, leaving the agent looking at the Confirm keyboard
            # whose only outcome is the same 409 -- the dead end this screen
            # exists to replace. The acknowledgement is best-effort; the EDIT
            # is the part that matters.
            await self._safe_callback_answer(update.callback_query, None, show_alert=False)
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        else:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML', disable_notification=True)

    def _pin_prompt(self, language: str):
        return CommonKeyboards.location_request(language, i18n.get('staff.sales.new.share_pin_button', language), include_cancel=False)

    @staticmethod
    def _skipped_step(update: Update) -> str:
        """Which step a `staff_sales_no_skip_<step>` tap claims to be skipping."""
        query = update.callback_query
        return (query.data or '').split('staff_sales_no_skip_', 1)[-1] if query else ''

    async def _stay(self, update: Update, state: int) -> int:
        """A callback whose payload does not belong to the step the agent is on.

        The registered patterns are `\\w+` (see OUTLET_TYPES for why), so the
        SUFFIX is checked in each handler and anything else lands here: answer
        the tap so the button stops spinning and return the SAME state, which
        leaves the prompt and its keyboard exactly where they are. Deliberately
        not re-editing the message -- the copy would be identical and Telegram
        answers `message is not modified`, so a re-render is noise, not a
        re-prompt.
        """
        query = update.callback_query
        logger.warning("Unroutable new-outlet callback %s in state %s", query.data if query else None, state)
        if query:
            # Through `_safe_callback_answer` like every other acknowledgement
            # in this file (see `_say`): a bare `answer()` on a stale tap raises
            # "query is too old or invalid" out of the STATE handler, which PTB
            # swallows without returning a state -- so the agent's flow would
            # silently stay on the step with no acknowledgement at all.
            await self._safe_callback_answer(query, None, show_alert=False)
        return state

    @staticmethod
    def _payload(flow: Dict) -> Dict:
        contact = None
        if flow.get('contact_name') or flow.get('contact_phone'):
            contact = {'name': flow.get('contact_name') or flow.get('name'), 'phone': flow.get('contact_phone'), 'role': 'owner'}
        return {
            'name': flow.get('name'), 'outlet_type': flow.get('outlet_type'), 'contact': contact,
            'latitude': flow.get('latitude'), 'longitude': flow.get('longitude'),
            'address_text': flow.get('address_text'), 'district': flow.get('district'),
            'class': flow.get('class'), 'notes': flow.get('notes'),
            'force': bool(flow.get('force')), 'link_user_id': flow.get('link_user_id'),
        }

    def _summary(self, flow: Dict, language: str) -> str:
        lines = [
            f"<b>{i18n.get('staff.sales.new.summary_title', language)}</b>",
            f"🏪 {escape_html(flow.get('name') or '')} · {i18n.get('staff.sales.type.' + str(flow.get('outlet_type')), language)}",
        ]
        if flow.get('contact_name') or flow.get('contact_phone'):
            lines.append(f"📞 {escape_html(flow.get('contact_name') or '')} {escape_html(flow.get('contact_phone') or '')}".rstrip())
        if flow.get('address_text'):
            lines.append(f"📍 {escape_html(flow['address_text'])}")
        if flow.get('class'):
            lines.append(f"{i18n.get('staff.sales.card.class', language)}: {flow['class']}")
        if flow.get('notes'):
            lines.append(f"📝 {escape_html(flow['notes'])}")
        return "\n".join(lines)

    # ---- steps -------------------------------------------------------------------
    @require_auth
    @require_sales_agent
    async def start_new_outlet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        context.user_data[FLOW_KEY] = {}
        await self._say(update, i18n.get('staff.sales.new.choose_type', language), SalesKeyboards.outlet_type(language))
        return NO_TYPE

    @require_auth
    @require_sales_agent
    async def select_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        outlet_type = update.callback_query.data.split('staff_sales_no_type_', 1)[-1]
        if outlet_type not in OUTLET_TYPES:
            # The registered pattern is deliberately wider than the keyboard
            # (see OUTLET_TYPES); answer the tap rather than write a type the
            # backend would reject at the very end of the walk-in.
            return await self._stay(update, NO_TYPE)
        flow['outlet_type'] = outlet_type
        await self._say(update, i18n.get('staff.sales.new.enter_name', language))
        return NO_NAME

    @require_auth
    @require_sales_agent
    async def receive_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        name = (update.message.text or '').strip()[:200]
        if not name:
            await self._say(update, i18n.get('staff.sales.new.enter_name', language))
            return NO_NAME
        flow['name'] = name
        await self._say(update, i18n.get('staff.sales.new.enter_contact_name', language), SalesKeyboards.skip(language, 'contact_name'))
        return NO_CONTACT_NAME

    @require_auth
    @require_sales_agent
    async def receive_contact_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        flow['contact_name'] = (update.message.text or '').strip()[:100] or None
        await self._say(update, i18n.get('staff.sales.new.enter_contact_phone', language), SalesKeyboards.skip(language, 'contact_phone'))
        return NO_CONTACT_PHONE

    @require_auth
    @require_sales_agent
    async def skip_contact_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if self._skipped_step(update) != 'contact_name':
            return await self._stay(update, NO_CONTACT_NAME)
        flow['contact_name'] = None
        await self._say(update, i18n.get('staff.sales.new.enter_contact_phone', language), SalesKeyboards.skip(language, 'contact_phone'))
        return NO_CONTACT_PHONE

    async def _ask_pin(self, update: Update, language: str) -> int:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(i18n.get('staff.sales.new.share_pin', language), parse_mode='HTML')
            await update.effective_message.reply_text(i18n.get('staff.sales.new.share_pin_hint', language), reply_markup=self._pin_prompt(language), parse_mode='HTML', disable_notification=True)
        else:
            await update.message.reply_text(i18n.get('staff.sales.new.share_pin', language), reply_markup=self._pin_prompt(language), parse_mode='HTML', disable_notification=True)
        return NO_PIN

    @require_auth
    @require_sales_agent
    async def receive_contact_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        is_valid, result = validate_phone((update.message.text or '').strip())
        if not is_valid:
            await self._say(update, i18n.get('staff.operator.invalid_phone', language), SalesKeyboards.skip(language, 'contact_phone'))
            return NO_CONTACT_PHONE
        flow['contact_phone'] = result
        return await self._ask_pin(update, language)

    @require_auth
    @require_sales_agent
    async def skip_contact_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if self._skipped_step(update) != 'contact_phone':
            return await self._stay(update, NO_CONTACT_PHONE)
        flow['contact_phone'] = None
        return await self._ask_pin(update, language)

    async def _after_pin(self, update: Update, context, flow: Dict, language: str) -> int:
        # The location request replaced the agent's main menu with a one-shot
        # prompt keyboard, so it is handed back here before the flow carries on.
        await update.effective_message.reply_text(
            i18n.get('staff.sales.new.pin_received', language, address=escape_html(flow.get('address_text') or '')),
            reply_markup=self._main_menu(context, language), parse_mode='HTML', disable_notification=True,
        )
        await update.effective_message.reply_text(i18n.get('staff.sales.new.choose_class', language), reply_markup=SalesKeyboards.outlet_class(language), parse_mode='HTML', disable_notification=True)
        return NO_CLASS

    @require_auth
    @require_sales_agent
    async def receive_pin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        message = update.effective_message
        location = message.location if message else None
        if not location or not is_within_tashkent(location.latitude, location.longitude):
            await message.reply_text(i18n.get('staff.operator.outside_delivery_area', language), reply_markup=self._pin_prompt(language), parse_mode='HTML')
            return NO_PIN
        flow['latitude'], flow['longitude'] = location.latitude, location.longitude
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        async with api_client as client:
            response = await client.reverse_geocode_address(token, location.latitude, location.longitude)
        payload = response.data if response.success and isinstance(response.data, dict) else {}
        flow['address_text'] = payload.get('formatted_address') or None
        flow['district'] = payload.get('district') or None
        return await self._after_pin(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def receive_typed_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        text = (update.message.text or '').strip()
        async with api_client as client:
            response = await client.geocode_address(token, text)
        if geocoder_down(response):
            await update.message.reply_text(
                i18n.get('staff.operator.geocoder_down_use_pin', language),
                reply_markup=self._pin_prompt(language), parse_mode='HTML',
            )
            return NO_PIN
        payload = response.data if response.success and isinstance(response.data, dict) else {}
        latitude, longitude = payload.get('latitude'), payload.get('longitude')
        if latitude is None or longitude is None or not is_within_tashkent(latitude, longitude):
            await update.message.reply_text(i18n.get('staff.operator.address_not_found', language), reply_markup=self._pin_prompt(language), parse_mode='HTML')
            return NO_PIN
        flow['latitude'], flow['longitude'] = latitude, longitude
        flow['address_text'] = payload.get('formatted_address') or text
        flow['district'] = payload.get('district') or None
        return await self._after_pin(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def select_class(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        choice = update.callback_query.data.split('staff_sales_no_class_', 1)[-1]
        if choice not in OUTLET_CLASSES and choice != 'skip':
            # Coercing an unrecognised suffix to "skip" silently threw away a
            # class the agent believed they had picked; refuse it instead.
            return await self._stay(update, NO_CLASS)
        flow['class'] = choice if choice in OUTLET_CLASSES else None
        await self._say(update, i18n.get('staff.sales.new.enter_notes', language), SalesKeyboards.skip(language, 'notes'))
        return NO_NOTES

    async def _fetch_candidates(self, update: Update, context, flow: Dict, language: str):
        """``(candidates, outcome)``, outcome one of the ``LOOKUP_*`` constants.

        Three answers, not two, and the caller acts differently on each. An
        empty list under ``LOOKUP_OK`` means "we asked and there are none".
        ``LOOKUP_FAILED`` means "we do not know" -- reading that as the first
        walked the agent past the one screen that exists to stop a duplicate,
        and the backend then refused the create with a 409 they had no way to
        act on, so the flow STAYS PUT and the step can be repeated.
        ``LOOKUP_NO_TOKEN`` is not a retryable step at all: the session is gone,
        so the conversation ends exactly as it does in `receive_pin`,
        `receive_typed_address` and `_submit`. Every failure is surfaced here.
        """
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return [], LOOKUP_NO_TOKEN
        params = {k: v for k, v in {'name': flow.get('name'), 'phone': flow.get('contact_phone'),
                                    'latitude': flow.get('latitude'), 'longitude': flow.get('longitude')}.items() if v is not None}
        async with api_client as client:
            response = await client.sales_dedupe_outlet(token, params)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return [], LOOKUP_FAILED
        return (response.data or {}).get('candidates', []), LOOKUP_OK

    def _duplicates_text(self, flow: Dict, candidates, language: str) -> str:
        found = "\n".join(f"• {escape_html(c.get('name') or '')} {escape_html(c.get('phone') or '')} ({c.get('distance_m') or '?'} m)" for c in candidates[:5])
        text = f"{self._summary(flow, language)}\n\n⚠️ <b>{i18n.get('staff.sales.new.duplicates_title', language)}</b>"
        return f"{text}\n{found}" if found else text

    async def _show_confirm(self, update: Update, context, flow: Dict, language: str,
                            on_lookup_failure: int = NO_CONFIRM) -> int:
        candidates = []
        if not flow.get('force'):
            candidates, outcome = await self._fetch_candidates(update, context, flow, language)
            if outcome == LOOKUP_NO_TOKEN:
                return ConversationHandler.END
            if outcome == LOOKUP_FAILED:
                # Stay where the agent is, with their prompt and its keyboard
                # still on screen, so the step can simply be repeated.
                return on_lookup_failure
        if candidates:
            await self._say(update, self._duplicates_text(flow, candidates, language),
                            SalesKeyboards.duplicate_choice(language, candidates))
        else:
            text = f"{self._summary(flow, language)}\n\n{i18n.get('staff.sales.new.confirm_hint', language)}"
            await self._say(update, text, SalesKeyboards.confirm_outlet(language))
        return NO_CONFIRM

    async def _offer_duplicates(self, update: Update, context, flow: Dict, language: str,
                                response) -> int:
        """The backend refused the create as a duplicate: show WHICH duplicates.

        The 409 names them itself — `OutletService.create` raises with
        `details={"candidates": [...]}`, and the staff client keeps the error
        body on `data` for 409s (api_client.py:415-431) — so the rows the
        backend actually matched on are the rows this screen draws. A second
        `GET /dedupe` would run against a table being written to and could
        name a different set, or none, while claiming to explain this refusal;
        it stays only as the fallback for a body that carries no candidates.

        Leaving the Confirm/Cancel keyboard up instead would let the agent tap
        Confirm forever against a 409 that can never clear; `duplicate_choice`
        gives them the three real exits (link, open, create anyway), and it
        draws "create anyway" even for an empty list.
        """
        details = (getattr(response, 'data', None) or {}).get('details') or {}
        candidates = details.get('candidates')
        if candidates is None:
            candidates, outcome = await self._fetch_candidates(update, context, flow, language)
            if outcome == LOOKUP_NO_TOKEN:
                return ConversationHandler.END
            if outcome == LOOKUP_FAILED:
                return NO_CONFIRM
        await self._say(update, self._duplicates_text(flow, candidates, language),
                        SalesKeyboards.duplicate_choice(language, candidates))
        return NO_CONFIRM

    @require_auth
    @require_sales_agent
    async def receive_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        flow['notes'] = (update.message.text or '').strip() or None
        return await self._show_confirm(update, context, flow, language, on_lookup_failure=NO_NOTES)

    @require_auth
    @require_sales_agent
    async def skip_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        if self._skipped_step(update) != 'notes':
            return await self._stay(update, NO_NOTES)
        flow['notes'] = None
        return await self._show_confirm(update, context, flow, language, on_lookup_failure=NO_NOTES)

    @require_auth
    @require_sales_agent
    async def create_anyway(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        flow['force'] = True
        return await self._show_confirm(update, context, flow, language)

    async def _submit(self, update: Update, context, flow: Dict, language: str) -> int:
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END
        async with api_client as client:
            response = await client.sales_create_outlet(token, self._payload(flow))
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            if getattr(response, 'error_code', None) == 'SALES_OUTLET_DUPLICATE':
                return await self._offer_duplicates(update, context, flow, language, response)
            return NO_CONFIRM
        outlet = (response.data or {}).get('outlet') or {}
        context.user_data.pop(FLOW_KEY, None)
        try:
            text = f"✅ {i18n.get('staff.sales.new.created', language)}\n\n{format_outlet_card(outlet, language)}"
            await self._say(update, text, SalesKeyboards.outlet_card(language, outlet))
        except Exception as e:  # render failure must not be mistaken for a failed write
            logger.error(f"Outlet created but card render failed: {e}", exc_info=True)
            await self._notify_user(update, i18n.get('staff.sales.new.created', language), show_alert=True)
        return ConversationHandler.END

    @require_auth
    @require_sales_agent
    async def confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        return await self._submit(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def link_existing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        flow = await self._flow(update, context)
        if flow is None:
            return ConversationHandler.END
        flow['link_user_id'] = int(update.callback_query.data.split('_')[-1])
        return await self._submit(update, context, flow, language)

    @require_auth
    @require_sales_agent
    async def open_existing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A duplicate is an outlet we already have: leave the flow and open its card."""
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        await self._show_card(update, context, outlet_id, language)
        return ConversationHandler.END

    @require_auth
    @require_sales_agent
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        context.user_data.pop(FLOW_KEY, None)
        await self._say(update, i18n.get('staff.cancelled', language), CommonKeyboards.back_button(language))
        return ConversationHandler.END
