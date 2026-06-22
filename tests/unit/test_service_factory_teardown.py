"""Unit tests for the Flask teardown exception filter in service_factory.

The app-context teardown hook logged ``"Request ended with exception: ..."`` for
ANY exception unwinding the context — including Celery control-flow signals
(Retry / Ignore / Reject) raised when a task reschedules itself. That surfaced
benign back-off (e.g. ``auto_assign_delivery_task``'s no-driver 900s retry) as a
misleading WARNING. ``_is_loggable_teardown_exception`` keeps real exceptions
logged while filtering out Celery control-flow signals.
"""

import pytest
from celery.exceptions import Ignore, Reject, Retry

from business_app.utils.service_factory import _is_loggable_teardown_exception


@pytest.mark.unit
def test_none_is_not_logged():
    assert _is_loggable_teardown_exception(None) is False


@pytest.mark.unit
@pytest.mark.parametrize("exc", [Retry(), Ignore(), Reject()])
def test_celery_control_flow_signals_are_not_logged(exc):
    assert _is_loggable_teardown_exception(exc) is False


@pytest.mark.unit
@pytest.mark.parametrize("exc", [ValueError("boom"), RuntimeError("kaboom"), Exception("x")])
def test_real_exceptions_are_logged(exc):
    assert _is_loggable_teardown_exception(exc) is True
