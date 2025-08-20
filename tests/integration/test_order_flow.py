"""
Integration tests for complete order flow
Tests the full customer journey from product selection to order completion
"""
import pytest
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from datetime import datetime, UTC

from business_app.models.user import User
from business_app.models.product import Product
from business_app.models.order import Order
from business_app.utils.constants import UserRole, OrderStatus, PaymentStatus


@pytest.mark.integration
@pytest.mark.critical
@pytest.mark.order
class TestCompleteOrderFlow:
    """Test complete order flow from start to finish"""
    
    def test_complete_order_flow_success(self, client, db, sample_user, sample_product):
        """Test successful complete order flow"""
        
        # Step 1: User authentication
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            assert login_response.status_code == 200
            auth_data = login_response.get_json()
            assert auth_data['success'] is True
            
            # Get auth token for subsequent requests
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Step 2: Browse products
        products_response = client.get('/api/v1/products', headers=auth_headers)
        assert products_response.status_code == 200
        products_data = products_response.get_json()
        assert 'products' in products_data
        
        # Step 3: Get product details
        product_response = client.get(f'/api/v1/products/{sample_product.id}', headers=auth_headers)
        assert product_response.status_code == 200
        product_data = product_response.get_json()
        assert product_data['id'] == sample_product.id
        
        # Step 4: Create order
        with patch('business_app.services.inventory_service.InventoryService.check_availability') as mock_inventory:
            with patch('business_app.services.delivery_service.DeliveryService.calculate_delivery_fee') as mock_delivery:
                mock_inventory.return_value = True
                mock_delivery.return_value = Decimal('3000.00')
                
                order_data = {
                    'items': [
                        {
                            'product_id': sample_product.id,
                            'quantity': 2,
                            'unit_price': str(sample_product.base_price)
                        }
                    ],
                    'delivery_address': {
                        'address_line1': '123 Test Street',
                        'city': 'Tashkent',
                        'latitude': 41.2995,
                        'longitude': 69.2401
                    },
                    'delivery_time_slot_id': 1,
                    'notes': 'Test order from integration test',
                    'payment_method': 'card'
                }
                
                order_response = client.post('/api/v1/orders', 
                                           json=order_data, 
                                           headers=auth_headers)
                
                assert order_response.status_code == 201
                order_result = order_response.get_json()
                assert 'order_id' in order_result
                
                order_id = order_result['order_id']
        
        # Step 5: Process payment
        with patch('business_app.services.payment_service.PaymentService.process_payment') as mock_payment:
            mock_payment.return_value = {
                'success': True,
                'payment_id': 'test_payment_123',
                'transaction_id': 'test_tx_456',
                'status': 'completed'
            }
            
            payment_data = {
                'amount': '33000.00',  # 2 * 15000 + 3000 delivery
                'currency': 'UZS',
                'payment_method': 'card',
                'card_token': 'test_card_token_123'
            }
            
            payment_response = client.post(f'/api/v1/orders/{order_id}/payment',
                                         json=payment_data,
                                         headers=auth_headers)
            
            assert payment_response.status_code == 200
            payment_result = payment_response.get_json()
            assert payment_result['success'] is True
        
        # Step 6: Verify order status
        order_status_response = client.get(f'/api/v1/orders/{order_id}', headers=auth_headers)
        assert order_status_response.status_code == 200
        
        final_order = order_status_response.get_json()
        assert final_order['id'] == order_id
        assert final_order['status'] in ['confirmed', 'processing']
        
        # Step 7: Verify order in database
        order = Order.query.get(order_id)
        assert order is not None
        assert order.user_id == sample_user.id
        assert order.status in [OrderStatus.CONFIRMED, OrderStatus.PROCESSING]
    
    def test_order_flow_insufficient_stock(self, client, db, sample_user, sample_product):
        """Test order flow when product is out of stock"""
        
        # Authenticate user
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            auth_data = login_response.get_json()
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Try to create order with insufficient stock
        with patch('business_app.services.inventory_service.InventoryService.check_availability') as mock_inventory:
            mock_inventory.return_value = False  # No stock available
            
            order_data = {
                'items': [
                    {
                        'product_id': sample_product.id,
                        'quantity': 1000,  # Requesting more than available
                        'unit_price': str(sample_product.base_price)
                    }
                ],
                'delivery_address': {
                    'address_line1': '123 Test Street',
                    'city': 'Tashkent',
                    'latitude': 41.2995,
                    'longitude': 69.2401
                }
            }
            
            order_response = client.post('/api/v1/orders',
                                       json=order_data,
                                       headers=auth_headers)
            
            assert order_response.status_code == 400
            error_data = order_response.get_json()
            assert 'insufficient stock' in error_data['error'].lower() or 'not available' in error_data['error'].lower()
    
    def test_order_flow_payment_failure(self, client, db, sample_user, sample_product):
        """Test order flow when payment fails"""
        
        # Authenticate user
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            auth_data = login_response.get_json()
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Create order successfully
        with patch('business_app.services.inventory_service.InventoryService.check_availability') as mock_inventory:
            with patch('business_app.services.delivery_service.DeliveryService.calculate_delivery_fee') as mock_delivery:
                mock_inventory.return_value = True
                mock_delivery.return_value = Decimal('3000.00')
                
                order_data = {
                    'items': [
                        {
                            'product_id': sample_product.id,
                            'quantity': 1,
                            'unit_price': str(sample_product.base_price)
                        }
                    ],
                    'delivery_address': {
                        'address_line1': '123 Test Street',
                        'city': 'Tashkent',
                        'latitude': 41.2995,
                        'longitude': 69.2401
                    }
                }
                
                order_response = client.post('/api/v1/orders',
                                           json=order_data,
                                           headers=auth_headers)
                
                assert order_response.status_code == 201
                order_result = order_response.get_json()
                order_id = order_result['order_id']
        
        # Attempt payment with failure
        with patch('business_app.services.payment_service.PaymentService.process_payment') as mock_payment:
            mock_payment.return_value = {
                'success': False,
                'error': 'Payment declined by bank',
                'error_code': 'PAYMENT_DECLINED'
            }
            
            payment_data = {
                'amount': '18000.00',
                'currency': 'UZS',
                'payment_method': 'card',
                'card_token': 'invalid_card_token'
            }
            
            payment_response = client.post(f'/api/v1/orders/{order_id}/payment',
                                         json=payment_data,
                                         headers=auth_headers)
            
            assert payment_response.status_code == 400
            payment_result = payment_response.get_json()
            assert payment_result['success'] is False
            assert 'declined' in payment_result['error'].lower()
        
        # Verify order status is still pending
        order_status_response = client.get(f'/api/v1/orders/{order_id}', headers=auth_headers)
        final_order = order_status_response.get_json()
        assert final_order['status'] == 'pending'
    
    def test_order_flow_invalid_delivery_address(self, client, db, sample_user, sample_product):
        """Test order flow with invalid delivery address"""
        
        # Authenticate user
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            auth_data = login_response.get_json()
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Mock delivery service to reject address
        with patch('business_app.services.delivery_service.DeliveryService.validate_delivery_address') as mock_validate:
            mock_validate.return_value = {
                'valid': False,
                'error': 'Address is outside delivery zone'
            }
            
            order_data = {
                'items': [
                    {
                        'product_id': sample_product.id,
                        'quantity': 1,
                        'unit_price': str(sample_product.base_price)
                    }
                ],
                'delivery_address': {
                    'address_line1': 'Mars Colony Alpha',
                    'city': 'Mars',
                    'latitude': 0.0,
                    'longitude': 0.0
                }
            }
            
            order_response = client.post('/api/v1/orders',
                                       json=order_data,
                                       headers=auth_headers)
            
            assert order_response.status_code == 400
            error_data = order_response.get_json()
            assert 'delivery' in error_data['error'].lower() or 'address' in error_data['error'].lower()


