"""
Language selection handler for Staff Bot
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from i18n import i18n
from keyboards.menu import MenuKeyboards

logger = logging.getLogger(__name__)


class LanguageHandler(BaseHandler):
    """Handles language selection for staff bot"""

    async def language_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show language selection menu"""
        language = await self._get_language(update, context)

        keyboard = []
        for lang_code in ['en', 'uz', 'ru']:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, language)
            current = " \u2713" if lang_code == language else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"{flag} {name}{current}",
                    callback_data=f"staff_set_language_{lang_code}"
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                i18n.get('staff.back', language),
                callback_data="staff_back_to_main"
            )
        ])

        text = i18n.get('staff.select_language', language)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif update.message:
            await update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set user's preferred language"""
        query = update.callback_query
        await query.answer()

        lang_code = query.data.replace('staff_set_language_', '')

        if lang_code not in ['en', 'uz', 'ru']:
            return

        # Update in context and database
        context.user_data['language'] = lang_code
        telegram_id = update.effective_user.id
        await self.user_repo.update_user_language(telegram_id, lang_code)

        logger.info(f"Staff user {telegram_id} changed language to {lang_code}")

        # Show confirmation and return to main menu
        staff_roles = context.user_data.get('staff_roles', [])
        await query.edit_message_text(
            i18n.get('staff.language_changed', lang_code),
            reply_markup=MenuKeyboards.main_menu_inline(lang_code, staff_roles)
        )
