"""Automatic "arrived" from the driver bot's location shares (owner decision 2026-11-17).

Driven through the endpoint the staff bot posts to, `POST /api/v1/staff/delivery/me/location`, with the bot's exact
body. Only external I/O is mocked: the two Celery publishes. The arrival goes through the real
`DeliveryService.mark_delivery_arrived` and the real bottle guard (the refusal case turns strict enforcement on
instead of faking the exception); the one other stand-in is an unexpected `RuntimeError`, which no real input raises.
"""
import logging
import threading

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.tasks.delivery_tasks import optimize_driver_route_task
from business_app.tasks.notification_tasks import send_delivery_update_task
from business_app.services.delivery_eta_service import DeliveryEtaService
from business_app.services import delivery_service as delivery_service_module
from business_app.services.delivery_service import DeliveryService
from business_app.utils.helpers import calculate_distance
from shared.enums import DeliveryStatus, OrderStatus
from shared.constants import TASHKENT_COORDINATES
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.eta_plan_builders import make_driver, order_at_km, shown_promise, tap

pytestmark = pytest.mark.integration

URL = "/api/v1/staff/delivery/me/location"
NOTES = "Automatically marked as arrived based on location tracking"
METRE_IN_DEGREES = 1 / 111320
BASE = (TASHKENT_COORDINATES["latitude"], TASHKENT_COORDINATES["longitude"])  # where the driver "stands"


@pytest.fixture
def publishes():
    """Both Celery publishes the share can cause, mocked. `notify` records the history ids sent to the customer."""
    with (
        patch.object(send_delivery_update_task, "delay") as notify,
        patch.object(optimize_driver_route_task, "delay"),
    ):
        yield notify


def _stop(db, driver, metres, status=DeliveryStatus.IN_TRANSIT, *, coords=True):
    """A stop of `driver` whose door is `metres` north of BASE; asserts the distance is what the case intends."""
    order = order_at_km(db, 1.0, created_at=datetime.now(UTC), driver=driver, status=OrderStatus.OUT_FOR_DELIVERY)
    address = order.delivery_address
    if coords:
        address.latitude = BASE[0] + metres * METRE_IN_DEGREES
        address.longitude = BASE[1]
        assert calculate_distance(BASE[0], BASE[1], address.latitude, address.longitude) * 1000 == pytest.approx(
            metres, abs=1.5
        )
    else:
        address.latitude = address.longitude = None
    order.delivery.status = status
    db.session.commit()
    return order.delivery.id


def _share(client, headers, accuracy=20.0):
    body = {"latitude": BASE[0], "longitude": BASE[1]}
    if accuracy is not None:
        body["horizontal_accuracy"] = accuracy
    return client.post(URL, json=body, headers=headers)


def _status(db, delivery_id):
    db.session.expire_all()
    return db.session.get(Delivery, delivery_id).status


def _history(db, delivery_id):
    return DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id, new_status=DeliveryStatus.ARRIVED).all()


def _location_saved(db, driver):
    db.session.expire_all()
    person = DeliveryPerson.query.filter_by(user_id=driver.id).one()
    assert person.current_location_lat == pytest.approx(BASE[0])
    assert person.current_location_lng == pytest.approx(BASE[1])


def test_a_share_at_the_door_marks_the_stop_arrived(client, db, driver_auth_headers, driver_with_location, publishes):
    delivery_id = _stop(db, driver_with_location, 50)

    resp = _share(client, driver_auth_headers)

    assert resp.status_code == 200
    assert _status(db, delivery_id) == DeliveryStatus.ARRIVED
    rows = _history(db, delivery_id)
    assert len(rows) == 1
    # Attributed to the driver (R6'), as the Arrived button records them; `automatic` says it was not a tap.
    assert (rows[0].old_status, rows[0].automatic, rows[0].changed_by, rows[0].notes) == (
        DeliveryStatus.IN_TRANSIT,
        True,
        driver_with_location.id,
        NOTES,
    )
    publishes.assert_called_once_with(rows[0].id)
    items = resp.get_json()["data"]["items"]
    assert [(i["delivery_id"], i["status"]) for i in items] == [(delivery_id, "arrived")]
    # Like the button, the driver is moved to the door.
    door = db.session.get(Delivery, delivery_id).order.delivery_address
    person = DeliveryPerson.query.filter_by(user_id=driver_with_location.id).one()
    assert (person.current_location_lat, person.current_location_lng) == (door.latitude, door.longitude)


