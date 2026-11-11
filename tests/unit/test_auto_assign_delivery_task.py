"""Unit tests for the retry/exception flow of `auto_assign_delivery_task`.

Regression guard for the retry anti-pattern where `raise self.retry(...)` on
the no-driver path sat inside the blanket `except Exception` block, so the
`celery.exceptions.Retry` control-flow exception was logged as ERROR and
`self.retry` was invoked a second time; exhausted retries then surfaced as a
`MaxRetriesExceededError` storm instead of one clean warning.

We call the raw task function with a mocked `self` (the task is `bind=True`)
so we can script `self.retry` behavior: in a real worker `self.retry()`
raises `Retry` while scheduling the next run, and raises
`MaxRetriesExceededError` once retries are exhausted.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry

from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from shared.enums import DeliveryStatus

# Raw (unbound) function behind the bound task so `self` can be a MagicMock.
_run_task = auto_assign_delivery_task.run.__func__

TASKS_MODULE = "business_app.tasks.delivery_tasks"


def _mock_self(retry_side_effect):
    mock_self = MagicMock(name="task_self")
    mock_self.retry.side_effect = retry_side_effect
    return mock_self


def _patch_no_driver_scenario():
    """Patch task-module collaborators: delivery is SCHEDULED, zero drivers."""
    delivery = MagicMock(name="delivery")
    delivery.status = DeliveryStatus.SCHEDULED

    delivery_patch = patch(f"{TASKS_MODULE}.Delivery")
    person_patch = patch(f"{TASKS_MODULE}.DeliveryPerson")
    service_patch = patch(f"{TASKS_MODULE}.DeliveryService")
    return delivery, delivery_patch, person_patch, service_patch


@pytest.mark.unit
@pytest.mark.delivery
class TestAutoAssignDeliveryTaskRetryFlow:
    def test_no_driver_retry_propagates_untouched(self):
        """The Retry raised by self.retry(countdown=900) must escape the task
        without being re-caught by the blanket except (which would call
        self.retry a second time and log an ERROR)."""
        delivery, delivery_patch, person_patch, service_patch = _patch_no_driver_scenario()
        with delivery_patch as MockDelivery, person_patch as MockPerson, service_patch:
            MockDelivery.query.with_for_update.return_value.get.return_value = delivery
            MockPerson.query.filter.return_value.all.return_value = []

            mock_self = _mock_self(Retry("Retry in 900s"))

            with pytest.raises(Retry):
                _run_task(mock_self, 42)

            mock_self.retry.assert_called_once_with(countdown=900)

    def test_no_driver_max_retries_returns_clean_result(self):
        """When retries are exhausted on the no-driver path the task must
        return a failure dict instead of crashing with MaxRetriesExceededError."""
        delivery, delivery_patch, person_patch, service_patch = _patch_no_driver_scenario()
        with delivery_patch as MockDelivery, person_patch as MockPerson, service_patch:
            MockDelivery.query.with_for_update.return_value.get.return_value = delivery
            MockPerson.query.filter.return_value.all.return_value = []

            mock_self = _mock_self(MaxRetriesExceededError())

            result = _run_task(mock_self, 42)

            assert result == {"success": False, "error": "no_available_drivers_max_retries"}
            mock_self.retry.assert_called_once_with(countdown=900)

    def test_generic_exception_retries_once_with_original_exc(self):
        """A real failure goes through self.retry(exc=...) exactly once and the
        resulting Retry propagates so Celery schedules the next attempt."""
        boom = RuntimeError("db exploded")
        with patch(f"{TASKS_MODULE}.Delivery") as MockDelivery:
            MockDelivery.query.with_for_update.side_effect = boom

            mock_self = _mock_self(Retry("Retry in 300s"))

            with pytest.raises(Retry):
                _run_task(mock_self, 42)

            mock_self.retry.assert_called_once()
            assert mock_self.retry.call_args.kwargs["exc"] is boom

    def test_generic_exception_max_retries_reraises_original_exc(self):
        """When retries are exhausted the task must fail with the ORIGINAL
        exception, not a MaxRetriesExceededError wrapper."""
        boom = RuntimeError("db exploded")
        with patch(f"{TASKS_MODULE}.Delivery") as MockDelivery:
            MockDelivery.query.with_for_update.side_effect = boom

            mock_self = _mock_self(MaxRetriesExceededError())

            with pytest.raises(RuntimeError, match="db exploded"):
                _run_task(mock_self, 42)

            mock_self.retry.assert_called_once()

    def test_cod_blocked_driver_skipped_for_cash_order(self):
        """A COD-blocked driver must be skipped when auto-assigning a CASH order.

        Setup: one available driver who is COD-blocked; order payment_method=CASH.
        Expected: no driver is found → task retries (no assignment made).
        """
        from shared.enums import PaymentMethod

        # Build a realistic delivery mock: SCHEDULED status, CASH payment order.
        delivery = MagicMock(name="delivery")
        delivery.status = DeliveryStatus.SCHEDULED
        order = MagicMock(name="order")
        order.payment_method = PaymentMethod.CASH
        delivery.order = order

        # Build one available driver.
        driver = MagicMock(name="driver")
        driver.user_id = 99
        driver.is_working_now = True

        recon_path = "business_app.services.driver_reconciliation_service.DriverReconciliationService"

        with (
            patch(f"{TASKS_MODULE}.Delivery") as MockDelivery,
            patch(f"{TASKS_MODULE}.DeliveryPerson") as MockPerson,
            patch(f"{TASKS_MODULE}.DeliveryService"),
            patch(recon_path) as MockRecon,
        ):
            MockDelivery.query.with_for_update.return_value.get.return_value = delivery
            # Query returns the one driver; is_working_now=True so it enters available list.
            MockPerson.query.filter.return_value.all.return_value = [driver]

            # The driver is COD-blocked.
            mock_recon_instance = MagicMock()
            mock_recon_instance.is_driver_blocked_from_cod.return_value = True
            MockRecon.return_value = mock_recon_instance

            # Retry raises Retry on first call (no-driver path).
            mock_self = _mock_self(Retry("Retry in 900s"))

            with pytest.raises(Retry):
                _run_task(mock_self, 42)

            # The COD check was invoked for driver 99.
            mock_recon_instance.is_driver_blocked_from_cod.assert_called_once_with(driver.user_id)
            # self.retry was called with countdown (no-driver path), not exc (exception path).
            mock_self.retry.assert_called_once_with(countdown=900)


# ---------------------------------------------------------------------------
# Bottle-aware candidate filter (spec A8): real rows, real services. Only the driver push and the route rebuild,
# which talk to Telegram and the maps provider, are faked.
# ---------------------------------------------------------------------------
from celery.exceptions import Retry as _Retry  # noqa: E402

from business_app.models.bottle import DriverBottleSession  # noqa: E402
from business_app.models.delivery import Delivery  # noqa: E402
from business_app.models.order import Order  # noqa: E402
from business_app.services.bottle_tracking_service import BottleTrackingService  # noqa: E402
from business_app.services.delivery_service import DeliveryService  # noqa: E402
from shared.enums import DriverBottleSessionStatus, OrderStatus  # noqa: E402
from tests.integration.eta_plan_builders import make_driver  # noqa: E402
from tests.unit.test_staff_error_endpoints import _assigned_delivery, _order_with_bottles  # noqa: E402

ALL_DAY = ("00:00", "23:59")  # on shift whatever the hour the suite runs


class _RetrySpy:
    """`Task.retry`'s own signature; raises `Retry` the way a worker does, and remembers every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, args=None, kwargs=None, exc=None, throw=True, eta=None, countdown=None, max_retries=None, **opts):
        self.calls.append({"exc": exc, "countdown": countdown})
        raise _Retry("retry")


