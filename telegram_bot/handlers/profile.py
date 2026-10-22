"""
User profile and registration handlers
"""
import html
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional
from telegram import constants, InlineKeyboardMarkup, Update, ReplyKeyboardRemove
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes, ConversationHandler

from i18n import i18n
from shared import reorder_reminder_copy
from keyboards import ProfileKeyboards, MenuKeyboards, LanguageKeyboards, KeyboardBuilder
from shared.constants import (
    TASHKENT_DISTRICTS,
    get_district_name,
    get_district_center,
    get_all_districts,
    is_within_tashkent,
    WEB_LINK_START_PREFIX,
)
from shared.business_config import PHONE_OTP_EXPIRY
from shared.enums import UserType
from shared.user_names import greeting_name, joined_name
from handlers.menu import main_menu_handler
# Conversation states live in `handlers.conversation_states`. The
# optional-detail step table (`_ADDRESS_STEPS` / `_SKIP_TARGETS` /
# `_ADDRESS_FIELD_DATA_KEYS` / `_cleared_by_skip`) and its pure renderer live
# in `handlers.address_flow` (split out so the step machine can be read and
# tested without an Update, a bot, or a ConversationHandler — see that
# module's docstring), which re-exports the states. Imported here so every
# existing reader in this file, and every external `from handlers.profile
# import ADDRESS_BUILDING`-style import (bot.py, several tests), keeps
# working unchanged.
from handlers.address_flow import (
    SELECT_LANGUAGE, PHONE, ADDRESS_LOCATION, ADDRESS_TITLE,
    ADDRESS_REGION, ADDRESS_DISTRICT, ADDRESS_STREET, ADDRESS_BUILDING,
    ADDRESS_APARTMENT, ADDRESS_FLOOR,
    ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM,
    PHONE_VERIFY_PHONE, PHONE_VERIFY_NAME, PHONE_VERIFY_OTP,
    LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP,
    _ADDRESS_STEPS, _SKIP_TARGETS, _ADDRESS_FIELD_DATA_KEYS,
    _cleared_by_skip, render_prompt, RenderTarget, handle_input,
)
# The web-link confirm step is registration's own state, not the address flow's,
# so it comes straight from the leaf module rather than through address_flow.
from handlers.conversation_states import WEB_LINK_CONFIRM
from api_client import api_client
from database import db_manager, BotUserRepository
from flow_exit import ADDRESS_FLOW_KEYS, PHONE_FLOW_KEYS, PHONE_OTP_SENT_AT
from menu_keyboard import is_menu_customer, send_menu_keyboard
from utils import (
    user_middleware,
    validate_phone_number,
    normalize_phone_number,
    get_auth_token,
    otp_rate_limiter,
    arm_location_request,
    user_cache,
    otp_code_in,
    is_stamp_stale,
)
from config import config
from handlers.base import BaseHandler

logger = logging.getLogger('handlers')

# Column widths of UserAddress.apartment_number / floor_number (both String(20)).
# A longer answer reaches Postgres as a DataError -> 500, and the customer loses
# the whole address after five steps, so the bot rejects it first.
ADDRESS_DETAIL_MAX_LENGTH = 20

# The DB-backed prompts this handler group ARMS, grouped by the screen that
# owns them. A screen the customer can reach while one of its own prompts is
# armed names its group when it disarms (see
# ``BotUserRepository.clear_awaiting_input``) — never a blanket wipe, which
# would also throw away a concern report armed by handlers/support.py.
_PROFILE_EDIT_PROMPTS = ('edit_profile_name', 'edit_profile_birthday')
_ADDRESS_EDIT_PROMPTS = ('edit_address_title', 'edit_address_instructions')

# Phone-code lifetime stamps (spec 2026-10-21 §2, "Phone-code steps"). A code is
# good for `PHONE_OTP_EXPIRY` from the moment its SMS went out, wherever the
# customer tapped since. `PHONE_OTP_ARMED_AT` is the durable `bot_state` companion
# of `awaiting_input == 'phone_otp'`; `PHONE_OTP_SENT_AT` (from flow_exit, the
# `user_data` key list's owner) is the `PHONE_VERIFY_OTP` step's in-memory copy,
# which a flow exit carries into the durable prompt so leaving never extends it.
PHONE_OTP_ARMED_AT = 'phone_otp_armed_at'

# A web-only customer connecting this Telegram from /my-account (spec 2026-10-19
# D5, D12, D15). The one-time token waits in `user_data` under the PENDING key,
# never in a callback, so the conversation timeout, Cancel and a plain /start can
# drop it: {'token', 'language', 'message_id'}, the language being the page's (from
# the preview) and the message id the card's, so only the newest card answers.
WEB_LINK_PENDING_KEY = 'web_link_pending'
# Set after Yes when the account still needs its phone (D12): the step that
# finishes it ends with this greeting instead of "Registration completed".
# {'name', 'language'}. A phone-link merge ends with its own success message
# instead and drops it (R8).
WEB_LINK_GREETING_KEY = 'web_link_greeting'
# The backend's refusals (CustomerTelegramLinkService). A dead link (expired,
# used, or the account can no longer take a Telegram) and a Telegram that
# already has its own account read differently (R6); any other failure,
# Redis down included, is "try again in a minute" (D15f).
_WEB_LINK_DEAD_CODES = frozenset({'WEB_LINK_EXPIRED', 'WEB_LINK_NOT_AVAILABLE'})
_WEB_LINK_HAS_ACCOUNT = 'TELEGRAM_ALREADY_HAS_ACCOUNT'
# The phone-link code refusals that end the step (auth_service.verify_phone_link_and_merge_accounts):
# the code or its link request is gone, or an account it named is. Read from `error_code`, never from
# the message, which arrives in the customer's language. Any other refusal asks for the code again.
_LINK_CODE_DEAD_CODES = frozenset({'OTP_EXPIRED', 'USER_NOT_FOUND'})


def _markdown_copy(key: str, language: str, **data: Any) -> str:
    """Render seeded Markdown copy with every interpolated value escaped as DATA.

    The seeded copy is MARKUP — ``*Aniqlangan joylashuv:*``, ``**{title}**`` —
    and that is why these messages go out with ``parse_mode='Markdown'`` at all.
    Everything interpolated INTO it is data the customer or a geocoder wrote,
    and Uzbek addresses really do carry ``_`` (building suffixes like "15_A"),
    ``[`` (geocoder annotations) and ``*``. Unescaped, one of those makes
    Telegram refuse the whole message with "can't parse entities"; the handler's
    ``except Exception`` then turns a formatting problem into a dead screen.
    ``_edit_or_replace_callback_message`` now degrades a refused edit to plain
    text (raw ``*`` showing), which is a backstop, not a fix: escaping the
    values is still the fix.

    So: escape the VALUES, never the template. Escaping the rendered string
    instead (what ``district_selected`` does for MarkdownV2) would neuter the
    copy's own bold — the trap on the other side of this fix.

    One helper rather than an ``escape_markdown`` at each call site: the rule
    "copy is markup, parameters are data" then has exactly one expression, and a
    new Markdown screen inherits it by using it.
    """
    return i18n.get(
        key,
        language,
        **{name: escape_markdown(str(value)) for name, value in data.items()},
    )


# The columns the optional-detail chain owns, i.e. everything a shared-pin
# address can still gain AFTER it has been created. Sent in full on every
# enrichment write rather than merged key-by-key: Skip CLEARS a value, and a
# payload that omitted the key could not express "this is now empty".
_ADDRESS_DETAIL_KEYS = ('apartment_number', 'floor_number', 'delivery_instructions')

# Snapshot of the detail payload last pushed to the backend, so a step that
# changed nothing (every Skip) costs no HTTP call. Lives inside
# temp_address_data because it dies with the flow; it is never read by
# _build_address_payload, which names the columns it sends explicitly.
_DETAIL_SYNC_SNAPSHOT_KEY = 'detail_sync_snapshot'


def _is_shared_pin_address(context) -> bool:
    """True when this address came from a shared map pin rather than manual entry.

    The two flows meet at several steps but diverge on what comes next, and this
    is the single fact that tells them apart. It decides whether the title step
    continues into the detail chain or saves, and whether the terminal step saves
    directly or geocodes first — so it is defined once, here.

    Checking coordinates alongside this would be redundant: `location_source`
    is set to 'shared' in exactly one place (`location_received`), in the same
    block that stores latitude and longitude, and only after the delivery-zone
    guard has rejected a (0, 0)-style falsy pin.
    """
    addr_data = context.user_data.get('temp_address_data', {})
    return addr_data.get('location_source') == 'shared'


