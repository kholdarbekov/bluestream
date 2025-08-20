"""
Staging environment configuration
"""
import os
from datetime import timedelta
from .base import BaseConfig


class StagingConfig(BaseConfig):
    """Staging configuration - mirrors production but with relaxed security for testing"""
    
    DEBUG = False
    
    # Database Configuration
    @property
    def SQLALCHEMY_DATABASE_URI(self):
        uri = os.environ.get('DATABASE_URL')
        if not uri:
            db_user = os.environ.get('DB_USER', 'postgres')
            db_password = os.environ.get('DB_PASSWORD')
            db_host = os.environ.get('DB_HOST', 'localhost')
            db_port = os.environ.get('DB_PORT', '5432')
            db_name = os.environ.get('DB_NAME', 'bluestream_staging')
            
            if not db_password:
                raise ValueError("DB_PASSWORD environment variable is required in staging")
            
            uri = f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
        return uri
    
    SQLALCHEMY_ECHO = False
    
    # Redis Configuration
    REDIS_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/1'
    
    # Rate Limiting Configuration
    RATELIMIT_STORAGE_URL = REDIS_URL
    RATELIMIT_DEFAULT = os.environ.get('RATE_LIMIT_REQUESTS', '200/hour')
    RATELIMIT_ENABLED = True
    
    # Celery Configuration
    CELERY = {
        'broker_url': os.environ.get('CELERY_BROKER_URL', REDIS_URL),
        'result_backend': os.environ.get('CELERY_RESULT_BACKEND', REDIS_URL),
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': 'Asia/Tashkent',
        'enable_utc': True,
        'task_track_started': True,
        'result_expires': 3600,
        'worker_prefetch_multiplier': 1,
        'task_acks_late': True,
        'worker_disable_rate_limits': False,
        'task_compression': 'gzip',
        'result_compression': 'gzip',
        'task_always_eager': False,
    }
    
    # CORS Configuration - Staging domains
    CORS_ORIGINS = [
        'https://staging.bluestream.uz',
        'https://admin-staging.bluestream.uz',
        'https://api-staging.bluestream.uz'
    ]
    
    # Cache Configuration
    CACHE_REDIS_URL = REDIS_URL
    
    # JWT Configuration
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)  # Shorter refresh in staging
    JWT_COOKIE_SECURE = True  # Use HTTPS in staging
    
    # Session Configuration
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Payment Gateway Configuration - Test mode
    PAYME_TEST_MODE = True
    PAYME_ENDPOINT_URL = 'https://checkout.test.paycom.uz'
    CLICK_TEST_MODE = True
    CLICK_ENDPOINT_URL = 'https://api.click.uz/v2/merchant'
    
    # Email Configuration - Real email but with staging templates
    MAIL_BACKEND = 'smtp'
    MAIL_SUBJECT_PREFIX = '[STAGING] '
    
    # File Storage - S3 with staging bucket
    STORAGE_TYPE = os.environ.get('STORAGE_TYPE', 's3')
    AWS_S3_BUCKET = os.environ.get('AWS_S3_BUCKET', 'bluestream-staging')
    UPLOAD_FOLDER = 'uploads/staging/'
    
    # Logging Configuration
    LOG_LEVEL = 'INFO'
    LOG_FILE = 'logs/staging.log'
    
    # Error tracking
    SENTRY_DSN = os.environ.get('SENTRY_DSN')
    SENTRY_ENVIRONMENT = 'staging'
    SENTRY_RELEASE = os.environ.get('SENTRY_RELEASE', 'staging')
    
    # Monitoring Configuration
    METRICS_ENABLED = True
    
    # Security Configuration - Production-like but more permissive for testing
    PASSWORD_MIN_LENGTH = 8
    MAX_LOGIN_ATTEMPTS = 10  # More attempts allowed in staging
    LOCKOUT_DURATION = 900  # 15 minutes
    
    # Swagger Configuration - Available but with authentication
    SWAGGER_UI_ENABLED = True
    SWAGGER_AUTH_REQUIRED = True
    
    # Content Security Policy - Relaxed for staging
    CONTENT_SECURITY_POLICY = {
        'default-src': ["'self'"],
        'script-src': ["'self'", "'unsafe-inline'", "'unsafe-eval'", "https://cdn.jsdelivr.net"],
        'style-src': ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
        'font-src': ["'self'", "https://fonts.gstatic.com"],
        'img-src': ["'self'", "data:", "https:"],
        'connect-src': ["'self'", "https://api-staging.bluestream.uz"],
    }
    
    # Security Headers
    SECURITY_HEADERS = {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'SAMEORIGIN',
        'X-XSS-Protection': '1; mode=block',
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'Referrer-Policy': 'strict-origin-when-cross-origin'
    }
    
    @classmethod
    def validate_debug_mode(cls):
        """Ensure debug mode is disabled in staging"""
        if cls.DEBUG:
            raise ValueError("DEBUG mode must be disabled in staging environment")
    
    @classmethod
    def validate_staging_settings(cls):
        """Validate staging-specific settings"""
        if not cls.SESSION_COOKIE_SECURE:
            raise ValueError("SESSION_COOKIE_SECURE must be True in staging")
        if not cls.JWT_COOKIE_SECURE:
            raise ValueError("JWT_COOKIE_SECURE must be True in staging")
        if cls.PAYME_TEST_MODE is False:
            raise ValueError("Payment gateways should be in test mode in staging")
    
    @classmethod
    def init_app(cls, app):
        """Initialize staging-specific configuration"""
        super().init_app(app)
        cls.validate_staging_settings()
        
        # Initialize Sentry for error tracking
        if cls.SENTRY_DSN:
            try:
                import sentry_sdk
                from sentry_sdk.integrations.flask import FlaskIntegration
                from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
                from sentry_sdk.integrations.redis import RedisIntegration
                
                sentry_sdk.init(
                    dsn=cls.SENTRY_DSN,
                    integrations=[
                        FlaskIntegration(),
                        SqlalchemyIntegration(),
                        RedisIntegration(),
                    ],
                    environment=cls.SENTRY_ENVIRONMENT,
                    release=cls.SENTRY_RELEASE,
                    traces_sample_rate=0.1,  # Lower sample rate in staging
                    debug=False
                )
            except ImportError:
                app.logger.warning("Sentry SDK not installed, skipping error tracking initialization")
        
        # Add security headers middleware
        @app.after_request
        def add_security_headers(response):
            for header, value in cls.SECURITY_HEADERS.items():
                response.headers[header] = value
            return response
        
        # Initialize Flask-Talisman for CSP
        try:
            from flask_talisman import Talisman
            Talisman(
                app,
                force_https=True,
                strict_transport_security=True,
                content_security_policy=cls.CONTENT_SECURITY_POLICY
            )
        except ImportError:
            app.logger.warning("Flask-Talisman not installed, skipping CSP initialization")