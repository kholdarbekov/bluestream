"""Unit tests for the log_service_call decorator's exception classification.

Expected business exceptions (ValidationError, NotFoundError, etc.) are routine
client-facing rejections — they must be logged at WARNING without a traceback,
matching the API error-handler's classification. Only unexpected exceptions
warrant ERROR + traceback. Mirrors a prod noise issue where admin order-form
validation rejections (min order quantity / amount) flooded the error logs with
full tracebacks via this decorator.
"""

from unittest.mock import MagicMock, patch

import pytest

from business_app.utils import service_logging
from business_app.utils.exceptions import ValidationError, NotFoundError


class _Svc:
    @service_logging.log_service_call(operation_type="order")
    def reject_validation(self):
        raise ValidationError("minimum order quantity is 3 (you ordered 2)")

    @service_logging.log_service_call(operation_type="order")
    def reject_not_found(self):
        raise NotFoundError("Order not found")

    @service_logging.log_service_call(operation_type="order")
    def blow_up(self):
        raise RuntimeError("unexpected boom")


@pytest.mark.unit
def test_expected_business_exception_logged_at_warning_without_traceback():
    mock_pl = MagicMock()
    with patch.object(service_logging, "performance_logger", mock_pl), patch.object(
        service_logging, "app_metrics", MagicMock()
    ):
        with pytest.raises(ValidationError):
            _Svc().reject_validation()

    # Routine rejection → WARNING, never ERROR, and no exc_info traceback dump.
    mock_pl.logger.error.assert_not_called()
    assert mock_pl.logger.warning.called
    _, warn_kwargs = mock_pl.logger.warning.call_args
    assert not warn_kwargs.get("exc_info")


@pytest.mark.unit
def test_not_found_is_also_treated_as_expected_warning():
    mock_pl = MagicMock()
    with patch.object(service_logging, "performance_logger", mock_pl), patch.object(
        service_logging, "app_metrics", MagicMock()
    ):
        with pytest.raises(NotFoundError):
            _Svc().reject_not_found()

    mock_pl.logger.error.assert_not_called()
    assert mock_pl.logger.warning.called


@pytest.mark.unit
def test_unexpected_exception_logged_at_error_with_traceback():
    mock_pl = MagicMock()
    with patch.object(service_logging, "performance_logger", mock_pl), patch.object(
        service_logging, "app_metrics", MagicMock()
    ):
        with pytest.raises(RuntimeError):
            _Svc().blow_up()

    # Genuine fault → ERROR with a full traceback for diagnosis.
    assert mock_pl.logger.error.called
    _, err_kwargs = mock_pl.logger.error.call_args
    assert err_kwargs.get("exc_info") is True
    mock_pl.logger.warning.assert_not_called()
