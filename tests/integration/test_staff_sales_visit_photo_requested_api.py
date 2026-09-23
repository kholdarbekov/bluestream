"""`photo_requested` (D26/D28): the check-in reply tells the staff bot whether to ask for a photo.

Driven through the routes the bot calls -- check-in and resume -- because the bot reads the field
off exactly those two replies, and a field present on one and absent on the other is a prompt a
second device would never show.
"""
import pytest

from tests.integration.test_staff_sales_visits_api import (
    INDIVIDUAL_PAYLOAD,
    OUTLETS,
    VISITS,
    WORKPLACE_PAYLOAD,
    _approved_outlet,
    _start,
)

pytestmark = pytest.mark.integration


def _outlet(client, agent_headers, operator_headers, outlet_type):
    if outlet_type == "individual":
        # An individual outlet activates on creation: it needs no contract.
        created = client.post(OUTLETS, json=INDIVIDUAL_PAYLOAD, headers=agent_headers)
        assert created.status_code == 201, created.get_data(as_text=True)
        return created.get_json()["data"]["outlet"]
    payload = WORKPLACE_PAYLOAD if outlet_type == "workplace" else None
    return _approved_outlet(client, agent_headers, operator_headers, payload)


@pytest.mark.parametrize(
    "outlet_type, expected",
    [("grocery_store", True), ("workplace", True), ("individual", False)],
)
def test_the_check_in_reply_says_whether_to_ask_for_a_photo(
    client, sales_agent_auth_headers, operator_auth_headers, outlet_type, expected
):
    outlet = _outlet(client, sales_agent_auth_headers, operator_auth_headers, outlet_type)
    assert outlet["outlet_type"] == outlet_type
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    checked_in = client.post(f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers)

    assert checked_in.status_code == 200, checked_in.get_data(as_text=True)
    assert checked_in.get_json()["data"]["visit"]["photo_requested"] is expected
    current = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers).get_json()["data"]
    assert current["visit"]["photo_requested"] is expected