class ProfileHandlers(BaseHandler):
    """Profile management handlers"""

    @staticmethod
    def _is_callback_message_deletable(callback_query) -> bool:
        """
        Guard deletion attempts with Telegram constraints.
        Bot API deleteMessage is time-limited (typically ~48h), so old callback
        source messages should not be deleted.
        """
        message = getattr(callback_query, "message", None)
        if not message:
            return False

        message_date = getattr(message, "date", None)
        if not message_date:
            return False

        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)

        age_seconds = (datetime.now(timezone.utc) - message_date.astimezone(timezone.utc)).total_seconds()
        return age_seconds < 47 * 3600

    async def profile_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user profile menu"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # The customer navigated away from any prompt this handler group
            # armed — but a concern report armed elsewhere is not ours to throw
            # away, so only our own prompts are disarmed.
            await self.user_repo.clear_awaiting_input(
                user_id, *_PROFILE_EDIT_PROMPTS, *_ADDRESS_EDIT_PROMPTS
            )

            # Get user profile from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_profile(user_token)
                if not response.success and response.status_code == 404:
                    # Recover from stale cached token pointing to an old/deleted user.
                    token_manager = context.bot_data.get('token_manager') if context.bot_data else None
                    if token_manager:
                        await token_manager.invalidate_tokens(user_id)

                    user_token = await get_auth_token(
                        update,
                        context,
                        client,
                        force_refresh=True
                    )
                    if not user_token:
                        await self._handle_auth_error(update, language)
                        return

                    response = await client.get_user_profile(user_token)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                profile = response.data['data']

            not_set_label = i18n.get('telegram.common.not_set', language)
            full_name = (f"{profile.get('first_name', '')} {profile.get('last_name', '')}" or not_set_label).strip()

            dob_raw = profile.get('date_of_birth')
            if isinstance(dob_raw, str) and dob_raw:
                try:
                    dob_parsed = datetime.strptime(dob_raw[:10], "%Y-%m-%d")
                    dob_display = dob_parsed.strftime("%d-%m-%Y")
                except ValueError:
                    dob_display = dob_raw[:10]
            else:
                dob_display = not_set_label

            # Format profile information
            profile_text = f"{i18n.get('telegram.profile_title', language)}\n\n"
            profile_text += f"{i18n.get('telegram.profile_name', language)}: {full_name}\n"
            profile_text += f"{i18n.get('telegram.profile_phone', language)}: {profile.get('phone', not_set_label)}\n"
            profile_text += f"{i18n.get('telegram.profile_email', language)}: {profile.get('email', not_set_label)}\n"
            profile_text += f"{i18n.get('telegram.profile_birthday', language)}: {dob_display}\n"
            profile_text += f"{i18n.get('telegram.profile_language', language)}: {language}"

            keyboard = ProfileKeyboards.profile_menu(language)

            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query, profile_text, reply_markup=keyboard
                )
                await self._ack(update.callback_query)
            else:
                await update.message.reply_text(
                    text=profile_text,
                    reply_markup=keyboard
                )

            logger.info(f"Profile menu shown to user {user_id}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="profile_menu")

    def _extract_delivery_telegram_status_updates_enabled(self, api_payload: Dict[str, Any]) -> bool:
        """Extract Telegram delivery-status toggle from notifications preferences payload."""
        preferences = (api_payload or {}).get('data', {}).get('preferences', {})
        enabled = preferences.get('delivery_telegram_status_updates_enabled')
        return enabled if isinstance(enabled, bool) else True

    def _extract_reorder_reminders_enabled(self, api_payload: Dict[str, Any]) -> Optional[bool]:
        """The EFFECTIVE reorder-reminder setting the backend publishes (spec 2026-10-09 §8), or
        None when it publishes none. The screen then simply has no reminder row."""
        preferences = (api_payload or {}).get('data', {}).get('preferences', {})
        enabled = (preferences.get('reorder_reminders') or {}).get('enabled')
        return enabled if isinstance(enabled, bool) else None

    async def _render_notification_settings(
        self,
        update: Update,
        language: str,
        delivery_telegram_status_updates_enabled: bool,
        callback_toast: str | None = None,
        reorder_reminders_enabled: Optional[bool] = None,
    ) -> None:
        """Render notification settings UI."""
        status_key = (
            'telegram.notifications.current_status_enabled'
            if delivery_telegram_status_updates_enabled
            else 'telegram.notifications.current_status_disabled'
        )
        text = (
            f"{i18n.get('telegram.notifications.title', language)}\n\n"
            f"{i18n.get('telegram.notifications.delivery_status_updates_label', language)}\n"
            f"{i18n.get('telegram.notifications.delivery_status_updates_description', language)}\n\n"
            f"{i18n.get(status_key, language)}"
        )
        if reorder_reminders_enabled is not None:
            text += f"\n\n{reorder_reminder_copy.settings_status(i18n.renderer(language), reorder_reminders_enabled)}"
        keyboard = ProfileKeyboards.notification_settings(
            language=language,
            delivery_telegram_status_updates_enabled=delivery_telegram_status_updates_enabled,
            reorder_reminders_enabled=reorder_reminders_enabled,
        )

        if update.callback_query:
            await self._edit_or_replace_callback_message(
                update.callback_query, text, reply_markup=keyboard
            )
            await self._ack(update.callback_query, callback_toast)
            return

        await update.message.reply_text(text=text, reply_markup=keyboard)

    async def notification_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show Telegram delivery-status notification toggle screen."""
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

                response = await client.get_notification_preferences(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                enabled = self._extract_delivery_telegram_status_updates_enabled(response.data)
                reminders_enabled = self._extract_reorder_reminders_enabled(response.data)

            await self._render_notification_settings(
                update,
                language,
                delivery_telegram_status_updates_enabled=enabled,
                reorder_reminders_enabled=reminders_enabled,
            )
        except Exception as e:
            await self._handle_error(update, exc=e, operation="notification_settings")

    async def toggle_delivery_telegram_status_notifications(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Toggle Telegram delivery status notifications and refresh notification settings screen."""
        await self._apply_notification_toggle(update, context, 'delivery_telegram_status_updates_enabled')

    async def toggle_reorder_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Profile -> Notifications: reorder reminders on/off (spec 2026-10-09 §8). The body
        says only on/off; the backend records the route as telegram_settings from this bot's
        User-Agent."""
        await self._apply_notification_toggle(update, context, 'reorder_reminders_enabled')

    async def _apply_notification_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE, field: str):
        """One PUT of one field, then the screen redrawn from the backend's answer. Shared by
        both toggles on the screen, so they cannot drift apart: each sends only ITS field (a
        delivery flip must never write a reminder setting), and the redrawn screen shows what the
        backend published, never what the tap asked for."""
        try:
            user = await user_middleware(update)
            if not user:
                return

            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            enabled = query.data.rsplit('_', 1)[-1] == 'on'

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.update_notification_preferences(user_token, {field: enabled})
                if not response.success:
                    await self._ack(query,
                        i18n.get('telegram.notifications.update_failed', language),
                        show_alert=True,
                    )
                    return

            await self._render_notification_settings(
                update,
                language,
                delivery_telegram_status_updates_enabled=self._extract_delivery_telegram_status_updates_enabled(
                    response.data
                ),
                reorder_reminders_enabled=self._extract_reorder_reminders_enabled(response.data),
                callback_toast=i18n.get('telegram.notifications.update_success', language),
            )
        except Exception as e:
            logger.error(f"Error toggling notification setting {field}: {e}")
            if update.callback_query:
                await self._ack(update.callback_query,
                    i18n.get('telegram.notifications.update_failed', await self._resolve_language(update)),
                    show_alert=True,
                )

    async def phone_verification_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show phone verification menu with add/verify options"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user profile from API to check phone status
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_profile(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                profile = response.data['data']

            phone = profile.get('phone')
            phone_verified = profile.get('phone_verified_at') is not None or profile.get('phone_verified', False)

            # Build status message
            if not phone:
                status_text = i18n.get('telegram.phone.title', language) + "\n\n" + i18n.get('telegram.phone.no_phone_added', language)
                buttons = [
                    [{'text': i18n.get('telegram.phone.add_prompt', language), 'callback_data': 'add_phone_number'}],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
                ]
            elif not phone_verified:
                status_text = i18n.get('telegram.phone.title', language) + f"\n\n{i18n.get('telegram.profile_phone', language)}: {phone}\n" + i18n.get('telegram.phone.phone_not_verified', language)
                buttons = [
                    [{'text': i18n.get('telegram.phone.verification_prompt', language), 'callback_data': 'verify_phone_number'}],
                    [{'text': i18n.get('telegram.phone.change_number', language), 'callback_data': 'add_phone_number'}],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
                ]
            else:
                status_text = i18n.get('telegram.phone.title', language) + f"\n\n{i18n.get('telegram.profile_phone', language)}: {phone}\n" + i18n.get('telegram.phone.phone_verified', language)
                buttons = [
                    [{'text': i18n.get('telegram.phone.change_number', language), 'callback_data': 'add_phone_number'}],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
                ]

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query, status_text, reply_markup=keyboard
                )
                await self._ack(update.callback_query)
            else:
                await update.message.reply_text(
                    text=status_text,
                    reply_markup=keyboard
                )

            logger.info(f"Phone verification menu shown to user {user_id}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="phone_verification_menu")

    async def add_phone_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start phone number addition/change flow - entry point for phone verification conversation"""
        try:
            user = await user_middleware(update)
            if not user:
                return ConversationHandler.END

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Prompt user to share phone
            phone_prompt = i18n.get('telegram.phone.send_code_prompt', language)

            # Send the prompt with reply keyboard; its Cancel row leads back to the menu.
            keyboard = ProfileKeyboards.phone_request(language, with_cancel=True)

            if update.callback_query:
                await self._ack(update.callback_query)
                await update.callback_query.message.reply_text(
                    phone_prompt,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    phone_prompt,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )

            logger.info(f"Phone addition flow started for user {user_id}, entering PHONE_VERIFY_PHONE state")
            return PHONE_VERIFY_PHONE

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_phone_number")
            return ConversationHandler.END

    async def phone_verify_contact_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone contact shared during phone verification flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            contact = update.message.contact

            # Verify the contact belongs to the user
            if contact.user_id != user_id:
                await update.message.reply_text(
                    i18n.get('telegram.phone.share_own_phone', language),
                    reply_markup=ProfileKeyboards.phone_request(language, with_cancel=True)
                )
                return PHONE_VERIFY_PHONE

            phone = normalize_phone_number(contact.phone_number)
            if not phone:
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language),
                    reply_markup=ProfileKeyboards.phone_request(language, with_cancel=True)
                )
                return PHONE_VERIFY_PHONE
            logger.info(f"Phone contact received for user {user_id}: {phone}")

            # Store phone in context for later
            context.user_data['pending_phone'] = phone

            # Contact sharing is trusted by Telegram, mark phone as verified
            await self.user_repo.set_user_phone_verified(user_id, phone)

            # Also update via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    try:
                        await client.update_user_profile(user_token, {'phone': phone})
                        logger.info(f"Phone updated via API for user {user_id}")
                    except Exception as api_error:
                        logger.warning(f"Failed to update phone via API: {api_error}")

            # The menu replaces the phone request keyboard, then the name is asked for
            success_text = i18n.get('telegram.phone.phone_accepted', language)
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id, success_text, language
            )

            # Ask for full name
            name_prompt = i18n.get('telegram.enter_name', language)
            await update.message.reply_text(name_prompt)

            logger.info(f"Phone accepted for user {user_id}, asking for name")
            return PHONE_VERIFY_NAME

        except Exception as e:
            logger.error(f"Error in phone_verify_contact_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return await self._end_flow_with_menu(update, context)

    async def phone_verify_text_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle a typed phone number during the phone verification flow.

        A typed number is not Telegram-trusted, so nothing is written here: it
        is staged by /send-otp and saved only when its code is verified
        (phone_verify_otp_received). Writing it straight into `users.phone`
        let a customer park somebody else's number on their account and lock
        the real owner out.
        tests/telegram_bot/test_change_phone_journey_dispatcher.py
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            phone_text = update.message.text.strip()

            if not await validate_phone_number(phone_text):
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language),
                    reply_markup=ProfileKeyboards.phone_request(language, with_cancel=True)
                )
                return PHONE_VERIFY_PHONE

            phone = normalize_phone_number(phone_text)
            logger.info(f"Phone text received for user {user_id}: {phone}")

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await update.message.reply_text(
                        i18n.get('telegram.auth.failed_try_again', language),
                        reply_markup=ProfileKeyboards.phone_request(language, with_cancel=True)
                    )
                    return PHONE_VERIFY_PHONE

                return await self._send_typed_phone_code(
                    update, context, client, user_token, phone, language,
                    phone_state=PHONE_VERIFY_PHONE, code_state=PHONE_VERIFY_OTP, registered=True,
                )

        except Exception as e:
            logger.error(f"Error in phone_verify_text_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return await self._end_flow_with_menu(update, context)

    async def phone_verify_otp_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verify the code for a number typed in Profile → Phone (PHONE_VERIFY_OTP).

        The backend's /verify-phone saves the number; then the flow goes on to
        the name step, as it does after a shared contact.
        """
        async def _verified(update, language):
            await update.message.reply_text(i18n.get('telegram.phone.phone_verified', language))
            await update.message.reply_text(i18n.get('telegram.enter_name', language))
            return PHONE_VERIFY_NAME

        # This flow's own closing copy points back to My Profile; registration's
        # points to /start.
        return await self._receive_typed_phone_code(
            update, context,
            code_state=PHONE_VERIFY_OTP,
            on_verified=_verified,
            missing_key='telegram.phone.verification_flow_timed_out',
            expired_key='telegram.phone.verification_flow_timed_out',
        )

    async def _send_typed_phone_code(self, update, context, client, user_token, phone, language,
                                     *, phone_state, code_state, registered: bool):
        """Send the code for a TYPED number and move to the flow's code step.

        Shared by registration's typed branch (PHONE → REGISTER_OTP) and
        Profile → Phone (PHONE_VERIFY_PHONE → PHONE_VERIFY_OTP). /send-otp only
        stages the number; nothing is saved until _receive_typed_phone_code's
        /verify-phone succeeds. A refusal keeps the customer on ``phone_state``
        with the share-contact keyboard, so they can try another number.

        ``registered`` is True for Profile → Phone, whose customer has the bottom
        menu: the share-contact keyboard keeps its Cancel row, and an accepted
        number puts the menu back. Registration (False) has no menu yet, so an
        accepted number only takes the share-contact keyboard down.
        """
        otp_response = await client.send_phone_verification(user_token, phone)
        if not otp_response.success:
            logger.error(f"Failed to send phone verification OTP: {otp_response.error}")
            if otp_response.status_code == 409:
                # Another account already holds this number, in any typed format.
                refusal = i18n.get('telegram.phone.already_registered_use_different', language)
            else:
                refusal = i18n.get(
                    'telegram.phone.verification_sms_send_failed',
                    language,
                    error=otp_response.error
                )
            await update.message.reply_text(
                refusal,
                reply_markup=(
                    ProfileKeyboards.phone_request(language, with_cancel=True) if registered
                    else ProfileKeyboards.phone_request(language)
                )
            )
            return phone_state

        # Replace the contact keyboard, then ask for the code
        accepted = i18n.get('telegram.phone.phone_accepted', language)
        if registered:
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, update.effective_user.id, accepted, language
            )
        else:
            await update.message.reply_text(accepted, reply_markup=ReplyKeyboardRemove())

        otp_data = otp_response.data.get('data', {}) if isinstance(otp_response.data, dict) else {}
        phone_masked = otp_data.get('phone_masked', phone)

        await update.message.reply_text(
            i18n.get(
                'telegram.phone.verification_code_sent_to_phone_prompt',
                language,
                phone_masked=phone_masked
            )
        )

        # Stay INSIDE the conversation and capture the OTP in ``code_state``.
        # We deliberately do NOT set the global 'awaiting_otp' flag here — the
        # in-conversation handler owns this path, so /cancel and /start behave
        # correctly during OTP entry and the global catch-all won't fire.
        context.user_data['pending_phone_verification'] = phone
        # The code's lifetime starts now, not when the customer next taps away
        # (`flow_exit.leave_all_flows` carries this into the durable prompt).
        context.user_data[PHONE_OTP_SENT_AT] = datetime.now(timezone.utc).isoformat()

        logger.info(f"Phone verification OTP sent to {phone} for user {update.effective_user.id}")
        return code_state

    async def phone_verify_name_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle name input during phone verification flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            text = update.message.text.strip()

            logger.info(f"Name received for user {user_id}: {text}")

            # Validate name - must have at least 2 characters and contain letters
            if len(text) < 2:
                await update.message.reply_text(
                    i18n.get('telegram.name.too_short', language)
                )
                return PHONE_VERIFY_NAME

            # Check for valid name (letters and spaces only)
            if not any(c.isalpha() for c in text):
                await update.message.reply_text(
                    i18n.get('telegram.name.invalid', language)
                )
                return PHONE_VERIFY_NAME

            # Parse first and last name
            name_parts = text.split()
            first_name = name_parts[0]
            last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''

            # Update profile via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    profile_data = {
                        'first_name': first_name,
                        'last_name': last_name
                    }
                    response = await client.update_user_profile(user_token, profile_data)
                    if response.success:
                        logger.info(f"Name updated via API for user {user_id}: {first_name} {last_name}")
                    else:
                        logger.warning(f"Failed to update name via API: {response.error}")

            # Show success and main menu
            success_text = i18n.get('telegram.profile_updated', language)
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, update.effective_user.id, success_text, language
            )

            # The flow is done: drop every phone-flow key, the code's send stamp included.
            for name in PHONE_FLOW_KEYS:
                context.user_data.pop(name, None)

            logger.info(f"Phone verification flow completed for user {user_id}")
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in phone_verify_name_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def cancel_phone_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel phone verification flow: one message, which puts the bottom menu back."""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Clear pending data
            for name in PHONE_FLOW_KEYS:
                context.user_data.pop(name, None)

            if update.callback_query:
                await self._ack(update.callback_query)
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id,
                i18n.get('telegram.action_cancelled', language), language,
            )

            logger.info(f"Phone verification cancelled for user {user_id}")
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error cancelling phone verification: {e}")
            return ConversationHandler.END

    async def _end_flow_with_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """A flow step failed unexpectedly: say so, put the bottom menu back, end the flow.

        The step may have left a phone or location keyboard on screen (one-time ones are
        already gone), so ending in silence would leave the customer with a dead keyboard
        or none at all. Best-effort: a failure here is logged and the flow still ends.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id,
                i18n.get('telegram.error_occurred', language), language,
            )
        except Exception as e:
            logger.warning("Could not put the menu back after a failed flow step: %s", e)
        return ConversationHandler.END

    # --- Phone-code steps (spec 2026-10-21 §2) ---------------------------------
    #
    # A code typed within `PHONE_OTP_EXPIRY` of its SMS still verifies, even
    # after the customer tapped elsewhere: an exit never disarms `phone_otp`
    # (`flow_exit.PROMPTS_LEFT_BY_EXIT`). Text that is not a code ends the step
    # and is routed like any other message, and after the lifetime the prompt
    # is disarmed. tests/telegram_bot/test_phone_code_steps.py

    async def arm_phone_otp(self, context, telegram_id: int, phone: str, *,
                            prompted_update_id: Optional[int] = None,
                            armed_at: Optional[str] = None) -> None:
        """Arm the code prompt: the in-memory marker and its DURABLE TWIN.

        `user_data` dies with the process, and the customer is reading this code
        off an SMS that does not. Without the `bot_state` half, the digits they
        type after a deploy fall through the router to `capture_support_message`
        (silent by design) and a live one-time password lands unredacted in the
        admin Support Inbox while the phone stays unverified.

        `armed_at` is when the SMS was sent; it defaults to now, for a caller
        that has just sent it.
        """
        context.user_data['awaiting_otp'] = True
        context.user_data['pending_phone_verification'] = phone
        if prompted_update_id is not None:
            context.user_data['otp_prompted_update_id'] = prompted_update_id
        await self.user_repo.arm_awaiting_input(
            telegram_id, 'phone_otp',
            pending_phone_verification=phone,
            **{PHONE_OTP_ARMED_AT: armed_at or datetime.now(timezone.utc).isoformat()},
        )

    async def disarm_phone_otp(self, context, telegram_id: int) -> None:
        """Drop both halves of the code prompt. The durable half is best-effort.

        Leaving the `bot_state` row armed would be worse than the bug it fixes:
        every later code-shaped message would be read as another verification
        code. `disarm` names only `phone_otp`, so a newer prompt is left alone
        and a half-finished `address_draft` is not collateral.
        """
        for name in PHONE_FLOW_KEYS:
            context.user_data.pop(name, None)
        try:
            await self.user_repo.disarm(telegram_id, 'phone_otp')
        except Exception as disarm_error:
            logger.warning("Could not disarm the OTP prompt for %s: %s", telegram_id, disarm_error)

    @staticmethod
    def may_arm_phone_otp(state: dict) -> bool:
        """True while no OTHER prompt holds `bot_state.awaiting_input`.

        The code prompt is (re-)armed only into an empty slot or its own: a
        prompt the customer opened after the code was sent is newer, and its
        text is its own, so it is never overwritten.
        """
        return state.get('awaiting_input') in (None, 'phone_otp')

    async def keep_code_answerable(self, context, telegram_id: int) -> None:
        """The `PHONE_VERIFY_OTP` step is ending: keep its code answerable.

        The SMS may still be on its way, so the step's code moves into the
        durable `phone_otp` prompt the group-0 router answers, stamped with the
        SMS's send time (`PHONE_OTP_SENT_AT`): ending the step never extends the
        code's lifetime, and a code typed within it never lands in the Support
        Inbox (spec §2, "Why"). Used by every way the step ends without a code:
        non-code text (`end_phone_code_step`) and a flow exit
        (`flow_exit.leave_all_flows`). Never overwrites a newer prompt.
        """
        phone = context.user_data.get('pending_phone_verification')
        if not phone:
            return
        if not self.may_arm_phone_otp(await self.user_repo.get_user_state(telegram_id)):
            return
        await self.arm_phone_otp(context, telegram_id, phone, armed_at=context.user_data.get(PHONE_OTP_SENT_AT))

    @staticmethod
    def phone_code_expired(state: dict) -> bool:
        """True once the durable prompt's code is past `PHONE_OTP_EXPIRY`.

        A missing stamp reads as expired. The router stamps an unstamped
        `phone_otp` prompt on first sight before asking, so a missing stamp
        reaches here only when a newer prompt holds `awaiting_input`: the text
        is that prompt's, never a code.
        """
        return is_stamp_stale(state.get(PHONE_OTP_ARMED_AT), PHONE_OTP_EXPIRY / 60)

    async def cancel_phone_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The Cancel under the profile code prompt: disarm it and show the menu."""
        query = update.callback_query
        try:
            await self._ack(query)
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            await self.disarm_phone_otp(context, user_id)
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id,
                i18n.get('telegram.action_cancelled', language), language,
            )
        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_phone_otp")

    async def end_phone_code_step(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """`PHONE_VERIFY_OTP` got text that is not a code: the step ends, silently.

        Registered WITHOUT `_consumes`, so the same text walks on to the group-0
        catch-all and is handled like any other message, instead of the old
        endless "the code must be 6 digits" reply. The code itself stays
        answerable for the rest of its lifetime (`keep_code_answerable`).
        """
        telegram_id = update.effective_user.id
        try:
            await self.keep_code_answerable(context, telegram_id)
        except Exception as arm_error:
            logger.warning("Could not keep the phone code answerable for %s: %s", telegram_id, arm_error)
        for name in PHONE_FLOW_KEYS:
            context.user_data.pop(name, None)
        return ConversationHandler.END

    async def verify_phone_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start phone verification flow for existing phone"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user's phone from profile
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_profile(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                profile = response.data['data']
                phone = profile.get('phone')

            if not phone:
                await self._ack(update.callback_query, i18n.get('telegram.phone.no_phone_added', language))
                await update.callback_query.message.reply_text(
                    i18n.get('telegram.phone.no_phone_added', language)
                )
                return

            # Send verification code
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    response = await client.send_phone_verification(user_token, phone)
                    if response.success:
                        # Sent as Markdown a few lines down, so the phone goes
                        # through the same door as every other interpolated
                        # value in this file.
                        verification_msg = _markdown_copy(
                            'telegram.phone.verification_sms_sent',
                            language,
                            phone=phone
                        )

                        await self._ack(update.callback_query,
                            i18n.get('telegram.phone.verification_code_sent_toast', language)
                        )
                        await update.callback_query.message.reply_text(
                            verification_msg,
                            parse_mode='Markdown',
                            reply_markup=ProfileKeyboards.phone_code_prompt(language),
                        )

                        # The SMS just went out, so the default stamp (now) is its send time.
                        await self.arm_phone_otp(context, user_id, phone, prompted_update_id=update.update_id)

                        logger.info(f"Verification SMS sent to {phone} for user {user_id}")
                    else:
                        await self._ack(update.callback_query,
                            i18n.get('telegram.phone.verification_code_send_failed_toast', language)
                        )
                        await update.callback_query.message.reply_text(
                            i18n.get(
                                'telegram.phone.verification_sms_send_failed',
                                language,
                                error=response.error
                            )
                        )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="verify_phone_number")

    # Recognized /start deep-link acquisition markers (AEO roadmap, task AG-M3).
    #   ``ai_<engine>``   — arrived from an AI assistant surface, e.g.
    #                       t.me/<bot>?start=ai_chatgpt (links seeded in llms.txt
    #                       and other AI-facing surfaces).
    #   ``src_<channel>`` — arrived from a claimed business profile / directory,
    #                       e.g. t.me/<bot>?start=src_2gis.
    # Both are whitelist-validated so arbitrary deep-link strings can never
    # reach logs or the registration payload.
    _AI_ENGINES = frozenset(
        {"chatgpt", "gemini", "perplexity", "claude", "copilot", "alisa", "deepseek", "other"}
    )
    _SRC_CHANNELS = frozenset(
        {"gbp", "yandex", "2gis", "fsq", "bing", "apple", "olx", "site", "press"}
    )

    @classmethod
    def _capture_referral_arg(cls, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Store /start deep-link params for use at registration.

        Handles ``ref_<code>`` (referral, unchanged behaviour) plus the
        ``ai_<engine>`` / ``src_<channel>`` acquisition markers above. An
        acquisition marker is logged immediately — a visit-level signal that
        fires for existing users too — and, for new users, attached to the
        registration payload (consumed by the backend once
        ``users.acquisition_source`` lands with task AG-M1).
        """
        args = getattr(context, "args", None) or []
        if not args:
            return
        if context.user_data is None:
            return
        param = str(args[0]).strip()
        if param.startswith("ref_"):
            code = param[len("ref_"):].strip()
            if code:
                context.user_data["referral_code"] = code
            return
        lowered = param.lower()
        source = None
        if lowered.startswith("ai_") and lowered[len("ai_"):] in cls._AI_ENGINES:
            source = lowered
        elif lowered.startswith("src_") and lowered[len("src_"):] in cls._SRC_CHANNELS:
            source = lowered
        if source:
            context.user_data["acquisition_source"] = source
            # Stable, greppable marker for log-based telemetry (task AG-M4).
            logger.info("acquisition_deeplink source=%s", source)

    async def start_registration_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start registration process"""
        try:
            user_id = update.effective_user.id

            # Every /start forgets a web-link confirm that was still open (D15e):
            # only a link opened by THIS /start may be confirmed.
            if context.user_data is not None:
                context.user_data.pop(WEB_LINK_PENDING_KEY, None)

            # A /my-account link (`/start web_<token>`) is answered before the
            # referral capture and before the lookup below: an unknown Telegram
            # must never reach the language card, whose tap creates the row that
            # would then block the link (spec 2026-10-19 D5).
            web_link_token = self._web_link_token(context)
            if web_link_token is not None:
                return await self._start_web_link(update, context, web_link_token)

            # Capture a referral deep-link param (t.me/<bot>?start=ref_CODE arrives
            # as "/start ref_CODE") so it can be applied on registration below.
            self._capture_referral_arg(context)

            user_repo = BotUserRepository(db_manager)
            # Check if user already exists
            existing_user = await user_repo.get_user_by_telegram_id(user_id)
            logger.info(f"existing_user {existing_user} for user_id {user_id}")

            if not existing_user:
                welcome_text_en = i18n.get('telegram.registration_welcome', "en")
                welcome_text_uz = i18n.get('telegram.registration_welcome', "uz")
                welcome_text_ru = i18n.get('telegram.registration_welcome', "ru")
                welcome_text = f"{welcome_text_en}\n\n{welcome_text_uz}\n\n{welcome_text_ru}"

                await update.message.reply_text(
                    welcome_text,
                    reply_markup=LanguageKeyboards.select_language(
                        (context.user_data or {}).get('referral_code')
                    )
                )

                return SELECT_LANGUAGE
            else:
                return await self._resume_existing_account(update, context, existing_user)

        except Exception as e:
            logger.error(f"Error starting registration: {e}")
            return ConversationHandler.END

    async def _resume_existing_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       existing_user: dict) -> int:
        """What /start does for a Telegram that already has its row.

        Shared with a dead web link from such a Telegram, which behaves exactly
        like a plain /start for that account (spec 2026-10-19 D15a).
        """
        user_id = update.effective_user.id

        # Existing row but phone never captured -> resume phone collection
        # instead of dropping the user at the main menu with no phone.
        if not is_menu_customer(existing_user):
            language = existing_user.get('preferred_language') or await i18n.get_user_language(user_id)
            keyboard = ProfileKeyboards.phone_request(language)

            await update.message.reply_text(
                text=i18n.get('telegram.registration.share_contact_prompt', language),
                reply_markup=keyboard
            )

            return PHONE

        # Already registered, show main menu — honour the user's saved
        # language rather than the Telegram client locale.
        language = existing_user.get('preferred_language') or await i18n.get_user_language(user_id)
        await send_menu_keyboard(
            context.bot, update.effective_chat.id, user_id,
            i18n.get('telegram.main_menu', language), language,
        )

        return ConversationHandler.END

    # ------------------------------------------------------------------
    # Web-to-Telegram link (spec 2026-10-19): a web-only customer taps
    # "Open in Telegram" on /my-account and lands here as /start web_<token>.
    # One confirm card (R4); nothing is connected until Yes. Every message of
    # this flow is in `_web_link_language`, and goes out with send_message
    # whenever the update may be a tap rather than a message.
    # ------------------------------------------------------------------

    @staticmethod
    def _web_link_token(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
        """The token of a `/start web_<token>` deep link, or None for any other /start.

        An empty or truncated token is still a token (``""`` included): the
        backend answers it as a dead link, so a mangled link reads like an
        expired one and never falls through to the language card (RF2).
        """
        args = getattr(context, "args", None) or []
        if not args:
            return None
        param = str(args[0])
        if not param.startswith(WEB_LINK_START_PREFIX):
            return None
        return param[len(WEB_LINK_START_PREFIX):]

    @staticmethod
    def _web_link_language(update: Update, *, pending: Optional[dict] = None,
                           existing: Optional[dict] = None) -> str:
        """The language of every web-link message.

        The page's language first: the preview publishes it and the pending
        link keeps it, so even a refused confirm answers in it (spec D11). Then
        this Telegram's own row, then the Telegram client's locale.
        """
        return i18n.normalize_language(
            (pending or {}).get('language')
            or (existing or {}).get('preferred_language')
            or update.effective_user.language_code
        )

    @staticmethod
    def _web_link_keyboard(language: str) -> InlineKeyboardMarkup:
        """Yes / Cancel. No token in the data, and neither matches `^link_` (D15d)."""
        return KeyboardBuilder.build_inline_keyboard([
            [{'text': i18n.get('telegram.web_link.yes_button', language), 'callback_data': 'weblink_yes'}],
            [{'text': i18n.get('telegram.cancel', language), 'callback_data': 'weblink_no'}],
        ])

    async def _start_web_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> int:
        """Show the confirm card for a /my-account link, or say why it cannot connect.

        The preview never uses the link up. The token waits in `user_data`, not
        in the buttons, so the timeout, Cancel and a plain /start can drop it.
        """
        user_id = update.effective_user.id
        async with api_client as client:
            response = await client.web_link_preview(user_id, token)

        if response.success:
            preview = response.data.get('data', {}) if isinstance(response.data, dict) else {}
            language = self._web_link_language(update, pending={'language': preview.get('language')})
            # HTML, not `_markdown_copy`: the masked label's own asterisks would
            # need escapes inside the bold, and Telegram refuses an escape inside
            # an entity ("can't parse entities"), so the card would never show (RF3).
            card = await update.message.reply_text(
                i18n.get(
                    'telegram.web_link.confirm_prompt',
                    language,
                    account=html.escape(preview.get('account_label') or ''),
                ),
                parse_mode='HTML',
                reply_markup=self._web_link_keyboard(language),
            )
            context.user_data[WEB_LINK_PENDING_KEY] = {
                'token': token, 'language': language, 'message_id': card.message_id,
            }
            return WEB_LINK_CONFIRM

        existing = await self.user_repo.get_user_by_telegram_id(user_id)
        code = self._response_error_code(response)
        if code in _WEB_LINK_DEAD_CODES and existing:
            # A dead link from a Telegram that has its own row: a plain /start (D15a).
            return await self._resume_existing_account(update, context, existing)
        return await self._web_link_refused(
            update, context, code,
            existing=existing,
            language=self._web_link_language(update, existing=existing),
        )

    async def _web_link_refused(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                code: Optional[str], *, existing: Optional[dict], language: str) -> int:
        """Say why the link did not connect (R6, D15f) and end. Never the language card.

        A Telegram with its own account is pointed to this chat and the
        conversation ends, so its next message reaches the Support Inbox; the
        bottom keyboard comes along only when that account has its phone (D15a):
        `send_menu_keyboard` decides that from the account's row, so `existing`
        only supplies the language.
        """
        if code == _WEB_LINK_HAS_ACCOUNT:
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, update.effective_user.id,
                i18n.get('telegram.web_link.has_account', language), language,
            )
            return ConversationHandler.END

        key = 'telegram.web_link.expired' if code in _WEB_LINK_DEAD_CODES else 'telegram.web_link.failed'
        await context.bot.send_message(chat_id=update.effective_chat.id, text=i18n.get(key, language))
        return ConversationHandler.END

    async def _retire_web_link_card(self, query, text: Optional[str] = None, **kwargs) -> None:
        """Answer a tap on the confirm card and take its buttons away.

        Both halves are tidy-up: a refused ack or edit (an old card, a deleted
        message) never costs what the tap asked for.
        """
        await self._ack(query, text, **kwargs)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            logger.info("Could not drop the web-link card's buttons (cosmetic): %s", exc)

    async def _older_card_tapped(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Answer a tap on an older confirm card as stale, and say so.

        After `/start web_A` then `/start web_B`, card A still shows its buttons
        but names account A, while link B is the one waiting. The buttons carry
        no token (D5), so the card's message id tells them apart: only the
        newest card answers (R4), and it stays live.
        """
        query = update.callback_query
        pending = context.user_data.get(WEB_LINK_PENDING_KEY)
        if not pending or pending['message_id'] == getattr(query.message, 'message_id', None):
            return False
        await self._retire_web_link_card(
            query, i18n.get('telegram.web_link.expired', pending['language']), show_alert=True
        )
        return True

    async def web_link_yes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Yes on the confirm card: connect this Telegram to the web account (R4, R5)."""
        try:
            if await self._older_card_tapped(update, context):
                return WEB_LINK_CONFIRM
            await self._retire_web_link_card(update.callback_query)
            pending = context.user_data.pop(WEB_LINK_PENDING_KEY, None)
            user = update.effective_user

            if not pending:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=i18n.get('telegram.web_link.expired', self._web_link_language(update)),
                )
                return ConversationHandler.END

            async with api_client as client:
                response = await client.web_link_confirm(
                    user.id, pending['token'],
                    username=user.username, first_name=user.first_name, last_name=user.last_name,
                )

            if not response.success:
                existing = await self.user_repo.get_user_by_telegram_id(user.id)
                return await self._web_link_refused(
                    update, context, self._response_error_code(response),
                    existing=existing,
                    language=self._web_link_language(update, pending=pending, existing=existing),
                )

            data = response.data.get('data', {}) if isinstance(response.data, dict) else {}
            tokens = data.get('tokens') or {}
            token_manager = context.bot_data.get('token_manager') if context.bot_data else None
            if token_manager:
                await token_manager.store_tokens(
                    user.id,
                    tokens['access_token'],
                    tokens['refresh_token'],
                    tokens.get('expires_in', 3600)
                )
            # The cached row (if any) predates the link.
            user_cache.remove(user.id)
            logger.info("Telegram %s connected to a web account by link", user.id)

            return await self._after_web_link(update, context, data)

        except Exception as e:
            logger.error("Error in web_link_yes: %s", e, exc_info=True)
            return ConversationHandler.END

    async def web_link_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel on the confirm card: nothing is connected, and no language card follows."""
        try:
            if await self._older_card_tapped(update, context):
                return WEB_LINK_CONFIRM
            await self._retire_web_link_card(update.callback_query)
            pending = context.user_data.pop(WEB_LINK_PENDING_KEY, None)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=i18n.get('telegram.web_link.cancelled', self._web_link_language(update, pending=pending)),
            )
        except Exception as e:
            logger.error("Error in web_link_cancel: %s", e, exc_info=True)
        return ConversationHandler.END

    async def web_link_timeout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Registration's TIMEOUT while the confirm card waits (D15b): drop the token, say so.

        Never registration's "Send /start" copy: that /start would put an
        unknown Telegram on the language card, whose tap creates the row that
        blocks the next link. bot.py routes the TIMEOUT here while the link is
        pending.
        """
        try:
            pending = context.user_data.pop(WEB_LINK_PENDING_KEY, None)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=i18n.get('telegram.web_link.expired', self._web_link_language(update, pending=pending)),
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception as e:
            logger.error("Error announcing the web-link timeout: %s", e)
        return ConversationHandler.END

    async def web_link_step_expired(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Answer a confirm-card button whose conversation is gone (D15c).

        After a restart, the timeout or a plain /start the card still shows its
        buttons, but the token went with the conversation. Registered in the
        default group (bot.py), like `_signup_step_expired` for the account-link
        answers; it lives here so every web-link message is in one module.
        """
        query = update.callback_query
        try:
            row = await self.user_repo.get_user_by_telegram_id(update.effective_user.id)
            language = self._web_link_language(update, existing=row)
            await self._retire_web_link_card(
                query, i18n.get('telegram.web_link.expired', language), show_alert=True
            )
        except Exception as e:
            logger.warning("Could not answer an expired web-link button: %s", e)

    async def _after_web_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict) -> int:
        """After Yes: greet now, or first settle the account's phone (R5, D12).

        Confirm publishes `phone_status`. A verified number is greeted at once.
        A number never verified is confirmed by SMS; no number at all gets
        today's contact-share step, where a number another account holds gets
        the usual merge offer (R8). Either of those ends with the greeting, which
        waits under WEB_LINK_GREETING_KEY, unless a merge ends it with its own
        success message. The update is the Yes tap, so every message here goes
        out with send_message.
        """
        language = self._web_link_language(update, pending={'language': data.get('language')})
        greeting = {'name': data.get('greeting_name'), 'language': language}
        chat_id = update.effective_chat.id

        if data.get('phone_status') == 'verified':
            return await self._send_web_link_greeting(context, chat_id, update.effective_user.id, greeting)

        context.user_data[WEB_LINK_GREETING_KEY] = greeting
        if data.get('phone_status') == 'missing':
            await context.bot.send_message(
                chat_id=chat_id,
                text=i18n.get('telegram.registration.share_contact_prompt', language),
                reply_markup=ProfileKeyboards.phone_request(language),
            )
            return PHONE
        return await self._confirm_number_on_file(update, context, data.get('phone'), language)

    async def _confirm_number_on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      phone: str, language: str) -> int:
        """Confirm the number already on the account with an SMS code (D12).

        Profile → Phone → Verify's endpoints (/auth/send-otp now, /auth/verify-phone
        in REGISTER_OTP), authenticated with the link's own tokens. Never the
        contact-share keyboard: a shared contact would silently replace the
        number on file. A code that cannot be sent still ends with the greeting;
        the number stays unverified, as it was.
        """
        chat_id = update.effective_chat.id
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            response = await client.send_phone_verification(user_token, phone) if user_token else None

        if response is not None and response.success:
            sent = response.data.get('data', {}) if isinstance(response.data, dict) else {}
            context.user_data['pending_phone_verification'] = phone
            await context.bot.send_message(
                chat_id=chat_id,
                text=i18n.get(
                    'telegram.web_link.confirm_number_prompt',
                    language,
                    phone_masked=sent.get('phone_masked', phone),
                ),
            )
            return REGISTER_OTP

        await context.bot.send_message(
            chat_id=chat_id,
            text=i18n.get(
                'telegram.phone.verification_sms_send_failed',
                language,
                error=(response.error if response is not None else None) or '',
            ),
        )
        return await self._send_web_link_greeting(
            context, chat_id, update.effective_user.id, context.user_data.pop(WEB_LINK_GREETING_KEY)
        )

    async def _send_web_link_greeting(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                      telegram_id: int, greeting: dict) -> int:
        """R5: "Connected, {name}!" with the bottom keyboard, in the website account's language."""
        language = greeting['language']
        name = greeting['name'] or i18n.get('telegram.common.user_fallback', language)
        await send_menu_keyboard(
            context.bot, chat_id, telegram_id,
            i18n.get('telegram.web_link.connected', language, name=name), language,
        )
        return ConversationHandler.END

    async def _finish_signup(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str) -> int:
        """The last message once signup has its phone, and END.

        After a web link (D12) that is the greeting the link deferred, in the
        account's language; otherwise today's "Registration completed".
        """
        greeting = context.user_data.pop(WEB_LINK_GREETING_KEY, None)
        if greeting:
            return await self._send_web_link_greeting(
                context, update.effective_chat.id, update.effective_user.id, greeting
            )

        await send_menu_keyboard(
            context.bot, update.effective_chat.id, update.effective_user.id,
            i18n.get('telegram.registration_complete', language), language,
        )
        return ConversationHandler.END

    async def _number_taken(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str) -> int:
        """The shared number belongs to an account this Telegram may not claim.

        The backend refuses (`can_link` false) when that account has its own
        Telegram, is not active, may not be claimed by phone, or is one person
        across phones, which the merge itself would refuse. Mid-signup that is
        today's prompt for another number. After a web link (D12) this Telegram
        already holds the customer's own account, which has no phone yet, so the
        customer is asked for a phone again exactly as /start does for a
        phoneless account (spec 2026-10-21 §1). The greeting is left waiting, so
        an accepted number still ends in `_finish_signup`'s greeting; a
        greeting without a phone would be a dead end.
        """
        greeting = context.user_data.get(WEB_LINK_GREETING_KEY)
        if greeting:
            await update.message.reply_text(
                i18n.get('telegram.registration.share_contact_prompt', greeting['language']),
                reply_markup=ProfileKeyboards.phone_request(greeting['language']),
            )
            return PHONE

        await update.message.reply_text(
            i18n.get('telegram.phone.already_linked_other_account', language),
            reply_markup=ProfileKeyboards.phone_request(language)
        )
        return PHONE

    async def language_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection during registration"""
        try:
            query = update.callback_query
            user = update.effective_user
            user_id = user.id
            # Index 2, never [-1]: `set_language_<lang>` may carry a
            # `_ref<code>` suffix (keyboards.LanguageKeyboards.select_language),
            # which is what lets a deep-link referral survive the deploy that
            # empties user_data before this row exists.
            language_code = query.data.split('_')[2]
            logger.info(f"User {user_id} (@{user.username}) register started with language: {language_code}")

            # Validate language code
            if language_code not in config.localization.supported_languages:
                await self._ack(query, i18n.get('telegram.registration.invalid_language_selection', 'en'))
                return SELECT_LANGUAGE  # Stay in language selection state

            user_repo = BotUserRepository(db_manager)

            existing_user = await user_repo.get_user_by_telegram_id(user_id)
            logger.info(f"existing_user {existing_user} for user_id {user_id}")

            if not existing_user:
                try:
                    async with api_client as client:
                        registration_data = {
                            'first_name': user.first_name,
                            'last_name': user.last_name,
                            'username': user.username,
                            'language_code': language_code
                        }
                        # Apply a referral code captured from a /start deep link.
                        referral_code = (context.user_data or {}).get('referral_code')
                        if not referral_code and '_ref' in query.data:
                            referral_code = query.data.split('_ref', 1)[1] or None
                        if referral_code:
                            registration_data['referral_code'] = referral_code
                        # Attach an AI/profile acquisition marker captured from a
                        # /start deep link (e.g. ?start=ai_chatgpt). The backend
                        # ignores unknown keys today (and INFO-logs the payload);
                        # it will persist this once users.acquisition_source
                        # ships with task AG-M1 (AEO roadmap WS1).
                        acquisition_source = (context.user_data or {}).get('acquisition_source')
                        if acquisition_source:
                            registration_data['acquisition_source'] = acquisition_source
                        response = await client.register_telegram_user(user_id, registration_data)
                        if not response.success:
                            logger.error(f"Failed to register telegram user {user_id}: {response.error}")
                            await self._ack(query, i18n.get('telegram.registration.failed_toast', language_code))
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=i18n.get('telegram.registration.failed_contact_support', language_code)
                            )
                            return ConversationHandler.END

                        # Replace any stale cached token with the fresh token
                        # returned for the newly created telegram user.
                        response_payload = response.data.get('data', {}) if isinstance(response.data, dict) else {}
                        tokens = response_payload.get('tokens', {}) if isinstance(response_payload, dict) else {}
                        if tokens.get('access_token') and tokens.get('refresh_token'):
                            token_manager = context.bot_data.get('token_manager') if context.bot_data else None
                            if token_manager:
                                await token_manager.store_tokens(
                                    user_id,
                                    tokens['access_token'],
                                    tokens['refresh_token'],
                                    tokens.get('expires_in', 3600)
                                )
                                logger.info(f"Cached fresh registration tokens for user {user_id}")

                        # Referral / acquisition params (if any) have now been
                        # consumed by the backend.
                        if context.user_data is not None:
                            context.user_data.pop('referral_code', None)
                            context.user_data.pop('acquisition_source', None)
                except Exception as e:
                    logger.error(f"Exception during telegram user registration: {e}")
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    await self._ack(query, i18n.get('telegram.registration.failed_toast', language_code))
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=i18n.get('telegram.registration.failed_try_start', language_code)
                    )
                    return ConversationHandler.END
            else:
                # Update user's preferred language
                await self.user_repo.update_user_language(user_id, language_code)
                await self._ack(query, i18n.get('telegram.registration.language_updated_toast', language_code))

            # Proceed to phone number input
            phone_text = i18n.get('telegram.registration.enter_phone', language_code)
            keyboard = ProfileKeyboards.phone_request(language_code)

            # Send the phone request message first
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=phone_text,
                reply_markup=keyboard
            )

            # Then try to delete the old language selection message
            try:
                await query.delete_message()
            except Exception as del_error:
                logger.warning(f"Could not delete language selection message: {del_error}")

            return PHONE

        except Exception as e:
            logger.error(f"Error in language selection: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def phone_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number from contact - checks for duplicates and offers linking"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            contact = update.message.contact

            if contact.user_id != user_id:
                await update.message.reply_text(
                    i18n.get('telegram.registration.share_own_contact', language),
                    reply_markup=ReplyKeyboardRemove()
                )
                return PHONE

            phone = normalize_phone_number(contact.phone_number)
            if not phone:
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE

            # Check if phone is available via API
            try:
                async with api_client as client:
                    response = await client.check_phone_availability(user_id, phone)

                    # Extract nested data - API returns {'data': {...}, 'success': True}
                    response_data = response.data.get('data', {}) if response.data else {}

                    if response.success and response_data.get('available'):
                        # Contact sharing is trusted by Telegram, mark phone as verified
                        await self.user_repo.set_user_phone_verified(user_id, phone)

                        # Clean up the temporary phone-share keyboard before moving on.
                        await update.message.reply_text(
                            i18n.get('telegram.phone.phone_accepted', language),
                            reply_markup=ReplyKeyboardRemove()
                        )

                        # Registration complete
                        logger.info(f"Registration completed for user {user_id}")
                        return await self._finish_signup(update, context, language)

                    elif response.success and not response_data.get('available'):
                        # Phone exists - check if linking is possible
                        available = response_data.get('available', False)
                        can_link = response_data.get('can_link', False)
                        existing_user = response_data.get('existing_user_masked', {})

                        logger.info(f"Phone check for user {user_id}: available={available}, can_link={can_link}, existing_user={existing_user}")

                        if can_link:
                            # Store phone for linking
                            context.user_data['pending_link_phone'] = phone

                            # Clean up the temporary phone-share keyboard before next inline step.
                            await update.message.reply_text(
                                i18n.get('telegram.phone.phone_accepted', language),
                                reply_markup=ReplyKeyboardRemove()
                            )

                            # Show linking option
                            masked_name = existing_user.get('name', '***') if existing_user else '***'

                            link_text = i18n.get(
                                'telegram.phone.already_registered_link_prompt',
                                language,
                                masked_name=masked_name
                            )

                            keyboard = KeyboardBuilder.build_inline_keyboard([
                                [{'text': i18n.get('telegram.phone.link_yes_button', language), 'callback_data': "link_yes"}],
                                [{'text': i18n.get('telegram.phone.link_no_button', language), 'callback_data': "link_no"}]
                            ])

                            await update.message.reply_text(
                                text=link_text,
                                reply_markup=keyboard
                            )

                            return LINK_ACCOUNT_CONFIRM
                        else:
                            # Cannot link: the number's account may not be claimed (see _number_taken)
                            return await self._number_taken(update, context, language)
                    else:
                        # API error
                        logger.error(f"Failed to check phone availability: {response.error}")
                        await update.message.reply_text(
                            i18n.get('telegram.phone.verify_unavailable', language),
                            reply_markup=ProfileKeyboards.phone_request(language)
                        )
                        return PHONE

            except Exception as api_error:
                logger.error(f"API error checking phone: {api_error}")
                await update.message.reply_text(
                    i18n.get('telegram.phone.verify_unavailable_now', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE

        except Exception as e:
            logger.error(f"Error handling phone: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

            # Check if it's a duplicate key error
            if 'duplicate key' in str(e).lower() or 'unique constraint' in str(e).lower():
                language = await i18n.get_user_language(update.effective_user.id)
                await update.message.reply_text(
                    i18n.get('telegram.phone.already_registered_use_different', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE

            return ConversationHandler.END

    async def phone_text_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle phone number as text"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            phone_text = update.message.text.strip()

            if not await validate_phone_number(phone_text):
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language)
                )
                return PHONE

            phone = normalize_phone_number(phone_text)

            # Check if phone is available via API
            try:
                async with api_client as client:
                    response = await client.check_phone_availability(user_id, phone)

                    # Extract nested data - API returns {'data': {...}, 'success': True}
                    response_data = response.data.get('data', {}) if response.data else {}

                    if response.success and response_data.get('available'):
                        # For text input, phone must be OTP-verified
                        user_token = await get_auth_token(
                            update,
                            context,
                            client,
                            force_refresh=True
                        )
                        if not user_token:
                            await update.message.reply_text(
                                i18n.get('telegram.auth.failed_try_again', language),
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE

                        # The code is captured in REGISTER_OTP (register_otp_received).
                        return await self._send_typed_phone_code(
                            update, context, client, user_token, phone, language,
                            phone_state=PHONE, code_state=REGISTER_OTP, registered=False,
                        )

                    elif response.success and not response_data.get('available'):
                        # Phone exists - check if linking is possible
                        available = response_data.get('available', False)
                        can_link = response_data.get('can_link', False)
                        existing_user = response_data.get('existing_user_masked', {})

                        logger.info(f"Phone check for user {user_id}: available={available}, can_link={can_link}, existing_user={existing_user}")

                        if can_link:
                            # Store phone for linking
                            context.user_data['pending_link_phone'] = phone

                            # Remove the share contact keyboard first
                            await update.message.reply_text(
                                i18n.get('telegram.phone.phone_accepted', language),
                                reply_markup=ReplyKeyboardRemove()
                            )

                            # Show linking option
                            masked_name = existing_user.get('name', '***') if existing_user else '***'

                            link_text = i18n.get(
                                'telegram.phone.already_registered_link_prompt',
                                language,
                                masked_name=masked_name
                            )

                            keyboard = KeyboardBuilder.build_inline_keyboard([
                                [{'text': i18n.get('telegram.phone.link_yes_button', language), 'callback_data': "link_yes"}],
                                [{'text': i18n.get('telegram.phone.link_no_button', language), 'callback_data': "link_no"}]
                            ])

                            await update.message.reply_text(
                                text=link_text,
                                reply_markup=keyboard
                            )

                            return LINK_ACCOUNT_CONFIRM
                        else:
                            # Cannot link: the number's account may not be claimed (see _number_taken)
                            return await self._number_taken(update, context, language)
                    else:
                        # API error
                        logger.error(f"Failed to check phone availability: {response.error}")
                        await update.message.reply_text(
                            i18n.get('telegram.phone.verify_unavailable', language),
                            reply_markup=ProfileKeyboards.phone_request(language)
                        )
                        return PHONE

            except Exception as api_error:
                logger.error(f"API error checking phone: {api_error}")
                await update.message.reply_text(
                    i18n.get('telegram.phone.verify_unavailable_now', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE

        except Exception as e:
            logger.error(f"Error handling phone text: {e}")

            # Check if it's a duplicate key error
            if 'duplicate key' in str(e).lower() or 'unique constraint' in str(e).lower():
                language = await i18n.get_user_language(update.effective_user.id)
                await update.message.reply_text(
                    i18n.get('telegram.phone.already_registered_use_different', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE

            return ConversationHandler.END

    async def link_account_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user's choice to link or cancel account linking"""
        try:
            query = update.callback_query
            # Cosmetic: a refused ack ("query is too old", routine when a
            # redeploy redelivers a backlog) must not abort the branch below
            # and drop the customer out of signup.
            await self._ack(query)

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            callback_data = query.data

            if callback_data == "link_yes":
                # User wants to link - send OTP
                phone = context.user_data.get('pending_link_phone')

                if not phone:
                    await self._edit_or_replace_callback_message(
                        query,
                        i18n.get('telegram.phone.session_expired_share_again', language),
                    )
                    return PHONE

                # Rate limit OTP requests
                if not await otp_rate_limiter.allow_otp_request(user_id):
                    await self._edit_or_replace_callback_message(
                        query,
                        i18n.get('telegram.phone.too_many_verification_attempts', language),
                    )
                    return PHONE

                # Call API to send OTP
                try:
                    async with api_client as client:
                        response = await client.link_phone_send_otp(user_id, phone)

                        if response.success:
                            # Backend wraps payloads as {success, message, data:{...}}.
                            link_payload = response.data.get('data', {}) if isinstance(response.data, dict) else {}
                            phone_masked = link_payload.get('phone_masked', phone)
                            await self._edit_or_replace_callback_message(
                                query,
                                i18n.get(
                                    'telegram.phone.verification_code_sent_to_phone_prompt',
                                    language,
                                    phone_masked=phone_masked
                                ),
                            )
                            return LINK_ACCOUNT_OTP
                        else:
                            error_msg = response.error or i18n.get(
                                'telegram.phone.verification_code_send_failed_default',
                                language
                            )
                            await self._edit_or_replace_callback_message(
                                query,
                                i18n.get(
                                    'telegram.phone.verification_code_send_failed_retry_or_different',
                                    language,
                                    error=error_msg
                                ),
                            )
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=i18n.get('telegram.phone.share_phone_using_button', language),
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE

                except Exception as api_error:
                    logger.error(f"API error sending OTP: {api_error}")
                    await self._edit_or_replace_callback_message(
                        query,
                        i18n.get('telegram.phone.verification_code_send_failed_generic', language),
                    )
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=i18n.get('telegram.phone.share_phone_using_button', language),
                        reply_markup=ProfileKeyboards.phone_request(language)
                    )
                    return PHONE

            elif callback_data == "link_no":
                # User wants to use different phone
                context.user_data.pop('pending_link_phone', None)

                # Rewriting the link question is cosmetic; the share-phone
                # keyboard below is the whole step. A refused edit used to take
                # both with it and end registration, leaving the customer
                # looking at the link question with no keyboard and no way on.
                await self._edit_or_replace_callback_message(
                    query,
                    i18n.get('telegram.phone.share_different_phone_prompt', language),
                )

                # Send keyboard for phone sharing
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=i18n.get('telegram.phone.share_phone_using_button', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )

                return PHONE

            return self._end_link_step(context)

        except Exception as e:
            logger.error(f"Error in link_account_confirm: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return self._end_link_step(context)

    @staticmethod
    def _end_link_step(context) -> int:
        """Every END of the phone-link steps: a web link's deferred greeting never outlives them.

        A merge ends with its own success message, and any other END with its own copy; a greeting
        left behind would end a later, unrelated phone step with "Connected" (R8, T6-M6).
        """
        context.user_data.pop(WEB_LINK_GREETING_KEY, None)
        return ConversationHandler.END

    async def link_account_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle OTP verification for account linking"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            otp = otp_code_in(update.message.text)

            # Validate OTP format
            if otp is None:
                await update.message.reply_text(
                    i18n.get('telegram.phone.enter_valid_6_digit_code', language)
                )
                return LINK_ACCOUNT_OTP

            phone = context.user_data.get('pending_link_phone')
            if not phone:
                await update.message.reply_text(
                    i18n.get('telegram.phone.session_expired_start_again', language)
                )
                return self._end_link_step(context)

            # Call API to verify OTP and link accounts
            try:
                async with api_client as client:
                    response = await client.link_phone_verify(user_id, otp)

                    if response.success:
                        # Account linked successfully! It ends with the merge's own
                        # message, never a web link's deferred greeting (R8).
                        context.user_data.pop('pending_link_phone', None)
                        context.user_data.pop(WEB_LINK_GREETING_KEY, None)

                        # Backend wraps payloads as {success, message, data:{...}}.
                        verify_payload = response.data.get('data', {}) if isinstance(response.data, dict) else {}

                        # Update cached tokens with the merged account's tokens
                        new_tokens = verify_payload.get('tokens', {})
                        if new_tokens.get('access_token') and new_tokens.get('refresh_token'):
                            token_manager = context.bot_data.get('token_manager')
                            if token_manager:
                                await token_manager.store_tokens(
                                    user_id,
                                    new_tokens['access_token'],
                                    new_tokens['refresh_token'],
                                    new_tokens.get('expires_in', 3600)
                                )
                                logger.info(f"Updated cached tokens after account merge for user {user_id}")

                        user_data = verify_payload.get('user', {})
                        # One naming rule for the bot and the email (spec 2026-10-09 §7.3): first
                        # name, company for an entity, never "Telegram User" and never "None". The
                        # full name is joined from its parts: the body's own `full_name` comes from
                        # `User.to_dict()`, which prints a missing part as "None" ("None None").
                        name = greeting_name(
                            first_name=user_data.get('first_name'),
                            full_name=joined_name(user_data.get('first_name'), user_data.get('last_name')),
                            company_name=user_data.get('company_name'),
                            is_entity=user_data.get('user_type') == UserType.ENTITY.value,
                        ) or i18n.get('telegram.common.user_fallback', language)

                        await send_menu_keyboard(
                            context.bot, update.effective_chat.id, update.effective_user.id,
                            i18n.get('telegram.phone.accounts_linked_success', language, name=name),
                            language,
                        )

                        logger.info(f"Account linking completed for user {user_id}")
                        return self._end_link_step(context)
                    else:
                        error_msg = response.error or i18n.get('telegram.phone.invalid_verification_code_default', language)

                        if self._response_error_code(response) in _LINK_CODE_DEAD_CODES:
                            await update.message.reply_text(
                                i18n.get('telegram.phone.verification_code_expired_start_again', language)
                            )
                            return self._end_link_step(context)
                        else:
                            await update.message.reply_text(
                                i18n.get('telegram.phone.verification_failed_with_error_retry', language, error=error_msg)
                            )
                            return LINK_ACCOUNT_OTP

            except Exception as api_error:
                logger.error(f"API error verifying OTP: {api_error}")
                await update.message.reply_text(
                    i18n.get('telegram.phone.verification_failed_retry', language)
                )
                return LINK_ACCOUNT_OTP

        except Exception as e:
            logger.error(f"Error in link_account_otp: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return self._end_link_step(context)

    async def register_otp_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verify the registration OTP from WITHIN the conversation (REGISTER_OTP).

        This is the in-conversation counterpart to the legacy global
        bot._handle_otp_verification catch-all. It verifies the code via the
        REGISTRATION endpoint (client.verify_phone_otp) — NOT the account-merge
        endpoint (client.link_phone_verify), which would be wrong/dangerous on
        the fresh-registration path.

        DRY note: the verify core is _receive_typed_phone_code, shared with
        Profile → Phone (phone_verify_otp_received). It is deliberately NOT
        shared with bot._handle_otp_verification: that method is on the bot
        class in bot.py while this is on ProfileHandlers in profile.py, so a
        shared helper would force awkward cross-class/cross-module coupling for
        a ~3-line verify call, and the surrounding success/error UX
        (conversation states, main-menu keyboard, re-prompt vs. END) differs
        entirely between the two paths.
        """
        async def _registration_complete(update, language):
            # Same completion the contact path uses.
            return await self._finish_signup(update, context, language)

        return await self._receive_typed_phone_code(
            update, context,
            code_state=REGISTER_OTP,
            on_verified=_registration_complete,
            missing_key='telegram.phone.session_expired_start_again',
            expired_key='telegram.phone.verification_code_expired_start_again',
        )

    async def _receive_typed_phone_code(self, update, context, *, code_state, on_verified,
                                        missing_key, expired_key):
        """Verify the code sent by _send_typed_phone_code, inside a conversation.

        /verify-phone is the only writer of a typed number: it saves the number
        the code was sent to, and only when the code matches. A wrong code or a
        backend error re-prompts in ``code_state``; an unrecoverable one says
        ``missing_key`` / ``expired_key`` and ends. On success the pending
        number is dropped and ``on_verified(update, language)`` sends the
        flow's own copy and returns its next state.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            otp = otp_code_in(update.message.text)

            # Validate OTP format. Only registration's REGISTER_OTP reaches this
            # retry prompt: PHONE_VERIFY_OTP's state filter sends anything that is
            # not a code to `end_phone_code_step` instead.
            if otp is None:
                await update.message.reply_text(
                    i18n.get('telegram.bot.otp.invalid_format', language)
                )
                return code_state

            # Defensive: if the pending phone is missing (stale/restarted state),
            # end cleanly instead of verifying against no pending context
            # (mirrors link_account_otp's missing-phone guard).
            if not context.user_data.get('pending_phone_verification'):
                await update.message.reply_text(i18n.get(missing_key, language))
                return ConversationHandler.END

            try:
                async with api_client as client:
                    user_token = await get_auth_token(update, context, client)
                    if not user_token:
                        await update.message.reply_text(
                            i18n.get('telegram.bot.otp.auth_error', language)
                        )
                        context.user_data.pop('pending_phone_verification', None)
                        return ConversationHandler.END

                    # Correct verify endpoint (NOT link_phone_verify).
                    response = await client.verify_phone_otp(user_token, otp)

                    if response.success:
                        context.user_data.pop('pending_phone_verification', None)
                        logger.info(f"Phone verification OTP verified for user {user_id}")
                        return await on_verified(update, language)

                    error_msg = response.error or i18n.get(
                        'telegram.phone.invalid_verification_code_default', language
                    )

                    # Expired / not-found: the code is unrecoverable, so end.
                    if 'expired' in error_msg.lower() or 'not found' in error_msg.lower():
                        await update.message.reply_text(i18n.get(expired_key, language))
                        context.user_data.pop('pending_phone_verification', None)
                        return ConversationHandler.END

                    # Otherwise re-prompt so the user can retry the code.
                    await update.message.reply_text(
                        i18n.get(
                            'telegram.phone.verification_failed_with_error_retry',
                            language,
                            error=error_msg
                        )
                    )
                    return code_state

            except Exception as api_error:
                logger.error(f"API error verifying phone verification OTP: {api_error}")
                await update.message.reply_text(
                    i18n.get('telegram.phone.verification_failed_retry', language)
                )
                return code_state

        except Exception as e:
            logger.error(f"Error in _receive_typed_phone_code: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def continue_registration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Continue registration after phone sharing"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Check if user already has full profile
            user_data = await self.user_repo.get_user_by_telegram_id(user_id)
            if user_data and user_data.get('full_name'):
                # Already registered, show main menu
                await self.profile_menu(update, context)
                return

            # Ask for name
            name_text = i18n.get('telegram.registration.enter_name', language)
            await update.message.reply_text(name_text)

        except Exception as e:
            logger.error(f"Error continuing registration: {e}")

    async def cancel_registration(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel registration process"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # A web link still waiting for Yes, or its deferred greeting, goes too.
            for key in (WEB_LINK_PENDING_KEY, WEB_LINK_GREETING_KEY):
                context.user_data.pop(key, None)

            # One message. `send_menu_keyboard` takes the phone keyboard down for an
            # account with no row or no phone, which is every registration state but
            # a web-linked account that already has its number.
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id,
                i18n.get('telegram.action_cancelled', language), language,
            )

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error canceling registration: {e}")
            return ConversationHandler.END

    async def edit_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the profile field-edit sub-menu (Name / Birthday / Language / Phone)."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Opening the sub-menu abandons the name/birthday prompt it offers;
            # nothing else armed is ours to clear.
            await self.user_repo.clear_awaiting_input(user_id, *_PROFILE_EDIT_PROMPTS)

            await self._edit_or_replace_callback_message(
                query,
                i18n.get('telegram.profile.edit_menu_title', language),
                reply_markup=ProfileKeyboards.profile_edit_menu(language)
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_profile")

    async def edit_profile_name_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for a new name and arm the contextual-input completer."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await self._edit_or_replace_callback_message(
                query,
                i18n.get('telegram.profile.name_prompt', language),
                reply_markup=MenuKeyboards.cancel_button(language)
            )
            await self._ack(query)

            await self.user_repo.arm_awaiting_input(user_id, 'edit_profile_name')

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_profile_name_prompt")

    async def handle_profile_name_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       text: str, user_state: Dict):
        """Complete a profile name edit: split first-token/remainder and PUT to backend."""
        language = 'en'
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            cleaned = text.strip()

            if len(cleaned) < 2:
                await update.message.reply_text(i18n.get('telegram.name.too_short', language))
                return
            if not any(c.isalpha() for c in cleaned):
                await update.message.reply_text(i18n.get('telegram.name.invalid', language))
                return

            parts = cleaned.split()
            first_name = parts[0]
            last_name = ' '.join(parts[1:]) if len(parts) > 1 else ''

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.update_user_profile(
                    user_token, {'first_name': first_name, 'last_name': last_name}
                )

            if response.success:
                await update.message.reply_text(
                    text=i18n.get('telegram.profile.name_updated', language),
                    reply_markup=ProfileKeyboards.profile_edit_menu(language)
                )
                await self.user_repo.clear_awaiting_input(user_id, 'edit_profile_name')
                logger.info(f"Profile name updated for user {user_id}: {first_name} {last_name}")
            else:
                await update.message.reply_text(
                    i18n.get('telegram.error_occurred', language)
                )
                logger.warning(f"Failed to update profile name for {user_id}: {response.error}")

        except Exception as e:
            logger.error(f"Error handling profile name edit: {e}")
            await update.message.reply_text(i18n.get('telegram.error_occurred', language))

    async def edit_profile_birthday_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt for a birthday in DD-MM-YYYY text format and arm the contextual-input completer."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await self._edit_or_replace_callback_message(
                query,
                i18n.get('telegram.profile.birthday_prompt', language),
                reply_markup=MenuKeyboards.cancel_button(language)
            )
            await self._ack(query)

            await self.user_repo.arm_awaiting_input(user_id, 'edit_profile_birthday')

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_profile_birthday_start")

    async def handle_profile_birthday_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                           text: str, user_state: Dict):
        """Complete a profile birthday edit: parse DD-MM-YYYY, convert to ISO, PUT to backend."""
        language = 'en'
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            try:
                parsed = datetime.strptime(text.strip(), "%d-%m-%Y")
            except ValueError:
                await update.message.reply_text(
                    i18n.get('telegram.profile.birthday_invalid_format', language)
                )
                # Keep state so the user can retry
                return

            iso_date = parsed.strftime("%Y-%m-%d")

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.update_user_profile(user_token, {'date_of_birth': iso_date})

            if response.success:
                await update.message.reply_text(
                    text=i18n.get('telegram.profile.birthday_updated', language),
                    reply_markup=ProfileKeyboards.profile_edit_menu(language)
                )
                await self.user_repo.clear_awaiting_input(user_id, 'edit_profile_birthday')
                logger.info(f"Profile birthday updated for user {user_id}: {iso_date}")
            else:
                await update.message.reply_text(
                    i18n.get('telegram.profile.birthday_update_failed', language)
                )
                # Keep state so the user can retry after backend rejection (e.g. too young/old)
                logger.warning(f"Failed to update birthday for {user_id}: {response.error}")

        except Exception as e:
            logger.error(f"Error handling profile birthday edit: {e}")
            await update.message.reply_text(i18n.get('telegram.error_occurred', language))

    async def manage_addresses(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address management"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # The address list is where an abandoned title/instructions edit
            # ends up; disarm those and leave every other flow armed.
            await self.user_repo.clear_awaiting_input(user_id, *_ADDRESS_EDIT_PROMPTS)

            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                addresses = response.data.get('data', {}).get('addresses', [])

            if not addresses:
                addresses_text = i18n.get('telegram.address.no_addresses', language)
                keyboard = ProfileKeyboards.empty_addresses(language)
                logger.info(f"No addresses found, showing empty addresses keyboard")
            else:
                addresses_text = i18n.get('telegram.address.list_header', language, count=len(addresses))
                for i, addr in enumerate(addresses, 1):
                    status = "🏠" if addr.get('is_default') else "📍"
                    addresses_text += f"{status} {addr.get('title', i18n.get('telegram.address.title_fallback', language, index=i))}\n"
                    addresses_text += f"   {addr.get('full_address', i18n.get('telegram.address.no_address_placeholder', language))}\n\n"

                # Create proper address management keyboard
                keyboard = ProfileKeyboards.addresses_management(addresses, language)
                logger.info(f"Found {len(addresses)} addresses, showing management keyboard")

            await self._edit_or_replace_callback_message(
                query, addresses_text, reply_markup=keyboard
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="manage_addresses")

    async def _persist_address_draft(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, step: str
    ) -> None:
        """Dual-write the in-flight `temp_address_data` onto `bot_state`.

        SDD 2026-08-26-address-flow-bot-state, Task 6 (P2): every handler
        below that mutates `temp_address_data` calls this right after making
        its change, naming the step the flow is moving TO. Reads still come
        entirely from `context.user_data` — nothing about how this flow runs
        changes in this task; the durable copy exists purely so a later phase
        can resume it after a restart or an interruption. Deliberately never
        arms `awaiting_input` — see `BotUserRepository.save_address_draft`'s
        docstring for why arming here, before P3a wires up the read path,
        would route an escaped text update into `bot.py`'s
        "invalid input" branch instead of the flow.

        BEST-EFFORT, on purpose: `context.user_data` stays the live source of
        truth for THIS conversation (nothing reads `address_draft` back yet),
        so a failure writing the durable twin — a DB hiccup, a disconnected
        pool — must never abort a flow the customer is actively standing in.
        Every step handler already wraps its own body the identical way
        (`except Exception: ... return ConversationHandler.END`) for the same
        reason. Logged, not silent, so a real regression still surfaces.

        `address_id` is excluded from the `data` snapshot: it already has its
        own field below (`save_address_draft`'s `address_id=` kwarg is the
        single source of truth a resume reads it from), so carrying it a
        second time inside `data` would let the two silently disagree if a
        future edit ever wrote one without the other (M3, final whole-branch
        review).
        """
        try:
            addr_data = context.user_data.get('temp_address_data') or {}
            snapshot = {k: v for k, v in addr_data.items() if k != 'address_id'}
            await self.user_repo.save_address_draft(
                update.effective_user.id,
                step=step,
                data=snapshot,
                address_id=addr_data.get('address_id'),
                origin=context.user_data.get('address_flow_origin'),
            )
        except Exception as e:
            logger.warning(f"Could not persist address draft at step {step!r}: {e}")

    async def add_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start address adding process - entry point for enhanced address flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Deliberately does NOT clear `bot_state`. That clear was a guard
            # against an armed `awaiting_input` eating this flow's typed
            # answers, and `WaterBusinessBot._consumes` now stops every
            # conversation step in group -2, so the guard is obsolete — while
            # the damage was not: a customer who tapped "Report an issue" and
            # then came here had their armed concern report silently thrown
            # away, with its prompt and Cancel button still on screen saying a
            # report was open. Arming is DB-backed precisely so it survives
            # until the customer sends it or cancels it.
            logger.info(f"=== ADD ADDRESS CONVERSATION ENTRY POINT ===")
            logger.info(f"User: {user_id}")
            if update.callback_query:
                logger.info(f"Callback data: {update.callback_query.data}")
            logger.info(f"Starting add address conversation for user {user_id}")

            # Preserve source so successful save can route users back into checkout.
            if update.callback_query and update.callback_query.data == 'add_new_address_checkout':
                context.user_data['address_flow_origin'] = 'checkout'
            else:
                context.user_data.pop('address_flow_origin', None)

            # Initialize temp address data
            context.user_data['temp_address_data'] = {}
            await self._persist_address_draft(update, context, 'location')

            # Use enhanced location request with skip option
            location_text = i18n.get('telegram.address.location_prompt_enhanced', language)
            keyboard = ProfileKeyboards.location_request(
                language,
                extra_rows=(i18n.get('telegram.address.enter_manually_button', language),),
            )
            await arm_location_request(context, update.effective_user.id)

            if update.callback_query:
                logger.info(f"Editing message via callback query")
                query = update.callback_query
                if self._is_callback_message_deletable(query):
                    # Deletion is non-critical UI cleanup.
                    await self._delete_callback_message(query)
                else:
                    logger.info("Skipping callback message deletion in add_address: message not deletable by policy")

                await self._ack(query)
                # Send keyboard in new message
                if query.message:
                    await query.message.reply_text(
                        text=location_text,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                else:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=location_text,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                logger.info(f"Callback query processed and keyboard sent")
            else:
                logger.info(f"Replying to message directly")
                await update.message.reply_text(
                    text=location_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )

            logger.info(f"Address conversation started, returning ADDRESS_LOCATION state ({ADDRESS_LOCATION})")
            return ADDRESS_LOCATION

        except Exception as e:
            logger.error(f"Error starting add address: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def location_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle location sharing for address - primary path with reverse geocoding"""
        logger.info(f"=== LOCATION_RECEIVED METHOD CALLED ===")

        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not update.message or not update.message.location:
                logger.error(f"ERROR: No location in message!")
                return ConversationHandler.END

            location = update.message.location
            logger.info(f"Location received: lat={location.latitude}, lng={location.longitude}")

            # Enforce the delivery-zone SSOT (TASHKENT_POLYGON) before accepting.
            # The backend re-validates authoritatively; this gives instant, localized UX.
            if not is_within_tashkent(location.latitude, location.longitude):
                logger.info(
                    f"User {user_id} shared out-of-zone location: "
                    f"{location.latitude}, {location.longitude}"
                )
                await arm_location_request(context, update.effective_user.id)
                await update.message.reply_text(
                    i18n.get('telegram.address.outside_delivery_area', language),
                    reply_markup=ProfileKeyboards.location_request(
                        language,
                        extra_rows=(i18n.get('telegram.address.enter_manually_button', language),),
                    )
                )
                return ADDRESS_LOCATION

            # Store location in temp address data
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}

            context.user_data['temp_address_data']['latitude'] = location.latitude
            context.user_data['temp_address_data']['longitude'] = location.longitude
            context.user_data['temp_address_data']['location_source'] = 'shared'

            # Attempt reverse geocoding
            reverse_geocoded_address = None
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    response = await client.reverse_geocode(user_token, location.latitude, location.longitude)
                    if response.success and response.data.get('data'):
                        reverse_geocoded_address = response.data['data'].get('formatted_address')
                        context.user_data['temp_address_data']['full_address'] = reverse_geocoded_address
                        logger.info(f"Reverse geocoded address: {reverse_geocoded_address}")

            await self._persist_address_draft(update, context, 'title')

            # The menu replaces the location keyboard
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id,
                i18n.get('telegram.address.location_received', language), language,
            )

            # Ask for address title with suggestions.
            #
            # The geocoded address is DATA, not markup, so it is escaped before
            # it goes anywhere near `parse_mode='Markdown'`. Unescaped, a street
            # whose name carries a `_`, `*`, `[` or backtick made Telegram
            # refuse the whole message; the handler's `except Exception` then
            # returned ConversationHandler.END, so the customer saw "location
            # received" and nothing else — deterministically, forever, for
            # everyone living on that street. The prefix copy itself carries
            # deliberate `*bold*`, which is why the parse mode stays.
            title_prompt = i18n.get('telegram.address.title_prompt', language)
            if reverse_geocoded_address:
                title_prompt = _markdown_copy(
                    'telegram.address.detected_location_prefix',
                    language,
                    address=reverse_geocoded_address,
                ) + title_prompt

            keyboard = ProfileKeyboards.address_title_suggestions(language)

            await self._reply_markdown_or_plain(update.message, title_prompt, keyboard)

            logger.info(f"Transitioning to ADDRESS_TITLE state")
            return ADDRESS_TITLE

        except Exception as e:
            logger.error(f"CRITICAL ERROR in location_received: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return await self._end_flow_with_menu(update, context)

    async def address_title_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address title typed as free text"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            title = update.message.text.strip()

            logger.info(f"User {user_id} entered title: {title}")

            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}
            context.user_data['temp_address_data']['title'] = title
            await self._persist_address_draft(update, context, 'title')

            # The title step sits at a different position in each flow: a
            # shared pin asks for the title early, right after the location,
            # so titling here still has apartment/floor/instructions ahead
            # of it. A manually typed address only reaches this step LAST,
            # after geocode confirmation, so titling here is the save.
            if _is_shared_pin_address(context):
                await self._create_address_now(update, context, language)
                return await self._prompt_address_step(update, context, language, 'apartment')

            return await self.save_address_final(update, context)

        except Exception as e:
            logger.error(f"Error handling address title: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def clear_address_flow_keys(
        self, context: ContextTypes.DEFAULT_TYPE, telegram_id: int,
        *, touch_activity: bool = True,
    ) -> None:
        """Clear every key an address-creation flow may leave behind, both in
        `user_data` and in the durable twin now shadowing it.

        Public because the flow exit (`flow_exit.leave_all_flows`, spec
        2026-10-21 §2) runs it too: leaving the form for a menu label or a
        command is a teardown like any other, and like the timeout it keeps an
        address the pin step already saved (only Cancel discards one).

        `touch_activity=False` is for `address_flow_timeout` ONLY: a timeout
        fires on a synthetic PTB update, not a customer action, so clearing
        the draft there must not stamp `last_bot_interaction` (M1, final
        whole-branch review — see `BotUserRepository.clear_address_draft`'s
        docstring for the incident). Every other teardown (cancel,
        cancel-text, a completed save, a flow exit) is a real customer action
        and keeps the default.

        One list for the `user_data` keys, `flow_exit.ADDRESS_FLOW_KEYS`, so a
        future addition can never again be forgotten at one of the flow's
        several teardown sites the way `awaiting_location` originally was: it
        was armed by `utils.arm_location_request` but only ever cleared by
        `_route_address_location_entry` popping it off an ARRIVING pin
        (bot.py) — every path that ends the flow WITHOUT a pin (cancel,
        text-cancel, timeout, or a successful save) left it `True` forever,
        so a customer who armed the checkout keyboard and then backed out
        had every later, unrelated pin misrouted into address creation
        instead of the support inbox. Call this at every such teardown site.

        Also clears `BotUserRepository`'s `address_draft` (SDD
        2026-08-26-address-flow-bot-state, Task 6) for the identical reason:
        a cancelled, timed-out, or completed flow must not leave a draft
        behind for a later phase to wrongly resume into once nothing is
        actually mid-conversation any more. Turned this from a `@staticmethod`
        into an instance method for exactly this — `self.user_repo` and a
        `telegram_id` are both needed to reach the database.

        The database call is BEST-EFFORT for the same reason
        `_persist_address_draft` is: the `user_data` pops above are what
        actually end the flow the customer is standing in, so a failure
        clearing the durable twin must not stop those, or surface as a crash
        on top of an otherwise-successful cancel/save.
        """
        for name in ADDRESS_FLOW_KEYS:
            context.user_data.pop(name, None)
        try:
            await self.user_repo.clear_address_draft(telegram_id, touch_activity=touch_activity)
        except Exception as e:
            logger.warning(f"Could not clear address draft for {telegram_id}: {e}")
        # The pin arming now has a DURABLE twin as well, so this teardown has
        # two halves too. Clearing only the `user_data` pop above would
        # reintroduce the exact 2026-08-26 regression through the new channel:
        # a customer who armed the checkout pin prompt and then cancelled would
        # have every later, unrelated pin swept back into address creation —
        # this time for as long as the row lives, not just the process.
        # tests/telegram_bot/test_support_attachment_dispatch.py
        try:
            await self.user_repo.forget_pin_prompt(telegram_id)
        except Exception as e:
            logger.warning(f"Could not clear the pin prompt for {telegram_id}: {e}")

    async def cancel_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel address adding process"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)

            # Cancel is the one exit that means "I do not want this address",
            # so it has to undo the row the pin flow created back at the title
            # step. Done BEFORE the temp data is popped — the id lives there.
            await self._discard_created_address(update, context)

            # Handle both message and callback query. An edit carries no menu: the
            # bottom keyboard is the way on (spec 2026-10-21 §1).
            if update.callback_query:
                await self._ack(update.callback_query)
                await self._edit_or_replace_callback_message(update.callback_query, cancel_text)
            else:
                await send_menu_keyboard(
                    context.bot, update.effective_chat.id, user_id, cancel_text, language
                )

            # Clear all temporary address data
            await self.clear_address_flow_keys(context, user_id)

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error canceling address: {e}")
            return ConversationHandler.END

    async def cancel_address_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel address adding from the location keyboard's Cancel row: the menu replaces it."""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)

            # Read before the pop below consumes it.
            origin = context.user_data.get('address_flow_origin')

            # Same contract as the inline Cancel: an explicit cancel undoes the
            # row the pin flow created at the title step.
            await self._discard_created_address(update, context)

            if origin == 'checkout':
                # The customer has a full cart. Dropping them on the main menu
                # makes them navigate back for no reason. Return to the CART,
                # not to checkout — checkout would re-render the very
                # zero-address prompt they just cancelled, which is a loop.
                # The menu still replaces the location keyboard first.
                from handlers.products import product_handlers

                await send_menu_keyboard(
                    context.bot, update.effective_chat.id, user_id,
                    i18n.get('telegram.action_cancelled_short', language), language,
                )
                await product_handlers.show_cart(update, context)
            else:
                await send_menu_keyboard(
                    context.bot, update.effective_chat.id, user_id, cancel_text, language
                )

            # Clear all temporary address data
            await self.clear_address_flow_keys(context, user_id)

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error canceling address from text: {e}")
            return ConversationHandler.END

    async def address_flow_timeout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The address conversation hit `conversation_timeout` and is over.

        Without this the flow expires in total silence: the customer is left
        staring at a prompt whose buttons no longer do anything, and the flow's
        keys survive it in `user_data` — a stale ``address_flow_origin ==
        'checkout'`` then hijacks the NEXT, unrelated address save and bounces
        that customer into checkout.

        A timeout is not a cancel: an address already created by the pin flow
        stays. Only the in-flight flow state is dropped.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            addr_data = context.user_data.get('temp_address_data') or {}
            saved_address_id = addr_data.get('address_id')

            # touch_activity=False: this fires on a SYNTHETIC PTB update, not
            # something the customer did — see `clear_address_flow_keys`'s
            # docstring (M1, final whole-branch review).
            await self.clear_address_flow_keys(context, user_id, touch_activity=False)

            logger.info(
                f"Address flow timed out for user {user_id} "
                f"(saved address: {saved_address_id or 'none'})"
            )

            text = i18n.get(
                'telegram.address.flow_timed_out_saved' if saved_address_id
                else 'telegram.address.flow_timed_out',
                language,
            )

            # A timeout arrives as a synthetic update carrying whatever the last
            # real one was, so its chat may be missing.
            chat = update.effective_chat
            await send_menu_keyboard(
                context.bot, chat.id if chat is not None else user_id, user_id, text, language
            )

        except Exception as e:
            logger.error(f"Error in address_flow_timeout: {e}")

        return ConversationHandler.END

    # ==================== MANUAL ADDRESS ENTRY HANDLERS ====================

    async def skip_location_sharing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle skip location - start manual entry flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            logger.info(f"User {user_id} chose manual address entry")

            # Initialize temp address data if not exists
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}
            context.user_data['temp_address_data']['location_source'] = 'manual'
            await self._persist_address_draft(update, context, 'region')

            # The menu replaces the location keyboard
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id,
                i18n.get('telegram.address.manual_entry_started', language), language,
            )

            # Show region selection (only Tashkent for now)
            region_prompt = i18n.get('telegram.address.select_region', language)
            keyboard = ProfileKeyboards.region_selection(language)

            await update.message.reply_text(
                region_prompt,
                reply_markup=keyboard
            )

            return ADDRESS_REGION

        except Exception as e:
            logger.error(f"Error in skip_location_sharing: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return await self._end_flow_with_menu(update, context)

    async def region_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle region selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract region from callback data
            region = query.data.replace('region_', '')
            logger.info(f"User {user_id} selected region: {region}")

            # Store region
            if 'temp_address_data' not in context.user_data:
                context.user_data['temp_address_data'] = {}
            context.user_data['temp_address_data']['region'] = region
            context.user_data['temp_address_data']['city'] = 'Tashkent'
            await self._persist_address_draft(update, context, 'district')

            await self._ack(query)

            # Show district selection
            district_prompt = i18n.get('telegram.address.select_district', language)
            districts = get_all_districts(language)
            keyboard = ProfileKeyboards.district_selection(districts, language)

            await self._edit_or_replace_callback_message(
                query, district_prompt, reply_markup=keyboard
            )

            return ADDRESS_DISTRICT

        except Exception as e:
            logger.error(f"Error in region_selected: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def back_to_region(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle back button from district selection - go back to region selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await self._ack(query)
            logger.info(f"User {user_id} going back to region selection")

            # Show region selection again
            region_prompt = i18n.get('telegram.address.select_region', language)
            keyboard = ProfileKeyboards.region_selection(language)

            await self._edit_or_replace_callback_message(
                query, region_prompt, reply_markup=keyboard
            )

            return ADDRESS_REGION

        except Exception as e:
            logger.error(f"Error in back_to_region: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def district_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle district selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract district from callback data
            district_key = query.data.replace('district_', '')
            district_name = get_district_name(district_key, language)
            logger.info(f"User {user_id} selected district: {district_key} ({district_name})")

            # Store district
            context.user_data['temp_address_data']['district'] = district_key
            context.user_data['temp_address_data']['district_name'] = district_name

            # Get district center for geocoding hint
            center = get_district_center(district_key)
            context.user_data['temp_address_data']['hint_lat'] = center[0]
            context.user_data['temp_address_data']['hint_lon'] = center[1]
            await self._persist_address_draft(update, context, 'street')

            await self._ack(query)

            # Ask for street name (required, no skip option)
            street_prompt = escape_markdown(
                i18n.get('telegram.address.enter_street_required', language, district_name=district_name),
                version=2
            )
            # No Skip keyboard - street is required. It still needs an escape:
            # this state matches any typed text as the answer for up to 24h
            # (`conversation_timeout`), with no menu on screen to fall back to
            # (see C1, keyboards.py::ProfileKeyboards._cancel_address_row).

            # A refused edit ("message to edit not found" — the customer
            # deleted the bubble) used to unwind into `except Exception` and
            # end the flow in silence: the street they then typed reached a bot
            # that was no longer listening and was filed as a support ticket.
            await self._edit_or_replace_callback_message(
                query,
                street_prompt,
                reply_markup=ProfileKeyboards.required_field_keyboard(language),
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )

            return ADDRESS_STREET

        except Exception as e:
            logger.error(f"Error in district_selected: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    @staticmethod
    def _build_address_payload(addr_data: Dict[str, Any], language: str) -> Dict[str, Any]:
        """The CREATE payload for an address, from whatever the flow collected.

        SSOT for the two moments an address can be born: a shared pin creates
        it the instant it has a title, a manually typed one still creates it at
        the end, after geocode confirmation. One builder so the two cannot
        drift into writing different columns.
        """
        payload = {
            'title': addr_data.get('title', i18n.get('telegram.address.default_title', language)),
            'full_address': addr_data.get('full_address', ''),
            'street_address': addr_data.get('street_address'),
            'city': addr_data.get('city', i18n.get('telegram.address.default_city', language)),
            'district': addr_data.get('district'),
            'latitude': addr_data.get('latitude'),
            'longitude': addr_data.get('longitude'),
            'apartment_number': addr_data.get('apartment_number'),
            'floor_number': addr_data.get('floor_number'),
            'delivery_instructions': addr_data.get('delivery_instructions'),
        }
        return {k: v for k, v in payload.items() if v is not None}

    @staticmethod
    def _detail_payload(addr_data: Dict[str, Any]) -> Dict[str, Any]:
        """The UPDATE payload for an address that already exists.

        Deliberately NOT None-filtered, unlike the create payload: Skip clears
        a value, and the backend's updater only touches keys the payload
        carries, so an omitted key would silently mean "leave it alone" when
        the customer meant "there is none".
        """
        return {key: addr_data.get(key) for key in _ADDRESS_DETAIL_KEYS}

    async def _create_address_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  language: str):
        """Write the address the moment it is deliverable, and remember its id.

        A shared pin plus a title IS a deliverable address. Everything the flow
        asks afterwards renders a Skip button — the flow itself calls those
        fields optional — yet holding the address until the customer answers
        them lost 20 of 33 pin flows in the 30 days to 2026-08-21 (Loki). So it
        is created here and merely ENRICHED later.

        Returns the new id, or None when the write could not happen. None is
        not fatal: the terminal step still creates the address the old way, so
        a transient backend failure degrades to the previous behaviour instead
        of stranding the customer in a chain that can never commit.

        CORRECTS rather than duplicates when this flow already created a row.
        `location_received` is an ENTRY POINT and the conversation sets
        `allow_reentry=True`, so PTB re-enters on a second pin even mid-flow: a
        customer who spots a bad pin and drops a better one arrives back here
        with an address already in hand. They are moving that address, not
        adding another — and the first pin's row must not be left behind at the
        wrong coordinates for a driver to deliver to.
        """
        addr_data = context.user_data.setdefault('temp_address_data', {})
        existing_id = addr_data.get('address_id')
        payload = self._build_address_payload(addr_data, language)

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                logger.warning("No auth token; deferring address create to the end of the flow")
                return None

            if existing_id:
                response = await client.update_user_address(user_token, existing_id, payload)
            else:
                response = await client.add_user_address(user_token, payload)

        if not response.success:
            logger.error(
                f"Early address {'update' if existing_id else 'create'} failed "
                f"({response.error}); deferring to the end of the flow"
            )
            return None

        if existing_id:
            addr_data[_DETAIL_SYNC_SNAPSHOT_KEY] = self._detail_payload(addr_data)
            logger.info(f"Address {existing_id} moved to the corrected pin")
            return existing_id

        address = ((response.data or {}).get('data') or {}).get('address') or {}
        address_id = address.get('id')
        if address_id is None:
            logger.error("Address created but the response carried no id; deferring enrichment")
            return None

        addr_data['address_id'] = address_id
        addr_data[_DETAIL_SYNC_SNAPSHOT_KEY] = self._detail_payload(addr_data)
        logger.info(f"Address {address_id} created early for user {update.effective_user.id}")
        return address_id

    async def _sync_address_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Push the optional details collected so far onto the created address.

        Called after every optional step so an answer the customer HAS given
        survives them abandoning the next one. No-ops when the address does not
        exist yet (manual entry, or an early create that failed) and when the
        detail set is unchanged, so a run of Skips costs no HTTP at all.
        """
        addr_data = context.user_data.get('temp_address_data') or {}
        address_id = addr_data.get('address_id')
        if not address_id:
            return False

        payload = self._detail_payload(addr_data)
        if payload == addr_data.get(_DETAIL_SYNC_SNAPSHOT_KEY):
            return True

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                logger.warning(f"No auth token; address {address_id} keeps its previous details")
                return False

            response = await client.update_user_address(user_token, address_id, payload)

        if not response.success:
            logger.error(f"Failed to enrich address {address_id}: {response.error}")
            return False

        addr_data[_DETAIL_SYNC_SNAPSHOT_KEY] = payload
        return True

    async def _discard_created_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete the address an explicitly CANCELLED pin flow already created.

        Cancel is the only exit that means "I do not want this address". Timing
        out or walking away does not — the customer dropped a pin and named it,
        and that address is theirs to keep.
        """
        addr_data = context.user_data.get('temp_address_data') or {}
        address_id = addr_data.get('address_id')
        if not address_id:
            return

        try:
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    logger.error(f"No auth token; cancelled address {address_id} was left behind")
                    return

                response = await client.delete_user_address(user_token, address_id)

            if response.success:
                logger.info(f"Deleted cancelled address {address_id}")
            else:
                logger.error(f"Failed to delete cancelled address {address_id}: {response.error}")
        except Exception as e:
            logger.error(f"Error deleting cancelled address {address_id}: {e}")

    def _send_for(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> RenderTarget:
        """The delivery target for `update`, as a `RenderTarget`.

        Both address flows converge on this chain, and every step is
        reachable from either a typed answer (message) or a Skip tap
        (callback), so the delivery target is derived from the update rather
        than passed by each caller.

        `send_text`'s callback branch keeps `_edit_or_replace_callback_message`
        because a REFUSED edit is a rendering problem, not a flow problem: a
        bare edit used to unwind into
        `except Exception: return ConversationHandler.END`, so Telegram's most
        benign rejection ("message is not modified") left the customer looking
        at a correct prompt whose Skip button was wired to nothing.

        `send_markdown_or_plain` and `send_location` always operate on a
        MESSAGE (the callback's own `query.message` when there is one,
        `update.message` otherwise) rather than editing in place — matching
        `geocode_and_confirm`, whose pin and confirmation bubble are new
        messages below whatever bubble carried the Skip button that led here,
        not an edit of it.

        `request_location` cannot edit-in-place either: Telegram does not let
        `editMessageText` attach a `ReplyKeyboardMarkup` (only
        `InlineKeyboardMarkup`), which is exactly why `add_address` always
        SENDS a fresh message for this prompt. It calls `arm_location_request`
        first — this closure's own site for it, baked into the transport so a
        caller reaching this RenderTarget cannot forget it the way a
        hand-rolled call site could (`utils.arm_location_request`). It is not
        the only arm site: six call `arm_location_request` in total — this one
        (this method's `request_location`), `profile.py::add_address`,
        `profile.py::location_received` (out-of-zone re-prompt),
        `profile.py::geocode_and_confirm` (out-of-zone re-prompt),
        `profile.py::retry_geocode`, and `orders.py::checkout_handler`
        (zero-address checkout) — each arming its own location prompt
        separately, for the same reason. Cited by function name, not line
        number: this exact list has already rotted FOUR times now from a
        docstring edit shifting the very lines it cited (M2, final
        whole-branch review) — a line number in a cross-file citation does
        not survive an edit to the file it points into. `request_location`
        then acks and replies to the callback's own message when there is
        one, falling back to `context.bot.send_message` when a callback
        arrived with no message attached, mirroring `add_address`'s own
        fallback.
        """
        query = update.callback_query

        async def send_text(text, keyboard=None, parse_mode=None):
            if query is not None:
                await self._edit_or_replace_callback_message(
                    query, text, reply_markup=keyboard, parse_mode=parse_mode
                )
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)

        async def send_markdown_or_plain(text, keyboard=None):
            message = query.message if query is not None else update.message
            await self._reply_markdown_or_plain(message, text, keyboard)

        async def send_location(latitude, longitude):
            message = query.message if query is not None else update.message
            await message.reply_location(latitude=latitude, longitude=longitude)

        async def request_location(text, keyboard):
            await arm_location_request(context, update.effective_user.id)
            if query is not None:
                await self._ack(query)
                if query.message:
                    await query.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_user.id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode='Markdown',
                    )
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='Markdown')

        return SimpleNamespace(
            send_text=send_text,
            send_markdown_or_plain=send_markdown_or_plain,
            send_location=send_location,
            request_location=request_location,
        )

    async def _prompt_address_step(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str, field: str
    ):
        """Render one step's prompt AND dual-write the draft moving to it.

        Every caller of this method — `street_received`, `building_received`,
        `apartment_received`, `floor_received`, `skip_field_handler`'s
        non-terminal branch, and the pin branch of `address_title_received` /
        `address_title_callback` — passes the step the flow is advancing to,
        so a single `_persist_address_draft` call here covers all of them
        instead of repeating it at each call site (they only ever call this
        with a field in `_ADDRESS_STEPS`: building/apartment/floor/
        delivery_instructions). Persisted BEFORE rendering so the durable
        draft reflects reality even if sending the prompt itself later fails.

        `data` is passed through to `render_prompt` on every call, not just
        the fields that currently need it. I3 (final whole-branch review):
        this used to call `render_prompt(field, language, target)` with no
        `data` at all — every existing caller only ever names a field in
        `_ADDRESS_STEPS`, none of which read `data`, so it was silently fine
        today. But `_render_street` and `_render_geocode_confirm`
        (`handlers/address_flow.py`) both raise `ValueError` without it, and
        every caller here is wrapped in `except Exception: return
        ConversationHandler.END` — a future caller that reaches this method
        with `field='street'` or `'geocode_confirm'` (a Task 4 draft-resume
        driver, say) would silently kill the customer's conversation with no
        message at all. Passing it unconditionally costs nothing for today's
        callers and closes that trap for tomorrow's.
        """
        await self._persist_address_draft(update, context, field)
        data = context.user_data.get('temp_address_data') or {}
        return await render_prompt(field, language, self._send_for(update, context), data)

    async def _reject_overlong_detail(self, update: Update, language: str, field: str):
        """Re-prompt the same step when an answer is too long for its column.

        Without this the value reaches Postgres as a DataError, the save 500s,
        and the customer loses every answer they gave.
        """
        step = _ADDRESS_STEPS[field]

        await update.message.reply_text(
            i18n.get(
                'telegram.address.field_too_long',
                language,
                max_length=ADDRESS_DETAIL_MAX_LENGTH,
            ),
            reply_markup=step.keyboard(language),
        )

        return step.state

    async def street_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle street name input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            street = update.message.text.strip()

            logger.info(f"User {user_id} entered street: {street}")
            next_field = handle_input('street', street, context.user_data['temp_address_data'])

            return await self._prompt_address_step(update, context, language, next_field)

        except Exception as e:
            logger.error(f"Error in street_received: {e}")
            return ConversationHandler.END

    async def building_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle building number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            building = update.message.text.strip()

            logger.info(f"User {user_id} entered building: {building}")
            next_field = handle_input('building', building, context.user_data['temp_address_data'])

            return await self._prompt_address_step(update, context, language, next_field)

        except Exception as e:
            logger.error(f"Error in building_received: {e}")
            return ConversationHandler.END

    async def apartment_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle apartment number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            apartment = update.message.text.strip()

            if len(apartment) > ADDRESS_DETAIL_MAX_LENGTH:
                logger.info(f"User {user_id} gave an over-long apartment ({len(apartment)} chars)")
                return await self._reject_overlong_detail(update, language, 'apartment')

            logger.info(f"User {user_id} entered apartment: {apartment}")
            next_field = handle_input('apartment', apartment, context.user_data['temp_address_data'])

            await self._sync_address_details(update, context)
            return await self._prompt_address_step(update, context, language, next_field)

        except Exception as e:
            logger.error(f"Error in apartment_received: {e}")
            return ConversationHandler.END

    async def floor_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle floor number input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            floor = update.message.text.strip()

            if len(floor) > ADDRESS_DETAIL_MAX_LENGTH:
                logger.info(f"User {user_id} gave an over-long floor ({len(floor)} chars)")
                return await self._reject_overlong_detail(update, language, 'floor')

            logger.info(f"User {user_id} entered floor: {floor}")
            next_field = handle_input('floor', floor, context.user_data['temp_address_data'])

            await self._sync_address_details(update, context)
            return await self._prompt_address_step(update, context, language, next_field)

        except Exception as e:
            logger.error(f"Error in floor_received: {e}")
            return ConversationHandler.END

    async def delivery_instructions_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle delivery instructions input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            instructions = update.message.text.strip()

            logger.info(f"User {user_id} entered delivery instructions")
            # Terminal step (handle_input returns None here): what comes next
            # is a save, not another prompt, so the return value has no
            # `_prompt_address_step` call to feed.
            handle_input('delivery_instructions', instructions, context.user_data['temp_address_data'])
            await self._persist_address_draft(update, context, 'delivery_instructions')

            # A shared pin is already an exact coordinate, so it saves straight
            # away; a manually typed address still has to be geocoded first.
            if _is_shared_pin_address(context):
                logger.info(f"Location already set from sharing, saving address directly")
                return await self.save_address_final(update, context)
            else:
                # Manual entry flow - proceed to geocoding and confirmation
                return await self.geocode_and_confirm(update, context)

        except Exception as e:
            logger.error(f"Error in delivery_instructions_received: {e}")
            return ConversationHandler.END

    async def skip_field_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the Skip button on an optional address field"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            field_name = query.data.replace('skip_', '')
            logger.info(f"User {user_id} skipped field: {field_name}")

            # Cosmetic, and deliberately not allowed to abort the step: a stale
            # tap answered after Telegram's ~60s window used to take the whole
            # Skip with it (no state change, no prompt, no error).
            await self._ack(query)

            # Skip means "I have no value for this" — for the field tapped AND
            # for every field the jump lands past. retry_geocode reruns the
            # whole chain, so an answer typed before the retry would otherwise
            # survive the Skip and still be saved.
            addr_data = context.user_data.get('temp_address_data', {})
            for data_key in _cleared_by_skip(field_name):
                addr_data.pop(data_key, None)

            if field_name == 'delivery_instructions':
                # Terminal step. A shared pin is already an exact coordinate, so
                # it saves straight away; a manually typed address still has to
                # be geocoded and confirmed first.
                await self._persist_address_draft(update, context, 'delivery_instructions')
                if _is_shared_pin_address(context):
                    logger.info("Location already set from sharing, saving address directly")
                    return await self.save_address_final(update, context, is_callback=True)
                return await self.geocode_and_confirm(update, context, is_callback=True)

            next_field = _SKIP_TARGETS.get(field_name)
            if next_field is None:
                logger.warning(f"Unknown field skipped: {field_name}")
                return ConversationHandler.END

            # A Skip that CLEARS a value already written to the backend (only
            # reachable via retry_geocode) has to clear it there too; a Skip
            # over a field that was never answered changes nothing and costs
            # no HTTP call.
            await self._sync_address_details(update, context)
            return await self._prompt_address_step(update, context, language, next_field)

        except Exception as e:
            logger.error(f"Error in skip_field_handler: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def geocode_and_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  is_callback: bool = False):
        """Geocode the manually typed address and show the confirmation pin.

        ONE implementation for both ways a customer reaches this step: TYPING
        the delivery instructions (a message) or TAPPING Skip on them (a
        callback). They used to be two near-identical functions, and the copy
        behind the Skip button had silently lost the `is_within_tashkent`
        guard — so which of two equivalent buttons the customer pressed decided
        whether the delivery-zone SSOT was enforced at all. The Skip customer
        was shown an out-of-zone pin as if it were fine, named it, and only
        then got the generic "could not save" from the backend backstop.

        The only difference left between the two entry points is where the
        confirmation is delivered, which is derived here rather than copied.
        """
        try:
            query = update.callback_query if is_callback else None
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            addr_data = context.user_data.get('temp_address_data', {})

            if query is not None:
                await self._ack(query, i18n.get('telegram.common.processing', language))

            # Build address string for geocoding
            address_parts = []
            if addr_data.get('street_address'):
                address_parts.append(f"{addr_data['street_address']} street")
            if addr_data.get('building_number'):
                address_parts.append(addr_data['building_number'])
            if addr_data.get('district_name'):
                address_parts.append(addr_data['district_name'])
            address_parts.append('Tashkent, Uzbekistan')

            address_string = ', '.join(address_parts)
            logger.info(f"Geocoding address: {address_string}")

            # Attempt geocoding
            geocode_success = False
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    hint_lat = addr_data.get('hint_lat')
                    hint_lon = addr_data.get('hint_lon')

                    response = await client.geocode_address(
                        user_token, address_string, hint_lat, hint_lon
                    )

                    if response.success and response.data.get('data'):
                        geo_data = response.data['data']
                        addr_data['latitude'] = geo_data.get('latitude')
                        addr_data['longitude'] = geo_data.get('longitude')
                        addr_data['full_address'] = geo_data.get('formatted_address', address_string)
                        geocode_success = True
                        logger.info(f"Geocoding successful: {addr_data['latitude']}, {addr_data['longitude']}")

            # If geocoding failed, use district center as fallback
            if not geocode_success:
                logger.warning(f"Geocoding failed, using district center as fallback")
                district_key = addr_data.get('district', 'yunusabad')
                center = get_district_center(district_key)
                addr_data['latitude'] = center[0]
                addr_data['longitude'] = center[1]
                addr_data['full_address'] = address_string

            # Persisted so a RESUMED draft's `geocode_confirm` render
            # (`_render_geocode_confirm`, address_flow.py) can reproduce the
            # same safety disclosure `confirm_text` gets below
            # (`geocode_note_approximate_center`) — without a producer here,
            # `data['approximate']` stays unset forever and that warning
            # silently never reaches a customer confirming a fallback pin.
            addr_data['approximate'] = not geocode_success

            context.user_data['temp_address_data'] = addr_data

            # The bubble that carried the Skip button is spent either way: what
            # comes next (a pin, then a confirmation, or the out-of-zone
            # re-prompt) arrives as new messages below it.
            if query is not None:
                await self._delete_callback_message(query)

            target = query.message if query is not None else update.message

            # Enforce the delivery-zone SSOT (TASHKENT_POLYGON). The district-center
            # fallback is always in-zone; this guards against a geocoder returning a
            # point outside the coverage area. The backend re-validates authoritatively.
            final_lat = addr_data.get('latitude')
            final_lng = addr_data.get('longitude')
            if final_lat is not None and final_lng is not None and not is_within_tashkent(final_lat, final_lng):
                logger.info(f"User {user_id} geocoded to out-of-zone point: {final_lat}, {final_lng}")
                await arm_location_request(context, update.effective_user.id)
                await self._persist_address_draft(update, context, 'location')
                await target.reply_text(
                    i18n.get('telegram.address.outside_delivery_area', language),
                    reply_markup=ProfileKeyboards.location_request(
                        language,
                        extra_rows=(i18n.get('telegram.address.enter_manually_button', language),),
                    )
                )
                return ADDRESS_LOCATION

            # Send location pin for confirmation
            await target.reply_location(
                latitude=addr_data['latitude'],
                longitude=addr_data['longitude']
            )

            # Show confirmation message
            confirm_text = i18n.get(
                'telegram.address.geocode_found_with_address',
                language,
                address=addr_data.get('full_address', i18n.get('telegram.common.not_set', language))
            )
            if not geocode_success:
                confirm_text += i18n.get('telegram.address.geocode_note_approximate_center', language)

            keyboard = ProfileKeyboards.geocode_confirmation(language, show_edit=False)

            # The geocoder's formatted address is DATA inside a Markdown
            # message; a street name carrying `_` or `[` would otherwise be
            # refused and end the flow on the last step before the save.
            await self._reply_markdown_or_plain(target, confirm_text, keyboard)

            await self._persist_address_draft(update, context, 'geocode_confirm')
            return ADDRESS_GEOCODE_CONFIRM

        except Exception as e:
            logger.error(f"Error in geocode_and_confirm: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def confirm_geocode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """User confirms the geocoded location - proceed to title"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await self._ack(query, i18n.get('telegram.address.location_confirmed_toast', language))
            logger.info(f"User {user_id} confirmed geocoded location")

            # Ask for address title
            title_prompt = i18n.get('telegram.address.title_prompt', language)
            keyboard = ProfileKeyboards.address_title_suggestions(language)

            await self._edit_or_replace_callback_message(
                query, title_prompt, reply_markup=keyboard
            )

            return ADDRESS_TITLE

        except Exception as e:
            logger.error(f"Error in confirm_geocode: {e}")
            return ConversationHandler.END

    async def retry_geocode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """User says location is wrong - offer to share location or re-enter manually"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await self._ack(query, i18n.get('telegram.address.retry_location_toast', language))
            logger.info(f"User {user_id} says geocode is wrong, offering correction options")

            # Delete previous message with inline keyboard
            await self._delete_callback_message(query)

            # Keep temp address data but reset for potential location share
            if 'temp_address_data' in context.user_data:
                context.user_data['temp_address_data']['location_source'] = 'retry'
                await self._persist_address_draft(update, context, 'location')

            # Offer location sharing or manual re-entry
            retry_text = i18n.get('telegram.address.retry_location', language)

            await arm_location_request(context, update.effective_user.id)
            keyboard = ProfileKeyboards.location_request(
                language,
                extra_rows=(
                    i18n.get('telegram.address.reenter_manually_button', language),
                    i18n.get('telegram.cancel', language),
                ),
            )

            await query.message.reply_text(
                retry_text,
                reply_markup=keyboard
            )

            return ADDRESS_LOCATION  # Go back to location state to handle shared location

        except Exception as e:
            logger.error(f"Error in retry_geocode: {e}")
            return ConversationHandler.END

    async def address_title_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address title from callback (suggestions)"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract title from callback
            title_key = query.data.replace('addr_title_', '')
            titles = {
                'home': {'en': 'Home', 'uz': 'Uy', 'ru': 'Дом'},
                'work': {'en': 'Work', 'uz': 'Ish', 'ru': 'Работа'},
                'other': {'en': 'Other', 'uz': 'Boshqa', 'ru': 'Другое'}
            }
            title = titles.get(title_key, {}).get(language, title_key.capitalize())

            logger.info(f"User {user_id} selected title: {title}")
            context.user_data['temp_address_data']['title'] = title
            await self._persist_address_draft(update, context, 'title')

            await self._ack(query)

            # The title step sits at a different position in each flow: a
            # shared pin asks for the title early, right after the location,
            # so titling here still has apartment/floor/instructions ahead
            # of it. A manually typed address only reaches this step LAST,
            # after geocode confirmation, so titling here is the save.
            if _is_shared_pin_address(context):
                await self._create_address_now(update, context, language)
                return await self._prompt_address_step(update, context, language, 'apartment')

            return await self.save_address_final(update, context, is_callback=True)

        except Exception as e:
            logger.error(f"Error in address_title_callback: {e}")
            return ConversationHandler.END

    async def _resume_checkout_after_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Continue checkout flow after address is saved from checkout path."""
        from handlers.orders import order_handlers

        await order_handlers.checkout_handler(self.as_message_update(update), context)

    async def save_address_final(self, update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool = False):
        """Finish the address flow: create the address, or complete the one the
        flow already created.

        A shared pin creates its address back at the title step, so reaching
        here means the row exists and only its optional details are still
        outstanding. A manually typed address — and a pin whose early create
        failed — is still born here.

        A save that could not happen at all (no token, backend refusal) does
        NOT end the flow: the answers stay in `temp_address_data` and the
        conversation stays in ADDRESS_TITLE, so the customer retries with one
        tap instead of retyping seven answers.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            addr_data = context.user_data.get('temp_address_data', {})
            address_id = addr_data.get('address_id')

            if address_id:
                address_payload = self._detail_payload(addr_data)
                logger.info(f"Completing address {address_id} for user {user_id}: {address_payload}")
            else:
                address_payload = self._build_address_payload(addr_data, language)
                logger.info(f"Saving address for user {user_id}: {address_payload}")

            # Save via API
            success = False
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    if address_id:
                        response = await client.update_user_address(
                            user_token, address_id, address_payload
                        )
                    else:
                        response = await client.add_user_address(user_token, address_payload)
                    if response.success:
                        success = True
                        logger.info(f"Address saved successfully for user {user_id}")
                    elif address_id:
                        # The row already exists — this call was only adding
                        # OPTIONAL details. Telling the customer the save failed
                        # would send them through the whole flow again and leave
                        # them with a duplicate, which is worse than losing the
                        # delivery note.
                        success = True
                        logger.error(
                            f"Address {address_id} is saved but its final details "
                            f"were not applied: {response.error}"
                        )
                    else:
                        logger.error(f"Failed to save address: {response.error}")

            if not success:
                # Nothing was written, so nothing the customer answered may be
                # thrown away: keeping `temp_address_data` (and the checkout
                # origin) alive means retapping the title button retries the
                # save with all seven answers intact, instead of restarting the
                # whole flow. Same contract as the pin branch's failed early
                # create, which degrades rather than discarding.
                failure_text = i18n.get('telegram.address.save_failed', language)
                retry_keyboard = ProfileKeyboards.address_title_suggestions(language)

                if is_callback:
                    await self._edit_or_replace_callback_message(
                        update.callback_query, failure_text, reply_markup=retry_keyboard
                    )
                else:
                    await update.message.reply_text(
                        text=failure_text,
                        reply_markup=retry_keyboard
                    )

                return ADDRESS_TITLE

            resume_checkout_after_save = context.user_data.get('address_flow_origin') == 'checkout'

            # Clear temp data
            await self.clear_address_flow_keys(context, user_id)

            if resume_checkout_after_save:
                await self._resume_checkout_after_address(update, context)
                return ConversationHandler.END

            success_text = i18n.get('telegram.address.saved_successfully', language)

            if is_callback:
                # A refused edit is a rendering problem: the address IS saved,
                # and losing this confirmation sends the customer round the
                # flow again and leaves them with a duplicate. An edit carries
                # no menu: the bottom keyboard is the way on.
                await self._edit_or_replace_callback_message(update.callback_query, success_text)
            else:
                await send_menu_keyboard(
                    context.bot, update.effective_chat.id, update.effective_user.id, success_text, language
                )

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in save_address_final: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def view_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View specific address details"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # This screen IS the Cancel button of the title/instructions edit
            # prompts, so reaching it disarms them — and only them.
            await self.user_repo.clear_awaiting_input(user_id, *_ADDRESS_EDIT_PROMPTS)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Get address details from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                addresses = response.data.get('data', {}).get('addresses', [])
                address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)

                if not address:
                    await self._ack(query, i18n.get('telegram.address.not_found', language))
                    return

            # Format address details. Every value below is DATA the customer or
            # a geocoder wrote, and this screen is rendered as Markdown, so it
            # goes through `_markdown_copy` — an address carrying `_` used to
            # make Telegram refuse the message and the customer could not view,
            # edit or delete their own address.
            address_text = _markdown_copy(
                'telegram.address.details_title',
                language,
                title=address.get('title', i18n.get('telegram.address.untitled', language))
            )
            address_text += _markdown_copy(
                'telegram.address.details_full_address',
                language,
                address=address.get('full_address', i18n.get('telegram.common.not_set', language))
            )
            if address.get('street_address'):
                address_text += _markdown_copy('telegram.address.details_street', language, street=address.get('street_address'))
            if address.get('city'):
                address_text += _markdown_copy('telegram.address.details_city', language, city=address.get('city'))
            if address.get('is_default'):
                address_text += i18n.get('telegram.address.details_default_badge', language)

            # Create action buttons for this address
            buttons = [
                [
                    {'text': i18n.get('telegram.address.edit', language), 'callback_data': f'edit_address_{address_id}'},
                    {'text': i18n.get('telegram.address.delete', language), 'callback_data': f'delete_address_{address_id}'}
                ]
            ]

            if not address.get('is_default'):
                buttons.insert(0, [{'text': i18n.get('telegram.address.set_default', language), 'callback_data': f'set_default_address_{address_id}'}])

            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'manage_addresses'}])

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            await self._edit_or_replace_callback_message(
                query, address_text, reply_markup=keyboard, parse_mode='Markdown'
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="view_address")

    async def select_edit_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show address selection for editing"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                addresses = response.data.get('data', {}).get('addresses', [])

            if not addresses:
                await self._ack(query, i18n.get('telegram.address.no_addresses_to_edit', language))
                return

            edit_text = i18n.get('telegram.address.select_edit_prompt', language)

            # Create selection buttons
            buttons = []
            for addr in addresses:
                status = "🏠" if addr.get('is_default') else "📍"
                addr_title = addr.get(
                    'title',
                    i18n.get('telegram.address.title_fallback', language, index=addr.get('id'))
                )
                buttons.append([{
                    'text': f"{status} {addr_title}",
                    'callback_data': f"edit_address_{addr['id']}"
                }])

            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'manage_addresses'}])

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            await self._edit_or_replace_callback_message(
                query, edit_text, reply_markup=keyboard, parse_mode='Markdown'
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_edit_address")

    async def select_delete_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show address selection for deletion"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                addresses = response.data.get('data', {}).get('addresses', [])

            if not addresses:
                await self._ack(query, i18n.get('telegram.address.no_addresses_to_delete', language))
                return

            delete_text = i18n.get('telegram.address.select_delete_prompt', language)

            # Create selection buttons
            buttons = []
            for addr in addresses:
                status = "🏠" if addr.get('is_default') else "📍"
                addr_title = addr.get(
                    'title',
                    i18n.get('telegram.address.title_fallback', language, index=addr.get('id'))
                )
                # `delete_address_` — NOT `confirm_delete_address_`: the row
                # has to land on `delete_address_handler`, which names the
                # address and asks first. Pointing it straight at
                # `confirm_delete_address` made the picker delete on a SINGLE
                # tap and bypassed the confirmation dialog that exists in this
                # very file.
                buttons.append([{
                    'text': f"{status} {addr_title}",
                    'callback_data': f"delete_address_{addr['id']}"
                }])

            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'manage_addresses'}])

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            await self._edit_or_replace_callback_message(
                query, delete_text, reply_markup=keyboard, parse_mode='Markdown'
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_delete_address")

    async def set_default_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set address as default"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Set address as default via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Call the API to set address as default
                response = await client.set_default_address(user_token, int(address_id))
                if response.success:
                    await self._ack(query, i18n.get('telegram.address.set_default_success_toast', language))
                    logger.info(f"Address {address_id} successfully set as default")

                    # Refresh the address view to show updated status
                    await self.view_address(update, context)
                else:
                    await self._ack(query,
                        i18n.get('telegram.address.set_default_failed_toast', language, error=response.error)
                    )
                    logger.error(f"Failed to set address {address_id} as default: {response.error}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="set_default_address")

    async def edit_address_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing specific address"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Back on the edit menu, so the prompt the customer left is one of
            # ours; anything else armed stays armed.
            await self.user_repo.clear_awaiting_input(user_id, *_ADDRESS_EDIT_PROMPTS)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Show editing options for the address
            edit_text = i18n.get('telegram.address.edit_options_text', language)

            # Create editing options buttons
            buttons = [
                [
                    {'text': i18n.get('telegram.address.edit_title_button', language), 'callback_data': f'edit_title_{address_id}'},
                    {'text': i18n.get('telegram.address.edit_location_button', language), 'callback_data': f'edit_location_{address_id}'}
                ],
                [
                    {'text': i18n.get('telegram.address.edit_details_button', language), 'callback_data': f'edit_details_{address_id}'},
                    {'text': i18n.get('telegram.address.edit_instructions_button', language), 'callback_data': f'edit_instructions_{address_id}'}
                ],
                [
                    {'text': i18n.get('telegram.address.delete_readd_button', language), 'callback_data': f'delete_address_{address_id}'},
                    {'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}
                ]
            ]

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            await self._edit_or_replace_callback_message(
                query, edit_text, reply_markup=keyboard, parse_mode='Markdown'
            )
            await self._ack(query)
            logger.info(f"Address editing options shown for address {address_id}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_address_handler")

    async def delete_address_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address deletion confirmation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Get address details for confirmation
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                addresses = response.data.get('data', {}).get('addresses', [])
                address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)

                if not address:
                    await self._ack(query, i18n.get('telegram.address.not_found', language))
                    return

            # Show confirmation dialog
            confirm_text = _markdown_copy(
                'telegram.address.delete_confirmation',
                language,
                title=address.get('title', 'Untitled'),
                address=address.get('full_address', 'N/A'),
            )

            buttons = [
                [
                    {'text': i18n.get('telegram.address.delete_confirm_yes', language), 'callback_data': f'confirm_delete_address_{address_id}'},
                    {'text': i18n.get('telegram.cancel', language), 'callback_data': f'view_address_{address_id}'}
                ]
            ]

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            await self._edit_or_replace_callback_message(
                query, confirm_text, reply_markup=keyboard, parse_mode='Markdown'
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="delete_address_handler")

    async def confirm_delete_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute address deletion"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Delete address via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Call the API to delete the address
                response = await client.delete_user_address(user_token, int(address_id))

                # A DELETE of an address that is already gone is the outcome
                # the customer asked for. The confirm button stays tappable
                # forever and the toast only lands after the round trip, so
                # impatient customers do tap twice — and reporting the second
                # 404 as a FAILURE sent them looking for an address that had
                # in fact been deleted.
                already_gone = response.status_code == 404
                if response.success or already_gone:
                    await self._ack(
                        query, i18n.get('telegram.address.deleted_success_toast', language)
                    )
                    if already_gone:
                        logger.info(f"Address {address_id} was already deleted; reporting success")
                    else:
                        logger.info(f"Address {address_id} successfully deleted")

                    # Redirect back to address management
                    await self.manage_addresses(update, context)
                else:
                    await self._ack(
                        query,
                        i18n.get('telegram.address.delete_failed_toast', language, error=response.error)
                    )
                    logger.error(f"Failed to delete address {address_id}: {response.error}")

                    # Show error and go back to address view
                    error_text = _markdown_copy('telegram.address.delete_failed_detail', language, error=response.error)
                    back_button = [[{'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}]]

                    from keyboards import KeyboardBuilder
                    keyboard = KeyboardBuilder.build_inline_keyboard(back_button)

                    await self._edit_or_replace_callback_message(
                        query, error_text, reply_markup=keyboard, parse_mode='Markdown'
                    )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="confirm_delete_address")

    async def edit_title_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing address title"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Store address ID for conversation
            context.user_data['edit_address_id'] = address_id
            context.user_data['edit_field'] = 'title'

            # Get current address details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if response.success:
                    addresses = response.data.get('data', {}).get('addresses', [])
                    address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)

                    if address:
                        current_title = address.get('title', i18n.get('telegram.address.untitled', language))
                        edit_text = _markdown_copy('telegram.address.edit_title_prompt', language, current_title=current_title)

                        cancel_button = [[{'text': i18n.get('telegram.cancel', language), 'callback_data': f'view_address_{address_id}'}]]
                        from keyboards import KeyboardBuilder
                        keyboard = KeyboardBuilder.build_inline_keyboard(cancel_button)

                        await self._edit_or_replace_callback_message(
                            query, edit_text, reply_markup=keyboard, parse_mode='Markdown'
                        )
                        await self._ack(query)

                        # Arm via `arm_awaiting_input`, which read-modify-writes
                        # the document: every key it sets (besides
                        # `_PRESERVED_KEYS`, e.g. `address_draft`) still belongs
                        # to the single flow being armed here, so
                        # `clear_awaiting_input`/`disarm` can keep assuming that.
                        await self.user_repo.arm_awaiting_input(
                            user_id,
                            'edit_address_title',
                            edit_address_id=address_id,
                        )

                        return

            await self._ack(query, i18n.get('telegram.address.not_found', language))

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_title_handler")

    async def edit_location_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing address location"""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            address_id = query.data.split('_')[-1]

            await self._ack(query, i18n.get('telegram.address.location_edit_not_supported', language))
            logger.info(f"Location edit requested for address {address_id} - redirecting to delete/add flow")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_location_handler")

    async def edit_details_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing address details"""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            address_id = query.data.split('_')[-1]

            await self._ack(query, i18n.get('telegram.address.details_edit_coming_soon', language))
            logger.info(f"Details edit requested for address {address_id} - not yet implemented")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_details_handler")

    async def edit_instructions_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle editing delivery instructions"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract address ID from callback data
            address_id = query.data.split('_')[-1]

            # Get current address details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_addresses(user_token)
                if response.success:
                    addresses = response.data.get('data', {}).get('addresses', [])
                    address = next((addr for addr in addresses if str(addr.get('id')) == address_id), None)

                    if address:
                        current_instructions = address.get('delivery_instructions') or i18n.get('telegram.address.none_value', language)
                        edit_text = _markdown_copy(
                            'telegram.address.edit_instructions_prompt',
                            language,
                            current_instructions=current_instructions
                        )

                        cancel_button = [[{'text': i18n.get('telegram.cancel', language), 'callback_data': f'view_address_{address_id}'}]]
                        from keyboards import KeyboardBuilder
                        keyboard = KeyboardBuilder.build_inline_keyboard(cancel_button)

                        await self._edit_or_replace_callback_message(
                            query, edit_text, reply_markup=keyboard, parse_mode='Markdown'
                        )
                        await self._ack(query)

                        # Armed via `arm_awaiting_input`, for the reason spelled
                        # out at the title prompt above.
                        await self.user_repo.arm_awaiting_input(
                            user_id,
                            'edit_address_instructions',
                            edit_address_id=address_id,
                        )

                        return

            await self._ack(query, i18n.get('telegram.address.not_found', language))

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_instructions_handler")

    async def handle_address_title_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      text: str, user_state: Dict):
        """Handle address title editing input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            address_id = user_state.get('edit_address_id')

            if not address_id:
                await update.message.reply_text(i18n.get('telegram.address.edit_session_expired', language))
                await self.user_repo.clear_awaiting_input(user_id, 'edit_address_title')
                return

            # Validate title input
            if len(text.strip()) < 2:
                await update.message.reply_text(i18n.get('telegram.address.title_too_short', language))
                return

            if len(text.strip()) > 50:
                await update.message.reply_text(i18n.get('telegram.address.title_too_long', language))
                return

            # Update address via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Prepare update data with just the title
                update_data = {'title': text.strip()}

                response = await client.update_user_address(user_token, int(address_id), update_data)
                if response.success:
                    success_text = _markdown_copy('telegram.address.title_updated_success', language, title=text.strip())

                    back_button = [[{'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}]]
                    from keyboards import KeyboardBuilder
                    keyboard = KeyboardBuilder.build_inline_keyboard(back_button)

                    await update.message.reply_text(
                        text=success_text,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )

                    # This flow is over; nobody else's is touched.
                    await self.user_repo.clear_awaiting_input(user_id, 'edit_address_title')
                    logger.info(f"Address {address_id} title updated to: {text.strip()}")

                else:
                    error_text = _markdown_copy('telegram.address.title_update_failed', language, error=response.error)
                    await update.message.reply_text(error_text, parse_mode='Markdown')
                    logger.error(f"Failed to update address {address_id} title: {response.error}")

        except Exception as e:
            logger.error(f"Error handling address title edit: {e}")
            await update.message.reply_text(i18n.get('telegram.address.title_update_error', language))

    async def handle_address_instructions_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                             text: str, user_state: Dict):
        """Handle address delivery instructions editing input"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            address_id = user_state.get('edit_address_id')

            if not address_id:
                await update.message.reply_text(i18n.get('telegram.address.edit_session_expired', language))
                await self.user_repo.clear_awaiting_input(user_id, 'edit_address_instructions')
                return

            # Validate instructions input
            if len(text.strip()) > 200:
                await update.message.reply_text(i18n.get('telegram.address.instructions_too_long', language))
                return

            # Update address via API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Prepare update data with delivery instructions
                update_data = {'delivery_instructions': text.strip()}

                response = await client.update_user_address(user_token, int(address_id), update_data)
                if response.success:
                    success_text = i18n.get('telegram.address.instructions_updated_intro', language)
                    if text.strip():
                        success_text += _markdown_copy('telegram.address.instructions_new_value', language, value=text.strip())
                    else:
                        success_text += i18n.get('telegram.address.instructions_cleared', language)

                    back_button = [[{'text': i18n.get('telegram.back', language), 'callback_data': f'view_address_{address_id}'}]]
                    from keyboards import KeyboardBuilder
                    keyboard = KeyboardBuilder.build_inline_keyboard(back_button)

                    await update.message.reply_text(
                        text=success_text,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )

                    # This flow is over; nobody else's is touched.
                    await self.user_repo.clear_awaiting_input(user_id, 'edit_address_instructions')
                    logger.info(f"Address {address_id} delivery instructions updated")

                else:
                    error_text = _markdown_copy('telegram.address.instructions_update_failed', language, error=response.error)
                    await update.message.reply_text(error_text, parse_mode='Markdown')
                    logger.error(f"Failed to update address {address_id} instructions: {response.error}")

        except Exception as e:
            logger.error(f"Error handling address instructions edit: {e}")
            await update.message.reply_text(i18n.get('telegram.address.instructions_update_error', language))


# Global handler instance
profile_handlers = ProfileHandlers()

# Export conversation states
profile_handlers.SELECT_LANGUAGE = SELECT_LANGUAGE
profile_handlers.PHONE = PHONE
profile_handlers.ADDRESS_LOCATION = ADDRESS_LOCATION
profile_handlers.ADDRESS_TITLE = ADDRESS_TITLE
profile_handlers.ADDRESS_REGION = ADDRESS_REGION
profile_handlers.ADDRESS_DISTRICT = ADDRESS_DISTRICT
profile_handlers.ADDRESS_STREET = ADDRESS_STREET
profile_handlers.ADDRESS_BUILDING = ADDRESS_BUILDING
profile_handlers.ADDRESS_APARTMENT = ADDRESS_APARTMENT
profile_handlers.ADDRESS_FLOOR = ADDRESS_FLOOR
profile_handlers.ADDRESS_DELIVERY_INSTRUCTIONS = ADDRESS_DELIVERY_INSTRUCTIONS
profile_handlers.ADDRESS_GEOCODE_CONFIRM = ADDRESS_GEOCODE_CONFIRM
profile_handlers.PHONE_VERIFY_PHONE = PHONE_VERIFY_PHONE
profile_handlers.PHONE_VERIFY_NAME = PHONE_VERIFY_NAME
profile_handlers.PHONE_VERIFY_OTP = PHONE_VERIFY_OTP
