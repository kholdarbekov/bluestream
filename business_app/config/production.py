"""
Production environment configuration
"""

import os
from datetime import timedelta
from .base import BaseConfig, decimal_safe_json_serializer
from shared.constants import DISPLAY_TIMEZONE


class ProductionConfig(BaseConfig):
    """Production configuration with maximum security and performance"""

    DEBUG = False

    # Database Configuration
    # ARCH-010: production refuses to boot on a fallback-assembled URL.
    # Operators must supply DATABASE_URL explicitly so there is no chance of
    # pointing at `postgres://postgres:postgres@localhost` by accident.
    @property
    def SQLALCHEMY_DATABASE_URI(self):
        uri = os.environ.get("DATABASE_URL")
        if not uri:
            raise ValueError(
                "DATABASE_URL environment variable is required in production (no fallback assembly allowed)"
            )
        return uri

    # ARCH-010: JWT_SECRET_KEY must be an independent env var in production.
    # Base config falls back to SECRET_KEY, which silently collapses two distinct
    # secrets into one. Override to hard-require JWT_SECRET_KEY and refuse the fallback.
    @property
    def JWT_SECRET_KEY(self):
        jwt_secret = os.environ.get("JWT_SECRET_KEY")
        if not jwt_secret:
            raise ValueError("JWT_SECRET_KEY environment variable is required in production")
        return jwt_secret

    SQLALCHEMY_ECHO = False

    # Enhanced database configuration for production
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 50,  # Increased for production load
        "pool_timeout": 30,
        "pool_recycle": 3600,  # Recycle connections every hour
        "max_overflow": 20,
        "pool_pre_ping": True,
        # Decimal-tolerant JSON serializer (this override replaces the base dict
        # wholesale, so it must be repeated here). Prevents the prod
        # "Object of type Decimal is not JSON serializable" report crash.
        "json_serializer": decimal_safe_json_serializer,
        "connect_args": {
            "sslmode": "prefer",  # Prefer SSL but allow fallback if not supported
            "connect_timeout": 10,
            "application_name": os.environ.get("DB_NAME", "bluestream_prod"),
        },
    }

    # Redis Configuration
    @property
    def REDIS_URL(self):
        redis_url = os.environ.get("REDIS_URL")
        if not redis_url:
            raise ValueError("REDIS_URL environment variable is required in production")
        return redis_url

    # Rate Limiting Configuration - Strict in production
    # Note: This is the FALLBACK limit for endpoints without specific rate limits
    # Most endpoints should use the custom rate_limit decorator for fine-grained control
    @property
    def RATELIMIT_STORAGE_URL(self):
        return self.REDIS_URL

    RATELIMIT_DEFAULT = os.environ.get("RATE_LIMIT_REQUESTS", "1000/hour")  # Reasonable default for general endpoints
    RATELIMIT_ENABLED = True

    # Celery Configuration
    @property
    def CELERY(self):
        redis_url = self.REDIS_URL
        return {
            "broker_url": os.environ.get("CELERY_BROKER_URL", redis_url),
            "result_backend": os.environ.get("CELERY_RESULT_BACKEND", redis_url),
            "task_serializer": "json",
            "accept_content": ["json"],
            "result_serializer": "json",
            "timezone": DISPLAY_TIMEZONE,
            "enable_utc": True,
            "task_track_started": True,
            "result_expires": 3600,
            "worker_prefetch_multiplier": 1,
            "task_acks_late": True,
            "worker_disable_rate_limits": False,
            "task_compression": "gzip",
            "result_compression": "gzip",
            "task_always_eager": False,
            "worker_max_tasks_per_child": 1000,  # Prevent memory leaks
            "broker_connection_retry_on_startup": True,
        }

    # CORS Configuration - Production domains only
    CORS_ORIGINS = [
        "https://aqua-element.uz",
        "https://www.aqua-element.uz",
        "https://admin.aqua-element.uz",
        "https://api.aqua-element.uz",
    ]

    # Cache Configuration
    @property
    def CACHE_REDIS_URL(self):
        return self.REDIS_URL

    CACHE_DEFAULT_TIMEOUT = 600  # Longer cache in production

    # JWT Configuration
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)  # Shorter tokens in production
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    JWT_COOKIE_SECURE = True  # Force HTTPS
    JWT_COOKIE_DOMAIN = ".aqua-element.uz"  # Allow cookies across subdomains
    JWT_COOKIE_PATH = "/"  # Make cookies available to entire site
    # UI-001: Strict in production for defense-in-depth against cross-site
    # navigation attacks on the admin panel. CSRF double-submit still covers
    # same-site CSRF. Keep base `Lax` for dev to support cross-port flows.
    JWT_COOKIE_SAMESITE = "Strict"

    # Session Configuration - Maximum security
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"  # Lax is safer for navigation and prevents session dropping
    SESSION_COOKIE_NAME = "__Secure-session"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=4)

    # Payment Gateway Configuration - Live mode
    PAYME_TEST_MODE = False
    PAYME_ENDPOINT_URL = "https://checkout.paycom.uz/api"
    CLICK_TEST_MODE = False
    CLICK_ENDPOINT_URL = "https://api.click.uz/v2/merchant"

    # Email Configuration
    MAIL_BACKEND = "smtp"
    MAIL_SUBJECT_PREFIX = ""

    # File Storage - S3 production bucket
    STORAGE_TYPE = "local"
    AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET", "bluestream-production")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads/")

    # Enhanced security for file uploads
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf"}  # Restricted set
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB limit

    # Logging Configuration
    LOG_LEVEL = "INFO"
    LOG_FILE = "logs/production.log"

    # Error tracking - Optional in production
    @classmethod
    def SENTRY_DSN(self):
        return os.environ.get("SENTRY_DSN")

    SENTRY_ENVIRONMENT = "production"
    SENTRY_RELEASE = os.environ.get("SENTRY_RELEASE", "unknown")

    # Monitoring Configuration
    METRICS_ENABLED = True

    # Security Configuration - Maximum security
    PASSWORD_MIN_LENGTH = 10  # Longer passwords in production
    MAX_LOGIN_ATTEMPTS = 3  # Fewer attempts allowed
    LOCKOUT_DURATION = 3600  # 1 hour lockout

    # Two-factor authentication
    TWO_FACTOR_REQUIRED = os.environ.get("TWO_FACTOR_REQUIRED", "False").lower() == "true"

    # Swagger Configuration - Disabled in production
    SWAGGER_UI_ENABLED = False

    # Content Security Policy - Strict
    CONTENT_SECURITY_POLICY = {
        "default-src": ["'self'"],
        "script-src": ["'self'", "'unsafe-inline'", "https://unpkg.com"],  # Allow inline scripts + Leaflet
        "style-src": ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com", "https://unpkg.com"],  # Leaflet CSS
        "font-src": ["'self'", "https://fonts.gstatic.com"],
        "img-src": ["'self'", "data:", "https://aqua-element.uz", "https:"],
        "connect-src": [
            "'self'",
            "https://api.aqua-element.uz",
            "https://aqua-element.uz",
            "wss://aqua-element.uz",
            "wss://api.aqua-element.uz",
        ],  # Allow API and WebSocket connections
        "frame-src": ["'self'", "https://www.youtube.com", "https://www.youtube-nocookie.com"],  # Allow YouTube embeds
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
        "upgrade-insecure-requests": [],
    }

    # Security Headers - Maximum security
    SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    }

    # Content Security Policy - Production settings
    CSP_REPORT_ONLY = False  # Enforce CSP in production
    CSP_SOURCES = {
        "script-src": ["'self'", "'unsafe-inline'", "https://unpkg.com"],  # Allow inline scripts + Leaflet
        "style-src": ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com", "https://unpkg.com"],  # Leaflet CSS
        "img-src": ["'self'", "data:", "https:"],
        "connect-src": [
            "'self'",
            "https://api.aqua-element.uz",
            "https://aqua-element.uz",
            "wss://aqua-element.uz",
            "wss://api.aqua-element.uz",
        ],  # Allow API connections
        "font-src": ["'self'", "https://fonts.gstatic.com"],
        "media-src": ["'self'"],
        "object-src": ["'none'"],
        "frame-src": ["'self'", "https://www.youtube.com", "https://www.youtube-nocookie.com"],  # Allow YouTube embeds
    }

    # Feature flags for production
    FEATURE_FLAGS = {
        "maintenance_mode": os.environ.get("MAINTENANCE_MODE", "False").lower() == "true",
        "new_user_registration": os.environ.get("ALLOW_REGISTRATION", "True").lower() == "true",
        "payment_processing": os.environ.get("ALLOW_PAYMENTS", "True").lower() == "true",
    }

    @classmethod
    def validate_debug_mode(cls):
        """Ensure debug mode is disabled in production"""
        if cls.DEBUG:
            raise ValueError("DEBUG mode must be disabled in production environment")

    @classmethod
    def validate_production_settings(cls):
        """Validate production-specific settings"""
        # Security validations
        if not cls.SESSION_COOKIE_SECURE:
            raise ValueError("SESSION_COOKIE_SECURE must be True in production")
        if not cls.JWT_COOKIE_SECURE:
            raise ValueError("JWT_COOKIE_SECURE must be True in production")
        if cls.PAYME_TEST_MODE:
            raise ValueError("PAYME_TEST_MODE must be False in production")
        if cls.CLICK_TEST_MODE:
            raise ValueError("CLICK_TEST_MODE must be False in production")

        # Required services validations
        required_services = [
            "REDIS_URL",
            "SENTRY_DSN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "SENDGRID_API_KEY",
        ]

        missing_services = []
        for service in required_services:
            if not os.environ.get(service):
                missing_services.append(service)

        if missing_services:
            raise ValueError(f"Missing required production services: {', '.join(missing_services)}")

    @classmethod
    def validate_required_env_vars(cls):
        """Validate all required environment variables for production"""
        # ARCH-010: DATABASE_URL (not DB_PASSWORD-assembled fallback) + REDIS_URL
        # + both independent secret keys are MUST-HAVE to boot production.
        required_vars = [
            "SECRET_KEY",
            "JWT_SECRET_KEY",
            "DATABASE_URL",
            "REDIS_URL",
            "SENTRY_DSN",
            "SENDGRID_API_KEY",
        ]

        missing_vars = [v for v in required_vars if not os.environ.get(v)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

    @classmethod
    def validate_production_secrets(cls):
        """ARCH-010: extra entropy / distinctness checks that only apply in production."""
        secret_key = os.environ.get("SECRET_KEY") or ""
        jwt_secret = os.environ.get("JWT_SECRET_KEY") or ""

        if len(secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters in production")
        if len(jwt_secret) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters in production")
        if cls._secret_is_weak(secret_key):
            raise ValueError("SECRET_KEY is a known-weak placeholder; generate a fresh random secret")
        if cls._secret_is_weak(jwt_secret):
            raise ValueError("JWT_SECRET_KEY is a known-weak placeholder; generate a fresh random secret")
        if secret_key == jwt_secret:
            raise ValueError("JWT_SECRET_KEY must differ from SECRET_KEY in production (no shared-secret collapse)")

    @classmethod
    def init_app(cls, app):
        """Initialize production-specific configuration"""
        super().init_app(app)
        cls.validate_production_secrets()
        cls.validate_production_settings()

        # Initialize Sentry for error tracking
        try:
            import sentry_sdk
            from sentry_sdk.integrations.flask import FlaskIntegration
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
            from sentry_sdk.integrations.redis import RedisIntegration
            from sentry_sdk.integrations.celery import CeleryIntegration

            from business_app.utils.sentry import before_send as _sentry_before_send

            sentry_sdk.init(
                dsn=cls.SENTRY_DSN(),
                integrations=[
                    FlaskIntegration(),
                    SqlalchemyIntegration(),
                    RedisIntegration(),
                    CeleryIntegration(),
                ],
                environment=cls.SENTRY_ENVIRONMENT,
                release=cls.SENTRY_RELEASE,
                traces_sample_rate=0.05,
                profiles_sample_rate=0.01,
                debug=False,
                attach_stacktrace=True,
                send_default_pii=False,
                before_send=_sentry_before_send,
            )
        except ImportError:
            raise ImportError("Sentry SDK is required in production environment")

        # Add security headers middleware
        @app.after_request
        def add_security_headers(response):
            for header, value in cls.SECURITY_HEADERS.items():
                response.headers[header] = value
            return response

        # Initialize Flask-Talisman for enhanced security
        try:
            from flask_talisman import Talisman

            Talisman(
                app,
                force_https=False,  # Disabled - nginx handles SSL termination for external traffic
                strict_transport_security=True,
                strict_transport_security_max_age=63072000,
                strict_transport_security_include_subdomains=True,
                strict_transport_security_preload=True,
                content_security_policy=cls.CONTENT_SECURITY_POLICY,
                referrer_policy="strict-origin-when-cross-origin",
                feature_policy=cls.SECURITY_HEADERS.get("Permissions-Policy", ""),
                # Removed content_security_policy_nonce_in to allow 'unsafe-inline' to work
            )
        except ImportError:
            raise ImportError("Flask-Talisman is required in production environment")

        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

        # Note: Logging is configured in business_app.utils.logging_config.setup_enhanced_logging()
        # which is called from setup_logging() in __init__.py
        # This ensures consistent logging across all environments with proper console output for Docker

        # Note: Health check endpoint is defined in the main app factory
