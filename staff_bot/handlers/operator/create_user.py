"""
Create Client User Handler for Staff Bot
Allows operators to create new customer accounts via conversation flow.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.operator import OperatorKeyboards
from staff_bot.utils.validators import validate_phone, validate_name
from staff_bot.utils.formatters import format_user_card, escape_html
from staff_bot.permissions import require_auth, require_operator
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)

# Conversation states
ENTER_PHONE, ENTER_FIRST_NAME, ENTER_LAST_NAME, SELECT_LANGUAGE, CONFIRM_CREATE = range(10, 15)

# Name parts the operator can be asked for, in the order they are read out.
CLIENT_NAME_PARTS = ('first_name', 'last_name')


def build_client_display_name(client_data: dict) -> str:
    """The customer's name, assembled from the parts that are actually there.

    ONE expression of "does this customer have a surname". The confirm screen
    used ``if client_data.get('last_name')`` while the success screen used
    ``f"{first} {last}".strip()`` — and ``.strip()`` removes whitespace, not
    the literal ``"None"`` that formatting a ``None`` produces. Most callers
    give one name, so the operator's success screen read "👤 Dilnoza None" and
    they read that line back to the caller.

    A missing surname is the common case, not an error: the WRITE stores
    ``last_name: null`` and that is correct. Only the rendering was wrong.
    """
    parts = [str(client_data.get(part) or '').strip() for part in CLIENT_NAME_PARTS]
    return ' '.join(part for part in parts if part)


class CreateUserHandler(BaseHandler):
    """Handle client user creation flow"""

    @require_auth
    @require_operator
    async def start_create_user(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the create user conversation"""
        language = await self._get_language(update, context)

        # Clear any previous creation data
        context.user_data.pop('new_client', None)
        context.user_data['new_client'] = {}

        text = i18n.get('staff.operator.enter_phone', language)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        else:
            await update.message.reply_text(text, parse_mode='HTML')

        return ENTER_PHONE

    @require_auth
    @require_operator
    async def receive_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive and validate phone number"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        phone_text = update.message.text.strip()
        is_valid, result = validate_phone(phone_text)

        if not is_valid:
            await update.message.reply_text(
                i18n.get('staff.operator.invalid_phone', language),
                parse_mode='HTML'
            )
            return ENTER_PHONE

        normalized_phone = result

        # Check if user already exists
        try:
            async with api_client as client:
                response = await client.search_clients(token, normalized_phone, search_type='phone')

            if response.success:
                clients = response.data if isinstance(response.data, list) else response.data.get('items', [])
                # Check for exact phone match
                for client_user in clients:
                    if client_user.get('phone') == normalized_phone:
                        # User already exists - show info and offer options
                        card = format_user_card(client_user, language)
                        text = (
                            f"{i18n.get('staff.operator.user_exists', language)}\n\n"
                            f"{card}"
                        )
                        keyboard = OperatorKeyboards.user_found(language, client_user['id'])
                        await update.message.reply_text(
                            text, reply_markup=keyboard, parse_mode='HTML'
                        )
                        return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error checking existing user: {e}")

        # Store phone and continue
        context.user_data['new_client']['phone'] = normalized_phone

        await update.message.reply_text(
            i18n.get('staff.operator.enter_first_name', language),
            parse_mode='HTML'
        )
        return ENTER_FIRST_NAME

    @require_auth
    @require_operator
    async def receive_first_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive first name"""
        language = await self._get_language(update, context)
        name = update.message.text.strip()

        is_valid, error = validate_name(name)
        if not is_valid:
            await update.message.reply_text(
                i18n.get('staff.operator.invalid_name', language),
                parse_mode='HTML'
            )
            return ENTER_FIRST_NAME

        context.user_data['new_client']['first_name'] = name

        await update.message.reply_text(
            i18n.get('staff.operator.enter_last_name', language),
            parse_mode='HTML'
        )
        return ENTER_LAST_NAME

    @require_auth
    @require_operator
    async def receive_last_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive last name (or skip)"""
        language = await self._get_language(update, context)
        text = update.message.text.strip()

        if text.lower() in ('-', 'skip', 'пропустить', "o'tkazish"):
            context.user_data['new_client']['last_name'] = None
        else:
            is_valid, error = validate_name(text)
            if not is_valid:
                await update.message.reply_text(
                    i18n.get('staff.operator.invalid_name', language),
                    parse_mode='HTML'
                )
                return ENTER_LAST_NAME
            context.user_data['new_client']['last_name'] = text

        # Language selection
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(i18n.get_language_name('uz', 'uz'), callback_data="staff_op_lang_uz"),
                InlineKeyboardButton(i18n.get_language_name('ru', 'ru'), callback_data="staff_op_lang_ru"),
                InlineKeyboardButton(i18n.get_language_name('en', 'en'), callback_data="staff_op_lang_en"),
            ]
        ])

        await update.message.reply_text(
            i18n.get('staff.operator.select_client_language', language),
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return SELECT_LANGUAGE

    @require_auth
    @require_operator
    async def select_client_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle client language selection"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        # Parse: staff_op_lang_{lang}
        client_lang = query.data.split('_')[-1]
        context.user_data['new_client']['preferred_language'] = client_lang

        # Show confirmation
        client_data = context.user_data['new_client']
        lang_name = i18n.get_language_name(client_lang, language)

        text = (
            f"👤 <b>{i18n.get('staff.operator.confirm_create_user', language)}</b>\n\n"
            f"📞 {escape_html(client_data['phone'])}\n"
            f"👤 {escape_html(build_client_display_name(client_data))}\n"
            f"🌐 {lang_name}"
        )

        keyboard = CommonKeyboards.confirm_cancel(
            language,
            confirm_data="staff_op_confirm_create_user",
            cancel_data="staff_back_to_main"
        )

        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return CONFIRM_CREATE

    @require_auth
    @require_operator
    async def confirm_create(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and create the client user"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            client_data = context.user_data.get('new_client', {})

            async with api_client as client:
                response = await client.create_client_user(token, client_data)

            if not response.success:
                if response.status_code == 409:
                    await query.edit_message_text(
                        f"❌ {i18n.get('staff.operator.user_already_exists', language)}",
                        reply_markup=CommonKeyboards.back_button(language),
                        parse_mode='HTML'
                    )
                else:
                    await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            created_user = response.data or {}

            text = (
                f"✅ {i18n.get('staff.operator.user_created', language)}\n\n"
                f"👤 {escape_html(build_client_display_name(client_data))}\n"
                f"📞 {escape_html(client_data.get('phone', ''))}"
            )

            # Offer to create order for the new user
            user_id = created_user.get('id')
            if user_id:
                keyboard = OperatorKeyboards.user_found(language, user_id)
            else:
                keyboard = CommonKeyboards.back_button(language)

            # PAST THIS POINT THE CUSTOMER EXISTS: the POST returned 2xx.
            #
            # Telegram refuses `editMessageText` routinely — "message is not
            # modified", "message to edit not found" after 48 hours, a bubble
            # the operator deleted. That is a failure of the REDRAW, not of the
            # write, and the two must not share an `except`: the generic "an
            # error occurred" told an operator mid-call that a customer who
            # exists was never created, and the natural next move is to create
            # them again. Report what is true — it was created — and let the
            # stale screen be the smaller problem.
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            except Exception as render_error:
                logger.warning(
                    "Client %s created but the operator's success screen could not be "
                    "rendered: %s",
                    created_user.get('id'),
                    render_error,
                )
                await self._notify_user(
                    update,
                    f"✅ {i18n.get('staff.operator.user_created', language)}",
                    show_alert=True,
                )

        except Exception as e:
            logger.error(f"Error creating client user: {e}", exc_info=True)
            await self._handle_error(update, context)

        # Clear creation data
        context.user_data.pop('new_client', None)
        return ConversationHandler.END

    @require_auth
    @require_operator
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel user creation"""
        context.user_data.pop('new_client', None)
        language = await self._get_language(update, context)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                i18n.get('staff.cancelled', language),
                reply_markup=CommonKeyboards.back_button(language)
            )
        else:
            await update.message.reply_text(
                i18n.get('staff.cancelled', language),
                reply_markup=CommonKeyboards.back_button(language)
            )
        return ConversationHandler.END
