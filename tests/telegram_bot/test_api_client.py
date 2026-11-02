"""Unit tests for telegram bot API client internals."""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
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


@pytest.mark.unit
@pytest.mark.anyio
class TestReportTelegramReachability:
    """Spec 2026-10-09 §7.2 (a, b): the bot tells the backend a customer blocked or reached it,
    signed like telegram-login because no customer JWT travels with it."""

    AT = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)

    async def test_the_report_is_signed_over_the_exact_bytes_sent(self):
        client = BusinessAPIClient()
        client.max_retries = 0
        fake_http = _FakeHTTPClient(_FakeResponse(200, payload={"success": True, "data": {"changed": True}}))
        client._client = fake_http

        assert await client.report_telegram_reachability("700100200", "blocked", self.AT) is True

        assert fake_http.last_url.endswith("/api/v1/bot/telegram-reachability")
        sent = fake_http.last_kwargs["content"]
        assert json.loads(sent) == {
            "telegram_id": "700100200",
            "status": "blocked",
            "at": "2026-10-14T09:30:00+00:00",
            "source": "my_chat_member",
        }
        assert fake_http.last_kwargs.get("json") is None
        expected = hmac.new(bot_config.security.webhook_secret.encode("utf-8"), sent, hashlib.sha256).hexdigest()
        assert fake_http.last_headers["X-Bot-Webhook-Signature"] == expected

    async def test_a_refused_report_answers_false_and_never_raises(self):
        client = BusinessAPIClient()
        client._make_request = AsyncMock(
            return_value=APIResponse(success=False, error="Invalid signature", status_code=401)
        )

        assert await client.report_telegram_reachability("700100200", "reachable", self.AT, source="recovery") is False

        client._make_request.assert_awaited_once_with(
            "POST",
            "/api/v1/bot/telegram-reachability",
            data={
                "telegram_id": "700100200",
                "status": "reachable",
                "at": "2026-10-14T09:30:00+00:00",
                "source": "recovery",
            },
            sign=True,
        )


@pytest.mark.unit
@pytest.mark.anyio
class TestIdentityCallsAreSigned:
    """The four customer-bot calls that travel with no user JWT are signed like telegram-login:
    the HMAC over the exact bytes on the wire is the only proof the bot sent them."""

    CALLS = [
        (
            "register_telegram_user",
            ("700100200", {"first_name": "Kamola", "language_code": "uz"}),
            "/api/v1/auth/telegram-register",
            {"telegram_id": "700100200", "first_name": "Kamola", "language_code": "uz"},
        ),
        (
            "check_phone_availability",
            ("700100200", "+998901234567"),
            "/api/v1/auth/check-phone-availability",
            {"telegram_id": "700100200", "phone": "+998901234567"},
        ),
        (
            "link_phone_send_otp",
            ("700100200", "+998901234567"),
            "/api/v1/auth/link-phone-account/send-otp",
            {"telegram_id": "700100200", "phone": "+998901234567"},
        ),
        (
            "link_phone_verify",
            ("700100200", "123456"),
            "/api/v1/auth/link-phone-account/verify",
            {"telegram_id": "700100200", "otp": "123456"},
        ),
    ]

    @pytest.mark.parametrize("method,args,endpoint,body", CALLS, ids=[c[0] for c in CALLS])
    async def test_the_call_is_signed_over_the_exact_bytes_sent(self, method, args, endpoint, body):
        client = BusinessAPIClient()
        client.max_retries = 0
        fake_http = _FakeHTTPClient(_FakeResponse(200, payload={"success": True, "data": {"ok": True}}))
        client._client = fake_http

        response = await getattr(client, method)(*args)

        assert response.success is True
        assert fake_http.last_url.endswith(endpoint)
        sent = fake_http.last_kwargs.get("content")
        assert sent is not None
        assert fake_http.last_kwargs.get("json") is None
        assert json.loads(sent) == body

        secret = bot_config.security.webhook_secret
        assert secret, "test env must seed BOT_WEBHOOK_SECRET (see tests/conftest.py)"
        expected = hmac.new(secret.encode("utf-8"), sent, hashlib.sha256).hexdigest()
        assert fake_http.last_headers["X-Bot-Webhook-Signature"] == expected


