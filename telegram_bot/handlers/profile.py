"""
User profile and registration handlers
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Callable, Dict, Any, NamedTuple
from telegram import constants, InlineKeyboardMarkup, Update, ReplyKeyboardRemove
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest

from eligibility import main_menu_for
from i18n import i18n
from keyboards import ProfileKeyboards, MenuKeyboards, LanguageKeyboards, KeyboardBuilder
from shared.constants import (
    TASHKENT_DISTRICTS,
    get_district_name,
    get_district_center,
    get_all_districts,
    is_within_tashkent,
)
from handlers.menu import main_menu_handler
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import (
    user_middleware,
    validate_phone_number,
    normalize_phone_number,
    get_auth_token,
    otp_rate_limiter,
    maybe_remove_stale_reply_keyboard,
)
from config import config
from handlers.base import BaseHandler

logger = logging.getLogger('handlers')

# Conversation states.
# NOTE: renumbering these is safe. No `persistence` is configured on the
# Application (see telegram_bot/bot.py), so conversation state lives only in
# memory and already resets on every restart — no stored state can survive a
# deploy carrying a different numbering.
(SELECT_LANGUAGE, PHONE, ADDRESS_LOCATION, ADDRESS_TITLE,
 ADDRESS_REGION, ADDRESS_DISTRICT, ADDRESS_STREET, ADDRESS_BUILDING,
 ADDRESS_APARTMENT, ADDRESS_FLOOR,
 ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM,
 PHONE_VERIFY_PHONE, PHONE_VERIFY_NAME,
 LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP) = range(17)

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


def _markdown_copy(key: str, language: str, **data: Any) -> str:
    """Render seeded Markdown copy with every interpolated value escaped as DATA.

    The seeded copy is MARKUP — ``*Aniqlangan joylashuv:*``, ``**{title}**`` —
    and that is why these messages go out with ``parse_mode='Markdown'`` at all.
    Everything interpolated INTO it is data the customer or a geocoder wrote,
    and Uzbek addresses really do carry ``_`` (building suffixes like "15_A"),
    ``[`` (geocoder annotations) and ``*``. Unescaped, one of those makes
    Telegram refuse the whole message with "can't parse entities"; the handler's
    ``except Exception`` then turns a formatting problem into a dead screen, and
    ``_edit_or_replace_callback_message`` cannot rescue it because its fallback
    re-sends with the SAME parse mode.

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


class AddressStep(NamedTuple):
    """One step of the optional address-detail chain.

    Named rather than a bare tuple because all three members are read at
    unrelated call sites: the prompt key by the prompt helper, the keyboard by
    both the prompt and the too-long re-prompt, and the state by every caller
    that returns it to the ConversationHandler.
    """

    prompt_key: str
    keyboard: Callable[[str], InlineKeyboardMarkup]
    state: int


# SSOT for the optional-detail chain both address flows converge on.
# There is deliberately no 'entrance' step. UserAddress has no entrance column,
# so entrance is captured as free text by the delivery-instructions prompt.
_ADDRESS_STEPS = {
    'building': AddressStep(
        'telegram.address.enter_building',
        lambda language: ProfileKeyboards.optional_field_keyboard('building', language),
        ADDRESS_BUILDING,
    ),
    'apartment': AddressStep(
        'telegram.address.enter_apartment',
        lambda language: ProfileKeyboards.optional_field_keyboard('apartment', language),
        ADDRESS_APARTMENT,
    ),
    'floor': AddressStep(
        'telegram.address.enter_floor',
        lambda language: ProfileKeyboards.optional_field_keyboard('floor', language),
        ADDRESS_FLOOR,
    ),
    'delivery_instructions': AddressStep(
        'telegram.address.enter_delivery_instructions',
        ProfileKeyboards.delivery_instructions_keyboard,
        ADDRESS_DELIVERY_INSTRUCTIONS,
    ),
}

# Where a Skip tap lands. Skipping the building number means there is no building
# to be inside, so apartment and floor are skipped along with it (private house).
# Street is required and renders no Skip button; it stays here as a safety net.
_SKIP_TARGETS = {
    'street': 'building',
    'building': 'delivery_instructions',
    'apartment': 'floor',
    'floor': 'delivery_instructions',
}

# Which key in temp_address_data each step writes, IN FLOW ORDER. Skip CLEARS
# it, so Skip means what it says: retry_geocode reruns the whole chain, so a
# value typed before the retry would otherwise survive a later Skip and still be
# saved. The order is load-bearing — `_cleared_by_skip` walks it to find the
# steps a Skip jumps OVER.
_ADDRESS_FIELD_DATA_KEYS = {
    'street': 'street_address',
    'building': 'building_number',
    'apartment': 'apartment_number',
    'floor': 'floor_number',
    'delivery_instructions': 'delivery_instructions',
}


def _cleared_by_skip(field: str) -> tuple[str, ...]:
    """The temp_address_data keys a Skip on `field` must clear.

    Not just the field that was tapped: a Skip that JUMPS OVER steps clears
    those too. Skipping the building number means there is no building to be
    inside, so `_SKIP_TARGETS` lands on delivery instructions — and an
    apartment and floor typed before a `retry_geocode` rerun would otherwise be
    saved onto a house whose owner has just said it has neither.

    An unknown field (a Skip button rendered by an older deploy) clears
    nothing: it must not take somebody's real answers with it on its way out.
    """
    fields = list(_ADDRESS_FIELD_DATA_KEYS)
    if field not in fields:
        return ()

    start = fields.index(field)
    target = _SKIP_TARGETS.get(field)
    stop = fields.index(target) if target in fields else start + 1
    return tuple(_ADDRESS_FIELD_DATA_KEYS[name] for name in fields[start:stop])

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

    async def _render_notification_settings(
        self,
        update: Update,
        language: str,
        delivery_telegram_status_updates_enabled: bool,
        callback_toast: str | None = None,
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
        keyboard = ProfileKeyboards.notification_settings(
            language=language,
            delivery_telegram_status_updates_enabled=delivery_telegram_status_updates_enabled,
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

            await self._render_notification_settings(
                update,
                language,
                delivery_telegram_status_updates_enabled=enabled,
            )
        except Exception as e:
            await self._handle_error(update, exc=e, operation="notification_settings")

    async def toggle_delivery_telegram_status_notifications(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ):
        """Toggle Telegram delivery status notifications and refresh notification settings screen."""
        try:
            user = await user_middleware(update)
            if not user:
                return

            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            toggle_to = query.data.rsplit('_', 1)[-1]
            enabled = toggle_to == 'on'

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.update_notification_preferences(
                    user_token,
                    {'delivery_telegram_status_updates_enabled': enabled},
                )
                if not response.success:
                    await self._ack(query,
                        i18n.get('telegram.notifications.update_failed', language),
                        show_alert=True,
                    )
                    return

                refreshed_enabled = self._extract_delivery_telegram_status_updates_enabled(response.data)

            await self._render_notification_settings(
                update,
                language,
                delivery_telegram_status_updates_enabled=refreshed_enabled,
                callback_toast=i18n.get('telegram.notifications.update_success', language),
            )
        except Exception as e:
            logger.error(f"Error toggling delivery telegram notifications: {e}")
            if update.callback_query:
                await self._ack(update.callback_query,
                    i18n.get('telegram.notifications.update_failed', 'en'),
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

            # Send the prompt with reply keyboard
            keyboard = ProfileKeyboards.phone_request(language)

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
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE_VERIFY_PHONE

            phone = normalize_phone_number(contact.phone_number)
            if not phone:
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
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

            # Remove the phone request keyboard and ask for name
            success_text = i18n.get('telegram.phone.phone_accepted', language)
            await update.message.reply_text(
                success_text,
                reply_markup=ReplyKeyboardRemove()
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
            return ConversationHandler.END

    async def phone_verify_text_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle typed phone number during phone verification flow."""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            phone_text = update.message.text.strip()

            if not await validate_phone_number(phone_text):
                await update.message.reply_text(
                    i18n.get('telegram.phone.invalid_format', language),
                    reply_markup=ProfileKeyboards.phone_request(language)
                )
                return PHONE_VERIFY_PHONE

            phone = normalize_phone_number(phone_text)
            logger.info(f"Phone text received for user {user_id}: {phone}")

            # Typed phone input is not Telegram-trusted contact, keep it unverified.
            await self.user_repo.set_user_phone(user_id, phone)
            context.user_data['pending_phone'] = phone

            # Keep backend profile phone in sync.
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    try:
                        await client.update_user_profile(user_token, {'phone': phone})
                        logger.info(f"Phone updated via API for user {user_id} from text input")
                    except Exception as api_error:
                        logger.warning(f"Failed to update phone via API from text input: {api_error}")

            await update.message.reply_text(
                i18n.get('telegram.phone.phone_accepted', language),
                reply_markup=ReplyKeyboardRemove()
            )
            await update.message.reply_text(i18n.get('telegram.enter_name', language))

            return PHONE_VERIFY_NAME

        except Exception as e:
            logger.error(f"Error in phone_verify_text_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

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
            keyboard = await main_menu_for(update.effective_user.id, language)

            await update.message.reply_text(
                text=success_text,
                reply_markup=keyboard
            )

            # Clear pending phone from context
            context.user_data.pop('pending_phone', None)

            logger.info(f"Phone verification flow completed for user {user_id}")
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in phone_verify_name_received: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def cancel_phone_verification(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel phone verification flow"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)
            keyboard = await main_menu_for(update.effective_user.id, language)

            # Clear pending data
            context.user_data.pop('pending_phone', None)

            if update.callback_query:
                await self._ack(update.callback_query)
                await update.callback_query.message.reply_text(
                    text=i18n.get('telegram.action_cancelled_short', language),
                    reply_markup=ReplyKeyboardRemove()
                )
                await update.callback_query.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=i18n.get('telegram.action_cancelled_short', language),
                    reply_markup=ReplyKeyboardRemove()
                )
                await update.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )

            logger.info(f"Phone verification cancelled for user {user_id}")
            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error cancelling phone verification: {e}")
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
                            parse_mode='Markdown'
                        )

                        # Store awaiting OTP flag
                        context.user_data['awaiting_otp'] = True
                        context.user_data['pending_phone_verification'] = phone
                        context.user_data['otp_prompted_update_id'] = update.update_id

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
                    reply_markup=LanguageKeyboards.select_language()
                )

                return SELECT_LANGUAGE
            else:
                # Existing row but phone never captured -> resume phone collection
                # instead of dropping the user at the main menu with no phone.
                if not existing_user.get('phone'):
                    language = existing_user.get('preferred_language') or await i18n.get_user_language(user_id)
                    keyboard = ProfileKeyboards.phone_request(language)
                    await maybe_remove_stale_reply_keyboard(update, context)

                    await update.message.reply_text(
                        text=i18n.get('telegram.registration.share_contact_prompt', language),
                        reply_markup=keyboard
                    )

                    return PHONE

                # Already registered, show main menu — honour the user's saved
                # language rather than the Telegram client locale.
                language = existing_user.get('preferred_language') or await i18n.get_user_language(user_id)
                complete_text = i18n.get('telegram.welcome', language)
                keyboard = await main_menu_for(update.effective_user.id, language)
                await maybe_remove_stale_reply_keyboard(update, context)

                await update.message.reply_text(
                    text=complete_text,
                    reply_markup=keyboard
                )

                return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error starting registration: {e}")
            return ConversationHandler.END

    async def language_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection during registration"""
        try:
            query = update.callback_query
            user = update.effective_user
            user_id = user.id
            language_code = query.data.split('_')[-1]
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
                        complete_text = i18n.get('telegram.registration_complete', language)
                        keyboard = await main_menu_for(update.effective_user.id, language)

                        await update.message.reply_text(
                            text=complete_text,
                            reply_markup=keyboard
                        )

                        logger.info(f"Registration completed for user {user_id}")
                        return ConversationHandler.END

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
                            # Cannot link - phone belongs to another telegram user
                            await update.message.reply_text(
                                i18n.get('telegram.phone.already_linked_other_account', language),
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE
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

                        otp_response = await client.send_phone_verification(user_token, phone)
                        if not otp_response.success:
                            logger.error(f"Failed to send OTP during registration: {otp_response.error}")
                            await update.message.reply_text(
                                i18n.get(
                                    'telegram.phone.verification_sms_send_failed',
                                    language,
                                    error=otp_response.error
                                ),
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE

                        # Remove contact keyboard and ask for OTP
                        await update.message.reply_text(
                            i18n.get('telegram.phone.phone_accepted', language),
                            reply_markup=ReplyKeyboardRemove()
                        )

                        otp_data = otp_response.data.get('data', {}) if isinstance(otp_response.data, dict) else {}
                        phone_masked = otp_data.get('phone_masked', phone)

                        await update.message.reply_text(
                            i18n.get(
                                'telegram.phone.verification_code_sent_to_phone_prompt',
                                language,
                                phone_masked=phone_masked
                            )
                        )

                        # Stay INSIDE the conversation and capture the OTP via
                        # the dedicated REGISTER_OTP state (register_otp_received).
                        # We deliberately do NOT set the global 'awaiting_otp'
                        # flag here anymore — the in-conversation handler owns
                        # this path now, so /cancel and /start behave correctly
                        # during OTP entry and the global catch-all won't fire.
                        context.user_data['pending_phone_verification'] = phone

                        logger.info(f"Registration OTP sent to {phone} for user {user_id}")
                        return REGISTER_OTP

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
                            # Cannot link - phone belongs to another telegram user
                            await update.message.reply_text(
                                i18n.get('telegram.phone.already_linked_other_account', language),
                                reply_markup=ProfileKeyboards.phone_request(language)
                            )
                            return PHONE
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

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error in link_account_confirm: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    async def link_account_otp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle OTP verification for account linking"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            otp = update.message.text.strip()

            # Validate OTP format
            if not otp.isdigit() or len(otp) != 6:
                await update.message.reply_text(
                    i18n.get('telegram.phone.enter_valid_6_digit_code', language)
                )
                return LINK_ACCOUNT_OTP

            phone = context.user_data.get('pending_link_phone')
            if not phone:
                await update.message.reply_text(
                    i18n.get('telegram.phone.session_expired_start_again', language)
                )
                return ConversationHandler.END

            # Call API to verify OTP and link accounts
            try:
                async with api_client as client:
                    response = await client.link_phone_verify(user_id, otp)

                    if response.success:
                        # Account linked successfully!
                        context.user_data.pop('pending_link_phone', None)

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
                        name = user_data.get('first_name', i18n.get('telegram.common.user_fallback', language))

                        await update.message.reply_text(
                            i18n.get('telegram.phone.accounts_linked_success', language, name=name),
                            reply_markup=await main_menu_for(update.effective_user.id, language)
                        )

                        logger.info(f"Account linking completed for user {user_id}")
                        return ConversationHandler.END
                    else:
                        error_msg = response.error or i18n.get('telegram.phone.invalid_verification_code_default', language)

                        # Check if it's an expired/invalid OTP
                        if 'expired' in error_msg.lower() or 'not found' in error_msg.lower():
                            await update.message.reply_text(
                                i18n.get('telegram.phone.verification_code_expired_start_again', language)
                            )
                            return ConversationHandler.END
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
            return ConversationHandler.END

    async def register_otp_received(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Verify the registration OTP from WITHIN the conversation (REGISTER_OTP).

        This is the in-conversation counterpart to the legacy global
        bot._handle_otp_verification catch-all. It verifies the code via the
        REGISTRATION endpoint (client.verify_phone_otp) — NOT the account-merge
        endpoint (client.link_phone_verify), which would be wrong/dangerous on
        the fresh-registration path.

        DRY note: the actual verify call (get_auth_token + verify_phone_otp) is
        ~3 lines and is deliberately duplicated from bot._handle_otp_verification
        rather than extracted into a shared helper — that method is on the bot
        class in bot.py while this is on ProfileHandlers in profile.py, so a
        shared helper would force awkward cross-class/cross-module coupling. The
        surrounding success/error UX (conversation states, main-menu keyboard,
        re-prompt vs. END) differs entirely between the two paths.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            otp = update.message.text.strip()

            # Validate OTP format (mirrors link_account_otp's 6-digit guard).
            if not otp.isdigit() or len(otp) != 6:
                await update.message.reply_text(
                    i18n.get('telegram.bot.otp.invalid_format', language)
                )
                return REGISTER_OTP

            # Defensive: if the pending phone is missing (stale/restarted state),
            # end cleanly instead of verifying against no pending context
            # (mirrors link_account_otp's missing-phone guard).
            if not context.user_data.get('pending_phone_verification'):
                await update.message.reply_text(
                    i18n.get('telegram.phone.session_expired_start_again', language)
                )
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

                    # Correct registration endpoint (NOT link_phone_verify).
                    response = await client.verify_phone_otp(user_token, otp)

                    if response.success:
                        # Registration complete — clear pending state and show
                        # the same completion message the contact path uses.
                        context.user_data.pop('pending_phone_verification', None)

                        await update.message.reply_text(
                            i18n.get('telegram.registration_complete', language),
                            reply_markup=await main_menu_for(update.effective_user.id, language)
                        )

                        logger.info(f"Registration OTP verified for user {user_id}")
                        return ConversationHandler.END

                    error_msg = response.error or i18n.get(
                        'telegram.phone.invalid_verification_code_default', language
                    )

                    # Expired / not-found: the code is unrecoverable, so end.
                    if 'expired' in error_msg.lower() or 'not found' in error_msg.lower():
                        await update.message.reply_text(
                            i18n.get('telegram.phone.verification_code_expired_start_again', language)
                        )
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
                    return REGISTER_OTP

            except Exception as api_error:
                logger.error(f"API error verifying registration OTP: {api_error}")
                await update.message.reply_text(
                    i18n.get('telegram.phone.verification_failed_retry', language)
                )
                return REGISTER_OTP

        except Exception as e:
            logger.error(f"Error in register_otp_received: {e}")
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

            cancel_text = i18n.get('telegram.action_cancelled', language)
            keyboard = await main_menu_for(update.effective_user.id, language)

            await update.message.reply_text(
                text=i18n.get('telegram.action_cancelled_short', language),
                reply_markup=ReplyKeyboardRemove()
            )
            await update.message.reply_text(
                text=cancel_text,
                reply_markup=keyboard
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

            await self.user_repo.update_user_state(user_id, {'awaiting_input': 'edit_profile_name'})

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

            await self.user_repo.update_user_state(user_id, {'awaiting_input': 'edit_profile_birthday'})

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
            context.user_data['conversation_state'] = 'address_location'
            logger.info(f"Set conversation state to: address_location")

            # Use enhanced location request with skip option
            location_text = i18n.get('telegram.address.location_prompt_enhanced', language)
            keyboard = ProfileKeyboards.location_request(
                language,
                extra_rows=(i18n.get('telegram.address.enter_manually_button', language),),
            )

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

            # Remove reply keyboard
            await update.message.reply_text(
                i18n.get('telegram.address.location_received', language),
                reply_markup=ReplyKeyboardRemove()
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
            return ConversationHandler.END

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

            # The title step sits at a different position in each flow: a
            # shared pin asks for the title early, right after the location,
            # so titling here still has apartment/floor/instructions ahead
            # of it. A manually typed address only reaches this step LAST,
            # after geocode confirmation, so titling here is the save.
            if _is_shared_pin_address(context):
                await self._create_address_now(update, context, language)
                return await self._prompt_address_step(update, language, 'apartment')

            return await self.save_address_final(update, context)

        except Exception as e:
            logger.error(f"Error handling address title: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

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

            keyboard = await main_menu_for(update.effective_user.id, language)

            # Handle both message and callback query
            if update.callback_query:
                await self._ack(update.callback_query)
                await self._edit_or_replace_callback_message(
                    update.callback_query, cancel_text, reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )

            # Clear all temporary address data
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)
            context.user_data.pop('temp_address_data', None)
            context.user_data.pop('address_flow_origin', None)

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error canceling address: {e}")
            return ConversationHandler.END

    async def cancel_address_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel address adding from text button (removes ReplyKeyboard)"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            cancel_text = i18n.get('telegram.action_cancelled', language)

            # Read before the pop below consumes it.
            origin = context.user_data.get('address_flow_origin')

            # Same contract as the inline Cancel: an explicit cancel undoes the
            # row the pin flow created at the title step.
            await self._discard_created_address(update, context)

            # First remove the reply keyboard
            await update.message.reply_text(
                i18n.get('telegram.action_cancelled_short', language),
                reply_markup=ReplyKeyboardRemove()
            )

            if origin == 'checkout':
                # The customer has a full cart. Dropping them on the main menu
                # makes them navigate back for no reason. Return to the CART,
                # not to checkout — checkout would re-render the very
                # zero-address prompt they just cancelled, which is a loop.
                from handlers.products import product_handlers

                await product_handlers.show_cart(update, context)
            else:
                keyboard = await main_menu_for(update.effective_user.id, language)
                await update.message.reply_text(
                    text=cancel_text,
                    reply_markup=keyboard
                )

            # Clear all temporary address data
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)
            context.user_data.pop('temp_address_data', None)
            context.user_data.pop('address_flow_origin', None)

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

            context.user_data.pop('temp_address_data', None)
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)
            context.user_data.pop('address_flow_origin', None)

            logger.info(
                f"Address flow timed out for user {user_id} "
                f"(saved address: {saved_address_id or 'none'})"
            )

            text = i18n.get(
                'telegram.address.flow_timed_out_saved' if saved_address_id
                else 'telegram.address.flow_timed_out',
                language,
            )
            keyboard = await main_menu_for(user_id, language)

            # A timeout arrives as a synthetic update carrying whatever the last
            # real one was, so the reply target is derived rather than assumed.
            if update.callback_query is not None and update.callback_query.message is not None:
                await update.callback_query.message.reply_text(text, reply_markup=keyboard)
            elif update.message is not None:
                await update.message.reply_text(text, reply_markup=keyboard)
            else:
                await context.bot.send_message(
                    chat_id=user_id, text=text, reply_markup=keyboard
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

            # Remove reply keyboard
            await update.message.reply_text(
                i18n.get('telegram.address.manual_entry_started', language),
                reply_markup=ReplyKeyboardRemove()
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
            return ConversationHandler.END

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

            await self._ack(query)

            # Ask for street name (required, no skip option)
            street_prompt = escape_markdown(
                i18n.get('telegram.address.enter_street_required', language, district_name=district_name),
                version=2
            )
            # No skip keyboard - street is required

            # A refused edit ("message to edit not found" — the customer
            # deleted the bubble) used to unwind into `except Exception` and
            # end the flow in silence: the street they then typed reached a bot
            # that was no longer listening and was filed as a support ticket.
            await self._edit_or_replace_callback_message(
                query,
                street_prompt,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )

            return ADDRESS_STREET

        except Exception as e:
            logger.error(f"Error in district_selected: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return ConversationHandler.END

    @staticmethod
    async def _reply_markdown_or_plain(message, text: str, reply_markup=None):
        """Send `text` as Markdown, falling back to plain text if Telegram says no.

        Escaping the interpolated geocoder string fixes the hazard we know
        about; it cannot fix the one we do not. The cost of being wrong here is
        total — a refused prompt used to end the conversation, leaving the
        customer on a dead screen with no way to self-rescue, because
        re-sharing the pin re-runs the same deterministic failure. Losing the
        bold is a much smaller price than losing the flow.
        """
        try:
            return await message.reply_text(
                text, reply_markup=reply_markup, parse_mode='Markdown'
            )
        except BadRequest as e:
            logger.warning(f"Markdown prompt refused ({e}); resending as plain text")
            return await message.reply_text(text, reply_markup=reply_markup)

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

    async def _prompt_address_step(self, update: Update, language: str, field: str):
        """Send one optional address step's prompt and return its state.

        Both address flows converge on this chain, and every step is reachable
        from either a typed answer (message) or a Skip tap (callback), so the
        send path is derived from the update rather than passed by each caller.

        The callback path goes through `_edit_or_replace_callback_message`
        because a REFUSED edit is a rendering problem, not a flow problem: this
        bare edit used to unwind into the caller's `except Exception: return
        ConversationHandler.END`, so Telegram's most benign rejection
        ("message is not modified") left the customer looking at a
        correct-looking prompt whose Skip button was wired to nothing.
        """
        step = _ADDRESS_STEPS[field]

        text = i18n.get(step.prompt_key, language)
        keyboard = step.keyboard(language)

        if update.callback_query is not None:
            await self._edit_or_replace_callback_message(
                update.callback_query, text, reply_markup=keyboard
            )
        else:
            await update.message.reply_text(text, reply_markup=keyboard)

        return step.state

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
            context.user_data['temp_address_data']['street_address'] = street

            return await self._prompt_address_step(update, language, 'building')

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
            context.user_data['temp_address_data']['building_number'] = building

            return await self._prompt_address_step(update, language, 'apartment')

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
            context.user_data['temp_address_data']['apartment_number'] = apartment

            await self._sync_address_details(update, context)
            return await self._prompt_address_step(update, language, 'floor')

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
            context.user_data['temp_address_data']['floor_number'] = floor

            await self._sync_address_details(update, context)
            return await self._prompt_address_step(update, language, 'delivery_instructions')

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
            context.user_data['temp_address_data']['delivery_instructions'] = instructions

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
            return await self._prompt_address_step(update, language, next_field)

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

            # Offer location sharing or manual re-entry
            retry_text = i18n.get('telegram.address.retry_location', language)

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

            await self._ack(query)

            # The title step sits at a different position in each flow: a
            # shared pin asks for the title early, right after the location,
            # so titling here still has apartment/floor/instructions ahead
            # of it. A manually typed address only reaches this step LAST,
            # after geocode confirmation, so titling here is the save.
            if _is_shared_pin_address(context):
                await self._create_address_now(update, context, language)
                return await self._prompt_address_step(update, language, 'apartment')

            return await self.save_address_final(update, context, is_callback=True)

        except Exception as e:
            logger.error(f"Error in address_title_callback: {e}")
            return ConversationHandler.END

    async def _resume_checkout_after_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Continue checkout flow after address is saved from checkout path."""
        from handlers.orders import order_handlers

        callback_query = getattr(update, 'callback_query', None)
        callback_message = callback_query.message if callback_query else None
        # When a callback drove this flow, anchor the resumed update on the
        # callback's message (the bubble that held the inline buttons) — that
        # is what the user actually sees and what checkout should reply to.
        # Fall back to update.message only for non-callback (text) entry points.
        resume_message = callback_message or update.message

        if resume_message is not None:
            synthetic_update = SimpleNamespace(
                effective_user=update.effective_user,
                callback_query=None,
                message=resume_message,
            )
            await order_handlers.checkout_handler(synthetic_update, context)
            return

        await order_handlers.checkout_handler(update, context)

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

            resume_checkout_after_save = context.user_data.pop('address_flow_origin', None) == 'checkout'

            # Clear temp data
            context.user_data.pop('temp_address_data', None)
            context.user_data.pop('temp_location', None)
            context.user_data.pop('temp_address', None)

            if resume_checkout_after_save:
                await self._resume_checkout_after_address(update, context)
                return ConversationHandler.END

            success_text = i18n.get('telegram.address.saved_successfully', language)
            keyboard = await main_menu_for(update.effective_user.id, language)

            if is_callback:
                # A refused edit is a rendering problem: the address IS saved,
                # and losing this confirmation sends the customer round the
                # flow again and leaves them with a duplicate.
                await self._edit_or_replace_callback_message(
                    update.callback_query, success_text, reply_markup=keyboard
                )
            else:
                await update.message.reply_text(
                    text=success_text,
                    reply_markup=keyboard
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

                        # Arm by WRITING A FRESH state, never by merging into
                        # the one already there: `clear_awaiting_input` relies
                        # on every key in the document belonging to the single
                        # flow that is armed.
                        await self.user_repo.update_user_state(user_id, {
                            'awaiting_input': 'edit_address_title',
                            'edit_address_id': address_id
                        })

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

                        # A fresh state, for the reason spelled out at the
                        # title prompt above.
                        await self.user_repo.update_user_state(user_id, {
                            'awaiting_input': 'edit_address_instructions',
                            'edit_address_id': address_id
                        })

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

    async def logout_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user logout from all platforms"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Confirm logout action
            logout_text = i18n.get('telegram.profile.logout_confirmation_text', language)

            buttons = [
                [
                    {'text': i18n.get('telegram.profile.logout_yes_button', language), 'callback_data': 'confirm_logout'},
                    {'text': i18n.get('telegram.cancel', language), 'callback_data': 'profile_menu'}
                ]
            ]

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)

            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query, logout_text, reply_markup=keyboard, parse_mode='Markdown'
                )
                await self._ack(update.callback_query)
            else:
                await update.message.reply_text(
                    text=logout_text,
                    reply_markup=keyboard,
                    parse_mode='Markdown'
                )

            logger.info(f"Logout confirmation shown to user {user_id}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="logout_handler")

    async def confirm_logout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute logout"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Call logout API to invalidate tokens
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    try:
                        # Call logout-all endpoint to invalidate all sessions
                        await client.logout_all_sessions(user_token)
                        logger.info(f"Successfully logged out user {user_id} from all sessions")
                    except Exception as api_error:
                        logger.warning(f"API logout failed for user {user_id}: {api_error}")
                        # Continue with local logout even if API fails

            # Clear local bot user data
            await self.user_repo.clear_user_session(user_id)

            # Show logout success message
            logout_success = i18n.get('telegram.profile.logout_success_text', language)

            # Remove inline keyboard
            await self._edit_or_replace_callback_message(
                query, logout_success, parse_mode='Markdown'
            )
            await self._ack(query, i18n.get('telegram.profile.logout_success_toast', language))

            logger.info(f"User {user_id} successfully logged out")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="confirm_logout")



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
