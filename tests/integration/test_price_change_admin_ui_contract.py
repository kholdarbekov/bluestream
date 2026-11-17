"""The price changes page (plan 2026-11-16, Task 53) against the real admin routes.

The page's Vitest fixtures declare the key Sets they build rows from; each is diffed here against the real HTTP
response, so a fixture cannot carry a field the backend does not send or miss one it does. The change is APPLIED, with
one `none` notice per kind and one MANAGER courtesy, built with the rows-only builders.
"""

import pathlib
from datetime import timedelta

import pytest

from business_app.models.price_change import CourtesySource
from business_app.services.price_change.schedule import PRICE_CHANGE_STAGES
from business_app.utils.constants import DEFAULT_PAGE_SIZE
from business_app.utils.local_windows import local_date
from tests.integration.price_change_builders import (
    APPLIED_AT,
    applied_change,
    bottles,
    courtesy_entry,
    make_admin,
    queued,
)
from tests.integration.reorder_reminder_builders import make_customer
from tests.unit.test_lapsed_customers_ui_contract import _declared_key_set, _declared_list

pytestmark = pytest.mark.integration

ADMIN_UI = pathlib.Path(__file__).resolve().parents[2] / "admin_ui" / "src"
PAGE = ADMIN_UI / "pages" / "PriceChanges.js"
PAGE_TEST = ADMIN_UI / "__tests__" / "pages" / "PriceChanges.test.js"
FORM_TEST = ADMIN_UI / "__tests__" / "pages" / "PriceChanges.form.test.js"
BASE = "/api/v1/admin/price-changes"
REASON = "Cancelled after the switch"


@pytest.fixture
def world(db):
    admin = make_admin(db)
    b = bottles(db)
    change = applied_change(db, b, created_by=admin)
    customer = make_customer(db, first_name="Dilnoza")
    for kind in ("announcement", "reminder"):
        notice = queued(db, change, customer, kind, APPLIED_AT)
        notice.channel = "none"
    courtesy_entry(
        db, change, customer, source=CourtesySource.MANAGER, reason=REASON, granted_by=admin
    )
    db.session.commit()
    return change, customer


