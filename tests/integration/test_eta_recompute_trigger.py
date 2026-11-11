"""The trigger of the capacity-aware ETA plan: one detector, one drain (honest-ETA Phase 2, Task 7; R15).

A write that changes what the plan reads asks for one recompute once it has committed. The detector, a session-level
`before_flush` listener, reads the attribute history of what each flush writes and flags the transaction when it
touches `PLAN_INPUTS`. The drain, an `after_commit` listener, then calls `request_recompute()` once for that
transaction, and its debounce queues one run however many commits a burst holds. Nothing is requested for a rollback,
before the outer commit of a SAVEPOINT, for the engine's own writes or a driver's GPS ping, nor at all with the kill
switch off, and an eager Celery app publishes nothing (the beat recomputes).

Each "requests a recompute" test drives the route or task a client or the beat calls, with the body it sends: a
returning customer's order, confirmed at once; a driver's accept and status tap in the staff bot; the dispatch map's
reassign; the customer's cancel; an admin's reschedule, address change, order edit, driver edit and account status;
the release sweep; and a driver's session open, bottle hand-off and close. The scene is built with the kill switch off,
as the tests' config has it, and the switch goes on just before the write under test, so what is recorded is what that
write requested.

Faked, as external I/O only: the publish (the one seam, `_publish_recompute`, bound to a recorder; one test drives the
real seam down to `Task.apply_async` instead) and Redis (`eta_redis`), because the suite's lanes share the real Redis
DBs.
"""

import inspect
import logging
import sys
from contextlib import contextmanager
from datetime import timedelta
from typing import Dict, Iterator, List
from unittest import mock

import pytest
from celery import _state as celery_state
from sqlalchemy import event

from business_app import db as _db
from business_app.models.order import OrderItem
from business_app.models.user import UserAddress
from business_app.services.eta_plan.engine import EtaEngine
from business_app.tasks.delivery_eta_tasks import recompute_eta_plan
from business_app.tasks.order_tasks import release_due_scheduled_orders
from business_app.utils import eta_recompute_dispatch
from business_app.utils.local_windows import local_date
from business_app.utils.transactions import atomic_transaction
from shared.enums import PaymentMethod, UserStatus
from shared.redis_keyspace import RedisKeyspace
from staff_bot.api_client import StaffAPIClient
from tests.integration.contract_pricing_factory import customer_headers as bearer
from tests.integration.eta_plan_builders import (  # noqa: F401 -- the fixtures
    add_fit,
    dated_order,
    eleven_yesterday,
    eta_redis_fixture,
    make_driver,
    open_session,
    order_at_km,
    orders_water,
    person_of,
    point_at_km,
    published_fixture,
    recompute_requests_fixture,
    returning_customer,
    store_route,
)
from tests.integration.reorder_reminder_builders import captured_logs

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("eta_redis")]

ORDER_CANCEL = "/api/v1/orders/{}/cancel"
MOVE_PIN = "/api/v1/auth/addresses/{}"
ACCEPT = "/api/v1/staff/delivery/accept/{}"
STATUS = "/api/v1/staff/delivery/{}/status"
LOCATION_PING = "/api/v1/staff/delivery/me/location"
OPEN_SESSION = "/api/v1/staff/bottles/session/open"
CLOSE_SESSION = "/api/v1/staff/bottles/session/close"
SEND_BOTTLES = "/api/v1/staff/bottles/transfers"
CONFIRM_BOTTLES = "/api/v1/staff/bottles/transfers/{}/confirm"
ASSIGN = "/api/v1/admin/dispatch/stops/{}/assign"
RESCHEDULE = "/api/v1/admin/orders/{}/schedule"
MOVE_ORDER = "/api/v1/admin/orders/{}/delivery-address"
EDIT_ORDER = "/api/v1/admin/orders/{}/edit"
BULK_ACTIONS = "/api/v1/admin/bulk-actions"
EDIT_DRIVER = "/api/v1/admin/staff/delivery-persons/{}"
USER_STATUS = "/api/v1/admin/users/{}/status"


def _ok(response, status=200):
    assert response.status_code == status, response.get_data(as_text=True)
    return response.get_json()["data"]


