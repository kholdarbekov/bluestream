"""Unit tests: staff bot signs its login request byte-exactly.

The backend now rejects unsigned `/api/v1/staff/auth/login` requests with a
401 (`X-Bot-Webhook-Signature: HMAC-SHA256(secret, raw_request_body)` is
required). These tests prove byte-exactness end-to-end: the signature the
bot sends must be computed over the *exact bytes placed on the wire* (sent
via `content=`), not a re-serialization of `data` that could desync from
what httpx actually sends (which is what `json=` would risk).
"""

import hashlib
import hmac

import pytest

from staff_bot.api_client import APIResponse, StaffAPIClient
from staff_bot.config import config as staff_config


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        # Truthy so `_make_request` calls `.json()` on it, matching real httpx.Response.
        self.content = b"{}"

    def json(self):
        return self._payload


class _FakeHTTPClient:
    def __init__(self, response):
        self.response = response
        self.last_method = None
        self.last_url = None
        self.last_kwargs = None

    async def request(self, method, url, **kwargs):
        self.last_method = method
        self.last_url = url
        self.last_kwargs = kwargs
        return self.response


@pytest.mark.unit
@pytest.mark.anyio
class TestStaffLoginSignsRequest:
    async def test_staff_login_sends_matching_signature(self):
        client = StaffAPIClient()
        fake_response = _FakeResponse(200, payload={"data": {"access_token": "t"}})
        fake_http = _FakeHTTPClient(fake_response)
        client._client = fake_http

        response = await client.staff_login(987654321)

        assert isinstance(response, APIResponse)
        assert response.success is True

        # Byte-exactness: signed with content=, not json= (json= would let
        # httpx re-serialize and could desync from the signed bytes).
        sent_content = fake_http.last_kwargs.get("content")
        assert sent_content is not None
        assert fake_http.last_kwargs.get("json") is None

        secret = staff_config.security.webhook_secret
        assert secret, "test env must seed WEBHOOK_SECRET (see tests/conftest.py)"
        expected_signature = hmac.new(
            secret.encode("utf-8"), sent_content, hashlib.sha256
        ).hexdigest()
        assert fake_http.last_kwargs["headers"]["X-Bot-Webhook-Signature"] == expected_signature

    async def test_non_login_post_is_unsigned_and_uses_json(self):
        """Regression guard: only the staff-login call site opts into `sign=True`."""
        client = StaffAPIClient()
        fake_response = _FakeResponse(200, payload={"data": {"ok": True}})
        fake_http = _FakeHTTPClient(fake_response)
        client._client = fake_http

        response = await client._make_request(
            "POST", "/api/v1/staff/delivery/pool/accept", data={"delivery_id": 1}
        )

        assert response.success is True
        assert fake_http.last_kwargs.get("json") == {"delivery_id": 1}
        assert fake_http.last_kwargs.get("content") is None
        assert "X-Bot-Webhook-Signature" not in fake_http.last_kwargs["headers"]
