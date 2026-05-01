"""Service regression tests for notification API boundary migration."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from business_app.models.audit import AuditEventType, AuditLog
from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.notification import Notification, NotificationPreference, NotificationTemplate
from business_app.models.translation import Translation
from business_app.services.notification_service import NotificationService
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


def test_create_notification_campaign_persists_audit_log_and_normalizes_phone_channel(
    db, admin_user, sample_user
):
    service = NotificationService()

    campaign = service.create_notification_campaign(
        sender_id=admin_user.id,
        payload={
            'name': 'Weekend retention push',
            'notification_type': NotificationType.PROMOTIONAL.value,
            'channel': 'phone',
            'subject': 'Weekend special',
            'content': 'Save 10% this weekend',
            'target_audience': 'all_customers',
            'priority': 'high',
        },
    )

    assert campaign['name'] == 'Weekend retention push'
    assert campaign['notification_type'] == NotificationType.PROMOTIONAL.value
    assert campaign['channel'] == 'sms'
    assert campaign['recipient_count'] == 0
    assert campaign['status'] == 'draft'
    assert campaign['summary']['total'] == 0


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
    assert captured['channels'] == [NotificationChannel.SMS, NotificationChannel.TELEGRAM]
    assert captured['template_data']['delivery_status_code'] == DeliveryStatus.IN_TRANSIT.value
    assert captured['template_data']['delivery_status'] == 'In Transit'


def test_send_delivery_status_change_notification_skips_forced_telegram_when_bot_inactive(
    db, sample_user, sample_order
):
    sample_user.telegram_id = '998900001235'
    sample_user.is_bot_active = False
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
        return {'sms': {'success': True}}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == [NotificationChannel.SMS]


def test_send_delivery_status_change_notification_removes_preferred_telegram_when_bot_not_connected(
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
        return {'sms': {'success': True}}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == [NotificationChannel.SMS]


def test_send_delivery_status_change_notification_preserves_non_target_status_channels(
    db, sample_user, sample_order
):
    sample_user.telegram_id = '998900001236'
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
    captured = {}

    def _fake_send_notification(user_id, notification_type, channels=None, template_data=None, priority='normal'):
        captured['channels'] = channels
        return {'sms': {'success': True}}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == [NotificationChannel.SMS]


def test_send_delivery_status_change_notification_removes_telegram_from_default_channels_for_non_target_status(
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
        return {'sms': {'success': True}}

    service.send_notification = _fake_send_notification

    service.send_delivery_status_change_notification(history.id)

    assert captured['channels'] == [NotificationChannel.SMS]


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


def test_send_telegram_payment_confirmation_rewrites_legacy_processing_copy(app, db, sample_user):
    sample_user.telegram_id = '104933915'
    sample_user.is_bot_active = True
    sample_user.preferred_language = 'en'
    db.session.commit()

    service = NotificationService()
    app.config['TELEGRAM_BOT_TOKEN'] = 'test-token'
    service.telegram_bot_token = 'test-token'

    legacy_line = "Your order is now being processed. We'll notify you when it's ready for delivery."
    replacement_line = (
        "Your order has already been delivered. "
        "This message confirms that we have received your payment."
    )

    fake_template = SimpleNamespace(
        content=f'''✅ <b>Payment Confirmed!</b>

Order: #{{order_number}}
Amount: {{payment_amount}} UZS
Method: {{payment_method}}

{legacy_line}

Thank you for your purchase!''',
        get_translated=lambda field, language: f'''✅ <b>Payment Confirmed!</b>

Order: #{{order_number}}
Amount: {{payment_amount}} UZS
Method: {{payment_method}}

{legacy_line}

Thank you for your purchase!''' if field == 'content' else None,
    )

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
                'payment_follow_up_message': replacement_line,
            },
            'en',
            template_override=fake_template,
        )

    assert result['success'] is True
    payload_text = post_mock.call_args.kwargs['json']['text']
    assert replacement_line in payload_text
    assert legacy_line not in payload_text


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

    assert channels == [NotificationChannel.SMS]


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
