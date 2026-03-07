"""Route tests for admin debt-aware customer payment method lookup."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.models.user import User
from business_app.utils.constants import UserRole, UserType
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
        email='operator.payment.methods@example.com',
        phone='+998901117755',
        password_hash=hash_password('OperatorPassword123!'),
        first_name='Operator',
        last_name='Payments',
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
    )
    db.session.add(operator)
    db.session.commit()
    return operator


def test_admin_get_user_payment_methods_route_delegates_to_staff_service(
    client,
    app,
    admin_user,
    sample_user,
    monkeypatch,
):
    payload = {
        'customer_id': sample_user.id,
        'available_methods': [{'method': 'payme'}],
        'payment_restrictions': {'cod_restricted': True},
    }
    mocked_method = Mock(return_value=payload)
    monkeypatch.setattr(
        'business_app.services.staff_service.StaffService.get_client_payment_methods',
        mocked_method,
    )

    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/payment-methods',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['data']['payment_restrictions']['cod_restricted'] is True
    mocked_method.assert_called_once_with(sample_user.id)


def test_non_manager_cannot_get_user_payment_methods(
    client,
    app,
    db,
    sample_user,
):
    operator_user = _create_operator_user(db)

    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/payment-methods',
        headers=_operator_headers(app, operator_user.id),
    )

    assert response.status_code == 403
