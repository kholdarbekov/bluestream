"""The price change report the admin page reads (spec §5.2): counts, unreachable customers and exceptions.

Driven through `/api/v1/admin/price-changes` with the role-claim tokens the page sends. Rows the later tasks write
(notices, courtesies) are inserted inline: the builders module arrives in Task 13.
"""

from datetime import timedelta

import pytest
from flask import current_app

from business_app import db as _db
from business_app.models.price_change import (
    CourtesySource,
    PriceChange,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceChangeStatus,
    PriceCourtesy,
)
from business_app.utils.constants import DEFAULT_PAGE_SIZE
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import get_utc_now
from tests.integration.reorder_reminder_builders import delivered_order, make_customer

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/price-changes"
REASON = "moved house, missed the notice"


def _get(headers, path):
    return current_app.test_client().get(f"{BASE}{path}", headers=headers)


def _applied_change(headers, product):
    today = local_date()
    body = {
        "announce_on": (today + timedelta(days=7)).isoformat(),
        "change_on": (today + timedelta(days=35)).isoformat(),
        "items": [{"product_id": product.id, "new_price": 20000, "courtesy_cap": 2}],
    }
    response = current_app.test_client().post(BASE, json=body, headers=headers)
    assert response.status_code == 201, response.get_data(as_text=True)
    pc_id = response.get_json()["data"]["price_change"]["id"]
    now = get_utc_now()
    price_change = _db.session.get(PriceChange, pc_id)
    price_change.status = PriceChangeStatus.APPLIED
    price_change.announcement_started_at = now
    price_change.applied_at = now
    _db.session.commit()
    return pc_id


def _notice(pc_id, user, channel, kind=PriceChangeNoticeKind.ANNOUNCEMENT):
    now = get_utc_now()
    _db.session.add(
        PriceChangeNotice(
            price_change_id=pc_id,
            user_id=user.id,
            kind=kind,
            channel=channel,
            queued_at=now,
            sent_at=now if channel in ("telegram", "email") else None,
        )
    )


def _courtesy(pc_id, user, source, *, granter=None, reason=None, order=None):
    now = get_utc_now()
    _db.session.add(
        PriceCourtesy(
            price_change_id=pc_id,
            user_id=user.id,
            source=source,
            reason=reason,
            granted_by_id=granter.id if granter else None,
            granted_at=now,
            expires_at=now + timedelta(days=14),
            order_id=order.id if order else None,
            used_at=now if order else None,
        )
    )


@pytest.fixture
def world(db, manager_claim_headers, manager_user, sample_product):
    pc_id = _applied_change(manager_claim_headers, sample_product)
    people = [make_customer(db, first_name=name) for name in ("Aziza", "Bobur", "Cami", "Dilnoza", "Eldor", "Farid")]
    aziza, bobur, cami, dilnoza, eldor, farid = people
    for user, channel in ((cami, "telegram"), (dilnoza, "telegram"), (eldor, "email"), (aziza, "none"), (bobur, "none")):
        _notice(pc_id, user, channel)
    _notice(pc_id, farid, None)
    _notice(pc_id, cami, "telegram", PriceChangeNoticeKind.REMINDER)
    order = delivered_order(db, cami, get_utc_now())
    _courtesy(pc_id, cami, CourtesySource.AUTOMATIC, order=order)
    _courtesy(pc_id, dilnoza, CourtesySource.AUTOMATIC)
    _courtesy(pc_id, eldor, CourtesySource.AUTOMATIC)
    _courtesy(pc_id, bobur, CourtesySource.MANAGER, granter=manager_user, reason=REASON)
    _db.session.commit()
    return pc_id


def test_the_detail_carries_the_counts(world, manager_claim_headers):
    response = _get(manager_claim_headers, f"/{world}")

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["price_change"]["id"] == world
    assert data["counts"] == {
        "announcement": {"telegram": 2, "email": 1, "none": 2, "queued": 1},
        "reminder": {"telegram": 1, "email": 0, "none": 0, "queued": 0},
        "eligible": 3,
        "courtesy_used": 1,
        "exceptions": 1,
    }


