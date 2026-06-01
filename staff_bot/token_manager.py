"""
Token Manager for Staff Bot
Handles JWT token caching, refresh, and lifecycle management using Redis.
Reuses the same pattern as the customer bot's TokenManager.
"""
import asyncio
import json
import logging
import base64
import redis.asyncio as redis
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from shared.redis_failure import report_redis_failure
from shared.redis_keyspace import RedisKeyspace
from shared import business_config

logger = logging.getLogger(__name__)


class TokenManager:
    """
    Manages JWT tokens with Redis caching for the Staff Bot.

    Features:
    - Caches access and refresh tokens per staff user
    - Auto-refreshes tokens when near expiration
    - Falls back to full re-authentication only when necessary
    """

    # Token lifecycle constants — single source of truth in shared.business_config
    # (shared by telegram_bot + staff_bot).
    REFRESH_BUFFER_SECONDS = business_config.TOKEN_REFRESH_BUFFER_SECONDS
    REFRESH_TOKEN_LIFETIME_DAYS = business_config.REFRESH_TOKEN_LIFETIME_DAYS
    DEFAULT_TTL_SECONDS = REFRESH_TOKEN_LIFETIME_DAYS * 24 * 3600
    # Redis TTL for the per-user refresh lock. The previous 10s was too tight
    # for a slow backend round-trip — when refresh exceeded the TTL the lock
    # silently expired and a second concurrent caller could acquire it,
    # producing the very double-refresh the lock exists to prevent. 30s gives
    # the longest sane refresh time enough headroom while still releasing the
    # lock in a reasonable window if the holder crashes mid-flight.
    REFRESH_LOCK_TTL_SECONDS = 30
    # When another caller holds the refresh lock we poll the token cache for
    # the freshly-stored result. 30 polls × 100 ms = 3 s ceiling — well under
    # Telegram's 60 s callback-query timeout, well over the typical refresh
    # round-trip, and short enough to give up and reuse the still-valid old
    # access token instead of leaving the user staring at a spinner. The old
    # exponential 1+2+4 s schedule blocked for up to 7 s and would have made
    # button taps feel unresponsive even when the cache filled in 200 ms.
    REFRESH_WAIT_POLL_SECONDS = 0.1
    REFRESH_WAIT_MAX_POLLS = 30

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
        self._connected = False
        self._disabled = False
        # In-process refresh-coordination lock, keyed by telegram_id. Only used
        # as a fallback when the Redis distributed lock is unavailable (Redis
        # down, transient network blip). It can't prevent cross-process double
        # refresh, but it does prevent the in-process stampede that the
        # previous "lock_acquired = True on Redis exception" path produced.
        # A plain dict + setdefault is intentional: setdefault is atomic under
        # the GIL, so concurrent get_valid_token calls for the same user
        # serialize on a single Lock instance. Locks are cheap (~150 B each)
        # and accumulate at most one per active driver, so we accept the
        # bounded memory growth in exchange for race-free lookup.
        self._local_locks: Dict[int, asyncio.Lock] = {}

    def _get_local_lock(self, telegram_id: int) -> asyncio.Lock:
        return self._local_locks.setdefault(telegram_id, asyncio.Lock())

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

        # Refresh token expired → no path forward but full re-auth.
        if not self.is_refresh_token_valid(tokens):
            await self.invalidate_tokens(telegram_id)
            return None

        # Try the Redis distributed lock first. It's the only thing that
        # serializes refreshes across multiple bot processes; the in-process
        # asyncio.Lock below is a fallback when Redis itself is unreachable.
        lock_key = RedisKeyspace.staff_bot_refresh_lock(telegram_id)
        redis_lock_acquired = False
        redis_lock_failed = False
        if self._connected and self.redis is not None:
            try:
                redis_lock_acquired = bool(
                    await self.redis.set(
                        lock_key, "1", nx=True, ex=self.REFRESH_LOCK_TTL_SECONDS
                    )
                )
            except Exception as e:
                # RED-005: TIER_RELIABILITY — Redis lock unavailable. We'll
                # fall back to the in-process lock below.  Reporting at
                # reliability tier surfaces sustained Redis outages to ops.
                report_redis_failure(
                    "staff_bot.token_manager.get_valid_token.lock",
                    str(e),
                    tier="reliability",
                )
                redis_lock_failed = True
        else:
            # Redis disconnected at startup or via DISABLE_TOKEN_CACHE — the
            # in-process lock is the only coordination available.
            redis_lock_failed = True

        if not redis_lock_acquired:
            if redis_lock_failed:
                # Fall back to the per-user in-process lock. After acquiring
                # it, re-check the cache: another in-process handler may have
                # just refreshed, in which case we use its result instead of
                # making a second backend round-trip.
                async with self._get_local_lock(telegram_id):
                    refreshed = await self.get_cached_tokens(telegram_id)
                    if refreshed and self.is_access_token_valid(refreshed):
                        return refreshed['access_token']
                    return await self._perform_refresh(telegram_id, tokens, api_client)
            # Another caller holds the Redis lock → poll for their result.
            return await self._wait_for_concurrent_refresh(telegram_id, tokens)

        try:
            return await self._perform_refresh(telegram_id, tokens, api_client)
        finally:
            try:
                if self._connected and self.redis is not None:
                    await self.redis.delete(lock_key)
            except Exception:
                # Lock will expire on its own via REFRESH_LOCK_TTL_SECONDS.
                logger.debug("Failed to release Redis refresh lock", exc_info=True)

    async def _wait_for_concurrent_refresh(
        self, telegram_id: int, tokens: Dict[str, Any]
    ) -> Optional[str]:
        """Poll the cache for a freshly-stored token while another handler
        refreshes. Returns the stale-but-valid token as a soft-degradation
        fallback when the poll times out, only forcing a re-auth when the
        existing access token is also expired."""
        for _ in range(self.REFRESH_WAIT_MAX_POLLS):
            await asyncio.sleep(self.REFRESH_WAIT_POLL_SECONDS)
            refreshed = await self.get_cached_tokens(telegram_id)
            if refreshed and self.is_access_token_valid(refreshed):
                return refreshed['access_token']
        if self.is_access_token_valid(tokens):
            return tokens.get('access_token')
        logger.warning(
            f"Refresh lock contention exhausted and token expired for staff user {telegram_id}"
        )
        return None

    async def _perform_refresh(
        self, telegram_id: int, tokens: Dict[str, Any], api_client
    ) -> Optional[str]:
        """Call the backend refresh endpoint and react to the response.

        Distinguishes between three outcomes:
        - **Success**: store the new access token and return it.
        - **Explicit auth failure (401/403)**: the refresh token has been
          revoked / expired server-side.  Wipe the cache and force re-auth.
        - **Transport failure (network blip, 5xx, timeout)**: keep the cached
          tokens. A 5-minute backend hiccup must NOT log every active driver
          out — they'll retry on the next handler tick and either succeed or
          fall back to the still-valid existing access token.
        """
        try:
            response = await api_client.refresh_token(tokens['refresh_token'])
        except Exception as e:
            # Hard exception inside the http client → treat as transport.
            logger.warning(f"Token refresh raised: {e}")
            return tokens.get('access_token') if self.is_access_token_valid(tokens) else None

        if response.success and isinstance(response.data, dict) and 'access_token' in response.data:
            await self.store_tokens(
                telegram_id,
                response.data['access_token'],
                tokens['refresh_token'],
                response.data.get('expires_in', 3600),
            )
            return response.data['access_token']

        if response.status_code in (401, 403):
            logger.info(
                f"Refresh token rejected (status={response.status_code}, "
                f"error_code={response.error_code}) for staff user {telegram_id}; "
                "invalidating cached session"
            )
            await self.invalidate_tokens(telegram_id)
            return None

        # Anything else — 5xx, no status, network error surfaced as a non-success
        # APIResponse — is a transport problem.  Keep the cached session and let
        # the user retry; only fall through to None if even the cached access
        # token has expired (in which case there's nothing usable to return).
        logger.warning(
            f"Token refresh failed transiently (status={response.status_code}, "
            f"error={response.error}) for staff user {telegram_id}; keeping cached tokens"
        )
        if self.is_access_token_valid(tokens):
            return tokens.get('access_token')
        return None
