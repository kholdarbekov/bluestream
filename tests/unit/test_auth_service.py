"""
Unit tests for Authentication Service - Critical Business Logic
Tests user authentication, authorization, JWT handling, and security features
"""
import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, UTC, timedelta
import jwt

from business_app.services.auth_service import AuthService
from business_app.models.user import User
from business_app.utils.constants import UserRole
from business_app.utils.exceptions import AuthenticationError, ValidationError, SecurityError


@pytest.fixture
def auth_service(mock_redis):
    """Create AuthService instance with mocked dependencies"""
    service = AuthService()
    service.redis_client = mock_redis
    return service


@pytest.mark.critical
@pytest.mark.auth
class TestUserAuthentication:
    """Test user authentication logic"""
    
    def test_authenticate_user_valid_credentials(self, auth_service, sample_user, db):
        """Test authentication with valid credentials"""
        password = 'ValidPassword123!'
        
        # Mock password verification
        with patch.object(auth_service, '_verify_password', return_value=True):
            result = auth_service.authenticate_user('test@example.com', password)
            
            assert result['success'] is True
            assert result['user_id'] == sample_user.id
            assert result['role'] == sample_user.role
    
    def test_authenticate_user_invalid_email(self, auth_service):
        """Test authentication with non-existent email"""
        result = auth_service.authenticate_user('nonexistent@example.com', 'password')
        
        assert result['success'] is False
        assert 'Invalid credentials' in result['error']
    
    def test_authenticate_user_invalid_password(self, auth_service, sample_user):
        """Test authentication with invalid password"""
        with patch.object(auth_service, '_verify_password', return_value=False):
            result = auth_service.authenticate_user('test@example.com', 'wrongpassword')
            
            assert result['success'] is False
            assert 'Invalid credentials' in result['error']
    
    def test_authenticate_user_account_locked(self, auth_service, sample_user, db):
        """Test authentication with locked account"""
        # Lock the user account
        sample_user.is_locked = True
        sample_user.locked_until = datetime.now(UTC) + timedelta(hours=1)
        db.session.commit()
        
        result = auth_service.authenticate_user('test@example.com', 'password')
        
        assert result['success'] is False
        assert 'Account is locked' in result['error']
    
    def test_authenticate_user_unverified_account(self, auth_service, sample_user, db):
        """Test authentication with unverified account"""
        sample_user.is_verified = False
        db.session.commit()
        
        result = auth_service.authenticate_user('test@example.com', 'password')
        
        assert result['success'] is False
        assert 'Account not verified' in result['error']
    
    def test_failed_login_attempts_tracking(self, auth_service, sample_user, mock_redis):
        """Test failed login attempts tracking"""
        email = 'test@example.com'
        
        # Simulate failed attempts
        for i in range(3):
            auth_service._record_failed_attempt(email)
        
        # Check if account gets locked
        mock_redis.incr.assert_called()
        mock_redis.expire.assert_called()
    
    def test_account_lockout_after_max_attempts(self, auth_service, sample_user, db, mock_redis):
        """Test account lockout after maximum failed attempts"""
        # Mock Redis to return max failed attempts
        mock_redis.get.return_value = b'5'  # 5 failed attempts
        
        with patch.object(auth_service, '_verify_password', return_value=False):
            result = auth_service.authenticate_user('test@example.com', 'password')
            
            assert result['success'] is False
            
            # Verify user is locked
            db.session.refresh(sample_user)
            assert sample_user.is_locked is True
            assert sample_user.locked_until > datetime.now(UTC)


