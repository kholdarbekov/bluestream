"""The staff client keeps what the backend said on EVERY refusal.

It used to keep the error body on 409 only, so a 400's numbers (e.g. "4 of 5
bottles") never reached the bot. Real httpx client, MockTransport backend.
"""
import asyncio

import httpx

from staff_bot.api_client import StaffAPIClient, TRANSPORT_AMBIGUOUS_ERROR_CODE

CAPACITY_BODY = {
    "error": "VALIDATION_ERROR",
    "message": "Session 178 only has 4 bottle(s) available; cannot deliver 5.",
    "details": {"available": 4, "required": 5, "shortfall": 1},
    "error_code": "BOTTLE_SESSION_CAPACITY_EXCEEDED",
    "status_code": 400,
}


def _client_answering(status: int, body):
    client = StaffAPIClient()
    client.max_retries = 1
    client.retry_delay = 0
    client._client = httpx.AsyncClient(
        base_url="http://backend",
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
    )
    return client


def _put(client):
    return asyncio.run(client._make_request("PUT", "/api/v1/staff/delivery/1388/status"))


def test_a_400_keeps_details_message_and_error_type():
    response = _put(_client_answering(400, CAPACITY_BODY))
    assert response.success is False
    assert response.status_code == 400
    assert response.error_code == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
    assert response.details == {"available": 4, "required": 5, "shortfall": 1}
    assert response.server_message == CAPACITY_BODY["message"]
    assert response.error_type == "VALIDATION_ERROR"


def test_a_404_and_a_403_keep_them_too():
    for status in (403, 404):
        body = {"error": "NOT_FOUND", "message": "Delivery not found", "error_code": "STAFF_DELIVERY_NOT_FOUND"}
        response = _put(_client_answering(status, body))
        assert response.server_message == "Delivery not found"
        assert response.error_type == "NOT_FOUND"
        assert response.details is None


def test_a_409_still_carries_the_body_on_data():
    body = {"error": "CONFLICT", "message": "taken", "error_code": "X", "details": {"visit_id": 3}}
    response = _put(_client_answering(409, body))
    assert response.data == body
    assert response.details == {"visit_id": 3}


def test_a_401_never_offers_the_backends_sentence():
    response = _put(_client_answering(401, {"message": "The token has expired."}))
    assert response.error_code == "STAFF_AUTH_REQUIRED"
    assert response.server_message is None


def test_a_body_that_is_not_an_object_yields_nothing():
    response = _put(_client_answering(400, ["not", "an", "object"]))
    assert response.details is None
    assert response.server_message is None
    assert response.error_type is None


def test_a_transport_failure_has_no_server_message():
    client = StaffAPIClient()
    client.max_retries = 1
    client.retry_delay = 0

    def _boom(request):
        raise httpx.ReadTimeout("lost", request=request)

    client._client = httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(_boom))
    response = _put(client)
    assert response.error_code == TRANSPORT_AMBIGUOUS_ERROR_CODE
    assert response.server_message is None
