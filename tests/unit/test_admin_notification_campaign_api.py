"""Route tests for admin notification campaign endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.utils.constants import UserRole


def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.ADMIN.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_get_notification_campaigns_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_notification_campaigns_paginated.return_value = {
        'items': [
            {
                'id': 101,
                'name': 'Spring reminder',
                'channel': 'push',
                'status': 'draft',
                'recipient_count': 12,
                'sent_count': 0,
            }
        ],
        'page': 2,
        'per_page': 5,
        'total': 7,
    }
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.get(
        '/api/v1/admin/notification-campaigns?page=2&per_page=5&search=spring&status=draft&type=push',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['data']['items'][0]['name'] == 'Spring reminder'
    assert payload['meta']['total'] == 7
    service.get_notification_campaigns_paginated.assert_called_once()
    kwargs = service.get_notification_campaigns_paginated.call_args.kwargs
    assert int(kwargs['requester_id']) == admin_user.id
    assert kwargs['page'] == 2
    assert kwargs['per_page'] == 5
    assert kwargs['search'] == 'spring'
    assert kwargs['status'] == 'draft'
    assert kwargs['channel'] == 'push'


def test_create_notification_campaign_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.create_notification_campaign.return_value = {
        'id': 202,
        'name': 'Weekend retention push',
        'channel': 'sms',
        'status': 'scheduled',
        'recipient_count': 44,
        'sent_count': 0,
    }
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    payload = {
        'name': 'Weekend retention push',
        'channel': 'phone',
        'subject': 'Weekend special',
        'content': 'Save 10% this weekend',
        'target_audience': 'all_customers',
        'priority': 'high',
        'status': 'scheduled',
    }

    response = client.post(
        '/api/v1/admin/notification-campaigns',
        headers=_admin_headers(app, admin_user.id),
        json=payload,
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body['data']['campaign']['id'] == 202
    service.create_notification_campaign.assert_called_once()
    kwargs = service.create_notification_campaign.call_args.kwargs
    assert int(kwargs['sender_id']) == admin_user.id
    assert kwargs['payload'] == payload


def test_get_notification_campaign_detail_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_notification_campaign_detail.return_value = {'id': 303, 'name': 'Detail campaign'}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.get(
        '/api/v1/admin/notification-campaigns/303',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    assert response.get_json()['data']['campaign']['id'] == 303
    service.get_notification_campaign_detail.assert_called_once_with(
        requester_id=str(admin_user.id),
        campaign_id=303,
    )


def test_update_notification_campaign_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.update_notification_campaign.return_value = {'id': 404, 'status': 'draft'}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    payload = {'name': 'Updated campaign', 'notification_type': 'promotional', 'channel': 'telegram', 'target_audience': 'all_customers'}
    response = client.put(
        '/api/v1/admin/notification-campaigns/404',
        headers=_admin_headers(app, admin_user.id),
        json=payload,
    )

    assert response.status_code == 200
    service.update_notification_campaign.assert_called_once_with(
        sender_id=str(admin_user.id),
        campaign_id=404,
        payload=payload,
    )


def test_send_notification_campaign_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.queue_notification_campaign.return_value = {'id': 505, 'status': 'scheduled'}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.post(
        '/api/v1/admin/notification-campaigns/505/send',
        headers=_admin_headers(app, admin_user.id),
        json={'send_now': False},
    )

    assert response.status_code == 200
    service.queue_notification_campaign.assert_called_once_with(
        sender_id=str(admin_user.id),
        campaign_id=505,
        send_now=False,
    )


def test_cancel_notification_campaign_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.cancel_notification_campaign.return_value = {'id': 606, 'status': 'cancelled'}
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.post(
        '/api/v1/admin/notification-campaigns/606/cancel',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.cancel_notification_campaign.assert_called_once_with(
        sender_id=str(admin_user.id),
        campaign_id=606,
    )
