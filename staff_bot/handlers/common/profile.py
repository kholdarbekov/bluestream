"""
Profile Handler for Staff Bot
Shows staff member's profile information.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.permissions import require_auth, require_any_staff_role
from staff_bot.i18n import i18n
from staff_bot.utils.formatters import escape_html

logger = logging.getLogger(__name__)


class ProfileHandler(BaseHandler):
    """Handle staff profile display"""

    @require_auth
    @require_any_staff_role
    async def show_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show staff member's profile"""
        language = await self._get_language(update, context)

        try:
            first_name = context.user_data.get('first_name', '')
            last_name = context.user_data.get('last_name', '')
            full_name = escape_html(
                f"{first_name} {last_name}".strip() or i18n.get('staff.common.not_available', language)
            )
            phone = escape_html(
                context.user_data.get('phone') or i18n.get('staff.common.not_available', language)
            )
            staff_roles = context.user_data.get('staff_roles', [])

            role_labels = []
            for role in staff_roles:
                role_labels.append(i18n.get(f'staff.role.{role}', language))

            lines = [
                f"\U0001f464 <b>{i18n.get('staff.profile.title', language)}</b>\n",
                f"\U0001f464 {i18n.get('staff.profile.name', language)}: {full_name}",
                f"\U0001f4de {i18n.get('staff.profile.phone', language)}: {phone}",
                f"\U0001f3f7 {i18n.get('staff.profile.roles', language)}: {', '.join(role_labels)}",
                f"\U0001f310 {i18n.get('staff.profile.language', language)}: {language.upper()}",
            ]

            text = '\n'.join(lines)
            keyboard = MenuKeyboards.profile_hub(language, staff_roles)

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as e:
            logger.error(f"Error showing profile: {e}", exc_info=True)
            await self._handle_error(update, context)
