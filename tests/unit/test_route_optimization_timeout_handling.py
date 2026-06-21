"""Regression: optimize_driver_route_task soft-time-limit handling + budgets.

Prod incident (02:02 TimeLimitExceeded(120)): the task's hard limit was an
anomalously tight 120s (soft 100s). The route optimizer does sequential
external geocode/matrix I/O, and a stalled map call blew past 120s -> the
worker was SIGKILLed mid-commit. The blanket ``except Exception`` also caught
the SoftTimeLimitExceeded and turned it into a blind ``self.retry`` that just
re-ran the same slow I/O.

The fix:
  - Bumped budgets to time_limit=300 / soft_time_limit=270.
  - Added an explicit ``except SoftTimeLimitExceeded`` that rolls back and
    returns ``{"optimized": False, "reason": "time_budget_exceeded"}`` instead
    of retrying.

These tests complement the two existing cases in
``test_route_optimization_tasks.py`` (do NOT edit that file). They drive the
soft-limit and generic-exception paths through both the in-context entrypoint
and the raw bound function with a MagicMock ``self`` (so ``self.retry`` can be
scripted), mirroring ``test_auto_assign_delivery_task.py``.
"""

from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import MaxRetriesExceededError, Retry, SoftTimeLimitExceeded

from business_app.tasks.delivery_tasks import optimize_driver_route_task
from shared.enums import UserRole, UserType

# Raw (unbound) function behind the bound task so `self` can be a MagicMock.
_run_task = optimize_driver_route_task.run.__func__

RO_SERVICE_PATH = "business_app.services.route_optimization_service.RouteOptimizationService"


def _mock_self(retry_side_effect=None):
    mock_self = MagicMock(name="task_self")
    if retry_side_effect is not None:
        mock_self.retry.side_effect = retry_side_effect
    return mock_self


