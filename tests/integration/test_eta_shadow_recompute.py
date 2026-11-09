"""One recompute of the capacity-aware ETA plan, end to end (honest-ETA Phase 2, Task 6).

`EtaEngine().recompute(now)` reads supply, demand and the fitted parameters, runs the planner and records the hidden S1
promises (`engine_s1_v1`, stage `waiting`, never shown). Each test drives the routes the clients call: a driver loads
bottles in the staff bot (`POST /api/v1/staff/bottles/session/open`), a returning customer orders water with cash
(`POST /api/v1/orders/`, confirmed at once), a dispatcher hands a stop to a driver on the dispatch map, a driver
accepts and taps through the bot's own routes. Then `rebase_clock(to)` moves what the real clock stamped to `to`, 11:00
yesterday in Tashkent, and the engine runs with `now = to`; a second phase uses `to + 1 min`. So no test depends on
the time of day.

Faked, as external I/O only: Celery publishing (the suite's no-op, and the one recompute seam bound to a recorder) and
Redis (`eta_redis`), because the suite's lanes share the real Redis DBs. The isolation test reads Track on a pinned
clock, so its countdown does not depend on how fast the test runs.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List

import pytest
from sqlalchemy import select

from business_app import db as _db
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.delivery_promise import DeliveryPromise
from business_app.services import order_service
from business_app.services.delivery_eta_service import DeliveryEtaService
from business_app.services.eta_plan import engine
from business_app.services.eta_plan.engine import EtaEngine
from business_app.services.eta_plan.recorder import latest_promises, record
from business_app.services.eta_plan.supply import cars_for_day
from business_app.utils.local_windows import local_date, work_day_of
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus
from shared.redis_keyspace import RedisKeyspace
from staff_bot.api_client import StaffAPIClient
from tests.integration.contract_pricing_factory import customer_headers as bearer
from tests.integration.eta_plan_builders import (  # noqa: F401 -- the fixtures
    dated_order,
    eleven_yesterday,
    engine_on_fixture,
    eta_redis_fixture,
    make_driver,
    order_at_km,
    orders_water,
    rebase_clock_fixture,
    recompute_requests_fixture,
    returning_customer,
    shown_legs,
    shown_promise,
    tap,
)
from tests.integration.reorder_reminder_builders import captured_logs

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("engine_on", "eta_redis", "recompute_requests")]

OPEN_SESSION = "/api/v1/staff/bottles/session/open"
ACCEPT = "/api/v1/staff/delivery/accept/{}"
STATUS = "/api/v1/staff/delivery/{}/status"
ASSIGN = "/api/v1/admin/dispatch/stops/{}/assign"
TRACK = "/api/v1/orders/{}/track"
ENGINE_MODEL = "engine_s1_v1"


def _ok(response, status=200):
    assert response.status_code == status, response.get_data(as_text=True)
    return response.get_json()["data"]


def _loads_bottles(app, driver, bottles=30) -> None:
    """The driver loads `bottles` at the depot in the staff bot: an open session, the car's load today. A fresh client
    each call: the session-scoped one shares a cookie jar."""
    response = app.test_client().post(OPEN_SESSION, json={"bottles_loaded": bottles}, headers=bearer(app, driver))
    _ok(response, 201)


def _a_car_and_a_confirmed_order(app, db, customer, address, product):
    """The scene most tests start from: a driver who has loaded bottles, and a returning customer's confirmed cash
    order in the pool."""
    driver = make_driver(db)
    _loads_bottles(app, driver)
    returning_customer(db, customer, address)
    return driver, orders_water(app, db, customer, address, product)


def _engine_rows() -> List[DeliveryPromise]:
    return DeliveryPromise.query.filter_by(model_version=ENGINE_MODEL).order_by(DeliveryPromise.id).all()


def _latest_window_ends(orders) -> List[datetime]:
    latest = latest_promises(order.delivery.id for order in orders)
    return [ensure_utc(latest[order.delivery.id].window_end) for order in orders]


def test_a_confirmed_order_gets_a_hidden_promise(
    app, db, rebase_clock, recompute_requests, caplog, sample_user, user_address, sample_product
):
    """A returning customer's cash order, confirmed at once and waiting in the pool while a driver has loaded, gets
    one hidden promise for today, with a window. The run says what it did, in its summary and its log, and a run that
    took the lock queues no other."""
    to = eleven_yesterday()
    _driver, order = _a_car_and_a_confirmed_order(app, db, sample_user, user_address, sample_product)
    rebase_clock(to)
    queued_by_the_scene = list(recompute_requests)  # the scene's commits requested this run (the trigger, Task 7)

    with captured_logs(caplog, engine.logger, logging.INFO):
        summary = EtaEngine().recompute(now=to)

    assert summary == {"stops": 1, "recorded": 1, "cars": 1, "orphans": 0, "params_source": "seed"}
    assert DeliveryPromise.query.count() == 1
    (row,) = _engine_rows()
    assert row.delivery_id == order.delivery.id
    assert (row.stage, row.shown, row.delivery_status_history_id, row.day_kind) == ("waiting", False, None, "today")
    assert ensure_utc(row.window_end) > ensure_utc(row.window_start) >= to
    assert (ensure_utc(row.computed_at), row.promised_date, row.drivers_working) == (to, local_date(to), 1)
    assert [line.getMessage() for line in caplog.records if line.getMessage().startswith("eta_plan_recomputed")]
    assert recompute_requests == queued_by_the_scene


def test_drivers_working_counts_only_cars_that_work(app, db, rebase_clock):
    """Spec §4.2: before anyone works, the plan uses the drivers auto-assign would pick, but none of them is working.
    Two rostered, available drivers with no session and no delivery stand in for the morning's cars, and the promise
    records 0 drivers working. Once one loads bottles in the staff bot, a new order's promise records 1."""
    to = eleven_yesterday()
    first, _second = make_driver(db), make_driver(db)
    waiting = order_at_km(db, 3, created_at=to - timedelta(minutes=30))
    rebase_clock(to)

    assert EtaEngine().recompute(now=to)["cars"] == 2  # the early-morning set plans the stop
    (before,) = _engine_rows()
    assert (before.delivery_id, before.drivers_working) == (waiting.delivery.id, 0)

    _loads_bottles(app, first)
    newer = order_at_km(db, 5, created_at=to)
    later = to + timedelta(minutes=1)
    rebase_clock(later)
    EtaEngine().recompute(now=later)

    latest = latest_promises([newer.delivery.id])[newer.delivery.id]
    assert (ensure_utc(latest.computed_at), latest.drivers_working) == (later, 1)


