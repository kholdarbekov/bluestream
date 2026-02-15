"""
Help Handler for Staff Bot
Shows role-aware help information.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from keyboards.common import CommonKeyboards
from permissions import require_auth
from i18n import i18n

logger = logging.getLogger(__name__)


class HelpHandler(BaseHandler):
    """Handle help display"""

    @require_auth
    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show help text based on user roles"""
        language = await self._get_language(update, context)
        staff_roles = context.user_data.get('staff_roles', [])

        try:
            help_text = i18n.get('staff.help.text', language)

            if 'delivery_driver' in staff_roles:
                help_text += "\n\n" + i18n.get('staff.help.delivery', language)
            if 'operator' in staff_roles:
                help_text += "\n\n" + i18n.get('staff.help.operator', language)

            keyboard = CommonKeyboards.back_button(language)

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    help_text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    help_text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Error showing help: {e}", exc_info=True)
            await self._handle_error(update, context)
