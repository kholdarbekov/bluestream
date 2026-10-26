"""The slots a subscription's day-and-slot chooser offers (plan 2026-10-24 Task 4; spec 2026-10-23 §2.9).

A recurring delivery has no date yet, so the bot asks `GET /api/v1/delivery/time-slots?weekday=<0-6|any>`:
the active slots offered that weekday (0 = Monday), or every active slot. The per-date query the web
checkout uses (`?date=`) is unchanged. Driven through the real endpoint with the exact query the bot sends.
"""

import pytest

from business_app.models.delivery import DeliveryTimeSlot

pytestmark = pytest.mark.integration

URL = "/api/v1/delivery/time-slots"


@pytest.fixture
def slots(db):
    rows = {
        "morning": DeliveryTimeSlot(
            name="Morning", start_time="10:00", end_time="12:00", is_active=True, available_days=[0, 1, 2, 3, 4, 5, 6]
        ),
        "weekdays": DeliveryTimeSlot(
            name="Weekday evening", start_time="18:00", end_time="20:00", is_active=True, available_days=[0, 1, 2, 3, 4]
        ),
        "saturday": DeliveryTimeSlot(
            name="Saturday only", start_time="09:00", end_time="11:00", is_active=True, available_days=[5]
        ),
        "retired": DeliveryTimeSlot(
            name="Retired", start_time="07:00", end_time="09:00", is_active=False, available_days=[0, 1, 2, 3, 4, 5, 6]
        ),
    }
    db.session.add_all(rows.values())
    db.session.commit()
    return rows


def _ids(response):
    assert response.status_code == 200, response.get_json()
    return [slot["id"] for slot in response.get_json()["time_slots"]]


def test_a_weekday_lists_only_the_active_slots_offered_that_day(client, slots):
    thursday = _ids(client.get(URL, query_string={"weekday": "3"}))
    saturday = _ids(client.get(URL, query_string={"weekday": "5"}))

    assert thursday == [slots["morning"].id, slots["weekdays"].id]
    assert saturday == [slots["saturday"].id, slots["morning"].id]  # ordered by start time


def test_sunday_is_weekday_six_not_zero(client, slots):
    assert _ids(client.get(URL, query_string={"weekday": "6"})) == [slots["morning"].id]
    assert slots["weekdays"].id in _ids(client.get(URL, query_string={"weekday": "0"}))


def test_any_lists_every_active_slot_and_never_a_retired_one(client, slots):
    ids = _ids(client.get(URL, query_string={"weekday": "any"}))

    assert ids == [slots["saturday"].id, slots["morning"].id, slots["weekdays"].id]
    assert slots["retired"].id not in ids


def test_a_slot_row_carries_what_the_chooser_prints(client, slots):
    body = client.get(URL, query_string={"weekday": "5"}).get_json()

    assert body["time_slots"][0] == {
        "id": slots["saturday"].id,
        "name": "Saturday only",
        "start_time": "09:00",
        "end_time": "11:00",
        "time_range": "09:00-11:00",
        "available_days": [5],
    }


@pytest.mark.parametrize("bad", ["7", "-1", "monday", ""])
def test_a_weekday_that_is_not_a_weekday_is_a_400(client, slots, bad):
    response = client.get(URL, query_string={"weekday": bad})

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_the_per_date_query_the_web_checkout_uses_is_unchanged(client, slots):
    # 2999-01-04 is a Friday: the morning and the weekday-evening slot are offered, with capacity counts.
    body = client.get(URL, query_string={"date": "2999-01-04"}).get_json()

    assert sorted(slot["id"] for slot in body["time_slots"]) == sorted([slots["morning"].id, slots["weekdays"].id])
    assert all("available_capacity" in slot for slot in body["time_slots"])
    assert client.get(URL).status_code == 400
