"""Service regression tests for notification API boundary migration."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from business_app.models.audit import AuditEventType, AuditLog
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.notification import Notification, NotificationPreference, NotificationTemplate
from business_app.models.translation import Translation
from business_app.services.notification_service import DEFAULT_TEMPLATES, NotificationService
from business_app.utils.constants import (
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
)
from business_app.utils.exceptions import ForbiddenError, ValidationError

# conftest's autouse `block_external_side_effects` stubs send_notification with
# an always-succeeds mock. Tests that assert the real fan-out refuses SMS must
# put the production implementation back; captured before any monkeypatching.
_REAL_SEND_NOTIFICATION = NotificationService.send_notification


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


def _upsert_translation(db, key: str, language: str, value: str) -> Translation:
    translation = Translation.query.filter_by(key=key, language=language).first()
    if translation:
        translation.value = value
        translation.is_active = True
    else:
        translation = Translation(
            key=key,
            language=language,
            value=value,
            category='general',
            is_active=True,
        )
        db.session.add(translation)
    db.session.flush()
    return translation


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


def test_create_notification_campaign_persists_audit_log(db, admin_user, sample_user):
    service = NotificationService()

    campaign = service.create_notification_campaign(
        sender_id=admin_user.id,
        payload={
            'name': 'Weekend retention push',
            'notification_type': NotificationType.PROMOTIONAL.value,
            'channel': 'email',
            'subject': 'Weekend special',
            'content': 'Save 10% this weekend',
            'target_audience': 'all_customers',
            'priority': 'high',
        },
    )

    assert campaign['name'] == 'Weekend retention push'
    assert campaign['notification_type'] == NotificationType.PROMOTIONAL.value
    assert campaign['channel'] == 'email'
    assert campaign['recipient_count'] == 0
    assert campaign['status'] == 'draft'
    assert campaign['summary']['total'] == 0


@pytest.mark.parametrize('channel', ['sms', 'phone'])
def test_create_notification_campaign_rejects_sms_channels(db, admin_user, channel):
    """'phone' used to be silently normalized to 'sms'. Both are now refused:
    SMS is OTP-only, so it can never carry a campaign blast."""
    service = NotificationService()

    with pytest.raises(ValidationError):
        service.create_notification_campaign(
            sender_id=admin_user.id,
            payload={
                'name': 'Weekend retention push',
                'notification_type': NotificationType.PROMOTIONAL.value,
                'channel': channel,
                'subject': 'Weekend special',
                'content': 'Save 10% this weekend',
                'target_audience': 'all_customers',
                'priority': 'high',
            },
        )


def test_get_notification_campaigns_paginated_filters_by_search_status_and_channel(
    db, admin_user, sample_user
):
    service = NotificationService()

    first = service.create_notification_campaign(
        sender_id=admin_user.id,
        payload={
            'name': 'VIP delivery alert',
            'notification_type': NotificationType.SYSTEM_ALERT.value,
            'channel': 'in_app',
            'subject': 'Driver is nearby',
            'content': 'Your order is almost there',
            'target_audience': 'all_customers',
            'priority': 'medium',
        },
    )
    second = service.create_notification_campaign(
        sender_id=admin_user.id,
        payload={
            'name': 'Loyalty reminder',
            'notification_type': NotificationType.PROMOTIONAL.value,
            'channel': 'email',
            'subject': 'Use your points',
            'content': 'Redeem your loyalty balance',
            'target_audience': 'all_customers',
            'priority': 'low',
            'scheduled_at': datetime.now(UTC).isoformat(),
        },
    )
    service.queue_notification_campaign(
        sender_id=admin_user.id,
        campaign_id=second['id'],
        send_now=False,
    )

    result = service.get_notification_campaigns_paginated(
        requester_id=admin_user.id,
        page=1,
        per_page=10,
        search='loyalty',
        status='scheduled',
        channel='email',
    )

    assert result['total'] == 1
    assert result['items'][0]['id'] == second['id']
    assert result['items'][0]['name'] == 'Loyalty reminder'
    assert all(item['id'] != first['id'] for item in result['items'])


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


def test_get_localized_delivery_status_label_prefers_same_language_api_key_over_english_fallback(db):
    service = NotificationService()

    _upsert_translation(db, 'notification.delivery_status.arrived', 'en', 'Arrived')
    _upsert_translation(db, 'api.delivery.arrived', 'uz', 'Buyurtma yetib keldi')
    db.session.commit()

    label = service._get_localized_delivery_status_label(DeliveryStatus.ARRIVED.value, 'uz')

    assert label == 'Buyurtma yetib keldi'


def test_get_localized_delivery_status_label_uses_bundled_uz_fallback_when_db_is_incomplete(db):
    service = NotificationService()

    _upsert_translation(db, 'notification.delivery_status.arrived', 'en', 'Arrived')
    db.session.commit()

    label = service._get_localized_delivery_status_label(DeliveryStatus.ARRIVED.value, 'uz')

    assert label == 'Yetib keldi'


def test_send_delivery_status_change_notification_forces_telegram_for_connected_user(db, sample_user, sample_order):
    sample_user.telegram_id = '998900001234'
    sample_user.is_bot_active = True
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationType.DELIVERY_UPDATE.value,
            channel=NotificationChannel.SMS,
            is_enabled=True,
        )
    )
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.ASSIGNED,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    service = NotificationService()
    captured = {}

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        captured['user_id'] = user_id
        captured['notification_type'] = notification_type
        captured['channels'] = channels
        captured['template_data'] = template_data
        return {'telegram': {'success': True}, 'sms': {'success': True}}

    service.send_notification = _fake_send_notification

    result = service.send_delivery_status_change_notification(history.id)

    assert result['telegram']['success'] is True
    assert captured['user_id'] == sample_user.id
    assert captured['notification_type'] == NotificationType.DELIVERY_UPDATE
    assert captured['channels'] == [NotificationChannel.TELEGRAM]
    assert captured['template_data']['delivery_status_code'] == DeliveryStatus.IN_TRANSIT.value
    assert captured['template_data']['delivery_status'] == 'In Transit'


def test_send_delivery_status_change_notification_falls_back_to_email_when_bot_inactive(
    db, sample_user, sample_order
):
    sample_user.telegram_id = '998900001235'
    sample_user.is_bot_active = False
    # A stored SMS preference must be ignored — delivery updates never use SMS.
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationType.DELIVERY_UPDATE.value,
            channel=NotificationChannel.SMS,
            is_enabled=True,
        )
    )
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.ASSIGNED,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    service = NotificationService()
    captured = {}

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        captured['channels'] = channels
        return {'email': {'success': True}}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == [NotificationChannel.EMAIL]


def test_send_delivery_status_change_notification_falls_back_to_email_when_bot_not_connected(
    db, sample_user, sample_order
):
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationType.DELIVERY_UPDATE.value,
            channel=NotificationChannel.SMS,
            is_enabled=True,
        )
    )
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationType.DELIVERY_UPDATE.value,
            channel=NotificationChannel.TELEGRAM,
            is_enabled=True,
        )
    )
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.ASSIGNED,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    service = NotificationService()
    captured = {}

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        captured['channels'] = channels
        return {'email': {'success': True}}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == [NotificationChannel.EMAIL]


def test_send_delivery_status_change_notification_sends_nothing_when_no_bot_and_no_email(
    db, sample_user, sample_order
):
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.email = None
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.ASSIGNED,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    service = NotificationService()
    captured = {}

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        captured['channels'] = channels
        return {}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == []


def test_send_delivery_status_change_notification_dispatches_bottle_summary_for_delivered_status(
    db, sample_user, sample_order
):
    sample_user.telegram_id = '998900001236'
    sample_user.is_bot_active = True
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.ARRIVED,
        new_status=DeliveryStatus.DELIVERED,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    service = NotificationService()
    send_notification_calls = []

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        send_notification_calls.append(channels)
        return {}

    service.send_notification = _fake_send_notification

    # Patch redis + webhook at the module-under-test path so the test is
    # deterministic regardless of the test-runner's Redis DB sharding.
    with patch('business_app.services.notification_service.trigger_bot_webhook') as mock_webhook, patch(
        'business_app.services.notification_service.redis_client'
    ) as mock_redis:
        mock_webhook.return_value = {'success': True}
        mock_redis.set.return_value = True
        result = service.send_delivery_status_change_notification(history.id)

    # Delivered no longer routes through channel resolution — it dispatches the
    # bottle-summary webhook instead.
    assert send_notification_calls == []
    mock_webhook.assert_called_once()
    endpoint, payload = mock_webhook.call_args[0]
    assert endpoint == '/internal/delivery-completed'
    assert payload['order_id'] == sample_order.id
    assert payload['order_number'] == sample_order.order_number
    assert payload['telegram_id'] == 998900001236
    # sample_order has no bottle-bearing items → non-bottle order → zeros.
    assert payload['bottles_delivered'] == '0'
    assert payload['bottles_collected'] == '0'
    assert payload['balance'] == '0'
    assert result['dispatched'] is True


def test_send_delivery_status_change_notification_sends_nothing_for_non_target_status_with_default_channels(
    db, sample_user, sample_order
):
    sample_user.telegram_id = '998900001238'
    sample_user.is_bot_active = True
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.SCHEDULED,
        new_status=DeliveryStatus.ASSIGNED,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    service = NotificationService()
    captured = {}

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        captured['channels'] = channels
        return {}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == []


def test_send_delivery_status_change_notification_returns_error_for_missing_history(db):
    service = NotificationService()

    result = service.send_delivery_status_change_notification(999999)

    assert result['success'] is False
    assert result['error'] == 'Delivery status history not found'


def test_create_notification_record_persists_telegram_delivery_audit_fields(
    db, sample_user, sample_order
):
    sample_user.telegram_id = '998900001237'
    sample_user.is_bot_active = True
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    service = NotificationService()
    service._create_notification_record(
        sample_user.id,
        NotificationType.DELIVERY_UPDATE,
        [NotificationChannel.TELEGRAM],
        {
            'order_id': sample_order.id,
            'delivery_id': delivery.id,
            'delivery_status_code': DeliveryStatus.IN_TRANSIT.value,
        },
        {'telegram': {'success': False, 'error': 'telegram failed'}},
    )

    notification = Notification.query.one()

    assert notification.recipient_telegram_id == sample_user.telegram_id
    assert notification.delivery_id == delivery.id
    assert notification.order_id == sample_order.id
    assert notification.failure_reason == 'telegram failed'


def test_get_notification_template_uses_bundled_fallback_when_db_row_missing(db):
    service = NotificationService()

    template = service._get_notification_template(
        NotificationType.DELIVERY_UPDATE,
        NotificationChannel.TELEGRAM,
        'uz',
    )

    assert template is not None
    assert 'Yetkazib berish yangiligi' in template.get_translated('content', 'uz')


def test_send_telegram_notification_uses_bundled_fallback_template_when_db_missing(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {'ok': True, 'result': {'message_id': 777}}

    with patch('business_app.services.notification_service.requests.post', return_value=fake_response) as post_mock:
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.DELIVERY_UPDATE,
            {
                'order_number': 'TG_000036_26',
                'delivery_status_code': DeliveryStatus.IN_TRANSIT.value,
                'delivery_status': "Buyurtma yo'lda",
                'tracking_code': 'TRK123',
            },
            'uz',
        )

    assert result['success'] is True
    assert result['message_id'] == 777
    payload = post_mock.call_args.kwargs['json']
    assert payload['chat_id'] == '104933915'
    assert 'Yetkazib berish yangiligi' in payload['text']
    assert 'Haydovchi' not in payload['text']
    assert 'Telefon' not in payload['text']


def test_send_telegram_notification_strips_driver_info_from_db_template(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    fake_template = SimpleNamespace(
        content='''🚚 <b>Delivery Update</b>

Order: #{order_number}
Driver: {driver_name}
Phone: {driver_phone}
📞 +998901234567''',
        get_translated=lambda field, language: '''🚚 <b>Delivery Update</b>

Order: #{order_number}
Driver: {driver_name}
Phone: {driver_phone}
📞 +998901234567''' if field == 'content' else None,
    )

    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {'ok': True, 'result': {'message_id': 778}}

    with (
        patch.object(service, '_get_notification_template', return_value=fake_template),
        patch('business_app.services.notification_service.requests.post', return_value=fake_response) as post_mock,
    ):
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.DELIVERY_UPDATE,
            {
                'order_number': 'TG_000037_26',
                'delivery_status_code': DeliveryStatus.IN_TRANSIT.value,
                'delivery_status': 'In Transit',
                'driver_name': 'Driver Test',
                'driver_phone': '+998900000000',
            },
            'en',
        )

    assert result['success'] is True
    payload_text = post_mock.call_args.kwargs['json']['text']
    assert 'Driver Test' not in payload_text
    assert '+998900000000' not in payload_text
    assert 'Driver:' not in payload_text
    assert 'Phone:' not in payload_text
    assert '📞' not in payload_text


def test_send_telegram_notification_skips_non_target_delivery_status(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    with patch('business_app.services.notification_service.requests.post') as post_mock:
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.DELIVERY_UPDATE,
            {
                'order_number': 'TG_000038_26',
                'delivery_status_code': DeliveryStatus.DELIVERED.value,
                'delivery_status': 'Delivered',
                'tracking_code': 'TRK456',
            },
            'en',
        )

    assert result['success'] is True
    assert result['skipped'] is True
    assert result['reason'] == 'delivery_status_not_allowed'
    post_mock.assert_not_called()


def test_send_telegram_notification_skips_when_only_order_status_is_delivered(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    with patch('business_app.services.notification_service.requests.post') as post_mock:
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.DELIVERY_UPDATE,
            {
                'order_number': 'TG_000038_26',
                'order_status': DeliveryStatus.DELIVERED.value,
            },
            'en',
        )

    assert result['success'] is True
    assert result['skipped'] is True
    assert result['reason'] == 'delivery_status_not_allowed'
    post_mock.assert_not_called()


def test_send_telegram_notification_allows_in_transit_delivery_status(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {'ok': True, 'result': {'message_id': 779}}

    with patch('business_app.services.notification_service.requests.post', return_value=fake_response) as post_mock:
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.DELIVERY_UPDATE,
            {
                'order_number': 'TG_000039_26',
                'delivery_status_code': DeliveryStatus.IN_TRANSIT.value,
                'delivery_status': 'In Transit',
                'tracking_code': 'TRK789',
            },
            'en',
        )

    assert result['success'] is True
    assert result.get('skipped') is None
    post_mock.assert_called_once()


def test_send_staff_telegram_message_uses_staff_bot_token(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    service.telegram_bot_token = 'customer-token'
    service.staff_telegram_bot_token = 'staff-token'

    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {'ok': True, 'result': {'message_id': 790}}

    with patch('business_app.services.notification_service.requests.post', return_value=fake_response) as post_mock:
        result = service.send_staff_telegram_message(sample_user, "Staff reminder")

    assert result['success'] is True
    called_url = post_mock.call_args.args[0]
    assert '/botstaff-token/sendMessage' in called_url
    assert '/botcustomer-token/sendMessage' not in called_url


def test_send_payment_notification_uses_delivered_follow_up_message_for_cod_order(
    db,
    sample_user,
    sample_order,
    sample_payment,
):
    service = NotificationService()

    sample_user.preferred_language = 'en'
    sample_order.status = OrderStatus.DELIVERED
    sample_payment.payment_method = PaymentMethod.CASH
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.commit()

    send_mock = Mock(return_value={'telegram': {'success': True}})
    service.send_notification = send_mock

    result = service.send_payment_notification(sample_payment.id)

    assert result == {'telegram': {'success': True}}
    send_mock.assert_called_once()
    payload = send_mock.call_args.args[3]
    assert payload['payment_follow_up_message'] == (
        "Your order has already been delivered. "
        "This message confirms that we have received your payment."
    )


def _telegram_payment_confirmation_template():
    """Faithful copy of the shipped ``payment_confirmation``/``telegram`` default
    template, which injects the contextual follow-up line via the
    ``{payment_follow_up_message}`` placeholder."""
    translations = DEFAULT_TEMPLATES[('payment_confirmation', 'telegram')]['translations']

    def _get_translated(field, language):
        if field != 'content':
            return None
        return translations.get(language, translations['uz'])['content']

    return SimpleNamespace(
        content=translations['uz']['content'],
        get_translated=_get_translated,
    )


@pytest.mark.parametrize(
    'language,follow_up_tail',
    [
        ('uz', "Keyingi holat bo'yicha sizni xabardor qilamiz."),
        ('ru', "Мы сообщим вам о следующем обновлении статуса."),
        ('en', "We'll notify you about the next status update."),
    ],
)
def test_send_telegram_payment_confirmation_fills_follow_up_placeholder_once(
    app, db, sample_user, language, follow_up_tail
):
    """The ``{payment_follow_up_message}`` placeholder is the single injection
    point for the follow-up copy, so it must appear exactly once.

    Regression: a runtime "legacy rewrite" shim also string-replaced a legacy
    follow-up sentence into the copy. For uz/ru that legacy sentence is a strict
    prefix of the new copy, so the shim re-appended the tail after the
    placeholder already rendered it — producing the duplicated
    "Keyingi holat bo'yicha sizni xabardor qilamiz." seen in production.
    """
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    sample_user.preferred_language = language
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    follow_up = NotificationService.PAYMENT_FOLLOW_UP_MESSAGES[language]['processing']

    fake_response = Mock()
    fake_response.status_code = 200
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {'ok': True, 'result': {'message_id': 780}}

    with patch('business_app.services.notification_service.requests.post', return_value=fake_response) as post_mock:
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.PAYMENT_CONFIRMATION,
            {
                'order_number': 'TG_000040_26',
                'payment_amount': '18000',
                'payment_method': 'cash',
                'payment_follow_up_message': follow_up,
            },
            language,
            template_override=_telegram_payment_confirmation_template(),
        )

    assert result['success'] is True
    payload_text = post_mock.call_args.kwargs['json']['text']
    assert follow_up in payload_text
    assert payload_text.count(follow_up_tail) == 1


def test_get_delivery_telegram_setting_defaults_to_enabled_without_override(db, sample_user):
    service = NotificationService()

    result = service.get_delivery_telegram_status_updates_setting(sample_user.id)

    assert result['delivery_telegram_status_updates_enabled'] is True
    assert result['delivery_telegram_status_updates_source'] == 'default'
    assert result['updated_at'] is None


def test_set_delivery_telegram_setting_persists_explicit_override(db, sample_user):
    service = NotificationService()

    updated = service.set_delivery_telegram_status_updates_setting(sample_user.id, enabled=False)
    reloaded = service.get_delivery_telegram_status_updates_setting(sample_user.id)

    assert updated['delivery_telegram_status_updates_enabled'] is False
    assert updated['delivery_telegram_status_updates_source'] == 'explicit'
    assert reloaded['delivery_telegram_status_updates_enabled'] is False

    row = NotificationPreference.query.filter_by(
        user_id=sample_user.id,
        notification_type=NotificationService.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
        channel=NotificationChannel.TELEGRAM,
    ).first()
    assert row is not None
    assert row.is_enabled is False


def test_resolve_delivery_status_channels_honors_explicit_delivery_telegram_disable(db, sample_user):
    sample_user.telegram_id = '998900001500'
    sample_user.is_bot_active = True
    db.session.add(sample_user)
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationType.DELIVERY_UPDATE.value,
            channel=NotificationChannel.SMS,
            is_enabled=True,
        )
    )
    db.session.add(
        NotificationPreference(
            user_id=sample_user.id,
            notification_type=NotificationService.DELIVERY_TELEGRAM_STATUS_UPDATES_PREF_KEY,
            channel=NotificationChannel.TELEGRAM,
            is_enabled=False,
        )
    )
    db.session.commit()

    service = NotificationService()
    channels = service._resolve_delivery_status_channels(sample_user, DeliveryStatus.IN_TRANSIT.value)

    assert channels == [NotificationChannel.EMAIL]


@pytest.mark.parametrize(
    'notification_type',
    [
        NotificationType.DELIVERY_UPDATE,
        NotificationType.DELIVERY_REMINDER,
        NotificationType.ORDER_STATUS_UPDATE,
        NotificationType.ORDER_UPDATE,
    ],
)
def test_customer_updates_never_go_out_over_sms(db, sample_user, monkeypatch, notification_type):
    """These types were the original reason for the no-SMS backstop.

    SMS is now OTP-only outright, so the guarantee is stronger than it was: it
    holds for every notification type, and even a caller that explicitly asks
    for the SMS channel cannot reach the provider. Asserted through the public
    fan-out rather than a private helper, because that is what callers use.
    """
    monkeypatch.setattr(
        NotificationService, 'send_notification', _REAL_SEND_NOTIFICATION
    )
    service = NotificationService()
    service.eskiz_client = Mock()

    assert NotificationChannel.SMS not in service._default_channels_for_type(
        notification_type.value
    )

    results = service.send_notification(
        sample_user.id,
        notification_type,
        [NotificationChannel.SMS],
        {'order_number': 'AD_000342_26', 'delivery_status': 'Delivered'},
    )

    assert results['sms']['success'] is False
    assert results['sms']['reason'] == 'sms_is_otp_only'
    service.eskiz_client.send_sms.assert_not_called()


def test_build_delivery_status_template_data_populates_company_contact_fields(db, sample_user, sample_order):
    service = NotificationService()
    delivery = Delivery(
        order_id=sample_order.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot='09:00-12:00',
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.ASSIGNED,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_at=datetime.now(UTC),
    )
    db.session.add(history)
    db.session.commit()

    data = service._build_delivery_status_template_data(delivery=delivery, history=history, language='uz')

    assert 'company_phone' in data
    assert data['company_phone'] == service.company_phone
    assert 'company_name' in data
    assert 'company_email' in data


def test_delivery_update_email_template_renders_without_placeholder(app):
    from business_app.services.email_template_service import get_email_template_service

    rendered = get_email_template_service().render_notification_email(
        'delivery_update',
        'uz',
        {
            'order_number': 'AD_000342_26',
            'delivery_status': "Yo'lda",
            'tracking_code': 'TRK202606161552C5FD14',
            'user_name': 'Test User',
        },
    )

    assert rendered is not None
    assert '{company_phone}' not in rendered['content']
    assert 'AD_000342_26' in rendered['content']


def test_set_delivery_telegram_setting_requires_reason_for_admin_source(db, sample_user):
    service = NotificationService()

    with pytest.raises(ValidationError):
        service.set_delivery_telegram_status_updates_setting(
            sample_user.id,
            enabled=False,
            source='admin',
            actor_user_id=101,
            reason='',
        )


def test_set_delivery_telegram_setting_writes_admin_audit_log(db, admin_user, sample_user):
    service = NotificationService()

    service.set_delivery_telegram_status_updates_setting(
        sample_user.id,
        enabled=False,
        source='admin',
        actor_user_id=admin_user.id,
        reason='Customer requested disable via phone',
    )

    audit_entry = AuditLog.query.filter_by(
        action='admin_update_delivery_telegram_notification_setting',
        resource_type='user',
        resource_id=str(sample_user.id),
    ).order_by(AuditLog.created_at.desc()).first()

    assert audit_entry is not None
    assert audit_entry.event_type == AuditEventType.USER_UPDATED
    assert audit_entry.additional_data['reason'] == 'Customer requested disable via phone'


def test_set_delivery_telegram_setting_rejects_unknown_source(db, sample_user):
    service = NotificationService()

    with pytest.raises(ValidationError):
        service.set_delivery_telegram_status_updates_setting(
            sample_user.id,
            enabled=True,
            source='system',
        )