def _trigger_on(app, monkeypatch) -> None:
    """The kill switch ETA_SHADOW_ENGINE_ENABLED on for the rest of the test (the tests' config has it off): the scene
    built before this requested nothing, so what is recorded from here is what the write under test requested."""
    monkeypatch.setitem(app.config, "ETA_SHADOW_ENGINE_ENABLED", True)


def _one_run(app) -> List[int]:
    """What a write that touched the plan publishes: one run, ETA_RECOMPUTE_COUNTDOWN_SECONDS out."""
    return [app.config["ETA_RECOMPUTE_COUNTDOWN_SECONDS"]]


def _sql_inside_the_detector(monkeypatch) -> List[List[str]]:
    """The SQL statements each call of the detector's body (`_touches_the_plan`) ran, one list per call, in order. The
    real body runs; the statements are read off the engine while it does. The detector must run none."""
    real_detector = eta_recompute_dispatch._touches_the_plan
    calls: List[List[str]] = []

    def watched_detector(session):
        statements: List[str] = []

        def on_statement(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        engine = _db.engine
        event.listen(engine, "before_cursor_execute", on_statement)
        try:
            return real_detector(session)
        finally:
            event.remove(engine, "before_cursor_execute", on_statement)
            calls.append(statements)

    monkeypatch.setattr(eta_recompute_dispatch, "_touches_the_plan", watched_detector)
    return calls


def _trigger_failures(caplog) -> List[str]:
    """What the trigger logged at WARNING or above: a detector or a drain that failed. Both swallow their own errors,
    so without this a test that expects no request could not tell "nothing changed" from "the detector crashed"."""
    return [
        line.getMessage()
        for line in caplog.records
        if line.name == eta_recompute_dispatch.logger.name and line.levelno >= logging.WARNING
    ]


def _edit_form(person) -> Dict:
    """The body the admin Delivery persons page saves (`DeliveryPersons.js`, `handleEditorSubmit`): every field of its
    edit form, filled from the row as `openEditModal` fills it."""
    return {
        "full_name": person.full_name,
        "phone": person.phone,
        "email": person.email,
        "vehicle_type": person.vehicle_type,
        "vehicle_number": person.vehicle_number,
        "max_concurrent_deliveries": person.max_concurrent_deliveries or 3,
        "working_hours_start": person.working_hours_start or "09:00",
        "working_hours_end": person.working_hours_end or "18:00",
        "is_active": bool(person.is_active),
        "is_available": bool(person.is_available),
    }


# ---------------------------------------------------------------------------------------------------------------------
# One recompute after the commit of each write that changes the plan.
# ---------------------------------------------------------------------------------------------------------------------


def test_confirming_an_order_requests_a_recompute(
    app, db, monkeypatch, recompute_requests, sample_user, user_address, sample_product
):
    """A returning customer's cash order, placed with the body the bot and the website send, is confirmed at once and
    waits in the drivers' pool: one recompute."""
    returning_customer(db, sample_user, user_address)
    _trigger_on(app, monkeypatch)

    orders_water(app, db, sample_user, user_address, sample_product)

    assert recompute_requests == _one_run(app)


def test_a_driver_accept_requests_a_recompute(app, db, monkeypatch, recompute_requests):
    """A driver who has loaded bottles accepts a pool order in the staff bot (the accept needs an open session for a
    bottle order): one recompute."""
    to = eleven_yesterday()
    driver = make_driver(db)
    open_session(db, driver, to)
    order = order_at_km(db, 3.0, created_at=to)
    _trigger_on(app, monkeypatch)

    _ok(app.test_client().post(ACCEPT.format(order.delivery.id), headers=bearer(app, driver)))

    assert recompute_requests == _one_run(app)


def test_an_admin_reassign_requests_a_recompute(app, db, monkeypatch, recompute_requests, admin_claim_headers):
    """A dispatcher moves a stop from one driver to another on the dispatch map: one recompute."""
    to = eleven_yesterday()
    first, second = make_driver(db), make_driver(db)
    order = order_at_km(db, 3.0, created_at=to, driver=first)
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().post(
            ASSIGN.format(order.delivery.id), json={"driver_id": second.id}, headers=admin_claim_headers
        )
    )

    assert recompute_requests == _one_run(app)