def _data(client, path, headers):
    response = client.get(f"{BASE}{path}", headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def test_the_row_fixture_keys_are_the_list_rows_keys(client, world, admin_auth_headers):
    data = _data(client, "", admin_auth_headers)

    row = data["items"][0]
    assert set(row) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_ROW_KEYS")
    assert set(row["items"][0]) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_ITEM_KEYS")
    assert set(row["allowed_actions"]) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_ACTION_KEYS")
    assert set(data["allowed_actions"]) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_LIST_ACTION_KEYS")
    action_keys = _declared_key_set(PAGE_TEST, "ACTION_KEYS")
    for action in [*row["allowed_actions"].values(), *data["allowed_actions"].values()]:
        assert set(action) == action_keys


def test_the_detail_publishes_the_row_and_the_counts(client, world, admin_auth_headers):
    change, _ = world

    data = _data(client, f"/{change.id}", admin_auth_headers)

    assert set(data) == {"price_change", "counts"}
    assert set(data["price_change"]) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_ROW_KEYS")
    counts = data["counts"]
    assert set(counts) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_COUNT_KEYS")
    notice_keys = _declared_key_set(PAGE_TEST, "NOTICE_COUNT_KEYS")
    assert set(counts["announcement"]) == set(counts["reminder"]) == notice_keys
    assert counts["announcement"]["none"] == 1 and counts["reminder"]["none"] == 1
    assert counts["exceptions"] == 1


def test_the_unreachable_and_exception_rows_match_their_fixtures(client, world, admin_auth_headers):
    change, customer = world

    unreachable = _data(client, f"/{change.id}/unreachable?page=1&per_page={DEFAULT_PAGE_SIZE}", admin_auth_headers)
    exceptions = _data(client, f"/{change.id}/exceptions", admin_auth_headers)

    assert unreachable["meta"]["total"] == 1
    assert set(unreachable["items"][0]) == _declared_key_set(PAGE_TEST, "UNREACHABLE_ROW_KEYS")
    assert unreachable["items"][0]["user_id"] == customer.id
    assert set(exceptions["items"][0]) == _declared_key_set(PAGE_TEST, "EXCEPTION_ROW_KEYS")
    assert exceptions["items"][0]["reason"] == REASON


def test_every_stage_the_page_names_is_published():
    assert _declared_list(PAGE, "STAGES") == list(PRICE_CHANGE_STAGES)


def test_an_operator_reads_every_route_the_page_calls(client, world, operator_auth_headers):
    change, _ = world

    for path in ("", f"/{change.id}", f"/{change.id}/unreachable?page=1&per_page=20", f"/{change.id}/exceptions"):
        response = client.get(f"{BASE}{path}", headers=operator_auth_headers)
        assert response.status_code == 200, (path, response.get_data(as_text=True))


# --- Task 54: the form the page sends -----------------------------------------------------------------------------


def _form_body(product, **overrides):
    """The body `PriceChangeFormModal` sends, built from the key Sets its Vitest declares; dates are local days ahead
    (Task 8's `_body`), never the literal 2027 dates the fixture uses."""
    today = local_date()
    values = {
        "announce_on": (today + timedelta(days=7)).isoformat(),
        "change_on": (today + timedelta(days=35)).isoformat(),
        "reminder_lead_days": 7,
        "courtesy_days": 14,
        "items": [{"product_id": product.id, "new_price": 20000, "courtesy_cap": 2}],
    }
    values.update(overrides)
    assert set(values) == _declared_key_set(FORM_TEST, "PAYLOAD_KEYS")
    assert set(values["items"][0]) == _declared_key_set(FORM_TEST, "PAYLOAD_ITEM_KEYS")
    return values


def test_the_payload_the_form_sends_creates_a_change(client, db, admin_auth_headers, sample_product):
    response = client.post(BASE, json=_form_body(sample_product), headers=admin_auth_headers)

    assert response.status_code == 201, response.get_data(as_text=True)
    data = response.get_json()["data"]
    assert set(data["price_change"]) == _declared_key_set(PAGE_TEST, "PRICE_CHANGE_ROW_KEYS")
    assert (data["price_change"]["reminder_lead_days"], data["price_change"]["courtesy_days"]) == (7, 14)
    assert data["price_change"]["items"][0]["product_id"] == sample_product.id


def test_the_edit_payload_updates_a_scheduled_change(client, db, admin_auth_headers, sample_product):
    created = client.post(BASE, json=_form_body(sample_product), headers=admin_auth_headers)
    change_id = created.get_json()["data"]["price_change"]["id"]
    edited = _form_body(sample_product, items=[{"product_id": sample_product.id, "new_price": 21000, "courtesy_cap": 2}])

    response = client.put(f"{BASE}/{change_id}", json=edited, headers=admin_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    row = response.get_json()["data"]["price_change"]
    assert row["id"] == change_id
    assert row["items"][0]["new_price"] == 21000.0


def test_cancel_takes_an_empty_body(client, db, admin_auth_headers, sample_product):
    created = client.post(BASE, json=_form_body(sample_product), headers=admin_auth_headers)
    change_id = created.get_json()["data"]["price_change"]["id"]

    response = client.post(f"{BASE}/{change_id}/cancel", json={}, headers=admin_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["price_change"]["stage"] == "cancelled"


def test_a_refusal_carries_a_sentence_the_modal_prints(client, db, admin_auth_headers, sample_product):
    sample_product.min_order_quantity = 2
    db.session.commit()
    body = _form_body(sample_product, items=[{"product_id": sample_product.id, "new_price": 20000, "courtesy_cap": 1}])

    response = client.post(BASE, json=body, headers=admin_auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    payload = response.get_json()
    # the three places `extractApiErrorMessages` reads, in its order
    sentences = payload.get("errors") or payload.get("details", {}).get("validation_errors") or [payload.get("message")]
    assert any(sample_product.name in sentence for sentence in sentences), payload


def test_the_form_reads_only_product_fields_the_admin_product_list_publishes(
    client, db, admin_auth_headers, sample_product
):
    response = client.get("/api/v1/admin/products?per_page=100&is_active=true", headers=admin_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    item = next(row for row in response.get_json()["data"]["items"] if row["id"] == sample_product.id)
    assert _declared_key_set(FORM_TEST, "PRODUCT_ROW_KEYS") <= set(item)
