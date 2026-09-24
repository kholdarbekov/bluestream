"""Business-rule unit tests for DeliveryService constraints and helpers."""

import inspect
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.user import UserAddress
from business_app.services.delivery_service import DeliveryService
from business_app.tasks import delivery_tasks, staff_tasks
from business_app.utils.constants import DeliveryType
from shared.enums import DeliveryStatus
from business_app.utils.exceptions import DeliveryError, ValidationError


def _patch_task_publish(monkeypatch, task, method, fake):
    """Replace `task.delay` / `task.apply_async` for one test, and leave nothing behind.

    Not `monkeypatch.setattr(task, method, fake)`: both methods live on the Task
    class, so setattr saves the BOUND method `getattr` returns (the autouse no-op
    from tests/conftest.py) and its undo writes that onto the task instance. The
    stale no-op then outlives the test and shadows every later class-level patch
    of `Task.delay` in the worker, so a recorder patching the class (`enqueued`
    in test_redispatch_reschedules_to_today.py) saw no publish at all. Patching
    the instance `__dict__` undoes to "absent".
    """
    monkeypatch.setitem(vars(task), method, fake)


def _unshadow_task_publish(monkeypatch):
    """Let a class-level patch of `Task.delay` / `Task.apply_async` see every publish.

    For this test only, drop any instance-level `delay` / `apply_async` that an
    earlier test in the worker left behind by patching a task with
    `monkeypatch.setattr` (several suites still do). Such a leftover routes the
    call around the class, so the recorder never sees it.
    """
    from celery import current_app

    for task in current_app.tasks.values():
        for method in ("delay", "apply_async"):
            monkeypatch.delitem(vars(task), method, raising=False)


def _task_spy(monkeypatch, task, method):
    """Record `.delay` / `.apply_async` calls on a Celery task, binding each call
    against the TASK's own signature (`run`, with `self` already bound on a
    `bind=True` task). `Task.delay(*args, **kwargs)` accepts any call at all, so
    a payload the worker could not run would stay green (same idea as `_spy` in
    tests/unit/test_agent_order_confirmation.py).

    `.delay` calls are recorded as `(args, kwargs)`, and `.apply_async` calls as
    `(args, kwargs, options)`.
    """
    signature = inspect.signature(task.run)
    calls = []

    if method == "delay":

        def fake(*args, **kwargs):
            signature.bind(*args, **kwargs)
            calls.append((args, kwargs))

    else:

        def fake(args=None, kwargs=None, **options):
            signature.bind(*(args or ()), **(kwargs or {}))
            calls.append((tuple(args or ()), dict(kwargs or {}), options))

    _patch_task_publish(monkeypatch, task, method, fake)
    return calls


@pytest.fixture
def delivery_service(app):
    with app.app_context():
        return DeliveryService()


