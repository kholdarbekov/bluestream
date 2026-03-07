"""Unit tests for telegram bot API client internals."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from api_client import APIResponse, BusinessAPIClient, CircuitBreaker


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

    async def get(self, url, **kwargs):
        self.last_url = url
        self.last_headers = kwargs.get("headers", {})
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
