"""Integration tests for current API endpoint contracts."""

from datetime import date, time, timedelta

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

    def test_delivery_time_slots_uses_aggregated_booking_query(self, client, delivery_slot):
        """The per-slot booking counter is GONE, not merely unused.

        It used to be monkeypatched to fail-if-called; it counted one query per
        slot by matching the free-text `orders.delivery_time_slot` against
        "start-end". That column no longer exists (migration c9e4a1f7b3d2) and
        an open-ended window cannot be matched that way at all, so the helper
        was deleted rather than ported — the endpoint aggregates bookings in a
        single grouped query. Asserting its absence is what keeps a per-slot
        counter from growing back.
        """
        target_date = (date.today() + timedelta(days=1)).isoformat()

        assert not hasattr(DeliveryTimeSlot, "get_current_orders_count")

        response = client.get(f'/api/v1/delivery/time-slots?date={target_date}')

        assert response.status_code == 200

    def test_slot_capacity_counts_only_orders_that_actually_book_that_slot(
        self, client, db, delivery_slot, sample_user
    ):
        """`available_capacity` gates a customer-facing choice, so it must not
        count orders that booked something else — or nothing at all.

        The window replacing `delivery_time_slot` is open-ended, which breaks
        two assumptions the old free-text key relied on:

        * an "after 19:00" order books no fixed slot, so it must not consume
          the 19:00-21:00 slot's capacity;
        * two active slots may share a start minute (09:00-12:00 / 09:00-18:00)
          and must not consume each other's.

        Both would hide a slot the customer may legitimately pick, since
        `is_available` is `available_capacity > 0` and the checkout renders
        only available slots.
        """
        from business_app.models.order import Order
        from shared.enums import OrderStatus

        target = date.today() + timedelta(days=1)
        db.session.add_all([
            DeliveryTimeSlot(
                name='Long', start_time='09:00', end_time='18:00', is_active=True,
                max_orders=50, available_days=[0, 1, 2, 3, 4, 5, 6],
            ),
            DeliveryTimeSlot(
                name='Evening', start_time='19:00', end_time='21:00', is_active=True,
                max_orders=50, available_days=[0, 1, 2, 3, 4, 5, 6],
            ),
        ])
        for number, start, end in [
            ('ORD-CAP-CLOSED', time(9, 0), time(12, 0)),   # books Morning only
            ('ORD-CAP-AFTER', time(19, 0), None),          # "after 19:00" — books nothing
            ('ORD-CAP-ANYTIME', None, None),               # "anytime" — books nothing
        ]:
            db.session.add(Order(
                user_id=sample_user.id, order_number=number, status=OrderStatus.CONFIRMED,
                total_amount=50000, delivery_date=target,
                delivery_window_start=start, delivery_window_end=end,
            ))
        db.session.commit()

        response = client.get(f'/api/v1/delivery/time-slots?date={target.isoformat()}')

        assert response.status_code == 200
        capacity = {
            (s['start_time'], s['end_time']): s['available_capacity']
            for s in response.get_json()['time_slots']
        }
        assert capacity[('09:00', '12:00')] == 49, 'the closed 09:00-12:00 window books this slot'
        assert capacity[('09:00', '18:00')] == 50, 'a shared start minute is not a shared booking'
        assert capacity[('19:00', '21:00')] == 50, '"after 19:00" books no slot'

    def test_calculate_delivery_fee_requires_auth(self, client):
        response = client.post('/api/v1/delivery/calculate-fee', json={'address_id': 1, 'order_total': 20000})
        assert response.status_code == 401
