"""Route tests for admin on-behalf customer notification settings management."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.models.user import User
from shared.enums import UserRole, UserType
from business_app.utils.exceptions import NotFoundError
from business_app.utils.password_security import hash_password


def _admin_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.ADMIN.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _manager_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.MANAGER.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _create_manager_user(db) -> User:
    manager = User(
        email='manager.notification@example.com',
        phone='+998901119977',
        password_hash=hash_password('ManagerPassword123!'),
        first_name='Manager',
        last_name='Notifications',
        user_type=UserType.STAFF,
        role=UserRole.MANAGER,
        is_verified=True,
    )
    db.session.add(manager)
    db.session.commit()
    return manager


def _create_operator_user(db) -> User:
    operator = User(
        email='operator.notification@example.com',
        phone='+998901119966',
        password_hash=hash_password('OperatorPassword123!'),
        first_name='Operator',
        last_name='Notifications',
        user_type=UserType.STAFF,
        role=UserRole.OPERATOR,
        is_verified=True,
    )
    db.session.add(operator)
    db.session.commit()
    return operator


def _operator_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(
            identity=str(user_id),
            additional_claims={'role': UserRole.OPERATOR.value},
        )
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_admin_get_user_notification_settings_route_delegates_to_service(
    client,
    app,
    admin_user,
    sample_user,
    monkeypatch,
):
    service = Mock()
    service.get_delivery_telegram_status_updates_setting.return_value = {
        'delivery_telegram_status_updates_enabled': True,
        'delivery_telegram_status_updates_source': 'default',
        'telegram_connected': True,
        'bot_active': True,
        'updated_at': None,
    }
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['data']['notification_settings']['delivery_telegram_status_updates_enabled'] is True
    service.get_delivery_telegram_status_updates_setting.assert_called_once_with(sample_user.id)


def test_manager_updates_user_notification_settings_route(
    client,
    app,
    db,
    sample_user,
    monkeypatch,
):
    manager_user = _create_manager_user(db)
    service = Mock()
    service.set_delivery_telegram_status_updates_setting.return_value = {
        'delivery_telegram_status_updates_enabled': False,
        'delivery_telegram_status_updates_source': 'explicit',
        'telegram_connected': True,
        'bot_active': True,
        'updated_at': '2026-03-05T10:00:00+00:00',
    }
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.put(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_manager_headers(app, manager_user.id),
        json={
            'delivery_telegram_status_updates_enabled': False,
            'reason': 'Customer requested disable via phone',
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['data']['notification_settings']['delivery_telegram_status_updates_enabled'] is False
    service.set_delivery_telegram_status_updates_setting.assert_called_once_with(
        user_id=sample_user.id,
        enabled=False,
        source='admin',
        actor_user_id=manager_user.id,
        reason='Customer requested disable via phone',
    )


def test_update_user_notification_settings_route_validates_reason(
    client,
    app,
    db,
    sample_user,
    monkeypatch,
):
    manager_user = _create_manager_user(db)
    service = Mock()
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.put(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_manager_headers(app, manager_user.id),
        json={
            'delivery_telegram_status_updates_enabled': False,
            'reason': '   ',
        },
    )

    assert response.status_code == 400
    service.set_delivery_telegram_status_updates_setting.assert_not_called()


def test_update_user_notification_settings_route_validates_boolean_toggle(
    client,
    app,
    db,
    sample_user,
    monkeypatch,
):
    manager_user = _create_manager_user(db)
    service = Mock()
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.put(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_manager_headers(app, manager_user.id),
        json={
            'delivery_telegram_status_updates_enabled': 'off',
            'reason': 'Customer requested disable via phone',
        },
    )

    assert response.status_code == 400
    service.set_delivery_telegram_status_updates_setting.assert_not_called()


def test_get_user_notification_settings_route_returns_not_found_when_user_missing(
    client,
    app,
    admin_user,
    sample_user,
    monkeypatch,
):
    service = Mock()
    service.get_delivery_telegram_status_updates_setting.side_effect = NotFoundError('User not found')
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.get(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_admin_headers(app, admin_user.id),
    )

    assert response.status_code == 404


def test_update_user_notification_settings_route_returns_not_found_when_user_missing(
    client,
    app,
    db,
    sample_user,
    monkeypatch,
):
    manager_user = _create_manager_user(db)
    service = Mock()
    service.set_delivery_telegram_status_updates_setting.side_effect = NotFoundError('User not found')
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.put(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_manager_headers(app, manager_user.id),
        json={
            'delivery_telegram_status_updates_enabled': True,
            'reason': 'Customer called support',
        },
    )

    assert response.status_code == 404


def test_update_user_notification_settings_route_forbids_operator_role(
    client,
    app,
    db,
    sample_user,
    monkeypatch,
):
    operator_user = _create_operator_user(db)
    service = Mock()
    monkeypatch.setattr('business_app.api.admin.get_notification_service', lambda: service)

    response = client.put(
        f'/api/v1/admin/users/{sample_user.id}/notification-settings',
        headers=_operator_headers(app, operator_user.id),
        json={
            'delivery_telegram_status_updates_enabled': False,
            'reason': 'Customer requested disable via phone',
        },
    )

    assert response.status_code == 403
    service.set_delivery_telegram_status_updates_setting.assert_not_called()
