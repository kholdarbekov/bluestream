"""Route-level regressions for migrated orders API/service boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.utils.constants import OrderStatus, PaymentMethod


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def _create_order(db, user_id: int) -> Order:
    address = UserAddress(
        user_id=user_id,
        title='Home',
        full_address='Street 1',
        street_address='Street 1',
        city='Tashkent',
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()

    order = Order(
        order_number='ORD-API-1',
        user_id=user_id,
        status=OrderStatus.PENDING,
        subtotal=Decimal('10000'),
        delivery_fee=Decimal('0'),
        total_amount=Decimal('10000'),
        delivery_address_id=address.id,
        payment_method=PaymentMethod.CASH,
        order_source='web',
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_get_order_statistics_route_delegates_to_service(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_user_order_statistics.return_value = {
        'period': 'year',
        'statistics': {'total_orders': 2, 'total_spent': 50000},
    }
    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)

    response = client.get('/api/v1/orders/statistics?period=year', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    service.get_user_order_statistics.assert_called_once()


def test_repeat_order_route_uses_repeat_order_for_user(client, app, db, sample_user, monkeypatch):
    created_order = _create_order(db, sample_user.id)

    service = Mock()
    service.repeat_order_for_user.return_value = created_order
    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)

    response = client.post(f'/api/v1/orders/repeat/{created_order.id}', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 201
    service.repeat_order_for_user.assert_called_once_with(created_order.id, str(sample_user.id))


def test_create_subscription_order_accepts_service_dict_response(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_user_or_raise.return_value = sample_user
    service.create_subscription_order.return_value = {
        'id': 42,
        'status': 'active',
        'delivery_frequency': 'weekly',
        'next_delivery_date': None,
        'created_at': datetime.now(UTC).isoformat(),
    }
    notification_service = Mock()

    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)
    monkeypatch.setattr('business_app.api.orders.get_notification_service', lambda: notification_service)

    response = client.post(
        '/api/v1/orders/subscription',
        headers=_auth_headers(app, sample_user.id),
        json={
            'items': [{'product_id': 1, 'quantity': 2}],
            'frequency': 'weekly',
            'delivery_address_id': 10,
            'auto_pay': True,
        },
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data['data']['subscription']['id'] == 42


def test_schedule_order_route_delegates_to_service_and_schedules_task(client, app, db, sample_user, monkeypatch):
    created_order = _create_order(db, sample_user.id)

    service = Mock()
    service.get_user_or_raise.return_value = sample_user
    service.create_scheduled_order.return_value = created_order

    monkeypatch.setattr('business_app.api.orders.get_order_service', lambda: service)

    scheduled_for = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    with patch('business_app.tasks.order_tasks.process_scheduled_order_task.apply_async') as apply_async:
        response = client.post(
            '/api/v1/orders/schedule',
            headers=_auth_headers(app, sample_user.id),
            json={
                'items': [{'product_id': 1, 'quantity': 2}],
                'delivery_address_id': created_order.delivery_address_id,
                'scheduled_date': scheduled_for,
                'delivery_time_slot': '09:00-12:00',
            },
        )

    assert response.status_code == 201
    service.create_scheduled_order.assert_called_once()
    apply_async.assert_called_once()
