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


# --------------------------------------------------------------------------- delivery days (spec 2026-10-27 §6.4)


def _choice(client, **query):
    response = client.get(URL, query_string=query)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_the_chooser_publishes_the_delivery_days(client, db, slots):
    """The one place delivery days are published: the weekdays an active slot offers. The bot's and the website's
    day pickers offer only these."""
    slots["morning"].is_active = False  # weekdays [0-4] and saturday [5] stay active
    db.session.commit()

    anyday = _choice(client, weekday="any")
    sunday = _choice(client, weekday="6")

    assert anyday["delivery_weekdays"] == [0, 1, 2, 3, 4, 5]
    assert sunday == {"weekday": "6", "time_slots": [], "delivery_weekdays": [0, 1, 2, 3, 4, 5]}


def test_no_active_slot_publishes_every_day(client, db):
    assert _choice(client, weekday="any") == {"weekday": "any", "time_slots": [], "delivery_weekdays": list(range(7))}

    db.session.add(DeliveryTimeSlot(name="Off", start_time="10:00", end_time="12:00", is_active=False,
                                    available_days=[0]))
    db.session.commit()

    assert _choice(client, weekday="any", frequency="daily")["delivery_weekdays"] == list(range(7))


def test_monthly_offers_the_slots_that_cover_every_delivery_day(client, db, slots):
    """A monthly date can land on any delivery day, so its slot must be offered on every delivery day; no longer on
    every day of the week."""
    slots["morning"].is_active = False
    mon_to_sat = DeliveryTimeSlot(name="Mon-Sat", start_time="12:00", end_time="14:00", is_active=True,
                                  available_days=[0, 1, 2, 3, 4, 5])
    db.session.add(mon_to_sat)
    db.session.commit()

    monthly = _choice(client, weekday="any", frequency="monthly")

    assert [slot["id"] for slot in monthly["time_slots"]] == [mon_to_sat.id]
    assert monthly["delivery_weekdays"] == [0, 1, 2, 3, 4, 5]
