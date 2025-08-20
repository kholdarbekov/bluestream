"""
Base configuration settings for the Water Business Platform
"""
import os
from datetime import timedelta
from typing import Optional

# Import secrets manager for secure secret retrieval
# Temporarily use fallback for development/testing
use_fallback = os.environ.get('USE_SECRETS_FALLBACK', 'true').lower() == 'true'

if not use_fallback:
    try:
        from shared.secrets_manager import get_secret, get_database_url, get_redis_url
    except ImportError:
        use_fallback = True

if use_fallback:
    # Fallback if shared module is not available or disabled
    def get_secret(secret_name: str, env_var: str = None, default: str = None, required: bool = True):
        """Fallback secret getter that uses environment variables"""
        value = os.environ.get(env_var or secret_name.upper())
        if not value and default:
            value = default
        if not value and required:
            raise ValueError(f"Required secret '{secret_name}' not found")
        return value
    
    def get_database_url():
        """Fallback database URL builder"""
        host = os.environ.get('POSTGRES_HOST', 'localhost')
        port = os.environ.get('POSTGRES_PORT', '5432')
        database = os.environ.get('POSTGRES_DB', 'bluestream_db')
        user = os.environ.get('POSTGRES_USER', 'postgres')
        password = os.environ.get('POSTGRES_PASSWORD', 'postgres')
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    
    def get_redis_url():
        """Fallback Redis URL builder"""
        host = os.environ.get('REDIS_HOST', 'localhost')
        port = os.environ.get('REDIS_PORT', '6379')
        db = os.environ.get('REDIS_DB', '0')
        password = os.environ.get('REDIS_PASSWORD')
        if password:
            return f"redis://:{password}@{host}:{port}/{db}"
        else:
            return f"redis://{host}:{port}/{db}"


