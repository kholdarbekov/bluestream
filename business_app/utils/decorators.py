"""
Custom decorators for the Water Business Platform
"""
import json
from functools import wraps
from flask import request, jsonify, g, current_app
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt
import time
from typing import List, Optional, Callable, Any
import redis

from .exceptions import UnauthorizedError, ForbiddenError, RateLimitError
from .constants import UserRole, UserStatus
from .rbac import (
    rbac, require_permission, require_role, require_admin, require_manager_or_admin,
    require_staff, require_own_resource_or_staff, audit_access, Permission, AccessLevel
)


def require_auth(f):
    """Require valid JWT token"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            verify_jwt_in_request()
            g.current_user_id = get_jwt_identity()
            return f(*args, **kwargs)
        except Exception as e:
            raise UnauthorizedError("Authentication required")
    return decorated_function


def require_roles(allowed_roles: List[UserRole]):
    """Require specific user roles"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            verify_jwt_in_request()
            claims = get_jwt()
            user_role = claims.get('role')
            
            if not user_role or UserRole(user_role) not in allowed_roles:
                raise ForbiddenError("Insufficient permissions")
            
            g.current_user_id = get_jwt_identity()
            g.current_user_role = UserRole(user_role)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def admin_required(f):
    """Require admin role"""
    return require_roles([UserRole.ADMIN])(f)


def super_admin_required(f):
    """Require admin role with additional validation"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        verify_jwt_in_request()
        claims = get_jwt()
        user_role = claims.get('role')
        user_id = get_jwt_identity()
        
        if not user_role or UserRole(user_role) != UserRole.ADMIN:
            raise ForbiddenError("Super admin access required")
        
        # Additional validation for super admin operations
        from business_app.models.user import User
        user = User.query.get(user_id)
        status_value = user.status.value if user and hasattr(user.status, 'value') else (user.status if user else None)
        if not user or not user.is_admin or status_value != UserStatus.ACTIVE.value:
            raise ForbiddenError("Active admin account required")
        
        g.current_user_id = user_id
        g.current_user_role = UserRole(user_role)
        g.current_user = user
        return f(*args, **kwargs)
    return decorated_function


def manager_or_higher_required(f):
    """Require manager or higher role with additional safety checks"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        verify_jwt_in_request()
        claims = get_jwt()
        user_role = claims.get('role')
        user_id = get_jwt_identity()
        
        current_app.logger.info(f"user {user_id}: JWT claims {claims}")
        
        try:
            role_enum = UserRole(user_role)
        except (ValueError, TypeError):
            raise ForbiddenError("Invalid user role")
        
        if role_enum.value not in [UserRole.ADMIN.value, UserRole.MANAGER.value]:
            raise ForbiddenError("Manager or admin access required")

        # Additional validation
        from business_app.models.user import User
        user = User.query.get(user_id)
        status_value = user.status.value if user and hasattr(user.status, 'value') else (user.status if user else None)
        if not user or status_value != UserStatus.ACTIVE.value:
            raise ForbiddenError("Active account required")

        # Check if user still has the claimed role
        role_value = user.role.value if hasattr(user.role, 'value') else user.role
        if role_value != role_enum.value:
            current_app.logger.warning(f"Role mismatch for user {user_id}: JWT claims {user_role}, DB has {role_value}")
            raise ForbiddenError("Role validation failed")
        
        g.current_user_id = user_id
        g.current_user_role = role_enum.value
        g.current_user = user
        return f(*args, **kwargs)
    return decorated_function


