"""
Utility functions for the Telegram Bot
"""
import logging
import asyncio
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from functools import wraps
import json

import sentry_sdk
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from database import db_manager, BotUserRepository
from i18n import i18n
from config import config

logger = logging.getLogger('utils')


class RateLimiter:
    """Rate limiting for bot requests"""
    
    def __init__(self):
        self.requests: Dict[int, list] = {}
        self.max_requests = config.telegram.rate_limit_requests
        self.window_seconds = config.telegram.rate_limit_window
    
    async def allow_request(self, user_id: int) -> bool:
        """Check if user is within rate limits"""
        if not config.telegram.rate_limit_enabled:
            return True
        
        now = datetime.now()
        
        # Clean old requests
        if user_id in self.requests:
            self.requests[user_id] = [
                req_time for req_time in self.requests[user_id]
                if now - req_time < timedelta(seconds=self.window_seconds)
            ]
        else:
            self.requests[user_id] = []
        
        # Check if under limit
        if len(self.requests[user_id]) >= self.max_requests:
            return False
        
        # Add current request
        self.requests[user_id].append(now)
        return True


class UserCache:
    """Cache for user data"""
    
    def __init__(self):
        self.cache: Dict[int, Dict[str, Any]] = {}
        self.cache_timeout = 300  # 5 minutes
    
    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user data from cache"""
        if user_id in self.cache:
            data, timestamp = self.cache[user_id]
            if datetime.now() - timestamp < timedelta(seconds=self.cache_timeout):
                return data
            else:
                # Expired, remove from cache
                del self.cache[user_id]
        
        return None
    
    def set(self, user_id: int, data: Dict[str, Any]):
        """Set user data in cache"""
        self.cache[user_id] = (data, datetime.now())
    
    def remove(self, user_id: int):
        """Remove user from cache"""
        if user_id in self.cache:
            del self.cache[user_id]


# Global instances
rate_limiter = RateLimiter()
user_cache = UserCache()


async def authenticate_telegram_user(update: Update, api_client_instance) -> Optional[str]:
    """Authenticate telegram user with business API and return token"""
    try:
        logger.info("=== AUTHENTICATION DEBUG START ===")
        
        if not update.effective_user:
            logger.error("No effective_user found in update")
            return None
        
        user_id = update.effective_user.id
        logger.info(f"Authenticating user ID: {user_id}")
        
        # Prepare user data from Telegram
        user_data = {
            'username': update.effective_user.username,
            'first_name': update.effective_user.first_name,
            'last_name': update.effective_user.last_name
        }
        logger.info(f"User data prepared: {user_data}")
        
        # Authenticate with business API
        logger.info("Calling api_client_instance.authenticate_user...")
        user_token = await api_client_instance.authenticate_user(user_id, user_data)
        
        if user_token:
            logger.info(f"Authentication successful, token received: {user_token[:20]}...")
        else:
            logger.error("Authentication failed - no token returned")
        
        logger.info("=== AUTHENTICATION DEBUG END ===")
        return user_token
        
    except Exception as e:
        logger.error(f"Error in authenticate_telegram_user: {e}")
        logger.error(f"Exception type: {type(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


async def user_middleware(update: Update) -> Optional[Dict[str, Any]]:
    """User middleware to ensure user exists and is authenticated"""
    try:
        if not update.effective_user:
            return None
        
        user_id = update.effective_user.id
        
        # Check cache first
        cached_user = user_cache.get(user_id)
        if cached_user:
            return cached_user
        
        # Get user from database
        user_repo = BotUserRepository(db_manager)
        user_data = await user_repo.get_user_by_telegram_id(user_id)
        
        if not user_data:
            # User doesn't exist, redirect to start
            language = update.effective_user.language_code or 'en'
            welcome_msg = i18n.get('telegram.registration_welcome', language)
            
            try:
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        f"{welcome_msg}\n\nPlease start with /start command."
                    )
                    await update.callback_query.answer()
                else:
                    await update.message.reply_text(
                        f"{welcome_msg}\n\nPlease start with /start command."
                    )
            except Exception as e:
                logger.error(f"Error sending registration message: {e}")
            
            return None
        
        # Cache user data
        user_cache.set(user_id, user_data)
        
        return user_data
        
    except Exception as e:
        logger.error(f"Error in user middleware: {e}")
        return None


def format_price(price: float) -> str:
    """Format price with thousands separator"""
    return f"{price:,.0f}"


def format_datetime(dt: datetime, language: str = 'en') -> str:
    """Format datetime for display"""
    if language == 'uz':
        return dt.strftime("%d.%m.%Y, %H:%M")
    elif language == 'ru':
        return dt.strftime("%d.%m.%Y, %H:%M")
    else:  # English
        return dt.strftime("%m/%d/%Y, %I:%M %p")


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text if it's too long"""
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for the bot"""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Try to get user information
    user_id = None
    language = 'en'
    username = None

    try:
        if isinstance(update, Update) and update.effective_user:
            user_id = update.effective_user.id
            username = update.effective_user.username
            language = await i18n.get_user_language(user_id)
    except:
        pass

    # Capture exception with Sentry
    try:
        with sentry_sdk.push_scope() as scope:
            # Add user context
            if user_id:
                scope.set_user({
                    "id": str(user_id),
                    "username": username,
                })

            # Add extra context about the update
            if isinstance(update, Update):
                scope.set_extra("update_id", update.update_id)
                if update.message:
                    scope.set_extra("message_text", update.message.text[:100] if update.message.text else None)
                    scope.set_extra("chat_id", update.message.chat_id)
                if update.callback_query:
                    scope.set_extra("callback_data", update.callback_query.data)

            # Tag with error type
            if context.error:
                scope.set_tag("error_type", type(context.error).__name__)

            # Capture the exception
            sentry_sdk.capture_exception(context.error)
    except Exception as sentry_error:
        logger.error(f"Failed to capture exception with Sentry: {sentry_error}")

    # Prepare error message
    error_msg = i18n.get('telegram.error_occurred', language)

    # Try to send error message to user
    try:
        if isinstance(update, Update):
            if update.callback_query:
                await update.callback_query.answer(error_msg)
            elif update.message:
                await update.message.reply_text(error_msg)
            elif update.edited_message:
                await update.edited_message.reply_text(error_msg)
    except Exception as send_error:
        logger.error(f"Failed to send error message to user: {send_error}")

    # Log error to analytics
    try:
        if user_id:
            await log_bot_analytics(
                user_id,
                'error',
                'system_error',
                {'error_type': str(type(context.error)), 'error_message': str(context.error)},
                success=False,
                error_message=str(context.error)
            )
    except Exception as analytics_error:
        logger.error(f"Failed to log error analytics: {analytics_error}")


async def log_bot_analytics(user_id: int, command: str, action: str, 
                           data: Dict = None, success: bool = True, 
                           error_message: str = None):
    """Log bot analytics to database"""
    try:
        query = """
        INSERT INTO bot_analytics (telegram_id, command, action, data, success, error_message)
        VALUES ($1, $2, $3, $4, $5, $6)
        """
        
        await db_manager.execute(
            query,
            user_id,
            command,
            action,
            json.dumps(data or {}),
            success,
            error_message
        )
    except Exception as e:
        logger.error(f"Failed to log analytics: {e}")


def admin_required(func):
    """Decorator to require admin privileges"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        
        if not config.is_admin(user_id):
            language = await i18n.get_user_language(user_id)
            error_msg = "❌ Admin access required"
            
            if update.callback_query:
                await update.callback_query.answer(error_msg)
            else:
                await update.message.reply_text(error_msg)
            
            return
        
        return await func(update, context, *args, **kwargs)
    
    return wrapper


