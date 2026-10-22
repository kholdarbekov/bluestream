"""
Main menu handler
"""
import logging
from telegram import ReplyKeyboardRemove, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import BotUserRepository, db_manager
from i18n import i18n
from menu_keyboard import is_menu_customer, send_menu_keyboard
from utils import user_middleware

logger = logging.getLogger('handlers')


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle main menu display"""
    try:
        logger.info("=== MAIN MENU HANDLER CALLED ===")
        # Skip user middleware for now - authentication handled by API
        # user = await user_middleware(update)
        # if not user:
        #     return

        user_id = update.effective_user.id
        logger.info(f"Main menu requested by user {user_id}")
        language = await i18n.get_user_language(user_id)

        # Neutral, friendly greeting shown on every return to the menu.
        # (telegram.welcome — "registration complete" — is reserved for the
        # actual post-registration moment in profile.py.)
        menu_text = i18n.get('telegram.main_menu', language)

        if update.callback_query:
            # An old inline Back (the bottom keyboard is the menu now, spec 2026-10-21 §1). It only
            # takes that bubble's buttons away; the text stays. A bubble Telegram no longer lets us
            # edit (too old, or deleted) has nothing to strip, so that is logged and the tap answered.
            try:
                await update.callback_query.edit_message_reply_markup(reply_markup=None)
            except TelegramError as e:
                logger.info(f"Back on a bubble that could not be edited: {e}")
            await update.callback_query.answer()
        else:
            # /menu always installs the bottom keyboard again: the customer who types it may have
            # cleared the chat, and the install marker cannot know. Only a customer with a phone
            # gets it. The gate reads the LIVE row (as `send_menu_keyboard` does), never
            # `user_middleware`'s cache: a phone saved by signup or OTP minutes ago must count.
            row = await BotUserRepository(db_manager).get_user_by_telegram_id(user_id)
            if not row:
                await user_middleware(update)  # answers a chat without a row with the start prompt
                return
            if not is_menu_customer(row):
                await update.message.reply_text(
                    text=i18n.get('telegram.registration.start_command_prompt', language),
                    reply_markup=ReplyKeyboardRemove(),
                )
                return
            await send_menu_keyboard(
                context.bot, update.effective_chat.id, user_id, menu_text, language
            )

        logger.info(f"Main menu displayed for user {user_id}")

    except Exception as e:
        logger.error(f"Error in main menu handler: {e}")

        # Try to send error message
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('telegram.error_occurred', language)

            if update.callback_query:
                await update.callback_query.answer(error_msg)
            else:
                await update.message.reply_text(error_msg)
        except Exception as e:
            logger.warning(f"Failed to send error message in main menu fallback: {e}")
