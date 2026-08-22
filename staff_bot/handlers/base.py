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

from staff_bot.i18n import i18n
from staff_bot.database import db_manager, StaffUserRepository
from staff_bot.keyboards.common import CommonKeyboards
# Module-level so this module carries the same `api_client` seam every
# handler module does — the guard below reads the backend, and a test that
# swaps a handler module's client has to be able to swap this one too.
from staff_bot.api_client import api_client

logger = logging.getLogger(__name__)


class BaseHandler:
    """Base class for staff bot handler groups with shared error handling."""

    MIN_TOKEN_TTL_SECONDS = 30
    TELEGRAM_RETRY_ATTEMPTS = 2
    TELEGRAM_RETRY_DELAY_SECONDS = 0.5
    API_ERROR_CODE_KEY_MAP = {
        'STAFF_AUTH_REQUIRED': 'staff.error.api.auth_failed',
        'STAFF_TELEGRAM_ID_REQUIRED': 'staff.error.api.validation',
        'STAFF_REQUEST_BODY_REQUIRED': 'staff.error.api.validation',
        'STAFF_REFRESH_TOKEN_REQUIRED': 'staff.error.api.auth_failed',
        'STAFF_STATUS_REQUIRED': 'staff.error.api.validation',
        'STAFF_COORDINATES_REQUIRED': 'staff.error.api.validation',
        'STAFF_CLIENT_ID_REQUIRED': 'staff.error.api.validation',
        'STAFF_USER_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_DELIVERY_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_ORDER_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_CLIENT_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_PRODUCT_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_DELIVERY_PERSON_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_PHONE_EXISTS': 'staff.error.api.conflict',
        'STAFF_EMAIL_EXISTS': 'staff.error.api.conflict',
        'STAFF_DELIVERY_PERSON_EXISTS': 'staff.error.api.conflict',
        'STAFF_EMPLOYEE_ID_EXISTS': 'staff.error.api.conflict',
        'STAFF_DELIVERY_ALREADY_TAKEN': 'staff.error.api.already_taken',
        # The place-scope lock ladder timed out (Postgres 55P03) — an admin is
        # regrouping this address right now. Transient and RETRYABLE, so it gets
        # its own "try again in a moment" copy instead of the generic conflict
        # text, which reads as a permanent refusal.
        'BOTTLE_SCOPE_LOCK_TIMEOUT': 'staff.error.api.scope_busy',
        'STAFF_DRIVER_COD_BLOCKED': 'staff.error.api.driver_cod_blocked',
        'COD_DRIVER_BLOCKED': 'staff.error.api.driver_cod_blocked',
        'COD_DEBT_LIMIT_REACHED': 'staff.error.api.cod_debt_limit_reached',
        'STAFF_INVALID_INVITE_TOKEN': 'staff.error.api.invalid_invite',
        'STAFF_TELEGRAM_NOT_APPROVED': 'staff.error.api.forbidden',
        'STAFF_NO_ROLE': 'staff.error.api.forbidden',
        'STAFF_ACCOUNT_DEACTIVATED': 'staff.error.api.account_deactivated',
        'STAFF_TELEGRAM_ALREADY_LINKED': 'staff.error.api.conflict',
        'STAFF_OPERATOR_ROLE_REQUIRED': 'staff.error.api.forbidden',
        'STAFF_SEARCH_QUERY_TOO_SHORT': 'staff.error.api.invalid_input',
        'STAFF_SEARCH_TYPE_INVALID': 'staff.error.api.invalid_input',
        'STAFF_PHONE_REQUIRED': 'staff.error.api.validation',
        'STAFF_FULL_NAME_PHONE_REQUIRED': 'staff.error.api.validation',
        'STAFF_DELIVERY_PERSON_LINK_MISSING': 'staff.error.api.validation',
        'STAFF_FULL_NAME_EMPTY': 'staff.error.api.validation',
        'STAFF_INVALID_STATUS': 'staff.error.api.validation',
        'STAFF_ROLE_REQUIRED': 'staff.error.api.validation',
        'STAFF_INVITE_REDIS_UNAVAILABLE': 'staff.error.api.service_unavailable',
        'STAFF_INVITE_STORE_UNAVAILABLE': 'staff.error.api.service_unavailable',
        'STAFF_INVITE_PAYLOAD_MALFORMED': 'staff.error.api.invalid_input',
        'STAFF_INVITE_PAYLOAD_USER_ID_REQUIRED': 'staff.error.api.invalid_input',
        'STAFF_INVALID_COORDINATES': 'staff.error.api.invalid_input',
        'STAFF_MAX_CONCURRENT_REACHED': 'staff.error.api.conflict',
        'STAFF_INVALID_STATUS_TRANSITION': 'staff.error.api.invalid_input',
        'STAFF_INVALID_FAIL_REASON': 'staff.error.api.invalid_input',
        'STAFF_ORDER_STATUS_INVALID_FOR_PREPARING': 'staff.error.api.invalid_input',
        'STAFF_PHONE_FIRST_NAME_REQUIRED': 'staff.error.api.validation',
        'STAFF_ORDER_ITEMS_REQUIRED': 'staff.error.api.validation',
        'BOTTLE_SESSION_REQUIRED': 'staff.error.api.bottle_session_required',
        'BOTTLE_SESSION_CAPACITY_EXCEEDED': 'staff.error.api.bottle_session_capacity_exceeded',
    }
    API_ERROR_MESSAGE_KEY_MAP = {
        'telegram_id is required': 'staff.error.api.validation',
        'request body is required': 'staff.error.api.validation',
        'refresh token is required': 'staff.error.api.auth_failed',
        'status field is required': 'staff.error.api.validation',
        'latitude and longitude are required': 'staff.error.api.validation',
        'client_id is required': 'staff.error.api.validation',
        'user not found': 'staff.error.api.not_found',
        'delivery not found': 'staff.error.api.not_found',
        'order not found': 'staff.error.api.not_found',
        'client user not found': 'staff.error.api.not_found',
        'this delivery has already been accepted by another driver': 'staff.error.api.already_taken',
        'a user with this phone number already exists': 'staff.error.api.conflict',
        'phone is already used by another user': 'staff.error.api.conflict',
        'search query must be at least 2 characters': 'staff.error.api.invalid_input',
        "search_type must be 'phone' or 'name'": 'staff.error.api.invalid_input',
        'invite token is invalid, expired, or already used': 'staff.error.api.invalid_invite',
        'this telegram account is not approved for staff bot access': 'staff.error.api.forbidden',
        'user does not have a staff role': 'staff.error.api.forbidden',
        'this telegram account is already linked to another user': 'staff.error.api.conflict',
        'service temporarily unavailable': 'staff.error.api.service_unavailable',
        'request failed after retries': 'staff.error.api.service_unavailable',
        'authentication failed': 'staff.error.api.auth_failed',
        'access denied': 'staff.error.api.forbidden',
        'not found': 'staff.error.api.not_found',
        'conflict': 'staff.error.api.conflict',
    }

    def __init__(self):
        self.user_repo = StaffUserRepository(db_manager)

    async def _get_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get user language from context or database."""
        raw_lang = context.user_data.get('language')
        if raw_lang:
            lang = i18n.normalize_language(raw_lang)
        else:
            lang = await i18n.get_user_language(update.effective_user.id)
        context.user_data['language'] = lang
        return lang

    async def _get_auth_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get auth token from context, using token_manager for refresh if needed."""
        token_manager = context.bot_data.get('token_manager') if context.bot_data else None
        user_id = update.effective_user.id

        if token_manager:
            from staff_bot.api_client import api_client
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
        from staff_bot.api_client import api_client

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
        if user_data.get('last_name') is not None:
            context.user_data['last_name'] = user_data.get('last_name') or ''
        if user_data.get('phone') is not None:
            context.user_data['phone'] = user_data.get('phone') or ''
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

    # ------------------------------------------------------------------
    # The stale-card guard.
    #
    # Every staff card is its own Telegram message; PTB gives the driver ONE
    # `context.user_data`, and `current_delivery` is a single overwritten key.
    # "Act on the delivery whose button was tapped" is therefore a rule of the
    # BOT, not of one handler group — the money handlers needed it first, then
    # Navigate, and the next screen that reads the snapshot will need it too.
    # It lives here so a caller inherits it instead of instantiating a sibling
    # handler to borrow its privates (which is how a second copy starts).
    # ------------------------------------------------------------------

    async def _anchor_current_delivery(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        delivery_id,
    ):
        """Re-anchor ``current_delivery`` on the delivery the driver TAPPED.

        Each active-delivery card is its own Telegram message, but PTB gives a
        driver ONE ``context.user_data`` and ``current_delivery`` is a single
        overwritten key. A driver who opens B and then acts on A's older —
        still perfectly live — card used to drive A's completion with B's
        anchor: B's "All N returned" posted against A's place, and A's screen
        titled with B's order number.

        Every handler that reads the snapshot must therefore compare it against
        the id in the callback and, on a mismatch, re-read the tapped delivery
        from ``/delivery/active`` (the same source ``view_active_delivery``
        builds the card from) rather than trust the stale one.

        Returns the snapshot to act on, or ``None`` when the tapped delivery can
        no longer be found — the caller MUST refuse to act on that.
        A snapshot without a ``delivery_id`` (pre-deploy card, cleared
        user_data) is left exactly as it is: there is nothing to compare, and
        refusing there would strand drivers mid-trip on a deploy.
        """
        info = context.user_data.get('current_delivery') or {}
        current_id = info.get('delivery_id')
        if delivery_id is None or current_id is None or current_id == delivery_id:
            return info

        logger.info(
            "current_delivery snapshot (%s) does not match the tapped delivery "
            "(%s); re-anchoring from /delivery/active",
            current_id, delivery_id,
        )
        token = await self._get_auth_token(update, context)
        if not token:
            return None

        async with api_client as client:
            response = await client.get_active_deliveries(token)
        if not response.success:
            return None

        data = response.data
        rows = data if isinstance(data, list) else (data or {}).get('items', [])
        for row in rows or []:
            if (row.get('delivery_id') or row.get('id')) != delivery_id:
                continue
            # The row IS the card payload `view_active_delivery` whitelists
            # from; copy it wholesale and add only the derived keys that
            # handler renames, so this can never fall behind that whitelist.
            snapshot = dict(row)
            snapshot['delivery_id'] = delivery_id
            snapshot.setdefault('origin_lat', row.get('current_location_lat'))
            snapshot.setdefault('origin_lng', row.get('current_location_lng'))
            snapshot.setdefault('destination_lat', row.get('destination_latitude'))
            snapshot.setdefault('destination_lng', row.get('destination_longitude'))
            context.user_data['current_delivery'] = snapshot
            return snapshot
        return None

    async def _refuse_stale_card(self, update: Update, language: str):
        """The tapped delivery is gone from the driver's active list.

        Acting on the snapshot that happens to be loaded is exactly the bug this
        guard exists to stop, so say so and send them back to the list.
        """
        text = i18n.get('staff.delivery.not_found', language)
        keyboard = CommonKeyboards.back_button(language, "staff_active_deliveries")
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error."""
        error_msg = i18n.get('staff.session_expired', language)
        await self._notify_user(update, error_msg, show_alert=True)

    def _resolve_api_error_message(
        self,
        language: str,
        error: Optional[str] = None,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> str:
        """Resolve backend error into a localized staff bot message."""
        if error_code:
            key = self.API_ERROR_CODE_KEY_MAP.get(str(error_code))
            if key:
                return i18n.get(key, language)

        normalized_error = (error or '').strip().lower()
        if normalized_error:
            key = self.API_ERROR_MESSAGE_KEY_MAP.get(normalized_error)
            if key:
                return i18n.get(key, language)
            if normalized_error.startswith('staff.'):
                return i18n.get(normalized_error, language)

        if status_code == 400:
            return i18n.get('staff.error.api.validation', language)
        if status_code == 401:
            return i18n.get('staff.error.api.auth_failed', language)
        if status_code == 403:
            return i18n.get('staff.error.api.forbidden', language)
        if status_code == 404:
            return i18n.get('staff.error.api.not_found', language)
        if status_code == 409:
            return i18n.get('staff.error.api.conflict', language)
        if status_code == 422:
            return i18n.get('staff.error.api.invalid_input', language)
        if status_code == 429:
            return i18n.get('staff.error.api.rate_limited', language)
        if status_code and status_code >= 500:
            return i18n.get('staff.error.api.service_unavailable', language)

        return i18n.get('staff.error.api.unexpected', language)

    async def _handle_api_error(
        self,
        update: Update,
        error: Optional[str],
        language: str,
        *,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
    ):
        """Handle API error."""
        error_msg = f"❌ {self._resolve_api_error_message(language, error, status_code, error_code)}"
        await self._notify_user(update, error_msg, show_alert=True)

    async def _handle_api_response_error(self, update: Update, response, language: str):
        """Handle API response object error."""
        await self._handle_api_error(
            update,
            getattr(response, 'error', None),
            language,
            status_code=getattr(response, 'status_code', None),
            error_code=getattr(response, 'error_code', None),
        )

    async def _handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Handle general error with language fallback."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('staff.error_occurred', language)
        except Exception:
            # The DB lookup failed, not the user's preference — fall back to the
            # deployment default (uz), not English. `normalize_language(None)`
            # is the SSOT for "no language known".
            error_msg = i18n.get('staff.error_occurred', i18n.normalize_language(None))

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
