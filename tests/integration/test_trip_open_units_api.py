"""Opening the bottle trip with its one-way unit lines, the Load screen and the empty-load rule (spec 2026-11-18 §6.3).

Driven through the routes the staff bot calls: `POST /staff/bottles/session/open` with the load body
(`bottles_loaded` plus `units: [{product_id, quantity}]`) and `GET /staff/bottles/session/load-screen`, the one read
the bot makes at Load.
The open writes one line per product the setting counts (an omitted one at 0) and refuses a load of nothing
(`BOTTLE_SESSION_EMPTY_LOAD`: the service's rule, not the request model's), a product the trip does not count
(`TRIP_UNIT_NOT_COUNTED`) and a driver who co-drives another trip (`BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE`, naming
the owner). Only the Celery publish is recorded (`enqueued`).
"""

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionUnit, DriverBottleTransfer
from business_app.models.delivery import DeliveryPerson
from business_app.serializers.bottle_serializers import DriverBottleSessionOpenRequest
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.exceptions import ValidationError
from shared.enums import DriverBottleSessionStatus, DriverBottleTransferStatus
from shared.staff_constants import MAX_BOTTLES_PER_SESSION
from staff_bot.handlers.base import BaseHandler
from tests.integration.test_bottle_transfer_split_api import _bot_body, seeded_copy  # noqa: F401 -- seeded_copy is a fixture
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    co_driver,
    driver,
    member,
    nineteen,
    owner,
    receiver,
    reoffers,
    sender,
    small,
    staff_headers,
    ten_litre,
    trip,
)

pytestmark = pytest.mark.integration

OPEN = "/api/v1/staff/bottles/session/open"
LOAD_SCREEN = "/api/v1/staff/bottles/session/load-screen"
CURRENT = "/api/v1/staff/bottles/session/current"
JOINABLE = "/api/v1/staff/bottles/sessions/joinable"
TRANSFERS = "/api/v1/staff/bottles/transfers"
CONFIRM = TRANSFERS + "/{}/confirm"


def _line(product_id, *, loaded):
    """A fresh trip line as the session payload publishes it: nothing delivered, transferred, promised or returned."""
    return {
        "product_id": product_id,
        "product_name": "10 Litr suv",
        "loaded": loaded,
        "delivered": 0,
        "transferred_in": 0,
        "transferred_out": 0,
        "on_board": loaded,
        "transferable": loaded,
        "free": loaded,
        "returned": None,
        "discrepancy": None,
    }


def _trips_of(driver):
    return DriverBottleSession.query.filter_by(driver_user_id=driver.id).count()


# --------------------------------------------------------------------------- #
# The open
# --------------------------------------------------------------------------- #


def test_zero_nineteen_and_twelve_ten_litre_opens(client, db, driver, small, enqueued):  # noqa: F811
    r = client.post(
        OPEN,
        json={"bottles_loaded": 0, "units": [{"product_id": small.id, "quantity": 12}]},
        headers=staff_headers(driver),
    )

    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["bottles_loaded"], data["units"]) == (0, [_line(small.id, loaded=12)])
    assert reoffers(enqueued) == 1


def test_the_staff_bots_own_open_body_is_what_the_endpoint_takes(client, db, driver, small, monkeypatch, enqueued):  # noqa: F811
    """The body comes from the bot's client itself, so a key renamed on either side fails here."""
    endpoint, body = _bot_body(
        monkeypatch, "open_bottle_session", 40, units=[{"product_id": small.id, "quantity": 12}]
    )

    r = client.post(endpoint, json=body, headers=staff_headers(driver))

    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["bottles_loaded"], data["units"]) == (40, [_line(small.id, loaded=12)])


@pytest.mark.parametrize("units", [[{"quantity": 0}], []], ids=["ten-litre-typed-as-0", "no-units"])
def test_zero_and_zero_is_refused_by_the_service_not_the_request_model(client, db, driver, small, units):
    body = {"bottles_loaded": 0, "units": [{"product_id": small.id, **line} for line in units]}

    r = client.post(OPEN, json=body, headers=staff_headers(driver))

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error_code"] == "BOTTLE_SESSION_EMPTY_LOAD"
    assert _trips_of(driver) == 0
    assert DriverBottleSessionOpenRequest(bottles_loaded=0).units == []


