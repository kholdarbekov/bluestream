"""Route-level regressions for admin try-out endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_get_admin_tryouts_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.list_tryouts.return_value = {
        'items': [{'id': 1, 'tryout_number': 'TRY_000001_26'}],
        'page': 1,
        'per_page': 20,
        'total': 1,
        'summary': {'active_tryouts': 1, 'total_tryouts': 1},
    }
    monkeypatch.setattr('business_app.api.admin_tryouts.AdminTryoutService', service)

    response = client.get(
        '/api/v1/admin/tryouts?search=TRY&status=active&pickup_state=overdue',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.list_tryouts.assert_called_once_with(
        page=1,
        per_page=20,
        search='TRY',
        status='active',
        outcome=None,
        pickup_state='overdue',
        driver_id=None,
        start_date=None,
        end_date=None,
        due_start_date=None,
        due_end_date=None,
    )
    assert response.get_json()['data']['items'][0]['tryout_number'] == 'TRY_000001_26'


def test_create_admin_tryout_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.create_tryout.return_value = {'id': 5}
    service.serialize_tryout.return_value = {'id': 5, 'tryout_number': 'TRY_000005_26'}
    monkeypatch.setattr('business_app.api.admin_tryouts.TryoutService', service)

    response = client.post(
        '/api/v1/admin/tryouts',
        headers=_auth_headers(app, admin_user.id),
        json={
            'trial_contact': {'first_name': 'Trial', 'phone': '+998901112233'},
            'address': {'full_address': 'Sample address'},
            'items': [{'product_id': 1, 'quantity': 2}],
        },
    )

    assert response.status_code == 201
    service.create_tryout.assert_called_once()
    assert response.get_json()['data']['tryout']['tryout_number'] == 'TRY_000005_26'


def test_convert_admin_tryout_route_returns_conversion_metadata(client, app, admin_user, monkeypatch):
    service = Mock()
    service.convert_tryout.return_value = {
        'tryout': {'id': 7},
        'action': 'linked_existing_user',
        'user': Mock(id=42, full_name='Existing User', phone='+998901112233'),
    }
    service.serialize_tryout.return_value = {
        'id': 7,
        'tryout_number': 'TRY_000007_26',
        'converted_user': {
            'id': 42,
            'full_name': 'Existing User',
            'phone': '+998901112233',
        },
    }
    monkeypatch.setattr('business_app.api.admin_tryouts.TryoutService', service)

    response = client.post(
        '/api/v1/admin/tryouts/7/convert',
        headers=_auth_headers(app, admin_user.id),
        json={},
    )

    assert response.status_code == 200
    service.convert_tryout.assert_called_once_with(7, str(admin_user.id))
    payload = response.get_json()['data']
    assert payload['tryout']['tryout_number'] == 'TRY_000007_26'
    assert payload['conversion']['action'] == 'linked_existing_user'
    assert payload['conversion']['user']['id'] == 42
