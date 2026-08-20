"""Diversion (spec §7): when a new pool order arrives and a driver HAS a
committed stop, offer 'go here first' ONLY when new-first beats
committed-first by >= ROUTE_DIVERSION_MIN_GAIN_MINUTES on ONE matrix
snapshot. No committed stop -> no offer. The evaluator is also the single
fan-out point: broadcast excludes the diverted driver (fixes the §10
double-Accept bug). Accepting never reverts any status."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.tasks.delivery_tasks import evaluate_pool_insertion_suggestions_task
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _make_driver(db, email, phone, telegram_id, lat=41.3000, lng=69.2500):
    user = User(
        email=email,
        phone=phone,
        password_hash="x",
        first_name="Div",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        telegram_id=telegram_id,
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Div Driver",
        phone=phone,
        current_location_lat=lat,
        current_location_lng=lng,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
        # 24h coverage so DeliveryPerson.is_working_now is True — the
        # evaluator filters candidates on it (same pattern as
        # tests/unit/test_route_optimization_tasks.py:75-87).
        working_hours_start="00:00",
        working_hours_end="23:59",
    )
    db.session.add(person)
    db.session.commit()
    return user


@pytest.fixture
def driver(db):
    return _make_driver(db, "div-driver@example.com", "+998900000131", "777000131")


@pytest.fixture
def customer(db):
    user = User(
        email="div-cust@example.com",
        phone="+998900000132",
        password_hash="x",
        first_name="C",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_delivery(db, customer_id, driver_id, order_no, lat, lng, status):
    addr = UserAddress(
        user_id=customer_id,
        title="Stop",
        full_address=f"Stop {order_no}",
        street_address=f"Stop {order_no}",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        delivery_address_id=addr.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _commit_to(db, delivery):
    db.session.add(
        DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=DeliveryStatus.PICKED_UP,
            new_status=DeliveryStatus.IN_TRANSIT,
            changed_by=delivery.delivery_person_id,
            changed_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            km = 0.0 if i == j else calculate_distance(pi[0], pi[1], pj[0], pj[1])
            matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


@pytest.fixture(autouse=True)
def _matrix(monkeypatch):
    monkeypatch.setattr(
        "business_app.services.maps_service.MapsService.get_distance_matrix",
        _haversine_matrix,
    )


@pytest.mark.unit
@pytest.mark.delivery
class TestComputeDiversionGain:
    def test_none_without_committed_stop(self, app, db, driver, customer):
        _make_delivery(db, customer.id, driver.id, "ORD-DV-1", 41.30, 69.33, DeliveryStatus.ASSIGNED)
        pool = _make_delivery(db, customer.id, None, "POOL-DV-1", 41.3005, 69.2505, DeliveryStatus.SCHEDULED)
        with app.app_context():
            assert RouteOptimizationService().compute_diversion_gain(driver.id, pool.id) is None

    def test_gain_when_new_stop_is_next_door_and_committed_is_far(
        self, app, db, driver, customer
    ):
        """Committed stop ~6.7 km east; new order ~50 m from the driver.
        committed-first ≈ 2*6.7km worth of minutes; new-first ≈ one leg.
        Gain is large and positive."""
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-2", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-2", 41.3004, 69.2504, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            gain = RouteOptimizationService().compute_diversion_gain(driver.id, pool.id)
        assert gain is not None
        assert gain["gain_minutes"] > 8.0
        assert gain["committed_delivery_id"] == committed.id
        assert gain["committed_order_number"] == "ORD-DV-2"

    def test_no_gain_when_new_stop_is_on_the_way_past_committed(
        self, app, db, driver, customer
    ):
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-3", 41.300, 69.290, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-3", 41.300, 69.330, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            gain = RouteOptimizationService().compute_diversion_gain(driver.id, pool.id)
        assert gain is not None
        assert gain["gain_minutes"] <= 0.0

    def test_gain_unaffected_by_route_service_time(self, app, db, driver, customer, monkeypatch):
        """`compute_diversion_gain` compares committed-first vs new-first on
        the SAME set of nodes (§7 threshold is measured in minutes) — it must
        stay on `_sum_path_minutes` (travel-only), never `_sum_route_metrics`
        (route-UX plan 2026-08-11 Task 5 SSOT). Even if it did fold service
        time in, a flat per-stop constant would apply to both candidate
        orders equally (same stop count) and cancel out of the subtraction —
        this test pins that outcome directly against two different
        ROUTE_SERVICE_TIME_MINUTES values rather than trusting the algebra."""
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-5", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-5", 41.3004, 69.2504, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            monkeypatch.setitem(app.config, "ROUTE_SERVICE_TIME_MINUTES", 4.0)
            gain_a = RouteOptimizationService().compute_diversion_gain(driver.id, pool.id)
            monkeypatch.setitem(app.config, "ROUTE_SERVICE_TIME_MINUTES", 50.0)
            gain_b = RouteOptimizationService().compute_diversion_gain(driver.id, pool.id)
        assert gain_a is not None and gain_b is not None
        assert gain_a["gain_minutes"] == pytest.approx(gain_b["gain_minutes"])


@pytest.mark.unit
@pytest.mark.delivery
class TestDiversionTask:
    def test_offer_sent_and_broadcast_excludes_diverted_driver(
        self, app, db, driver, customer
    ):
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-4", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-4", 41.3004, 69.2504, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            with patch(
                "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
            ) as hook, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(pool.id)
        assert result["suggested"] is True
        offers = [c.args[1] for c in hook.call_args_list
                  if c.args[0] == "/internal/pool-insertion-suggestion"]
        assert len(offers) == 1
        p = offers[0]
        assert p["driver_id"] == driver.id
        assert p["gain_minutes"] > 8.0
        assert p["committed_order_number"] == "ORD-DV-4"
        assert p["delivery_id"] == pool.id
        broadcast.assert_called_once_with(pool.order_id, exclude_driver_user_id=driver.id)

    def test_below_threshold_no_offer_full_broadcast(self, app, db, driver, customer):
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-5", 41.300, 69.290, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-5", 41.300, 69.330, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            with patch(
                "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
            ) as hook, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(pool.id)
        assert result["suggested"] is False
        assert [c for c in hook.call_args_list
                if c.args[0] == "/internal/pool-insertion-suggestion"] == []
        broadcast.assert_called_once_with(pool.order_id, exclude_driver_user_id=None)

    def test_driver_without_committed_stop_gets_no_offer(self, app, db, driver, customer):
        _make_delivery(db, customer.id, driver.id, "ORD-DV-6", 41.300, 69.330, DeliveryStatus.ASSIGNED)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-6", 41.3004, 69.2504, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            with patch(
                "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
            ), patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(pool.id)
        assert result["suggested"] is False
        broadcast.assert_called_once_with(pool.order_id, exclude_driver_user_id=None)

    def test_accepting_the_diversion_never_reverts_the_committed_status(
        self, app, db, driver, customer
    ):
        """Accept = the normal accept flow; the committed delivery keeps its
        IN_TRANSIT status (there is no IN_TRANSIT->ASSIGNED edge, and none is
        needed — starting the new stop later re-commits by recency)."""
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-7", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-7", 41.3004, 69.2504, DeliveryStatus.SCHEDULED
        )
        from business_app.services.staff_service import StaffService

        with app.app_context():
            with patch(
                "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
            ), patch("business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True):
                StaffService.accept_order(pool.id, driver.id)
            # Flask-SQLAlchemy 3.1.1 scopes db.session to the app context: a
            # fresh `with app.app_context()` gets a fresh session, so `committed`/
            # `pool` (loaded under the outer `db` fixture's context) are not
            # persistent in THIS session and `.refresh()` on them would raise.
            # Re-fetch by id instead of mutating the pre-context objects.
            committed_after = Delivery.query.get(committed.id)
            assert committed_after.status == DeliveryStatus.IN_TRANSIT
            pool_after = Delivery.query.get(pool.id)
            assert pool_after.delivery_person_id == driver.id

    def test_failed_targeted_push_does_not_exclude_driver_from_broadcast(
        self, app, db, driver, customer
    ):
        """Review fix 1: `_send_staff_bot_webhook` never raises -- it returns
        False on a non-2xx status, a connection error, or a missing
        WEBHOOK_SECRET (e.g. the staff bot rate-limits or 500s the
        /internal/pool-insertion-suggestion endpoint). If the targeted offer
        fails to actually reach the driver, they must NOT be excluded from
        the broadcast -- otherwise the single best-fit driver silently gets
        nothing at all, which contradicts 'diversion failures degrade to a
        full broadcast, never to silence.'"""
        committed = _make_delivery(
            db, customer.id, driver.id, "ORD-DV-8", 41.300, 69.330, DeliveryStatus.IN_TRANSIT
        )
        _commit_to(db, committed)
        pool = _make_delivery(
            db, customer.id, None, "POOL-DV-8", 41.3004, 69.2504, DeliveryStatus.SCHEDULED
        )
        with app.app_context():
            with patch(
                "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=False
            ) as hook, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(pool.id)
        # A gain was found -- the best-fit driver was identified...
        assert result["suggested"] is True
        offers = [c.args[1] for c in hook.call_args_list
                  if c.args[0] == "/internal/pool-insertion-suggestion"]
        assert len(offers) == 1
        # ...but the push failed (bot returned non-2xx), so the driver must
        # NOT be excluded -- they still need the full broadcast.
        broadcast.assert_called_once_with(pool.order_id, exclude_driver_user_id=None)
