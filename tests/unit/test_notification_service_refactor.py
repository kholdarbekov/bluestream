"""Service regression tests for notification API boundary migration."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from business_app.models.notification import Notification, NotificationPreference, NotificationTemplate
from business_app.services.notification_service import NotificationService
from business_app.utils.constants import NotificationChannel, NotificationStatus
from business_app.utils.exceptions import ForbiddenError, ValidationError


def _create_notification(db, user_id: int, status: NotificationStatus) -> Notification:
    notification = Notification(
        user_id=user_id,
        notification_type='system',
        channel=NotificationChannel.PUSH,
        title='System Notification',
        message='Test message',
        delivery_status=status,
        created_at=datetime.now(UTC),
        extra_data={},
    )
    db.session.add(notification)
    db.session.commit()
    return notification


def test_mark_all_notifications_read_updates_delivery_status_and_metadata(db, sample_user):
    service = NotificationService()

    first = _create_notification(db, sample_user.id, NotificationStatus.PENDING)
    second = _create_notification(db, sample_user.id, NotificationStatus.SENT)

    marked_count = service.mark_all_notifications_read(sample_user.id)

    db.session.refresh(first)
    db.session.refresh(second)

    assert marked_count == 2
    assert first.delivery_status == NotificationStatus.READ
    assert second.delivery_status == NotificationStatus.READ
    assert isinstance(first.extra_data, dict) and first.extra_data.get('read_at')
    assert isinstance(second.extra_data, dict) and second.extra_data.get('read_at')


def test_create_default_preferences_is_idempotent(db, sample_user):
    service = NotificationService()

    service.create_default_preferences(sample_user.id)
    first_count = NotificationPreference.query.filter_by(user_id=sample_user.id).count()

    service.create_default_preferences(sample_user.id)
    second_count = NotificationPreference.query.filter_by(user_id=sample_user.id).count()

    assert first_count > 0
    assert second_count == first_count


def test_queue_bulk_notification_uses_task_contract_and_validates_channels(db, admin_user, sample_user):
    service = NotificationService()
    template = NotificationTemplate(
        name='bulk_system_template',
        notification_type='system',
        channel='email',
        subject='Bulk update',
        content='Hello',
        is_active=True,
    )
    db.session.add(template)
    db.session.commit()

    delay_mock = Mock(return_value=SimpleNamespace(id='task-123'))

    with patch('business_app.tasks.notification_tasks.send_bulk_notification_task.delay', delay_mock):
        result = service.queue_bulk_notification(
            sender_id=admin_user.id,
            user_ids=[sample_user.id],
            template_code='bulk_system_template',
            template_data={'hello': 'world'},
            channels=['push', 'email'],
        )

    delay_mock.assert_called_once_with(
        notification_type='system',
        recipient_ids=[sample_user.id],
        template_data={'hello': 'world'},
        channels=['push', 'email'],
    )
    assert result == {'task_id': 'task-123', 'recipient_count': 1}

    with pytest.raises(ValidationError):
        service.queue_bulk_notification(
            sender_id=admin_user.id,
            user_ids=[sample_user.id],
            template_code='bulk_system_template',
            template_data={},
            channels=['bad-channel'],
        )


def test_queue_bulk_notification_requires_admin(db, sample_user):
    service = NotificationService()

    with pytest.raises(ForbiddenError):
        service.queue_bulk_notification(
            sender_id=sample_user.id,
            user_ids=[sample_user.id],
            template_code='anything',
            template_data={},
            channels=['email'],
        )


def test_get_delivery_reports_paginated_uses_delivery_status_summary(db, admin_user, sample_user):
    service = NotificationService()

    _create_notification(db, sample_user.id, NotificationStatus.DELIVERED)
    _create_notification(db, sample_user.id, NotificationStatus.FAILED)
    _create_notification(db, sample_user.id, NotificationStatus.PENDING)

    reports = service.get_delivery_reports_paginated(
        requester_id=admin_user.id,
        page=1,
        per_page=10,
    )

    assert reports['total'] == 3
    assert reports['summary']['total_sent'] == 3
    assert reports['summary']['delivered'] == 1
    assert reports['summary']['failed'] == 1
    assert reports['summary']['pending'] == 1
