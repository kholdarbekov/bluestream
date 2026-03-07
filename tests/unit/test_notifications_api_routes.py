"""Route-level regression tests for notifications API delegation."""

from types import SimpleNamespace
from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.utils.exceptions import ForbiddenError, ValidationError


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def _preferences_view(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        email_enabled=True,
        sms_enabled=True,
        push_enabled=True,
        in_app_enabled=True,
        telegram_enabled=False,
        delivery_telegram_status_updates_enabled=True,
        order_notifications=True,
        delivery_notifications=True,
        payment_notifications=True,
        promotion_notifications=False,
        system_notifications=True,
        loyalty_notifications=False,
        security_notifications=True,
        reminder_notifications=True,
        quiet_hours_enabled=False,
        quiet_hours_start=None,
        quiet_hours_end=None,
        digest_enabled=False,
        digest_frequency='weekly',
        updated_at=None,
    )


def test_get_notification_preferences_route_delegates_to_service(client, app, sample_user, monkeypatch):
    service = Mock()
    service.create_default_preferences.return_value = _preferences_view(sample_user.id)
    monkeypatch.setattr('business_app.api.notifications.get_notification_service', lambda: service)

    response = client.get('/api/v1/notifications/preferences', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    body = response.get_json()
    assert body['data']['preferences']['delivery_telegram_status_updates_enabled'] is True
    service.create_default_preferences.assert_called_once()
    assert int(service.create_default_preferences.call_args.args[0]) == sample_user.id


def test_update_notification_preferences_route_delegates_to_service(client, app, sample_user, monkeypatch):
    service = Mock()
    updated_view = _preferences_view(sample_user.id)
    updated_view.delivery_telegram_status_updates_enabled = False
    service.update_notification_preferences_for_user.return_value = updated_view
    monkeypatch.setattr('business_app.api.notifications.get_notification_service', lambda: service)

    payload = {
        'email_enabled': False,
        'system_notifications': True,
        'delivery_telegram_status_updates_enabled': False,
    }
    response = client.put(
        '/api/v1/notifications/preferences',
        headers=_auth_headers(app, sample_user.id),
        json=payload,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body['data']['preferences']['delivery_telegram_status_updates_enabled'] is False
    service.update_notification_preferences_for_user.assert_called_once()
    kwargs = service.update_notification_preferences_for_user.call_args.kwargs
    assert int(kwargs['user_id']) == sample_user.id
    assert kwargs['payload'] == payload


def test_register_push_token_route_maps_validation_error(client, app, sample_user, monkeypatch):
    service = Mock()
    service.register_push_token_for_user.side_effect = ValidationError('Invalid platform')
    monkeypatch.setattr('business_app.api.notifications.get_notification_service', lambda: service)

    response = client.post(
        '/api/v1/notifications/push-token',
        headers=_auth_headers(app, sample_user.id),
        json={'token': 'token-1', 'platform': 'bad-platform'},
    )

    assert response.status_code == 400


def test_send_bulk_notification_route_maps_forbidden_error(client, app, sample_user, monkeypatch):
    service = Mock()
    service.queue_bulk_notification.side_effect = ForbiddenError('Admin access required')
    monkeypatch.setattr('business_app.api.notifications.get_notification_service', lambda: service)

    response = client.post(
        '/api/v1/notifications/bulk-send',
        headers=_auth_headers(app, sample_user.id),
        json={
            'user_ids': [sample_user.id],
            'template_code': 'bulk_system_template',
            'channels': ['push'],
        },
    )

    assert response.status_code == 403


def test_get_delivery_reports_route_returns_paginated_service_data(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_delivery_reports_paginated.return_value = {
        'items': [
            {
                'id': 1,
                'user_id': sample_user.id,
                'channel': 'push',
                'status': 'delivered',
                'created_at': '2026-02-26T00:00:00+00:00',
                'sent_at': None,
                'delivered_at': None,
                'error_message': None,
            }
        ],
        'page': 1,
        'per_page': 20,
        'total': 1,
        'summary': {
            'total_sent': 1,
            'delivered': 1,
            'failed': 0,
            'pending': 0,
            'delivery_rate': 100.0,
        },
    }
    monkeypatch.setattr('business_app.api.notifications.get_notification_service', lambda: service)

    response = client.get(
        '/api/v1/notifications/delivery-reports?page=1&per_page=20',
        headers=_auth_headers(app, sample_user.id),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data['meta']['summary']['total_sent'] == 1
