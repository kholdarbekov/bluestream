"""Unit tests for the route-optimization Celery tasks.

The conftest `block_external_side_effects` fixture replaces `Task.delay` with
a mock; we don't need eager Celery here. Instead we call the task body
directly (the function under `@shared_task` is still callable as a regular
function, with `self` substituted by a `MagicMock`).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.tasks.delivery_tasks import (
    evaluate_pool_insertion_suggestions_task,
    optimize_driver_route_task,
)
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            if i == j:
                matrix[(i, j)] = {"distance_km": 0.0, "duration_minutes": 0.0}
            else:
                km = calculate_distance(pi[0], pi[1], pj[0], pj[1])
                matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


@pytest.fixture
def driver(db):
    user = User(
        email="task-driver@example.com",
        phone="+998901111100",
        password_hash="x",
        first_name="Task",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="task-cust@example.com",
        phone="+998907700099",
        password_hash="x",
        first_name="C",
        last_name="ust",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def driver_with_location(db, driver):
    # Set working_hours to 24-hour coverage so `is_working_now` is True
    # regardless of when the test runs in UTC.
    person = DeliveryPerson(
        user_id=driver.id,
        full_name="x",
        phone="x",
        current_location_lat=41.300,
        current_location_lng=69.250,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
        working_hours_start="00:00",
        working_hours_end="23:59",
    )
    db.session.add(person)
    db.session.commit()
    return person


def _add_assigned_delivery(db, customer_id, driver_id, lat, lng, order_no):
    addr = UserAddress(
        user_id=customer_id,
        title="x",
        full_address="x",
        street_address="x",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _add_pool_delivery(db, customer_id, lat, lng, order_no):
    addr = UserAddress(
        user_id=customer_id,
        title="pool",
        full_address="pool",
        street_address="pool",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _add_committed_delivery(db, customer_id, driver_id, lat, lng, order_no):
    """An IN_TRANSIT delivery WITH a matching DeliveryStatusHistory row, so
    `RouteOptimizationService.get_committed_stop` recognizes it via its
    primary path (route-UX plan 2026-08-11 §4.1/§7)."""
    addr = UserAddress(
        user_id=customer_id,
        title="x",
        full_address="x",
        street_address="x",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    db.session.add(
        DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=DeliveryStatus.PICKED_UP,
            new_status=DeliveryStatus.IN_TRANSIT,
            changed_by=driver_id,
            changed_at=datetime.now(UTC),
        )
    )
    db.session.commit()
    return delivery


# ---------------------------------------------------------------------------
# optimize_driver_route_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestOptimizeDriverRouteTask:
    def test_returns_no_active_deliveries_when_driver_has_none(self, app, db, driver):
        with app.app_context():
            with patch("business_app.utils.bot_webhook.notify_route_updated") as nru:
                result = optimize_driver_route_task.run(driver.id)
            assert result == {"optimized": False, "reason": "no_active_deliveries"}
            nru.assert_not_called()

    def test_debounced_location_update_reports_its_own_reason(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        """Review fix 4: a location_update solve skipped by the debounce
        (route-UX plan §4.5) must report a reason distinct from the generic
        'no_active_deliveries' label used when the driver truly has none —
        otherwise debounce rate can't be counted from task results."""
        with app.app_context():
            _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-deb")
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            with patch("business_app.utils.bot_webhook.notify_route_updated") as nru:
                first = optimize_driver_route_task.run(driver.id, trigger="location_update")
                assert first["optimized"] is True

                # Same spot, no time elapsed -> debounced.
                second = optimize_driver_route_task.run(driver.id, trigger="location_update")

            assert second == {"optimized": False, "reason": "location_debounced"}
            nru.assert_called_once()  # only the first (real) solve pushes a webhook

    def test_soft_time_limit_returns_graceful_result_without_retry(self, app, db, driver, monkeypatch):
        """A SoftTimeLimitExceeded must abort gracefully (so the hard 120s
        SIGKILL never fires mid-commit), not fall through to a blind retry."""
        from celery.exceptions import SoftTimeLimitExceeded

        class _StallService:
            def optimize_for_driver(self, *_a, **_k):
                raise SoftTimeLimitExceeded()

        with app.app_context():
            monkeypatch.setattr(
                "business_app.services.route_optimization_service.RouteOptimizationService",
                _StallService,
            )
            result = optimize_driver_route_task.run(driver.id, trigger="auto")

        assert result == {"optimized": False, "reason": "time_budget_exceeded"}

    def test_task_time_limits_provisioned_for_external_io(self):
        """The task does sequential external geocode/matrix I/O; its budget must
        not regress back to the anomalously tight 120s/100s that SIGKILLed it."""
        assert optimize_driver_route_task.time_limit >= 300
        assert optimize_driver_route_task.soft_time_limit >= 270

    def test_optimizes_and_pushes_route_updated_webhook(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        with app.app_context():
            d_far = _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-far")
            d_close = _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-close")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            # Helpers are imported lazily inside the task body — patch at the
            # source module, not at the task module.
            with patch("business_app.utils.bot_webhook.notify_route_updated", return_value=True) as nru:
                result = optimize_driver_route_task.run(driver.id, trigger="accept")

            assert result["optimized"] is True
            assert result["delivery_count"] == 2
            assert result["trigger"] == "accept"
            assert result["matrix_source"] == "haversine"

            # Persisted route reflects the closest-first sequence.
            route = (
                DeliveryRoute.query.filter_by(delivery_person_id=driver.id)
                .order_by(DeliveryRoute.created_at.desc())
                .first()
            )
            assert route.optimized_order == [d_close.id, d_far.id]

            # `.run()` calls the task body directly, bypassing Celery's
            # dispatch machinery -- there is no real task id, so
            # `self.request.id` resolves to None and event_id becomes
            # "route_updated:None". Real dispatch (.delay()/.apply_async())
            # always assigns a task id; see test_event_id_stable_across_retry
            # below for that path with a controlled, real task id.
            nru.assert_called_once_with(
                driver.id,
                sound=False,
                materiality=route.extra_data["materiality"],
                trigger="accept",
                event_id=f"route_updated:{optimize_driver_route_task.request.id}",
            )

    def test_event_id_stable_across_retry_same_task_id(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        """Task 8 review fix 1: a Celery `self.retry()` re-runs the task body
        from scratch but KEEPS the original task id. If the task minted its
        own random event_id, a retry of the SAME logical push would look
        like a brand-new event to the bot's dedup and could double-send --
        exactly the "too unique" failure mode the brief warns about.

        `.apply(task_id=...)` executes the task synchronously through
        Celery's real request-context machinery (unlike `.run()`, which
        bypasses it and always yields `request.id is None`), so this is the
        first test in the suite that observes a real, controlled task id
        flowing into the webhook payload.
        """
        with app.app_context():
            _add_assigned_delivery(db, customer.id, driver.id, 41.300, 69.260, "ORD-retry")
            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            with patch("business_app.utils.bot_webhook.notify_route_updated", return_value=True) as nru:
                # Same task id twice -- simulates the original attempt and a
                # `self.retry()` re-run of it.
                optimize_driver_route_task.apply(
                    args=(driver.id,), kwargs={"trigger": "accept"}, task_id="task-fixed-id"
                ).get()
                optimize_driver_route_task.apply(
                    args=(driver.id,), kwargs={"trigger": "accept"}, task_id="task-fixed-id"
                ).get()
                # A different task id -- a genuinely separate dispatch.
                optimize_driver_route_task.apply(
                    args=(driver.id,), kwargs={"trigger": "accept"}, task_id="task-different-id"
                ).get()

        ids = [c.kwargs["event_id"] for c in nru.call_args_list]
        assert ids[0] == ids[1] == "route_updated:task-fixed-id"
        assert ids[2] == "route_updated:task-different-id"
        assert ids[2] not in (ids[0], ids[1])


# ---------------------------------------------------------------------------
# evaluate_pool_insertion_suggestions_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestEvaluatePoolInsertionSuggestionsTask:
    """route-UX plan 2026-08-11 §7 / Task 13: the evaluator is now the §7
    diversion evaluator AND the single broadcast fan-out point. An offer is
    only ever made to a driver with a committed stop, and only when new-first
    beats committed-first by >= ROUTE_DIVERSION_MIN_GAIN_MINUTES. Every run —
    offer or not — ends with exactly one `notify_staff_new_order.delay` call,
    excluding the diverted driver (or nobody)."""

    def test_skips_when_delivery_already_assigned(
        self, app, db, driver, driver_with_location, customer
    ):
        with app.app_context():
            d = _add_assigned_delivery(db, customer.id, driver.id, 41.31, 69.27, "ORD-A")
            with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as npis, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(d.id)
            assert result == {"suggested": False, "reason": "already_assigned"}
            npis.assert_not_called()
            broadcast.assert_not_called()

    def test_no_offer_and_full_broadcast_when_no_active_drivers(self, app, db, customer):
        with app.app_context():
            d = _add_pool_delivery(db, customer.id, 41.31, 69.27, "ORD-POOL-1")
            with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as npis, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(d.id)
            assert result == {"suggested": False, "reason": "no_diversion_gain"}
            npis.assert_not_called()
            # No driver exists to divert -> the order must still reach the
            # pool via the unconditional broadcast (§10 fix: never silence).
            broadcast.assert_called_once_with(d.order_id, exclude_driver_user_id=None)

    def test_no_offer_without_committed_stop(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        """A driver with only an ASSIGNED (not started) stop has no committed
        stop (spec §7: 'no committed stop -> no offer; the optimizer just
        does the right thing silently') -- no matter how close the pool
        order is, but the order still reaches the full broadcast."""
        with app.app_context():
            _add_assigned_delivery(db, customer.id, driver.id, 41.301, 69.251, "ORD-close")
            pool_delivery = _add_pool_delivery(db, customer.id, 41.300, 69.330, "ORD-far")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as npis, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(pool_delivery.id)
            assert result == {"suggested": False, "reason": "no_diversion_gain"}
            npis.assert_not_called()
            broadcast.assert_called_once_with(pool_delivery.order_id, exclude_driver_user_id=None)

    def test_offers_diversion_when_gain_exceeds_threshold(
        self, app, db, driver, driver_with_location, customer, monkeypatch
    ):
        with app.app_context():
            # Committed stop ~6.7 km east; new pool stop ~50 m from the driver
            # -- new-first is a large, clear win over committed-first.
            committed = _add_committed_delivery(db, customer.id, driver.id, 41.300, 69.330, "ORD-east")
            close_pool = _add_pool_delivery(db, customer.id, 41.3004, 69.2504, "ORD-near-driver")

            monkeypatch.setattr(
                "business_app.services.maps_service.MapsService.get_distance_matrix",
                _haversine_matrix,
            )
            with patch(
                "business_app.utils.bot_webhook.notify_pool_insertion_suggestion",
                return_value=True,
            ) as npis, patch(
                "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
            ) as broadcast:
                result = evaluate_pool_insertion_suggestions_task.run(close_pool.id)

            assert result["suggested"] is True
            assert result["driver_id"] == driver.id
            assert result["gain_minutes"] > 8.0
            assert result["committed_delivery_id"] == committed.id
            assert result["committed_order_number"] == "ORD-east"
            npis.assert_called_once()
            # Verify the bot helper was called with the right payload shape.
            kwargs = npis.call_args.kwargs
            assert kwargs["driver_id"] == driver.id
            assert kwargs["delivery_id"] == close_pool.id
            assert kwargs["order_no"] == "ORD-near-driver"
            assert kwargs["gain_minutes"] > 8.0
            assert kwargs["committed_order_number"] == "ORD-east"
            # The diverted driver is excluded from the broadcast (§10 fix: no
            # second Accept button for the same order).
            broadcast.assert_called_once_with(close_pool.order_id, exclude_driver_user_id=driver.id)
