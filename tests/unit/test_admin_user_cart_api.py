"""Route tests for the admin read-only user cart view."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.models.user import User
from shared.enums import UserRole, UserType
from business_app.utils.password_security import hash_password


def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.ADMIN.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _operator_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.OPERATOR.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _create_operator_user(db) -> User:
    operator = User(
        email='operator.user.cart@example.com',
        phone='+998901119911',
        password_hash=hash_password('OperatorPassword123!'),
        first_name='Operator',
        last_name='Cart',
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
    )
    db.session.add(operator)
    db.session.commit()
    return operator


def test_admin_get_user_cart_returns_cart_payload(client, app, admin_user, sample_user, monkeypatch):
    payload = {
        'cart_items': [
            {
                'id': 1,
                'product_id': 5,
                'quantity': 2,
                'unit_price': 12000.0,
                'total_price': 24000.0,
                'product': {'id': 5, 'name': 'Aqua 1.5L'},
            }
        ],
        'item_count': 2,
        'subtotal': 24000.0,
        'estimated_total': 24000.0,
        'updated_at': '2026-06-29T10:00:00+00:00',
    }
    mocked = Mock(return_value=payload)
    monkeypatch.setattr(
        'business_app.services.cart_service.CartService.get_cart_details',
        mocked,
    )

    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/cart',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body['success'] is True
    assert body['data']['item_count'] == 2
    assert body['data']['cart_items'][0]['product']['name'] == 'Aqua 1.5L'
    assert body['data']['cart_items'][0]['total_price'] == 24000.0
    mocked.assert_called_once_with(sample_user.id)


def test_admin_get_user_cart_normalizes_empty_when_no_cart(client, app, admin_user, sample_user, monkeypatch):
    monkeypatch.setattr(
        'business_app.services.cart_service.CartService.get_cart_details',
        Mock(return_value=None),
    )

    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/cart',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body['data']['cart_items'] == []
    assert body['data']['item_count'] == 0
    assert body['data']['estimated_total'] == 0
    assert body['data']['subtotal'] == 0
    assert body['data']['estimated_delivery_fee'] == 0
    assert body['data']['updated_at'] is None


def test_admin_get_user_cart_404_for_missing_user(client, app, admin_user):
    response = client.get(
        '/api/v1/admin/users/99999999/cart',
        headers=_admin_headers(app, admin_user.id),
    )
    assert response.status_code == 404


def test_operator_without_view_users_cannot_get_user_cart(client, app, db, sample_user):
    operator_user = _create_operator_user(db)
    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/cart',
        headers=_operator_headers(app, operator_user.id),
    )
    assert response.status_code == 403
