"""Service-level regressions for orders API boundary migration."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from business_app.models.order import Order, OrderItem
from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService
from business_app.utils.constants import OrderStatus, PaymentMethod
from business_app.utils.exceptions import ForbiddenError, ValidationError


@pytest.fixture
def order_service(app, mock_inventory_service):
    with app.app_context():
        service = OrderService(inventory_service=mock_inventory_service)
        return service


def _create_address(db, user_id: int) -> UserAddress:
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
    db.session.commit()
    return address


def _create_order(db, user_id: int, address_id: int, order_number: str, status: OrderStatus, total: Decimal) -> Order:
    order = Order(
        order_number=order_number,
        user_id=user_id,
        status=status,
        subtotal=total,
        delivery_fee=Decimal('0'),
        total_amount=total,
        delivery_address_id=address_id,
        payment_method=PaymentMethod.CASH,
        order_source='web',
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_get_user_order_statistics_aggregates_without_runtime_errors(order_service, db, sample_user):
    address = _create_address(db, sample_user.id)
    _create_order(db, sample_user.id, address.id, 'ORD-STAT-1', OrderStatus.DELIVERED, Decimal('25000'))
    _create_order(db, sample_user.id, address.id, 'ORD-STAT-2', OrderStatus.CANCELLED, Decimal('15000'))

    result = order_service.get_user_order_statistics(sample_user.id, period='month')

    assert result['period'] == 'month'
    assert result['statistics']['total_orders'] == 2
    assert result['statistics']['total_spent'] == 40000.0
    assert result['statistics']['orders_by_status']['delivered'] == 1
    assert result['statistics']['orders_by_status']['cancelled'] == 1


def test_repeat_order_for_user_builds_payload_with_address(order_service, db, sample_user, sample_product):
    address = _create_address(db, sample_user.id)
    original = _create_order(db, sample_user.id, address.id, 'ORD-REP-1', OrderStatus.DELIVERED, Decimal('30000'))

    item = OrderItem(
        order_id=original.id,
        product_id=sample_product.id,
        quantity=2,
        unit_price=Decimal('15000'),
        total_price=Decimal('30000'),
    )
    db.session.add(item)
    db.session.commit()

    created_order = Mock(id=999)

    with patch.object(order_service, 'create_order', return_value=created_order) as create_order_mock:
        result = order_service.repeat_order_for_user(original.id, sample_user.id)

    create_order_mock.assert_called_once()
    call_user_id = create_order_mock.call_args.args[0]
    payload = create_order_mock.call_args.args[1]

    assert call_user_id == sample_user.id
    assert payload['delivery_address']['delivery_address_id'] == address.id
    assert payload['items'][0]['product_id'] == sample_product.id
    assert result is created_order


def test_perform_bulk_action_requires_admin(order_service, db, sample_user):
    with pytest.raises(ForbiddenError):
        order_service.perform_bulk_action('confirm', [1], sample_user.id)


def test_perform_bulk_action_cancel_uses_actor_id_without_order_ownership(order_service, db, sample_user, admin_user):
    address = _create_address(db, sample_user.id)
    order = _create_order(db, sample_user.id, address.id, 'ORD-BULK-CANCEL-1', OrderStatus.PENDING, Decimal('22000'))

    with patch.object(order_service, 'cancel_order') as cancel_order_mock:
        result = order_service.perform_bulk_action('cancel', [order.id], admin_user.id)

    assert result == [{'order_id': order.id, 'success': True}]
    cancel_order_mock.assert_called_once_with(order.id, reason='Bulk cancellation', actor_user_id=admin_user.id)


def test_create_subscription_order_delegates_to_subscription_service(order_service, sample_user):
    subscription_result = {
        'id': 55,
        'status': 'active',
        'delivery_frequency': 'weekly',
        'next_delivery_date': None,
        'created_at': datetime.now(UTC).isoformat(),
    }

    with patch('business_app.utils.service_factory.get_subscription_service') as get_service:
        get_service.return_value.create_subscription.return_value = subscription_result

        result = order_service.create_subscription_order(
            {
                'user_id': sample_user.id,
                'delivery_address_id': 123,
                'frequency': 'weekly',
                'start_date': datetime.now(UTC).isoformat(),
                'payment_method': 'cash',
            },
            [{'product_id': 1, 'quantity': 1}],
        )

    assert result['id'] == 55


def test_create_scheduled_order_rejects_past_datetime(order_service, db, sample_user):
    _create_address(db, sample_user.id)

    with pytest.raises(ValidationError):
        order_service.create_scheduled_order(
            {
                'user_id': sample_user.id,
                'delivery_address_id': 1,
                'scheduled_date': (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            },
            [{'product_id': 1, 'quantity': 1}],
        )
