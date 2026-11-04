"""The interrupted-leg rule on Postgres (honest-ETA spec 2026-11-03 §4.8 S2).

`DeliveryEtaService` decides in SQL whether a tap's leg was interrupted (`_leg_uninterrupted`): Track's minutes and
headroom's legs both depend on it, and Postgres scopes and plans the correlated subqueries its own way. The SQLite
suite builds its schema from the models; these tests run the real readers on a migrated throwaway Postgres database
(`pg_db`), with the row builders of tests/unit/test_delivery_eta_service.py. That file's `TestThePostgresPlanShape`
guards the SQL shape the plans depend on.
"""

from datetime import timedelta

import pytest

from business_app.models.user import UserAddress
from business_app.services.delivery_eta_service import CustomerEta, DeliveryEtaService
from shared.enums import DeliveryStatus
from tests.integration.test_delivery_promise_pg_constraints import _user
from tests.unit.test_delivery_eta_service import (
    CLEAR_OF_THE_HOURLY_LEGS,
    DOOR_AT,
    NOW,
    SEED,
    SPEC_TUNABLES,
    TAP,
    THIRTY_LEGS,
    _moved,
    _order_on_its_way,
    _promise_of,
    _recorded_legs,
    _tap,
)

pytestmark = pytest.mark.integration

READING = TAP + timedelta(minutes=5)  # A's promise, by = TAP + 29, has 24 minutes left


@pytest.fixture
def spec_tunables(pg_app, monkeypatch):
    for name, value in SPEC_TUNABLES.items():
        monkeypatch.setitem(pg_app.config, name, value)


@pytest.fixture
def people(pg_db):
    """The customer, the stop's driver, another driver and an admin."""
    return {name: _user(pg_db, name) for name in ("Kamola", "Sardor", "Aziz", "Admin")}


@pytest.fixture
def stop_a(pg_db, people, spec_tunables):
    """Sardor's stop A, tapped in transit at TAP with Google's promise: by = TAP + 29."""
    stop = _order_on_its_way(pg_db, people["Kamola"], people["Sardor"], number="PG-A")
    _promise_of(pg_db, _tap(pg_db, stop, people["Sardor"]), window_end=TAP + timedelta(minutes=29))
    return stop


def test_the_same_driver_setting_off_elsewhere_ends_the_promise(pg_db, people, stop_a):
    assert DeliveryEtaService().customer_eta(stop_a.order, now=READING).minutes_left == 24  # stands until B's tap
    stop_b = _order_on_its_way(pg_db, people["Kamola"], people["Sardor"], number="PG-B")
    tap_b = _tap(pg_db, stop_b, people["Sardor"], at=TAP + timedelta(minutes=3))
    _promise_of(pg_db, tap_b, window_end=TAP + timedelta(minutes=32))

    assert DeliveryEtaService().customer_eta(stop_a.order, now=READING) == CustomerEta(stage="on_the_way")
    assert DeliveryEtaService().customer_eta(stop_b.order, now=READING).minutes_left == 27


def test_another_drivers_tap_leaves_the_promise(pg_db, people, stop_a):
    theirs = _order_on_its_way(pg_db, people["Kamola"], people["Aziz"], number="PG-THEIRS")
    _tap(pg_db, theirs, people["Aziz"], at=TAP + timedelta(minutes=3))

    assert DeliveryEtaService().customer_eta(stop_a.order, now=READING).minutes_left == 24


def test_an_earlier_tap_on_another_stop_leaves_the_promise(pg_db, people, stop_a):
    earlier = _order_on_its_way(pg_db, people["Kamola"], people["Sardor"], number="PG-EARLIER")
    _tap(pg_db, earlier, people["Sardor"], at=TAP - timedelta(minutes=10))

    assert DeliveryEtaService().customer_eta(stop_a.order, now=READING).minutes_left == 24


def test_a_hand_over_ends_the_promise(pg_db, people, stop_a):
    assert DeliveryEtaService().customer_eta(stop_a.order, now=READING).minutes_left == 24  # stands until the hand-over
    _moved(
        pg_db,
        stop_a,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.IN_TRANSIT,
        at=TAP + timedelta(minutes=1),
        by=people["Admin"],
    )

    assert DeliveryEtaService().customer_eta(stop_a.order, now=READING) == CustomerEta(stage="on_the_way")


@pytest.mark.parametrize(
    "other_stop_tapped_after, expected",
    [
        pytest.param(timedelta(minutes=3), SEED, id="interrupted"),
        pytest.param(timedelta(minutes=90), THIRTY_LEGS, id="tapped-after-the-arrival"),
        pytest.param(None, THIRTY_LEGS, id="clean"),
    ],
)
def test_an_interrupted_leg_does_not_teach_headroom(pg_db, people, spec_tunables, other_stop_tapped_after, expected):
    """29 real legs, and a 30th that arrived 83 minutes after its tap against Google's 18 (r = 4.33). Clean, it is
    learned from (1.12). Its driver's tap on another stop 3 minutes in interrupts it, and the seed stands; the same
    tap 90 minutes in, after the arrival, does not.
    """
    customer, driver = people["Kamola"], people["Sardor"]
    latitude, longitude = DOOR_AT
    home = UserAddress(
        user_id=customer.id, title="Home", full_address="1 Test St", latitude=latitude, longitude=longitude
    )
    pg_db.session.add(home)
    pg_db.session.commit()
    _recorded_legs(pg_db, customer, driver, 29, address=home)
    [tap] = _recorded_legs(
        pg_db,
        customer,
        driver,
        1,
        start=29,
        drive_minutes=83.0,
        recorded_days_ago=CLEAR_OF_THE_HOURLY_LEGS,
        address=home,
    )
    if other_stop_tapped_after is not None:
        other_stop = _order_on_its_way(pg_db, customer, driver, number="PG-OTHER")
        _tap(pg_db, other_stop, driver, at=tap.changed_at + other_stop_tapped_after)

    assert DeliveryEtaService().headroom(now=NOW) == pytest.approx(expected)