@pytest.mark.integration
@pytest.mark.api
class TestOrderAPIIntegration:
    """Test order API integration with other services"""
    
    def test_order_list_with_filters(self, client, db, sample_user, sample_order):
        """Test order listing with various filters"""
        
        # Create additional test orders
        orders = []
        for i in range(5):
            order = Order(
                user_id=sample_user.id,
                order_number=f'ORD-TEST-{i:03d}',
                status=OrderStatus.CONFIRMED if i % 2 == 0 else OrderStatus.PENDING,
                subtotal=Decimal('15000.00'),
                total_amount=Decimal('18000.00'),
                created_at=datetime.now(UTC)
            )
            db.session.add(order)
            orders.append(order)
        
        db.session.commit()
        
        # Mock authentication
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            auth_data = login_response.get_json()
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Test: Get all orders
        response = client.get('/api/v1/orders', headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['orders']) >= 5
        
        # Test: Filter by status
        response = client.get('/api/v1/orders?status=confirmed', headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        for order in data['orders']:
            assert order['status'] == 'confirmed'
        
        # Test: Pagination
        response = client.get('/api/v1/orders?page=1&per_page=2', headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert len(data['orders']) <= 2
        assert 'pagination' in data
    
    def test_order_cancellation_flow(self, client, db, sample_user, sample_order):
        """Test order cancellation flow"""
        
        # Mock authentication
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            auth_data = login_response.get_json()
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Test cancellation
        with patch('business_app.services.payment_service.PaymentService.process_refund') as mock_refund:
            mock_refund.return_value = {
                'success': True,
                'refund_id': 'test_refund_123',
                'status': 'refunded'
            }
            
            cancel_data = {
                'reason': 'Customer requested cancellation',
                'refund_requested': True
            }
            
            response = client.post(f'/api/v1/orders/{sample_order.id}/cancel',
                                 json=cancel_data,
                                 headers=auth_headers)
            
            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] is True
            
            # Verify order status updated
            updated_order = Order.query.get(sample_order.id)
            assert updated_order.status == OrderStatus.CANCELLED


@pytest.mark.integration
@pytest.mark.critical
class TestOrderSecurityIntegration:
    """Test order security and authorization"""
    
    def test_order_access_control(self, client, db, sample_user, admin_user, sample_order):
        """Test that users can only access their own orders"""
        
        # Create another user's order
        other_user = User(
            email='other@test.com',
            phone='+998901234999',
            password_hash='$2b$12$test.hash.for.testing.purposes.only',
            first_name='Other',
            last_name='User',
            role=UserRole.CUSTOMER,
            is_verified=True
        )
        db.session.add(other_user)
        db.session.commit()
        
        other_order = Order(
            user_id=other_user.id,
            order_number='ORD-OTHER-001',
            status=OrderStatus.PENDING,
            subtotal=Decimal('20000.00'),
            total_amount=Decimal('23000.00')
        )
        db.session.add(other_order)
        db.session.commit()
        
        # Authenticate as sample_user
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
            mock_auth.return_value = {
                'success': True,
                'user_id': sample_user.id,
                'role': sample_user.role
            }
            
            login_response = client.post('/api/v1/auth/login', json={
                'email': sample_user.email,
                'password': 'testpassword'
            })
            
            auth_data = login_response.get_json()
            auth_headers = {'Authorization': f'Bearer {auth_data.get("access_token", "test-token")}'}
        
        # Test: User can access their own order
        response = client.get(f'/api/v1/orders/{sample_order.id}', headers=auth_headers)
        assert response.status_code == 200
        
        # Test: User cannot access other user's order
        response = client.get(f'/api/v1/orders/{other_order.id}', headers=auth_headers)
        assert response.status_code in [403, 404]  # Forbidden or Not Found
        
        # Test: Admin can access any order
        with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_admin_auth:
            mock_admin_auth.return_value = {
                'success': True,
                'user_id': admin_user.id,
                'role': admin_user.role
            }
            
            admin_login_response = client.post('/api/v1/auth/login', json={
                'email': admin_user.email,
                'password': 'adminpassword'
            })
            
            admin_auth_data = admin_login_response.get_json()
            admin_auth_headers = {'Authorization': f'Bearer {admin_auth_data.get("access_token", "admin-token")}'}
        
        # Admin should be able to access any order
        response = client.get(f'/api/v1/orders/{other_order.id}', headers=admin_auth_headers)
        assert response.status_code == 200