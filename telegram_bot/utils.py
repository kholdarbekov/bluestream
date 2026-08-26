"""
Utility functions for the Telegram Bot
"""
import logging
import asyncio
from typing import Dict, Optional, Any
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps
import json

from shared.constants import DISPLAY_TIMEZONE

import redis.asyncio as aioredis
import sentry_sdk
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from telegram.error import TelegramError, NetworkError, TimedOut

from database import db_manager, BotUserRepository
from i18n import i18n
from config import config
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger('utils')

_NETWORK_ERROR_LOG_COOLDOWN_SECONDS = 60
_last_network_error_log_at: Optional[datetime] = None
_REPLY_KEYBOARD_CLEANUP_COOLDOWN_SECONDS = 6 * 3600


def _is_transient_polling_network_error(update: object, error: Exception) -> bool:
    """Return True for expected polling transport disconnects that auto-retry."""
    if isinstance(update, Update):
        return False
    if not isinstance(error, NetworkError):
        return False

    error_text = str(error)
    transient_markers = (
        'RemoteProtocolError',
        'Server disconnected without sending a response',
        'ReadTimeout',
        'ConnectError',
        'PoolTimeout',
    )
    return any(marker in error_text for marker in transient_markers)


def _is_transient_telegram_request_error(error: Exception) -> bool:
    """Return True for transient Telegram request transport failures."""
    if isinstance(error, TimedOut):
        return True
    if not isinstance(error, NetworkError):
        return False

    error_text = str(error)
    transient_markers = (
        'Timed out',
        'ConnectTimeout',
        'ReadTimeout',
        'WriteTimeout',
        'PoolTimeout',
        'ConnectError',
        'RemoteProtocolError',
        'Server disconnected without sending a response',
    )
    return any(marker in error_text for marker in transient_markers)


def _should_log_network_warning() -> bool:
    """Rate-limit repeated transient polling warning logs."""
    global _last_network_error_log_at
    now = datetime.now(timezone.utc)
    if _last_network_error_log_at is None:
        _last_network_error_log_at = now
        return True
    if (now - _last_network_error_log_at).total_seconds() >= _NETWORK_ERROR_LOG_COOLDOWN_SECONDS:
        _last_network_error_log_at = now
        return True
    return False


class RateLimiter:
    """Redis-backed sliding window rate limiter for bot requests.

    Fails closed when Redis is unavailable (audit BOT-005 default) so multi-replica
    deployments do not silently degrade into `N_replicas × configured_limit`. Ops can
    set ``TELEGRAM_RATE_LIMIT_FAIL_MODE=open`` to preserve availability at the cost
    of defense — every denial in that mode emits a critical Sentry alert.
    """

    _FAIL_ALERT_COOLDOWN_SECONDS = 300  # throttle repeated Sentry alerts

    def __init__(self):
        self.max_requests = config.telegram.rate_limit_requests
        self.window_seconds = config.telegram.rate_limit_window
        self._fail_mode = (config.telegram.rate_limit_fail_mode or 'closed').lower()
        self._retry_seconds = max(5, int(config.telegram.rate_limit_redis_retry_seconds or 30))
        self._redis: Optional[aioredis.Redis] = None
        self._redis_available = False
        self._last_connect_attempt: Optional[datetime] = None
        self._last_fail_alert_at: Optional[datetime] = None

    async def _ensure_redis(self) -> bool:
        """Connect to Redis, retrying periodically on failure."""
        if self._redis_available:
            return True

        now = datetime.now(timezone.utc)
        if (
            self._last_connect_attempt is not None
            and (now - self._last_connect_attempt).total_seconds() < self._retry_seconds
        ):
            return False  # recent failure — back off
        self._last_connect_attempt = now

        try:
            if self._redis is None:
                self._redis = aioredis.from_url(
                    config.redis.url, encoding='utf-8', decode_responses=True
                )
            await self._redis.ping()
            self._redis_available = True
            logger.info("RateLimiter connected to Redis")
            return True
        except Exception as e:
            self._redis_available = False
            logger.error("RateLimiter Redis unavailable: %s", e)
            return False

    def _report_redis_failure(self, reason: str) -> None:
        """Send a throttled Sentry alert when Redis fails (fail-closed or -open)."""
        now = datetime.now(timezone.utc)
        if (
            self._last_fail_alert_at is not None
            and (now - self._last_fail_alert_at).total_seconds() < self._FAIL_ALERT_COOLDOWN_SECONDS
        ):
            return
        self._last_fail_alert_at = now
        logger.critical("RateLimiter Redis unavailable, fail_mode=%s: %s", self._fail_mode, reason)
        try:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag('component', 'bot_rate_limiter')
                scope.set_tag('fail_mode', self._fail_mode)
                scope.set_level('error')
                sentry_sdk.capture_message(
                    f"Bot rate limiter Redis unavailable (fail_mode={self._fail_mode}): {reason}"
                )
        except Exception:
            pass

    async def allow_request(self, user_id: int) -> bool:
        """Return True if the user is within rate limits.

        Falls closed (returns False) when Redis is unavailable unless
        ``TELEGRAM_RATE_LIMIT_FAIL_MODE=open`` is set.
        """
        if not config.telegram.rate_limit_enabled:
            return True

        if await self._ensure_redis():
            try:
                return await self._allow_request_redis(user_id)
            except Exception as e:
                self._redis_available = False
                self._report_redis_failure(f"pipeline error: {e}")
                return self._fail_mode == 'open'

        self._report_redis_failure("connection unavailable")
        return self._fail_mode == 'open'

    async def _allow_request_redis(self, user_id: int) -> bool:
        """Sliding window counter via Redis sorted set."""
        key = RedisKeyspace.bot_rate_limit(user_id)
        now = datetime.now(timezone.utc).timestamp()
        window_start = now - self.window_seconds

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, self.window_seconds)
        results = await pipe.execute()

        current_count = results[1]  # zcard result
        return current_count < self.max_requests


