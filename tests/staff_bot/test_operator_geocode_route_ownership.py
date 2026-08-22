"""One layer owns the forward-geocode route: the staff API client.

``ManageAddressHandler`` geocodes the address an operator types so the
delivery-zone SSOT has a coordinate to judge (``receive_address``). It used to
do that by declaring the endpoint path itself and reaching past the client into
``_make_request`` — a second place that knows how to talk to the backend, and
the thing CLAUDE.md forbids: when the route moves, one of the two copies is
left behind and only the operator flow breaks.

So the wrapper lives beside ``reverse_geocode_address`` and mirrors the
customer bot's ``telegram_bot/api_client.geocode_address``, and the handler
knows nothing but the method name. These tests pin BOTH halves, because either
alone can pass while the split is back: the wrapper can exist unused, and the
handler can stop declaring the constant while still inlining the path.
"""

import inspect
import re
from pathlib import Path

import pytest

from staff_bot.api_client import APIResponse, StaffAPIClient

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLER_PATH = REPO_ROOT / "staff_bot" / "handlers" / "operator" / "manage_address.py"
GEOCODE_ROUTE = "/api/v1/addresses/geocode"

pytestmark = [pytest.mark.unit, pytest.mark.anyio]


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.content = b"{}"

    def json(self):
        return self._payload


class _FakeHTTPClient:
    def __init__(self, response=None):
        self.response = response or _FakeResponse(
            200, {"data": {"latitude": 41.31, "longitude": 69.28}}
        )
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def _client_with_fake_http():
    client = StaffAPIClient()
    fake_http = _FakeHTTPClient()
    client._client = fake_http
    return client, fake_http


class TestTheClientPublishesForwardGeocoding:
    async def test_it_posts_the_typed_line_to_the_geocode_route(self):
        client, fake_http = _client_with_fake_http()

        response = await client.geocode_address("tok", "Chilonzor 15")

        assert isinstance(response, APIResponse)
        assert response.success is True
        assert response.data == {"latitude": 41.31, "longitude": 69.28}

        method, url, kwargs = fake_http.calls[0]
        assert (method, url) == ("POST", GEOCODE_ROUTE)
        assert kwargs["json"] == {"address": "Chilonzor 15"}
        assert kwargs["headers"]["Authorization"] == "Bearer tok"

    async def test_without_hints_the_body_carries_the_address_alone(self):
        """The operator journey asserts the body byte-for-byte, and so does the
        backend serializer: sending ``hint_lat: None`` is not the same request."""
        client, fake_http = _client_with_fake_http()

        await client.geocode_address("tok", "Chilonzor 15")

        assert fake_http.calls[0][2]["json"] == {"address": "Chilonzor 15"}

    async def test_hints_are_forwarded_when_the_caller_has_a_pin(self):
        client, fake_http = _client_with_fake_http()

        await client.geocode_address("tok", "Chilonzor 15", hint_lat=41.3, hint_lon=69.2)

        assert fake_http.calls[0][2]["json"] == {
            "address": "Chilonzor 15",
            "hint_lat": 41.3,
            "hint_lon": 69.2,
        }

    def test_it_mirrors_the_customer_bot_wrapper_signature(self):
        """Two bots, one backend route — the same optional hint pair on both."""
        parameters = list(inspect.signature(StaffAPIClient.geocode_address).parameters)

        assert parameters == ["self", "token", "address", "hint_lat", "hint_lon"]


class TestTheHandlerNoLongerKnowsTheRoute:
    def test_it_declares_no_endpoint_constant(self):
        import staff_bot.handlers.operator.manage_address as manage_address

        assert not hasattr(manage_address, "GEOCODE_ENDPOINT")

    def test_no_backend_path_is_spelled_out_in_the_handler(self):
        source = HANDLER_PATH.read_text(encoding="utf-8")

        assert GEOCODE_ROUTE not in source
        assert not re.search(r"['\"]/api/v\d+/", source), (
            "a handler that spells a route is a second place deciding where the backend lives"
        )

    def test_it_reaches_the_backend_only_through_published_wrappers(self):
        source = HANDLER_PATH.read_text(encoding="utf-8")

        assert "_make_request" not in source

    async def test_the_typed_address_is_geocoded_through_the_wrapper(self, monkeypatch):
        """Not just "the constant is gone" — the call really lands on the wrapper."""
        from staff_bot.handlers.operator.manage_address import ManageAddressHandler

        calls = []

        async def fake_geocode_address(token, address, hint_lat=None, hint_lon=None):
            calls.append((token, address, hint_lat, hint_lon))
            return APIResponse(success=True, data={"latitude": 41.31, "longitude": 69.28})

        monkeypatch.setattr(
            "staff_bot.handlers.operator.manage_address.api_client.geocode_address",
            fake_geocode_address,
        )

        latitude, longitude = await ManageAddressHandler()._geocode("tok", "Chilonzor 15")

        assert calls == [("tok", "Chilonzor 15", None, None)]
        assert (latitude, longitude) == (41.31, 69.28)
