"""
Main menu handler
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import MenuKeyboards
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

        menu_text = i18n.get('telegram.main_menu', language)
        keyboard = MenuKeyboards.main_menu(language)
        
        if update.callback_query:
            # Edit existing message
            await update.callback_query.edit_message_text(
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
        except:
            pass