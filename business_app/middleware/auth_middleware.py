"""
Authentication and Authorization Middleware for Blue Stream Water Business Platform
"""
from functools import wraps
from datetime import datetime, timezone
from flask import request, jsonify, current_app
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt
import logging

from business_app.models.user import User
from business_app.utils.constants import UserRole, UserStatus
from business_app.services.auth_service import AuthService

logger = logging.getLogger(__name__)


def jwt_required_with_refresh():
    """
    Custom JWT required decorator that handles token refresh
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                return f(*args, **kwargs)
            except Exception as e:
                logger.warning(f"JWT verification failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Authentication required',
                    'error_code': 'AUTH_REQUIRED'
                }), 401
        return decorated_function
    return decorator


def require_role(required_roles):
    """
    Decorator to require specific user roles
    
    Args:
        required_roles: Single role string or list of role strings
    
    Usage:
        @require_role('admin')
        @require_role(['admin', 'manager'])
    """
    if isinstance(required_roles, str):
        required_roles = [required_roles]
    
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                
                if not user_id:
                    return jsonify({
                        'success': False,
                        'message': 'Authentication required',
                        'error_code': 'AUTH_REQUIRED'
                    }), 401
                
                user = User.query.get(user_id)
                if not user:
                    return jsonify({
                        'success': False,
                        'message': 'User not found',
                        'error_code': 'USER_NOT_FOUND'
                    }), 404
                
                # Check if user is active
                if user.status != UserStatus.ACTIVE.value:
                    return jsonify({
                        'success': False,
                        'message': 'Account is not active',
                        'error_code': 'ACCOUNT_INACTIVE'
                    }), 403
                
                # Check if user has required role
                if user.role not in required_roles:
                    return jsonify({
                        'success': False,
                        'message': 'Insufficient permissions',
                        'error_code': 'INSUFFICIENT_PERMISSIONS'
                    }), 403
                
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"Role verification failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Authorization failed',
                    'error_code': 'AUTH_FAILED'
                }), 401
                
        return decorated_function
    return decorator


def require_permission(permission_name):
    """
    Decorator to require specific permission
    
    Args:
        permission_name: Permission string (e.g., 'can_manage_users')
    
    Usage:
        @require_permission('can_manage_users')
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                
                if not user_id:
                    return jsonify({
                        'success': False,
                        'message': 'Authentication required',
                        'error_code': 'AUTH_REQUIRED'
                    }), 401
                
                # Get user permissions
                auth_service = AuthService()
                permissions = auth_service.get_user_permissions(user_id)
                
                if not permissions.get(permission_name, False):
                    return jsonify({
                        'success': False,
                        'message': f'Permission denied: {permission_name}',
                        'error_code': 'PERMISSION_DENIED'
                    }), 403
                
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"Permission verification failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Authorization failed',
                    'error_code': 'AUTH_FAILED'
                }), 401
                
        return decorated_function
    return decorator


def admin_required(f):
    """
    Decorator to require admin role
    """
    return require_role([UserRole.ADMIN.value])(f)


def staff_required(f):
    """
    Decorator to require staff roles (admin, manager, operator)
    """
    return require_role([
        UserRole.ADMIN.value,
        UserRole.MANAGER.value,
        UserRole.OPERATOR.value
    ])(f)


def manager_or_admin_required(f):
    """
    Decorator to require manager or admin role
    """
    return require_role([
        UserRole.ADMIN.value,
        UserRole.MANAGER.value
    ])(f)


def customer_or_staff_required(f):
    """
    Decorator to require customer or staff access
    """
    return require_role([
        UserRole.CUSTOMER.value,
        UserRole.ADMIN.value,
        UserRole.MANAGER.value,
        UserRole.OPERATOR.value
    ])(f)


def delivery_driver_required(f):
    """
    Decorator to require delivery driver role
    """
    return require_role([UserRole.DELIVERY_DRIVER.value])(f)


