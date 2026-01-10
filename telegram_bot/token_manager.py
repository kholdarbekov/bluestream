"""
Token Manager for Telegram Bot
Handles JWT token caching, refresh, and lifecycle management using Redis.
"""
import json
import logging
import base64
import redis.asyncio as redis
from datetime import datetime, timezone
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class TokenManager:
    """
    Manages JWT tokens with Redis caching for the Telegram bot.
    
    Features:
    - Caches access and refresh tokens per telegram user
    - Auto-refreshes tokens when near expiration
    - Falls back to full re-authentication only when necessary
    """
    
    # Token refresh buffer - refresh 5 minutes before expiry
    REFRESH_BUFFER_SECONDS = 300
    
    # Default TTL for cached tokens (30 days to match refresh token lifetime)
    DEFAULT_TTL_SECONDS = 30 * 24 * 3600
    
    def __init__(self, redis_url: str):
        """
        Initialize TokenManager.
        
        Args:
            redis_url: Redis connection URL
        """
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
        self._connected = False
        self._disabled = False
        
    async def connect(self) -> bool:
        """
        Establish Redis connection.
        
        Returns:
            True if connection successful
        """
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
        """Generate Redis key for user's tokens."""
        return f"bot:tokens:{telegram_id}"
    
    def _decode_jwt_expiry(self, token: str) -> Optional[int]:
        """
        Extract expiration timestamp from JWT without full validation.
        
        Args:
            token: JWT token string
            
        Returns:
            Expiration timestamp or None if extraction fails
        """
        try:
            # JWT format: header.payload.signature
            parts = token.split('.')
            if len(parts) != 3:
                return None
            
            # Decode payload (add padding if needed)
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
        """
        Get cached tokens for a user.
        
        Args:
            telegram_id: Telegram user ID
            
        Returns:
            Token data dict or None if not cached/expired
        """
        if not self._connected or self._disabled:
            return None
            
        try:
            key = self._get_cache_key(telegram_id)
            data = await self.redis.get(key)
            
            if not data:
                logger.debug(f"No cached tokens for user {telegram_id}")
                return None
            
            tokens = json.loads(data)
            logger.debug(f"Found cached tokens for user {telegram_id}")
            return tokens
            
        except Exception as e:
            logger.warning(f"Error retrieving cached tokens: {e}")
            return None
    
    async def store_tokens(
        self, 
        telegram_id: int, 
        access_token: str, 
        refresh_token: str,
        expires_in: int = 3600
    ) -> bool:
        """
        Store tokens in Redis cache.
        
        Args:
            telegram_id: Telegram user ID
            access_token: JWT access token
            refresh_token: JWT refresh token
            expires_in: Access token expiry in seconds (from API response)
            
        Returns:
            True if successfully stored
        """
        if not self._connected or self._disabled:
            return False
            
        try:
            now = int(datetime.now(timezone.utc).timestamp())
            
            # Try to get expiry from tokens, fall back to provided expires_in
            access_expires = self._decode_jwt_expiry(access_token)
            refresh_expires = self._decode_jwt_expiry(refresh_token)
            
            if not access_expires:
                access_expires = now + expires_in
            if not refresh_expires:
                refresh_expires = now + (30 * 24 * 3600)  # 30 days default
            
            token_data = {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'access_expires_at': access_expires,
                'refresh_expires_at': refresh_expires,
                'last_updated': datetime.now(timezone.utc).isoformat()
            }
            
            key = self._get_cache_key(telegram_id)
            await self.redis.setex(
                key,
                self.DEFAULT_TTL_SECONDS,
                json.dumps(token_data)
            )
            
            logger.info(f"Cached tokens for user {telegram_id}")
            return True
            
        except Exception as e:
            logger.warning(f"Failed to cache tokens: {e}")
            return False
    
    async def invalidate_tokens(self, telegram_id: int) -> bool:
        """
        Remove cached tokens for a user.
        
        Args:
            telegram_id: Telegram user ID
            
        Returns:
            True if successfully removed
        """
        if not self._connected or self._disabled:
            return False
            
        try:
            key = self._get_cache_key(telegram_id)
            await self.redis.delete(key)
            logger.info(f"Invalidated cached tokens for user {telegram_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to invalidate tokens: {e}")
            return False
    
    def is_access_token_valid(self, tokens: Dict[str, Any]) -> bool:
        """
        Check if access token is still valid.
        
        Args:
            tokens: Token data dict with 'access_expires_at'
            
        Returns:
            True if token is not expired
        """
        now = int(datetime.now(timezone.utc).timestamp())
        expires_at = tokens.get('access_expires_at', 0)
        return now < expires_at
    
    def needs_refresh(self, tokens: Dict[str, Any]) -> bool:
        """
        Check if access token should be refreshed (within buffer of expiry).
        
        Args:
            tokens: Token data dict
            
        Returns:
            True if token should be refreshed
        """
        now = int(datetime.now(timezone.utc).timestamp())
        expires_at = tokens.get('access_expires_at', 0)
        return now >= (expires_at - self.REFRESH_BUFFER_SECONDS)
    
    def is_refresh_token_valid(self, tokens: Dict[str, Any]) -> bool:
        """
        Check if refresh token is still valid.
        
        Args:
            tokens: Token data dict
            
        Returns:
            True if refresh token is not expired
        """
        now = int(datetime.now(timezone.utc).timestamp())
        expires_at = tokens.get('refresh_expires_at', 0)
        return now < expires_at
    
    async def get_valid_token(
        self, 
        telegram_id: int, 
        api_client
    ) -> Optional[str]:
        """
        Get a valid access token, refreshing if necessary.
        
        This is the main method handlers should use.
        
        Args:
            telegram_id: Telegram user ID
            api_client: API client instance for refresh/re-auth
            
        Returns:
            Valid access token or None if authentication needed
        """
        if self._disabled:
            return None
            
        # Try to get cached tokens
        tokens = await self.get_cached_tokens(telegram_id)
        
        if not tokens:
            logger.debug(f"No cached tokens for user {telegram_id}")
            return None
        
        # Check if access token is still valid and not near expiry
        if self.is_access_token_valid(tokens) and not self.needs_refresh(tokens):
            logger.info(f"TokenManager: Using cached token for user {telegram_id}")
            return tokens['access_token']
        
        # Try to refresh if refresh token is valid
        if self.is_refresh_token_valid(tokens):
            logger.info(f"TokenManager: Refreshing token for user {telegram_id}")
            try:
                new_tokens = await api_client.refresh_token(tokens['refresh_token'])
                if new_tokens and 'access_token' in new_tokens:
                    # Store refreshed tokens
                    await self.store_tokens(
                        telegram_id,
                        new_tokens['access_token'],
                        tokens['refresh_token'],  # Keep existing refresh token
                        new_tokens.get('expires_in', 3600)
                    )
                    logger.info(f"TokenManager: Token refreshed for user {telegram_id}")
                    return new_tokens['access_token']
            except Exception as e:
                logger.warning(f"Token refresh failed: {e}")
        
        # Both tokens expired - need full re-authentication
        logger.info(f"TokenManager: Tokens expired for user {telegram_id}, re-auth needed")
        await self.invalidate_tokens(telegram_id)
        return None
