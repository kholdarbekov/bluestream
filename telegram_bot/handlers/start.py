"""
Start command handler and user registration
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from database import db_manager, BotUserRepository
from i18n import i18n
from keyboards import MenuKeyboards, LanguageKeyboards
from api_client import api_client
from utils import user_middleware, authenticate_telegram_user

logger = logging.getLogger('handlers')


async def handle_auth_linking(update: Update, auth_code: str) -> bool:
    """
    Handle authentication linking for web-to-telegram flow
    
    Args:
        update: Telegram update object
        auth_code: Authentication code from web app
        
    Returns:
        True if linking was successful, False otherwise
    """
    try:
        user = update.effective_user
        user_id = user.id
        
        # Prepare data for verification API call
        auth_data = {
            'telegram_id': str(user_id),
            'telegram_username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name
        }
        
        # Call the verification endpoint
        async with api_client as client:
            response = await client._make_request(
                'POST', 
                f'/api/v1/auth/verify-telegram-auth/{auth_code}',
                data=auth_data
            )
            
            if response.success:
                await update.message.reply_text(
                    "✅ *Account Successfully Linked!*\n\n"
                    "Your Telegram account has been linked to your web account. "
                    "You now have access to both platforms with a unified experience!\n\n"
                    "🌐 *What you can do now:*\n"
                    "• Use this bot for quick orders and notifications\n"
                    "• Access the full web app with your existing login\n"
                    "• Sync your preferences across both platforms\n\n"
                    "Welcome to the complete BluStream experience! 🎉",
                    parse_mode='Markdown'
                )
                
                # Get user's language and show main menu
                language = await i18n.get_user_language(user_id)
                await update.message.reply_text(
                    i18n.get('main_menu_prompt', language, default="Choose an option:"),
                    reply_markup=MenuKeyboards.main_menu(language)
                )
                
                logger.info(f"Successfully linked Telegram user {user_id} to web account")
                return True
            else:
                error_message = response.error or "Unknown error occurred"
                
                if 'already linked' in error_message.lower():
                    await update.message.reply_text(
                        "ℹ️ *Account Already Linked*\n\n"
                        "This Telegram account is already linked to another user. "
                        "If you believe this is an error, please contact support."
                    )
                elif 'expired' in error_message.lower():
                    await update.message.reply_text(
                        "⏰ *Link Expired*\n\n"
                        "The authentication code has expired. Please generate a new "
                        "link from the web app and try again."
                    )
                else:
                    await update.message.reply_text(
                        "❌ *Linking Failed*\n\n"
                        f"Unable to link your accounts: {error_message}\n\n"
                        "Please try generating a new link from the web app."
                    )
                
                logger.warning(f"Failed to link Telegram user {user_id}: {error_message}")
                return False
                
    except Exception as e:
        logger.error(f"Error in auth linking for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ *Authentication Error*\n\n"
            "An error occurred while linking your accounts. "
            "Please try again or contact support if the problem persists."
        )
        return False


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    try:
        logger.info("=== START HANDLER CALLED ===")
        user = update.effective_user
        user_id = user.id
        logger.info(f"User {user_id} (@{user.username}) called /start, context.args: {context.args}")
        
        # Check if this is an authentication linking request
        if context.args and len(context.args) > 0:
            arg = context.args[0]
            if arg.startswith('auth_'):
                auth_code = arg[5:]  # Remove 'auth_' prefix
                logger.info(f"Processing authentication code: {auth_code}")
                
                # Handle authentication linking
                success = await handle_auth_linking(update, auth_code)
                if success:
                    return
                # If linking failed, continue with normal start flow
        
        # Initialize user repository
        user_repo = BotUserRepository(db_manager)
        
        # Check if user already exists
        existing_user = await user_repo.get_user_by_telegram_id(user_id)
        logger.info(f"existing_user {existing_user} for user_id {user_id}")
        
        if not existing_user:
            # Register user through business API (unified user creation)
            try:
                async with api_client as client:
                    language_code = user.language_code if user.language_code in ('en', 'uz', 'ru') else 'ru'
                    registration_data = {
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'username': user.username,
                        'language_code': language_code
                    }
                    response = await client.register_telegram_user(user_id, registration_data)
                    if not response.success:
                        logger.error(f"Failed to register telegram user {user_id}: {response.error}")
                        # Fall back to error message but don't crash the bot
                        await update.message.reply_text(
                            "❌ Registration failed. Please try again later or contact support."
                        )
                        return
                
                is_new_user = True
            except Exception as e:
                logger.error(f"Exception during telegram user registration: {e}")
                await update.message.reply_text(
                    "❌ Registration failed. Please try again later."
                )
                return
        else:
            is_new_user = False
        
        # Get user's language preference
        language = await i18n.get_user_language(user_id)
        logger.info(f"language {language} for user_id {user_id}")

        # Send welcome message
        if is_new_user:
            welcome_text_en = i18n.get('registration_welcome', "en")
            welcome_text_uz = i18n.get('registration_welcome', "uz")
            welcome_text_ru = i18n.get('registration_welcome', "ru")
            welcome_text = f"{welcome_text_en}\n\n{welcome_text_uz}\n\n{welcome_text_ru}"
            await update.message.reply_text(
                welcome_text,
                reply_markup=LanguageKeyboards.select_language()
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