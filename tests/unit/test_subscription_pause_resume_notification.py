"""Regression: subscription pause/resume and notifications must use real model
fields (the model was refactored: frequency→billing_cycle, total_amount→
billing_amount, plan→name, pause_until→pause_end_date, resumed_at→resume_date).

resume_subscription read ``subscription.frequency`` → AttributeError (crash);
pause wrote ``subscription.pause_until`` (phantom, silently lost); and
NotificationService.send_subscription_notification read plan/frequency/
total_amount (all phantom) → crash whenever a subscription notification fired.
"""

from datetime import UTC, datetime, timedelta
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
        billing_cycle=SubscriptionFrequency.WEEKLY,
        billing_amount=Decimal("50000.00"),
        next_billing_date=datetime.now(UTC) + timedelta(days=7),
        delivery_frequency=SubscriptionFrequency.WEEKLY,
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
    def test_pause_sets_pause_end_date(self, app, db, sample_user):
        with app.app_context():
            sub = _make_subscription(db, sample_user, status=SubscriptionStatus.ACTIVE, number="SUB-P1")
            until = datetime.now(UTC) + timedelta(days=14)

            SubscriptionService().pause_subscription(sub.id, pause_until=until)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.PAUSED
            assert refreshed.pause_end_date is not None

    def test_resume_recalculates_billing_without_crashing(self, app, db, sample_user):
        with app.app_context():
            sub = _make_subscription(db, sample_user, status=SubscriptionStatus.PAUSED, number="SUB-R1")

            SubscriptionService().resume_subscription(sub.id)

            refreshed = Subscription.query.get(sub.id)
            assert refreshed.status == SubscriptionStatus.ACTIVE
            assert refreshed.resume_date is not None
            assert isinstance(refreshed.next_billing_date, datetime)


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