@pytest.fixture
def retry_spy(monkeypatch):
    spy = _RetrySpy()
    monkeypatch.setattr(auto_assign_delivery_task, "retry", spy)
    return spy


@pytest.fixture(autouse=False)
def no_driver_push(monkeypatch):
    monkeypatch.setattr(DeliveryService, "_notify_driver", lambda self, delivery: None)
    monkeypatch.setattr(DeliveryService, "_optimize_driver_route", lambda self, *a, **k: None)


def _shift_driver(db):
    return make_driver(db, roster=ALL_DAY)


def _open_session(db, driver, loaded, collected=0):
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=loaded,
        bottles_collected_from_customers=collected,
        status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(session)
    db.session.commit()
    return session


def _pool_order(db, customer, product, quantity):
    order = _order_with_bottles(db, customer, product, quantity=quantity)
    order.status = OrderStatus.CONFIRMED
    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.SCHEDULED,
        scheduled_date=datetime.now(UTC) + timedelta(hours=1),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _run(delivery_id):
    return auto_assign_delivery_task.run(delivery_id)


@pytest.mark.unit
class TestAutoAssignBottleFilter:
    def test_skips_driver_with_only_empties(self, app, db, no_driver_push, sample_user, sample_product):
        a, b = _shift_driver(db), _shift_driver(db)
        _open_session(db, a, loaded=3, collected=15)  # 18 on the truck, 3 full
        _open_session(db, b, loaded=10)
        delivery = _pool_order(db, sample_user, sample_product, quantity=8)

        result = _run(delivery.id)

        assert result["success"] is True
        assert result["driver_id"] == b.id
        assert Delivery.query.get(delivery.id).delivery_person_id == b.id

    def test_on_the_way_orders_reduce_free_full(self, app, db, retry_spy, sample_user, sample_product):
        a = _shift_driver(db)
        _open_session(db, a, loaded=10)
        _assigned_delivery(
            db, _order_with_bottles(db, sample_user, sample_product, quantity=4), a, status=DeliveryStatus.IN_TRANSIT
        )  # 6 free full left
        delivery = _pool_order(db, sample_user, sample_product, quantity=8)

        result = _run(delivery.id)

        assert result == {"success": False, "delivery_id": delivery.id, "error": "no_driver_with_enough_bottles"}
        assert Delivery.query.get(delivery.id).delivery_person_id is None

    def test_no_driver_with_bottles_gives_reason_without_retry(self, app, db, retry_spy, sample_user, sample_product):
        a = _shift_driver(db)
        _open_session(db, a, loaded=3, collected=15)
        delivery = _pool_order(db, sample_user, sample_product, quantity=8)

        result = _run(delivery.id)

        assert result == {"success": False, "delivery_id": delivery.id, "error": "no_driver_with_enough_bottles"}
        assert retry_spy.calls == []

    def test_driver_without_a_session_is_not_a_candidate(self, app, db, retry_spy, sample_user, sample_product):
        _shift_driver(db)  # on shift, never loaded
        delivery = _pool_order(db, sample_user, sample_product, quantity=2)

        result = _run(delivery.id)

        assert result == {"success": False, "delivery_id": delivery.id, "error": "no_driver_with_enough_bottles"}
        assert retry_spy.calls == []

    def test_no_candidates_at_all_still_retries(self, app, db, retry_spy, sample_user, sample_product):
        delivery = _pool_order(db, sample_user, sample_product, quantity=2)  # nobody on the roster

        with pytest.raises(_Retry):
            _run(delivery.id)

        assert retry_spy.calls == [{"exc": None, "countdown": 900}]

    def test_race_refusal_is_treated_as_shortfall(
        self, app, db, retry_spy, no_driver_push, monkeypatch, sample_user, sample_product
    ):
        """The filter saw 10 free; by the time the locked assign judged it only 3 were left: same answer, no retry."""
        a = _shift_driver(db)
        _open_session(db, a, loaded=3)
        delivery = _pool_order(db, sample_user, sample_product, quantity=8)
        monkeypatch.setattr(
            BottleTrackingService, "free_full_bottles", lambda self, driver_user_id, *, exclude_delivery_id=None: 10
        )

        result = _run(delivery.id)

        assert result == {"success": False, "delivery_id": delivery.id, "error": "no_driver_with_enough_bottles"}
        assert retry_spy.calls == []
        assert Delivery.query.get(delivery.id).delivery_person_id is None

    def test_zero_bottle_order_still_auto_assigns_without_session(
        self, app, db, no_driver_push, sample_user, sample_product
    ):
        a = _shift_driver(db)  # no session at all
        delivery = _pool_order(db, sample_user, sample_product, quantity=2)
        sample_product.tracks_returnable_bottles = False
        sample_product.returnable_bottles_per_unit = 0
        db.session.commit()
        assert BottleTrackingService.calculate_bottles_for_order(Order.query.get(delivery.order_id)) == 0

        result = _run(delivery.id)

        assert result["success"] is True and result["driver_id"] == a.id
        assert Delivery.query.get(delivery.id).delivery_person_id == a.id