def test_a_status_tap_requests_a_recompute(app, db, monkeypatch, recompute_requests):
    """The driver taps "Picked up" in the staff bot on a stop they accepted: one recompute."""
    to = eleven_yesterday()
    driver = make_driver(db)
    open_session(db, driver, to)
    order = order_at_km(db, 3.0, created_at=to)
    _ok(app.test_client().post(ACCEPT.format(order.delivery.id), headers=bearer(app, driver)))
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().put(
            STATUS.format(order.delivery.id),
            json=StaffAPIClient.delivery_status_body("picked_up"),
            headers=bearer(app, driver),
        )
    )

    assert recompute_requests == _one_run(app)


def test_a_customer_cancel_requests_a_recompute(app, db, monkeypatch, recompute_requests):
    """The customer cancels their order in the pool, with the `via` their screen showed: one recompute."""
    order = order_at_km(db, 3.0, created_at=eleven_yesterday())
    customer = order.user
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().post(
            ORDER_CANCEL.format(order.id), json={"expected_via": "cancel"}, headers=bearer(app, customer)
        )
    )

    assert recompute_requests == _one_run(app)


def test_a_reschedule_requests_a_recompute(app, db, monkeypatch, recompute_requests, admin_claim_headers):
    """An admin moves an order waiting in the pool to the day after tomorrow, with the body the Reschedule modal sends:
    its delivery is held for that day, and the plan is recomputed once."""
    order = order_at_km(db, 3.0, created_at=eleven_yesterday())
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().patch(
            RESCHEDULE.format(order.id),
            json={
                "delivery_date": (local_date() + timedelta(days=2)).isoformat(),
                "delivery_window_start": None,
                "delivery_window_end": None,
                "reason": "The customer asked for another day",
            },
            headers=admin_claim_headers,
        )
    )

    assert recompute_requests == _one_run(app)


@pytest.mark.parametrize("change", ["the-admin-moves-the-order", "the-customer-moves-the-pin"])
def test_an_address_change_requests_a_recompute(app, db, monkeypatch, recompute_requests, admin_claim_headers, change):
    """The stop's door moves: an admin moves the order to another saved address of the customer, or the customer moves
    the pin of the address the order goes to (the bot's and the website's address edit). One recompute either way.
    The admin's change sets the order's `delivery_address` relationship, and the detector reads that relationship's
    history with no SQL at all."""
    order = order_at_km(db, 3.0, created_at=eleven_yesterday())
    customer, address_id = order.user, order.delivery_address_id
    latitude, longitude = point_at_km(5.0)
    detector_sql = _sql_inside_the_detector(monkeypatch)
    if change == "the-admin-moves-the-order":
        office = UserAddress(
            user_id=customer.id,
            title="office",
            full_address="5 North St, Tashkent",
            latitude=latitude,
            longitude=longitude,
        )
        db.session.add(office)
        db.session.commit()
        _trigger_on(app, monkeypatch)
        _ok(
            app.test_client().patch(
                MOVE_ORDER.format(order.id), json={"address_id": office.id}, headers=admin_claim_headers
            )
        )
    else:
        _trigger_on(app, monkeypatch)
        _ok(
            app.test_client().put(
                MOVE_PIN.format(address_id),
                json={"latitude": latitude, "longitude": longitude},
                headers=bearer(app, customer),
            )
        )

    assert recompute_requests == _one_run(app)
    assert detector_sql, "the detector looked at the write"
    assert all(statements == [] for statements in detector_sql), detector_sql


def test_an_admin_order_edit_that_removes_a_line_requests_a_recompute(
    app, db, monkeypatch, recompute_requests, admin_claim_headers, sample_product
):
    """An admin removes one line of a confirmed cash order waiting in the pool, with the body the Orders page's edit
    sends: the kept line as the form holds it, and the dropped one at quantity 0. The edit takes the line out of the
    order's collection, and delete-orphan deletes the row only inside the flush, after the detector has looked, so the
    line shows as an item that left its order. One recompute."""
    order = order_at_km(db, 3.0, created_at=eleven_yesterday(), bottles=2)
    order.payment_method = PaymentMethod.CASH
    dropped = OrderItem(
        order_id=order.id,
        product_id=sample_product.id,
        quantity=1,
        unit_price=sample_product.base_price,
        total_price=sample_product.base_price,
    )
    db.session.add(dropped)
    db.session.commit()
    (kept,) = [line for line in order.order_items if line.id != dropped.id]
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().post(
            EDIT_ORDER.format(order.id),
            json={
                "items": [
                    {"orderItemId": kept.id, "productId": kept.product_id, "quantity": kept.quantity},
                    {"orderItemId": dropped.id, "productId": dropped.product_id, "quantity": 0},
                ],
                "reason": "The customer no longer wants it",
            },
            headers=admin_claim_headers,
        )
    )

    assert [line.id for line in OrderItem.query.filter_by(order_id=order.id)] == [kept.id]
    assert recompute_requests == _one_run(app)


