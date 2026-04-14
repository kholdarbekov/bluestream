"""
Development environment configuration
"""
import os
from datetime import timedelta
from .base import BaseConfig
from shared.constants import DISPLAY_TIMEZONE


class DevelopmentConfig(BaseConfig):
    """Development configuration"""
    
    DEBUG = True
    
    # Database Configuration
    @property
    def SQLALCHEMY_DATABASE_URI(self):
        uri = os.environ.get('DATABASE_URL')
        if not uri:
            db_user = os.environ.get('DB_USER', 'postgres')
            db_password = os.environ.get('DB_PASSWORD', 'postgres')
            db_host = os.environ.get('DB_HOST', 'localhost')
            db_port = os.environ.get('DB_PORT', '5432')
            db_name = os.environ.get('DB_NAME', 'bluestream_dev')
            
            uri = f'postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}'
        return uri
    
    SQLALCHEMY_ECHO = False  # Log all SQL statements
    
    # Redis Configuration
    REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
    
    # Rate Limiting Configuration - More lenient for development
    RATELIMIT_STORAGE_URL = REDIS_URL
    RATELIMIT_DEFAULT = os.environ.get('RATE_LIMIT_REQUESTS', '1000/hour')
    RATELIMIT_ENABLED = True
    
    # Celery Configuration
    CELERY = {
        'broker_url': os.environ.get('CELERY_BROKER_URL', REDIS_URL),
        'result_backend': os.environ.get('CELERY_RESULT_BACKEND', REDIS_URL),
        'task_serializer': 'json',
        'accept_content': ['json'],
        'result_serializer': 'json',
        'timezone': DISPLAY_TIMEZONE,
        'enable_utc': True,
        'task_track_started': True,
        'result_expires': 3600,
        'worker_prefetch_multiplier': 1,
        'task_acks_late': True,
        'worker_disable_rate_limits': False,
        'task_compression': 'gzip',
        'result_compression': 'gzip',
        'task_always_eager': True,  # Run tasks synchronously in development
    }
    
    # CORS Configuration - Allow local development
    CORS_ORIGINS = [
        'https://aqua-element.uz',
        'https://www.aqua-element.uz',
        'https://admin.aqua-element.uz',
        'https://api.aqua-element.uz',
        'http://localhost:3000',
        'http://localhost:3001',
        'http://127.0.0.1:3000',
        'http://127.0.0.1:3001',
    ]
    
    # Cache Configuration
    CACHE_REDIS_URL = REDIS_URL
    
    # JWT Configuration - Longer tokens for development convenience
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=24)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    JWT_COOKIE_SECURE = False  # Allow HTTP in development
    
    # Session Configuration
    SESSION_COOKIE_SECURE = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Payment Gateway Configuration - Test mode
    PAYME_TEST_MODE = True
    PAYME_ENDPOINT_URL = 'https://checkout.paycom.uz/api'
    CLICK_TEST_MODE = True
    CLICK_ENDPOINT_URL = 'https://api.click.uz/v2/merchant'
    
    # Email Configuration - Use file backend for development
    MAIL_BACKEND = 'file'
    MAIL_FILE_PATH = 'logs/emails'
    
    # File Storage - Local storage for development
    STORAGE_TYPE = 'local'
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads/dev/')
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')
    
    # Logging Configuration
    LOG_LEVEL = 'DEBUG'
    LOG_FILE = 'logs/dev.log'
    
    # Development-specific features
    FLASK_PROFILER = {
        'enabled': os.environ.get('FLASK_PROFILER_ENABLED', 'False').lower() == 'true',
        'storage': {
            'engine': 'sqlite',
            'SQLITE_PATH': 'logs/profiler.sql'
        },
        'basicAuth': {
            'enabled': True,
            'username': 'admin',
            'password': 'admin'
        },
        'ignore': [
            '^/static/.*',
            '^/health$',
            '^/metrics$'
        ]
    }
    
    # Debug Toolbar Configuration
    DEBUG_TB_ENABLED = os.environ.get('DEBUG_TB_ENABLED', 'False').lower() == 'true'
    DEBUG_TB_INTERCEPT_REDIRECTS = False
    DEBUG_TB_PROFILER_ENABLED = True
    
    # Swagger Configuration - Show all endpoints in development
    SWAGGER_UI_ENABLED = True
    
    # Content Security Policy - Development settings (more permissive)
    CSP_REPORT_ONLY = True  # Use report-only mode in development
    CSP_SOURCES = {
        'script-src': ["'self'", "'unsafe-inline'", "'unsafe-eval'", 'localhost:*', '127.0.0.1:*'],
        'style-src': ["'self'", "'unsafe-inline'", 'fonts.googleapis.com'],
        'img-src': ["'self'", 'data:', 'https:', 'http:', 'blob:'],
        'connect-src': ["'self'", 'ws:', 'wss:', 'localhost:*', '127.0.0.1:*'],
        'font-src': ["'self'", 'fonts.gstatic.com', 'data:'],
        'media-src': ["'self'"],
        'object-src': ["'none'"],
        'frame-src': ["'self'", 'localhost:*']
    }
    
    @classmethod
    def validate_debug_mode(cls):
        """Development allows debug mode"""
        pass
    
    @classmethod
    def validate_required_env_vars(cls):
        """Override to make some secrets optional in development"""
        from .base import get_secret
        
        # Only require SECRET_KEY in development
        required_secrets = [
            ('secret_key', 'SECRET_KEY'),
        ]
        
        missing_secrets = []
        for secret_name, env_var in required_secrets:
            try:
                value = get_secret(secret_name, env_var, required=True)
                if not value:
                    missing_secrets.append(f"{secret_name}/{env_var}")
            except ValueError:
                missing_secrets.append(f"{secret_name}/{env_var}")
        
        if missing_secrets:
            raise ValueError(f"Missing required secrets in development: {', '.join(missing_secrets)}")
    
    @classmethod
    def init_app(cls, app):
        """Initialize development-specific configuration"""
        super().init_app(app)
        
        # Create development-specific directories
        import pathlib
        
        pathlib.Path('logs').mkdir(exist_ok=True)
        pathlib.Path(app.config.get('UPLOAD_FOLDER', cls.UPLOAD_FOLDER)).mkdir(parents=True, exist_ok=True)
        pathlib.Path('logs/emails').mkdir(parents=True, exist_ok=True)
        
        if cls.MAIL_BACKEND == 'file':
            pathlib.Path(cls.MAIL_FILE_PATH).mkdir(parents=True, exist_ok=True)
        
        # Initialize Flask-DebugToolbar if enabled
        if cls.DEBUG_TB_ENABLED:
            try:
                from flask_debugtoolbar import DebugToolbarExtension
                DebugToolbarExtension(app)
            except ImportError:
                app.logger.warning("Flask-DebugToolbar not installed, skipping initialization")
        
        # Initialize Flask-Profiler if enabled
        if cls.FLASK_PROFILER.get('enabled'):
            try:
                from flask_profiler import Profiler
                Profiler(app)
            except ImportError:
                app.logger.warning("Flask-Profiler not installed, skipping initialization")