def test_unreachable_lists_name_and_phone_a_page_at_a_time(world, manager_claim_headers):
    first = _get(manager_claim_headers, f"/{world}/unreachable?page=1&per_page=1").get_json()["data"]
    second = _get(manager_claim_headers, f"/{world}/unreachable?page=2&per_page=1").get_json()["data"]

    assert [row["name"] for row in first["items"]] == ["Aziza Test"]
    assert first["items"][0]["phone"]
    assert set(first["items"][0]) == {"user_id", "name", "phone"}
    assert first["meta"]["total"] == 2
    assert [row["name"] for row in second["items"]] == ["Bobur Test"]


def test_unreachable_defaults_to_the_standard_page_size(world, manager_claim_headers):
    data = _get(manager_claim_headers, f"/{world}/unreachable").get_json()["data"]

    assert data["meta"]["per_page"] == DEFAULT_PAGE_SIZE
    assert [row["name"] for row in data["items"]] == ["Aziza Test", "Bobur Test"]


def test_exceptions_say_who_when_and_why(world, manager_claim_headers, manager_user):
    response = _get(manager_claim_headers, f"/{world}/exceptions")

    assert response.status_code == 200
    (row,) = response.get_json()["data"]["items"]
    assert row["reason"] == REASON
    assert row["granted_by_id"] == manager_user.id
    assert row["granted_by_name"] == "Malika Menejer"
    assert row["customer_name"] == "Bobur Test"
    assert row["customer_phone"]
    assert row["granted_at"]
    assert (row["used_order_id"], row["used_order_number"], row["used_at"]) == (None, None, None)


def test_a_used_exception_names_the_order(db, world, manager_claim_headers):
    courtesy = PriceCourtesy.query.filter_by(price_change_id=world, source=CourtesySource.MANAGER).one()
    order = delivered_order(db, courtesy.user, get_utc_now())
    courtesy.order_id = order.id
    courtesy.used_at = get_utc_now()
    db.session.commit()

    (row,) = _get(manager_claim_headers, f"/{world}/exceptions").get_json()["data"]["items"]

    assert (row["used_order_id"], row["used_order_number"]) == (order.id, order.order_number)
    assert row["used_at"]
    assert _get(manager_claim_headers, f"/{world}").get_json()["data"]["counts"]["courtesy_used"] == 2


def test_a_fresh_change_reports_zeros(db, manager_claim_headers, sample_product):
    pc_id = _applied_change(manager_claim_headers, sample_product)
    nothing = {"telegram": 0, "email": 0, "none": 0, "queued": 0}

    detail = _get(manager_claim_headers, f"/{pc_id}").get_json()["data"]
    unreachable = _get(manager_claim_headers, f"/{pc_id}/unreachable").get_json()["data"]
    exceptions = _get(manager_claim_headers, f"/{pc_id}/exceptions").get_json()["data"]

    assert detail["counts"] == {
        "announcement": nothing,
        "reminder": nothing,
        "eligible": 0,
        "courtesy_used": 0,
        "exceptions": 0,
    }
    assert unreachable["items"] == [] and unreachable["meta"]["total"] == 0
    assert exceptions == {"items": []}


def test_operators_read_the_report_drivers_do_not(world, operator_auth_headers, driver_auth_headers):
    for path in (f"/{world}", f"/{world}/unreachable", f"/{world}/exceptions"):
        assert _get(operator_auth_headers, path).status_code == 200
        assert _get(driver_auth_headers, path).status_code == 403


@pytest.mark.parametrize("path", ["/999999", "/999999/unreachable", "/999999/exceptions"])
def test_an_unknown_change_is_404(db, manager_claim_headers, path):
    response = _get(manager_claim_headers, path)

    assert response.status_code == 404
    assert response.get_json()["error_code"] == "PRICE_CHANGE_NOT_FOUND"