def staff_or_higher_required(f):
    """Require staff or higher role with validation"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        verify_jwt_in_request()
        claims = get_jwt()
        user_role = claims.get('role')
        user_id = get_jwt_identity()
        
        current_app.logger.info(f"user {user_id}: JWT claims {claims}")
        
        try:
            role_enum = UserRole(user_role)
        except (ValueError, TypeError):
            raise ForbiddenError("Invalid user role")
        
        if role_enum.value not in [UserRole.ADMIN.value, UserRole.MANAGER.value, UserRole.OPERATOR.value]:
            raise ForbiddenError("Staff access required")

        # Additional validation
        from business_app.models.user import User
        user = User.query.get(user_id)
        status_value = user.status.value if user and hasattr(user.status, 'value') else (user.status if user else None)
        if not user or status_value != UserStatus.ACTIVE.value:
            raise ForbiddenError("Active account required")

        # Check if user still has the claimed role
        role_value = user.role.value if hasattr(user.role, 'value') else user.role
        if role_value != role_enum.value:
            current_app.logger.warning(f"Role mismatch for user {user_id}: JWT claims {user_role}, DB has {role_value}")
            raise ForbiddenError("Role validation failed")
        
        g.current_user_id = user_id
        g.current_user_role = role_enum.value
        g.current_user = user
        return f(*args, **kwargs)
    return decorated_function


def validate_admin_action(required_permissions: List[str] = None):
    """Validate admin action with specific permissions"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            
            from business_app.models.user import User
            user = User.query.get(user_id)
            if not user:
                raise ForbiddenError("User not found")

            # Extract role and status values for comparison
            role_value = user.role.value if hasattr(user.role, 'value') else user.role
            status_value = user.status.value if hasattr(user.status, 'value') else user.status

            # Check if user has admin privileges
            if role_value not in [UserRole.ADMIN.value, UserRole.MANAGER.value, UserRole.OPERATOR.value]:
                raise ForbiddenError("Administrative access required")

            # Check account status
            if status_value != UserStatus.ACTIVE.value:
                raise ForbiddenError("Account suspended or inactive")
            
            # Check specific permissions if provided
            if required_permissions:
                # This would integrate with a permission system
                # For now, we'll implement basic role-based checks
                user_permissions = []

                if role_value == UserRole.ADMIN.value:
                    user_permissions = ['all']  # Admin has all permissions
                elif role_value == UserRole.MANAGER.value:
                    user_permissions = [
                        'view_users', 'manage_orders', 'view_reports',
                        'manage_products', 'view_analytics'
                    ]
                elif role_value == UserRole.OPERATOR.value:
                    user_permissions = [
                        'view_orders', 'update_orders', 'view_products'
                    ]
                
                # Check if user has required permissions
                has_permission = (
                    'all' in user_permissions or 
                    any(perm in user_permissions for perm in required_permissions)
                )
                
                if not has_permission:
                    current_app.logger.warning(
                        f"Permission denied for user {user_id} ({user.role}). "
                        f"Required: {required_permissions}, Has: {user_permissions}"
                    )
                    raise ForbiddenError("Insufficient permissions for this action")
            
            g.current_user_id = user_id
            g.current_user_role = user.role
            g.current_user = user
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def manager_or_admin_required(f):
    """Require manager or admin role"""
    return require_roles([UserRole.ADMIN, UserRole.MANAGER])(f)


def staff_required(f):
    """Require staff role (admin, manager, or operator)"""
    return require_roles([UserRole.ADMIN, UserRole.MANAGER, UserRole.OPERATOR])(f)


