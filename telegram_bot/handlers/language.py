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
            
            menu_text = f"{i18n.get('menu_language', current_language)}\n\nSelect your preferred language:"
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
            
            logger.info(f"Language menu displayed for user {user_id}")
            
        except Exception as e:
            logger.error(f"Error in language menu: {e}")
            
            try:
                language = await i18n.get_user_language(update.effective_user.id)
                error_msg = i18n.get('error_occurred', language)
                
                if update.callback_query:
                    await update.callback_query.answer(error_msg)
                else:
                    await update.message.reply_text(error_msg)
            except:
                pass
    
    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            
            # Extract language code from callback data
            language_code = query.data.split('_')[-1]
            
            # Validate language code
            if language_code not in config.localization.supported_languages:
                await query.answer("❌ Invalid language selection")
                return
            
            # Update user language in database
            await self.user_repo.update_user_language(user_id, language_code)
            
            # Show success message in new language
            success_msg = i18n.get('success', language_code)
            flag = i18n.get_language_flag(language_code)
            language_name = i18n.get_language_name(language_code, language_code)
            
            await query.answer(f"{flag} Language changed to {language_name}")
            
            # Return to main menu with new language
            menu_text = i18n.get('main_menu', language_code)
            keyboard = MenuKeyboards.main_menu(language_code)
            
            await query.edit_message_text(
                text=menu_text,
                reply_markup=keyboard
            )
            
            logger.info(f"User {user_id} changed language to {language_code}")
            
        except Exception as e:
            logger.error(f"Error setting language: {e}")
            
            try:
                await update.callback_query.answer("❌ Error changing language")
            except:
                pass


# Create global handler instance
language_handler = LanguageHandler()