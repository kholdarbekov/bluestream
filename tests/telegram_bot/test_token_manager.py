"""Unit tests for telegram bot TokenManager."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from token_manager import TokenManager


def _jwt_with_exp(exp: int) -> str:
    def _b64(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    return f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64({'exp': exp})}.signature"


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.locks = set()

    async def get(self, key):
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        self.locks.discard(key)
        return 1

    async def set(self, key, value, nx=True, ex=10):
        if nx and key in self.locks:
            return False
        self.locks.add(key)
        return True


@pytest.mark.unit
@pytest.mark.anyio
class TestTokenManager:
    async def test_decode_jwt_expiry_returns_timestamp(self):
        manager = TokenManager("redis://localhost:6379/1")
        exp = int(datetime.now(timezone.utc).timestamp()) + 3600
        token = _jwt_with_exp(exp)

        assert manager._decode_jwt_expiry(token) == exp

    async def test_store_and_get_cached_tokens(self):
        manager = TokenManager("redis://localhost:6379/1")
        fake_redis = _FakeRedis()
        manager.redis = fake_redis
        manager._connected = True

        now = int(datetime.now(timezone.utc).timestamp())
        access_token = _jwt_with_exp(now + 1800)
        refresh_token = _jwt_with_exp(now + 86400)

        stored = await manager.store_tokens(telegram_id=1001, access_token=access_token, refresh_token=refresh_token)
        cached = await manager.get_cached_tokens(1001)

        assert stored is True
        assert cached is not None
        assert cached["access_token"] == access_token
        assert cached["refresh_token"] == refresh_token

    async def test_get_valid_token_returns_cached_when_not_near_expiry(self):
        manager = TokenManager("redis://localhost:6379/1")
        now = int(datetime.now(timezone.utc).timestamp())
        manager.get_cached_tokens = AsyncMock(
            return_value={
                "access_token": "cached-token",
                "refresh_token": "refresh-token",
                "access_expires_at": now + 3600,
                "refresh_expires_at": now + 7200,
            }
        )

        token = await manager.get_valid_token(telegram_id=1010, api_client=AsyncMock())
        assert token == "cached-token"

    async def test_get_valid_token_refreshes_when_needed(self):
        manager = TokenManager("redis://localhost:6379/1")
        manager._connected = True
        manager.redis = _FakeRedis()
        now = int(datetime.now(timezone.utc).timestamp())

        manager.get_cached_tokens = AsyncMock(
            return_value={
                "access_token": "old-access",
                "refresh_token": "good-refresh",
                "access_expires_at": now + 60,  # within refresh buffer
                "refresh_expires_at": now + 3600,
            }
        )
        manager.store_tokens = AsyncMock(return_value=True)
        api_client = AsyncMock()
        api_client.refresh_token = AsyncMock(return_value={"access_token": "new-access", "expires_in": 3600})

        token = await manager.get_valid_token(telegram_id=2020, api_client=api_client)

        assert token == "new-access"
        api_client.refresh_token.assert_awaited_once_with(
            "good-refresh", telegram_id=2020, token_manager=manager
        )
        manager.store_tokens.assert_awaited_once_with(2020, "new-access", "good-refresh", 3600)

    async def test_get_valid_token_invalidates_when_both_tokens_expired(self):
        manager = TokenManager("redis://localhost:6379/1")
        now = int(datetime.now(timezone.utc).timestamp())
        manager.get_cached_tokens = AsyncMock(
            return_value={
                "access_token": "expired-access",
                "refresh_token": "expired-refresh",
                "access_expires_at": now - 10,
                "refresh_expires_at": now - 5,
            }
        )
        manager.invalidate_tokens = AsyncMock(return_value=True)

        token = await manager.get_valid_token(telegram_id=3030, api_client=AsyncMock())

        assert token is None
        manager.invalidate_tokens.assert_awaited_once_with(3030)

    async def test_get_valid_token_handles_refresh_lock_contention(self, monkeypatch):
        manager = TokenManager("redis://localhost:6379/1")
        manager._connected = True
        manager.redis = _FakeRedis()
        now = int(datetime.now(timezone.utc).timestamp())

        first = {
            "access_token": "old-access",
            "refresh_token": "refresh",
            "access_expires_at": now + 5,
            "refresh_expires_at": now + 3600,
        }
        second = {
            "access_token": "already-refreshed",
            "refresh_token": "refresh",
            "access_expires_at": now + 3600,
            "refresh_expires_at": now + 7200,
        }
        manager.get_cached_tokens = AsyncMock(side_effect=[first, second])

        # Simulate another worker holding the refresh lock.
        lock_key = f"bot:refresh_lock:{4040}"
        manager.redis.locks.add(lock_key)

        async def _fast_sleep(_seconds):
            return None

        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

        token = await manager.get_valid_token(telegram_id=4040, api_client=AsyncMock())
        assert token == "already-refreshed"
