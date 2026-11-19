"""No test reaches a real server through httpx.

`block_external_side_effects` (tests/conftest.py) refused outbound HTTP made with `requests`, but not with httpx, and
eskiz_sms sends every SMS through httpx. A test that put back the real SMS path without replacing Eskiz's transport
would have reached notify.eskiz.uz and could have sent a real SMS. Real network I/O is now refused at httpx's
transport, below the client, so an `httpx.MockTransport` (how tests stand in for a server) still answers.
"""

import asyncio

import httpx
import pytest

# `.invalid` never resolves (RFC 2606), so even before the guard existed no request could leave the container
UNREACHABLE = "http://outbound-check.invalid/api/auth/login"
BLOCKED = "Outbound HTTP blocked during tests"


def _answer(request):
    return httpx.Response(200, json={"path": request.url.path})


@pytest.mark.unit
def test_a_real_httpx_request_is_refused():
    with httpx.Client() as http:
        with pytest.raises(RuntimeError, match=BLOCKED):
            http.post(UNREACHABLE, data={"email": "nobody@example.com"})


@pytest.mark.unit
def test_a_real_async_httpx_request_is_refused():
    async def _send():
        async with httpx.AsyncClient() as http:
            await http.post(UNREACHABLE, data={"email": "nobody@example.com"})

    with pytest.raises(RuntimeError, match=BLOCKED):
        asyncio.run(_send())


@pytest.mark.unit
def test_a_mock_transport_still_answers():
    with httpx.Client(transport=httpx.MockTransport(_answer)) as http:
        assert http.post(UNREACHABLE).json() == {"path": "/api/auth/login"}


@pytest.mark.unit
def test_an_async_mock_transport_still_answers():
    async def _send():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_answer)) as http:
            return (await http.post(UNREACHABLE)).json()

    assert asyncio.run(_send()) == {"path": "/api/auth/login"}
