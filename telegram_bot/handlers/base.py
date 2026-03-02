"""
Base handler class with shared error handling for Telegram bot handlers.
"""
import logging
from typing import Any
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from database import db_manager, BotUserRepository

logger = logging.getLogger(__name__)


class BaseHandler:
    """Base class for bot handler groups with shared error handling and user repository."""

    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)

    async def _edit_or_replace_callback_message(
        self,
        query: Any,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        """Edit callback text when possible, otherwise replace the message with a text reply."""
        kwargs = {"text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode

        try:
            await query.edit_message_text(**kwargs)
            return
        except Exception as exc:
            logger.warning("Failed to edit callback message text; falling back to replace message: %s", exc)

        message = getattr(query, "message", None)
        if not message:
            raise

        try:
            await message.delete()
        except Exception as delete_exc:
            logger.warning("Failed to delete callback message before fallback send: %s", delete_exc)

        await message.reply_text(**kwargs)

    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error."""
        error_msg = i18n.get('telegram.error.auth_failed', language)

        if update.callback_query:
            await self._edit_or_replace_callback_message(update.callback_query, error_msg)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(error_msg)

    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Handle API error."""
        error_msg = f"\u274c {error}"

        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)

    async def _handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Handle general error with language fallback."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('telegram.error_occurred', language)
        except Exception as e:
            logger.warning(f"Failed to get user language for error message: {e}")
            error_msg = i18n.get('telegram.error_occurred', 'en')

        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)

    async def _send_error_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message: str,
    ) -> None:
        """Send a formatted error message to the user."""
        try:
            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query,
                    f"\u274c {message}",
                )
            elif update.message:
                await update.message.reply_text(f"\u274c {message}")
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