def test_a_release_sweep_requests_a_recompute(app, db, monkeypatch, recompute_requests):
    """The release sweep hands a dated order to the drivers once its day has come: its delivery row is created, and
    the plan is recomputed once. The order is dated yesterday, as after a missed tick, so it is due whatever the time
    of day."""
    to = eleven_yesterday()
    dated_order(db, local_date(to), created_at=to - timedelta(days=1))
    _trigger_on(app, monkeypatch)

    summary = release_due_scheduled_orders()

    assert summary == {"released": 1, "failed": 0, "awaiting": 0}
    assert recompute_requests == _one_run(app)


def test_opening_closing_and_transferring_a_session_request_a_recompute(
    app, db, monkeypatch, eta_redis, recompute_requests
):
    """A driver's day in the staff bot: they load bottles (a session opens), hand five to a colleague, who counts them
    in (the colleague's session opens with the hand-off), and return the rest (the session closes). Each step requests
    its own recompute. Each is taken after the run the step before queued has started, which clears the queued marker
    as it starts, so each step's request shows on its own."""
    driver, colleague = make_driver(db), make_driver(db)
    _trigger_on(app, monkeypatch)

    def the_queued_run_starts() -> None:
        eta_redis.delete(RedisKeyspace.eta_recompute_scheduled())
        recompute_requests.clear()

    _ok(app.test_client().post(OPEN_SESSION, json={"bottles_loaded": 30}, headers=bearer(app, driver)), 201)
    assert recompute_requests == _one_run(app)

    the_queued_run_starts()
    sent = _ok(
        app.test_client().post(
            SEND_BOTTLES, json={"receiver_driver_id": colleague.id, "quantity": 5}, headers=bearer(app, driver)
        ),
        201,
    )
    assert recompute_requests == _one_run(app)

    the_queued_run_starts()
    _ok(
        app.test_client().post(
            CONFIRM_BOTTLES.format(sent["id"]), json={"confirmed_quantity": 5}, headers=bearer(app, colleague)
        )
    )
    assert recompute_requests == _one_run(app)

    the_queued_run_starts()
    _ok(app.test_client().post(CLOSE_SESSION, json={"bottles_returned_to_warehouse": 25}, headers=bearer(app, driver)))
    assert recompute_requests == _one_run(app)


@pytest.mark.parametrize("counter", ["empties_transferred_in", "empties_transferred_out"])
def test_an_empties_counter_change_on_a_session_requests_a_recompute(app, db, monkeypatch, recompute_requests, counter):
    """The plan reads every bottle counter of a session, the two empties counters included: moving empties between
    drivers changes what a truck can carry, so the write requests one run."""
    to = eleven_yesterday()
    driver = make_driver(db)
    session = open_session(db, driver, to - timedelta(hours=1))
    _trigger_on(app, monkeypatch)

    setattr(session, counter, 3)
    db.session.commit()

    assert recompute_requests == _one_run(app)


@pytest.mark.parametrize(
    ("change", "requests_one"),
    [
        ({"is_active": False}, True),
        ({"is_available": False}, True),
        ({"working_hours_start": "10:00"}, True),
        ({"working_hours_end": "20:00"}, True),
        ({"vehicle_number": "01A777AA"}, False),
    ],
    ids=["deactivated", "made-unavailable", "later-start", "later-end", "only-the-vehicle"],
)
def test_a_driver_flag_or_hours_change_requests_a_recompute(
    app, db, monkeypatch, recompute_requests, admin_claim_headers, caplog, change, requests_one
):
    """An admin edits a driver on the Delivery persons page, whose form sends every field on each save. A change to
    whether the driver works (`is_active`, `is_available`) or to their roster requests one recompute. A save that
    changes nothing the plan reads requests none, though it sends every flag and hour again as they were, and not
    because the detector failed: it logged nothing."""
    person = person_of(make_driver(db))
    _trigger_on(app, monkeypatch)

    with captured_logs(caplog, eta_recompute_dispatch.logger, logging.WARNING):
        _ok(
            app.test_client().put(
                EDIT_DRIVER.format(person.id), json={**_edit_form(person), **change}, headers=admin_claim_headers
            )
        )

    assert recompute_requests == (_one_run(app) if requests_one else [])
    assert _trigger_failures(caplog) == []