class BaseConfig:
    """Base configuration class with common settings"""
    
    # Basic Flask Configuration
    @property
    def SECRET_KEY(self):
        return get_secret('secret_key', 'SECRET_KEY', required=True)
    
    @classmethod
    def validate_secret_key(cls):
        """Validate that SECRET_KEY is properly configured"""
        secret_key = get_secret('secret_key', 'SECRET_KEY', required=True)
        if not secret_key:
            raise ValueError("SECRET_KEY environment variable is required")
        if len(secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long")
        if secret_key in ['dev-secret-key-change-in-production', 'your-secret-key-here']:
            raise ValueError("Default SECRET_KEY must be changed")
    
    TESTING = False
    
    # Database Configuration
    @property
    def SQLALCHEMY_DATABASE_URI(self):
        return get_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_size': 20,
        'pool_timeout': 20,
        'pool_recycle': -1,
        'max_overflow': 0,
        'pool_pre_ping': True  # Validate connections before use
    }
    
    # JWT Configuration
    @property
    def JWT_SECRET_KEY(self):
        return get_secret('secret_key', 'JWT_SECRET_KEY', default=self.SECRET_KEY, required=True)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    JWT_ALGORITHM = 'HS256'
    JWT_CSRF_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE']  # Enable CSRF for state-changing methods
    JWT_COOKIE_CSRF_PROTECT = True
    
    # Support both cookies and headers for JWT tokens
    JWT_TOKEN_LOCATION = ['headers', 'cookies']
    JWT_ACCESS_COOKIE_NAME = 'access_token_cookie'
    JWT_REFRESH_COOKIE_NAME = 'refresh_token_cookie'
    JWT_COOKIE_HTTPONLY = True  # Prevent JavaScript access to cookies
    JWT_COOKIE_SAMESITE = 'Lax'
    
    # File Storage Configuration
    STORAGE_TYPE = os.environ.get('STORAGE_TYPE', 'local')  # 'local' or 's3'
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads/')
    MAX_CONTENT_LENGTH = int(os.environ.get('MAX_UPLOAD_SIZE', 16 * 1024 * 1024))  # 16MB
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}
    
    # AWS S3 Configuration
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = get_secret('aws_secret_access_key', 'AWS_SECRET_ACCESS_KEY', required=False)
    AWS_S3_BUCKET = os.environ.get('AWS_S3_BUCKET')
    AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
    
    # Telegram Bot Configuration
    @property
    def TELEGRAM_BOT_TOKEN(self):
        return get_secret('telegram_bot_token', 'TELEGRAM_BOT_TOKEN', required=True)
    WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
    TELEGRAM_ADMIN_CHAT_ID = os.environ.get('TELEGRAM_ADMIN_CHAT_ID')
    
    # Maps Configuration
    MAPS_PROVIDER = os.environ.get('MAPS_PROVIDER', 'google')  # 'google', 'yandex', 'osm'
    GOOGLE_MAPS_API_KEY = get_secret('google_maps_api_key', 'GOOGLE_MAPS_API_KEY', required=False)
    YANDEX_MAPS_API_KEY = get_secret('yandex_maps_api_key', 'YANDEX_MAPS_API_KEY', required=False)
    
    # Email Configuration
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.sendgrid.net')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', 'apikey')
    MAIL_PASSWORD = get_secret('sendgrid_api_key', 'SENDGRID_API_KEY', required=False)
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@bluestream.uz')
    MAIL_SUPPORT_EMAIL = os.environ.get('MAIL_SUPPORT_EMAIL', 'support@bluestream.uz')
    
    # SMS Configuration
    TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
    TWILIO_AUTH_TOKEN = get_secret('twilio_auth_token', 'TWILIO_AUTH_TOKEN', required=False)
    TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER')
    
    # Business Configuration
    COMPANY_NAME = os.environ.get('COMPANY_NAME', 'BlueStream Water Delivery')
    COMPANY_PHONE = os.environ.get('COMPANY_PHONE', '+998901234567')
    COMPANY_EMAIL = os.environ.get('COMPANY_EMAIL', 'info@bluestream.uz')
    COMPANY_ADDRESS = os.environ.get('COMPANY_ADDRESS', 'Tashkent, Uzbekistan')
    COMPANY_WEBSITE = os.environ.get('COMPANY_WEBSITE', 'https://bluestream.uz')
    
    # Delivery Configuration
    DEFAULT_DELIVERY_FEE = int(os.environ.get('DEFAULT_DELIVERY_FEE', 5000))  # UZS
    FREE_DELIVERY_THRESHOLD = int(os.environ.get('FREE_DELIVERY_THRESHOLD', 50000))  # UZS
    DELIVERY_RADIUS_KM = int(os.environ.get('DELIVERY_RADIUS_KM', 20))
    MAX_DELIVERY_TIME_HOURS = int(os.environ.get('MAX_DELIVERY_TIME_HOURS', 24))
    MIN_ORDER_AMOUNT = int(os.environ.get('MIN_ORDER_AMOUNT', 10000))  # UZS
    
    # Loyalty Program Configuration
    LOYALTY_POINTS_RATIO = int(os.environ.get('LOYALTY_POINTS_RATIO', 100))  # 1 point per 100 UZS
    LOYALTY_REDEMPTION_RATIO = int(os.environ.get('LOYALTY_REDEMPTION_RATIO', 1))  # 1 point = 1 UZS
    REFERRAL_BONUS_POINTS = int(os.environ.get('REFERRAL_BONUS_POINTS', 500))
    
    # Subscription Configuration
    SUBSCRIPTION_TRIAL_DAYS = int(os.environ.get('SUBSCRIPTION_TRIAL_DAYS', 7))
    SUBSCRIPTION_BILLING_DAY = int(os.environ.get('SUBSCRIPTION_BILLING_DAY', 1))
    MAX_SUBSCRIPTION_ITEMS = int(os.environ.get('MAX_SUBSCRIPTION_ITEMS', 10))
    
    # Security Configuration
    PASSWORD_MIN_LENGTH = int(os.environ.get('PASSWORD_MIN_LENGTH', 8))
    MAX_LOGIN_ATTEMPTS = int(os.environ.get('MAX_LOGIN_ATTEMPTS', 5))
    LOCKOUT_DURATION = int(os.environ.get('LOCKOUT_DURATION', 1800))  # 30 minutes
    
    # CSRF Protection Configuration
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = int(os.environ.get('CSRF_TIME_LIMIT', 3600))  # 1 hour
    WTF_CSRF_SSL_STRICT = os.environ.get('CSRF_SSL_STRICT', 'True').lower() == 'true'
    @property
    def WTF_CSRF_SECRET_KEY(self):
        return get_secret('secret_key', 'CSRF_SECRET_KEY', default=self.SECRET_KEY, required=False)
    WTF_CSRF_CHECK_DEFAULT = False  # Manual CSRF checking for API endpoints
    WTF_CSRF_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE']
    
    # Password Security Configuration
    BCRYPT_ROUNDS = int(os.environ.get('BCRYPT_ROUNDS', 12))  # Default 12 rounds for production
    PASSWORD_REHASH_ON_LOGIN = os.environ.get('PASSWORD_REHASH_ON_LOGIN', 'True').lower() == 'true'
    
    # API Configuration
    API_PREFIX = '/api/v1'
    API_DOCS_URL = '/docs'
    SWAGGER = {
        'title': 'BlueStream Water Platform API',
        'uiversion': 3,
        'version': '1.0.0',
        'description': '''
# BlueStream Water Delivery Platform API

Complete REST API for the BlueStream water delivery platform providing endpoints for:

- **Authentication & Authorization**: User registration, login, JWT token management
- **Product Management**: Browse products, categories, pricing, availability  
- **Order Processing**: Create orders, track status, order history
- **Payment Integration**: Multiple payment methods, refunds, transactions
- **Delivery Management**: Schedule deliveries, track drivers, delivery zones
- **User Management**: Profile management, addresses, preferences
- **Loyalty Program**: Points, rewards, referrals
- **Notifications**: Real-time updates and alerts

## Authentication
JWT Bearer tokens required for authenticated endpoints. Include in Authorization header:
```
Authorization: Bearer <your_jwt_token>
```

## Rate Limiting
- Authentication: 10-20 requests/hour
- General API: 1000 requests/hour  
- Admin endpoints: 500 requests/hour

## Error Format
All responses follow consistent format with `success`, `message`, `data`/`errors` fields.

For complete documentation, examples, and SDKs visit: [API Documentation](/docs/api)
        ''',
        'termsOfService': 'https://bluestream.uz/terms',
        'contact': {
            'name': 'BlueStream API Support',
            'url': 'https://bluestream.uz/support',
            'email': 'api-support@bluestream.uz',
        },
        'license': {
            'name': 'Proprietary',
            'url': 'https://bluestream.uz/license'
        },
        'externalDocs': {
            'description': 'Complete API Documentation',
            'url': '/docs/api'
        }
    }
    
    # Caching Configuration
    CACHE_TYPE = 'redis'
    CACHE_DEFAULT_TIMEOUT = 300
    
    # Redis Configuration
    @property
    def REDIS_URL(self):
        return get_redis_url()
    
    @property
    def CELERY_BROKER_URL(self):
        return get_redis_url()
    
    @property
    def CELERY_RESULT_BACKEND(self):
        return get_redis_url()
    
    @property
    def CACHE_REDIS_URL(self):
        return get_redis_url()
    
    # Logging Configuration
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = os.environ.get('LOG_FILE', 'logs/app.log')
    LOG_MAX_BYTES = int(os.environ.get('LOG_MAX_BYTES', 10485760))  # 10MB
    LOG_BACKUP_COUNT = int(os.environ.get('LOG_BACKUP_COUNT', 5))
    
    # Language Configuration
    LANGUAGES = {
        'en': 'English',
        'uz': 'O\'zbek',
        'ru': 'Русский'
    }
    DEFAULT_LANGUAGE = 'en'
    BABEL_DEFAULT_LOCALE = 'en'
    BABEL_DEFAULT_TIMEZONE = 'Asia/Tashkent'
    
    # Timezone Configuration
    # All internal operations use UTC, display uses local timezone
    USE_TZ = True
    TIMEZONE = 'UTC'  # Internal storage timezone
    DISPLAY_TIMEZONE = os.environ.get('DISPLAY_TIMEZONE', 'Asia/Tashkent')  # User display timezone
    ALLOWED_TIMEZONES = [
        'UTC',
        'Asia/Tashkent',
        'Asia/Almaty',
        'Asia/Bishkek',
        'Asia/Dushanbe',
        'Asia/Ashgabat',
        'Europe/Moscow'
    ]
    
    # Date/Time formatting
    DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S %Z'
    DATE_FORMAT = '%Y-%m-%d'
    TIME_FORMAT = '%H:%M:%S'
    ISO_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'
    
    # Health Check Configuration
    HEALTH_CHECK_ENABLED = True
    HEALTH_CHECK_ENDPOINT = '/health'
    
    # Monitoring Configuration
    METRICS_ENABLED = os.environ.get('METRICS_ENABLED', 'False').lower() == 'true'
    METRICS_ENDPOINT = '/metrics'
    
    # Content Security Policy settings
    CSP_REPORT_ONLY = os.environ.get('CSP_REPORT_ONLY', 'False').lower() == 'true'
    CSP_REPORT_URI = '/csp-report'
    CSP_SOURCES = {
        # Additional CSP sources can be configured here
        'script-src': [],
        'style-src': [],
        'img-src': [],
        'connect-src': [],
        'font-src': [],
        'media-src': [],
        'object-src': [],
        'frame-src': []
    }
    
    @classmethod
    def validate_required_env_vars(cls):
        """Validate that required environment variables and secrets are set"""
        required_secrets = [
            ('secret_key', 'SECRET_KEY'),
            ('postgres_password', 'POSTGRES_PASSWORD'),
            ('telegram_bot_token', 'TELEGRAM_BOT_TOKEN'),
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
            raise ValueError(f"Missing required secrets: {', '.join(missing_secrets)}")
    
    @classmethod
    def validate_debug_mode(cls):
        """Validate debug mode is properly configured"""
        # Base implementation - can be overridden in subclasses
        pass
    
    @classmethod
    def init_app(cls, app):
        """Initialize application with this configuration"""
        cls.validate_required_env_vars()
        cls.validate_secret_key()
        cls.validate_debug_mode()
        
        # Create necessary directories
        import pathlib
        log_dir = pathlib.Path(cls.LOG_FILE).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        
        upload_dir = pathlib.Path(cls.UPLOAD_FOLDER)
        upload_dir.mkdir(parents=True, exist_ok=True)