def rate_limit(max_requests: int, window_seconds: int, per: str = 'ip'):
    """Rate limiting decorator"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Get identifier based on 'per' parameter
            if per == 'ip':
                identifier = request.remote_addr
            elif per == 'user':
                try:
                    verify_jwt_in_request()
                    identifier = get_jwt_identity()
                except:
                    identifier = request.remote_addr
            else:
                identifier = request.remote_addr
            
            # Create Redis key
            key = f"rate_limit:{f.__name__}:{identifier}"
            
            try:
                redis_client = redis.from_url(current_app.config['REDIS_URL'])
                
                # Get current count
                current_count = redis_client.get(key)
                
                if current_count is None:
                    # First request in window
                    redis_client.setex(key, window_seconds, 1)
                    return f(*args, **kwargs)
                else:
                    current_count = int(current_count)
                    if current_count >= max_requests:
                        # Rate limit exceeded
                        ttl = redis_client.ttl(key)
                        raise RateLimitError(
                            f"Rate limit exceeded. Try again in {ttl} seconds.",
                            details={'retry_after': ttl, 'limit': max_requests}
                        )
                    else:
                        # Increment counter
                        redis_client.incr(key)
                        return f(*args, **kwargs)
            
            except redis.RedisError:
                # If Redis is down, allow the request
                current_app.logger.warning("Redis unavailable for rate limiting")
                return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def rate_limit_by_telegram_id(max_requests: int, window_seconds: int):
    """
    Rate limiting decorator for telegram-login endpoint.
    
    Rate limits by telegram_id from request body instead of IP address,
    giving each Telegram user their own rate limit bucket.
    
    Args:
        max_requests: Maximum requests allowed per user
        window_seconds: Time window in seconds
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Extract telegram_id from request body
            data = request.get_json(silent=True) or {}
            telegram_id = data.get('telegram_id')
            
            if telegram_id:
                # Use telegram_id as identifier for per-user rate limiting
                identifier = f"telegram:{telegram_id}"
            else:
                # Fall back to IP if no telegram_id provided
                identifier = f"ip:{request.remote_addr}"
            
            # Create Redis key
            key = f"rate_limit:{f.__name__}:{identifier}"
            
            try:
                redis_client = redis.from_url(current_app.config['REDIS_URL'])
                
                # Get current count
                current_count = redis_client.get(key)
                
                if current_count is None:
                    # First request in window
                    redis_client.setex(key, window_seconds, 1)
                    return f(*args, **kwargs)
                else:
                    current_count = int(current_count)
                    if current_count >= max_requests:
                        # Rate limit exceeded
                        ttl = redis_client.ttl(key)
                        current_app.logger.warning(
                            f"Rate limit exceeded for {identifier}: "
                            f"{current_count}/{max_requests} requests in {window_seconds}s window"
                        )
                        raise RateLimitError(
                            f"Rate limit exceeded. Try again in {ttl} seconds.",
                            details={
                                'retry_after': ttl, 
                                'limit': max_requests,
                                'identifier_type': 'telegram_id' if telegram_id else 'ip'
                            }
                        )
                    else:
                        # Increment counter
                        redis_client.incr(key)
                        return f(*args, **kwargs)
            
            except redis.RedisError as e:
                # If Redis is down, allow the request but log warning
                current_app.logger.warning(f"Redis unavailable for rate limiting: {e}")
                return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def cache_result(timeout: int = 300, key_prefix: str = None):
    """Cache function result"""

    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Generate cache key
            if key_prefix:
                cache_key = f"{key_prefix}:{hash(str(args) + str(kwargs))}"
            else:
                cache_key = f"{f.__name__}:{hash(str(args) + str(kwargs))}"
            
            try:
                redis_client = redis.from_url(current_app.config['REDIS_URL'])
                
                # Try to get from cache
                cached_result = redis_client.get(cache_key)
                if cached_result:
                    import json
                    return json.loads(cached_result)
                
                # Execute function and cache result
                result = f(*args, **kwargs)
                redis_client.setex(cache_key, timeout, json.dumps(result, default=str))
                return result
            
            except (redis.RedisError, TypeError, ValueError):
                # If caching fails, just return the result
                return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def cache_response(timeout: int = 300, key_prefix: str = None):
    """Cache HTTP response - includes language in cache key for i18n support"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Generate cache key from request path and query params
            from urllib.parse import urlencode
            query_string = urlencode(sorted(request.args.items()))
            
            # CRITICAL: Include language in cache key to prevent serving wrong language
            # Priority: URL param > g.language > session > default
            language = request.args.get('language') or request.args.get('lang')
            if not language:
                language = getattr(g, 'language', None)
            if not language:
                from flask import session
                language = session.get('language')
            if not language:
                language = current_app.config.get('DEFAULT_LANGUAGE', 'uz')
            
            if key_prefix:
                cache_key = f"{key_prefix}:{language}:{request.path}:{query_string}:{hash(str(args) + str(kwargs))}"
            else:
                cache_key = f"response:{language}:{request.path}:{query_string}:{hash(str(args) + str(kwargs))}"
            
            current_app.logger.debug(f"[CACHE-RESPONSE] cache_key={cache_key}, lang={language}")
            
            try:
                redis_client = redis.from_url(current_app.config['REDIS_URL'])
                
                # Try to get from cache
                cached_response = redis_client.get(cache_key)
                if cached_response:
                    current_app.logger.debug(f"[CACHE-RESPONSE] HIT: {cache_key}")
                    cached_data = json.loads(cached_response)
                    response = jsonify(cached_data['data'])
                    response.status_code = cached_data['status_code']
                    return response
                
                current_app.logger.debug(f"[CACHE-RESPONSE] MISS: {cache_key}")
                
                # Execute function and cache response
                result = f(*args, **kwargs)

                # Extract response data and status code
                if isinstance(result, tuple) and len(result) == 2:
                    # (response, status_code) tuple from jsonify()
                    response_obj, status_code = result
                    if hasattr(response_obj, 'get_json'):
                        # Flask Response object
                        response_data = {
                            'data': response_obj.get_json(),
                            'status_code': status_code
                        }
                    else:
                        # Plain dict response
                        response_data = {
                            'data': response_obj,
                            'status_code': status_code
                        }
                elif hasattr(result, 'get_json') and hasattr(result, 'status_code'):
                    # Flask Response object without tuple
                    response_data = {
                        'data': result.get_json(),
                        'status_code': result.status_code
                    }
                else:
                    # Plain response dict
                    response_data = {
                        'data': result,
                        'status_code': 200
                    }

                redis_client.setex(cache_key, timeout, json.dumps(response_data, default=str))
                return result
            
            except (redis.RedisError, TypeError, ValueError):
                # If caching fails, just return the result
                return f(*args, **kwargs)
        
        return decorated_function
    return decorator


def measure_time(f):
    """Measure function execution time"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        result = f(*args, **kwargs)
        execution_time = time.time() - start_time
        
        current_app.logger.info(
            f"Function {f.__name__} executed in {execution_time:.4f} seconds"
        )
        return result
    return decorated_function