@pytest.mark.critical
@pytest.mark.auth
class TestPasswordSecurity:
    """Test password security and validation"""
    
    def test_password_strength_validation(self, auth_service):
        """Test password strength requirements"""
        # Valid strong password
        strong_password = 'StrongP@ssw0rd123!'
        assert auth_service._validate_password_strength(strong_password) is True
        
        # Too short
        with pytest.raises(ValidationError, match="Password must be at least"):
            auth_service._validate_password_strength('weak')
        
        # Missing uppercase
        with pytest.raises(ValidationError, match="Password must contain"):
            auth_service._validate_password_strength('lowercase123!')
        
        # Missing special character
        with pytest.raises(ValidationError, match="Password must contain"):
            auth_service._validate_password_strength('NoSpecial123')
        
        # Common weak password
        with pytest.raises(ValidationError, match="Password is too common"):
            auth_service._validate_password_strength('password123')
    
    def test_password_hashing(self, auth_service):
        """Test secure password hashing"""
        password = 'TestPassword123!'
        
        hash1 = auth_service._hash_password(password)
        hash2 = auth_service._hash_password(password)
        
        # Hashes should be different (due to salt)
        assert hash1 != hash2
        assert hash1.startswith('$2b$')
        
        # Both should verify correctly
        assert auth_service._verify_password(password, hash1) is True
        assert auth_service._verify_password(password, hash2) is True
    
    def test_password_history_prevention(self, auth_service, sample_user, db):
        """Test prevention of password reuse"""
        old_passwords = [
            '$2b$12$old.hash.1',
            '$2b$12$old.hash.2',
            '$2b$12$old.hash.3'
        ]
        
        # Mock user's password history
        sample_user.password_history = old_passwords
        db.session.commit()
        
        new_password = 'NewPassword123!'
        
        with patch.object(auth_service, '_verify_password') as mock_verify:
            # Mock that new password matches one of the old ones
            mock_verify.side_effect = [False, True, False]  # Matches second old password
            
            with pytest.raises(ValidationError, match="Cannot reuse recent passwords"):
                auth_service._validate_password_history(sample_user.id, new_password)


@pytest.mark.critical
@pytest.mark.auth
class TestJWTTokenManagement:
    """Test JWT token creation and validation"""
    
    def test_create_access_token(self, auth_service, sample_user):
        """Test access token creation"""
        token = auth_service.create_access_token(sample_user)
        
        # Decode token to verify contents
        decoded = jwt.decode(token, options={"verify_signature": False})
        
        assert decoded['user_id'] == sample_user.id
        assert decoded['role'] == sample_user.role
        assert decoded['type'] == 'access'
        assert 'exp' in decoded
    
    def test_create_refresh_token(self, auth_service, sample_user):
        """Test refresh token creation"""
        token = auth_service.create_refresh_token(sample_user)
        
        decoded = jwt.decode(token, options={"verify_signature": False})
        
        assert decoded['user_id'] == sample_user.id
        assert decoded['type'] == 'refresh'
        assert 'exp' in decoded
    
    def test_validate_token_valid(self, auth_service, sample_user):
        """Test validation of valid token"""
        token = auth_service.create_access_token(sample_user)
        
        result = auth_service.validate_token(token)
        
        assert result['valid'] is True
        assert result['user_id'] == sample_user.id
        assert result['role'] == sample_user.role
    
    def test_validate_token_expired(self, auth_service, sample_user):
        """Test validation of expired token"""
        # Create token with past expiration
        with patch('business_app.services.auth_service.datetime') as mock_datetime:
            past_time = datetime.now(UTC) - timedelta(hours=2)
            mock_datetime.now.return_value = past_time
            token = auth_service.create_access_token(sample_user)
        
        result = auth_service.validate_token(token)
        
        assert result['valid'] is False
        assert 'expired' in result['error'].lower()
    
    def test_validate_token_invalid_signature(self, auth_service):
        """Test validation of token with invalid signature"""
        invalid_token = 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.invalid.signature'
        
        result = auth_service.validate_token(invalid_token)
        
        assert result['valid'] is False
        assert 'invalid' in result['error'].lower()
    
    def test_token_blacklist(self, auth_service, sample_user, mock_redis):
        """Test token blacklisting for logout"""
        token = auth_service.create_access_token(sample_user)
        
        # Blacklist token
        auth_service.blacklist_token(token)
        
        # Verify token is blacklisted
        mock_redis.setex.assert_called()
        
        # Check blacklist validation
        mock_redis.get.return_value = b'blacklisted'
        result = auth_service.validate_token(token)
        
        assert result['valid'] is False
        assert 'blacklisted' in result['error'].lower()
    
    def test_refresh_token_rotation(self, auth_service, sample_user, mock_redis):
        """Test refresh token rotation for security"""
        refresh_token = auth_service.create_refresh_token(sample_user)
        
        # Use refresh token to get new tokens
        result = auth_service.refresh_tokens(refresh_token)
        
        assert result['success'] is True
        assert 'access_token' in result
        assert 'refresh_token' in result
        
        # Old refresh token should be blacklisted
        mock_redis.setex.assert_called()