def test_only_the_nearest_of_two_stops_in_range_arrives(client, db, driver_auth_headers, driver_with_location, publishes):
    far = _stop(db, driver_with_location, 80)
    near = _stop(db, driver_with_location, 40)

    assert _share(client, driver_auth_headers).status_code == 200

    assert _status(db, near) == DeliveryStatus.ARRIVED
    assert _status(db, far) == DeliveryStatus.IN_TRANSIT
    assert publishes.call_count == 1


def test_a_tie_goes_to_the_lower_delivery_id(client, db, driver_auth_headers, driver_with_location, publishes):
    first = _stop(db, driver_with_location, 40)
    second = _stop(db, driver_with_location, 40)
    assert first < second

    assert _share(client, driver_auth_headers).status_code == 200

    assert _status(db, first) == DeliveryStatus.ARRIVED
    assert _status(db, second) == DeliveryStatus.IN_TRANSIT


def test_a_stop_out_of_range_is_left_alone(client, db, driver_auth_headers, driver_with_location, publishes):
    delivery_id = _stop(db, driver_with_location, 150)

    assert _share(client, driver_auth_headers).status_code == 200

    assert _status(db, delivery_id) == DeliveryStatus.IN_TRANSIT
    publishes.assert_not_called()


@pytest.mark.parametrize("accuracy", [150.0, None], ids=["coarse", "absent"])
def test_a_coarse_or_unknown_accuracy_never_arrives(
    client, db, driver_auth_headers, driver_with_location, publishes, accuracy
):
    delivery_id = _stop(db, driver_with_location, 30)

    resp = _share(client, driver_auth_headers, accuracy=accuracy)

    assert resp.status_code == 200
    assert _status(db, delivery_id) == DeliveryStatus.IN_TRANSIT
    publishes.assert_not_called()
    _location_saved(db, driver_with_location)


def test_another_drivers_stop_is_left_alone(client, db, driver_auth_headers, driver_with_location, publishes):
    other = make_driver(db)
    delivery_id = _stop(db, other, 30)

    assert _share(client, driver_auth_headers).status_code == 200

    assert _status(db, delivery_id) == DeliveryStatus.IN_TRANSIT
    publishes.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [s for s in DeliveryStatus if s != DeliveryStatus.IN_TRANSIT],
    ids=lambda s: s.value,
)
def test_a_stop_not_in_transit_is_left_alone(client, db, driver_auth_headers, driver_with_location, publishes, status):
    delivery_id = _stop(db, driver_with_location, 30, status)

    assert _share(client, driver_auth_headers).status_code == 200

    assert _status(db, delivery_id) == status
    publishes.assert_not_called()


def _strict_bottle_guard(app, monkeypatch):
    """Strict enforcement with the driver holding no bottle session: the real guard refuses every arrival with
    BOTTLE_SESSION_REQUIRED."""
    monkeypatch.setitem(app.config, "BOTTLE_SESSION_ENFORCEMENT_STRICT", True)


def test_a_refusal_of_the_bottle_guard_never_fails_the_share(
    app, client, db, driver_auth_headers, driver_with_location, publishes, monkeypatch, caplog
):
    _strict_bottle_guard(app, monkeypatch)
    delivery_id = _stop(db, driver_with_location, 30)

    with captured_logs(caplog, app.logger, logging.INFO):
        resp = _share(client, driver_auth_headers)

    assert resp.status_code == 200
    assert _status(db, delivery_id) == DeliveryStatus.IN_TRANSIT
    publishes.assert_not_called()
    _location_saved(db, driver_with_location)
    # R4: an expected refusal is one info line naming the delivery and the code, never an error.
    refusals = [r for r in caplog.records if r.levelno == logging.INFO and "BOTTLE_SESSION_REQUIRED" in r.getMessage()]
    assert [str(delivery_id) in r.getMessage() for r in refusals] == [True]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_the_service_answers_none_on_a_refusal_and_releases_the_lock(
    app, db, driver_with_location, publishes, monkeypatch
):
    _strict_bottle_guard(app, monkeypatch)
    delivery_id = _stop(db, driver_with_location, 30)

    result = DeliveryService().mark_arrived_from_driver_location(driver_with_location.id, BASE[0], BASE[1], 20.0)

    assert result is None
    # R4: the refusal ended its own transaction, so the row lock is gone.
    assert not db.session().in_transaction()
    assert _status(db, delivery_id) == DeliveryStatus.IN_TRANSIT
    assert _history(db, delivery_id) == []


def test_the_service_ends_its_transaction_when_nothing_is_in_range(db, driver_with_location, publishes):
    _stop(db, driver_with_location, 150)

    assert DeliveryService().mark_arrived_from_driver_location(driver_with_location.id, BASE[0], BASE[1], 20.0) is None
    assert not db.session().in_transaction()


