"""The harness reads a refusal's body the way the real client does.

A journey test is only as honest as the `APIResponse` the harness hands the
bot. On a 409 the real client reads `details` out of the response body, which
tests pass as `data=`, so a harness that took `details` only from its own
argument handed the bot `None` where production hands it the dict.
"""
import pytest

from tests.staff_bot.ptb_harness import FakeStaffBackend, staff_backend_failure

pytestmark = pytest.mark.anyio

ENDPOINT = "/api/v1/staff/sales/visits/31/order"


async def _refuse_with(failure):
    backend = FakeStaffBackend()
    backend.route("POST", ENDPOINT, lambda _call: failure)
    return await backend.handle("POST", ENDPOINT)


async def test_a_409_whose_details_live_only_in_data_carries_them():
    body = {
        "error": "CONFLICT",
        "message": "This visit already has an order",
        "error_code": "SALES_VISIT_ORDER_EXISTS",
        "details": {"order": {"id": 413, "order_number": "SA_000413_26"}},
    }
    response = await _refuse_with(staff_backend_failure(
        "This visit already has an order", 409, "SALES_VISIT_ORDER_EXISTS", data=body,
    ))

    assert response.data == body
    assert response.details == body["details"]
    assert response.server_message == "This visit already has an order"
    assert response.error_type == "CONFLICT"


@pytest.mark.parametrize("status_code", [401, None])
async def test_a_401_or_a_transport_failure_offers_no_backend_sentence(status_code):
    response = await _refuse_with(staff_backend_failure(
        "The token has expired.", status_code, details={"x": 1}, error_type="UNAUTHORIZED",
    ))

    assert response.server_message is None
    assert response.details is None
    assert response.error_type is None
