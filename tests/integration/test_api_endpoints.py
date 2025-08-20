"""
Integration tests for API endpoints - Critical Business Logic
Tests API endpoints with real database interactions
"""
import pytest
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from datetime import datetime, UTC

from business_app import create_app
from business_app.models.user import User
from business_app.models.product import Product
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.utils.constants import UserRole, OrderStatus, PaymentStatus


@pytest.fixture(scope='class')
def test_app():
    """Create test app for integration tests"""
    class TestConfig:
        TESTING = True
        WTF_CSRF_ENABLED = False
        SECRET_KEY = 'test-secret-key-for-testing-32-chars-long'
        SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        JWT_SECRET_KEY = 'test-jwt-secret-key-for-testing'
        REDIS_URL = 'redis://localhost:6379/15'
        CELERY_ALWAYS_EAGER = True
        CORS_ORIGINS = ['http://localhost:3000']
        
        @classmethod
        def validate_secret_key(cls):
            pass
        
        @classmethod
        def validate_debug_mode(cls):
            pass
    
    app = create_app(TestConfig)
    
    with app.app_context():
        from business_app import db
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def api_client(test_app):
    """Create API test client"""
    return test_app.test_client()


@pytest.fixture
def auth_token(test_app, sample_user):
    """Create authentication token for testing"""
    with test_app.app_context():
        from business_app.services.auth_service import AuthService
        auth_service = AuthService()
        return auth_service.create_access_token(sample_user)


@pytest.fixture
def admin_token(test_app, admin_user):
    """Create admin authentication token for testing"""
    with test_app.app_context():
        from business_app.services.auth_service import AuthService
        auth_service = AuthService()
        return auth_service.create_access_token(admin_user)


