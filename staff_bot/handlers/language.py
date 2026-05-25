"""
Language selection handler for Staff Bot
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.permissions import require_any_staff_role, require_auth

logger = logging.getLogger(__name__)


class LanguageHandler(BaseHandler):
    """Handles language selection for staff bot"""

    @require_auth
    @require_any_staff_role
    async def language_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show language selection menu"""
        language = await self._get_language(update, context)

        keyboard = []
        for lang_code in ['en', 'uz', 'ru']:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, language)
            current = " ✓" if lang_code == language else ""
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

    @require_auth
    @require_any_staff_role
    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set user's preferred language"""
        query = update.callback_query
        await query.answer()

        raw_lang_code = query.data.replace('staff_set_language_', '').strip().lower().replace('_', '-')
        candidate_code = raw_lang_code.split('-', 1)[0]

        if candidate_code not in i18n.supported_languages:
            return
        lang_code = i18n.normalize_language(candidate_code)

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

        # Inline keyboards do not replace the user's persistent reply keyboard.
        # Send a fresh reply-keyboard menu so button labels immediately switch language.
        if query.message:
            await query.message.reply_text(
                i18n.get('staff.menu.title', lang_code),
                reply_markup=MenuKeyboards.main_menu(lang_code, staff_roles),
            )
