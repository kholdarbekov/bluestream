"""Schedule, edit and cancel a price change through the admin routes the Price changes page calls (spec §5.2).

Driven through `/api/v1/admin/price-changes` with the body the admin modal sends. `validate_admin_action` checks the
DB row, so `admin_auth_headers` / `operator_auth_headers` work; the cancel route reads the manager and admin by
role claim fixtures. Price-change rows are written inline (`_stamp`, `_seed`): the builders module arrives in Task 13.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from flask import current_app

from business_app import db as _db
from business_app.models.price_change import (
    PriceChange,
    PriceChangeItem,
    PriceChangeNotice,
    PriceChangeNoticeKind,
    PriceChangeStatus,
)
from business_app.services.auth_service import AuthService
from business_app.services.price_change.schedule import MAX_COURTESY_DAYS, REFUSAL_MESSAGES, price_change_stage
from business_app.utils.local_windows import local_date, local_day_bounds
from business_app.utils.timezone_utils import get_utc_now
from tests.integration.reorder_reminder_builders import make_customer

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/price-changes"
PROFILE = "/api/v1/auth/profile"
T = {"allowed": True, "block_reason": None}
HIDDEN = {"allowed": False, "block_reason": None}


def _client():
    return current_app.test_client()


def _body(product, **overrides):
    today = local_date()
    body = {
        "announce_on": (today + timedelta(days=7)).isoformat(),
        "change_on": (today + timedelta(days=35)).isoformat(),
        "items": [{"product_id": product.id, "new_price": 20000, "courtesy_cap": 2}],
    }
    body.update(overrides)
    return body


def _post(headers, body):
    return _client().post(BASE, json=body, headers=headers)


def _stamp(price_change_id, **columns):
    """Write lifecycle columns the way Tasks 19 and 44 will."""
    price_change = _db.session.get(PriceChange, price_change_id)
    for name, value in columns.items():
        setattr(price_change, name, value)
    _db.session.commit()


def _scheduled(headers, product, **overrides):
    response = _post(headers, _body(product, **overrides))
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()["data"]["price_change"]["id"]


def _announced(headers, product):
    pc_id = _scheduled(headers, product)
    _stamp(pc_id, status=PriceChangeStatus.ANNOUNCED, announcement_started_at=get_utc_now())
    return pc_id


def _applied(headers, product):
    pc_id = _scheduled(headers, product)
    now = get_utc_now()
    _stamp(pc_id, status=PriceChangeStatus.APPLIED, announcement_started_at=now, applied_at=now)
    return pc_id


def _error(response):
    return response.status_code, response.get_json().get("error_code")


def test_a_manager_schedules_a_price_change(db, manager_claim_headers, manager_user, sample_product):
    today = local_date()

    response = _post(manager_claim_headers, _body(sample_product))

    assert response.status_code == 201, response.get_data(as_text=True)
    row = response.get_json()["data"]["price_change"]
    assert row["status"] == row["stage"] == "scheduled"
    assert (row["reminder_lead_days"], row["courtesy_days"]) == (7, 14)
    assert row["reminder_on"] == (today + timedelta(days=28)).isoformat()
    assert row["courtesy_last_day"] == (today + timedelta(days=48)).isoformat()
    assert row["price_valid_until"] == (today + timedelta(days=34)).isoformat()
    from datetime import datetime

    assert datetime.fromisoformat(row["effective_at"]) == local_day_bounds(today + timedelta(days=35))[0]
    assert row["created_by_id"] == manager_user.id
    assert row["created_by_name"] == manager_user.full_name
    assert row["allowed_actions"] == {"edit": T, "cancel": T}
    assert row["items"] == [
        {
            "product_id": sample_product.id,
            "product_name": "Pure Water 19L",
            "current_price": 15000.0,
            "new_price": 20000.0,
            "old_price": None,
            "courtesy_cap": 2,
            "min_order_quantity": 1,
        }
    ]


def _discount(body, product):
    product.discount_price = Decimal("14000.00")


def _min_order(body, product):
    product.min_order_quantity = 3


def _set(**overrides):
    return lambda body, product: body.update(overrides)


def _item(**fields):
    def mutate(body, product):
        body["items"][0].update(fields)

    return mutate


def _twice(body, product):
    body["items"] = body["items"] * 2


def _past(body, product):
    body["announce_on"] = (local_date() - timedelta(days=1)).isoformat()


def _same_day(body, product):
    body["change_on"] = body["announce_on"]


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (_set(items=[]), (400, "PRICE_CHANGE_ITEMS_REQUIRED")),
        (_twice, (400, "PRICE_CHANGE_DUPLICATE_PRODUCT")),
        (_item(product_id=999999), (404, "PRICE_CHANGE_PRODUCT_NOT_FOUND")),
        (_past, (400, "PRICE_CHANGE_ANNOUNCE_IN_PAST")),
        (_same_day, (400, "PRICE_CHANGE_DATES_INVALID")),
        (_set(reminder_lead_days=35), (400, "PRICE_CHANGE_REMINDER_INVALID")),
        (_set(reminder_lead_days=0), (400, "PRICE_CHANGE_REMINDER_INVALID")),
        (_set(courtesy_days=0), (400, "PRICE_CHANGE_COURTESY_DAYS_INVALID")),
        (_set(courtesy_days=MAX_COURTESY_DAYS + 1), (400, "PRICE_CHANGE_COURTESY_DAYS_INVALID")),
        (_set(courtesy_days=10**12), (400, "PRICE_CHANGE_COURTESY_DAYS_INVALID")),
        (_set(reminder_lead_days=10**12), (400, "PRICE_CHANGE_REMINDER_INVALID")),
        (_item(new_price=0), (400, "PRICE_CHANGE_NEW_PRICE_INVALID")),
        (_item(new_price=15000), (400, "PRICE_CHANGE_NOT_AN_INCREASE")),
        (_item(new_price=14000), (400, "PRICE_CHANGE_NOT_AN_INCREASE")),
        (_item(courtesy_cap=2), (400, "PRICE_CHANGE_CAP_BELOW_MIN_ORDER")),
        (_discount, (400, "PRICE_CHANGE_PRODUCT_HAS_DISCOUNT_PRICE")),
    ],
    ids=[
        "items-empty", "duplicate", "unknown-product", "announce-past", "dates", "reminder-too-long",
        "reminder-zero", "courtesy-days", "courtesy-over-max", "courtesy-huge", "reminder-huge", "price-zero", "equal-price", "lower-price", "cap-below-min", "discount",
    ],
)
def test_each_rule_is_refused_with_its_code(db, manager_claim_headers, sample_product, mutate, expected):
    body = _body(sample_product)
    if expected[1] == "PRICE_CHANGE_CAP_BELOW_MIN_ORDER":
        _min_order(body, sample_product)
        db.session.commit()
    mutate(body, sample_product)
    db.session.commit()

    response = _post(manager_claim_headers, body)

    assert _error(response) == expected
    assert PriceChange.query.count() == 0
    if expected[1] in {
        "PRICE_CHANGE_NEW_PRICE_INVALID",
        "PRICE_CHANGE_NOT_AN_INCREASE",
        "PRICE_CHANGE_CAP_BELOW_MIN_ORDER",
        "PRICE_CHANGE_PRODUCT_HAS_DISCOUNT_PRICE",
        "PRICE_CHANGE_DUPLICATE_PRODUCT",
    }:
        assert sample_product.name in response.get_json()["message"]
        assert response.get_json()["details"]["product_id"] == sample_product.id


def test_an_edit_cannot_turn_a_change_into_a_decrease(db, manager_claim_headers, sample_product):
    pc_id = _scheduled(manager_claim_headers, sample_product)
    body = _body(sample_product)
    body["items"][0]["new_price"] = 15000

    response = _client().put(f"{BASE}/{pc_id}", json=body, headers=manager_claim_headers)

    assert _error(response) == (400, "PRICE_CHANGE_NOT_AN_INCREASE")
    assert [i.new_price for i in PriceChangeItem.query.filter_by(price_change_id=pc_id)] == [Decimal("20000.00")]


def test_one_pending_change_at_a_time(db, manager_claim_headers, sample_product):
    first = _scheduled(manager_claim_headers, sample_product)

    assert _error(_post(manager_claim_headers, _body(sample_product))) == (400, "PRICE_CHANGE_ALREADY_PENDING")
    listing = _client().get(BASE, headers=manager_claim_headers).get_json()["data"]
    assert listing["allowed_actions"]["create"] == {
        "allowed": False,
        "block_reason": REFUSAL_MESSAGES["PRICE_CHANGE_ALREADY_PENDING"],
    }

    cancelled = _client().post(f"{BASE}/{first}/cancel", headers=manager_claim_headers)
    assert cancelled.status_code == 200
    assert _post(manager_claim_headers, _body(sample_product)).status_code == 201


def test_operators_read_but_do_not_write(
    db, manager_claim_headers, operator_auth_headers, driver_auth_headers, sample_product
):
    pc_id = _scheduled(manager_claim_headers, sample_product)
    nothing = {"edit": HIDDEN, "cancel": HIDDEN}

    listing = _client().get(BASE, headers=operator_auth_headers)
    detail = _client().get(f"{BASE}/{pc_id}", headers=operator_auth_headers)

    assert (listing.status_code, detail.status_code) == (200, 200)
    data = listing.get_json()["data"]
    assert data["allowed_actions"] == {"create": HIDDEN}
    assert [row["allowed_actions"] for row in data["items"]] == [nothing]
    assert detail.get_json()["data"]["price_change"]["allowed_actions"] == nothing
    body = _body(sample_product)
    assert _post(operator_auth_headers, body).status_code == 403
    assert _client().put(f"{BASE}/{pc_id}", json=body, headers=operator_auth_headers).status_code == 403
    assert _client().post(f"{BASE}/{pc_id}/cancel", headers=operator_auth_headers).status_code == 403
    assert _client().get(BASE, headers=driver_auth_headers).status_code == 403


def test_a_manager_edits_before_the_announcement(db, manager_claim_headers, sample_product):
    pc_id = _scheduled(manager_claim_headers, sample_product)
    body = _body(sample_product, courtesy_days=10)
    body["items"][0].update(new_price=21000, courtesy_cap=3)

    response = _client().put(f"{BASE}/{pc_id}", json=body, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    row = response.get_json()["data"]["price_change"]
    assert row["courtesy_days"] == 10
    assert [(i["new_price"], i["courtesy_cap"]) for i in row["items"]] == [(21000.0, 3)]
    assert PriceChangeItem.query.filter_by(price_change_id=pc_id).count() == 1


def test_the_change_is_locked_once_the_announcement_starts(db, manager_claim_headers, sample_product):
    pc_id = _announced(manager_claim_headers, sample_product)
    body = _body(sample_product, courtesy_days=3)

    locked = _client().put(f"{BASE}/{pc_id}", json=body, headers=manager_claim_headers)
    _stamp(pc_id, announce_on=local_date() - timedelta(days=2))
    past = _client().put(f"{BASE}/{pc_id}", json=_body(sample_product), headers=manager_claim_headers)

    assert _error(locked) == (409, "PRICE_CHANGE_LOCKED")
    assert _error(past) == (409, "PRICE_CHANGE_LOCKED")
    assert _db.session.get(PriceChange, pc_id).courtesy_days == 14


def test_a_manager_cancels_before_the_announcement(db, manager_claim_headers, manager_user, sample_product):
    pc_id = _scheduled(manager_claim_headers, sample_product)

    response = _client().post(f"{BASE}/{pc_id}/cancel", json={}, headers=manager_claim_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    row = response.get_json()["data"]["price_change"]
    assert (row["stage"], row["cancelled_by_id"]) == ("cancelled", manager_user.id)


def test_only_an_admin_cancels_an_announced_change(
    db, manager_claim_headers, admin_claim_headers, admin_user, sample_product
):
    pc_id = _announced(manager_claim_headers, sample_product)

    refused = _client().post(f"{BASE}/{pc_id}/cancel", headers=manager_claim_headers)
    allowed = _client().post(f"{BASE}/{pc_id}/cancel", headers=admin_claim_headers)

    assert _error(refused) == (403, "PRICE_CHANGE_CANCEL_ADMIN_ONLY")
    assert allowed.status_code == 200, allowed.get_data(as_text=True)
    assert allowed.get_json()["data"]["price_change"]["cancelled_by_id"] == admin_user.id


def test_an_applied_change_cannot_be_cancelled(db, manager_claim_headers, admin_claim_headers, sample_product):
    pc_id = _applied(manager_claim_headers, sample_product)

    for headers in (manager_claim_headers, admin_claim_headers):
        response = _client().post(f"{BASE}/{pc_id}/cancel", headers=headers)
        assert _error(response) == (409, "PRICE_CHANGE_NOT_CANCELLABLE")


def test_an_unknown_change_is_a_404(db, manager_claim_headers, sample_product):
    assert _error(_client().get(f"{BASE}/999", headers=manager_claim_headers)) == (404, "PRICE_CHANGE_NOT_FOUND")
    assert _error(_client().post(f"{BASE}/999/cancel", headers=manager_claim_headers)) == (
        404,
        "PRICE_CHANGE_NOT_FOUND",
    )


def _blocked(code):
    return {"allowed": False, "block_reason": REFUSAL_MESSAGES[code]}


@pytest.mark.parametrize(
    "viewer,state,edit,cancel",
    [
        ("operator", "scheduled", HIDDEN, HIDDEN),
        ("manager", "scheduled", T, T),
        ("manager", "announced", _blocked("PRICE_CHANGE_LOCKED"), HIDDEN),
        ("admin", "announced", _blocked("PRICE_CHANGE_LOCKED"), T),
        ("admin", "applied", _blocked("PRICE_CHANGE_LOCKED"), _blocked("PRICE_CHANGE_NOT_CANCELLABLE")),
        ("manager", "applied", _blocked("PRICE_CHANGE_LOCKED"), _blocked("PRICE_CHANGE_NOT_CANCELLABLE")),
        ("admin", "cancelled", _blocked("PRICE_CHANGE_LOCKED"), _blocked("PRICE_CHANGE_NOT_CANCELLABLE")),
    ],
)
def test_allowed_actions_follow_the_rules(
    db, manager_claim_headers, admin_claim_headers, operator_auth_headers, sample_product, viewer, state, edit, cancel
):
    pc_id = _scheduled(manager_claim_headers, sample_product)
    now = get_utc_now()
    if state == "announced":
        _stamp(pc_id, status=PriceChangeStatus.ANNOUNCED, announcement_started_at=now)
    elif state == "applied":
        _stamp(pc_id, status=PriceChangeStatus.APPLIED, announcement_started_at=now, applied_at=now)
    elif state == "cancelled":
        _stamp(pc_id, status=PriceChangeStatus.CANCELLED, cancelled_at=now)
    headers = {"operator": operator_auth_headers, "manager": manager_claim_headers, "admin": admin_claim_headers}[viewer]

    row = _client().get(f"{BASE}/{pc_id}", headers=headers).get_json()["data"]["price_change"]

    assert row["allowed_actions"] == {"edit": edit, "cancel": cancel}


def test_stage_follows_the_timeline(db, manager_user, sample_product):
    today = local_date()
    pc = PriceChange(
        announce_on=today + timedelta(days=7),
        change_on=today + timedelta(days=35),
        created_by_id=manager_user.id,
        status=PriceChangeStatus.SCHEDULED,
    )
    db.session.add(pc)
    db.session.commit()
    now = get_utc_now()

    assert price_change_stage(pc, now=now) == "scheduled"

    pc.status, pc.announcement_started_at = PriceChangeStatus.ANNOUNCED, now
    notice = PriceChangeNotice(
        price_change_id=pc.id,
        user_id=manager_user.id,
        kind=PriceChangeNoticeKind.ANNOUNCEMENT,
        channel=None,
        queued_at=now,
    )
    db.session.add(notice)
    db.session.commit()
    assert price_change_stage(pc, now=now) == "announcing"

    notice.channel = "telegram"
    db.session.commit()
    assert price_change_stage(pc, now=now) == "announced"

    pc.reminder_started_at = now
    db.session.commit()
    assert price_change_stage(pc, now=now) == "reminded"

    pc.status, pc.applied_at = PriceChangeStatus.APPLIED, now
    db.session.commit()
    ends = pc.courtesy_ends_at
    assert price_change_stage(pc, now=ends - timedelta(seconds=1)) == "applied"
    assert price_change_stage(pc, now=ends) == "window_closed"

    pc.status, pc.applied_at, pc.cancelled_at = PriceChangeStatus.CANCELLED, None, now
    db.session.commit()
    assert price_change_stage(pc, now=now) == "cancelled"


def test_login_publishes_the_price_change_flags(
    db, admin_user, manager_user, operator_user, delivery_driver
):
    names = ("can_view_price_changes", "can_manage_price_changes", "can_cancel_announced_price_changes")
    customer = make_customer(db)

    def flags(user):
        permissions = AuthService().get_user_permissions(user.id)
        return tuple(permissions[name] for name in names)

    assert flags(admin_user) == (True, True, True)
    assert flags(manager_user) == (True, True, False)
    assert flags(operator_user) == (True, False, False)
    assert flags(delivery_driver) == (False, False, False)
    assert flags(customer) == (False, False, False)


def test_the_view_flag_equals_what_the_list_route_answers(
    db, admin_auth_headers, manager_claim_headers, operator_auth_headers, driver_auth_headers
):
    seen = []
    for headers in (admin_auth_headers, manager_claim_headers, operator_auth_headers, driver_auth_headers):
        client = _client()
        flag = client.get(PROFILE, headers=headers).get_json()["data"]["permissions"]["can_view_price_changes"]
        status = client.get(BASE, headers=headers).status_code
        assert status in (200, 403)
        assert flag == (status == 200)
        seen.append(flag)

    assert seen == [True, True, True, False]
