"""
Token Manager for Staff Bot
Handles JWT token caching, refresh, and lifecycle management using Redis.
Reuses the same pattern as the customer bot's TokenManager.
"""
import json
import logging
import base64
import redis.asyncio as redis
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)


class TokenManager:
    """
    Manages JWT tokens with Redis caching for the Staff Bot.

    Features:
    - Caches access and refresh tokens per staff user
    - Auto-refreshes tokens when near expiration
    - Falls back to full re-authentication only when necessary
    """

    REFRESH_BUFFER_SECONDS = 300
    REFRESH_TOKEN_LIFETIME_DAYS = 30
    DEFAULT_TTL_SECONDS = REFRESH_TOKEN_LIFETIME_DAYS * 24 * 3600

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
        self._connected = False
        self._disabled = False

    async def connect(self) -> bool:
        """Establish Redis connection."""
        import os
        if os.environ.get('DISABLE_TOKEN_CACHE', '').lower() == 'true':
            logger.warning("Token caching disabled via environment variable")
            self._disabled = True
            return False

        try:
            self.redis = redis.from_url(
                self.redis_url,
                encoding='utf-8',
                decode_responses=True
            )
            await self.redis.ping()
            self._connected = True
            logger.info("TokenManager connected to Redis successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self._connected = False
            return False

    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            self._connected = False
            logger.info("TokenManager disconnected from Redis")

    def _get_cache_key(self, telegram_id: int) -> str:
        """Generate Redis key for staff user's tokens."""
        return RedisKeyspace.staff_bot_token_cache(telegram_id)

    def _decode_jwt_expiry(self, token: str) -> Optional[int]:
        """Extract expiration timestamp from JWT without full validation."""
        try:
            parts = token.split('.')
            if len(parts) != 3:
                return None
            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += '=' * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload.get('exp')
        except Exception as e:
            logger.debug(f"Could not decode JWT expiry: {e}")
            return None

    async def get_cached_tokens(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        """Get cached tokens for a staff user."""
        if not self._connected or self._disabled:
            return None
        try:
            key = self._get_cache_key(telegram_id)
            data = await self.redis.get(key)
            if not data:
                return None
            return json.loads(data)
        except Exception as e:
            # RED-005: TIER_CACHE — returning None forces re-auth against backend.
            report_redis_failure("staff_bot.token_manager.get_cached_tokens", str(e), tier="cache")
            return None

    async def store_tokens(
        self, telegram_id: int,
        access_token: str, refresh_token: str,
        expires_in: int = 3600
    ) -> bool:
        """Store tokens in Redis cache."""
        if not self._connected or self._disabled:
            return False
        try:
            now = int(datetime.now(timezone.utc).timestamp())
            access_expires = self._decode_jwt_expiry(access_token) or (now + expires_in)
            refresh_expires = self._decode_jwt_expiry(refresh_token) or (now + 30 * 24 * 3600)

            token_data = {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'access_expires_at': access_expires,
                'refresh_expires_at': refresh_expires,
                'last_updated': datetime.now(timezone.utc).isoformat()
            }

            key = self._get_cache_key(telegram_id)
            await self.redis.setex(key, self.DEFAULT_TTL_SECONDS, json.dumps(token_data))
            logger.info(f"Cached tokens for staff user {telegram_id}")
            return True
        except Exception as e:
            # RED-005: TIER_CACHE — caller has in-memory tokens, just loses cache reuse.
            report_redis_failure("staff_bot.token_manager.store_tokens", str(e), tier="cache")
            return False

    async def invalidate_tokens(self, telegram_id: int) -> bool:
        """Remove cached tokens for a staff user."""
        if not self._connected or self._disabled:
            return False
        try:
            key = self._get_cache_key(telegram_id)
            await self.redis.delete(key)
            logger.info(f"Invalidated cached tokens for staff user {telegram_id}")
            return True
        except Exception as e:
            # RED-005: TIER_RELIABILITY — failure to invalidate a revoked token
            # keeps it usable until natural TTL expiry. Alert ops.
            report_redis_failure("staff_bot.token_manager.invalidate_tokens", str(e), tier="reliability")
            return False

    def is_access_token_valid(self, tokens: Dict[str, Any]) -> bool:
        """Check if access token is still valid."""
        now = int(datetime.now(timezone.utc).timestamp())
        return now < tokens.get('access_expires_at', 0)

    def needs_refresh(self, tokens: Dict[str, Any]) -> bool:
        """Check if access token should be refreshed."""
        now = int(datetime.now(timezone.utc).timestamp())
        return now >= (tokens.get('access_expires_at', 0) - self.REFRESH_BUFFER_SECONDS)

    def is_refresh_token_valid(self, tokens: Dict[str, Any]) -> bool:
        """Check if refresh token is still valid."""
        now = int(datetime.now(timezone.utc).timestamp())
        return now < tokens.get('refresh_expires_at', 0)

    async def get_valid_token(self, telegram_id: int, api_client) -> Optional[str]:
        """
        Get a valid access token, refreshing if necessary.
        Main method handlers should use.
        """
        if self._disabled:
            return None

        tokens = await self.get_cached_tokens(telegram_id)
        if not tokens:
            return None

        if self.is_access_token_valid(tokens) and not self.needs_refresh(tokens):
            return tokens['access_token']

        if self.is_refresh_token_valid(tokens):
            lock_key = RedisKeyspace.staff_bot_refresh_lock(telegram_id)
            try:
                lock_acquired = await self.redis.set(lock_key, "1", nx=True, ex=10)
            except Exception as e:
                # RED-005: TIER_RELIABILITY — without the lock, concurrent handlers
                # may double-refresh. Caller continues (graceful degradation).
                report_redis_failure(
                    "staff_bot.token_manager.get_valid_token.lock", str(e), tier="reliability"
                )
                # Continue without lock as a last resort.
                lock_acquired = True

            if not lock_acquired:
                import asyncio
                for attempt in range(3):
                    await asyncio.sleep(2 ** attempt)
                    refreshed = await self.get_cached_tokens(telegram_id)
                    if refreshed and self.is_access_token_valid(refreshed):
                        return refreshed['access_token']
                # Return existing token only if still valid.
                if self.is_access_token_valid(tokens):
                    return tokens.get('access_token')
                logger.warning(
                    f"Refresh lock contention exhausted and token expired for staff user {telegram_id}"
                )
                return None

            try:
                new_tokens = await api_client.refresh_token(tokens['refresh_token'])
                if new_tokens and 'access_token' in new_tokens:
                    await self.store_tokens(
                        telegram_id,
                        new_tokens['access_token'],
                        tokens['refresh_token'],
                        new_tokens.get('expires_in', 3600)
                    )
                    return new_tokens['access_token']
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}")
            finally:
                try:
                    await self.redis.delete(lock_key)
                except Exception:
                    pass

        await self.invalidate_tokens(telegram_id)
        return None