async def send_typing_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send typing action to show bot is working"""
    try:
        if update.effective_chat:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id,
                action="typing"
            )
    except Exception as e:
        logger.warning(f"Failed to send typing action: {e}")


def escape_markdown(text: str) -> str:
    """Escape markdown special characters"""
    special_chars = ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


async def validate_phone_number(phone: str) -> bool:
    """Validate phone number format"""
    import re
    
    # Uzbekistan phone number pattern
    pattern = r'^(\+998|998|8)?[0-9]{9}$'
    
    # Clean phone number
    clean_phone = re.sub(r'[\s\-\(\)]', '', phone)
    
    return bool(re.match(pattern, clean_phone))


def normalize_phone_number(phone: str) -> str:
    """Normalize phone number to standard format"""
    import re
    
    # Remove all non-digit characters except +
    clean_phone = re.sub(r'[^\d+]', '', phone)
    
    # Handle different formats
    if clean_phone.startswith('+998'):
        return clean_phone
    elif clean_phone.startswith('998'):
        return f'+{clean_phone}'
    elif clean_phone.startswith('8') and len(clean_phone) == 10:
        return f'+99{clean_phone[1:]}'
    elif len(clean_phone) == 9:
        return f'+998{clean_phone}'
    
    return clean_phone


async def get_user_cart(user_id: int) -> Dict[str, Any]:
    """Get user's shopping cart from cache/database"""
    # This would typically use Redis for cart storage
    # For now, return empty cart
    return {
        'items': [],
        'total': 0,
        'item_count': 0
    }


