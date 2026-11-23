"""The duplicate-address guard, end to end (spec 2026-11-18 §6): the HTTP routes the clients really call, with the
bodies they really send. Sections are added per task: "Customer create and update" (`/api/v1/auth/addresses`: the
website wizard's and the customer bot's calls) and "Move" (`POST /api/v1/auth/addresses/<id>/move`: the bot's and
the website's Move, and the bot's Edit location).

Faked: the post-commit seams (`address_fanout`) and the audit sink (`audit_events`), each bound against the real
signature it stands in for, and the staff-bot webhook the driver alert posts to. Every radius is read from
`app.config`.
"""

import json
from pathlib import Path

import pytest

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import OrderEditHistory
from business_app.models.tryout import ProductTryout
from business_app.models.user import UserAddress
from business_app.services.address_guard_service import pin_move_refusal
from business_app.services.delivery_service import DeliveryService
from business_app.tasks import staff_tasks
from business_app.utils.audit_logger import AuditEventType, AuditSeverity
from shared.constants import TASHKENT_COORDINATES, TASHKENT_DISTRICTS, is_within_tashkent
from shared.enums import DeliveryStatus as S
from shared.enums import OrderStatus
from tests.integration.address_guard_geometry import BASE, at
from tests.integration.sales_pay_builders import make_outlet
from tests.integration.test_admin_order_address_change import _AddressFanOut, address_fanout  # noqa: F401  fixture
from tests.integration.test_admin_order_reschedule import (  # noqa: F401  fixtures
    DRIVER_TELEGRAM_ID,
    _seed,
    audit_events,
    rostered_driver,
)
from tests.integration.test_delivery_failed_alert import _webhook
from tests.unit._scope_money_helpers import make_place_group

pytestmark = pytest.mark.integration

CREATE = "/api/v1/auth/addresses"
MOVE = "/api/v1/auth/addresses/{}/move"
_WEB_ADDRESSES = Path(__file__).resolve().parents[1] / "fixtures" / "web_addresses"
# The website wizard's recorded create body (`payload()`), which its Move sends too.
WEB_NEW = json.loads((_WEB_ADDRESSES / "new_payload.json").read_text(encoding="utf-8"))
# The website wizard's recorded edit body: every field, `is_default`, and `point_source` null.
WEB_EDIT = json.loads((_WEB_ADDRESSES / "edit_payload.json").read_text(encoding="utf-8"))
CITY_CENTRE = (TASHKENT_COORDINATES["latitude"], TASHKENT_COORDINATES["longitude"])
CHILANZAR = TASHKENT_DISTRICTS["chilanzar"]["center"]
YUNUSABAD = TASHKENT_DISTRICTS["yunusabad"]["center"]


def bot_pin_body(point, **extra):
    """The bot's `_build_address_payload` shape (telegram_bot/handlers/profile.py) plus `point_source`."""
    return {
        "title": "Uy",
        "full_address": "15, Chilonzor dahasi",
        "city": "Toshkent",
        "latitude": point[0],
        "longitude": point[1],
        "point_source": "map_pin",
        **extra,
    }