class _RaisingHTTPClient:
    """The httpx transport: every call raises `exc`, or answers `response` once `fail_times` calls are spent."""

    def __init__(self, exc, response=None, fail_times=None):
        self.exc = exc
        self.response = response
        self.fail_times = fail_times
        self.calls = []

    async def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.fail_times is None or len(self.calls) <= self.fail_times:
            raise self.exc
        return self.response

    async def get(self, url, **kwargs):
        return await self._call("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._call("POST", url, **kwargs)


def _retrying_client(http):
    client = BusinessAPIClient()
    client.max_retries = 2
    client.retry_delay = 0
    client._client = http
    return client


@pytest.mark.unit
@pytest.mark.anyio
class TestRetrySafety:
    """RFC 9110 section 9.2.2: a write that may have reached the server is never re-sent by the retrier."""

    async def test_a_post_that_hits_a_read_timeout_is_sent_exactly_once(self):
        http = _RaisingHTTPClient(httpx.ReadTimeout("response lost"))
        client = _retrying_client(http)

        response = await client._make_request("POST", "/api/v1/orders/1/retry-payment", data={})

        assert [c[0] for c in http.calls] == ["POST"]
        assert response.success is False

    async def test_a_post_that_loses_its_connection_mid_exchange_is_sent_exactly_once(self):
        http = _RaisingHTTPClient(httpx.RemoteProtocolError("server closed"))
        client = _retrying_client(http)

        response = await client._make_request("POST", "/api/v1/orders/1/cancel", data={})

        assert len(http.calls) == 1
        assert response.success is False

    @pytest.mark.parametrize("exc", [
        httpx.ConnectTimeout("no handshake"), httpx.ConnectError("refused"),
        httpx.PoolTimeout("no connection free"), httpx.ProxyError("no tunnel"),
    ])
    async def test_a_post_that_never_left_the_bot_is_retried(self, exc):
        ok = _FakeResponse(200, payload={"success": True})
        http = _RaisingHTTPClient(exc, response=ok, fail_times=1)
        client = _retrying_client(http)

        response = await client._make_request("POST", "/api/v1/orders/1/retry-payment", data={})

        assert [c[0] for c in http.calls] == ["POST", "POST"]
        assert response.success is True

    async def test_a_get_that_hits_a_read_timeout_is_still_retried(self):
        ok = _FakeResponse(200, payload={"success": True})
        http = _RaisingHTTPClient(httpx.ReadTimeout("slow"), response=ok, fail_times=1)
        client = _retrying_client(http)

        response = await client._make_request("GET", "/api/v1/orders/1")

        assert [c[0] for c in http.calls] == ["GET", "GET"]
        assert response.success is True

    @pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
    async def test_a_write_verb_that_hits_a_read_timeout_is_sent_exactly_once(self, method):
        """PUT included: the customer bot's saves carry expected_state, so a replay of a committed one is a 409."""
        http = _RaisingHTTPClient(httpx.ReadTimeout("response lost"))
        http.put = lambda url, **kw: http._call("PUT", url, **kw)
        http.patch = lambda url, **kw: http._call("PATCH", url, **kw)
        http.delete = lambda url, **kw: http._call("DELETE", url, **kw)
        client = _retrying_client(http)

        response = await client._make_request(method, "/api/v1/subscriptions/1", data={})

        assert [c[0] for c in http.calls] == [method]
        assert response.success is False

    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout("response lost"), httpx.RemoteProtocolError("server closed"),
        httpx.DecodingError("bad body"),
    ])
    async def test_a_given_up_post_records_exactly_one_breaker_failure(self, exc):
        http = _RaisingHTTPClient(exc)
        client = _retrying_client(http)

        await client._make_request("POST", "/api/v1/orders/1/cancel", data={})

        assert client._circuit_breaker._failure_count == 1
        assert len(http.calls) == 1

    async def test_a_post_that_fails_in_some_other_way_is_not_resent(self):
        http = _RaisingHTTPClient(httpx.TooManyRedirects("loop"))
        client = _retrying_client(http)

        response = await client._make_request("POST", "/api/v1/orders/1/cancel", data={})

        assert len(http.calls) == 1
        assert response.success is False