def log_request(f):
    """Log request details"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        current_app.logger.info(
            f"Request: {request.method} {request.path} "
            f"from {request.remote_addr} "
            f"User-Agent: {request.headers.get('User-Agent', 'Unknown')}"
        )
        return f(*args, **kwargs)
    return decorated_function


def validate_json(required_fields: List[str] = None):
    """Validate JSON request data"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not request.is_json:
                return jsonify({'error': 'Content-Type must be application/json'}), 400
            
            data = request.get_json()
            if not data:
                return jsonify({'error': 'Invalid JSON data'}), 400
            
            if required_fields:
                missing_fields = [field for field in required_fields if field not in data]
                if missing_fields:
                    return jsonify({
                        'error': 'Missing required fields',
                        'missing_fields': missing_fields
                    }), 400
            
            g.json_data = data
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def validate_order_input(validation_type: str):
    """Comprehensive order input validation decorator"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not request.is_json:
                return jsonify({'error': 'Content-Type must be application/json'}), 400
            
            data = request.get_json()
            if not data:
                return jsonify({'error': 'Invalid JSON data'}), 400
            
            # Import here to avoid circular imports
            from .order_validators import OrderInputValidator
            
            validator = OrderInputValidator()
            
            # Choose validation method based on type
            if validation_type == 'create_order':
                errors = validator.validate_create_order(data)
            elif validation_type == 'cart_estimate':
                errors = validator.validate_cart_estimate(data)
            elif validation_type == 'order_feedback':
                errors = validator.validate_order_feedback(data)
            elif validation_type == 'emergency_order':
                errors = validator.validate_emergency_order(data)
            elif validation_type == 'bulk_action':
                errors = validator.validate_bulk_action(data)
            elif validation_type == 'subscription_order':
                errors = validator.validate_subscription_order(data)
            elif validation_type == 'scheduled_order':
                errors = validator.validate_scheduled_order(data)
            elif validation_type == 'export':
                errors = validator.validate_export(data)
            else:
                current_app.logger.error(f"Unknown validation type: {validation_type}")
                return jsonify({'error': 'Internal validation error'}), 500
            
            if errors:
                return jsonify({
                    'error': 'Validation failed',
                    'validation_errors': errors
                }), 400
            
            g.validated_data = data
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def validate_query_params(validator_func: Callable):
    """Validate query parameters using a custom validator function"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            errors = validator_func(request.args.to_dict())
            
            if errors:
                return jsonify({
                    'error': 'Invalid query parameters',
                    'validation_errors': errors
                }), 400
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def handle_exceptions(f):
    """Handle exceptions and return JSON response - DEPRECATED: Use handle_api_exception instead"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Import the new error handler
        from .error_handlers import handle_api_exception
        
        # Apply the new error handler
        return handle_api_exception(f)(*args, **kwargs)
    
    return decorated_function


def require_feature(feature_name: str):
    """Require feature to be enabled"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            from .constants import FEATURES
            if not FEATURES.get(feature_name, False):
                return jsonify({
                    'error': 'Feature Disabled',
                    'message': f'Feature {feature_name} is not available'
                }), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def validate_file_upload(allowed_extensions: set = None, max_size: int = None, 
                         allowed_categories: Optional[List[str]] = None,
                         expected_category: Optional[str] = None,
                         enable_enhanced_validation: bool = True):
    """
    Enhanced file upload validation decorator with comprehensive security checks
    
    Args:
        allowed_extensions: Set of allowed file extensions (deprecated - use categories)
        max_size: Maximum file size in bytes
        allowed_categories: List of allowed file categories ('images', 'documents', etc.)
        expected_category: Expected file category for additional validation
        enable_enhanced_validation: Enable comprehensive security validation
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'file' not in request.files:
                return jsonify({'error': 'No file provided'}), 400
            
            file = request.files['file']
            if file.filename == '':
                return jsonify({'error': 'No file selected'}), 400
            
            if enable_enhanced_validation:
                # Use enhanced validation system
                try:
                    from business_app.utils.file_validation import validate_upload_file, FileValidationError
                    
                    validation_result = validate_upload_file(
                        file=file,
                        filename=file.filename,
                        allowed_categories=allowed_categories,
                        expected_category=expected_category
                    )
                    
                    # Check service-specific file size limit if provided
                    if max_size:
                        file_size = validation_result['validation_results']['size']
                        if file_size > max_size:
                            from .helpers import format_file_size
                            return jsonify({
                                'error': 'File too large for this endpoint',
                                'max_size': format_file_size(max_size),
                                'file_size': format_file_size(file_size)
                            }), 400
                    
                    # Log any warnings
                    warnings = validation_result['validation_results'].get('warnings', [])
                    if warnings:
                        current_app.logger.warning(f"File upload warnings for {file.filename}: {warnings}")
                    
                    # Store enhanced validation results
                    g.uploaded_file = file
                    g.file_validation_result = validation_result
                    
                except FileValidationError as e:
                    return jsonify({'error': f'File validation failed: {str(e)}'}), 400
                except Exception as e:
                    current_app.logger.error(f"File validation error: {e}")
                    return jsonify({'error': 'File validation failed'}), 400
            
            else:
                # Legacy validation (deprecated)
                current_app.logger.warning("Using deprecated file upload validation. Enable enhanced validation.")
                
                # Check file extension (legacy)
                if allowed_extensions:
                    from .helpers import get_file_extension
                    ext = get_file_extension(file.filename)
                    if ext not in allowed_extensions:
                        return jsonify({
                            'error': 'Invalid file type',
                            'allowed_extensions': list(allowed_extensions)
                        }), 400
                
                # Check file size (legacy)
                if max_size:
                    file.seek(0, 2)  # Seek to end
                    size = file.tell()
                    file.seek(0)  # Reset to beginning
                    
                    if size > max_size:
                        from .helpers import format_file_size
                        return jsonify({
                            'error': 'File too large',
                            'max_size': format_file_size(max_size),
                            'actual_size': format_file_size(size)
                        }), 400
                
                # Store file in g for use in route handler
                g.uploaded_file = file
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_subscription(subscription_type: str = None):
    """Require active subscription"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            
            # Import here to avoid circular imports
            from business_app.models.subscription import Subscription
            from business_app.models.user import User
            
            user = User.query.get(user_id)
            if not user:
                raise UnauthorizedError("User not found")
            
            active_subscription = Subscription.query.filter_by(
                user_id=user_id,
                status='active'
            ).first()
            
            if not active_subscription:
                return jsonify({
                    'error': 'Subscription Required',
                    'message': 'Active subscription required for this feature'
                }), 403
            
            if subscription_type and active_subscription.plan.name != subscription_type:
                return jsonify({
                    'error': 'Subscription Upgrade Required',
                    'message': f'This feature requires {subscription_type} subscription'
                }), 403
            
            g.current_subscription = active_subscription
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def business_hours_only(f):
    """Restrict access to business hours only"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from datetime import datetime
        from .constants import BUSINESS_HOURS
        
        now = datetime.now()
        if not (BUSINESS_HOURS['start'] <= now.hour < BUSINESS_HOURS['end']):
            return jsonify({
                'error': 'Outside Business Hours',
                'message': f'This service is only available between {BUSINESS_HOURS["start"]}:00 and {BUSINESS_HOURS["end"]}:00',
                'business_hours': f'{BUSINESS_HOURS["start"]}:00 - {BUSINESS_HOURS["end"]}:00'
            }), 403
        
        return f(*args, **kwargs)
    return decorated_function


def require_verification(verification_type: str = 'email'):
    """Require user verification"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            
            # Import here to avoid circular imports
            from business_app.models.user import User
            
            user = User.query.get(user_id)
            if not user:
                raise UnauthorizedError("User not found")
            
            if verification_type == 'email' and not user.email_verified:
                return jsonify({
                    'error': 'Email Verification Required',
                    'message': 'Please verify your email address first'
                }), 403
            
            if verification_type == 'phone' and not user.phone_verified:
                return jsonify({
                    'error': 'Phone Verification Required',
                    'message': 'Please verify your phone number first'
                }), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_minimum_order(min_amount: int = None):
    """Require minimum order amount"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if min_amount is None:
                min_amount = current_app.config['MIN_ORDER_AMOUNT']
            
            # This decorator should be used with order-related endpoints
            # The actual validation logic would be in the endpoint
            g.minimum_order_amount = min_amount
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def track_user_activity(activity_type: str):
    """Track user activity for analytics"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                verify_jwt_in_request()
                user_id = get_jwt_identity()
                
                # Import here to avoid circular imports
                from business_app.services.analytics_service import AnalyticsService
                
                # Track activity asynchronously
                from ..tasks.analytics_tasks import track_user_activity_task
                track_user_activity_task.delay(
                    user_id=user_id,
                    activity_type=activity_type,
                    endpoint=request.endpoint,
                    method=request.method,
                    ip_address=request.remote_addr,
                    user_agent=request.headers.get('User-Agent')
                )
            except Exception as e:
                # Don't fail the request if activity tracking fails
                current_app.logger.warning(f"Failed to track activity: {e}")
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def conditional_cache(condition_func: Callable, timeout: int = 300):
    """Conditionally cache result based on condition function"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if condition_func(*args, **kwargs):
                return cache_result(timeout)(f)(*args, **kwargs)
            else:
                return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_fresh_token(f):
    """Require fresh JWT token (not from refresh)"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        verify_jwt_in_request(fresh=True)
        g.current_user_id = get_jwt_identity()
        return f(*args, **kwargs)
    return decorated_function


def ip_whitelist(allowed_ips: List[str]):
    """Allow access only from whitelisted IPs"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            client_ip = request.remote_addr
            if client_ip not in allowed_ips:
                current_app.logger.warning(f"Access denied for IP: {client_ip}")
                return jsonify({
                    'error': 'Access Denied',
                    'message': 'Access not allowed from this IP address'
                }), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator