"""Admin cash routes pass the refusal's machine-readable code through to the client.

The admin UI branches on `data.error_code` (admin_ui/src/utils/apiError.js::apiErrorCode), so a refusal that
drops the code shows the operator prose only. Every test drives the HTTP route the admin screen calls.
"""

import pytest

from tests.integration._prepayment_payout_helpers import (
    BASE,
    collect_on_trip,
    credit,
    hand_in,
    return_office,
)

pytestmark = pytest.mark.integration

SESSION = BASE + "/sessions/{}"


def test_force_close_of_a_handed_in_trip_names_its_code(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    session_id = collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 100000)
    hand_in(client, app, second_delivery_driver, 100000)
    r = client.post(
        SESSION.format(session_id) + "/force-close",
        json={"reason": "Driver left"},
        headers=admin_claim_headers,
    )
    assert r.status_code == 409
    assert r.get_json()["data"]["error_code"] == "CASH_SESSION_NOT_FORCE_CLOSEABLE"


def test_event_adjust_of_a_collection_a_return_drew_on_names_its_code(
    client, db, admin_claim_headers, sample_user
):
    event_id = credit(client, admin_claim_headers, sample_user.id, 50000)
    return_office(client, admin_claim_headers, sample_user.id, amount=20000)
    r = client.post(
        BASE + f"/events/{event_id}/adjust",
        json={"new_amount": 10000, "reason": "Miscounted"},
        headers=admin_claim_headers,
    )
    assert (r.status_code, r.get_json()["data"]["error_code"]) == (400, "EVENT_FUNDS_PREPAYMENT_RETURN")
