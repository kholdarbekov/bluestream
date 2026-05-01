"""Route tests for admin notification template endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from shared.enums import UserRole
def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.ADMIN.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_get_notification_templates_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_admin_notification_templates_paginated.return_value = {
        'items': [{'id': 1, 'name': 'Telegram reminder', 'channel': 'telegram'}],
        'page': 1,
        'per_page': 20,
        'total': 1,
    }
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.get(
        '/api/v1/admin/notification-templates?page=1&per_page=20&channel=telegram&is_active=true',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    assert response.get_json()['data']['items'][0]['channel'] == 'telegram'
    service.get_admin_notification_templates_paginated.assert_called_once_with(
        requester_id=str(admin_user.id),
        page=1,
        per_page=20,
        search=None,
        notification_type=None,
        channel='telegram',
        is_active=True,
    )


def test_create_notification_template_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.create_admin_notification_template.return_value = {'id': 5, 'channel': 'telegram'}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    payload = {
        'name': 'Telegram delivery reminder',
        'notification_type': 'delivery_update',
        'channel': 'telegram',
        'content': 'Driver is arriving'
    }
    response = client.post(
        '/api/v1/admin/notification-templates',
        headers=_admin_headers(app, admin_user.id),
        json=payload,
    )

    assert response.status_code == 201
    service.create_admin_notification_template.assert_called_once_with(
        requester_id=str(admin_user.id),
        payload=payload,
    )


def test_preview_notification_template_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.preview_admin_notification_template.return_value = {'content': 'Preview content'}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    payload = {'language': 'en', 'variables': {'user_name': 'Admin'}}
    response = client.post(
        '/api/v1/admin/notification-templates/10/preview',
        headers=_admin_headers(app, admin_user.id),
        json=payload,
    )

    assert response.status_code == 200
    service.preview_admin_notification_template.assert_called_once_with(
        requester_id=str(admin_user.id),
        template_id=10,
        payload=payload,
    )


def test_test_send_notification_template_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.test_send_admin_notification_template.return_value = {'notification_id': 99}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.post(
        '/api/v1/admin/notification-templates/11/test-send',
        headers=_admin_headers(app, admin_user.id),
        json={'variables': {'user_name': 'Admin'}},
    )

    assert response.status_code == 200
    service.test_send_admin_notification_template.assert_called_once()


def test_get_notification_channel_metadata_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_admin_notification_channels.return_value = [{'value': 'telegram', 'label': 'Telegram'}]
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.get(
        '/api/v1/admin/notification-templates/channels',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    assert response.get_json()['data']['channels'][0]['value'] == 'telegram'
    service.get_admin_notification_channels.assert_called_once_with(requester_id=str(admin_user.id))
