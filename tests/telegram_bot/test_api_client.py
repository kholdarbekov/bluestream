"""Unit tests for telegram bot API client internals."""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from api_client import APIResponse, BusinessAPIClient, CircuitBreaker
from config import config as bot_config


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


class _FakeHTTPClient:
    def __init__(self, response):
        self.response = response
        self.last_url = None
        self.last_headers = None
        self.last_kwargs = None

    async def get(self, url, **kwargs):
        self.last_url = url
        self.last_headers = kwargs.get("headers", {})
        self.last_kwargs = kwargs
        return self.response

    async def post(self, url, **kwargs):
        self.last_url = url
        self.last_headers = kwargs.get("headers", {})
        self.last_kwargs = kwargs
        return self.response


@pytest.mark.unit
@pytest.mark.anyio
class TestCircuitBreaker:
    async def test_circuit_breaker_opens_after_threshold_and_recovers(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
        assert cb.state == CircuitBreaker.CLOSED

        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.allow_request() is False

        cb._last_failure_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        assert cb.state == CircuitBreaker.HALF_OPEN
        assert cb.allow_request() is True

        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED


@pytest.mark.unit
@pytest.mark.anyio
class TestBusinessAPIClient:
    async def test_make_request_success_returns_payload(self):
        client = BusinessAPIClient()
        client.max_retries = 0
        fake_response = _FakeResponse(200, payload={"success": True, "data": {"id": 1}})
        fake_http = _FakeHTTPClient(fake_response)
        client._client = fake_http

        response = await client._make_request("GET", "/api/v1/products")

        assert isinstance(response, APIResponse)
        assert response.success is True
        assert response.status_code == 200
        assert response.data["data"]["id"] == 1
        assert fake_http.last_url.endswith("/api/v1/products")
        assert "X-Request-ID" in fake_http.last_headers

    async def test_make_request_401_invalidates_cached_tokens(self):
        client = BusinessAPIClient()
        client.max_retries = 0
        fake_response = _FakeResponse(401, payload={"message": "Unauthorized"})
        client._client = _FakeHTTPClient(fake_response)
        token_manager = AsyncMock()

        response = await client._make_request(
            "GET",
            "/api/v1/orders/",
            user_token="stale-token",
            token_manager=token_manager,
            telegram_id=9090,
        )

        assert response.success is False
        assert response.status_code == 401
        token_manager.invalidate_tokens.assert_awaited_once_with(9090)

    async def test_make_request_fails_fast_when_circuit_is_open(self):
        client = BusinessAPIClient()
        client._circuit_breaker._state = CircuitBreaker.OPEN
        client._circuit_breaker._last_failure_time = datetime.now(timezone.utc)

        response = await client._make_request("GET", "/api/v1/products")

        assert response.success is False
        assert "circuit breaker open" in (response.error or "").lower()

    async def test_get_notification_preferences_calls_expected_endpoint(self):
        client = BusinessAPIClient()
        client._make_request = AsyncMock(return_value=APIResponse(success=True, data={"ok": True}))

        result = await client.get_notification_preferences("user-token")

        assert result.success is True
        client._make_request.assert_awaited_once_with(
            "GET",
            "/api/v1/notifications/preferences",
            user_token="user-token",
        )

    async def test_update_notification_preferences_calls_expected_endpoint(self):
        client = BusinessAPIClient()
        client._make_request = AsyncMock(return_value=APIResponse(success=True, data={"ok": True}))
        payload = {"delivery_telegram_status_updates_enabled": False}

        result = await client.update_notification_preferences("user-token", payload)

        assert result.success is True
        client._make_request.assert_awaited_once_with(
            "PUT",
            "/api/v1/notifications/preferences",
            user_token="user-token",
            data=payload,
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestAuthenticateUserSignsLoginRequest:
    """Backend now rejects unsigned /api/v1/auth/telegram-login requests (401).

    These tests prove byte-exactness end-to-end: the signature the bot sends
    must be HMAC-SHA256(secret, <the exact bytes placed on the wire>) rather
    than a signature computed over a re-serialization of `data` that could
    drift from what httpx actually sends.
    """

    async def test_authenticate_user_sends_matching_signature(self):
        client = BusinessAPIClient()
        client.max_retries = 0
        fake_response = _FakeResponse(
            200,
            payload={
                "data": {
                    "access_token": "t",
                    "refresh_token": "r",
                    "expires_in": 3600,
                }
            },
        )
        fake_http = _FakeHTTPClient(fake_response)
        client._client = fake_http

        result = await client.authenticate_user(123456789, {"username": "x"})

        assert result is not None
        assert result["access_token"] == "t"

        # Byte-exactness: signed with content=, not json= (json= would let
        # httpx re-serialize and could desync from the signed bytes).
        sent_content = fake_http.last_kwargs.get("content")
        assert sent_content is not None
        assert fake_http.last_kwargs.get("json") is None

        secret = bot_config.security.webhook_secret
        assert secret, "test env must seed BOT_WEBHOOK_SECRET (see tests/conftest.py)"
        expected_signature = hmac.new(
            secret.encode("utf-8"), sent_content, hashlib.sha256
        ).hexdigest()
        assert fake_http.last_headers["X-Bot-Webhook-Signature"] == expected_signature

    async def test_non_login_post_is_unsigned_and_uses_json(self):
        """Regression guard: only the login call site opts into `sign=True`."""
        client = BusinessAPIClient()
        client.max_retries = 0
        fake_response = _FakeResponse(200, payload={"success": True, "data": {"ok": True}})
        fake_http = _FakeHTTPClient(fake_response)
        client._client = fake_http

        response = await client._make_request(
            "POST", "/api/v1/orders", data={"product_id": 1}
        )

        assert response.success is True
        assert fake_http.last_kwargs.get("json") == {"product_id": 1}
        assert fake_http.last_kwargs.get("content") is None
        assert "X-Bot-Webhook-Signature" not in (fake_http.last_headers or {})
