"""Route-level regressions for admin delivery management endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_get_admin_deliveries_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.list_deliveries.return_value = {
        'items': [{'id': 1, 'delivery_id': 'DLV-000001'}],
        'page': 2,
        'per_page': 10,
        'total': 14,
        'summary': {'total_deliveries': 14, 'active_deliveries': 3},
    }
    monkeypatch.setattr('business_app.api.admin.AdminDeliveryService', service)

    response = client.get(
        '/api/v1/admin/deliveries?page=2&per_page=10&search=ORD&status=assigned',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.list_deliveries.assert_called_once_with(
        page=2,
        per_page=10,
        search='ORD',
        status='assigned',
        start_date=None,
        end_date=None,
    )
    body = response.get_json()
    assert body['data']['items'][0]['delivery_id'] == 'DLV-000001'
    assert body['meta']['summary']['active_deliveries'] == 3


def test_update_admin_delivery_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.update_delivery.return_value = {
        'id': 7,
        'delivery_id': 'DLV-000007',
        'status': 'in_transit',
        'notes': 'Left warehouse',
    }
    monkeypatch.setattr('business_app.api.admin.AdminDeliveryService', service)

    response = client.put(
        '/api/v1/admin/deliveries/7',
        headers=_auth_headers(app, admin_user.id),
        json={'status': 'in_transit', 'notes': 'Left warehouse'},
    )

    assert response.status_code == 200
    service.update_delivery.assert_called_once_with(
        7,
        {'status': 'in_transit', 'notes': 'Left warehouse'},
        admin_user.id,
    )
    body = response.get_json()
    assert body['data']['delivery']['status'] == 'in_transit'