@pytest.mark.parametrize("works_by", ["an-open-session", "a-held-delivery"])
def test_a_second_working_driver_shortens_the_recorded_windows(app, db, rebase_clock, admin_claim_headers, works_by):
    """Spec §7: a driver works with "an open session or a held delivery". Five orders wait in the pool, due north of
    the depot 3, 5, 7, 9 and 11 km out, for one car that has loaded but not set off. Then a second driver loads bottles,
    or the dispatcher hands them a sixth order on the dispatch map, 10 km out, beside the 9 and 11 km ones (the map's
    assign needs no session; the bot's accept does). With two cars no promised window ends later, and some end
    earlier: the second car takes stops off the first, in parallel or on its way to its own stop."""
    to = eleven_yesterday()
    first, second = make_driver(db), make_driver(db)
    _loads_bottles(app, first)
    pool = [order_at_km(db, km, created_at=to - timedelta(minutes=60 - n)) for n, km in enumerate((3, 5, 7, 9, 11))]
    rebase_clock(to)
    assert EtaEngine().recompute(now=to)["cars"] == 1
    before = _latest_window_ends(pool)

    if works_by == "an-open-session":
        _loads_bottles(app, second)
    else:
        sixth = order_at_km(db, 10, created_at=to)
        _ok(
            app.test_client().post(
                ASSIGN.format(sixth.delivery.id), json={"driver_id": second.id}, headers=admin_claim_headers
            )
        )
    later = to + timedelta(minutes=1)
    rebase_clock(later)
    summary = EtaEngine().recompute(now=later)

    assert summary["cars"] == 2
    assert {car.driver_id for car in cars_for_day(work_day_of(later), later)} == {first.id, second.id}
    after = _latest_window_ends(pool)
    assert all(new <= old for new, old in zip(after, before)), (before, after)
    assert sum(new < old for new, old in zip(after, before)) >= 2, (before, after)


