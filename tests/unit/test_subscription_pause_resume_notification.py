"""Regression: subscription pause/resume and notifications must use real model
fields (the model was refactored: frequency→billing_cycle, total_amount→
billing_amount, plan→name, pause_until→pause_end_date, resumed_at→resume_date).

resume_subscription read ``subscription.frequency`` → AttributeError (crash);
pause wrote ``subscription.pause_until`` (phantom, silently lost); and
NotificationService.send_subscription_notification read plan/frequency/
total_amount (all phantom) → crash whenever a subscription notification fired.
(`frequency` is a real column again since the single-frequency change, plan 2026-10-24.)
"""

from datetime import UTC, date, datetime, timedelta
from business_app.utils.local_windows import local_date
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.subscription import Subscription
from business_app.models.user import UserAddress
from business_app.services.notification_service import NotificationService
from business_app.services.subscription_service import SubscriptionService
from shared.enums import PaymentMethod, SubscriptionFrequency, SubscriptionStatus


def _make_subscription(db, user, *, status, number):
    addr = UserAddress(
        user_id=user.id, title="h", full_address="addr", street_address="addr",
        latitude=41.3, longitude=69.25,
    )
    db.session.add(addr)
    db.session.flush()
    sub = Subscription(
        subscription_number=number,
        user_id=user.id,
        status=status,
        name="Standard Weekly",
        frequency=SubscriptionFrequency.WEEKLY,
        billing_amount=Decimal("50000.00"),
        next_delivery_date=(local_date() + timedelta(days=7)),
        delivery_address_id=addr.id,
        payment_method=PaymentMethod.CARD,
        start_date=datetime.now(UTC),
    )
    if status == SubscriptionStatus.PAUSED:
        sub.paused_at = datetime.now(UTC)
    db.session.add(sub)
    db.session.commit()
    return sub


@pytest.mark.unit
class TestSubscriptionPauseResume:
    """The live, user-scoped pause and resume (the legacy uncalled copies are gone)."""

    def test_pause_records_the_pause(self, app, db, sample_user):
        with app.app_context():
            sub = _make_subscription(db, sample_user, status=SubscriptionStatus.ACTIVE, number="SUB-P1")

            SubscriptionService().pause_subscription_for_user(sub.id, sample_user.id, "Away")

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.PAUSED
            assert (refreshed.pause_reason, refreshed.paused_at is not None) == ("Away", True)

    def test_resume_dates_the_next_delivery_without_crashing(self, app, db, sample_user):
        with app.app_context():
            sub = _make_subscription(db, sample_user, status=SubscriptionStatus.PAUSED, number="SUB-R1")

            with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"):
                SubscriptionService().resume_subscription_for_user(sub.id, sample_user.id)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.ACTIVE
            assert refreshed.paused_at is None
            assert isinstance(refreshed.next_delivery_date, date)


@pytest.mark.unit
class TestSubscriptionNotificationTemplate:
    def test_send_subscription_notification_uses_real_fields(self, app, db, sample_user):
        with app.app_context():
            sub = _make_subscription(db, sample_user, status=SubscriptionStatus.ACTIVE, number="SUB-N1")

            svc = NotificationService()
            with patch.object(svc, "send_notification", return_value={"success": True}) as send:
                result = svc.send_subscription_notification(sub.id, "paused")

            assert result == {"success": True}
            template = send.call_args.args[3]
            assert template["plan_name"] == "Standard Weekly"
            assert template["frequency"] == SubscriptionFrequency.WEEKLY.value
            assert template["total_amount"] == 50000.0
