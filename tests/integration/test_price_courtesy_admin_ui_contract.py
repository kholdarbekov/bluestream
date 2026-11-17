"""The customer page's courtesy panel against the real admin routes (plan 2026-11-16, Task 55).

The panel's Vitest fixtures declare the key Sets they build from; each is diffed here with the real HTTP answer. The
change is APPLIED with its window open now; one customer holds an automatic entry, another holds none.
"""

import pathlib
import re

import pytest

from business_app.models.price_change import CourtesySource, PriceCourtesy
from tests.integration.price_change_builders import bottles, courtesy_entry, make_admin, window_open_now
from tests.integration.reorder_reminder_builders import claim_headers, make_customer
from tests.unit.test_lapsed_customers_ui_contract import _declared_key_set

pytestmark = pytest.mark.integration

ADMIN_UI = pathlib.Path(__file__).resolve().parents[2] / "admin_ui" / "src"
PANEL_TEST = ADMIN_UI / "__tests__" / "components" / "priceChange" / "PriceCourtesyPanel.test.jsx"
PANEL = ADMIN_UI / "components" / "priceChange" / "PriceCourtesyPanel.jsx"
STATUS = ADMIN_UI / "components" / "priceChange" / "PriceCourtesyStatus.jsx"
URL = "/api/v1/admin/customers/{}/price-courtesy"
REASON = "Was abroad during the notice"


@pytest.fixture
def world(db):
    admin = make_admin(db)
    change = window_open_now(db, bottles(db), created_by=admin)
    entitled = make_customer(db, first_name="Dilnoza")
    stranger = make_customer(db, first_name="Bekzod")
    courtesy_entry(db, change, entitled)
    db.session.commit()
    return change, entitled, stranger


def _get(client, user, headers):
    response = client.get(URL.format(user.id), headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def _assert_published_shape(data):
    assert set(data) == _declared_key_set(PANEL_TEST, "COURTESY_RESPONSE_KEYS")
    assert set(data["courtesy_status"]) == _declared_key_set(PANEL_TEST, "COURTESY_STATUS_KEYS")
    for cap in data["courtesy_status"]["caps"]:
        assert set(cap) == _declared_key_set(PANEL_TEST, "COURTESY_CAP_KEYS")
    assert set(data["allowed_actions"]) == {"grant"}
    assert set(data["allowed_actions"]["grant"]) == _declared_key_set(PANEL_TEST, "COURTESY_ACTION_KEYS")


def test_the_status_fixture_keys_are_the_published_keys(client, world, manager_claim_headers):
    _, entitled, stranger = world

    available = _get(client, entitled, manager_claim_headers)
    none = _get(client, stranger, manager_claim_headers)

    _assert_published_shape(available)
    _assert_published_shape(none)
    assert available["courtesy_status"]["state"] == "available"
    assert available["courtesy_status"]["caps"], "the fixture's cap Set is only checked against a real cap"
    assert none["courtesy_status"]["state"] == "none"


def test_the_grant_body_the_modal_sends_is_accepted(client, db, world, manager_user, manager_claim_headers):
    change, _, stranger = world

    response = client.post(URL.format(stranger.id), json={"reason": REASON}, headers=manager_claim_headers)

    assert response.status_code == 201, response.get_data(as_text=True)
    data = response.get_json()["data"]
    _assert_published_shape(data)
    assert data["courtesy_status"]["state"] == "available"
    row = PriceCourtesy.query.filter_by(user_id=stranger.id).one()
    assert (row.source, row.granted_by_id, row.reason, row.price_change_id) == (
        CourtesySource.MANAGER,
        manager_user.id,
        REASON,
        change.id,
    )
    assert data["allowed_actions"]["grant"]["allowed"] is False
    assert data["allowed_actions"]["grant"]["block_reason"]


def test_an_operator_reads_the_status_but_holds_no_grant(client, app, world, operator_user):
    _, _, stranger = world
    headers = claim_headers(app, operator_user, "operator")

    data = _get(client, stranger, headers)
    refused = client.post(URL.format(stranger.id), json={"reason": REASON}, headers=headers)

    assert data["allowed_actions"]["grant"] == {"allowed": False, "block_reason": None}
    assert refused.status_code == 403
    assert PriceCourtesy.query.filter_by(user_id=stranger.id).count() == 0


def test_the_panel_reads_only_published_fields():
    status_keys = _declared_key_set(PANEL_TEST, "COURTESY_STATUS_KEYS")
    cap_keys = _declared_key_set(PANEL_TEST, "COURTESY_CAP_KEYS")
    action_keys = _declared_key_set(PANEL_TEST, "COURTESY_ACTION_KEYS")
    response_keys = _declared_key_set(PANEL_TEST, "COURTESY_RESPONSE_KEYS")
    text = STATUS.read_text(encoding="utf-8") + PANEL.read_text(encoding="utf-8")

    read_from_status = set(re.findall(r"\bstatus\??\.(\w+)", text))
    read_from_cap = set(re.findall(r"\bcap\??\.(\w+)", text))
    read_from_grant = set(re.findall(r"\bgrant\??\.(\w+)", text))
    read_from_data = set(re.findall(r"\bdata\??\.(\w+)", text))

    assert read_from_status and read_from_cap, "the scan is blind"
    assert read_from_status <= status_keys, read_from_status - status_keys
    assert read_from_cap <= cap_keys, read_from_cap - cap_keys
    assert read_from_grant <= action_keys, read_from_grant - action_keys
    assert read_from_data <= response_keys, read_from_data - response_keys