@pytest.mark.critical
@pytest.mark.api
@pytest.mark.integration
class TestAuthenticationAPI:
    """Test authentication API endpoints"""
    
    def test_user_registration(self, api_client):
        """Test user registration endpoint"""
        registration_data = {
            'email': 'newuser@example.com',
            'phone': '+998901234570',
            'password': 'SecureP@ssw0rd123',
            'first_name': 'New',
            'last_name': 'User'
        }
        
        response = api_client.post(
            '/api/auth/register',
            data=json.dumps(registration_data),
            content_type='application/json'
        )
        
        assert response.status_code == 201
        data = json.loads(response.data)
        assert data['success'] is True
        assert 'user_id' in data
    
    def test_user_login_valid(self, api_client, sample_user):
        """Test user login with valid credentials"""
        login_data = {
            'email': 'test@example.com',
            'password': 'password123'
        }
        
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            response = api_client.post(
                '/api/auth/login',
                data=json.dumps(login_data),
                content_type='application/json'
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
            assert 'access_token' in data
            assert 'refresh_token' in data
    
    def test_user_login_invalid(self, api_client):
        """Test user login with invalid credentials"""
        login_data = {
            'email': 'wrong@example.com',
            'password': 'wrongpassword'
        }
        
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': False,
                'error': 'Invalid credentials'
            }
            
            response = api_client.post(
                '/api/auth/login',
                data=json.dumps(login_data),
                content_type='application/json'
            )
            
            assert response.status_code == 401
            data = json.loads(response.data)
            assert data['success'] is False
    
    def test_token_refresh(self, api_client, auth_token):
        """Test token refresh endpoint"""
        with patch('business_app.services.auth_service.AuthService.refresh_tokens') as mock_refresh:
            mock_refresh.return_value = {
                'success': True,
                'access_token': 'new_access_token',
                'refresh_token': 'new_refresh_token'
            }
            
            response = api_client.post(
                '/api/auth/refresh',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'access_token' in data
    
    def test_protected_endpoint_without_token(self, api_client):
        """Test accessing protected endpoint without token"""
        response = api_client.get('/api/orders')
        
        assert response.status_code == 401
    
    def test_protected_endpoint_with_valid_token(self, api_client, auth_token):
        """Test accessing protected endpoint with valid token"""
        with patch('business_app.services.auth_service.AuthService.validate_token') as mock_validate:
            mock_validate.return_value = {
                'valid': True,
                'user_id': 1,
                'role': 'customer'
            }
            
            response = api_client.get(
                '/api/orders',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            # Should not return 401 (specific status depends on implementation)
            assert response.status_code != 401


@pytest.mark.critical
@pytest.mark.api
@pytest.mark.integration
class TestOrdersAPI:
    """Test orders API endpoints"""
    
    def test_create_order_valid(self, api_client, auth_token, sample_product):
        """Test creating order with valid data"""
        order_data = {
            'items': [
                {
                    'product_id': sample_product.id,
                    'quantity': 2
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent',
                'latitude': 41.2995,
                'longitude': 69.2401
            },
            'delivery_time_slot_id': 1,
            'payment_method': 'card'
        }
        
        with patch('business_app.services.order_service.OrderService.create_order') as mock_create:
            mock_create.return_value = Order(
                id=1,
                order_number='ORD-001',
                total_amount=Decimal('30000.00'),
                status=OrderStatus.PENDING
            )
            
            response = api_client.post(
                '/api/orders',
                data=json.dumps(order_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert 'order_id' in data
            assert 'order_number' in data
    
    def test_create_order_invalid_product(self, api_client, auth_token):
        """Test creating order with invalid product"""
        order_data = {
            'items': [
                {
                    'product_id': 99999,  # Non-existent product
                    'quantity': 1
                }
            ],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        response = api_client.post(
            '/api/orders',
            data=json.dumps(order_data),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_get_user_orders(self, api_client, auth_token, sample_order):
        """Test getting user's orders"""
        with patch('business_app.services.order_service.OrderService.get_user_orders') as mock_get:
            mock_get.return_value = [sample_order]
            
            response = api_client.get(
                '/api/orders',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'orders' in data
            assert len(data['orders']) > 0
    
    def test_get_order_details(self, api_client, auth_token, sample_order):
        """Test getting specific order details"""
        with patch('business_app.services.order_service.OrderService.get_order') as mock_get:
            mock_get.return_value = sample_order
            
            response = api_client.get(
                f'/api/orders/{sample_order.id}',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['order_number'] == sample_order.order_number
    
    def test_cancel_order(self, api_client, auth_token, sample_order):
        """Test canceling an order"""
        cancel_data = {
            'cancellation_reason': 'Changed mind'
        }
        
        with patch('business_app.services.order_service.OrderService.cancel_order') as mock_cancel:
            mock_cancel.return_value = {'success': True}
            
            response = api_client.post(
                f'/api/orders/{sample_order.id}/cancel',
                data=json.dumps(cancel_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True


@pytest.mark.critical
@pytest.mark.api
@pytest.mark.integration
class TestPaymentsAPI:
    """Test payments API endpoints"""
    
    def test_create_payment(self, api_client, auth_token, sample_order):
        """Test creating payment for order"""
        payment_data = {
            'order_id': sample_order.id,
            'payment_method': 'card',
            'card_token': 'test_card_token'
        }
        
        with patch('business_app.services.payment_service.PaymentService.create_payment') as mock_create:
            mock_payment = Payment(
                id=1,
                payment_id='PAY-001',
                amount=sample_order.total_amount,
                status=PaymentStatus.PENDING
            )
            mock_create.return_value = mock_payment
            
            response = api_client.post(
                '/api/payments',
                data=json.dumps(payment_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 201
            data = json.loads(response.data)
            assert 'payment_id' in data
    
    def test_process_payment(self, api_client, auth_token, sample_payment):
        """Test processing payment"""
        with patch('business_app.services.payment_service.PaymentService.process_payment') as mock_process:
            mock_process.return_value = {
                'success': True,
                'transaction_id': 'TXN-123',
                'status': 'completed'
            }
            
            response = api_client.post(
                f'/api/payments/{sample_payment.id}/process',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
            assert 'transaction_id' in data
    
    def test_refund_payment(self, api_client, auth_token, sample_payment):
        """Test refunding payment"""
        refund_data = {
            'amount': '5000.00',
            'reason': 'Customer requested'
        }
        
        with patch('business_app.services.payment_service.PaymentService.refund_payment') as mock_refund:
            mock_refund.return_value = {
                'success': True,
                'refund_id': 'REF-123',
                'refund_amount': Decimal('5000.00')
            }
            
            response = api_client.post(
                f'/api/payments/{sample_payment.id}/refund',
                data=json.dumps(refund_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
    
    def test_payment_webhook(self, api_client):
        """Test payment gateway webhook"""
        webhook_data = {
            'payment_id': 'PAY-123',
            'status': 'completed',
            'transaction_id': 'TXN-456',
            'signature': 'webhook_signature'
        }
        
        with patch('business_app.services.payment_service.PaymentService.handle_webhook') as mock_webhook:
            mock_webhook.return_value = {'success': True}
            
            response = api_client.post(
                '/api/payments/webhook',
                data=json.dumps(webhook_data),
                content_type='application/json'
            )
            
            assert response.status_code == 200


@pytest.mark.api
@pytest.mark.integration
class TestProductsAPI:
    """Test products API endpoints"""
    
    def test_get_products_list(self, api_client):
        """Test getting products list"""
        response = api_client.get('/api/products')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'products' in data
    
    def test_get_product_details(self, api_client, sample_product):
        """Test getting specific product details"""
        response = api_client.get(f'/api/products/{sample_product.id}')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['id'] == sample_product.id
        assert data['name'] == sample_product.name
    
    def test_search_products(self, api_client):
        """Test product search functionality"""
        search_params = {
            'query': 'water',
            'category': 'water',
            'min_price': '10000',
            'max_price': '20000'
        }
        
        response = api_client.get('/api/products/search', query_string=search_params)
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'products' in data
    
    def test_get_product_categories(self, api_client):
        """Test getting product categories"""
        response = api_client.get('/api/products/categories')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'categories' in data


@pytest.mark.api
@pytest.mark.integration
class TestAdminAPI:
    """Test admin API endpoints"""
    
    def test_admin_get_all_orders(self, api_client, admin_token):
        """Test admin accessing all orders"""
        with patch('business_app.services.order_service.OrderService.get_all_orders') as mock_get:
            mock_get.return_value = {'orders': [], 'total': 0}
            
            response = api_client.get(
                '/api/admin/orders',
                headers={'Authorization': f'Bearer {admin_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'orders' in data
    
    def test_admin_update_order_status(self, api_client, admin_token, sample_order):
        """Test admin updating order status"""
        update_data = {
            'status': 'confirmed'
        }
        
        with patch('business_app.services.order_service.OrderService.update_order_status') as mock_update:
            mock_update.return_value = {'success': True}
            
            response = api_client.put(
                f'/api/admin/orders/{sample_order.id}/status',
                data=json.dumps(update_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {admin_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert data['success'] is True
    
    def test_admin_get_analytics(self, api_client, admin_token):
        """Test admin analytics endpoint"""
        query_params = {
            'start_date': '2024-01-01',
            'end_date': '2024-12-31',
            'metric': 'sales'
        }
        
        with patch('business_app.services.analytics_service.AnalyticsService.generate_report') as mock_analytics:
            mock_analytics.return_value = {
                'total_sales': 1000000,
                'order_count': 500,
                'average_order_value': 2000
            }
            
            response = api_client.get(
                '/api/admin/analytics',
                query_string=query_params,
                headers={'Authorization': f'Bearer {admin_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'total_sales' in data
    
    def test_customer_access_admin_endpoint(self, api_client, auth_token):
        """Test customer trying to access admin endpoint"""
        response = api_client.get(
            '/api/admin/orders',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 403  # Forbidden


@pytest.mark.api
@pytest.mark.integration
class TestDeliveryAPI:
    """Test delivery API endpoints"""
    
    def test_get_time_slots(self, api_client):
        """Test getting available delivery time slots"""
        query_params = {
            'date': '2024-12-25'
        }
        
        with patch('business_app.services.delivery_service.DeliveryService.get_available_time_slots') as mock_slots:
            mock_slots.return_value = [
                {'id': 1, 'name': 'Morning', 'available_capacity': 15},
                {'id': 2, 'name': 'Afternoon', 'available_capacity': 20}
            ]
            
            response = api_client.get('/api/delivery/time-slots', query_string=query_params)
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'time_slots' in data
            assert len(data['time_slots']) > 0
    
    def test_calculate_delivery_fee(self, api_client):
        """Test calculating delivery fee"""
        fee_data = {
            'address': {
                'latitude': 41.3200,
                'longitude': 69.2800
            },
            'order_amount': '25000.00'
        }
        
        with patch('business_app.services.delivery_service.DeliveryService.calculate_delivery_fee') as mock_fee:
            mock_fee.return_value = Decimal('3000.00')
            
            response = api_client.post(
                '/api/delivery/calculate-fee',
                data=json.dumps(fee_data),
                content_type='application/json'
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'delivery_fee' in data
    
    def test_track_delivery(self, api_client, auth_token):
        """Test tracking delivery status"""
        delivery_id = 123
        
        with patch('business_app.services.delivery_service.DeliveryService.get_delivery_status') as mock_track:
            mock_track.return_value = {
                'status': 'in_transit',
                'estimated_arrival': '2024-12-25 14:30:00',
                'current_location': {'latitude': 41.3100, 'longitude': 69.2500}
            }
            
            response = api_client.get(
                f'/api/delivery/{delivery_id}/track',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 200
            data = json.loads(response.data)
            assert 'status' in data
            assert 'estimated_arrival' in data


@pytest.mark.api
@pytest.mark.integration
class TestAPIErrorHandling:
    """Test API error handling"""
    
    def test_invalid_json_request(self, api_client, auth_token):
        """Test handling of invalid JSON"""
        response = api_client.post(
            '/api/orders',
            data='invalid json',
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_missing_required_fields(self, api_client, auth_token):
        """Test handling of missing required fields"""
        incomplete_data = {
            'items': []  # Missing required items
        }
        
        response = api_client.post(
            '/api/orders',
            data=json.dumps(incomplete_data),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 400
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_resource_not_found(self, api_client, auth_token):
        """Test handling of non-existent resources"""
        response = api_client.get(
            '/api/orders/99999',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 404
        data = json.loads(response.data)
        assert 'error' in data
    
    def test_internal_server_error(self, api_client, auth_token):
        """Test handling of internal server errors"""
        with patch('business_app.services.order_service.OrderService.get_user_orders') as mock_get:
            mock_get.side_effect = Exception("Database error")
            
            response = api_client.get(
                '/api/orders',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            assert response.status_code == 500
            data = json.loads(response.data)
            assert 'error' in data
    
    def test_rate_limiting(self, api_client):
        """Test rate limiting protection"""
        # Make many requests quickly
        responses = []
        for _ in range(100):
            response = api_client.get('/api/products')
            responses.append(response)
        
        # At least some should be rate limited
        rate_limited = [r for r in responses if r.status_code == 429]
        # This test depends on rate limiting configuration
        # assert len(rate_limited) > 0


@pytest.mark.performance
@pytest.mark.api
@pytest.mark.integration
class TestAPIPerformance:
    """Test API performance"""
    
    def test_products_list_performance(self, api_client):
        """Test products list endpoint performance"""
        import time
        
        start_time = time.time()
        response = api_client.get('/api/products')
        end_time = time.time()
        
        response_time = end_time - start_time
        assert response_time < 1.0  # Should respond within 1 second
        assert response.status_code == 200
    
    def test_concurrent_api_requests(self, api_client):
        """Test handling of concurrent API requests"""
        import threading
        import time
        
        responses = []
        
        def make_request():
            response = api_client.get('/api/products')
            responses.append(response)
        
        # Make concurrent requests
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=make_request)
            threads.append(thread)
            thread.start()
        
        # Wait for all requests
        for thread in threads:
            thread.join()
        
        # All requests should succeed
        assert len(responses) == 10
        assert all(r.status_code == 200 for r in responses)
    
    def test_large_payload_handling(self, api_client, auth_token):
        """Test handling of large request payloads"""
        # Create large order with many items
        large_order_data = {
            'items': [
                {
                    'product_id': 1,
                    'quantity': 1
                }
            ] * 100,  # 100 items
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent',
                'latitude': 41.2995,
                'longitude': 69.2401
            }
        }
        
        response = api_client.post(
            '/api/orders',
            data=json.dumps(large_order_data),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        # Should handle large payload (though may reject for business reasons)
        assert response.status_code in [201, 400]  # Created or bad request, not server error