@pytest.mark.unit
def test_pool_offer_skips_driver_who_cannot_carry(app, db, monkeypatch, sample_user, sample_product):
    from business_app.services.route_optimization_service import RouteOptimizationService
    from business_app.tasks.delivery_tasks import evaluate_pool_insertion_suggestions_task
    from business_app.tasks.staff_tasks import notify_staff_new_order

    a, b = _shift_driver(db), _shift_driver(db)
    _open_session(db, a, loaded=3, collected=15)
    _open_session(db, b, loaded=10)
    delivery = _pool_order(db, sample_user, sample_product, quantity=8)

    def gain(self, driver_id, delivery_id):  # both would gain from the diversion; only one can carry the water
        return {"gain_minutes": 20.0, "committed_order_number": "ORD-X"}

    offers = []

    def offer(driver_id, delivery_id, order_no, detour_km, detour_minutes, gain_minutes, committed_order_number):
        offers.append(driver_id)
        return True

    monkeypatch.setattr(RouteOptimizationService, "compute_diversion_gain", gain)
    monkeypatch.setattr("business_app.utils.bot_webhook.notify_pool_insertion_suggestion", offer)
    monkeypatch.setattr(notify_staff_new_order, "delay", lambda order_id, exclude_driver_user_id=None: None)

    result = evaluate_pool_insertion_suggestions_task.run(delivery.id)

    assert offers == [b.id]
    assert result["suggested"] is True and result["driver_id"] == b.id
