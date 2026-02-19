"""
Base handler class with shared error handling for Staff Bot handlers.
"""
import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from telegram import Update
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from i18n import i18n
from database import db_manager, StaffUserRepository

logger = logging.getLogger(__name__)


class BaseHandler:
    """Base class for staff bot handler groups with shared error handling."""

    MIN_TOKEN_TTL_SECONDS = 30
    TELEGRAM_RETRY_ATTEMPTS = 2
    TELEGRAM_RETRY_DELAY_SECONDS = 0.5

    def __init__(self):
        self.user_repo = StaffUserRepository(db_manager)

    async def _get_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get user language from context or database."""
        lang = context.user_data.get('language')
        if not lang:
            lang = await i18n.get_user_language(update.effective_user.id)
            context.user_data['language'] = lang
        return lang

    async def _get_auth_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get auth token from context, using token_manager for refresh if needed."""
        token_manager = context.bot_data.get('token_manager') if context.bot_data else None
        user_id = update.effective_user.id

        if token_manager:
            from api_client import api_client
            token = await token_manager.get_valid_token(user_id, api_client)
            if token:
                context.user_data['access_token'] = token
                return token

        # Fallback to token persisted in context only if it is still valid.
        fallback_token = context.user_data.get('access_token')
        if self._is_token_usable(fallback_token):
            return fallback_token
        if fallback_token:
            context.user_data.pop('access_token', None)

        # Attempt transparent re-authentication for pre-linked staff users.
        token = await self._authenticate_staff_session(update, context, token_manager)
        if token:
            return token

        if token_manager:
            try:
                await token_manager.invalidate_tokens(user_id)
            except Exception as e:
                logger.warning("Failed to invalidate cached staff tokens for user %s: %s", user_id, e)
        return None

    @staticmethod
    def _decode_jwt_expiry(token: str) -> Optional[int]:
        """Extract JWT exp claim without verifying signature."""
        try:
            parts = token.split('.')
            if len(parts) != 3:
                return None

            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += '=' * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get('exp')
            return int(exp) if exp is not None else None
        except Exception:
            return None

    def _is_token_usable(self, token: Optional[str]) -> bool:
        """Return True only when token has at least MIN_TOKEN_TTL_SECONDS remaining."""
        if not token:
            return False
        exp = self._decode_jwt_expiry(token)
        if exp is None:
            return False

        now = int(datetime.now(timezone.utc).timestamp())
        return (exp - now) > self.MIN_TOKEN_TTL_SECONDS

    @staticmethod
    def _normalize_staff_roles(staff_roles) -> list:
        """Normalize role payload to list[str]."""
        if isinstance(staff_roles, list):
            return staff_roles
        if isinstance(staff_roles, str):
            try:
                decoded = json.loads(staff_roles)
                if isinstance(decoded, list):
                    return decoded
                if isinstance(decoded, str) and decoded:
                    return [decoded]
            except Exception:
                if staff_roles:
                    return [staff_roles]
        return []

    async def _authenticate_staff_session(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        token_manager=None
    ) -> Optional[str]:
        """Re-authenticate staff by telegram_id and update runtime context."""
        from api_client import api_client

        user_id = update.effective_user.id
        try:
            async with api_client as client:
                response = await client.staff_login(user_id)
        except Exception as e:
            logger.warning("Staff re-auth request failed for user %s: %s", user_id, e)
            return None

        if not response.success:
            logger.warning(
                "Staff re-auth failed for user %s: status=%s error=%s",
                user_id, response.status_code, response.error
            )
            if response.status_code in (401, 403, 404):
                context.user_data['authenticated'] = False
            return None

        data = response.data or {}
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')
        user_data = data.get('user') or {}

        if not access_token:
            logger.warning("Staff re-auth succeeded without access_token for user %s", user_id)
            return None

        staff_roles = self._normalize_staff_roles(user_data.get('staff_roles'))

        context.user_data['authenticated'] = True
        context.user_data['access_token'] = access_token
        if user_data.get('id') is not None:
            context.user_data['user_id'] = user_data.get('id')
        if user_data.get('first_name') is not None:
            context.user_data['first_name'] = user_data.get('first_name') or ''
        if user_data.get('delivery_person_id') is not None:
            context.user_data['delivery_person_id'] = user_data.get('delivery_person_id')
        context.user_data['staff_roles'] = staff_roles
        preferred_language = user_data.get('preferred_language')
        if preferred_language:
            context.user_data['language'] = preferred_language

        if token_manager and refresh_token:
            try:
                await token_manager.store_tokens(
                    user_id, access_token, refresh_token, data.get('expires_in', 3600)
                )
            except Exception as e:
                logger.warning("Failed to cache re-auth tokens for user %s: %s", user_id, e)

        return access_token

    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error."""
        error_msg = i18n.get('staff.session_expired', language)
        await self._notify_user(update, error_msg, show_alert=True)

    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Handle API error."""
        error_msg = f"\u274c {error}"
        await self._notify_user(update, error_msg, show_alert=True)

    async def _handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Handle general error with language fallback."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('staff.error_occurred', language)
        except Exception:
            error_msg = "An error occurred. Please try again."

        await self._notify_user(update, error_msg, show_alert=True)

    async def _notify_user(self, update: Update, message: str, show_alert: bool = False):
        """Send user feedback without propagating Telegram network exceptions."""
        callback_query = update.callback_query
        if callback_query:
            if await self._safe_callback_answer(callback_query, message, show_alert=show_alert):
                return

            fallback_message = callback_query.message
            if fallback_message:
                await self._safe_reply_text(fallback_message, message)
            return

        if update.message:
            await self._safe_reply_text(update.message, message)

    async def _safe_callback_answer(self, callback_query, message: str, show_alert: bool) -> bool:
        """Attempt callback query answer with retries on transient network failures."""
        for attempt in range(1, self.TELEGRAM_RETRY_ATTEMPTS + 1):
            try:
                await callback_query.answer(message, show_alert=show_alert)
                return True
            except (TimedOut, NetworkError) as e:
                if attempt < self.TELEGRAM_RETRY_ATTEMPTS:
                    await asyncio.sleep(self.TELEGRAM_RETRY_DELAY_SECONDS * attempt)
                    continue
                logger.warning("Failed to answer callback query after retries: %s", e)
            except TelegramError as e:
                logger.warning("Telegram error while answering callback query: %s", e)
                break
            except Exception as e:
                logger.error("Unexpected error while answering callback query: %s", e, exc_info=True)
                break
        return False

    async def _safe_reply_text(self, target_message, message: str):
        """Attempt reply_text with retries on transient network failures."""
        for attempt in range(1, self.TELEGRAM_RETRY_ATTEMPTS + 1):
            try:
                await target_message.reply_text(message)
                return
            except (TimedOut, NetworkError) as e:
                if attempt < self.TELEGRAM_RETRY_ATTEMPTS:
                    await asyncio.sleep(self.TELEGRAM_RETRY_DELAY_SECONDS * attempt)
                    continue
                logger.warning("Failed to send reply message after retries: %s", e)
            except TelegramError as e:
                logger.warning("Telegram error while sending reply message: %s", e)
                return
            except Exception as e:
                logger.error("Unexpected error while sending reply message: %s", e, exc_info=True)
                return