async def add_to_user_cart(user_id: int, product_id: int, quantity: int) -> bool:
    """Add item to user's cart"""
    try:
        # This would typically store in Redis
        # For now, just log the action
        logger.info(f"Added product {product_id} (qty: {quantity}) to cart for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to add to cart: {e}")
        return False


async def clear_user_cart(user_id: int) -> bool:
    """Clear user's shopping cart"""
    try:
        # This would typically clear Redis cart
        logger.info(f"Cleared cart for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to clear cart: {e}")
        return False


def chunk_list(lst: list, chunk_size: int) -> list:
    """Split list into chunks of specified size"""
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


async def is_business_hours() -> bool:
    """Check if current time is within business hours"""
    now = datetime.now()
    current_hour = now.hour
    
    business_start = config.features.get('business_hours_start', 9)
    business_end = config.features.get('business_hours_end', 21)
    
    return business_start <= current_hour < business_end


class MessageBuilder:
    """Helper class for building formatted messages"""
    
    @staticmethod
    def build_order_summary(order: Dict[str, Any], language: str = 'en') -> str:
        """Build order summary message"""
        lines = [
            f"📋 {i18n.get('telegram.order.number', language, order.get('order_number', 'N/A'))}",
            f"📅 Date: {order.get('created_at', 'N/A')[:10]}",
            f"💰 {i18n.get('telegram.order.total', language, format_price(order.get('total_amount', 0)))}"
        ]
        
        if order.get('status'):
            status_icons = {
                'pending': '🕐',
                'confirmed': '✅',
                'preparing': '👨‍🍳',
                'out_for_delivery': '🚚',
                'delivered': '📦',
                'cancelled': '❌'
            }
            icon = status_icons.get(order['status'], '📋')
            lines.append(f"📊 Status: {icon} {order['status'].replace('_', ' ').title()}")
        
        return '\n'.join(lines)
    
    @staticmethod
    def build_product_summary(product: Dict[str, Any], language: str = 'en') -> str:
        """Build product summary message"""
        lines = [
            f"🏷️ {product.get('name', 'Unknown Product')}",
            f"💰 {format_price(product['pricing'].get('base_price', 0))} UZS"
        ]
        
        if product['specifications'].get('volume'):
            lines.append(f"📦 {product['specifications']['volume']}{product['specifications'].get('volume_unit', '')}")
        
        if product['inventory'].get('stock_quantity') is not None:
            stock = product['inventory']['stock_quantity']
            status = "✅ In Stock" if stock > 0 else "❌ Out of Stock"
            lines.append(f"📊 {status}")
        
        return '\n'.join(lines)