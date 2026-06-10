"""Integration tests for current API endpoint contracts."""

from datetime import date, timedelta

import pytest

from business_app.models.delivery import DeliveryTimeSlot


@pytest.mark.integration
@pytest.mark.api
class TestAuthenticationAPI:
    def test_user_registration_returns_user_and_tokens(self, client, db):
        payload = {
            'email': 'new-user@example.com',
            'phone': '+998901231111',
            'password': 'StrongPass123!',
            'first_name': 'New',
            'last_name': 'User',
        }

        response = client.post('/api/v1/auth/register', json=payload)

        assert response.status_code == 201
        body = response.get_json()
        assert body['success'] is True
        assert body['data']['user']['email'] == payload['email']
        assert 'access_token' in body['data']['tokens']
        assert 'refresh_token' in body['data']['tokens']

    def test_user_login_success(self, client, sample_user):
        response = client.post(
            '/api/v1/auth/login',
            json={'identifier': sample_user.email, 'password': 'TestPassword123!'},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body['success'] is True
        assert body['data']['user']['id'] == sample_user.id
        assert 'access_token' in body['data']['tokens']

    def test_user_login_invalid_credentials(self, client, db):
        response = client.post(
            '/api/v1/auth/login',
            json={'identifier': 'missing@example.com', 'password': 'bad-password'},
        )

        assert response.status_code == 401
        assert response.get_json()['success'] is False

    def test_refresh_token_flow(self, client, sample_user):
        login_response = client.post(
            '/api/v1/auth/login',
            json={'identifier': sample_user.email, 'password': 'TestPassword123!'},
        )
        refresh_token = login_response.get_json()['data']['tokens']['refresh_token']

        refresh_response = client.post('/api/v1/auth/refresh', json={'refresh_token': refresh_token})

        assert refresh_response.status_code == 200
        body = refresh_response.get_json()
        assert body['success'] is True
        assert 'access_token' in body['data']

    def test_check_phone_availability_rejects_invalid_phone(self, client, db):
        # An un-normalizable phone must be rejected at the boundary with a clean
        # 400, never reach the service and return 200 with available:false (which
        # would degrade into a filter_by(phone=None) collision).
        response = client.post(
            '/api/v1/auth/check-phone-availability',
            json={'phone': 'garbage', 'telegram_id': 987654321},
        )

        assert response.status_code == 400

    def test_link_phone_send_otp_rejects_invalid_phone(self, client, db):
        response = client.post(
            '/api/v1/auth/link-phone-account/send-otp',
            json={'phone': 'garbage', 'telegram_id': 987654322},
        )

        assert response.status_code == 400


@pytest.mark.integration
@pytest.mark.api
class TestProductsAPI:
    def test_get_products_list(self, client, sample_product):
        response = client.get('/api/v1/products/')

        assert response.status_code == 200
        body = response.get_json()
        assert body['success'] is True
        assert 'items' in body['data']

    def test_get_product_details_success(self, client, sample_product):
        response = client.get(f'/api/v1/products/{sample_product.id}')

        assert response.status_code == 200
        body = response.get_json()
        assert body['success'] is True
        assert body['data']['product']['id'] == sample_product.id

    def test_get_product_details_not_found(self, client, db):
        response = client.get('/api/v1/products/999999')

        assert response.status_code == 404
        assert response.get_json()['success'] is False

    def test_get_categories(self, client, sample_category):
        response = client.get('/api/v1/products/categories')

        assert response.status_code == 200
        body = response.get_json()
        assert body['success'] is True
        assert 'categories' in body['data']

    def test_search_suggestions(self, client, sample_product):
        response = client.get('/api/v1/products/search-suggestions?q=Wa')

        assert response.status_code == 200
        body = response.get_json()
        assert body['success'] is True
        assert 'suggestions' in body['data']


@pytest.mark.integration
@pytest.mark.api
class TestOrdersAndDeliveryAPI:
    @pytest.fixture
    def delivery_slot(self, db):
        slot = DeliveryTimeSlot(
            name='Morning',
            start_time='09:00',
            end_time='12:00',
            is_active=True,
            max_orders=50,
            available_days=[0, 1, 2, 3, 4, 5, 6],
        )
        db.session.add(slot)
        db.session.commit()
        return slot

    def test_orders_endpoint_requires_auth(self, app, db):
        isolated_client = app.test_client(use_cookies=False)
        response = isolated_client.get('/api/v1/orders/')
        assert response.status_code == 401

    def test_get_orders_with_auth(self, client, auth_headers, sample_order):
        response = client.get('/api/v1/orders/', headers=auth_headers)

        assert response.status_code == 200
        body = response.get_json()
        assert body['success'] is True
        assert 'orders' in body['data']

    def test_get_order_not_found_for_user(self, client, auth_headers, db):
        response = client.get('/api/v1/orders/999999', headers=auth_headers)

        assert response.status_code == 404
        assert response.get_json()['success'] is False

    def test_cancel_nonexistent_order_returns_not_found(self, client, auth_headers, db):
        response = client.post('/api/v1/orders/999999/cancel', json={'reason': 'test'}, headers=auth_headers)

        assert response.status_code == 404
        assert response.get_json()['success'] is False

    def test_delivery_time_slots_requires_date(self, client):
        response = client.get('/api/v1/delivery/time-slots')
        assert response.status_code == 400

    def test_delivery_time_slots_success(self, client, delivery_slot):
        target_date = (date.today() + timedelta(days=1)).isoformat()

        response = client.get(f'/api/v1/delivery/time-slots?date={target_date}')

        assert response.status_code == 200
        body = response.get_json()
        assert body['date'] == target_date
        assert 'time_slots' in body

    def test_delivery_time_slots_uses_aggregated_booking_query(self, client, delivery_slot, monkeypatch):
        target_date = (date.today() + timedelta(days=1)).isoformat()

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("Per-slot booking count helper should not be called")

        monkeypatch.setattr(DeliveryTimeSlot, "get_current_orders_count", _fail_if_called)

        response = client.get(f'/api/v1/delivery/time-slots?date={target_date}')

        assert response.status_code == 200

    def test_calculate_delivery_fee_requires_auth(self, client):
        response = client.post('/api/v1/delivery/calculate-fee', json={'address_id': 1, 'order_total': 20000})
        assert response.status_code == 401
