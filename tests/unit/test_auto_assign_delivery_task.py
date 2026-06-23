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
