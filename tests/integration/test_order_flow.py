"""Integration tests for current order-related API flows."""

from decimal import Decimal

import pytest

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.order import Order
from shared.enums import OrderStatus
@pytest.mark.integration
@pytest.mark.order
class TestOrderFlow:
    @pytest.fixture
    def delivery_slot(self, db):
        slot = DeliveryTimeSlot(
            name='Evening',
            start_time='18:00',
            end_time='21:00',
            is_active=True,
            max_orders=50,
            available_days=[0, 1, 2, 3, 4, 5, 6],
        )
        db.session.add(slot)
        db.session.commit()
        return slot

    def test_authenticated_user_can_browse_products_and_orders(self, client, auth_headers, sample_product):
        products_response = client.get('/api/v1/products/')
        orders_response = client.get('/api/v1/orders/', headers=auth_headers)

        assert products_response.status_code == 200
        assert orders_response.status_code == 200
        assert products_response.get_json()['success'] is True
        assert orders_response.get_json()['success'] is True

    def test_order_list_is_scoped_to_current_user(self, client, db, sample_user, admin_user, auth_headers):
        user_order = Order(
            user_id=sample_user.id,
            status=OrderStatus.PENDING,
            subtotal=Decimal('20000.00'),
            total_amount=Decimal('20000.00'),
        )
        admin_order = Order(
            user_id=admin_user.id,
            status=OrderStatus.PENDING,
            subtotal=Decimal('25000.00'),
            total_amount=Decimal('25000.00'),
        )
        db.session.add_all([user_order, admin_order])
        db.session.commit()

        response = client.get('/api/v1/orders/', headers=auth_headers)

        assert response.status_code == 200
        orders = response.get_json()['data']['orders']
        assert len(orders) >= 1
        assert all(order['user_id'] == sample_user.id for order in orders)

    def test_user_cannot_access_other_user_order_details(self, client, db, admin_user, auth_headers):
        foreign_order = Order(
            user_id=admin_user.id,
            status=OrderStatus.PENDING,
            subtotal=Decimal('30000.00'),
            total_amount=Decimal('30000.00'),
        )
        db.session.add(foreign_order)
        db.session.commit()

        response = client.get(f'/api/v1/orders/{foreign_order.id}', headers=auth_headers)

        assert response.status_code == 404
        assert response.get_json()['success'] is False

    def test_delivery_slots_missing_date_returns_validation_error(self, client, auth_headers, delivery_slot):
        response = client.get('/api/v1/orders/delivery-slots', headers=auth_headers)

        assert response.status_code == 400

    def test_cancel_nonexistent_order_returns_not_found(self, client, auth_headers):
        response = client.post('/api/v1/orders/999999/cancel', json={'reason': 'test'}, headers=auth_headers)

        assert response.status_code == 404
        assert response.get_json()['success'] is False

    def test_delivery_fee_validation_with_auth(self, client, auth_headers):
        response = client.post('/api/v1/delivery/calculate-fee', json={'order_total': 20000}, headers=auth_headers)

        assert response.status_code == 400