@pytest.mark.critical
@pytest.mark.auth
class TestRoleBasedAccess:
    """Test role-based access control"""
    
    def test_check_permission_admin(self, auth_service, admin_user):
        """Test admin permissions"""
        # Admin should have access to all resources
        assert auth_service.check_permission(admin_user.id, 'users', 'read') is True
        assert auth_service.check_permission(admin_user.id, 'orders', 'write') is True
        assert auth_service.check_permission(admin_user.id, 'payments', 'admin') is True
    
    def test_check_permission_customer(self, auth_service, sample_user):
        """Test customer permissions"""
        # Customer should have limited permissions
        assert auth_service.check_permission(sample_user.id, 'orders', 'read') is True
        assert auth_service.check_permission(sample_user.id, 'orders', 'write') is True
        assert auth_service.check_permission(sample_user.id, 'users', 'admin') is False
        assert auth_service.check_permission(sample_user.id, 'payments', 'admin') is False
    
    def test_check_permission_delivery_driver(self, auth_service, delivery_driver):
        """Test delivery driver permissions"""
        # Delivery driver should have specific permissions
        assert auth_service.check_permission(delivery_driver.id, 'deliveries', 'read') is True
        assert auth_service.check_permission(delivery_driver.id, 'deliveries', 'write') is True
        assert auth_service.check_permission(delivery_driver.id, 'orders', 'read') is True
        assert auth_service.check_permission(delivery_driver.id, 'users', 'admin') is False
    
    def test_resource_ownership_validation(self, auth_service, sample_user):
        """Test that users can only access their own resources"""
        # User should access their own orders
        assert auth_service.check_resource_ownership(sample_user.id, 'order', sample_user.id) is True
        
        # User should not access other user's orders
        assert auth_service.check_resource_ownership(sample_user.id, 'order', 999) is False


@pytest.mark.critical
@pytest.mark.auth
class TestSessionManagement:
    """Test session management and security"""
    
    def test_create_user_session(self, auth_service, sample_user, mock_redis):
        """Test user session creation"""
        session_id = auth_service.create_user_session(sample_user.id, {
            'ip_address': '192.168.1.1',
            'user_agent': 'Test Browser',
            'device_id': 'test_device'
        })
        
        assert session_id is not None
        mock_redis.setex.assert_called()
    
    def test_validate_session(self, auth_service, sample_user, mock_redis):
        """Test session validation"""
        session_data = {
            'user_id': sample_user.id,
            'ip_address': '192.168.1.1',
            'created_at': datetime.now(UTC).isoformat()
        }
        
        mock_redis.get.return_value = str(session_data).encode()
        
        result = auth_service.validate_session('test_session_id')
        
        assert result['valid'] is True
        mock_redis.get.assert_called_with('session:test_session_id')
    
    def test_concurrent_session_limit(self, auth_service, sample_user, mock_redis):
        """Test concurrent session limits"""
        # Mock existing sessions
        mock_redis.keys.return_value = [
            b'session:user:1:session1',
            b'session:user:1:session2',
            b'session:user:1:session3'
        ]
        
        # Try to create 4th session (exceeding limit of 3)
        with patch.object(auth_service, 'MAX_CONCURRENT_SESSIONS', 3):
            session_id = auth_service.create_user_session(sample_user.id, {})
            
            # Should remove oldest session
            mock_redis.delete.assert_called()
    
    def test_suspicious_activity_detection(self, auth_service, sample_user):
        """Test detection of suspicious login activity"""
        # Different IP addresses in short time
        activities = [
            {'ip_address': '192.168.1.1', 'timestamp': datetime.now(UTC)},
            {'ip_address': '10.0.0.1', 'timestamp': datetime.now(UTC) + timedelta(minutes=1)},
            {'ip_address': '172.16.0.1', 'timestamp': datetime.now(UTC) + timedelta(minutes=2)}
        ]
        
        is_suspicious = auth_service._detect_suspicious_activity(sample_user.id, activities)
        
        assert is_suspicious is True