@pytest.fixture
def order_with_address(db, sample_order, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Yunusobod, Tashkent",
        street_address="Yunusobod street 1",
        latitude=41.3111,
        longitude=69.2797,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    sample_order.delivery_address_id = address.id
    sample_order.delivery_date = datetime.now(UTC) + timedelta(days=1)
    db.session.commit()
    return sample_order


@pytest.mark.unit
@pytest.mark.delivery
class TestDeliveryServiceBusinessRules:
    def test_create_delivery_rejects_when_delivery_already_exists(self, delivery_service, order_with_address, db):
        existing = Delivery(
            order_id=order_with_address.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(existing)
        db.session.commit()

        with pytest.raises(ValidationError, match="Delivery already exists"):
            delivery_service.create_delivery(order_with_address.id)

    def test_create_delivery_rejects_out_of_range_address(self, delivery_service, order_with_address, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.delivery_service.calculate_distance",
            lambda *_args, **_kwargs: delivery_service.max_delivery_distance + 1,
        )

        with pytest.raises(DeliveryError, match="outside our delivery range"):
            delivery_service.create_delivery(order_with_address.id)

    def test_create_delivery_succeeds_for_in_range_address(self, delivery_service, order_with_address, monkeypatch, db):
        monkeypatch.setattr("business_app.services.delivery_service.calculate_distance", lambda *_a, **_k: 5.25)
        monkeypatch.setattr(delivery_service, "_schedule_delivery_assignment", lambda *_a, **_k: None)

        delivery = delivery_service.create_delivery(order_with_address.id, delivery_type=DeliveryType.STANDARD)
        db.session.refresh(delivery)

        assert delivery.order_id == order_with_address.id
        assert delivery.status == DeliveryStatus.SCHEDULED
        assert delivery.distance_km == pytest.approx(5.25, rel=1e-3)

    def test_create_delivery_enqueues_only_the_evaluator_not_the_broadcast(
        self, delivery_service, order_with_address, monkeypatch, db
    ):
        """route-UX plan 2026-08-11 Task 13 (spec §10 fix): create_delivery
        must enqueue ONLY the diversion evaluator for a freshly-pooled
        (unassigned) delivery -- the evaluator is itself responsible for the
        broadcast fan-out (business_app/tasks/delivery_tasks.py). Re-adding
        a second, direct `notify_staff_new_order.delay(order_id)` call here
        reproduces the §10 bug where the best-fit driver gets two messages
        with two Accept buttons for the same order. Prior to this test the
        whole suite stayed green with that duplicate enqueue re-added."""
        monkeypatch.setattr("business_app.services.delivery_service.calculate_distance", lambda *_a, **_k: 5.25)
        monkeypatch.setattr(delivery_service, "_schedule_delivery_assignment", lambda *_a, **_k: None)

        with patch(
            "business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay"
        ) as evaluator_delay, patch(
            "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
        ) as broadcast_delay:
            delivery = delivery_service.create_delivery(order_with_address.id, delivery_type=DeliveryType.STANDARD)

        assert delivery.delivery_person_id is None
        evaluator_delay.assert_called_once_with(delivery.id)
        broadcast_delay.assert_not_called()

    def test_create_delivery_falls_back_to_broadcast_when_evaluator_enqueue_fails(
        self, delivery_service, order_with_address, monkeypatch, db
    ):
        """If enqueuing the evaluator itself fails (e.g. a broker hiccup),
        create_delivery must fall back to broadcasting directly so the pool
        order is never invisible."""
        monkeypatch.setattr("business_app.services.delivery_service.calculate_distance", lambda *_a, **_k: 5.25)
        monkeypatch.setattr(delivery_service, "_schedule_delivery_assignment", lambda *_a, **_k: None)

        with patch(
            "business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay",
            side_effect=RuntimeError("broker down"),
        ) as evaluator_delay, patch(
            "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
        ) as broadcast_delay:
            delivery = delivery_service.create_delivery(order_with_address.id, delivery_type=DeliveryType.STANDARD)

        evaluator_delay.assert_called_once_with(delivery.id)
        broadcast_delay.assert_called_once_with(order_with_address.id)

    def test_create_delivery_stamps_the_row_it_creates(self, delivery_service, order_with_address, monkeypatch, db):
        """Characterisation: what a freshly released row carries today. It must
        read the same once the stamping moves into `stamp_schedule`."""
        monkeypatch.setattr("business_app.services.delivery_service.calculate_distance", lambda *_a, **_k: 5.25)
        order_with_address.delivery_date = date(2026, 9, 25)
        order_with_address.delivery_window_start = None
        order_with_address.delivery_window_end = time(10, 0)
        db.session.commit()

        delivery = delivery_service.create_delivery(order_with_address.id)
        db.session.refresh(delivery)

        assert delivery.status == DeliveryStatus.SCHEDULED
        assert delivery.scheduled_date.date() == date(2026, 9, 25)
        assert delivery.scheduled_time_slot == "until 10:00"
        assert delivery.estimated_delivery_time is not None

    def test_get_available_time_slots_filters_by_capacity(self, delivery_service, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.delivery_service.get_time_slots",
            lambda: ["09:00-12:00", "14:00-17:00", "18:00-21:00"],
        )
        monkeypatch.setattr(
            delivery_service,
            "_check_slot_capacity",
            lambda _date, slot: slot != "14:00-17:00",
        )

        slots = delivery_service.get_available_time_slots(datetime.now(UTC).date(), DeliveryType.STANDARD)

        assert slots == ["09:00-12:00", "18:00-21:00"]

    def test_get_delivery_zones_returns_normalized_structure(self, delivery_service):
        zones = delivery_service.get_delivery_zones()
        assert zones
        assert all("name" in zone for zone in zones)
        assert all("max_distance_km" in zone for zone in zones)
        assert all("fee" in zone for zone in zones)
        assert all("estimated_time_minutes" in zone for zone in zones)

    def test_update_delivery_status_rejects_invalid_transition(self, delivery_service, order_with_address, db):
        delivery = Delivery(
            order_id=order_with_address.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        with pytest.raises(ValidationError, match="Cannot change status"):
            delivery_service.update_delivery_status(delivery.id, DeliveryStatus.ASSIGNED)

    def test_calculate_delivery_fee_current_policy_is_free(self, delivery_service):
        assert delivery_service.calculate_delivery_fee(41.30, 69.24, order_total=10000) == 0
        assert delivery_service.calculate_delivery_fee(41.30, 69.24, order_total=100000) == 0

    def test_cancel_delivery_marks_delivery_cancelled(self, delivery_service, order_with_address, db):
        delivery = Delivery(
            order_id=order_with_address.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        cancelled = delivery_service.cancel_delivery(delivery.id, reason="Order cancelled by customer")

        db.session.refresh(delivery)
        history = DeliveryStatusHistory.query.filter_by(delivery_id=delivery.id).order_by(DeliveryStatusHistory.id.asc()).all()

        assert cancelled.status == DeliveryStatus.CANCELLED
        assert delivery.status == DeliveryStatus.CANCELLED
        assert delivery.delivery_notes == "Order cancelled by customer"
        assert history[-1].new_status == DeliveryStatus.CANCELLED
        assert history[-1].notes == "Order cancelled by customer"

    def test_update_delivery_status_enqueues_notification_with_history_id(
        self, delivery_service, order_with_address, sample_user, db
    ):
        # ARCH-006: any delivery already past SCHEDULED must have a person on file.
        delivery = Delivery(
            order_id=order_with_address.id,
            status=DeliveryStatus.IN_TRANSIT,
            delivery_person_id=sample_user.id,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        with patch("business_app.tasks.notification_tasks.send_delivery_update_task.delay") as delay_mock:
            delivery_service.update_delivery_status(delivery.id, DeliveryStatus.ARRIVED)

        history = (
            DeliveryStatusHistory.query
            .filter_by(delivery_id=delivery.id, new_status=DeliveryStatus.ARRIVED)
            .order_by(DeliveryStatusHistory.id.desc())
            .first()
        )

        assert history is not None
        delay_mock.assert_called_once_with(history.id)


@pytest.mark.unit
@pytest.mark.delivery
class TestStampSchedule:
    """`stamp_schedule` is the ONE writer of a delivery's three schedule columns
    (spec §3.3). Create, reschedule and release all go through it, so the three
    admin pages that read three different columns cannot disagree."""

    NOW = datetime(2026, 9, 23, 6, 30, tzinfo=UTC)

    @staticmethod
    def _row(order, status, **extra):
        return Delivery(order_id=order.id, status=status, distance_km=5.25, **extra)

    def test_a_pool_row_gets_the_orders_day_and_window_and_an_eta_from_now(
        self, delivery_service, order_with_address
    ):
        order_with_address.delivery_date = date(2026, 9, 25)
        order_with_address.delivery_window_start = time(12, 0)
        order_with_address.delivery_window_end = time(18, 0)
        delivery = self._row(order_with_address, DeliveryStatus.SCHEDULED)

        delivery_service.stamp_schedule(delivery, order_with_address, now=self.NOW)

        assert delivery.scheduled_date == date(2026, 9, 25)
        assert delivery.scheduled_time_slot == "12:00-18:00"
        # Anchored on the injected `now`, not on the wall clock.
        assert delivery.estimated_delivery_time == delivery_service._calculate_estimated_delivery_time(
            5.25, DeliveryType.STANDARD, now=self.NOW
        )
        assert delivery.estimated_delivery_time > self.NOW

    def test_a_held_row_carries_no_eta_even_if_it_had_one(self, delivery_service, order_with_address):
        """A NULL ETA keeps the customer's Track screen from counting down hours to
        a day that has not come (spec §3.3)."""
        order_with_address.delivery_date = date(2026, 9, 25)
        order_with_address.delivery_window_start = None
        order_with_address.delivery_window_end = time(10, 0)
        delivery = self._row(
            order_with_address,
            DeliveryStatus.RESCHEDULED,
            estimated_delivery_time=self.NOW + timedelta(hours=2),
        )

        delivery_service.stamp_schedule(delivery, order_with_address, now=self.NOW)

        assert delivery.estimated_delivery_time is None
        assert delivery.scheduled_date == date(2026, 9, 25)
        assert delivery.scheduled_time_slot == "until 10:00"

    def test_an_undated_order_is_scheduled_for_now(self, delivery_service, order_with_address):
        order_with_address.delivery_date = None
        order_with_address.delivery_window_start = None
        order_with_address.delivery_window_end = None
        delivery = self._row(order_with_address, DeliveryStatus.PENDING)

        delivery_service.stamp_schedule(delivery, order_with_address, now=self.NOW)

        assert delivery.scheduled_date == self.NOW
        assert delivery.scheduled_time_slot == "anytime"
        assert delivery.estimated_delivery_time == delivery_service._calculate_estimated_delivery_time(
            5.25, DeliveryType.STANDARD, now=self.NOW
        )


@pytest.mark.unit
@pytest.mark.delivery
class TestOfferToDrivers:
    """`offer_to_drivers` is `create_delivery`'s fan-out, moved out whole so a
    released held row and a reschedule that lands in the pool reach drivers
    exactly as a fresh order does (spec §3.3)."""

    @staticmethod
    def _pool_row(db, order):
        delivery = Delivery(
            order_id=order.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="anytime",
        )
        db.session.add(delivery)
        db.session.commit()
        return delivery

    def test_arms_auto_assign_and_enqueues_only_the_evaluator(
        self, delivery_service, order_with_address, db, monkeypatch
    ):
        auto_assign = _task_spy(monkeypatch, delivery_tasks.auto_assign_delivery_task, "apply_async")
        evaluator = _task_spy(monkeypatch, delivery_tasks.evaluate_pool_insertion_suggestions_task, "delay")
        broadcast = _task_spy(monkeypatch, staff_tasks.notify_staff_new_order, "delay")
        delivery = self._pool_row(db, order_with_address)

        delivery_service.offer_to_drivers(delivery)

        assert auto_assign == [((delivery.id,), {}, {"countdown": 300})]
        assert evaluator == [((delivery.id,), {})]
        # The evaluator enqueues the broadcast itself (§10 duplicate-message bug).
        assert broadcast == []

    def test_falls_back_to_a_direct_broadcast_when_the_evaluator_cannot_be_enqueued(
        self, delivery_service, order_with_address, db, monkeypatch
    ):
        _task_spy(monkeypatch, delivery_tasks.auto_assign_delivery_task, "apply_async")

        def broker_down(*_args, **_kwargs):
            raise RuntimeError("broker down")

        _patch_task_publish(monkeypatch, delivery_tasks.evaluate_pool_insertion_suggestions_task, "delay", broker_down)
        broadcast = _task_spy(monkeypatch, staff_tasks.notify_staff_new_order, "delay")
        delivery = self._pool_row(db, order_with_address)

        delivery_service.offer_to_drivers(delivery)

        assert broadcast == [((order_with_address.id,), {})]

    def test_an_auto_assign_broker_failure_still_offers_the_row(
        self, delivery_service, order_with_address, db, monkeypatch
    ):
        def broker_down(*_args, **_kwargs):
            raise RuntimeError("broker down")

        _patch_task_publish(monkeypatch, delivery_tasks.auto_assign_delivery_task, "apply_async", broker_down)
        evaluator = _task_spy(monkeypatch, delivery_tasks.evaluate_pool_insertion_suggestions_task, "delay")
        delivery = self._pool_row(db, order_with_address)

        delivery_service.offer_to_drivers(delivery)

        assert evaluator == [((delivery.id,), {})]
