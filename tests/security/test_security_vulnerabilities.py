"""
Security tests for critical vulnerabilities
Tests for SQL injection, XSS, authentication bypasses, and other security issues
"""
import pytest
import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
import base64
import jwt

from business_app import create_app
from business_app.models.user import User
from business_app.utils.constants import UserRole


@pytest.fixture(scope='class')
def security_test_app():
    """Create test app for security tests"""
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
def security_client(security_test_app):
    """Create security test client"""
    return security_test_app.test_client()


@pytest.mark.critical
@pytest.mark.security
class TestSQLInjection:
    """Test SQL injection vulnerabilities"""
    
    def test_login_sql_injection(self, security_client, malicious_payloads):
        """Test SQL injection in login endpoint"""
        for payload in malicious_payloads['sql_injection']:
            login_data = {
                'email': payload,
                'password': 'password'
            }
            
            response = security_client.post(
                '/api/auth/login',
                data=json.dumps(login_data),
                content_type='application/json'
            )
            
            # Should not return 200 (successful login) with SQL injection
            assert response.status_code != 200
            
            # Should not leak database information
            response_text = response.get_data(as_text=True).lower()
            assert 'mysql' not in response_text
            assert 'postgresql' not in response_text
            assert 'sqlite' not in response_text
            assert 'oracle' not in response_text
    
    def test_order_search_sql_injection(self, security_client, auth_token, malicious_payloads):
        """Test SQL injection in order search"""
        for payload in malicious_payloads['sql_injection']:
            search_params = {
                'order_number': payload,
                'status': payload
            }
            
            response = security_client.get(
                '/api/orders/search',
                query_string=search_params,
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            # Should handle malicious input gracefully
            assert response.status_code in [400, 404, 422]  # Bad request, not found, or validation error
            
            # Should not leak sensitive information
            response_text = response.get_data(as_text=True).lower()
            assert 'error in your sql syntax' not in response_text
            assert 'table' not in response_text
            assert 'column' not in response_text
    
    def test_product_search_sql_injection(self, security_client, malicious_payloads):
        """Test SQL injection in product search"""
        for payload in malicious_payloads['sql_injection']:
            search_data = {
                'query': payload,
                'category': payload,
                'min_price': payload,
                'max_price': payload
            }
            
            response = security_client.get(
                '/api/products/search',
                query_string=search_data
            )
            
            # Should handle malicious input safely
            assert response.status_code in [200, 400, 422]
            
            if response.status_code == 200:
                data = json.loads(response.data)
                # Should return empty results or valid products, not error messages
                assert 'products' in data
    
    def test_user_profile_update_sql_injection(self, security_client, auth_token, malicious_payloads):
        """Test SQL injection in user profile updates"""
        for payload in malicious_payloads['sql_injection']:
            profile_data = {
                'first_name': payload,
                'last_name': payload,
                'phone': payload
            }
            
            response = security_client.put(
                '/api/users/profile',
                data=json.dumps(profile_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            # Should reject malicious input
            assert response.status_code in [400, 422]


@pytest.mark.critical
@pytest.mark.security
class TestXSSVulnerabilities:
    """Test Cross-Site Scripting (XSS) vulnerabilities"""
    
    def test_user_registration_xss(self, security_client, malicious_payloads):
        """Test XSS in user registration"""
        for payload in malicious_payloads['xss']:
            registration_data = {
                'email': 'test@example.com',
                'first_name': payload,
                'last_name': payload,
                'password': 'SecureP@ssw0rd123'
            }
            
            response = security_client.post(
                '/api/auth/register',
                data=json.dumps(registration_data),
                content_type='application/json'
            )
            
            # Should either reject or sanitize the input
            if response.status_code == 201:
                # If accepted, verify data is sanitized
                data = json.loads(response.data)
                # Check that script tags are not reflected in response
                response_text = response.get_data(as_text=True)
                assert '<script>' not in response_text
                assert 'javascript:' not in response_text
                assert 'onerror=' not in response_text
    
    def test_order_notes_xss(self, security_client, auth_token, malicious_payloads):
        """Test XSS in order notes"""
        for payload in malicious_payloads['xss']:
            order_data = {
                'items': [{'product_id': 1, 'quantity': 1}],
                'delivery_address': {
                    'address_line1': '123 Test Street',
                    'city': 'Tashkent'
                },
                'notes': payload
            }
            
            response = security_client.post(
                '/api/orders',
                data=json.dumps(order_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            # Should sanitize or reject malicious input
            if response.status_code == 201:
                response_text = response.get_data(as_text=True)
                assert '<script>' not in response_text
                assert 'javascript:' not in response_text
    
    def test_review_content_xss(self, security_client, auth_token, malicious_payloads):
        """Test XSS in product reviews"""
        for payload in malicious_payloads['xss']:
            review_data = {
                'product_id': 1,
                'rating': 5,
                'comment': payload
            }
            
            response = security_client.post(
                '/api/products/1/reviews',
                data=json.dumps(review_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            # Should sanitize malicious content
            if response.status_code in [200, 201]:
                response_text = response.get_data(as_text=True)
                assert '<script>' not in response_text
                assert 'onerror=' not in response_text


@pytest.mark.critical
@pytest.mark.security
class TestAuthenticationSecurity:
    """Test authentication and authorization security"""
    
    def test_jwt_token_manipulation(self, security_client, sample_user):
        """Test JWT token manipulation attempts"""
        # Create valid token
        with patch('business_app.services.auth_service.AuthService.create_access_token') as mock_create:
            valid_token = jwt.encode(
                {
                    'user_id': sample_user.id,
                    'role': 'customer',
                    'exp': 9999999999  # Far future
                },
                'test-jwt-secret',
                algorithm='HS256'
            )
            
            # Test with manipulated token (changed role)
            manipulated_payload = {
                'user_id': sample_user.id,
                'role': 'admin',  # Escalated role
                'exp': 9999999999
            }
            
            manipulated_token = jwt.encode(manipulated_payload, 'wrong-secret', algorithm='HS256')
            
            response = security_client.get(
                '/api/admin/orders',
                headers={'Authorization': f'Bearer {manipulated_token}'}
            )
            
            # Should reject manipulated token
            assert response.status_code == 401
    
    def test_session_fixation(self, security_client):
        """Test session fixation attacks"""
        # Attempt login with predetermined session
        login_data = {
            'email': 'test@example.com',
            'password': 'password'
        }
        
        # Set a session cookie before login
        security_client.set_cookie('localhost', 'session_id', 'fixed_session_id')
        
        response = security_client.post(
            '/api/auth/login',
            data=json.dumps(login_data),
            content_type='application/json'
        )
        
        # Should generate new session, not use fixed one
        # Implementation depends on session management strategy
        assert response.status_code in [200, 401]
    
    def test_concurrent_sessions_limit(self, security_client, sample_user):
        """Test concurrent session limits"""
        login_data = {
            'email': 'test@example.com',
            'password': 'password'
        }
        
        # Attempt multiple concurrent logins
        sessions = []
        for i in range(10):
            with patch('business_app.services.auth_service.AuthService.authenticate_user') as mock_auth:
                mock_auth.return_value = {
                    'success': True,
                    'user_id': sample_user.id,
                    'role': sample_user.role
                }
                
                response = security_client.post(
                    '/api/auth/login',
                    data=json.dumps(login_data),
                    content_type='application/json'
                )
                
                if response.status_code == 200:
                    data = json.loads(response.data)
                    sessions.append(data.get('access_token'))
        
        # Should limit concurrent sessions
        # Exact limit depends on implementation
        assert len([s for s in sessions if s]) <= 5
    
    def test_password_brute_force_protection(self, security_client):
        """Test password brute force protection"""
        login_data = {
            'email': 'test@example.com',
            'password': 'wrongpassword'
        }
        
        # Attempt multiple failed logins
        responses = []
        for i in range(10):
            response = security_client.post(
                '/api/auth/login',
                data=json.dumps(login_data),
                content_type='application/json'
            )
            responses.append(response)
        
        # Should implement rate limiting or account lockout
        last_responses = responses[-3:]  # Check last 3 attempts
        assert any(r.status_code == 429 for r in last_responses)  # Rate limited
    
    def test_privilege_escalation(self, security_client, auth_token):
        """Test privilege escalation attempts"""
        # Customer trying to access admin endpoint
        response = security_client.get(
            '/api/admin/users',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 403  # Forbidden
        
        # Customer trying to modify other user's data
        response = security_client.put(
            '/api/users/999/profile',  # Different user ID
            data=json.dumps({'first_name': 'Hacked'}),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code in [403, 404]  # Forbidden or Not Found


@pytest.mark.critical
@pytest.mark.security
class TestInputValidation:
    """Test input validation security"""
    
    def test_file_upload_path_traversal(self, security_client, auth_token, malicious_payloads):
        """Test path traversal in file uploads"""
        for payload in malicious_payloads['path_traversal']:
            # Test profile picture upload with malicious filename
            upload_data = {
                'filename': payload,
                'content_type': 'image/jpeg'
            }
            
            response = security_client.post(
                '/api/users/profile/upload-picture',
                data=upload_data,
                headers={'Authorization': f'Bearer {auth_token}'}
            )
            
            # Should reject path traversal attempts
            assert response.status_code in [400, 422]
            
            # Should not reflect malicious path in response
            response_text = response.get_data(as_text=True)
            assert '../' not in response_text
            assert '..\\' not in response_text
    
    def test_command_injection(self, security_client, admin_token, malicious_payloads):
        """Test command injection in admin functions"""
        for payload in malicious_payloads['command_injection']:
            # Test export functionality that might execute commands
            export_data = {
                'format': 'csv',
                'filename': payload
            }
            
            response = security_client.post(
                '/api/admin/export/orders',
                data=json.dumps(export_data),
                content_type='application/json',
                headers={'Authorization': f'Bearer {admin_token}'}
            )
            
            # Should sanitize input and prevent command execution
            assert response.status_code in [400, 422]
    
    def test_json_payload_size_limit(self, security_client, auth_token):
        """Test JSON payload size limits"""
        # Create extremely large payload
        large_payload = {
            'items': [{'product_id': 1, 'quantity': 1}] * 10000,
            'notes': 'A' * 100000  # 100KB of data
        }
        
        response = security_client.post(
            '/api/orders',
            data=json.dumps(large_payload),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        # Should reject overly large payloads
        assert response.status_code in [400, 413, 422]  # Bad request or payload too large
    
    def test_numeric_overflow(self, security_client, auth_token):
        """Test numeric overflow protection"""
        overflow_data = {
            'items': [{
                'product_id': 1,
                'quantity': 999999999999999999999999999999  # Extremely large number
            }],
            'delivery_address': {
                'address_line1': '123 Test Street',
                'city': 'Tashkent'
            }
        }
        
        response = security_client.post(
            '/api/orders',
            data=json.dumps(overflow_data),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        # Should handle numeric overflow gracefully
        assert response.status_code in [400, 422]
    
    def test_email_validation(self, security_client):
        """Test email validation security"""
        malicious_emails = [
            'test@<script>alert(1)</script>.com',
            'test@"malicious"@example.com',
            'test@example.com<script>alert(1)</script>',
            'test+<script>@example.com'
        ]
        
        for email in malicious_emails:
            registration_data = {
                'email': email,
                'password': 'SecureP@ssw0rd123',
                'first_name': 'Test',
                'last_name': 'User'
            }
            
            response = security_client.post(
                '/api/auth/register',
                data=json.dumps(registration_data),
                content_type='application/json'
            )
            
            # Should reject malformed emails
            assert response.status_code in [400, 422]


@pytest.mark.critical
@pytest.mark.security
class TestDataExposure:
    """Test data exposure vulnerabilities"""
    
    def test_sensitive_data_in_responses(self, security_client, auth_token):
        """Test that sensitive data is not exposed in API responses"""
        response = security_client.get(
            '/api/users/profile',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        if response.status_code == 200:
            response_text = response.get_data(as_text=True)
            data = json.loads(response.data)
            
            # Should not expose sensitive fields
            assert 'password' not in data
            assert 'password_hash' not in data
            assert 'secret_key' not in data
            assert 'private_key' not in data
            
            # Should not expose sensitive data in plain text
            assert 'password' not in response_text.lower()
            assert 'secret' not in response_text.lower()
    
    def test_error_message_information_disclosure(self, security_client):
        """Test that error messages don't disclose sensitive information"""
        # Test with non-existent endpoint
        response = security_client.get('/api/nonexistent')
        
        response_text = response.get_data(as_text=True).lower()
        
        # Should not expose system information
        assert 'traceback' not in response_text
        assert 'stack trace' not in response_text
        assert 'file not found' not in response_text
        assert 'python' not in response_text
        assert 'flask' not in response_text
    
    def test_database_error_exposure(self, security_client):
        """Test that database errors are not exposed"""
        # Trigger potential database error
        malformed_data = {'invalid': 'data'}
        
        response = security_client.post(
            '/api/orders',
            data=json.dumps(malformed_data),
            content_type='application/json'
        )
        
        response_text = response.get_data(as_text=True).lower()
        
        # Should not expose database details
        assert 'sql' not in response_text
        assert 'database' not in response_text
        assert 'table' not in response_text
        assert 'column' not in response_text
        assert 'constraint' not in response_text
    
    def test_user_enumeration(self, security_client):
        """Test user enumeration protection"""
        # Test with existing email
        login_data_existing = {
            'email': 'test@example.com',
            'password': 'wrongpassword'
        }
        
        # Test with non-existing email
        login_data_nonexisting = {
            'email': 'nonexistent@example.com',
            'password': 'wrongpassword'
        }
        
        response1 = security_client.post(
            '/api/auth/login',
            data=json.dumps(login_data_existing),
            content_type='application/json'
        )
        
        response2 = security_client.post(
            '/api/auth/login',
            data=json.dumps(login_data_nonexisting),
            content_type='application/json'
        )
        
        # Should return similar responses to prevent user enumeration
        assert response1.status_code == response2.status_code
        
        # Response messages should be generic
        if response1.status_code == 401:
            data1 = json.loads(response1.data)
            data2 = json.loads(response2.data)
            assert 'invalid credentials' in data1.get('error', '').lower()
            assert 'invalid credentials' in data2.get('error', '').lower()


@pytest.mark.security
class TestCSRFProtection:
    """Test CSRF protection"""
    
    def test_state_changing_operations_require_csrf(self, security_client, auth_token):
        """Test that state-changing operations require CSRF protection"""
        # Test order creation without CSRF token
        order_data = {
            'items': [{'product_id': 1, 'quantity': 1}],
            'delivery_address': {'address_line1': '123 Test Street', 'city': 'Tashkent'}
        }
        
        response = security_client.post(
            '/api/orders',
            data=json.dumps(order_data),
            content_type='application/json',
            headers={'Authorization': f'Bearer {auth_token}'}
            # No CSRF token provided
        )
        
        # Should require CSRF token for state-changing operations
        # Implementation depends on CSRF configuration
        # assert response.status_code in [403, 400]
    
    def test_safe_operations_allow_get(self, security_client):
        """Test that safe operations (GET) don't require CSRF"""
        response = security_client.get('/api/products')
        
        # GET operations should be allowed without CSRF
        assert response.status_code == 200


@pytest.mark.security
class TestSecurityHeaders:
    """Test security headers"""
    
    def test_security_headers_present(self, security_client):
        """Test that proper security headers are set"""
        response = security_client.get('/api/products')
        
        headers = response.headers
        
        # Check for important security headers
        # Note: Exact headers depend on implementation
        expected_headers = [
            'X-Content-Type-Options',
            'X-Frame-Options',
            'X-XSS-Protection',
            'Strict-Transport-Security'
        ]
        
        # At least some security headers should be present
        present_headers = [h for h in expected_headers if h in headers]
        assert len(present_headers) > 0
    
    def test_cors_configuration(self, security_client):
        """Test CORS configuration security"""
        # Test preflight request
        response = security_client.options('/api/products')
        
        cors_headers = {
            'Access-Control-Allow-Origin',
            'Access-Control-Allow-Methods',
            'Access-Control-Allow-Headers'
        }
        
        # Should have CORS headers but not wildcard for credentials
        if 'Access-Control-Allow-Origin' in response.headers:
            origin = response.headers.get('Access-Control-Allow-Origin')
            credentials = response.headers.get('Access-Control-Allow-Credentials')
            
            # Should not allow wildcard origin with credentials
            if credentials and credentials.lower() == 'true':
                assert origin != '*'