def verify_user_ownership(user_id_param='user_id'):
    """
    Decorator to verify that the current user owns the resource
    or has admin/manager privileges
    
    Args:
        user_id_param: Parameter name containing the user ID to check
    
    Usage:
        @verify_user_ownership('user_id')
        def get_user_orders(user_id):
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                current_user_id = get_jwt_identity()
                
                if not current_user_id:
                    return jsonify({
                        'success': False,
                        'message': 'Authentication required',
                        'error_code': 'AUTH_REQUIRED'
                    }), 401
                
                # Get target user ID from parameters
                target_user_id = kwargs.get(user_id_param)
                if target_user_id is None:
                    # Try to get from URL path
                    target_user_id = request.view_args.get(user_id_param)
                
                if target_user_id is None:
                    return jsonify({
                        'success': False,
                        'message': 'User ID not found in request',
                        'error_code': 'INVALID_REQUEST'
                    }), 400
                
                # Convert to int if string
                if isinstance(target_user_id, str):
                    target_user_id = int(target_user_id)
                if isinstance(current_user_id, str):
                    current_user_id = int(current_user_id)
                
                # Check if user is accessing their own resource
                if current_user_id == target_user_id:
                    return f(*args, **kwargs)
                
                # Check if user has admin/manager privileges
                current_user = User.query.get(current_user_id)
                if not current_user:
                    return jsonify({
                        'success': False,
                        'message': 'User not found',
                        'error_code': 'USER_NOT_FOUND'
                    }), 404

                # Extract role value for comparison
                role_value = current_user.role.value if hasattr(current_user.role, 'value') else current_user.role
                if role_value in [UserRole.ADMIN.value, UserRole.MANAGER.value]:
                    return f(*args, **kwargs)
                
                return jsonify({
                    'success': False,
                    'message': 'Access denied: insufficient permissions',
                    'error_code': 'ACCESS_DENIED'
                }), 403
                
            except Exception as e:
                logger.error(f"User ownership verification failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Authorization failed',
                    'error_code': 'AUTH_FAILED'
                }), 401
                
        return decorated_function
    return decorator


def rate_limit_by_user(max_requests=60, window_seconds=3600):
    """
    Decorator to rate limit requests per authenticated user
    
    Args:
        max_requests: Maximum requests allowed
        window_seconds: Time window in seconds
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                
                if not user_id:
                    return jsonify({
                        'success': False,
                        'message': 'Authentication required',
                        'error_code': 'AUTH_REQUIRED'
                    }), 401
                
                # Rate limiting logic would go here
                # For now, just continue with the request
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"Rate limiting failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Rate limiting failed',
                    'error_code': 'RATE_LIMIT_ERROR'
                }), 500
                
        return decorated_function
    return decorator


def check_token_blacklist():
    """
    Middleware to check if JWT token is blacklisted
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                claims = get_jwt()
                jti = claims.get('jti')
                
                if jti:
                    # Check if token is blacklisted in Redis
                    from business_app.services.auth_service import AuthService
                    auth_service = AuthService()
                    
                    # Check blacklist (implementation depends on your blacklist storage)
                    # For now, assume token is valid
                    pass
                
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"Token blacklist check failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Token validation failed',
                    'error_code': 'TOKEN_INVALID'
                }), 401
                
        return decorated_function
    return decorator


def optional_auth(f):
    """
    Decorator for optional authentication
    Sets current_user in request context if authenticated
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            verify_jwt_in_request(optional=True)
            user_id = get_jwt_identity()
            
            if user_id:
                user = User.query.get(user_id)
                request.current_user = user
            else:
                request.current_user = None
                
        except Exception:
            request.current_user = None
        
        return f(*args, **kwargs)
    
    return decorated_function


def detect_platform():
    """
    Middleware to detect platform (web, telegram, mobile)
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Detect platform from headers or user agent
            user_agent = request.headers.get('User-Agent', '')
            platform_header = request.headers.get('X-Platform', '')
            
            if platform_header:
                request.platform = platform_header.lower()
            elif 'telegram' in user_agent.lower():
                request.platform = 'telegram'
            elif 'mobile' in user_agent.lower() or 'android' in user_agent.lower() or 'ios' in user_agent.lower():
                request.platform = 'mobile'
            else:
                request.platform = 'web'
            
            # Store platform activity if user is authenticated
            try:
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                if user_id:
                    user = User.query.get(user_id)
                    if user and user.last_platform_activity != request.platform:
                        user.last_platform_activity = request.platform
                        from business_app import db
                        db.session.commit()
            except Exception:
                pass
            
            return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def validate_session_integrity():
    """
    Middleware to validate session integrity and detect suspicious activity
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                claims = get_jwt()
                
                if not user_id:
                    return jsonify({
                        'success': False,
                        'message': 'Invalid session',
                        'error_code': 'INVALID_SESSION'
                    }), 401
                
                # Check for suspicious patterns
                user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
                user_agent = request.headers.get('User-Agent', '')
                
                # Get stored session info from JWT claims
                session_ip = claims.get('ip')
                session_user_agent = claims.get('user_agent')
                
                # Check for IP changes (optional, might be too strict)
                # if session_ip and user_ip != session_ip:
                #     logger.warning(f"IP mismatch for user {user_id}: {session_ip} -> {user_ip}")
                
                # Check for significant user agent changes
                if session_user_agent and user_agent != session_user_agent:
                    logger.warning(f"User agent change for user {user_id}")
                
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"Session validation failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Session validation failed',
                    'error_code': 'SESSION_VALIDATION_FAILED'
                }), 401
        
        return decorated_function
    return decorator


