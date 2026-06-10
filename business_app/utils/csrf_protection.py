"""
CSRF Protection utilities for the Water Business Platform
Provides comprehensive Cross-Site Request Forgery protection for all forms and API endpoints
"""

import hmac
import hashlib
import secrets
import time
from typing import Optional, List
from functools import wraps

from flask import Flask, request, jsonify, current_app, session
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request

from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity


class CSRFProtectionManager:
    """Enhanced CSRF protection manager with multiple validation methods"""

    def __init__(self, app: Flask = None):
        self.app = app
        self.csrf = CSRFProtect()
        self.token_ttl = 3600  # 1 hour default
        self.double_submit_secret = None

        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask):
        """Initialize CSRF protection with the Flask app"""
        self.app = app

        # Configure Flask-WTF CSRF protection
        app.config.setdefault("WTF_CSRF_ENABLED", True)
        app.config.setdefault("WTF_CSRF_TIME_LIMIT", self.token_ttl)
        app.config.setdefault("WTF_CSRF_SSL_STRICT", app.config.get("ENV") == "production")
        app.config.setdefault("WTF_CSRF_CHECK_DEFAULT", False)  # We'll handle manually

        # Set CSRF secret key
        csrf_secret = app.config.get("WTF_CSRF_SECRET_KEY") or app.config.get("SECRET_KEY")
        if not csrf_secret:
            if app.config.get("ENV") == "production":
                raise RuntimeError("CSRF secret key must be configured in production")
            csrf_secret = secrets.token_urlsafe(32)

        app.config["WTF_CSRF_SECRET_KEY"] = csrf_secret
        self.double_submit_secret = csrf_secret

        # Initialize Flask-WTF CSRF
        self.csrf.init_app(app)

        # Configure custom error handlers
        self._setup_error_handlers(app)

        # Configure exemptions for specific endpoints
        self._setup_exemptions(app)

    def _setup_error_handlers(self, app: Flask):
        """Setup custom CSRF error handlers"""

        @app.errorhandler(CSRFError)
        def csrf_error(e):
            """Handle CSRF validation errors"""
            current_app.logger.warning(f"CSRF validation failed: {e.description}")

            # Log security event
            audit_logger.log_event(
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                action="csrf_validation_failed",
                severity=AuditSeverity.HIGH,
                success=False,
                resource_type="csrf_protection",
                description=f"CSRF validation failed: {e.description}",
                additional_data={
                    "reason": str(e.description),
                    "endpoint": request.endpoint,
                    "method": request.method,
                    "remote_addr": request.remote_addr,
                    "user_agent": request.headers.get("User-Agent"),
                    "referer": request.headers.get("Referer"),
                },
            )

            return jsonify({"error": "CSRF token validation failed", "message": "Invalid or missing CSRF token"}), 400

    def _setup_exemptions(self, app: Flask):
        """Setup CSRF exemptions for specific endpoints"""

        # Exempt webhook endpoints (they use signature validation)
        webhook_endpoints = ["payments.payment_webhook", "telegram.webhook"]

        for endpoint in webhook_endpoints:
            self.csrf.exempt(endpoint)

        # Exempt health check and public endpoints
        public_endpoints = ["health_check", "get_exchange_rates"]

        for endpoint in public_endpoints:
            self.csrf.exempt(endpoint)

    def generate_csrf_token(self, user_id: Optional[int] = None) -> str:
        """
        Generate a CSRF token for the current session or user

        Args:
            user_id: Optional user ID for user-specific tokens

        Returns:
            str: CSRF token
        """
        try:
            # Use Flask-WTF's token generation for session-based tokens
            from flask_wtf.csrf import generate_csrf

            return generate_csrf()

        except Exception as e:
            current_app.logger.error(f"Failed to generate CSRF token: {e}")
            # Fallback to manual token generation
            return self._generate_manual_token(user_id)

    def _generate_manual_token(self, user_id: Optional[int] = None) -> str:
        """
        Generate CSRF token manually as fallback

        Args:
            user_id: Optional user ID for user-specific tokens

        Returns:
            str: CSRF token
        """
        timestamp = str(int(time.time()))
        session_id = session.get("_id", secrets.token_urlsafe(16))
        user_part = str(user_id) if user_id else "anonymous"

        # Create token data
        token_data = f"{timestamp}:{session_id}:{user_part}"

        # Create HMAC signature
        signature = hmac.new(
            self.double_submit_secret.encode("utf-8"), token_data.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Combine data and signature
        token = f"{token_data}:{signature}"

        # Base64 encode for safer transport
        import base64

        return base64.urlsafe_b64encode(token.encode("utf-8")).decode("utf-8")

    def validate_csrf_token(self, token: str, user_id: Optional[int] = None) -> bool:
        """
        Validate a CSRF token

        Args:
            token: CSRF token to validate
            user_id: Optional user ID for validation

        Returns:
            bool: True if valid, False otherwise
        """
        try:
            # First try Flask-WTF validation
            from flask_wtf.csrf import validate_csrf

            validate_csrf(token)
            return True

        except CSRFError:
            # Fallback to manual validation
            return self._validate_manual_token(token, user_id)
        except Exception as e:
            current_app.logger.error(f"CSRF token validation error: {e}")
            return False

    def _validate_manual_token(self, token: str, user_id: Optional[int] = None) -> bool:
        """
        Validate manually generated CSRF token

        Args:
            token: CSRF token to validate
            user_id: Optional user ID for validation

        Returns:
            bool: True if valid, False otherwise
        """
        try:
            # Base64 decode
            import base64

            decoded_token = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")

            # Split token parts
            parts = decoded_token.split(":")
            if len(parts) != 4:
                return False

            timestamp_str, session_id, user_part, signature = parts

            # Validate timestamp (not too old)
            try:
                token_time = int(timestamp_str)
                current_time = int(time.time())
                if current_time - token_time > self.token_ttl:
                    return False
            except ValueError:
                return False

            # Validate user part
            expected_user_part = str(user_id) if user_id else "anonymous"
            if user_part != expected_user_part:
                return False

            # Recreate token data and validate signature
            token_data = f"{timestamp_str}:{session_id}:{user_part}"
            expected_signature = hmac.new(
                self.double_submit_secret.encode("utf-8"), token_data.encode("utf-8"), hashlib.sha256
            ).hexdigest()

            return hmac.compare_digest(signature, expected_signature)

        except Exception as e:
            current_app.logger.error(f"Manual CSRF token validation error: {e}")
            return False


# Global CSRF protection instance
csrf_protection = CSRFProtectionManager()


def csrf_required(f):
    """
    Decorator to require CSRF protection for a specific endpoint
    Can be used in addition to or instead of global CSRF protection
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Skip CSRF for excluded methods
        if request.method in ["GET", "HEAD", "OPTIONS"]:
            return f(*args, **kwargs)

        # Get CSRF token from various sources
        csrf_token = (
            request.headers.get("X-CSRFToken")
            or request.headers.get("X-CSRF-Token")
            or request.form.get("csrf_token")
            or request.json.get("csrf_token")
            if request.is_json
            else None
        )

        if not csrf_token:
            current_app.logger.warning(f"Missing CSRF token for {request.endpoint}")
            return (
                jsonify(
                    {
                        "error": "CSRF token required",
                        "message": "CSRF token must be provided in headers or request body",
                    }
                ),
                400,
            )

        # Get user ID if available (for JWT protected endpoints)
        user_id = None
        try:
            if hasattr(request, "headers") and "Authorization" in request.headers:
                verify_jwt_in_request(optional=True)
                user_id = get_jwt_identity()
        except Exception:
            pass  # JWT not present or invalid, continue with anonymous validation

        # Validate CSRF token
        if not csrf_protection.validate_csrf_token(csrf_token, user_id):
            current_app.logger.warning(f"Invalid CSRF token for {request.endpoint}")

            # Log security event
            audit_logger.log_event(
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                action="csrf_token_invalid",
                severity=AuditSeverity.HIGH,
                resource_type="csrf_protection",
                description=f"Invalid CSRF token for endpoint {request.endpoint}",
                additional_data={
                    "user_id": user_id,
                    "endpoint": request.endpoint,
                    "method": request.method,
                    "remote_addr": request.remote_addr,
                    "user_agent": request.headers.get("User-Agent"),
                },
            )

            return jsonify({"error": "Invalid CSRF token", "message": "CSRF token validation failed"}), 400

        return f(*args, **kwargs)

    return decorated_function


def csrf_exempt(f):
    """
    Decorator to exempt an endpoint from CSRF protection
    Use sparingly and only for endpoints with alternative protection
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        return f(*args, **kwargs)

    # Mark function as CSRF exempt
    decorated_function._csrf_exempt = True
    return decorated_function


def get_csrf_token() -> str:
    """
    Get CSRF token for the current request/session

    Returns:
        str: CSRF token
    """
    user_id = None
    try:
        if hasattr(request, "headers") and "Authorization" in request.headers:
            verify_jwt_in_request(optional=True)
            user_id = get_jwt_identity()
    except Exception:
        pass

    return csrf_protection.generate_csrf_token(user_id)


def setup_csrf_protection(app: Flask):
    """
    Setup CSRF protection for the Flask application

    Args:
        app: Flask application instance
    """
    # Initialize CSRF protection
    csrf_protection.init_app(app)

    # Add CSRF token endpoint
    @app.route("/api/v1/csrf-token", methods=["GET"])
    def get_csrf_token_endpoint():
        """Get CSRF token for client-side use"""
        try:
            token = get_csrf_token()
            return jsonify({"csrf_token": token, "expires_in": csrf_protection.token_ttl})
        except Exception as e:
            current_app.logger.error(f"Error generating CSRF token: {e}")
            return jsonify({"error": "Failed to generate CSRF token"}), 500

    # Add middleware to inject CSRF tokens in responses
    @app.after_request
    def inject_csrf_token(response):
        """Inject CSRF token in response headers for AJAX requests"""
        # Only inject for HTML and JSON responses
        if response.content_type and (
            "text/html" in response.content_type or "application/json" in response.content_type
        ):  # noqa: E501
            try:
                token = get_csrf_token()
                response.headers["X-CSRFToken"] = token
            except Exception as e:
                current_app.logger.debug(f"Could not inject CSRF token: {e}")

        return response

    app.logger.info("CSRF protection initialized successfully")


def protect_forms_with_csrf(endpoints: List[str]):
    """
    Apply CSRF protection to a list of form endpoints

    Args:
        endpoints: List of endpoint names to protect
    """
    for endpoint in endpoints:
        try:
            view_func = current_app.view_functions.get(endpoint)
            if view_func and not getattr(view_func, "_csrf_exempt", False):
                # Wrap the view function with CSRF protection
                current_app.view_functions[endpoint] = csrf_required(view_func)
                current_app.logger.debug(f"Applied CSRF protection to {endpoint}")
        except Exception as e:
            current_app.logger.error(f"Failed to apply CSRF protection to {endpoint}: {e}")


def validate_double_submit_csrf(token: str, cookie_token: str) -> bool:
    """
    Validate double-submit CSRF pattern

    Args:
        token: CSRF token from request
        cookie_token: CSRF token from cookie

    Returns:
        bool: True if valid, False otherwise
    """
    if not token or not cookie_token:
        return False

    return hmac.compare_digest(token, cookie_token)
