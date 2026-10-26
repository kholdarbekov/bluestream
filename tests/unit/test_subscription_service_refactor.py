"""Unit tests for user-scoped methods added to SubscriptionService."""

from datetime import UTC, datetime

from business_app.services.subscription_service import SubscriptionService
from shared.enums import SubscriptionStatus


def test_serialize_for_log_handles_enums_and_datetimes():
    now = datetime.now(UTC)

    assert SubscriptionService._serialize_for_log(SubscriptionStatus.ACTIVE) == "active"
    assert SubscriptionService._serialize_for_log(now) == now.isoformat()
    assert SubscriptionService._serialize_for_log("plain") == "plain"