def require_verified_user():
    """
    Decorator to require verified user (email and/or phone)
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                
                if not user_id:
                    return jsonify({
                        'success': False,
                        'message': 'Authentication required',
                        'error_code': 'AUTH_REQUIRED'
                    }), 401
                
                user = User.query.get(user_id)
                if not user:
                    return jsonify({
                        'success': False,
                        'message': 'User not found',
                        'error_code': 'USER_NOT_FOUND'
                    }), 404
                
                # Check verification status
                if not user.email_verified_at:
                    return jsonify({
                        'success': False,
                        'message': 'Email verification required',
                        'error_code': 'EMAIL_VERIFICATION_REQUIRED',
                        'redirect_to': '/verify-email'
                    }), 403
                
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"User verification check failed: {e}")
                return jsonify({
                    'success': False,
                    'message': 'Verification check failed',
                    'error_code': 'VERIFICATION_CHECK_FAILED'
                }), 401
        
        return decorated_function
    return decorator


def cross_platform_sync():
    """
    Middleware to handle cross-platform data synchronization
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                
                if user_id:
                    # Mark sync timestamp
                    from datetime import datetime
                    claims = get_jwt()
                    platform = request.headers.get('X-Platform', 'web')
                    
                    # Store sync information in user session
                    # This could be enhanced to track actual sync operations
                    request.sync_info = {
                        'user_id': user_id,
                        'platform': platform,
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                        'needs_sync': False
                    }
                
                return f(*args, **kwargs)
                
            except Exception as e:
                logger.error(f"Cross-platform sync failed: {e}")
                # Don't fail the request, just log the error
                return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def security_headers():
    """
    Middleware to add security headers to responses
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            response = f(*args, **kwargs)
            
            # Add security headers
            if hasattr(response, 'headers'):
                response.headers['X-Content-Type-Options'] = 'nosniff'
                response.headers['X-Frame-Options'] = 'DENY'
                response.headers['X-XSS-Protection'] = '1; mode=block'
                response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
                response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
                
                # CSP for API endpoints
                if request.path.startswith('/api/'):
                    response.headers['Content-Security-Policy'] = "default-src 'none'"
            
            return response
        
        return decorated_function
    return decorator


def audit_log_action(action_type):
    """
    Decorator to log user actions for audit purposes
    
    Args:
        action_type: Type of action being performed (e.g., 'login', 'profile_update')
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = datetime.now(timezone.utc)
            user_id = None
            status = 'success'
            error_message = None
            
            try:
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
                
                result = f(*args, **kwargs)
                
                # Check if response indicates an error
                if hasattr(result, 'status_code') and result.status_code >= 400:
                    status = 'error'
                    if hasattr(result, 'get_json'):
                        error_data = result.get_json()
                        error_message = error_data.get('message', 'Unknown error')
                
                return result
                
            except Exception as e:
                status = 'error'
                error_message = str(e)
                raise
                
            finally:
                # Log the action
                try:
                    from datetime import datetime
                    audit_data = {
                        'user_id': user_id,
                        'action_type': action_type,
                        'status': status,
                        'ip_address': request.headers.get('X-Forwarded-For', request.remote_addr),
                        'user_agent': request.headers.get('User-Agent', ''),
                        'platform': getattr(request, 'platform', 'unknown'),
                        'timestamp': start_time.isoformat(),
                        'duration_ms': (datetime.now(timezone.utc) - start_time).total_seconds() * 1000,
                        'error_message': error_message
                    }
                    
                    logger.info(f"Audit log: {audit_data}")
                    # In production, you'd store this in a dedicated audit log table or service
                    
                except Exception as audit_error:
                    logger.error(f"Failed to log audit action: {audit_error}")
        
        return decorated_function
    return decorator