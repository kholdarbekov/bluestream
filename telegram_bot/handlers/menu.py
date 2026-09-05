"""
Main menu handler
"""
import logging
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from eligibility import main_menu_for
from i18n import i18n
from keyboards import MenuKeyboards
from utils import maybe_remove_stale_reply_keyboard

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
        await maybe_remove_stale_reply_keyboard(update, context)

        # Neutral, friendly greeting shown on every return to the menu.
        # (telegram.welcome — "registration complete" — is reserved for the
        # actual post-registration moment in profile.py.)
        menu_text = i18n.get('telegram.main_menu', language)
        keyboard = await main_menu_for(update.effective_user.id, language)

        if update.callback_query:
            # Edit when the tapped message HAS text; otherwise replace it.
            #
            # A one-category shop sends its product list as a PHOTO
            # (`products.py` deletes the message and `send_photo`s the list), and
            # a photo has no text to edit — Telegram answers 400 "there is no
            # text in the message to edit". The `except` below only answers a
            # generic error toast and sends nothing, so Back from that screen
            # left the customer stranded on the one screen whose only way out
            # had just failed.
            #
            # `BaseHandler._edit_or_replace_callback_message` is the same rule
            # for handlers that have a `self`; this is a module-level function,
            # so the fallback is spelled out here rather than reached for.
            # tests/telegram_bot/test_menu_and_link_buttons_after_restart.py
            try:
                await update.callback_query.edit_message_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
            except BadRequest:
                await update.callback_query.message.reply_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
            await update.callback_query.answer()
        else:
            # Send new message
            await update.message.reply_text(
                text=menu_text,
                reply_markup=keyboard
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