def _saved(user, point, **columns):
    latitude, longitude = point if point is not None else (None, None)
    address = UserAddress(
        user_id=user.id,
        title=columns.pop("title", "Boshqa"),
        full_address=columns.pop("full_address", "Chilonzor 12-kv, 5-uy"),
        city="Tashkent",
        latitude=latitude,
        longitude=longitude,
        **columns,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _stored_pin(address_id):
    db.session.expire_all()
    row = db.session.get(UserAddress, address_id)
    return (row.latitude, row.longitude)


@pytest.fixture
def uy(user_address):
    """The customer's saved address at the base pin, titled "Uy"."""
    user_address.title = "Uy"
    db.session.commit()
    return user_address


@pytest.fixture
def put_url():
    return lambda address_id: f"{CREATE}/{address_id}"


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", ["map_pin", "gps"], ids=["a_placed_pin", "my_location"])
def test_a_pin_14_m_from_a_saved_address_is_refused_with_the_candidate_and_a_move_offer(
    client, auth_headers, uy, source
):
    """A point the customer placed: a map tap or drag (`map_pin`), or the website's "Use my location" (`gps`)."""
    response = client.post(CREATE, json=bot_pin_body(at(14), point_source=source), headers=auth_headers)

    assert response.status_code == 409, response.get_data(as_text=True)
    body = response.get_json()
    assert body["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert body["details"]["move_address_id"] == uy.id
    assert [c["id"] for c in body["details"]["candidates"]] == [uy.id]
    assert body["details"]["candidates"][0]["distance_m"] == 14
    assert UserAddress.query.filter_by(user_id=uy.user_id).count() == 1


@pytest.mark.parametrize("source", ["typed", "search", None, "bogus"])
def test_a_point_the_customer_did_not_place_gets_no_move_offer(client, auth_headers, uy, source):
    body = bot_pin_body(at(14))
    if source is None:
        del body["point_source"]
    else:
        body["point_source"] = source

    response = client.post(CREATE, json=body, headers=auth_headers)

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["details"]["move_address_id"] is None


@pytest.mark.parametrize("flag,status", [(True, 201), ("false", 409), ("true", 409), (1, 409)])
def test_only_a_json_true_confirms(client, auth_headers, uy, flag, status):
    response = client.post(CREATE, json=bot_pin_body(at(14), confirm_duplicate=flag), headers=auth_headers)

    assert response.status_code == status, response.get_data(as_text=True)
    saved = UserAddress.query.filter_by(user_id=uy.user_id).count()
    assert saved == (2 if status == 201 else 1)


def test_the_confirm_flag_and_point_source_are_never_stored(client, auth_headers, uy):
    response = client.post(CREATE, json=bot_pin_body(at(14), confirm_duplicate=True), headers=auth_headers)

    assert response.status_code == 201, response.get_data(as_text=True)
    row = response.get_json()["data"]["address"]
    assert "confirm_duplicate" not in row and "point_source" not in row


def test_the_radius_edge_comes_from_config(app, client, auth_headers, uy):
    radius = app.config["ADDRESS_DUPLICATE_RADIUS_M"]

    inside = client.post(CREATE, json=bot_pin_body(at(radius - 2)), headers=auth_headers)
    outside = client.post(CREATE, json=bot_pin_body(at(radius + 2)), headers=auth_headers)

    assert (inside.status_code, outside.status_code) == (409, 201)


def test_another_customers_address_3_m_away_never_counts(client, auth_headers, second_sample_user):
    _saved(second_sample_user, at(3))

    response = client.post(CREATE, json=bot_pin_body(BASE), headers=auth_headers)

    assert response.status_code == 201, response.get_data(as_text=True)


def test_known_different_apartments_save_and_one_unknown_apartment_asks(
    client, auth_headers, sample_user, user_address
):
    user_address.apartment_number = "5"
    db.session.commit()

    different = client.post(CREATE, json=bot_pin_body(at(3), apartment_number="7"), headers=auth_headers)
    unknown = client.post(CREATE, json=bot_pin_body(at(3)), headers=auth_headers)

    assert (different.status_code, unknown.status_code) == (201, 409)


@pytest.mark.parametrize("saved,status", [("5", 409), ("7", 201)], ids=["the_same_apartment", "another_apartment"])
def test_an_apartment_sent_as_a_json_number_is_compared_as_its_text(
    client, auth_headers, user_address, saved, status
):
    """API callers may send `apartment_number` as a JSON number: 5 is apartment "5", never a 500."""
    user_address.apartment_number = saved
    db.session.commit()

    response = client.post(CREATE, json=bot_pin_body(at(3), apartment_number=5), headers=auth_headers)

    assert response.status_code == status, response.get_data(as_text=True)


@pytest.mark.parametrize("fallback", [CHILANZAR, CITY_CENTRE], ids=["district_centre", "city_centre"])
def test_fallback_points_never_prompt(client, auth_headers, sample_user, fallback):
    _saved(sample_user, fallback)

    response = client.post(CREATE, json=bot_pin_body(fallback, point_source="search"), headers=auth_headers)

    assert response.status_code == 201, response.get_data(as_text=True)


def test_string_coordinates_are_guarded_like_numbers(client, auth_headers, uy):
    near = at(14)

    response = client.post(
        CREATE, json={"title": "Uy 2", "latitude": f"{near[0]}", "longitude": f"{near[1]}"}, headers=auth_headers
    )

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #


def test_the_wizards_full_put_with_unchanged_coordinates_is_never_re_asked(
    client, auth_headers, uy, sample_user, put_url
):
    first = client.post(CREATE, json=bot_pin_body(at(14), confirm_duplicate=True), headers=auth_headers)
    assert first.status_code == 201, first.get_data(as_text=True)
    second_id = first.get_json()["data"]["address"]["id"]
    row = UserAddress.query.get(second_id)

    response = client.put(
        put_url(second_id),
        json={**WEB_EDIT, "title": "Uy 2", "latitude": row.latitude, "longitude": row.longitude},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["address"]["title"] == "Uy 2"


def test_the_bots_detail_only_put_is_never_re_asked(client, auth_headers, uy, put_url):
    first = client.post(CREATE, json=bot_pin_body(at(14), confirm_duplicate=True), headers=auth_headers)
    second_id = first.get_json()["data"]["address"]["id"]

    response = client.put(
        put_url(second_id),
        json={"apartment_number": "9", "floor_number": "3", "delivery_instructions": "Door code 12", "landmark": "Bank"},
        headers=auth_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["address"]["apartment_number"] == "9"


def test_a_put_moving_the_pin_onto_another_own_address_asks_without_a_move_offer(
    client, auth_headers, uy, sample_user, put_url
):
    other = _saved(sample_user, at(500))
    target = at(14)

    asked = client.put(
        put_url(other.id),
        json={"latitude": target[0], "longitude": target[1], "point_source": "map_pin"},
        headers=auth_headers,
    )
    confirmed = client.put(
        put_url(other.id),
        json={"latitude": target[0], "longitude": target[1], "confirm_duplicate": True},
        headers=auth_headers,
    )

    assert asked.status_code == 409, asked.get_data(as_text=True)
    body = asked.get_json()
    assert body["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert body["details"]["move_address_id"] is None
    assert [c["id"] for c in body["details"]["candidates"]] == [uy.id]
    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    assert _stored_pin(other.id) == pytest.approx(target)


def test_a_brand_new_row_re_pinned_2_km_away_is_saved(client, auth_headers, sample_user, put_url):
    fresh = _saved(sample_user, BASE)
    target = at(2000)

    response = client.put(put_url(fresh.id), json={"latitude": target[0], "longitude": target[1]}, headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(fresh.id) == pytest.approx(target)


def test_an_address_with_an_order_moves_at_most_the_configured_cap(
    app, client, auth_headers, uy, rostered_driver, local_noon, put_url  # noqa: F811
):
    _seed(uy, rostered_driver, S.DELIVERED, local_noon.date(), order_status=OrderStatus.DELIVERED)
    cap = app.config["ADDRESS_MOVE_MAX_M"]
    far, near = at(cap + 50), at(cap - 30)

    refused = client.put(put_url(uy.id), json={"latitude": far[0], "longitude": far[1]}, headers=auth_headers)
    allowed = client.put(put_url(uy.id), json={"latitude": near[0], "longitude": near[1]}, headers=auth_headers)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error_code"] == "ADDRESS_MOVE_TOO_FAR"
    assert body["details"]["reason"] == "TOO_FAR"
    assert allowed.status_code == 200, allowed.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(near)


def test_a_fallback_pin_with_history_may_move_any_distance(
    client, auth_headers, sample_user, rostered_driver, local_noon, put_url  # noqa: F811
):
    stored = _saved(sample_user, YUNUSABAD)
    _seed(stored, rostered_driver, S.DELIVERED, local_noon.date(), order_status=OrderStatus.DELIVERED)
    target = at(3000, origin=YUNUSABAD)

    response = client.put(
        put_url(stored.id), json={"latitude": target[0], "longitude": target[1]}, headers=auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(stored.id) == pytest.approx(target)


def test_giving_a_pinless_address_its_first_pin_is_never_too_far(
    client, auth_headers, sample_user, rostered_driver, local_noon, put_url  # noqa: F811
):
    pinless = _saved(sample_user, None)
    _seed(pinless, rostered_driver, S.DELIVERED, local_noon.date(), order_status=OrderStatus.DELIVERED)
    far = at(2000)

    first = client.put(put_url(pinless.id), json={"latitude": far[0], "longitude": far[1]}, headers=auth_headers)

    assert first.status_code == 200, first.get_data(as_text=True)
    assert _stored_pin(pinless.id) == pytest.approx(far)

    other = _saved(sample_user, None)
    _seed(other, rostered_driver, S.DELIVERED, local_noon.date(), order_status=OrderStatus.DELIVERED)
    onto = at(2003)
    second = client.put(put_url(other.id), json={"latitude": onto[0], "longitude": onto[1]}, headers=auth_headers)

    assert second.status_code == 409, second.get_data(as_text=True)
    assert second.get_json()["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"


def test_a_pin_change_with_the_driver_is_refused_and_a_detail_put_is_not(
    client, auth_headers, uy, rostered_driver, local_noon, put_url  # noqa: F811
):
    order = _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())
    target = at(100)

    refused = client.put(put_url(uy.id), json={"latitude": target[0], "longitude": target[1]}, headers=auth_headers)
    detail = client.put(put_url(uy.id), json={"apartment_number": "9"}, headers=auth_headers)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error_code"] == "ADDRESS_PIN_WITH_DRIVER"
    assert {k: body["details"][k] for k in ("reason", "order_number")} == {
        "reason": "WITH_DRIVER",
        "order_number": order.order_number,
    }
    assert detail.status_code == 200, detail.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(BASE)


def test_a_put_that_nulls_the_pin_leaves_it_unchanged(
    client, auth_headers, uy, rostered_driver, local_noon, put_url  # noqa: F811
):
    _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())

    response = client.put(
        put_url(uy.id), json={"latitude": None, "longitude": None, "apartment_number": "4"}, headers=auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(BASE)
    assert response.get_json()["data"]["address"]["apartment_number"] == "4"


def test_a_put_with_an_out_of_range_coordinate_leaves_the_pin_and_writes_the_rest(client, auth_headers, uy, put_url):
    """An out-of-range point is no point (as a null or a non-numeric one): the edit is the plain write."""
    response = client.put(
        put_url(uy.id), json={"latitude": 200, "longitude": BASE[1], "apartment_number": "4"}, headers=auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(BASE)
    assert response.get_json()["data"]["address"]["apartment_number"] == "4"


def test_the_website_put_that_moves_a_pin_tells_the_driver_and_records_history(
    client, auth_headers, uy, sample_user, rostered_driver, local_noon, put_url, address_fanout, audit_events  # noqa: F811
):
    """The wizard's own edit body with a moved pin: the pin, every other field and the follow-up `is_default` are
    written together, and the driver alert, the history row and the audit event are the Move's (spec §6.12)."""
    home = _saved(sample_user, at(2000), is_default=True)
    uy.is_default = False
    db.session.commit()
    seeded = _seed(uy, rostered_driver, S.ASSIGNED, local_noon.date())
    target = at(100)
    new_line = WEB_EDIT["full_address"]
    before = {"id": uy.id, "title": "Uy", "full_address": LINE, "latitude": BASE[0], "longitude": BASE[1]}
    after = {
        "id": uy.id,
        "title": WEB_EDIT["title"],
        "full_address": new_line,
        "latitude": target[0],
        "longitude": target[1],
    }

    response = client.put(
        put_url(uy.id), json={**WEB_EDIT, "latitude": target[0], "longitude": target[1]}, headers=auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert address_fanout.calls["driver"] == [
        {
            "delivery_id": seeded.delivery_id,
            "driver_id": rostered_driver.id,
            "payload": {
                "telegram_id": int(DRIVER_TELEGRAM_ID),
                "delivery_id": seeded.delivery_id,
                "delivery_address_id": uy.id,
                "order_info": {
                    "delivery_id": seeded.delivery_id,
                    "order_id": seeded.order_id,
                    "order_number": seeded.order_number,
                    "status": "confirmed",
                    "total_amount": 50000.0,
                    "payment_method": "cash",
                    "delivery_address": new_line,
                },
                "old_address": LINE,  # read before the write: the edited address is the same row
                "new_address": new_line,
                "lat": target[0],
                "lng": target[1],
                "door": {
                    "address": new_line,
                    "district": WEB_EDIT["district"],
                    "apartment_number": "",
                    "floor_number": "",
                    "landmark": "",
                    "delivery_instructions": "",
                },
                "pin_moved_m": 100,
            },
        }
    ]
    assert address_fanout.calls["route"] == [
        {
            "driver_id": rostered_driver.id,
            "sound": False,
            "materiality": None,
            "trigger": "address_change",
            "event_id": None,
            "delivery_id": None,
        }
    ]
    assert address_fanout.calls["optimize"] == [{"driver_id": rostered_driver.id, "trigger": "address_change"}]
    rows = OrderEditHistory.query.filter_by(order_id=seeded.order_id).all()
    assert [(row.reason, row.edited_by_user_id, row.diff) for row in rows] == [
        (
            "customer moved the address pin",
            sample_user.id,
            {
                "kind": "address_change",
                "address_before": before,
                "address_after": after,
                "driver_notified": True,
                "pin_moved_m": 100,
            },
        )
    ]
    assert [e for e in audit_events if e["action"] == "address_pin_moved"] == [
        {
            "event_type": AuditEventType.USER_UPDATED,
            "action": "address_pin_moved",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "address",
            "resource_id": str(uy.id),
            "description": None,
            "old_values": before,
            "new_values": after,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "user_id": sample_user.id,
                "actor_id": sample_user.id,
                "pin_moved_m": 100,
                "order_ids": [seeded.order_id],
            },
        }
    ]
    stored = _row(uy.id)
    assert {
        field: stored[field] for field in ("title", "full_address", "district", "is_default", "is_business")
    } == {
        "title": WEB_EDIT["title"],
        "full_address": new_line,
        "district": WEB_EDIT["district"],
        "is_default": True,
        "is_business": True,
    }
    assert (stored["latitude"], stored["longitude"]) == pytest.approx(target)
    assert _row(home.id)["is_default"] is False


def test_the_address_list_publishes_each_rows_pin_move_refusal(
    client, auth_headers, sample_user, second_sample_user, rostered_driver, local_noon  # noqa: F811
):
    plain = _saved(sample_user, at(0))
    driving = _saved(sample_user, at(1000))
    grouped = _saved(sample_user, at(2000))
    held = _saved(sample_user, at(3000))
    _seed(driving, rostered_driver, S.PICKED_UP, local_noon.date())
    make_place_group(db, grouped, _saved(second_sample_user, at(2000)))
    outlet = make_outlet(db, onboarded_by=None, created_at=driving.created_at, user=sample_user)
    outlet.address_id = held.id
    db.session.commit()

    response = client.get(CREATE, headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    rows = response.get_json()["data"]["addresses"]
    assert {row["id"]: row["pin_move_refusal"] for row in rows} == {
        plain.id: None,
        driving.id: "WITH_DRIVER",
        grouped.id: "SHARED_PLACE",
        held.id: "OUTLET",
    }


# --------------------------------------------------------------------------- #
# Move
# --------------------------------------------------------------------------- #

LINE = "1 Test St, Tashkent"  # uy's written line (the `user_address` fixture)


def bot_move_body(point, **extra):
    """What the bot's prompt Move and its Edit location send: the pin only (spec §6.3)."""
    return {"latitude": point[0], "longitude": point[1], "point_source": "map_pin", **extra}


def web_move_body(point, **extra):
    """What the website's prompt Move sends: the wizard's `payload()` with the placed point, confirmed."""
    return {
        **WEB_NEW,
        "latitude": point[0],
        "longitude": point[1],
        "point_source": "map_pin",
        "confirm_duplicate": True,
        **extra,
    }


def _row(address_id):
    db.session.expire_all()
    return db.session.get(UserAddress, address_id).to_dict()


def test_the_website_move_replaces_the_pin_and_written_line_and_only_fills_empty_details(client, auth_headers, uy):
    uy.apartment_number, uy.floor_number, uy.delivery_instructions = "5", None, "Ring twice"
    db.session.commit()
    before = uy.to_dict()
    target = at(30)
    body = web_move_body(
        target, apartment_number="9", floor_number="3", delivery_instructions="x", title="Ofis", is_default=False
    )

    response = client.post(MOVE.format(uy.id), json=body, headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    # The same row: its id, title "Uy", default flag, apartment "5" and notes "Ring twice" are kept.
    expected = {
        **before,
        "latitude": target[0],
        "longitude": target[1],
        "full_address": WEB_NEW["full_address"],
        "street_address": WEB_NEW["street_address"],
        "district": WEB_NEW["district"],
        "floor_number": "3",  # the saved one was empty
        "landmark": WEB_NEW["landmark"],  # the saved one was empty
    }
    assert response.get_json()["data"]["address"] == expected
    assert _row(uy.id) == expected


def test_the_bot_move_changes_only_the_pin(client, auth_headers, uy):
    uy.full_address, uy.district = "Chilonzor 12-kv, 5-uy, 2-podyezd", "chilanzar"
    db.session.commit()
    before = uy.to_dict()
    target = at(40)

    response = client.post(MOVE.format(uy.id), json=bot_move_body(target), headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    expected = {**before, "latitude": target[0], "longitude": target[1]}
    assert response.get_json()["data"]["address"] == expected
    assert _row(uy.id) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"point_source": "map_pin"},
        {"latitude": 41.3115, "longitude": 69.2797, "point_source": "search"},
        {"latitude": CHILANZAR[0], "longitude": CHILANZAR[1], "point_source": "map_pin"},
        {"latitude": CITY_CENTRE[0], "longitude": CITY_CENTRE[1], "point_source": "gps"},
        {"latitude": 200, "longitude": 69.2797, "point_source": "map_pin"},
    ],
    ids=["no_point", "a_search_result", "a_district_centre", "the_city_centre", "an_out_of_range_latitude"],
)
def test_a_move_needs_a_placed_pin(client, auth_headers, uy, body):
    response = client.post(MOVE.format(uy.id), json=body, headers=auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "ADDRESS_MOVE_NEEDS_PIN"
    assert _stored_pin(uy.id) == BASE


def test_the_websites_my_location_moves_the_pin(client, auth_headers, uy):
    """The website's "Use my location" sends `point_source` "gps": a placed pin, so its Move goes through."""
    target = at(40)

    response = client.post(MOVE.format(uy.id), json=web_move_body(target, point_source="gps"), headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(target)


def test_a_move_past_the_cap_of_an_address_with_orders_is_too_far(
    app, client, auth_headers, uy, rostered_driver, local_noon, address_fanout, audit_events  # noqa: F811
):
    _seed(uy, rostered_driver, S.DELIVERED, local_noon.date(), order_status=OrderStatus.DELIVERED)
    far = at(app.config["ADDRESS_MOVE_MAX_M"] + 50)

    response = client.post(MOVE.format(uy.id), json=bot_move_body(far, confirm_duplicate=True), headers=auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert (body["error_code"], body["details"]["reason"]) == ("ADDRESS_MOVE_TOO_FAR", "TOO_FAR")
    assert _stored_pin(uy.id) == BASE
    assert address_fanout.calls == {seam: [] for seam in _AddressFanOut.SEAMS}
    assert audit_events == []


def test_a_move_within_a_metre_writes_no_pin_even_with_the_driver(
    client, auth_headers, uy, rostered_driver, local_noon, address_fanout, audit_events  # noqa: F811
):
    """Nothing moved, so nothing is locked, recorded or sent, and the point is not written either: a pin changes
    only through the pin-change path (ruling C4), so steps under a metre can never walk it under a carried order."""
    _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())

    response = client.post(MOVE.format(uy.id), json=bot_move_body(at(0.5)), headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == BASE
    assert address_fanout.calls == {seam: [] for seam in _AddressFanOut.SEAMS}
    assert audit_events == []


def test_a_move_next_to_another_own_address_asks_first(client, auth_headers, uy, sample_user):
    other = _saved(sample_user, at(100))
    target = at(90)

    asked = client.post(MOVE.format(uy.id), json=bot_move_body(target), headers=auth_headers)
    pin_after_asking = _stored_pin(uy.id)
    confirmed = client.post(
        MOVE.format(uy.id), json=bot_move_body(target, confirm_duplicate=True), headers=auth_headers
    )

    assert asked.status_code == 409, asked.get_data(as_text=True)
    body = asked.get_json()
    assert body["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert body["details"] == {"move_address_id": None, "candidates": [{**_row(other.id), "distance_m": 10}]}
    assert pin_after_asking == BASE
    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(target)


@pytest.mark.parametrize(
    "setup,code",
    [
        ("with_driver", "ADDRESS_PIN_WITH_DRIVER"),
        ("shared_place", "ADDRESS_PIN_SHARED_PLACE"),
        ("outlet", "ADDRESS_PIN_OUTLET"),
    ],
)
def test_the_move_offer_agrees_with_the_write(
    client, auth_headers, uy, sample_user, second_sample_user, rostered_driver, local_noon, setup, code  # noqa: F811
):
    if setup == "with_driver":
        _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())
    elif setup == "shared_place":
        make_place_group(db, uy, _saved(second_sample_user, at(3)))
    else:
        outlet = make_outlet(db, onboarded_by=None, created_at=uy.created_at, user=sample_user)
        outlet.address_id = uy.id
        db.session.commit()
    near = at(14)

    offered = client.post(CREATE, json=bot_pin_body(near), headers=auth_headers)
    moved = client.post(MOVE.format(uy.id), json=bot_move_body(near), headers=auth_headers)

    assert offered.status_code == 409, offered.get_data(as_text=True)
    assert offered.get_json()["details"]["move_address_id"] is None
    assert moved.status_code == 400, moved.get_data(as_text=True)
    assert moved.get_json()["error_code"] == code
    assert _stored_pin(uy.id) == BASE


def test_an_assigned_order_is_redirected_by_a_move(
    client, auth_headers, uy, sample_user, rostered_driver, local_noon, address_fanout, audit_events  # noqa: F811
):
    seeded = _seed(uy, rostered_driver, S.ASSIGNED, local_noon.date())
    seeded_km = db.session.get(Delivery, seeded.delivery_id).distance_km
    target = at(100)
    new_line = WEB_NEW["full_address"]
    before = {"id": uy.id, "title": "Uy", "full_address": LINE, "latitude": BASE[0], "longitude": BASE[1]}
    after = {**before, "full_address": new_line, "latitude": target[0], "longitude": target[1]}

    response = client.post(MOVE.format(uy.id), json=web_move_body(target), headers=auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert address_fanout.calls["driver"] == [
        {
            "delivery_id": seeded.delivery_id,
            "driver_id": rostered_driver.id,
            "payload": {
                "telegram_id": int(DRIVER_TELEGRAM_ID),
                "delivery_id": seeded.delivery_id,
                "delivery_address_id": uy.id,
                "order_info": {
                    "delivery_id": seeded.delivery_id,
                    "order_id": seeded.order_id,
                    "order_number": seeded.order_number,
                    "status": "confirmed",
                    "total_amount": 50000.0,
                    "payment_method": "cash",
                    "delivery_address": new_line,
                },
                "old_address": LINE,  # read before the write: the moved address is the same row
                "new_address": new_line,
                "lat": target[0],
                "lng": target[1],
                "door": {
                    "address": new_line,
                    "district": WEB_NEW["district"],
                    "apartment_number": WEB_NEW["apartment_number"],
                    "floor_number": WEB_NEW["floor_number"],
                    "landmark": WEB_NEW["landmark"],
                    "delivery_instructions": WEB_NEW["delivery_instructions"],
                },
                "pin_moved_m": 100,
            },
        }
    ]
    assert address_fanout.calls["route"] == [
        {
            "driver_id": rostered_driver.id,
            "sound": False,
            "materiality": None,
            "trigger": "address_change",
            "event_id": None,
            "delivery_id": None,
        }
    ]
    assert address_fanout.calls["optimize"] == [{"driver_id": rostered_driver.id, "trigger": "address_change"}]

    db.session.expire_all()
    recomputed = db.session.get(Delivery, seeded.delivery_id).distance_km
    assert recomputed == DeliveryService().distance_km(db.session.get(UserAddress, uy.id))
    assert recomputed != seeded_km
    rows = OrderEditHistory.query.filter_by(order_id=seeded.order_id).all()
    assert [(row.reason, row.edited_by_user_id, row.diff) for row in rows] == [
        (
            "customer moved the address pin",
            sample_user.id,
            {
                "kind": "address_change",
                "address_before": before,
                "address_after": after,
                "driver_notified": True,
                "pin_moved_m": 100,
            },
        )
    ]
    assert [e for e in audit_events if e["action"] == "address_pin_moved"] == [
        {
            "event_type": AuditEventType.USER_UPDATED,
            "action": "address_pin_moved",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "address",
            "resource_id": str(uy.id),
            "description": None,
            "old_values": before,
            "new_values": after,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "user_id": sample_user.id,
                "actor_id": sample_user.id,
                "pin_moved_m": 100,
                "order_ids": [seeded.order_id],
            },
        }
    ]


def test_two_moves_in_a_row_make_the_first_alert_stale(
    client, auth_headers, uy, rostered_driver, local_noon, address_fanout, monkeypatch  # noqa: F811
):
    seeded = _seed(uy, rostered_driver, S.ASSIGNED, local_noon.date())
    for north in (60, 120):
        response = client.post(MOVE.format(uy.id), json=bot_move_body(at(north)), headers=auth_headers)
        assert response.status_code == 200, response.get_data(as_text=True)
    first, second = address_fanout.calls["driver"]
    sent = _webhook(monkeypatch, True)
    db.session.expire_all()

    # A delayed retry of the first alert, after the second move queued its own.
    late = staff_tasks.notify_order_address_changed.apply(
        args=(first["delivery_id"], first["driver_id"], first["payload"]), task_id="move-1"
    ).get()
    current = staff_tasks.notify_order_address_changed.apply(
        args=(second["delivery_id"], second["driver_id"], second["payload"]), task_id="move-2"
    ).get()

    assert late == {"success": False, "reason": "stale", "delivery_id": seeded.delivery_id}
    assert current == {"success": True, "delivery_id": seeded.delivery_id}
    assert sent == [
        ("/internal/order-address-changed", {**second["payload"], "event_id": "order_address_changed:move-2"})
    ]


@pytest.mark.parametrize("whose", ["another_customers", "deleted"])
def test_moving_another_customers_address_is_404_and_writes_nothing(
    client,
    auth_headers,
    uy,
    sample_user,
    second_sample_user,
    rostered_driver,  # noqa: F811
    local_noon,
    address_fanout,  # noqa: F811
    audit_events,  # noqa: F811
    whose,
):
    """The id was tampered with, or the address was deleted between the prompt and the tap (Review Focus 5)."""
    if whose == "another_customers":
        target = _saved(second_sample_user, at(500))
        _seed(target, rostered_driver, S.ASSIGNED, local_noon.date())
        address_id, before = target.id, target.to_dict()
    else:
        gone = _saved(sample_user, at(500))
        address_id, before = gone.id, None
        db.session.delete(gone)
        db.session.commit()

    response = client.post(
        MOVE.format(address_id), json=bot_move_body(at(520), confirm_duplicate=True), headers=auth_headers
    )

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["error"] == "NOT_FOUND"
    db.session.expire_all()
    kept = db.session.get(UserAddress, address_id)
    assert (kept.to_dict() if kept is not None else None) == before
    assert address_fanout.calls == {seam: [] for seam in _AddressFanOut.SEAMS}
    assert audit_events == []
    assert OrderEditHistory.query.count() == 0


def test_a_move_whose_address_is_deleted_after_its_checks_is_a_404_not_a_500(
    client, auth_headers, sample_user, monkeypatch, address_fanout, audit_events  # noqa: F811
):
    """The address is deleted by another request after the move's checks passed and before its locked read: the
    locked read finds no row. (The delete is simulated in the move's own transaction, past the session's identity
    map, right after the duplicate question, the last check.)"""
    from business_app.services import address_pin_service

    doomed = _saved(sample_user, at(500))
    asked = address_pin_service.assert_not_duplicate

    def deleted_right_after_the_last_check(*args, **kwargs):
        asked(*args, **kwargs)
        table = UserAddress.__table__
        db.session.execute(table.delete().where(table.c.id == doomed.id))

    monkeypatch.setattr(address_pin_service, "assert_not_duplicate", deleted_right_after_the_last_check)

    response = client.post(MOVE.format(doomed.id), json=bot_move_body(at(520)), headers=auth_headers)

    assert response.status_code == 404, response.get_data(as_text=True)
    assert response.get_json()["error"] == "NOT_FOUND"
    assert address_fanout.calls == {seam: [] for seam in _AddressFanOut.SEAMS}
    assert audit_events == []


# --------------------------------------------------------------------------- #
# Staff and legacy
# --------------------------------------------------------------------------- #

ADMIN_ADDRESSES = "/api/v1/admin/users/{}/addresses"
OPERATOR_ADDRESSES = "/api/v1/staff/operator/users/{}/addresses"
LEGACY = "/api/v1/addresses/"


def admin_body(point, **extra):
    """The admin UI's address form: the pin as plain numbers, no `point_source`."""
    return {"title": "Ofis", "full_address": "Bobur 3, Yunusobod", "latitude": point[0], "longitude": point[1], **extra}


def test_the_admin_create_and_edit_answer_409_with_the_code_not_500(client, admin_claim_headers, uy, sample_user):
    other = _saved(sample_user, at(500))
    near = at(14)

    asked = client.post(ADMIN_ADDRESSES.format(sample_user.id), json=admin_body(near), headers=admin_claim_headers)
    created = client.post(
        ADMIN_ADDRESSES.format(sample_user.id),
        json=admin_body(near, confirm_duplicate=True),
        headers=admin_claim_headers,
    )
    edit_url = f"{ADMIN_ADDRESSES.format(sample_user.id)}/{other.id}"
    edit_asked = client.put(edit_url, json={"latitude": near[0], "longitude": near[1]}, headers=admin_claim_headers)
    edited = client.put(
        edit_url,
        json={"latitude": near[0], "longitude": near[1], "confirm_duplicate": True},
        headers=admin_claim_headers,
    )

    assert asked.status_code == 409, asked.get_data(as_text=True)
    body = asked.get_json()
    assert body["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert body["details"]["move_address_id"] is None  # staff are never offered a Move
    assert [c["id"] for c in body["details"]["candidates"]] == [uy.id]
    assert created.status_code == 201, created.get_data(as_text=True)
    new_row = created.get_json()["data"]["address"]
    assert "confirm_duplicate" not in new_row
    assert (new_row["title"], new_row["latitude"], new_row["longitude"]) == ("Ofis", near[0], near[1])
    assert edit_asked.status_code == 409, edit_asked.get_data(as_text=True)
    assert edit_asked.get_json()["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert edited.status_code == 200, edited.get_data(as_text=True)
    assert _stored_pin(other.id) == pytest.approx(near)


def test_the_admin_routes_still_refuse_unknown_users_blank_lines_and_out_of_zone_points(
    client, admin_claim_headers, uy, sample_user
):
    missing = client.post(ADMIN_ADDRESSES.format(999999), json=admin_body(at(500)), headers=admin_claim_headers)
    blank = client.post(
        ADMIN_ADDRESSES.format(sample_user.id), json=admin_body(at(500), full_address="  "), headers=admin_claim_headers
    )
    outside = client.post(
        ADMIN_ADDRESSES.format(sample_user.id), json=admin_body((51.5, 0.12)), headers=admin_claim_headers
    )
    no_address = client.put(
        f"{ADMIN_ADDRESSES.format(sample_user.id)}/999999", json={"title": "x"}, headers=admin_claim_headers
    )

    assert missing.status_code == 404
    assert blank.status_code == 400 and "Full address is required" in blank.get_data(as_text=True)
    assert outside.status_code == 400
    assert no_address.status_code == 404
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 1


def test_an_admin_edit_that_sends_a_null_pin_leaves_the_pin_and_still_writes_the_rest(
    client, admin_claim_headers, uy, sample_user
):
    response = client.put(
        f"{ADMIN_ADDRESSES.format(sample_user.id)}/{uy.id}",
        json={"latitude": None, "longitude": None, "apartment_number": "7", "title": "Uy 2"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(BASE)
    row = response.get_json()["data"]["address"]
    assert (row["apartment_number"], row["title"]) == ("7", "Uy 2")


@pytest.mark.parametrize("kind", ["shared_place", "outlet"])
def test_an_admin_pin_edit_of_a_shared_or_outlet_address_under_a_driver_goes_through_and_tells_the_driver(
    client,
    admin_claim_headers,
    admin_user,
    uy,
    sample_user,
    second_sample_user,
    rostered_driver,  # noqa: F811
    local_noon,
    address_fanout,  # noqa: F811
    kind,
):
    if kind == "shared_place":
        make_place_group(db, uy, _saved(second_sample_user, at(2000)))
    else:
        outlet = make_outlet(db, onboarded_by=None, created_at=uy.created_at, user=sample_user)
        outlet.address_id = uy.id
        db.session.commit()
    seeded = _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())
    target = at(100)

    response = client.put(
        f"{ADMIN_ADDRESSES.format(sample_user.id)}/{uy.id}",
        json={"latitude": target[0], "longitude": target[1]},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(target)
    [call] = address_fanout.calls["driver"]
    assert (call["delivery_id"], call["driver_id"]) == (seeded.delivery_id, rostered_driver.id)
    assert call["payload"]["pin_moved_m"] == 100
    assert (call["payload"]["lat"], call["payload"]["lng"]) == (pytest.approx(target[0]), pytest.approx(target[1]))
    rows = OrderEditHistory.query.filter_by(order_id=seeded.order_id).all()
    assert [(row.reason, row.edited_by_user_id) for row in rows] == [("admin edited the address pin", admin_user.id)]


def test_the_operator_create_answers_409_with_details_and_confirm_creates(
    client, operator_auth_headers, uy, sample_user
):
    near = at(14)
    body = {"title": "Ofis", "full_address": "Bobur 3", "latitude": near[0], "longitude": near[1]}

    asked = client.post(OPERATOR_ADDRESSES.format(sample_user.id), json=dict(body), headers=operator_auth_headers)
    created = client.post(
        OPERATOR_ADDRESSES.format(sample_user.id),
        json={**body, "confirm_duplicate": True},
        headers=operator_auth_headers,
    )

    assert asked.status_code == 409, asked.get_data(as_text=True)
    answer = asked.get_json()
    assert answer["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert answer["details"]["move_address_id"] is None
    assert [(c["id"], c["distance_m"]) for c in answer["details"]["candidates"]] == [(uy.id, 14)]
    assert created.status_code == 201, created.get_data(as_text=True)
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 2


def test_the_legacy_routes_use_the_same_guard(
    client, auth_headers, uy, sample_user, rostered_driver, local_noon  # noqa: F811
):
    near = at(14)
    other = _saved(sample_user, at(500))

    created_asked = client.post(
        LEGACY, json={"title": "Ofis", "full_address": "Bobur 3", "latitude": near[0], "longitude": near[1]},
        headers=auth_headers,
    )
    put_asked = client.put(
        f"{LEGACY}{other.id}", json={"latitude": near[0], "longitude": near[1]}, headers=auth_headers
    )
    created = client.post(
        LEGACY,
        json={"title": "Ofis", "full_address": "Bobur 3", "latitude": near[0], "longitude": near[1],
              "confirm_duplicate": True},
        headers=auth_headers,
    )
    order = _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())
    with_driver = client.put(
        f"{LEGACY}{uy.id}", json={"latitude": at(100)[0], "longitude": at(100)[1]}, headers=auth_headers
    )

    assert created_asked.status_code == 409, created_asked.get_data(as_text=True)
    assert created_asked.get_json()["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert put_asked.status_code == 409, put_asked.get_data(as_text=True)
    assert put_asked.get_json()["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert _stored_pin(other.id) == pytest.approx(at(500))
    assert created.status_code == 201, created.get_data(as_text=True)
    assert "confirm_duplicate" not in created.get_json()["data"]["address"]
    assert with_driver.status_code == 400, with_driver.get_data(as_text=True)
    refusal = with_driver.get_json()
    assert refusal["error_code"] == "ADDRESS_PIN_WITH_DRIVER"
    assert refusal["details"]["order_number"] == order.order_number
    assert _stored_pin(uy.id) == pytest.approx(BASE)


def test_the_legacy_put_that_nulls_the_pin_leaves_it_unchanged(client, auth_headers, uy):
    response = client.put(
        f"{LEGACY}{uy.id}", json={"latitude": None, "longitude": None, "apartment_number": "4"}, headers=auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _stored_pin(uy.id) == pytest.approx(BASE)
    assert response.get_json()["data"]["address"]["apartment_number"] == "4"


@pytest.mark.parametrize(
    ("door", "writes_country"),
    [("customer_update", False), ("legacy_update", True), ("admin_update", True)],
)
@pytest.mark.parametrize("moves_the_pin", [False, True], ids=["plain-edit", "pin-edit"])
def test_each_door_writes_its_own_fields_with_or_without_a_pin_change(
    client, auth_headers, admin_claim_headers, uy, door, writes_country, moves_the_pin
):
    """The edit doors keep their own field sets, on the plain write and through the pin-change path alike: the
    customer's own PUT never wrote the country, the legacy PUT and the admin edit do."""
    point = at(30) if moves_the_pin else BASE
    body = {"title": "Ofis", "country": "Kazakhstan", "latitude": point[0], "longitude": point[1]}
    url = {
        "customer_update": f"{CREATE}/{uy.id}",
        "legacy_update": f"{LEGACY}{uy.id}",
        "admin_update": f"{ADMIN_ADDRESSES.format(uy.user_id)}/{uy.id}",
    }[door]
    headers = admin_claim_headers if door == "admin_update" else auth_headers

    response = client.put(url, json=body, headers=headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    row = _row(uy.id)
    assert (row["title"], row["country"]) == ("Ofis", "Kazakhstan" if writes_country else "Uzbekistan")
    assert _stored_pin(uy.id) == pytest.approx(point)


def _route_requests(client, uy, other, near, headers):
    """Every route that can refuse, each sent a body that lands within the radius of `uy`."""
    owner = uy.user_id
    pin = {"latitude": near[0], "longitude": near[1]}
    return {
        "customer_create": lambda: client.post(CREATE, json=bot_pin_body(near), headers=headers["customer"]),
        "customer_update": lambda: client.put(f"{CREATE}/{other.id}", json=pin, headers=headers["customer"]),
        "customer_move": lambda: client.post(
            MOVE.format(other.id), json=bot_move_body(near), headers=headers["customer"]
        ),
        "admin_create": lambda: client.post(
            ADMIN_ADDRESSES.format(owner), json=admin_body(near), headers=headers["admin"]
        ),
        "admin_update": lambda: client.put(
            f"{ADMIN_ADDRESSES.format(owner)}/{other.id}", json=pin, headers=headers["admin"]
        ),
        "operator_create": lambda: client.post(
            OPERATOR_ADDRESSES.format(owner),
            json={"title": "Ofis", "full_address": "Bobur 3", **pin},
            headers=headers["operator"],
        ),
        "legacy_create": lambda: client.post(
            LEGACY, json={"title": "Ofis", "full_address": "Bobur 3", **pin}, headers=headers["customer"]
        ),
        "legacy_update": lambda: client.put(f"{LEGACY}{other.id}", json=pin, headers=headers["customer"]),
    }


ROUTES = [
    "customer_create",
    "customer_update",
    "customer_move",
    "admin_create",
    "admin_update",
    "operator_create",
    "legacy_create",
    "legacy_update",
]


@pytest.mark.parametrize("route", ROUTES)
def test_every_route_that_can_refuse_names_the_code_in_the_body(
    route, client, auth_headers, admin_claim_headers, operator_auth_headers, uy, sample_user
):
    other = _saved(sample_user, at(500))
    headers = {"customer": auth_headers, "admin": admin_claim_headers, "operator": operator_auth_headers}

    response = _route_requests(client, uy, other, at(14), headers)[route]()

    assert response.status_code == 409, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "ADDRESS_POSSIBLE_DUPLICATE"
    assert _stored_pin(other.id) == pytest.approx(at(500))
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 2


# --------------------------------------------------------------------------- #
# Try-out conversion (spec §6.4): adopts the nearest own address instead of duplicating it
# --------------------------------------------------------------------------- #

ADMIN_TRYOUTS = "/api/v1/admin/tryouts"


def _converted(client, headers, user, product, *, point, line, notes=None, outlet=None):
    """Create a try-out for `user` the way the admin form does, then POST its convert; returns the convert response."""
    address = {
        "label": "Try-out",
        "full_address": line,
        "city": "Tashkent",
        "latitude": point[0] if point else None,
        "longitude": point[1] if point else None,
        "is_default": True,
    }
    if notes:
        address["delivery_notes"] = notes
    created = client.post(
        ADMIN_TRYOUTS,
        json={
            "trial_contact": {
                "first_name": "Test",
                "last_name": "User",
                "phone": user.phone,
                "preferred_language": "uz",
            },
            "address": address,
            "items": [{"product_id": product.id, "quantity": 1}],
            "complete_handoff": False,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    tryout_id = created.get_json()["data"]["tryout"]["id"]
    if outlet is not None:
        db.session.get(ProductTryout, tryout_id).outlet_id = outlet.id
        db.session.commit()
    return client.post(f"{ADMIN_TRYOUTS}/{tryout_id}/convert", json={}, headers=headers)


@pytest.mark.parametrize(
    ("saved_notes", "expected_notes"),
    [(None, "Domofon 14"), ("Kod 1234", "Kod 1234")],
    ids=["notes-filled-when-empty", "notes-kept-when-present"],
)
def test_a_saved_address_20_m_from_the_tryout_point_is_adopted_not_duplicated(
    client, admin_claim_headers, sample_user, sample_product, uy, saved_notes, expected_notes
):
    uy.is_default = False
    uy.delivery_instructions = saved_notes
    db.session.commit()

    response = _converted(
        client, admin_claim_headers, sample_user, sample_product, point=at(20), line="Boshqa matn 7", notes="Domofon 14"
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    [row] = UserAddress.query.filter_by(user_id=sample_user.id).all()
    assert row.id == uy.id
    assert row.is_default is True
    assert (row.latitude, row.longitude) == BASE
    assert (row.title, row.full_address) == ("Uy", LINE)
    assert row.delivery_instructions == expected_notes


def test_adoption_does_not_care_that_the_driver_has_that_address(
    client, admin_claim_headers, sample_user, sample_product, uy, rostered_driver, local_noon  # noqa: F811
):
    _seed(uy, rostered_driver, S.PICKED_UP, local_noon.date())

    response = _converted(
        client, admin_claim_headers, sample_user, sample_product, point=at(20), line="Boshqa matn 7"
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 1
    assert _stored_pin(uy.id) == BASE


def test_an_exact_text_match_never_loses_its_pin(client, admin_claim_headers, sample_user, sample_product, uy):
    response = _converted(client, admin_claim_headers, sample_user, sample_product, point=at(300), line=LINE)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 1
    assert _stored_pin(uy.id) == BASE


def test_a_pinless_exact_text_match_in_a_cross_customer_place_group_still_converts(
    client, admin_claim_headers, sample_user, second_sample_user, sample_product
):
    pinless = _saved(sample_user, None, full_address="Guruh uyi 5")
    make_place_group(db, pinless, _saved(second_sample_user, at(2000)))
    point = at(300)
    assert pin_move_refusal(pinless, *point) == "SHARED_PLACE"

    response = _converted(client, admin_claim_headers, sample_user, sample_product, point=point, line="Guruh uyi 5")

    assert response.status_code == 200, response.get_data(as_text=True)
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 1
    assert _stored_pin(pinless.id) == pytest.approx(point)


def test_an_out_of_zone_first_pin_refuses_the_conversion_before_anything_is_written(
    client, admin_claim_headers, sample_user, sample_product, uy
):
    """A pinless exact-text match takes the snapshot's pin as its first pin, and a try-out's own pin is never
    zone-checked: out of the zone, the conversion is refused (400) before the matched address is renamed, given the
    try-out's notes or made the default, and the try-out stays unconverted."""
    pinless = _saved(sample_user, None, title="Ofis", full_address="Guruh uyi 5")
    point = at(40_000)
    assert not is_within_tashkent(*point)

    response = _converted(
        client, admin_claim_headers, sample_user, sample_product, point=point, line="Guruh uyi 5", notes="Domofon 14"
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    db.session.expire_all()
    kept = db.session.get(UserAddress, pinless.id)
    assert (kept.title, kept.latitude, kept.is_default, kept.delivery_instructions) == ("Ofis", None, False, None)
    assert db.session.get(UserAddress, uy.id).is_default is True
    assert ProductTryout.query.one().converted_user_id is None


def test_an_outlet_linked_tryout_keeps_todays_path_without_moving_a_pin(
    client, admin_claim_headers, sample_user, sample_product, uy
):
    far = _saved(sample_user, at(500), full_address="Do'kon 3")
    outlet = make_outlet(db, onboarded_by=None, created_at=uy.created_at, user=sample_user)

    # 20 m from `uy`: a customer try-out would adopt it; an outlet-linked one never does.
    response = _converted(
        client, admin_claim_headers, sample_user, sample_product, point=at(20), line="Do'kon 3", outlet=outlet
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 2
    assert _stored_pin(far.id) == pytest.approx(at(500))
    assert _stored_pin(uy.id) == BASE
    # The outlet_linked branch: the try-out lands on its own address (not the 20 m neighbour) as the default.
    db.session.expire_all()
    assert db.session.get(UserAddress, far.id).is_default is True
    assert db.session.get(UserAddress, uy.id).is_default is False