@pytest.mark.critical
@pytest.mark.auth
class TestTwoFactorAuthentication:
    """Test two-factor authentication"""
    
    def test_generate_2fa_code(self, auth_service, sample_user, mock_redis):
        """Test 2FA code generation"""
        code = auth_service.generate_2fa_code(sample_user.id)
        
        assert len(code) == 6
        assert code.isdigit()
        mock_redis.setex.assert_called()
    
    def test_verify_2fa_code_valid(self, auth_service, sample_user, mock_redis):
        """Test valid 2FA code verification"""
        code = '123456'
        mock_redis.get.return_value = code.encode()
        
        result = auth_service.verify_2fa_code(sample_user.id, code)
        
        assert result['valid'] is True
        mock_redis.delete.assert_called()  # Code should be consumed
    
    def test_verify_2fa_code_invalid(self, auth_service, sample_user, mock_redis):
        """Test invalid 2FA code verification"""
        mock_redis.get.return_value = b'123456'
        
        result = auth_service.verify_2fa_code(sample_user.id, '654321')
        
        assert result['valid'] is False
        assert 'Invalid code' in result['error']
    
    def test_verify_2fa_code_expired(self, auth_service, sample_user, mock_redis):
        """Test expired 2FA code verification"""
        mock_redis.get.return_value = None  # Code expired/not found
        
        result = auth_service.verify_2fa_code(sample_user.id, '123456')
        
        assert result['valid'] is False
        assert 'expired' in result['error'].lower()
    
    def test_2fa_rate_limiting(self, auth_service, sample_user, mock_redis):
        """Test 2FA attempt rate limiting"""
        # Mock multiple failed attempts
        mock_redis.get.side_effect = [b'3', b'123456']  # 3 failed attempts, then valid code
        
        result = auth_service.verify_2fa_code(sample_user.id, '123456')
        
        assert result['valid'] is False
        assert 'too many attempts' in result['error'].lower()


@pytest.mark.auth
class TestPasswordRecovery:
    """Test password recovery functionality"""
    
    def test_generate_password_reset_token(self, auth_service, sample_user, mock_redis):
        """Test password reset token generation"""
        token = auth_service.generate_password_reset_token(sample_user.email)
        
        assert token is not None
        assert len(token) >= 32
        mock_redis.setex.assert_called()
    
    def test_validate_reset_token_valid(self, auth_service, sample_user, mock_redis):
        """Test valid password reset token"""
        token = 'valid_reset_token'
        mock_redis.get.return_value = sample_user.email.encode()
        
        result = auth_service.validate_password_reset_token(token)
        
        assert result['valid'] is True
        assert result['email'] == sample_user.email
    
    def test_validate_reset_token_expired(self, auth_service, mock_redis):
        """Test expired password reset token"""
        mock_redis.get.return_value = None
        
        result = auth_service.validate_password_reset_token('expired_token')
        
        assert result['valid'] is False
        assert 'expired' in result['error'].lower()
    
    def test_reset_password_with_valid_token(self, auth_service, sample_user, db, mock_redis):
        """Test password reset with valid token"""
        token = 'valid_token'
        new_password = 'NewSecureP@ssw0rd123!'
        
        mock_redis.get.return_value = sample_user.email.encode()
        
        with patch.object(auth_service, '_hash_password', return_value='new_hash'):
            result = auth_service.reset_password(token, new_password)
            
            assert result['success'] is True
            mock_redis.delete.assert_called()  # Token should be consumed


@pytest.mark.performance
@pytest.mark.auth
class TestAuthPerformance:
    """Test authentication performance"""
    
    def test_authentication_performance(self, auth_service, sample_user):
        """Test that authentication completes within acceptable time"""
        import time
        
        with patch.object(auth_service, '_verify_password', return_value=True):
            start_time = time.time()
            auth_service.authenticate_user('test@example.com', 'password')
            end_time = time.time()
            
            auth_time = end_time - start_time
            assert auth_time < 0.5  # Should complete within 500ms
    
    def test_token_validation_performance(self, auth_service, sample_user):
        """Test token validation performance"""
        import time
        
        token = auth_service.create_access_token(sample_user)
        
        start_time = time.time()
        auth_service.validate_token(token)
        end_time = time.time()
        
        validation_time = end_time - start_time
        assert validation_time < 0.1  # Should validate within 100ms