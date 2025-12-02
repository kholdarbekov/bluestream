"""
Fine-Grained Rate Limiting Configuration
Provides custom rate limit decorators for different endpoint categories
"""
from functools import wraps
from flask import request
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from business_app import limiter


def get_user_identifier():
    """
    Get user identifier for rate limiting
    - For authenticated requests: use user ID
    - For anonymous requests: use IP address
    """
    try:
        # Try to get JWT user ID if available
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
        if user_id:
            return f"user:{user_id}"
    except:
        pass

    # Fallback to IP address
    return request.remote_addr


def get_ip_address():
    """Get client IP address"""
    # Check for forwarded IP (proxy/load balancer)
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr


# Rate limit configurations for different endpoint categories
RATE_LIMITS = {
    # Authentication endpoints
    'auth.register': ['5/hour', '20/day'],  # Very restrictive
    'auth.login': ['10/minute', '100/hour'],  # Prevent brute force
    'auth.forgot_password': ['3/hour', '10/day'],  # Prevent email bombing
    'auth.reset_password': ['5/hour', '20/day'],  # Prevent abuse
    'auth.refresh': ['30/hour'],  # Token refresh
    'auth.logout': ['100/hour'],  # Normal usage
    'auth.verify_email': ['10/hour'],  # Email verification

    # Profile and user endpoints
    'user.read': ['1000/hour'],  # GET profile, list users
    'user.write': ['100/hour'],  # PUT/PATCH profile updates
    'user.delete': ['10/hour'],  # DELETE operations

    # Product endpoints
    'products.read': ['2000/hour'],  # GET products list/detail
    'products.search': ['500/hour'],  # Search queries
    'products.write': ['200/hour'],  # Admin create/update products

    # Cart endpoints
    'cart.operations': ['500/hour'],  # All cart operations

    # Order endpoints
    'orders.create': ['20/hour', '100/day'],  # Create order
    'orders.read': ['500/hour'],  # View orders
    'orders.update': ['50/hour'],  # Update order status

    # Payment endpoints
    'payments.all': ['50/hour'],  # All payment operations
    'payments.webhook': ['1000/hour'],  # Payment gateway webhooks

    # Delivery endpoints
    'delivery.tracking': ['200/hour'],  # Track deliveries
    'delivery.update': ['100/hour'],  # Update delivery status

    # Notification endpoints
    'notifications.send': ['100/hour'],  # Send notifications
    'notifications.read': ['500/hour'],  # Read notifications

    # Loyalty endpoints
    'loyalty.operations': ['200/hour'],  # Loyalty program operations

    # Subscription endpoints
    'subscriptions.operations': ['100/hour'],  # Subscription management

    # Admin endpoints
    'admin.read': ['1000/hour'],  # Admin read operations
    'admin.write': ['500/hour'],  # Admin write operations
    'admin.delete': ['100/hour'],  # Admin delete operations

    # Analytics endpoints
    'analytics.query': ['200/hour'],  # Analytics queries

    # Blog endpoints
    'blog.read': ['1000/hour'],  # Read blog posts
    'blog.write': ['100/hour'],  # Create/update blog posts

    # File upload endpoints
    'upload.image': ['50/hour', '200/day'],  # Image uploads
    'upload.file': ['30/hour', '100/day'],  # File uploads

    # Static files - very high limit
    'static.files': ['5000/hour'],  # Static file serving

    # Default fallback
    'default': ['100/hour'],
}


class RateLimitDecorators:
    """
    Collection of rate limit decorators for different endpoint types
    """

    @staticmethod
    def auth_register(func):
        """Rate limit for registration endpoints"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['auth.register'],
            key_func=get_ip_address,
            error_message="Too many registration attempts. Please try again later."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def auth_login(func):
        """Rate limit for login endpoints"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['auth.login'],
            key_func=get_ip_address,
            error_message="Too many login attempts. Please try again later."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def auth_password_reset(func):
        """Rate limit for password reset endpoints"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['auth.forgot_password'],
            key_func=get_ip_address,
            error_message="Too many password reset requests. Please try again later."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def auth_refresh(func):
        """Rate limit for token refresh"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['auth.refresh'],
            key_func=get_user_identifier,
            error_message="Too many token refresh requests."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def user_read(func):
        """Rate limit for user read operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['user.read'],
            key_func=get_user_identifier
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def user_write(func):
        """Rate limit for user write operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['user.write'],
            key_func=get_user_identifier,
            error_message="Too many profile update requests."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def products_read(func):
        """Rate limit for product read operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['products.read'],
            key_func=get_ip_address
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def cart_operations(func):
        """Rate limit for cart operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['cart.operations'],
            key_func=get_user_identifier
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def orders_create(func):
        """Rate limit for order creation"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['orders.create'],
            key_func=get_user_identifier,
            error_message="Too many order creation requests. Please contact support if you need assistance."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def orders_read(func):
        """Rate limit for order read operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['orders.read'],
            key_func=get_user_identifier
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def payments(func):
        """Rate limit for payment operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['payments.all'],
            key_func=get_user_identifier,
            error_message="Too many payment requests. Please wait before trying again."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def payment_webhook(func):
        """Rate limit for payment webhooks (more lenient)"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['payments.webhook'],
            key_func=get_ip_address
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def admin_read(func):
        """Rate limit for admin read operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['admin.read'],
            key_func=get_user_identifier
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def admin_write(func):
        """Rate limit for admin write operations"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['admin.write'],
            key_func=get_user_identifier
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def upload_image(func):
        """Rate limit for image uploads"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['upload.image'],
            key_func=get_user_identifier,
            error_message="Too many image uploads. Please wait before uploading more files."
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def analytics(func):
        """Rate limit for analytics queries"""
        @wraps(func)
        @limiter.limit(
            RATE_LIMITS['analytics.query'],
            key_func=get_user_identifier
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper

    @staticmethod
    def custom(limits, key_func=None, error_message=None):
        """
        Custom rate limit decorator with specified limits

        Args:
            limits: List of rate limit strings (e.g., ['10/minute', '100/hour'])
            key_func: Function to get the rate limit key (default: get_user_identifier)
            error_message: Custom error message
        """
        def decorator(func):
            @wraps(func)
            @limiter.limit(
                limits,
                key_func=key_func or get_user_identifier,
                error_message=error_message or "Rate limit exceeded."
            )
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)
            return wrapper
        return decorator


# Convenience function to exempt endpoints from rate limiting
def exempt_from_rate_limit(func):
    """Exempt an endpoint from rate limiting (e.g., health checks)"""
    return limiter.exempt(func)


# Export commonly used decorators
rate_limit = RateLimitDecorators()
