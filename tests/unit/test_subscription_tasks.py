"""Regression tests for subscription task date filtering."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from business_app import db
from business_app.models.subscription import Subscription
from business_app.models.user import UserAddress
from business_app.tasks.subscription_tasks import (
    process_daily_subscription_billing,
    send_renewal_reminders,
)
from business_app.utils.constants import PaymentMethod, SubscriptionFrequency, SubscriptionStatus


def _create_address(sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Test address",
        city="Tashkent",
        country="Uzbekistan",
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _create_subscription(sample_user, address, subscription_number, next_billing_date):
    subscription = Subscription(
        subscription_number=subscription_number,
        user_id=sample_user.id,
        status=SubscriptionStatus.ACTIVE,
        name="Weekly Water",
        description="Scheduled delivery",
        billing_cycle=SubscriptionFrequency.MONTHLY,
        billing_amount=Decimal("10000.00"),
        next_billing_date=next_billing_date,
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_day_of_week=1,
        delivery_address_id=address.id,
        next_delivery_date=next_billing_date,
        start_date=datetime.now(timezone.utc) - timedelta(days=7),
        payment_method=PaymentMethod.CARD,
    )
    db.session.add(subscription)
    db.session.commit()
    return subscription


def test_process_daily_subscription_billing_uses_end_of_day_cutoff(db, sample_user):
    address = _create_address(sample_user)
    due_today = _create_subscription(
        sample_user,
        address,
        "SUB-DUE-TODAY",
        datetime.now(timezone.utc).replace(hour=23, minute=0, second=0, microsecond=0),
    )
    _create_subscription(
        sample_user,
        address,
        "SUB-DUE-TOMORROW",
        (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=1, minute=0, second=0, microsecond=0),
    )

    with patch("business_app.tasks.subscription_tasks.SubscriptionService") as subscription_service_cls:
        subscription_service_cls.return_value.process_subscription_billing.return_value = {"success": True, "order_id": 1}
        result = process_daily_subscription_billing()

    assert result["successful"] == 1
    subscription_service_cls.return_value.process_subscription_billing.assert_called_once_with(due_today.id)


def test_send_renewal_reminders_uses_billing_fields_and_day_range(db, sample_user):
    address = _create_address(sample_user)
    reminder_target = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)
    due_for_reminder = _create_subscription(
        sample_user,
        address,
        "SUB-REMINDER",
        reminder_target,
    )
    _create_subscription(
        sample_user,
        address,
        "SUB-NO-REMINDER",
        reminder_target + timedelta(days=1),
    )

    with patch("business_app.tasks.subscription_tasks.NotificationService") as notification_service_cls:
        result = send_renewal_reminders()

    assert result["sent_count"] == 1
    notification_service_cls.return_value.send_notification.assert_called_once()
    call_args = notification_service_cls.return_value.send_notification.call_args
    assert call_args.args[0] == due_for_reminder.user_id
    assert call_args.args[1] == "subscription_renewal_reminder"
    template_data = call_args.kwargs["template_data"]
    assert template_data["plan_name"] == due_for_reminder.name
    assert template_data["amount"] == due_for_reminder.billing_amount
    assert template_data["frequency"] == due_for_reminder.billing_cycle.value