class UserCache:
    """Cache for user data with max size and periodic cleanup"""

    MAX_SIZE = 10000
    CLEANUP_INTERVAL = 300  # 5 minutes
    DEFAULT_CACHE_TIMEOUT = 300  # 5 minutes

    def __init__(self):
        self.cache: Dict[int, Dict[str, Any]] = {}
        self.cache_timeout = self.DEFAULT_CACHE_TIMEOUT
        self._last_cleanup = datetime.now(timezone.utc)

    def _periodic_cleanup(self):
        """Remove expired entries periodically"""
        now = datetime.now(timezone.utc)
        if now - self._last_cleanup < timedelta(seconds=self.CLEANUP_INTERVAL):
            return
        self._last_cleanup = now
        cutoff = now - timedelta(seconds=self.cache_timeout)
        expired_keys = [
            uid for uid, (_, ts) in self.cache.items()
            if ts < cutoff
        ]
        for uid in expired_keys:
            del self.cache[uid]

    def get(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get user data from cache"""
        if user_id in self.cache:
            data, timestamp = self.cache[user_id]
            if datetime.now(timezone.utc) - timestamp < timedelta(seconds=self.cache_timeout):
                return data
            else:
                del self.cache[user_id]
        return None

    def set(self, user_id: int, data: Dict[str, Any]):
        """Set user data in cache"""
        self._periodic_cleanup()
        # Evict oldest entries if at max size
        if len(self.cache) >= self.MAX_SIZE and user_id not in self.cache:
            oldest_key = min(self.cache, key=lambda k: self.cache[k][1])
            del self.cache[oldest_key]
        self.cache[user_id] = (data, datetime.now(timezone.utc))

    def remove(self, user_id: int):
        """Remove user from cache"""
        if user_id in self.cache:
            del self.cache[user_id]


class OTPRateLimiter:
    """Redis-backed rate limiter for OTP requests (stricter limits).

    Fails closed when Redis is unavailable — OTP is a brute-force target, so an
    in-memory fallback across replicas would silently multiply the effective limit.
    Shares the fail_mode and retry cadence with ``RateLimiter`` (see BOT-005).
    """

    MAX_OTP_REQUESTS = 3
    OTP_WINDOW_SECONDS = 300  # 5 minutes
    _FAIL_ALERT_COOLDOWN_SECONDS = 300

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._redis_available = False
        self._fail_mode = (config.telegram.rate_limit_fail_mode or 'closed').lower()
        self._retry_seconds = max(5, int(config.telegram.rate_limit_redis_retry_seconds or 30))
        self._last_connect_attempt: Optional[datetime] = None
        self._last_fail_alert_at: Optional[datetime] = None

    async def _ensure_redis(self) -> bool:
        if self._redis_available:
            return True

        now = datetime.now(timezone.utc)
        if (
            self._last_connect_attempt is not None
            and (now - self._last_connect_attempt).total_seconds() < self._retry_seconds
        ):
            return False
        self._last_connect_attempt = now

        try:
            if self._redis is None:
                self._redis = aioredis.from_url(
                    config.redis.url, encoding='utf-8', decode_responses=True
                )
            await self._redis.ping()
            self._redis_available = True
            return True
        except Exception as e:
            self._redis_available = False
            logger.error("OTPRateLimiter Redis unavailable: %s", e)
            return False

    def _report_redis_failure(self, reason: str) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._last_fail_alert_at is not None
            and (now - self._last_fail_alert_at).total_seconds() < self._FAIL_ALERT_COOLDOWN_SECONDS
        ):
            return
        self._last_fail_alert_at = now
        logger.critical("OTPRateLimiter Redis unavailable, fail_mode=%s: %s", self._fail_mode, reason)
        try:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag('component', 'bot_otp_rate_limiter')
                scope.set_tag('fail_mode', self._fail_mode)
                scope.set_level('error')
                sentry_sdk.capture_message(
                    f"Bot OTP rate limiter Redis unavailable (fail_mode={self._fail_mode}): {reason}"
                )
        except Exception:
            pass

    async def allow_otp_request(self, user_id: int) -> bool:
        """Return True if the user is within OTP rate limits."""
        if await self._ensure_redis():
            try:
                return await self._allow_redis(user_id)
            except Exception as e:
                self._redis_available = False
                self._report_redis_failure(f"pipeline error: {e}")
                return self._fail_mode == 'open'

        self._report_redis_failure("connection unavailable")
        return self._fail_mode == 'open'

    async def _allow_redis(self, user_id: int) -> bool:
        key = RedisKeyspace.bot_otp_rate_limit(user_id)
        now = datetime.now(timezone.utc).timestamp()
        window_start = now - self.OTP_WINDOW_SECONDS

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, self.OTP_WINDOW_SECONDS)
        results = await pipe.execute()

        current_count = results[1]
        return current_count < self.MAX_OTP_REQUESTS


# Global instances
rate_limiter = RateLimiter()
otp_rate_limiter = OTPRateLimiter()
user_cache = UserCache()


async def authenticate_telegram_user(
    update: Update,
    api_client_instance,
    token_manager=None,
    force_refresh: bool = False
) -> Optional[str]:
    """
    Authenticate telegram user with business API and return token.

    Uses TokenManager for caching to avoid repeated auth calls.

    Args:
        update: Telegram Update object
        api_client_instance: Business API client
        token_manager: Optional TokenManager for token caching

    Returns:
        Access token string or None if authentication failed
    """
    try:
        if not update.effective_user:
            logger.error("No effective_user found in update")
            return None

        user_id = update.effective_user.id

        # Force refresh can be used in sensitive flows (e.g. registration/linking)
        # to avoid reusing stale cached tokens.
        if token_manager and force_refresh:
            await token_manager.invalidate_tokens(user_id)

        # Try to get cached token from TokenManager
        if token_manager and not force_refresh:
            cached_token = await token_manager.get_valid_token(user_id, api_client_instance)
            if cached_token:
                logger.debug(f"Using cached token for user {user_id}")
                return cached_token

        logger.info(f"Authenticating user {user_id} with backend API")

        # Prepare user data from Telegram
        user_data = {
            'username': update.effective_user.username,
            'first_name': update.effective_user.first_name,
            'last_name': update.effective_user.last_name
        }

        # Authenticate with business API
        auth_result = await api_client_instance.authenticate_user(user_id, user_data)

        if auth_result:
            # auth_result is now a dict with access_token, refresh_token, expires_in
            access_token = auth_result.get('access_token')
            refresh_token = auth_result.get('refresh_token')
            expires_in = auth_result.get('expires_in', 3600)

            if access_token:
                # Cache tokens for future requests
                if token_manager and refresh_token:
                    await token_manager.store_tokens(
                        user_id, access_token, refresh_token, expires_in
                    )

                logger.info(f"Authentication successful for user {user_id}")
                return access_token

        # Auth failed — invalidate any stale cached tokens so next attempt
        # triggers a fresh authentication instead of reusing bad tokens
        if token_manager:
            await token_manager.invalidate_tokens(user_id)
        logger.error(f"Authentication failed for user {user_id}")
        return None

    except Exception as e:
        logger.error(f"Error in authenticate_telegram_user: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


async def get_auth_token(
    update: Update,
    context,
    api_client_instance,
    force_refresh: bool = False
) -> Optional[str]:
    """
    Get authentication token with TokenManager integration.

    This is a convenience wrapper around authenticate_telegram_user that
    automatically retrieves the TokenManager from context.bot_data.

    Args:
        update: Telegram Update object
        context: Telegram context with bot_data containing token_manager
        api_client_instance: Business API client
        force_refresh: Skip cached JWT and force backend re-authentication

    Returns:
        Access token string or None if authentication failed
    """
    token_manager = context.bot_data.get('token_manager') if context.bot_data else None
    return await authenticate_telegram_user(
        update,
        api_client_instance,
        token_manager,
        force_refresh=force_refresh
    )


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
            start_prompt = i18n.get('telegram.registration.start_command_prompt', language)

            try:
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        f"{welcome_msg}\n\n{start_prompt}"
                    )
                    await update.callback_query.answer()
                else:
                    await update.message.reply_text(
                        f"{welcome_msg}\n\n{start_prompt}"
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
    """Format datetime for display in the configured display timezone.

    Converts UTC (or any timezone-aware) datetime to the display timezone
    before formatting for user display.
    """
    tashkent_tz = ZoneInfo(DISPLAY_TIMEZONE)
    if dt.tzinfo is None:
        # Assume naive datetimes are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(tashkent_tz)

    if language == 'uz':
        return dt_local.strftime("%d.%m.%Y, %H:%M")
    elif language == 'ru':
        return dt_local.strftime("%d.%m.%Y, %H:%M")
    else:  # English
        return dt_local.strftime("%m/%d/%Y, %I:%M %p")


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text if it's too long"""
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for the bot"""
    if context.error and _is_transient_polling_network_error(update, context.error):
        if _should_log_network_warning():
            logger.warning(
                "Transient polling network disconnect detected (%s). "
                "python-telegram-bot will retry automatically.",
                context.error,
            )
        else:
            logger.debug("Suppressed repeated transient polling disconnect: %s", context.error)
        return

    if context.error and _is_transient_telegram_request_error(context.error):
        if _should_log_network_warning():
            logger.warning(
                "Transient Telegram request failure while handling update (%s).",
                context.error,
            )
        else:
            logger.debug("Suppressed repeated transient Telegram request failure: %s", context.error)
        return

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
    except Exception as e:
        logger.warning(f"Failed to get user info in error handler: {e}")

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



# Note: Admin checks are now handled by the backend API permissions system


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


async def maybe_remove_stale_reply_keyboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    force: bool = False,
) -> bool:
    """
    Best-effort cleanup for lingering reply keyboards from older flows.

    Telegram keeps reply keyboards client-side until explicitly removed.
    This sends a transient cleanup message with ReplyKeyboardRemove and
    deletes it immediately to avoid chat clutter.
    """
    try:
        if not update or not context or not getattr(update, "effective_chat", None):
            return False

        user_data = getattr(context, "user_data", None)
        if user_data is None:
            return False

        now_ts = datetime.now(timezone.utc).timestamp()
        last_cleanup_ts = user_data.get("reply_keyboard_cleanup_at_ts")
        if (
            not force
            and isinstance(last_cleanup_ts, (int, float))
            and now_ts - last_cleanup_ts < _REPLY_KEYBOARD_CLEANUP_COOLDOWN_SECONDS
        ):
            return False

        cleanup_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⁠",
            reply_markup=ReplyKeyboardRemove(),
        )
        user_data["reply_keyboard_cleanup_at_ts"] = now_ts

        try:
            await cleanup_message.delete()
        except Exception as delete_error:
            logger.debug("Reply-keyboard cleanup message deletion skipped: %s", delete_error)

        return True
    except Exception as cleanup_error:
        logger.debug("Reply-keyboard cleanup skipped due to error: %s", cleanup_error)
        return False


def arm_location_request(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record that the BOT asked for a pin, so the next location update is
    routed to the address flow rather than filed as a spontaneous support
    message.

    SSOT for the "did the bot ask for a pin?" question `bot.py`'s address
    conversation entry point has to answer: `filters.LOCATION` matches every
    pin a customer shares, prompted or not, so the entry point cannot rely on
    "a conversation is active" the way a plain `ConversationHandler` normally
    would — the zero-address-checkout keyboard shows the pin prompt WITHOUT
    ever starting the conversation. Call this at every site that sends
    `ProfileKeyboards.location_request(...)`. A missed site means that flow's
    pin silently becomes a support ticket instead of an address.

    Deliberately a DIFFERENT key from `address_flow_origin`: that one means
    "route back to checkout after save" and must not be overloaded with a
    second, unrelated job.

    `awaiting_location_at` stamps when this arming happened. No
    `ConversationHandler` is running at the zero-address-checkout site (the
    keyboard is shown without ever entering the conversation), so there is no
    timeout to fall back on if the customer neither pins nor cancels —
    without a timestamp the flag would survive for the life of the process
    and a much later, unrelated pin would wrongly reopen address creation.
    `_route_address_location_entry` treats an arming older than
    `AWAITING_LOCATION_STALE_MINUTES` as not armed at all, mirroring the
    pattern `handlers/support.py`'s `_is_stale` already uses for the concern
    flow.
    """
    context.user_data['awaiting_location'] = True
    context.user_data['awaiting_location_at'] = datetime.now(timezone.utc).isoformat()


# Mirrors `handlers/support.py::_SUPPORT_STALE_MINUTES` — same 30-minute
# window, same rationale: a marker with no time-based expiry outlives the
# flow that armed it once event-based teardown is skipped (no conversation
# ever started, so no cancel/timeout ever fires).
AWAITING_LOCATION_STALE_MINUTES = 30


def is_awaiting_location_stale(armed_at_raw) -> bool:
    """True when the `awaiting_location_at` stamp is missing/invalid or older
    than `AWAITING_LOCATION_STALE_MINUTES`. Shared so bot.py's routing and any
    test asserting staleness agree on one definition."""
    if not armed_at_raw:
        return True
    try:
        armed_at = datetime.fromisoformat(armed_at_raw)
    except (TypeError, ValueError):
        return True
    if armed_at.tzinfo is None:
        armed_at = armed_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - armed_at) > timedelta(minutes=AWAITING_LOCATION_STALE_MINUTES)


async def validate_phone_number(phone: str) -> bool:
    """Validate phone number format (delegates to shared validator)"""
    from shared.validators import validate_phone_number as _validate
    return _validate(phone)


def normalize_phone_number(phone: str) -> Optional[str]:
    """Normalize phone number to standard format (delegates to shared validator)"""
    from shared.validators import normalize_phone_number as _normalize
    return _normalize(phone)


def chunk_list(lst: list, chunk_size: int) -> list:
    """Split list into chunks of specified size"""
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


async def is_business_hours() -> bool:
    """Check if current time is within business hours (display timezone)"""
    tashkent_tz = ZoneInfo(DISPLAY_TIMEZONE)
    now = datetime.now(tashkent_tz)
    current_hour = now.hour

    business_start = getattr(config.features, 'business_hours_start', 9)
    business_end = getattr(config.features, 'business_hours_end', 21)

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
            from shared.constants import ORDER_STATUS_ICONS, DEFAULT_STATUS_ICON
            icon = ORDER_STATUS_ICONS.get(order['status'], DEFAULT_STATUS_ICON)
            lines.append(f"📊 Status: {icon} {order['status'].replace('_', ' ').title()}")

        return '\n'.join(lines)