@pytest.fixture
def driver(db):
    from business_app.models.user import User

    user = User(
        email="ro-timeout-driver@example.com",
        phone="+998901111222",
        password_hash="x",
        first_name="RO",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


# ---------------------------------------------------------------------------
# Decorator budgets — must never regress to the anomalously tight 120s/100s.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestRouteTaskTimeBudgets:
    def test_hard_time_limit_at_least_300(self):
        assert optimize_driver_route_task.time_limit >= 300

    def test_soft_time_limit_at_least_270(self):
        assert optimize_driver_route_task.soft_time_limit >= 270

    def test_soft_limit_strictly_below_hard_limit(self):
        """A soft limit at/above the hard limit is useless — the graceful abort
        would never get a chance to run before the SIGKILL."""
        assert optimize_driver_route_task.soft_time_limit < optimize_driver_route_task.time_limit

    def test_did_not_regress_to_incident_values(self):
        """Explicitly guard the exact values from the 02:02 incident."""
        assert optimize_driver_route_task.time_limit != 120
        assert optimize_driver_route_task.soft_time_limit != 100


# ---------------------------------------------------------------------------
# SoftTimeLimitExceeded -> graceful result, NO retry.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestRouteTaskSoftLimitHandling:
    def test_soft_limit_returns_time_budget_exceeded(self, app, db, driver, monkeypatch):
        """The canonical graceful-abort contract."""

        class _StallService:
            def optimize_for_driver(self, *_a, **_k):
                raise SoftTimeLimitExceeded()

        with app.app_context():
            monkeypatch.setattr(RO_SERVICE_PATH, _StallService)
            result = optimize_driver_route_task.run(driver.id, trigger="auto")

        assert result == {"optimized": False, "reason": "time_budget_exceeded"}

    def test_soft_limit_does_not_call_retry(self):
        """A SoftTimeLimitExceeded must NOT fall through to self.retry — a blind
        retry would re-run the exact same slow I/O. Use a MagicMock self so we
        can prove self.retry was never touched."""

        class _StallService:
            def optimize_for_driver(self, *_a, **_k):
                raise SoftTimeLimitExceeded()

        mock_self = _mock_self(retry_side_effect=Retry("should not happen"))

        with patch(RO_SERVICE_PATH, _StallService), patch(
            "business_app.tasks.delivery_tasks.db"
        ) as mock_db:
            result = _run_task(mock_self, 7, trigger="auto")

        assert result == {"optimized": False, "reason": "time_budget_exceeded"}
        mock_self.retry.assert_not_called()
        # Graceful abort must roll back any half-built session state.
        mock_db.session.rollback.assert_called_once()

    def test_soft_limit_rolls_back_session(self, app, db, driver, monkeypatch):
        """Rollback on the soft-limit path prevents a half-built route leaking
        into a later commit."""

        class _StallService:
            def optimize_for_driver(self, *_a, **_k):
                raise SoftTimeLimitExceeded()

        rollback_spy = MagicMock()
        with app.app_context():
            monkeypatch.setattr(RO_SERVICE_PATH, _StallService)
            monkeypatch.setattr(
                "business_app.tasks.delivery_tasks.db.session.rollback", rollback_spy
            )
            result = optimize_driver_route_task.run(driver.id, trigger="auto")

        assert result == {"optimized": False, "reason": "time_budget_exceeded"}
        rollback_spy.assert_called_once()

    def test_soft_limit_does_not_push_route_webhook(self, app, db, driver, monkeypatch):
        """A timed-out optimization produced no route, so it must not push a
        route-updated webhook to the staff bot."""

        class _StallService:
            def optimize_for_driver(self, *_a, **_k):
                raise SoftTimeLimitExceeded()

        with app.app_context():
            monkeypatch.setattr(RO_SERVICE_PATH, _StallService)
            with patch("business_app.utils.bot_webhook.notify_route_updated") as nru:
                result = optimize_driver_route_task.run(driver.id, trigger="auto")

        assert result == {"optimized": False, "reason": "time_budget_exceeded"}
        nru.assert_not_called()


# ---------------------------------------------------------------------------
# Generic exceptions STILL retry (the soft-limit branch must not swallow them).
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestRouteTaskGenericExceptionRetries:
    def test_generic_exception_goes_through_retry_with_original_exc(self):
        """A real failure (not a soft-limit) must still flow through
        self.retry(exc=...) so Celery schedules the next attempt — the
        soft-limit handling must not have stolen the generic path."""
        boom = RuntimeError("matrix provider exploded")

        class _BoomService:
            def optimize_for_driver(self, *_a, **_k):
                raise boom

        mock_self = _mock_self(retry_side_effect=Retry("Retry scheduled"))

        with patch(RO_SERVICE_PATH, _BoomService), patch(
            "business_app.tasks.delivery_tasks.db"
        ):
            with pytest.raises(Retry):
                _run_task(mock_self, 7, trigger="auto")

        mock_self.retry.assert_called_once()
        assert mock_self.retry.call_args.kwargs["exc"] is boom

    def test_generic_exception_rolls_back_then_retries(self):
        boom = RuntimeError("transient db blip")

        class _BoomService:
            def optimize_for_driver(self, *_a, **_k):
                raise boom

        mock_self = _mock_self(retry_side_effect=Retry("Retry scheduled"))

        with patch(RO_SERVICE_PATH, _BoomService), patch(
            "business_app.tasks.delivery_tasks.db"
        ) as mock_db:
            with pytest.raises(Retry):
                _run_task(mock_self, 7, trigger="auto")

        mock_db.session.rollback.assert_called_once()
        mock_self.retry.assert_called_once()

    def test_generic_exception_max_retries_reraises_original(self):
        """When retries are exhausted, the original exception surfaces (Celery
        raises MaxRetriesExceededError from self.retry, which propagates)."""
        boom = RuntimeError("permanent failure")

        class _BoomService:
            def optimize_for_driver(self, *_a, **_k):
                raise boom

        mock_self = _mock_self(retry_side_effect=MaxRetriesExceededError())

        with patch(RO_SERVICE_PATH, _BoomService), patch(
            "business_app.tasks.delivery_tasks.db"
        ):
            with pytest.raises(MaxRetriesExceededError):
                _run_task(mock_self, 7, trigger="auto")

        mock_self.retry.assert_called_once()

    def test_soft_limit_is_not_treated_as_generic_retry(self):
        """Belt-and-suspenders: when the SAME service raises a soft limit, the
        task must take the time_budget_exceeded branch and NOT the generic
        retry branch — even though SoftTimeLimitExceeded is an Exception
        subclass that the blanket except would otherwise catch."""
        mock_self = _mock_self(retry_side_effect=Retry("should not happen"))

        class _StallService:
            def optimize_for_driver(self, *_a, **_k):
                raise SoftTimeLimitExceeded()

        with patch(RO_SERVICE_PATH, _StallService), patch(
            "business_app.tasks.delivery_tasks.db"
        ):
            result = _run_task(mock_self, 7, trigger="auto")

        assert result == {"optimized": False, "reason": "time_budget_exceeded"}
        mock_self.retry.assert_not_called()


# ---------------------------------------------------------------------------
# Happy/empty path sanity (no retry, no soft-limit) — confirms the normal
# control flow is unaffected by the new soft-limit handling.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestRouteTaskNormalPathUnaffected:
    def test_no_active_deliveries_returns_without_retry(self, app, db, driver):
        with app.app_context():
            with patch("business_app.utils.bot_webhook.notify_route_updated") as nru:
                result = optimize_driver_route_task.run(driver.id)
        assert result == {"optimized": False, "reason": "no_active_deliveries"}
        nru.assert_not_called()

    def test_none_route_does_not_trip_soft_limit_branch(self):
        """When the service legitimately returns None (no deliveries), the
        task returns the no_active_deliveries result — not the timeout one."""

        class _EmptyService:
            def optimize_for_driver(self, *_a, **_k):
                return None

        mock_self = _mock_self()

        with patch(RO_SERVICE_PATH, _EmptyService), patch(
            "business_app.tasks.delivery_tasks.db"
        ):
            result = _run_task(mock_self, 7, trigger="auto")

        assert result == {"optimized": False, "reason": "no_active_deliveries"}
        mock_self.retry.assert_not_called()