def test_an_omitted_counted_product_gets_a_zero_line(client, db, driver, small):
    r = client.post(OPEN, json={"bottles_loaded": 40}, headers=staff_headers(driver))

    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["bottles_loaded"], data["units"]) == (40, [_line(small.id, loaded=0)])
    stored = DriverBottleSessionUnit.query.filter_by(session_id=data["id"]).all()
    assert [(row.product_id, row.loaded, row.delivered, row.transferred_in, row.transferred_out) for row in stored] == [
        (small.id, 0, 0, 0, 0)
    ]


@pytest.mark.parametrize(
    "build",
    [lambda db, category: ten_litre(db, category, counted=False), nineteen],
    ids=["unticked-one-way-product", "returnable-bottle"],
)
def test_a_product_not_counted_is_refused(client, db, driver, small, sample_category, build):
    other = build(db, sample_category)

    r = client.post(
        OPEN,
        json={
            "bottles_loaded": 10,
            "units": [{"product_id": small.id, "quantity": 2}, {"product_id": other.id, "quantity": 3}],
        },
        headers=staff_headers(driver),
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    body = r.get_json()
    assert (body["error_code"], body["details"]) == (
        "TRIP_UNIT_NOT_COUNTED",
        {"product_id": other.id, "validation_errors": []},
    )
    assert _trips_of(driver) == 0
    assert DriverBottleSessionUnit.query.count() == 0


def test_a_unit_above_the_load_ceiling_is_refused(client, db, driver, small):
    r = client.post(
        OPEN,
        json={"bottles_loaded": 10, "units": [{"product_id": small.id, "quantity": MAX_BOTTLES_PER_SESSION + 1}]},
        headers=staff_headers(driver),
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    assert _trips_of(driver) == 0


@pytest.mark.parametrize("quantity", [MAX_BOTTLES_PER_SESSION + 1, -1], ids=["above-the-ceiling", "negative"])
def test_the_service_bounds_each_unit_without_the_request_model(db, driver, small, quantity):
    """A caller with no request body in its path (a task, a script) meets the same bounds."""
    with pytest.raises(ValidationError):
        BottleTrackingService().open_bottle_session(driver.id, 10, units={small.id: quantity})

    assert _trips_of(driver) == 0


def test_the_ceiling_itself_is_accepted_for_a_unit(client, db, driver, small):
    r = client.post(
        OPEN,
        json={"bottles_loaded": 0, "units": [{"product_id": small.id, "quantity": MAX_BOTTLES_PER_SESSION}]},
        headers=staff_headers(driver),
    )

    assert r.status_code == 201, r.get_data(as_text=True)
    assert [(u["product_id"], u["loaded"]) for u in r.get_json()["data"]["units"]] == [
        (small.id, MAX_BOTTLES_PER_SESSION)
    ]


def test_a_product_named_twice_is_refused_by_the_request_model(client, db, driver, small):
    r = client.post(
        OPEN,
        json={
            "bottles_loaded": 10,
            "units": [{"product_id": small.id, "quantity": 2}, {"product_id": small.id, "quantity": 3}],
        },
        headers=staff_headers(driver),
    )

    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["errors"] == [f"units: Product {small.id} is listed twice; give each product once."]
    assert _trips_of(driver) == 0


def test_an_active_co_driver_cannot_open_and_is_told_whose_trip(
    client, db, owner, co_driver, small, enqueued  # noqa: F811
):
    s = trip(db, owner, bottles=40)
    member(db, s, co_driver)
    before = DeliveryPerson.query.filter_by(user_id=co_driver.id).one().last_location_update

    r = client.post(OPEN, json={"bottles_loaded": 10}, headers=staff_headers(co_driver))

    body = r.get_json()
    assert (r.status_code, body["error_code"]) == (409, "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE")
    assert body["details"] == {"session_id": s.id, "owner_user_id": owner.id, "owner_name": owner.full_name}
    assert _trips_of(co_driver) == 0
    db.session.expire_all()
    # The anchor at the depot is written first and rolled back with the refusal.
    assert DeliveryPerson.query.filter_by(user_id=co_driver.id).one().last_location_update == before
    assert reoffers(enqueued) == 0


def test_a_second_open_names_the_drivers_own_trip(client, db, driver, small):
    s = trip(db, driver, bottles=40, units={small.id: 12})

    r = client.post(OPEN, json={"bottles_loaded": 10}, headers=staff_headers(driver))

    body = r.get_json()
    assert (r.status_code, body["error_code"]) == (409, "BOTTLE_SESSION_ALREADY_OPEN")
    assert body["details"] == {"session_id": s.id, "owner_user_id": driver.id, "owner_name": "Dilshod"}
    assert _trips_of(driver) == 1


def test_a_disputed_transfer_does_not_block_opening(client, db, driver, owner, small):
    """Dilshod's last trip is closed; a transfer he sent and one he received are both still disputed with an admin."""
    his_last = trip(db, driver, bottles=20, status=DriverBottleSessionStatus.CLOSED)
    theirs = trip(db, owner, bottles=20, status=DriverBottleSessionStatus.CLOSED)
    for sent_from, sender_id, receiver_id in ((his_last, driver.id, owner.id), (theirs, owner.id, driver.id)):
        db.session.add(
            DriverBottleTransfer(
                sender_session_id=sent_from.id,
                sender_driver_id=sender_id,
                receiver_driver_id=receiver_id,
                declared_quantity=3,
                confirmed_quantity=2,
                status=DriverBottleTransferStatus.DISPUTED,
            )
        )
    db.session.commit()

    r = client.post(OPEN, json={"bottles_loaded": 10}, headers=staff_headers(driver))

    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()["data"]
    assert (data["status"], data["bottles_loaded"], data["units"]) == ("open", 10, [_line(small.id, loaded=0)])


def test_a_join_refusal_never_carries_the_open_owner_copy(client, db, owner, co_driver):
    """Join keeps its own sentence: only the open names the owner, so the bot's owner copy is never drawn for a join."""
    s = trip(db, owner, bottles=40)
    member(db, s, co_driver)

    r = client.get(JOINABLE, headers=staff_headers(co_driver))

    body = r.get_json()
    assert (r.status_code, body["error_code"]) == (409, "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE")
    assert "owner_name" not in (body.get("details") or {})
    assert body["message"] == (
        f"Already an active co-driver member of session {s.id}. Leave that session before joining another."
    )


def test_the_bot_words_each_open_refusal(
    client, db, owner, co_driver, driver, small, sample_category, seeded_copy  # noqa: F811 -- fixtures
):
    """The running bot's resolver, fed the seeded English: the open names the owner, the join keeps its sentence."""
    seeded_copy(
        "staff.error.api.membership_already_active_owner_detail",
        "staff.error.api.already_in_session",
        "staff.error.api.bottle_session_empty_load",
        "staff.error.api.trip_unit_not_counted",
    )
    s = trip(db, owner, bottles=40)
    member(db, s, co_driver)
    unticked = ten_litre(db, sample_category, counted=False)
    refusals = [
        client.post(OPEN, json={"bottles_loaded": 10}, headers=staff_headers(co_driver)),
        client.get(JOINABLE, headers=staff_headers(co_driver)),
        client.post(OPEN, json={"bottles_loaded": 0}, headers=staff_headers(driver)),
        client.post(
            OPEN,
            json={"bottles_loaded": 5, "units": [{"product_id": unticked.id, "quantity": 1}]},
            headers=staff_headers(driver),
        ),
    ]

    shown = [
        BaseHandler.__new__(BaseHandler)._resolve_api_error_message(
            "en",
            r.get_json().get("message"),
            r.status_code,
            r.get_json()["error_code"],
            details=r.get_json().get("details"),
        )
        for r in refusals
    ]

    assert shown == [
        "You are on Olim's trip. Leave it first to open your own.",
        "You're already in another driver's session. Leave it before joining a new one.",
        "Type how many you loaded: at least one number must be above 0.",
        "This product is not counted on the bottle trip. Open the load screen again.",
    ]


def test_a_transfer_opened_trip_gets_zero_lines(client, db, sender, receiver, small, enqueued):  # noqa: F811
    """Bobur has nothing open and confirms Aziz's 3 full bottles: the trip the hand-off opens has a 10 L line too."""
    trip(db, sender, bottles=10)
    sent = client.post(
        TRANSFERS,
        json={"receiver_driver_id": receiver.id, "full_quantity": 3, "empty_quantity": 0},
        headers=staff_headers(sender),
    )
    assert sent.status_code == 201, sent.get_data(as_text=True)

    confirmed = client.post(
        CONFIRM.format(sent.get_json()["data"]["id"]),
        json={"confirm_as_declared": True},
        headers=staff_headers(receiver),
    )

    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    current = client.get(CURRENT, headers=staff_headers(receiver)).get_json()["data"]
    assert (current["bottles_loaded"], current["bottles_transferred_in"], current["units"]) == (
        0,
        3,
        [_line(small.id, loaded=0)],
    )


def test_the_deprecated_load_and_return_shims_are_gone(client, driver):
    for url, body in (
        ("/api/v1/staff/bottles/load", {"bottles_loaded": 10}),
        ("/api/v1/staff/bottles/return-to-warehouse", {"bottles_returned_to_warehouse": 0}),
    ):
        assert client.post(url, json=body, headers=staff_headers(driver)).status_code == 404, url


# --------------------------------------------------------------------------- #
# The Load screen
# --------------------------------------------------------------------------- #


def test_the_load_screen_lists_counted_products_and_nothing_else_for_a_free_driver(
    client, db, driver, small, big, sample_category
):
    ten_litre(db, sample_category, counted=False)

    data = client.get(LOAD_SCREEN, headers=staff_headers(driver)).get_json()["data"]

    assert data == {
        "products": [{"product_id": small.id, "product_name": "10 Litr suv"}],
        "refusal": None,
        "open_session": None,
    }


def test_the_load_screen_refuses_a_co_driver_with_the_owners_lines(client, db, owner, co_driver, small):
    s = trip(db, owner, bottles=40, units={small.id: 12})
    member(db, s, co_driver)

    data = client.get(LOAD_SCREEN, headers=staff_headers(co_driver)).get_json()["data"]

    assert data["refusal"] == {
        "error_code": "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE",
        "owner_name": owner.full_name,
        "session_id": s.id,
        "bottles_on_board": 40,
        "units": [
            {
                "product_id": small.id,
                "product_name": "10 Litr suv",
                "loaded": 12,
                "delivered": 0,
                "transferred_in": 0,
                "transferred_out": 0,
                "on_board": 12,
                "transferable": 12,
                "free": 12,
                "returned": None,
                "discrepancy": None,
            }
        ],
    }
    assert data["open_session"] is None
    assert data["products"] == [{"product_id": small.id, "product_name": "10 Litr suv"}]


def test_the_load_screen_on_an_own_open_trip(client, db, driver, small):
    s = trip(db, driver, bottles=40, units={small.id: 12})

    data = client.get(LOAD_SCREEN, headers=staff_headers(driver)).get_json()["data"]

    refusal = data["refusal"]
    assert (refusal["error_code"], refusal["owner_name"], refusal["session_id"], refusal["bottles_on_board"]) == (
        "BOTTLE_SESSION_ALREADY_OPEN",
        "Dilshod",
        s.id,
        40,
    )
    assert refusal["units"] == [_line(small.id, loaded=12)]
    assert refusal["units"] == data["open_session"]["units"]
    assert (data["open_session"]["id"], data["open_session"]["bottles_loaded"]) == (s.id, 40)