def test_the_service_ends_its_transaction_when_the_stop_moved_first(db, driver_with_location, publishes, monkeypatch):
    delivery_id = _stop(db, driver_with_location, 30)
    real = delivery_service_module.calculate_distance

    def distance_then_a_tap_wins(*args):
        result = real(*args)
        db.session.execute(Delivery.__table__.update().where(Delivery.id == delivery_id).values(status=DeliveryStatus.ARRIVED))
        db.session.commit()
        return result

    monkeypatch.setattr(delivery_service_module, "calculate_distance", distance_then_a_tap_wins)

    assert DeliveryService().mark_arrived_from_driver_location(driver_with_location.id, BASE[0], BASE[1], 20.0) is None
    assert not db.session().in_transaction()
    assert _history(db, delivery_id) == []


def test_an_unexpected_error_never_fails_the_share(
    client, db, driver_auth_headers, driver_with_location, publishes, monkeypatch
):
    delivery_id = _stop(db, driver_with_location, 30)

    def boom(self, delivery, **kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(BottleTrackingService, "assert_driver_can_progress_delivery", boom)

    resp = _share(client, driver_auth_headers)

    assert resp.status_code == 200
    assert _status(db, delivery_id) == DeliveryStatus.IN_TRANSIT
    publishes.assert_not_called()
    _location_saved(db, driver_with_location)


def test_a_stop_without_coordinates_is_skipped_not_an_error(
    client, db, driver_auth_headers, driver_with_location, publishes
):
    no_coords = _stop(db, driver_with_location, 10, coords=False)
    in_range = _stop(db, driver_with_location, 60)

    resp = _share(client, driver_auth_headers)

    assert resp.status_code == 200
    assert _status(db, no_coords) == DeliveryStatus.IN_TRANSIT
    assert _status(db, in_range) == DeliveryStatus.ARRIVED


def test_a_second_share_does_not_arrive_the_stop_twice(
    client, db, driver_auth_headers, driver_with_location, publishes
):
    delivery_id = _stop(db, driver_with_location, 50)

    assert _share(client, driver_auth_headers).status_code == 200
    assert _share(client, driver_auth_headers).status_code == 200

    assert len(_history(db, delivery_id)) == 1
    assert publishes.call_count == 1


def test_the_old_driver_update_location_route_is_gone(client, driver_auth_headers):
    resp = client.post(
        "/api/v1/delivery/driver/update-location", json={"lat": 41.3, "lng": 69.2}, headers=driver_auth_headers
    )

    assert resp.status_code in (404, 405)


def _pg_stop_with_token(pg_db, metres=50):
    """A committed IN_TRANSIT stop `metres` north of the point the driver shares, its driver's token, and that point."""
    from flask_jwt_extended import create_access_token

    from tests.integration.eta_plan_builders import point_at_km

    driver = make_driver(pg_db)
    order = order_at_km(pg_db, 1.0, created_at=datetime.now(UTC), driver=driver, status=OrderStatus.OUT_FOR_DELIVERY)
    door = point_at_km(1.0)
    address = order.delivery_address
    address.latitude = door[0] + metres * METRE_IN_DEGREES
    order.delivery.status = DeliveryStatus.IN_TRANSIT
    delivery_id = order.delivery.id
    pg_db.session.commit()
    assert calculate_distance(door[0], door[1], address.latitude, address.longitude) * 1000 == pytest.approx(
        metres, abs=1.5
    )
    return delivery_id, driver, door, {"Authorization": f"Bearer {create_access_token(identity=str(driver.id))}"}


def _share_body(door):
    return {"latitude": door[0], "longitude": door[1], "horizontal_accuracy": 20.0}


def test_a_share_at_the_door_arrives_the_stop_on_postgres(pg_app, pg_db):
    """Case one, end to end on the migrated Postgres schema (the row lock is real SQL there, SQLite ignores it), and
    the statements sent: the candidate read takes no lock (R3': a share never waits on the driver's other stops); the
    one row it picks is then locked alone."""
    from sqlalchemy import event

    delivery_id, driver, door, headers = _pg_stop_with_token(pg_db)
    sent = []

    def record(conn, cursor, statement, *args):
        sent.append(" ".join(statement.split()))

    event.listen(pg_db.engine, "before_cursor_execute", record)
    try:
        with patch.object(send_delivery_update_task, "delay") as notify, patch.object(
            optimize_driver_route_task, "delay"
        ):
            resp = pg_app.test_client().post(URL, json=_share_body(door), headers=headers)
    finally:
        event.remove(pg_db.engine, "before_cursor_execute", record)

    assert resp.status_code == 200
    assert _status(pg_db, delivery_id) == DeliveryStatus.ARRIVED
    rows = _history(pg_db, delivery_id)
    assert len(rows) == 1
    assert (rows[0].changed_by, rows[0].automatic) == (driver.id, True)
    notify.assert_called_once_with(rows[0].id)
    locking = [q for q in sent if q.startswith("SELECT") and "FOR UPDATE" in q and "FROM deliveries" in q]
    candidate_reads = [q for q in sent if q.startswith("SELECT") and "JOIN addresses" in q]
    assert candidate_reads and all("FOR UPDATE" not in q for q in candidate_reads)
    assert len(locking) == 1 and "WHERE deliveries.id =" in locking[0]


def test_two_simultaneous_shares_arrive_the_stop_once_on_postgres(pg_app, pg_db, monkeypatch):
    """R3: two live-location updates for the same driver and stop that overlap produce ONE arrival and ONE customer
    message. Each request runs on its own thread, app context, session and connection. Both are held (a barrier) after
    reading their candidates and again just before the arrival commits, so without the row lock both arrive the stop. Bounded: the
    barrier and the joins time out, and a stuck backend is terminated."""
    from sqlalchemy import text

    from business_app.services import delivery_service as delivery_service_module

    delivery_id, _, door, headers = _pg_stop_with_token(pg_db)
    barrier = threading.Barrier(2, timeout=15)
    real_distance = delivery_service_module.calculate_distance

    def distance_after_both_read(*args):
        result = real_distance(*args)
        barrier.wait()  # both threads have read the candidates unlocked and are about to pick the stop
        return result

    monkeypatch.setattr(delivery_service_module, "calculate_distance", distance_after_both_read)

    # Second meeting point, after the chosen row is locked and re-checked and before the arrival commits. With the
    # lock the second share is parked on the row, so the first waits out the short timeout alone and goes on; without
    # it both are here together and both arrive the stop.
    at_the_bottle_guard = threading.Barrier(2, timeout=3)
    real_guard = BottleTrackingService.assert_driver_can_progress_delivery

    def guard_after_both_got_here(self, *args, **kwargs):
        try:
            at_the_bottle_guard.wait()
        except threading.BrokenBarrierError:
            pass
        return real_guard(self, *args, **kwargs)

    monkeypatch.setattr(BottleTrackingService, "assert_driver_can_progress_delivery", guard_after_both_got_here)
    answers = []

    def share():
        try:
            with pg_app.app_context():
                answers.append(pg_app.test_client().post(URL, json=_share_body(door), headers=headers).status_code)
        except BaseException as exc:  # noqa: BLE001 -- named by the assertion below
            answers.append(exc)

    with patch.object(send_delivery_update_task, "delay") as notify, patch.object(optimize_driver_route_task, "delay"):
        threads = [threading.Thread(target=share, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        stuck = [t for t in threads if t.is_alive()]
        if stuck:
            with pg_db.engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND (state = 'idle in transaction' OR wait_event_type = 'Lock')"
                    )
                )
                connection.commit()
            for thread in stuck:
                thread.join(timeout=5)
        assert not stuck, "a share is still waiting after 30 s"

    assert answers == [200, 200]
    assert _status(pg_db, delivery_id) == DeliveryStatus.ARRIVED
    rows = _history(pg_db, delivery_id)
    assert len(rows) == 1
    notify.assert_called_once_with(rows[0].id)


def test_an_auto_arrival_is_the_arrival_the_eta_engine_learns_from(
    client, db, driver_auth_headers, driver_with_location, publishes
):
    """R6': the S2 reader counts an arrival only when its own driver made it, so an automatic one must carry the
    driver, or every auto-arrived leg vanishes from headroom learning and the accuracy report."""
    delivery_id = _stop(db, driver_with_location, 50, DeliveryStatus.PICKED_UP)  # a real PICKED_UP -> IN_TRANSIT tap
    delivery = db.session.get(Delivery, delivery_id)
    tapped = tap(db, delivery, DeliveryStatus.IN_TRANSIT, datetime.now(UTC) - timedelta(minutes=20))
    shown_promise(db, tapped)

    assert _share(client, driver_auth_headers).status_code == 200

    [arrival] = _history(db, delivery_id)
    legs = DeliveryEtaService.s2_leg_rows(start=datetime.now(UTC) - timedelta(hours=1), end=datetime.now(UTC) + timedelta(hours=1))
    assert [leg.arrival for leg in legs] == [arrival.changed_at.replace(tzinfo=UTC)]