def test_a_user_deactivation_requests_a_recompute(app, db, monkeypatch, recompute_requests, admin_claim_headers):
    """An admin deactivates a driver's account on the Users page. A driver whose account is not active is no car (R1),
    so the plan is recomputed once."""
    driver = make_driver(db)
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().put(
            USER_STATUS.format(driver.id),
            json={"status": UserStatus.INACTIVE.value, "reason": "Left the company"},
            headers=admin_claim_headers,
        )
    )

    assert recompute_requests == _one_run(app)


# ---------------------------------------------------------------------------------------------------------------------
# Nothing requested.
# ---------------------------------------------------------------------------------------------------------------------


def test_a_rollback_requests_nothing(app, db, monkeypatch, recompute_requests, caplog):
    """A write that flushed a plan input and then failed is rolled back by `atomic_transaction`, the services'
    transaction. Nothing is requested, not even by the session's next commit, and the detector logged no failure. The
    same change, committed, requests one recompute."""
    person = person_of(make_driver(db))
    _trigger_on(app, monkeypatch)

    with captured_logs(caplog, eta_recompute_dispatch.logger, logging.WARNING):
        with pytest.raises(RuntimeError, match="a later step failed"):
            with atomic_transaction():
                person.is_available = False
                db.session.flush()  # the detector has seen the change
                raise RuntimeError("a later step failed")
        person.vehicle_number = "01A777AA"  # the session's next transaction changes nothing the plan reads
        db.session.commit()

    assert recompute_requests == []
    assert _trigger_failures(caplog) == []
    assert person.is_available is True

    person.is_available = False
    db.session.commit()
    assert recompute_requests == _one_run(app)


def test_a_savepoint_release_does_not_publish_early(app, db, monkeypatch, recompute_requests, caplog):
    """SQLAlchemy dispatches `after_commit` for a SAVEPOINT release too, but the release is not the commit: nothing is
    requested until the outer transaction commits, and then once. A SAVEPOINT rolled back after it does not drop the
    request either: only the outer transaction decides (the guards of `payment_confirmation_dispatch`). The detector
    logged no failure meanwhile."""
    person = person_of(make_driver(db))
    _trigger_on(app, monkeypatch)

    with captured_logs(caplog, eta_recompute_dispatch.logger, logging.WARNING):
        with db.session.begin_nested():
            person.is_available = False
        assert recompute_requests == []
        with pytest.raises(RuntimeError, match="fails inside its savepoint"):
            with db.session.begin_nested():
                person.vehicle_number = "01A777AA"
                raise RuntimeError("a step that fails inside its savepoint")
    assert recompute_requests == []
    assert _trigger_failures(caplog) == []

    db.session.commit()

    assert recompute_requests == _one_run(app)


def test_engine_writes_and_gps_pings_request_nothing(app, db, monkeypatch, recompute_requests, caplog):
    """The engine's own writes and what the plan never reads request nothing: a run's hidden promises
    (`delivery_promises`), a weekly fit (`eta_model_fits`), the live location the driver's staff bot shares (the
    DeliveryPerson location columns) and the optimiser's bookkeeping on a stored route (`DeliveryRoute.extra_data`).
    The detector logged no failure meanwhile."""
    to = eleven_yesterday()
    driver = make_driver(db)
    open_session(db, driver, to - timedelta(hours=1))
    order_at_km(db, 3.0, created_at=to - timedelta(minutes=30))
    route = store_route(db, driver, [], to)
    _trigger_on(app, monkeypatch)

    with captured_logs(caplog, eta_recompute_dispatch.logger, logging.WARNING):
        summary = EtaEngine().recompute(now=to)
        add_fit(db, local_date(to) - timedelta(days=local_date(to).weekday()))
        latitude, longitude = point_at_km(1.0)
        _ok(
            app.test_client().post(
                LOCATION_PING,
                json={"latitude": latitude, "longitude": longitude, "horizontal_accuracy": 12.0},
                headers=bearer(app, driver),
            )
        )
        route.extra_data = {"provider": "osrm", "distance_km": 7.4}
        db.session.commit()

    assert summary["recorded"] == 1  # the engine did write its promise
    assert recompute_requests == []
    assert _trigger_failures(caplog) == []


