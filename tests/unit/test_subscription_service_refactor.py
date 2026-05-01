"""Unit tests for user-scoped methods added to SubscriptionService."""

from datetime import UTC, datetime

import pytest

from business_app.services.subscription_service import SubscriptionService
from shared.enums import SubscriptionStatus
from business_app.utils.exceptions import ValidationError


class _FakeSubscription:
    def __init__(self, status, failed_billing_attempts):
        self.status = status
        self.failed_billing_attempts = failed_billing_attempts


def _service_without_init():
    return SubscriptionService.__new__(SubscriptionService)


def test_validate_retry_billing_rejects_non_active_subscription():
    service = _service_without_init()
    subscription = _FakeSubscription(
        status=SubscriptionStatus.PAUSED,
        failed_billing_attempts=2,
    )
    service._get_user_subscription_or_raise = lambda *_: subscription

    with pytest.raises(ValidationError) as exc:
        service.validate_retry_billing_for_user(1, 2)

    assert exc.value.message == "api.subscriptions.error.only_active_retry"


def test_validate_retry_billing_rejects_when_no_failed_attempts():
    service = _service_without_init()
    subscription = _FakeSubscription(
        status=SubscriptionStatus.ACTIVE,
        failed_billing_attempts=0,
    )
    service._get_user_subscription_or_raise = lambda *_: subscription

    with pytest.raises(ValidationError) as exc:
        service.validate_retry_billing_for_user(1, 2)

    assert exc.value.message == "api.subscriptions.error.no_failed_billing_to_retry"


def test_validate_retry_billing_returns_subscription_when_valid():
    service = _service_without_init()
    subscription = _FakeSubscription(
        status=SubscriptionStatus.ACTIVE,
        failed_billing_attempts=3,
    )
    service._get_user_subscription_or_raise = lambda *_: subscription

    result = service.validate_retry_billing_for_user(1, 2)

    assert result is subscription


def test_serialize_for_log_handles_enums_and_datetimes():
    now = datetime.now(UTC)

    assert SubscriptionService._serialize_for_log(SubscriptionStatus.ACTIVE) == "active"
    assert SubscriptionService._serialize_for_log(now) == now.isoformat()
    assert SubscriptionService._serialize_for_log("plain") == "plain"
