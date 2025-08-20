"""
Start command handler and user registration
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import db_manager, BotUserRepository
from i18n import i18n
from keyboards import MenuKeyboards
from api_client import api_client
from utils import user_middleware

logger = logging.getLogger('handlers')


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
        logger.info("=== START HANDLER CALLED ===")
        user = update.effective_user
        user_id = user.id
        logger.info(f"User {user_id} (@{user.username}) called /start")
        
        # Initialize user repository
        user_repo = BotUserRepository(db_manager)
        
        # Check if user already exists
        existing_user = await user_repo.get_user_by_telegram_id(user_id)
        
        if not existing_user:
            # Create new user
            await user_repo.create_bot_user(
                telegram_id=user_id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code or 'en'
            )
            
            # Register user in business API
            async with api_client as client:
                registration_data = {
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'username': user.username,
                    'language_code': user.language_code or 'en'
                }
                await client.register_telegram_user(user_id, registration_data)
            
            is_new_user = True
        else:
            is_new_user = False
        
        # Get user's language preference
        language = await i18n.get_user_language(user_id)
        
        # Send welcome message
        if is_new_user:
            welcome_text = i18n.get('registration_welcome', language)
            await update.message.reply_text(
                welcome_text,
                reply_markup=MenuKeyboards.main_menu(language)
            )
        else:
            welcome_text = i18n.get('welcome', language)
            await update.message.reply_text(
                welcome_text,
                reply_markup=MenuKeyboards.main_menu(language)
            )
        
        # Log user interaction
        logger.info(f"User {user_id} started bot (new_user: {is_new_user})")
        
    except Exception as e:
        logger.error(f"Error in start handler: {e}")
        await update.message.reply_text(
            "❌ Something went wrong. Please try again later."
        )