def test_engine_rows_never_change_track_or_headroom(
    app, db, rebase_clock, monkeypatch, sample_user, user_address, sample_product
):
    """Engine rows are hidden: they never reach the customer's Track, nor the S2 learning. A colleague's 30 finished
    trips teach headroom (it is learned, not the seed). The driver accepts the customer's order in the bot, and the
    engine records its S1 row while it is assigned. The driver sets off through the bot's routes, and that tap's shown
    Google promise gives Track its minutes. Track and the learned headroom read the same with the engine's rows and
    without them."""
    to = eleven_yesterday()
    shown_legs(db, make_driver(db), 30, before=to)
    driver, order = _a_car_and_a_confirmed_order(app, db, sample_user, user_address, sample_product)
    _ok(app.test_client().post(ACCEPT.format(order.delivery.id), headers=bearer(app, driver)))
    rebase_clock(to)
    EtaEngine().recompute(now=to)
    assert [(row.delivery_id, row.stage) for row in _engine_rows()] == [(order.delivery.id, "waiting")]

    for status in ("picked_up", "in_transit"):
        _ok(
            app.test_client().put(
                STATUS.format(order.delivery.id),
                json=StaffAPIClient.delivery_status_body(status),
                headers=bearer(app, driver),
            )
        )
    rebase_clock(to + timedelta(minutes=1))
    on_the_way = DeliveryStatusHistory.query.filter_by(
        delivery_id=order.delivery.id, new_status=DeliveryStatus.IN_TRANSIT
    ).one()
    tapped_at = ensure_utc(on_the_way.changed_at)
    shown_promise(db, on_the_way, window_end=tapped_at + timedelta(minutes=29))
    reading = tapped_at + timedelta(minutes=2)
    monkeypatch.setattr(order_service, "get_utc_now", lambda: reading)

    def what_is_read() -> Dict:
        track = _ok(app.test_client().get(TRACK.format(order.id), headers=bearer(app, sample_user)))
        return {"track": track, "headroom": DeliveryEtaService().headroom(now=reading)}

    with_engine_rows = what_is_read()
    DeliveryPromise.query.filter_by(model_version=ENGINE_MODEL).delete()
    db.session.commit()

    assert what_is_read() == with_engine_rows
    assert (with_engine_rows["track"]["eta_stage"], with_engine_rows["track"]["eta_minutes_left"]) == ("on_the_way", 27)
    assert with_engine_rows["headroom"] != pytest.approx(app.config["ETA_HEADROOM_SEED"])


def _deliveries():
    """Every column of every `deliveries` row, as the database holds it."""
    table = Delivery.__table__
    return _db.session.execute(select(table).order_by(table.c.id)).all()


def test_the_recompute_writes_no_delivery_columns(app, db, rebase_clock, sample_user, user_address, sample_product):
    """Shadow only: the run reads every kind of stop and records promises, and writes nothing on `deliveries`. The
    scene has a pool order (posted), stops the driver holds (assigned, picked up, in transit) and a dated order
    parked for its day two days out."""
    to = eleven_yesterday()
    driver, _order = _a_car_and_a_confirmed_order(app, db, sample_user, user_address, sample_product)
    assigned, picked_up, in_transit = (
        order_at_km(db, km, created_at=to - timedelta(hours=2), driver=driver) for km in (2, 4, 6)
    )
    tap(db, picked_up.delivery, DeliveryStatus.PICKED_UP, to - timedelta(minutes=40))
    tap(db, in_transit.delivery, DeliveryStatus.PICKED_UP, to - timedelta(minutes=30))
    tap(db, in_transit.delivery, DeliveryStatus.IN_TRANSIT, to - timedelta(minutes=10))
    dated_order(db, local_date(to) + timedelta(days=2), created_at=to - timedelta(days=1), parked=True)
    rebase_clock(to)
    before = _deliveries()

    summary = EtaEngine().recompute(now=to)

    assert summary["recorded"] == 4  # the posted, assigned, picked-up and dated rows; in transit is S2's
    assert _deliveries() == before


def test_two_recomputes_never_interleave(
    app, db, rebase_clock, eta_redis, recompute_requests, monkeypatch, sample_user, user_address, sample_product
):
    """One run at a time. A run clears the "recompute queued" marker as it starts, so a commit during it queues the
    next. A second run that starts while the first holds the lock does no work: it queues another for when the lock
    runs out, and returns. And a run that outlives its lock, which then passes to another run, never frees that other
    run's lock."""
    to = eleven_yesterday()
    _a_car_and_a_confirmed_order(app, db, sample_user, user_address, sample_product)
    rebase_clock(to)
    lock, marker = RedisKeyspace.eta_recompute_lock(), RedisKeyspace.eta_recompute_scheduled()
    assert eta_redis.get(marker) is not None  # the scene's commits requested this run (the trigger, Task 7)
    queued_by_the_scene = list(recompute_requests)
    inside = {}

    def record_while_others_try(planned, params, now, drivers_working):
        inside["marker"] = eta_redis.get(marker)
        eta_redis.expiries[lock] = 97  # some of the lock's seconds have passed: 97 are left
        inside["second run"] = EtaEngine().recompute(now=now)
        inside["rows after the second run"] = len(_engine_rows())
        eta_redis.store[lock] = b"a-third-run"  # this run outlives its lock, and another run takes it
        return record(planned, params, now, drivers_working)

    monkeypatch.setattr(engine, "record", record_while_others_try)

    first = EtaEngine().recompute(now=to)

    assert inside == {"marker": None, "second run": {"skipped": "locked"}, "rows after the second run": 0}
    assert recompute_requests == queued_by_the_scene + [97]  # the second run queued another for when the lock runs out
    assert eta_redis.ttl(marker) == app.config["ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS"]
    assert first["recorded"] == 1 and len(_engine_rows()) == 1
    assert eta_redis.get(lock) == b"a-third-run"
