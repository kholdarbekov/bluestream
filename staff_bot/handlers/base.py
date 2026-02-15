"""
Base handler class with shared error handling for Staff Bot handlers.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from database import db_manager, StaffUserRepository

logger = logging.getLogger(__name__)


class BaseHandler:
    """Base class for staff bot handler groups with shared error handling."""

    def __init__(self):
        self.user_repo = StaffUserRepository(db_manager)

    async def _get_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get user language from context or database."""
        lang = context.user_data.get('language')
        if not lang:
            lang = await i18n.get_user_language(update.effective_user.id)
            context.user_data['language'] = lang
        return lang

    async def _get_auth_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get auth token from context, using token_manager for refresh if needed."""
        token_manager = context.bot_data.get('token_manager')
        if token_manager:
            from api_client import api_client
            token = await token_manager.get_valid_token(update.effective_user.id, api_client)
            if token:
                return token

        # Fallback to stored token in user_data
        return context.user_data.get('access_token')

    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error."""
        error_msg = i18n.get('staff.session_expired', language)
        if update.callback_query:
            await update.callback_query.answer(error_msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(error_msg)

    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Handle API error."""
        error_msg = f"\u274c {error}"
        if update.callback_query:
            await update.callback_query.answer(error_msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(error_msg)

    async def _handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Handle general error with language fallback."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('staff.error_occurred', language)
        except Exception:
            error_msg = "An error occurred. Please try again."

        if update.callback_query:
            await update.callback_query.answer(error_msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(error_msg)