# ---------------------------------------------------------------------------------------------------------------------
# Debounce and safety.
# ---------------------------------------------------------------------------------------------------------------------


def test_a_burst_of_commits_requests_one_recompute(app, db, monkeypatch, recompute_requests, admin_claim_headers):
    """An admin bulk action moves five confirmed orders to preparing, committing each order on its own. Each of the
    five committed transactions asks (`request_recompute`, once per transaction), and the debounce queues one run."""
    orders = [order_at_km(db, km, created_at=eleven_yesterday()) for km in (1.0, 2.0, 3.0, 4.0, 5.0)]
    asked: List[Dict] = []
    real_request = eta_recompute_dispatch.request_recompute

    def counted_request(*args, **kwargs):
        inspect.signature(real_request).bind(*args, **kwargs)
        asked.append(kwargs)
        return real_request(*args, **kwargs)

    monkeypatch.setattr(eta_recompute_dispatch, "request_recompute", counted_request)
    _trigger_on(app, monkeypatch)

    data = _ok(
        app.test_client().post(
            BULK_ACTIONS,
            json={
                "action": "process",
                "target_type": "order",
                "target_ids": [order.id for order in orders],
                "parameters": {},
            },
            headers=admin_claim_headers,
        )
    )

    assert data["results"]["success_count"] == 5
    assert asked == [{}] * 5
    assert recompute_requests == _one_run(app)


@pytest.mark.real_eta_publish
def test_a_commit_during_a_run_schedules_another(app, db, monkeypatch, eta_redis, admin_claim_headers, published):
    """A commit lands while a run holds the lock: its request sets the queued marker and publishes the next run. That
    run starts while the first still holds the lock. It clears the marker first, then finds the lock held and queues
    one more run for when the lock runs out (`EtaEngine.recompute`). Had it not cleared the marker first, the marker of
    the commit's own request would have swallowed that re-request, and the commit would never be planned. Each run
    expires ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS after its countdown, so it outlives it. Drives the real seam down to
    `Task.apply_async`."""
    person = person_of(make_driver(db))
    lock, marker = RedisKeyspace.eta_recompute_lock(), RedisKeyspace.eta_recompute_scheduled()
    eta_redis.set(lock, "the-running-run", nx=True, ex=app.config["ETA_RECOMPUTE_LOCK_SECONDS"])
    eta_redis.expiries[lock] = 97  # some of the running run's seconds have passed: 97 are left
    _trigger_on(app, monkeypatch)

    _ok(
        app.test_client().put(
            EDIT_DRIVER.format(person.id),
            json={**_edit_form(person), "is_available": False},
            headers=admin_claim_headers,
        )
    )
    assert eta_redis.get(marker) is not None  # the commit's request queued the next run
    assert EtaEngine().recompute(now=eleven_yesterday()) == {"skipped": "locked"}  # ...which starts under the lock

    countdown, ttl = app.config["ETA_RECOMPUTE_COUNTDOWN_SECONDS"], app.config["ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS"]
    runs = [options for name, options in published if name == recompute_eta_plan.name]
    assert runs == [
        {"countdown": countdown, "expires": countdown + ttl},  # the commit's
        {"countdown": 97, "expires": 97 + ttl},  # the locked run's, for when the lock runs out
    ]
    assert all(run["expires"] > run["countdown"] for run in runs)
    assert eta_redis.ttl(marker) == ttl  # the next run holds the queue again
    assert eta_redis.get(lock) == b"the-running-run"  # and the running run's lock is untouched


