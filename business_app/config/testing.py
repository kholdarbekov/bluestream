"""
Testing environment configuration
"""

import os
from datetime import timedelta
from .base import BaseConfig


class TestingConfig(BaseConfig):
    """Testing configuration for automated tests"""

    TESTING = True
    DEBUG = False  # Keep false to test production-like behavior

    # Database Configuration - In-memory SQLite for speed
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = False  # Disable SQL logging in tests

    # Override engine options for testing
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": False,
        "pool_recycle": -1,
    }

    # Redis Configuration - Use separate test database
    REDIS_URL = os.environ.get("REDIS_TEST_URL", "redis://localhost:6379/15")

    # Disable rate limiting for tests
    RATELIMIT_ENABLED = False
    RATELIMIT_STORAGE_URL = REDIS_URL
    RATELIMIT_DEFAULT = "10000/hour"

    # Celery Configuration - Synchronous execution for testing
    CELERY = {
        "task_always_eager": True,  # Execute tasks synchronously
        "task_eager_propagates": True,  # Propagate exceptions
        "broker_url": "memory://",
        "result_backend": "cache+memory://",
    }

    # CORS Configuration - Allow all for testing
    CORS_ORIGINS = ["*"]

    # Cache Configuration - Use simple cache for testing
    CACHE_TYPE = "simple"
    CACHE_DEFAULT_TIMEOUT = 1  # Short timeout for testing

    # JWT Configuration - Short-lived tokens for testing
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(seconds=10)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(seconds=30)
    JWT_COOKIE_SECURE = False  # Allow HTTP in tests

    # Session Configuration
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Payment Gateway Configuration - Test mode
    PAYME_TEST_MODE = True
    PAYME_ENDPOINT_URL = "https://test.paycom.uz"
    CLICK_TEST_MODE = True
    CLICK_ENDPOINT_URL = "https://test.click.uz"

    # Email Configuration - Use memory backend for testing
    MAIL_BACKEND = "memory"
    MAIL_SUPPRESS_SEND = True

    # File Storage - Local temporary storage for testing
    STORAGE_TYPE = "local"
    UPLOAD_FOLDER = "/tmp/bluestream_test_uploads/"
    MAX_CONTENT_LENGTH = 1024 * 1024  # 1MB for testing

    # Logging Configuration - Minimal logging for tests
    LOG_LEVEL = "ERROR"  # Only log errors in tests
    LOG_FILE = "/tmp/test.log"

    # Security Configuration - Relaxed for testing
    PASSWORD_MIN_LENGTH = 6
    MAX_LOGIN_ATTEMPTS = 10
    LOCKOUT_DURATION = 1  # 1 second for testing

    # Disable external services for testing
    SENTRY_DSN = None
    METRICS_ENABLED = False
    HEALTH_CHECK_ENABLED = False

    # Swagger Configuration - Disabled in testing
    SWAGGER_UI_ENABLED = False

    # Testing-specific configuration
    WTF_CSRF_ENABLED = False  # Disable CSRF for easier testing
    PRESERVE_CONTEXT_ON_EXCEPTION = False

    # Feature flags for testing
    FEATURE_FLAGS = {
        "maintenance_mode": False,
        "new_user_registration": True,
        "payment_processing": True,
    }

    # Test data configuration
    TEST_DATA = {
        "admin_email": "admin@test.com",
        "admin_password": "testpassword123",
        "customer_email": "customer@test.com",
        "customer_password": "testpassword123",
        "driver_email": "driver@test.com",
        "driver_password": "testpassword123",
    }

    @classmethod
    def validate_debug_mode(cls):
        """Testing allows any debug mode"""

    @classmethod
    def validate_required_env_vars(cls):
        """Override to not require env vars in testing"""
        # Only require SECRET_KEY for testing
        if not os.environ.get("SECRET_KEY"):
            # Use a default test secret key
            os.environ["SECRET_KEY"] = "test-secret-key-for-testing-32-chars-long"

    @classmethod
    def init_app(cls, app):
        """Initialize testing-specific configuration"""
        # Don't call super() to skip base validations

        # Create test directories
        import tempfile

        # Use temporary directory for uploads
        temp_dir = tempfile.mkdtemp(prefix="bluestream_test_")
        cls.UPLOAD_FOLDER = temp_dir

        # Configure test database
        if cls.SQLALCHEMY_DATABASE_URI == "sqlite:///:memory:":
            # Initialize in-memory database
            with app.app_context():
                from business_app import db

                db.create_all()

        # Setup test logging
        import logging

        app.logger.setLevel(logging.ERROR)

        # Disable all external HTTP requests during testing
        try:
            import responses

            responses.start()
        except ImportError:
            pass

        # Mock external services for testing
        cls._setup_test_mocks(app)

    @classmethod
    def _setup_test_mocks(cls, app):
        """Setup mocks for external services"""
        # Mock Redis if not available
        try:
            import redis

            r = redis.from_url(cls.REDIS_URL)
            r.ping()
        except (redis.ConnectionError, ConnectionRefusedError):
            # Use fake Redis for testing
            try:
                import fakeredis

                app.config["REDIS_CLIENT"] = fakeredis.FakeRedis()
            except ImportError:
                pass

        # Mock email sending
        @app.before_request
        def setup_test_context():
            from flask import g

            g.emails_sent = []

        # Mock SMS sending
        app.config["SMS_BACKEND"] = "memory"

        # Mock file storage
        app.config["FILE_STORAGE_BACKEND"] = "memory"
