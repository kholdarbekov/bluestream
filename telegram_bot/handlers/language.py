"""
Language selection handlers
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import LanguageKeyboards, MenuKeyboards
from database import db_manager, BotUserRepository
from utils import user_middleware
from config import config

logger = logging.getLogger(__name__)


class LanguageHandler:
    """Language selection handler class"""

    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)

    async def language_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show language selection menu"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            current_language = await i18n.get_user_language(user_id)

            # Build enhanced language selection message
            current_flag = i18n.get_language_flag(current_language)
            current_name = i18n.get_language_name(current_language, current_language)

            menu_text = f"🌐 {i18n.get('telegram.menu.language', current_language)}\n\n"
            menu_text += f"{i18n.get('telegram.language.current', current_language)}: {current_flag} {current_name}\n\n"
            menu_text += f"{i18n.get('telegram.language.select_prompt', current_language)}"

            keyboard = LanguageKeyboards.language_selection(current_language)

            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=menu_text,
                    reply_markup=keyboard
                )

            logger.info(f"Language menu displayed for user {user_id} (current: {current_language})")

        except Exception as e:
            logger.error(f"Error in language menu: {e}")

            try:
                language = await i18n.get_user_language(update.effective_user.id)
                error_msg = i18n.get('telegram.error_occurred', language)

                if update.callback_query:
                    await update.callback_query.answer(error_msg)
                else:
                    await update.message.reply_text(error_msg)
            except Exception as e:
                logger.warning(f"Failed to send error message in language handler fallback: {e}")

    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id

            # Extract language code from callback data
            language_code = query.data.split('_')[-1]

            # Get current language before change
            current_language = await i18n.get_user_language(user_id)

            # Validate language code
            if language_code not in config.localization.supported_languages:
                await query.answer(i18n.get('telegram.language.invalid_selection', current_language))
                return

            # Check if already using this language
            if language_code == current_language:
                flag = i18n.get_language_flag(language_code)
                language_name = i18n.get_language_name(language_code, language_code)
                already_using_msg = i18n.get('telegram.language.already_selected', language_code)
                await query.answer(f"{flag} {already_using_msg}")
                return

            # Update user language in database
            await self.user_repo.update_user_language(user_id, language_code)

            # Build comprehensive success message with language preview
            flag = i18n.get_language_flag(language_code)
            language_name = i18n.get_language_name(language_code, language_code)

            # Show popup notification
            success_msg = i18n.get('telegram.language.changed_success', language_code)
            await query.answer(f"{flag} {success_msg}", show_alert=False)

            # Build detailed confirmation message showing language change
            confirmation_text = f"{flag} {i18n.get('telegram.language.confirmation_title', language_code)}\n\n"
            now_using_template = i18n.get('telegram.language.now_using', language_code)
            try:
                now_using_text = now_using_template.format(
                    language=language_name,
                    language_name=language_name,
                )
            except Exception:
                now_using_text = f"{now_using_template} {language_name}"
            confirmation_text += f"✅ {now_using_text}\n\n"
            confirmation_text += f"{i18n.get('telegram.language.confirmation_message', language_code)}"

            # Return to main menu with new language
            keyboard = MenuKeyboards.main_menu(language_code)

            await query.edit_message_text(
                text=confirmation_text,
                reply_markup=keyboard
            )

            logger.info(f"User {user_id} changed language from {current_language} to {language_code}")

        except Exception as e:
            logger.error(f"Error setting language: {e}")

            try:
                current_language = await i18n.get_user_language(user_id)
                await update.callback_query.answer(i18n.get('telegram.language.error_changing', current_language))
            except Exception as e:
                logger.warning(f"Failed to send error message in set_language fallback: {e}")


# Create global handler instance
language_handler = LanguageHandler()