def test_the_kill_switch_stops_the_detector(app, db, monkeypatch, recompute_requests, admin_claim_headers):
    """With ETA_SHADOW_ENGINE_ENABLED off the detector returns at once: it looks at no flush, and a driver made
    unavailable requests nothing. Switched on, making them available again requests one recompute."""
    person = person_of(make_driver(db))
    form = _edit_form(person)
    looked: List = []
    real_detector = eta_recompute_dispatch._touches_the_plan

    def watched_detector(session):
        looked.append(session)
        return real_detector(session)

    monkeypatch.setattr(eta_recompute_dispatch, "_touches_the_plan", watched_detector)
    monkeypatch.setitem(app.config, "ETA_SHADOW_ENGINE_ENABLED", False)

    _ok(
        app.test_client().put(
            EDIT_DRIVER.format(person.id), json={**form, "is_available": False}, headers=admin_claim_headers
        )
    )
    assert (looked, recompute_requests) == ([], [])

    _trigger_on(app, monkeypatch)
    _ok(
        app.test_client().put(
            EDIT_DRIVER.format(person.id), json={**form, "is_available": True}, headers=admin_claim_headers
        )
    )
    assert looked
    assert recompute_requests == _one_run(app)


def test_a_detector_failure_never_fails_a_commit(app, db, monkeypatch, recompute_requests, admin_claim_headers, caplog):
    """A detector that raises (forced here) never reaches the write: the admin's edit commits, the failure is logged as
    a warning, and nothing is requested. The beat recomputes within ten minutes."""
    driver = make_driver(db)
    person = person_of(driver)

    def broken_detector(session):
        raise RuntimeError("the detector is broken")

    monkeypatch.setattr(eta_recompute_dispatch, "_touches_the_plan", broken_detector)
    _trigger_on(app, monkeypatch)

    with captured_logs(caplog, eta_recompute_dispatch.logger, logging.WARNING):
        _ok(
            app.test_client().put(
                EDIT_DRIVER.format(person.id),
                json={**_edit_form(person), "is_available": False},
                headers=admin_claim_headers,
            )
        )

    db.session.expire_all()
    assert person_of(driver).is_available is False
    assert recompute_requests == []
    warnings = [line for line in caplog.records if line.levelno == logging.WARNING]
    assert warnings
    assert all(line.exc_info and line.exc_info[0] is RuntimeError for line in warnings)


@contextmanager
def _sql_after_the_commit() -> Iterator[List[str]]:
    """The SQL run after the database COMMIT of the block's one transaction and before the block ends: what the
    commit's after-commit hooks ran. Filled when the block ends."""
    engine = _db.engine
    trail: List = []
    late: List[str] = []

    def on_commit(conn):  # noqa: ANN001
        trail.append(None)  # the COMMIT

    def on_statement(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        trail.append(statement)

    event.listen(engine, "commit", on_commit)
    event.listen(engine, "before_cursor_execute", on_statement)
    try:
        yield late
    finally:
        event.remove(engine, "commit", on_commit)
        event.remove(engine, "before_cursor_execute", on_statement)
    assert trail.count(None) == 1, "the block must commit exactly once"
    late.extend(trail[trail.index(None) + 1 :])


def _business_celery_app(app):
    """The business Celery app, imported as the unit modules import it: `create_app` patched to the pytest app for the
    one call `make_celery()` makes. The import makes it current (`Celery.__init__`), so a caller takes the current app
    first and puts it back."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name].celery


def test_an_eager_business_app_skips_event_publishes(app, db, monkeypatch, recompute_requests, eta_redis, caplog):
    """In a dev worker or beat the business Celery app is current and runs tasks eagerly, so a publish would run the
    recompute inline, inside the commit's after-commit hook. A commit there publishes nothing, sets no marker, logs the
    skip and runs no SQL after its COMMIT; the beat recomputes instead. The business app is current and eager for that
    commit only (as in tests/unit/test_celery_current_app_isolation.py); every other test runs on Celery's default app,
    which is not eager."""
    person = person_of(make_driver(db))
    _trigger_on(app, monkeypatch)
    previous = celery_state.get_current_app()
    business_celery = _business_celery_app(app)
    was_eager = business_celery.conf.task_always_eager
    business_celery.conf.task_always_eager = True
    business_celery.set_current()
    try:
        with captured_logs(caplog, eta_recompute_dispatch.logger, logging.INFO), _sql_after_the_commit() as late_sql:
            person.is_available = False
            db.session.commit()
    finally:
        business_celery.conf.task_always_eager = was_eager
        previous.set_current()

    assert late_sql == []
    assert (recompute_requests, eta_redis.store) == ([], {})
    assert [line for line in caplog.records if line.getMessage().startswith("eta_recompute_request_skipped")]
