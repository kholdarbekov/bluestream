"""
Configuration settings for the Staff Bot
"""
import os
from dataclasses import dataclass
from typing import List, Optional
from shared.constants import DISPLAY_TIMEZONE

# Import secrets manager for secure secret retrieval
# Temporarily use fallback for development/testing
use_fallback = os.environ.get('USE_SECRETS_FALLBACK', 'true').lower() == 'true'

if not use_fallback:
    try:
        from shared.secrets_manager import get_secret, get_database_url, get_redis_url
    except ImportError:
        use_fallback = True

if use_fallback:
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
        if os.environ.get('DATABASE_URL'):
            return os.environ.get('DATABASE_URL')
        host = os.environ.get('POSTGRES_HOST', 'postgres')
        port = os.environ.get('POSTGRES_PORT', '5432')
        database = os.environ.get('POSTGRES_DB', 'bluestream_db')
        user = os.environ.get('POSTGRES_USER', 'postgres')
        password = os.environ.get('POSTGRES_PASSWORD', 'postgres')
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

    def get_redis_url():
        """Fallback Redis URL builder"""
        if os.environ.get('REDIS_URL'):
            return os.environ.get('REDIS_URL')
        host = os.environ.get('REDIS_HOST', 'redis')
        port = os.environ.get('REDIS_PORT', '6379')
        db = os.environ.get('REDIS_DB', '2')  # Staff bot uses DB 2
        password = os.environ.get('REDIS_PASSWORD')
        if password:
            return f"redis://:{password}@{host}:{port}/{db}"
        else:
            return f"redis://{host}:{port}/{db}"


@dataclass
class TelegramConfig:
    """Telegram bot configuration"""
    bot_token: str
    webhook_url: Optional[str] = None
    webhook_port: int = 8443
    webhook_listen: str = "0.0.0.0"

    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 60
    rate_limit_window: int = 60  # seconds

    # Polling/network resilience settings
    polling_timeout: int = 30
    poll_interval: float = 0.0
    bootstrap_retries: int = -1
    drop_pending_updates: bool = True

    request_connection_pool_size: int = 8
    request_connect_timeout: float = 15.0
    request_read_timeout: float = 45.0
    request_write_timeout: float = 30.0
    request_pool_timeout: float = 10.0
    request_max_retries: int = 3
    request_retry_backoff_seconds: float = 0.75
    request_retry_max_backoff_seconds: float = 5.0

    get_updates_connection_pool_size: int = 4
    get_updates_connect_timeout: float = 20.0
    get_updates_read_timeout: float = 55.0
    get_updates_write_timeout: float = 30.0
    get_updates_pool_timeout: float = 10.0
    get_updates_max_retries: int = 3
    get_updates_retry_backoff_seconds: float = 0.75
    get_updates_retry_max_backoff_seconds: float = 5.0


@dataclass
class DatabaseConfig:
    """Database configuration"""
    url: str
    pool_size: int = 10
    max_overflow: int = 20
    pool_timeout: int = 30
    pool_recycle: int = 3600


@dataclass
class RedisConfig:
    """Redis configuration"""
    url: str
    encoding: str = "utf-8"
    decode_responses: bool = True
    socket_timeout: int = 5
    socket_connect_timeout: int = 5


@dataclass
class BusinessAPIConfig:
    """Business API configuration"""
    base_url: str
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0

    # SSL configuration
    ssl_verify: bool = True
    ssl_cert_path: Optional[str] = None

    # Staff API endpoints
    auth_endpoint: str = "/api/v1/staff/auth"
    delivery_endpoint: str = "/api/v1/staff/delivery"
    operator_endpoint: str = "/api/v1/staff/operator"


@dataclass
class LocalizationConfig:
    """Multi-language configuration"""
    default_language: str = "en"
    supported_languages: List[str] = None
    fallback_language: str = "en"

    def __post_init__(self):
        if self.supported_languages is None:
            self.supported_languages = ["en", "uz", "ru"]


@dataclass
class TimezoneConfig:
    """Timezone configuration"""
    default_timezone: str = DISPLAY_TIMEZONE
    display_format_uz: str = "%d.%m.%Y, %H:%M"
    display_format_ru: str = "%d.%m.%Y, %H:%M"
    display_format_en: str = "%m/%d/%Y, %I:%M %p"


@dataclass
class SecurityConfig:
    """Security configuration"""
    jwt_secret_key: str
    webhook_secret: Optional[str] = None
    jwt_expiry_hours: int = 24
    jwt_refresh_expiry_days: int = 30


@dataclass
class StaffConfig:
    """Staff-specific settings"""
    new_order_notification_enabled: bool = True
    location_update_interval_seconds: int = 30
    max_active_deliveries_per_person: int = 3
    order_pool_refresh_seconds: int = 60


@dataclass
class SentryConfig:
    """Sentry error tracking configuration"""
    dsn: Optional[str] = None
    environment: str = "development"
    traces_sample_rate: float = 1.0
    profiles_sample_rate: float = 1.0
    send_default_pii: bool = False
    debug: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)


class StaffBotConfig:
    """Main staff bot configuration"""

    def __init__(self):
        # Prefer new canonical name, fallback to legacy name.
        staff_bot_token = (
            os.environ.get('STAFF_BOT_TOKEN')
            or os.environ.get('STAFF_TELEGRAM_BOT_TOKEN')
            or get_secret('staff_bot_token', 'STAFF_BOT_TOKEN', required=False)
            or get_secret('staff_telegram_bot_token', 'STAFF_TELEGRAM_BOT_TOKEN', required=False)
        )

        self.telegram = TelegramConfig(
            bot_token=staff_bot_token,
            webhook_url=os.getenv('WEBHOOK_URL'),
            webhook_port=int(os.getenv('WEBHOOK_PORT', '8443')),
            polling_timeout=int(
                os.getenv(
                    'STAFF_TELEGRAM_POLLING_TIMEOUT',
                    os.getenv('TELEGRAM_POLLING_TIMEOUT', '30')
                )
            ),
            poll_interval=float(
                os.getenv(
                    'STAFF_TELEGRAM_POLL_INTERVAL',
                    os.getenv('TELEGRAM_POLL_INTERVAL', '0')
                )
            ),
            bootstrap_retries=int(
                os.getenv(
                    'STAFF_TELEGRAM_BOOTSTRAP_RETRIES',
                    os.getenv('TELEGRAM_BOOTSTRAP_RETRIES', '-1')
                )
            ),
            drop_pending_updates=os.getenv(
                'STAFF_TELEGRAM_DROP_PENDING_UPDATES',
                os.getenv('TELEGRAM_DROP_PENDING_UPDATES', 'true')
            ).lower() == 'true',
            request_connection_pool_size=int(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_CONNECTION_POOL_SIZE',
                    os.getenv('TELEGRAM_REQUEST_CONNECTION_POOL_SIZE', '8')
                )
            ),
            request_connect_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_CONNECT_TIMEOUT',
                    os.getenv('TELEGRAM_REQUEST_CONNECT_TIMEOUT', '15')
                )
            ),
            request_read_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_READ_TIMEOUT',
                    os.getenv('TELEGRAM_REQUEST_READ_TIMEOUT', '45')
                )
            ),
            request_write_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_WRITE_TIMEOUT',
                    os.getenv('TELEGRAM_REQUEST_WRITE_TIMEOUT', '30')
                )
            ),
            request_pool_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_POOL_TIMEOUT',
                    os.getenv('TELEGRAM_REQUEST_POOL_TIMEOUT', '10')
                )
            ),
            request_max_retries=int(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_MAX_RETRIES',
                    os.getenv('TELEGRAM_REQUEST_MAX_RETRIES', '3')
                )
            ),
            request_retry_backoff_seconds=float(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_RETRY_BACKOFF_SECONDS',
                    os.getenv('TELEGRAM_REQUEST_RETRY_BACKOFF_SECONDS', '0.75')
                )
            ),
            request_retry_max_backoff_seconds=float(
                os.getenv(
                    'STAFF_TELEGRAM_REQUEST_RETRY_MAX_BACKOFF_SECONDS',
                    os.getenv('TELEGRAM_REQUEST_RETRY_MAX_BACKOFF_SECONDS', '5')
                )
            ),
            get_updates_connection_pool_size=int(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE',
                    os.getenv('TELEGRAM_GET_UPDATES_CONNECTION_POOL_SIZE', '4')
                )
            ),
            get_updates_connect_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT',
                    os.getenv('TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT', '20')
                )
            ),
            get_updates_read_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_READ_TIMEOUT',
                    os.getenv('TELEGRAM_GET_UPDATES_READ_TIMEOUT', '55')
                )
            ),
            get_updates_write_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_WRITE_TIMEOUT',
                    os.getenv('TELEGRAM_GET_UPDATES_WRITE_TIMEOUT', '30')
                )
            ),
            get_updates_pool_timeout=float(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_POOL_TIMEOUT',
                    os.getenv('TELEGRAM_GET_UPDATES_POOL_TIMEOUT', '10')
                )
            ),
            get_updates_max_retries=int(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_MAX_RETRIES',
                    os.getenv('TELEGRAM_GET_UPDATES_MAX_RETRIES', '3')
                )
            ),
            get_updates_retry_backoff_seconds=float(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_RETRY_BACKOFF_SECONDS',
                    os.getenv('TELEGRAM_GET_UPDATES_RETRY_BACKOFF_SECONDS', '0.75')
                )
            ),
            get_updates_retry_max_backoff_seconds=float(
                os.getenv(
                    'STAFF_TELEGRAM_GET_UPDATES_RETRY_MAX_BACKOFF_SECONDS',
                    os.getenv('TELEGRAM_GET_UPDATES_RETRY_MAX_BACKOFF_SECONDS', '5')
                )
            ),
        )

        self.database = DatabaseConfig(
            url=get_database_url(),
            pool_size=int(os.getenv('DB_POOL_SIZE', '10')),
        )

        self.redis = RedisConfig(
            url=get_redis_url(),
        )

        self.business_api = BusinessAPIConfig(
            base_url=os.getenv('BUSINESS_APP_URL', 'http://business_app:80'),
            ssl_verify=os.getenv('BUSINESS_API_SSL_VERIFY', 'true').lower() == 'true',
            ssl_cert_path=os.getenv('BUSINESS_API_SSL_CERT_PATH'),
            timeout=int(os.getenv('BUSINESS_API_TIMEOUT', '30')),
            max_retries=int(os.getenv('BUSINESS_API_MAX_RETRIES', '3')),
        )

        self.localization = LocalizationConfig(
            default_language=os.getenv('DEFAULT_LANGUAGE', 'en'),
        )

        self.timezone = TimezoneConfig(
            default_timezone=os.getenv('DISPLAY_TIMEZONE', DISPLAY_TIMEZONE),
        )

        self.security = SecurityConfig(
            jwt_secret_key=os.environ.get('JWT_SECRET_KEY') or os.environ.get('SECRET_KEY'),
            webhook_secret=os.environ.get('WEBHOOK_SECRET'),
        )

        self.staff = StaffConfig(
            new_order_notification_enabled=os.getenv('STAFF_NEW_ORDER_NOTIFY', 'true').lower() == 'true',
            location_update_interval_seconds=int(os.getenv('STAFF_LOCATION_INTERVAL', '30')),
            max_active_deliveries_per_person=int(os.getenv('STAFF_MAX_ACTIVE_DELIVERIES', '3')),
            order_pool_refresh_seconds=int(os.getenv('STAFF_POOL_REFRESH', '60')),
        )

        self.sentry = SentryConfig(
            dsn=os.environ.get('SENTRY_DSN'),
            environment=os.environ.get('SENTRY_ENVIRONMENT', os.environ.get('FLASK_ENV', 'development')),
            traces_sample_rate=float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '1.0')),
            profiles_sample_rate=float(os.environ.get('SENTRY_PROFILES_SAMPLE_RATE', '1.0')),
            send_default_pii=os.environ.get('SENTRY_SEND_DEFAULT_PII', 'false').lower() == 'true',
            debug=os.environ.get('SENTRY_DEBUG', 'false').lower() == 'true',
        )

        self._validate_config()

    def _validate_config(self):
        """Validate required configuration values"""
        required_fields = [
            (self.telegram.bot_token, "STAFF_BOT_TOKEN (or legacy STAFF_TELEGRAM_BOT_TOKEN)"),
            (self.database.url, "DATABASE_URL"),
            (self.business_api.base_url, "BUSINESS_APP_URL"),
            (self.security.jwt_secret_key, "JWT_SECRET_KEY"),
        ]

        missing_fields = [field_name for field_value, field_name in required_fields if not field_value]

        if missing_fields:
            raise ValueError(f"Missing required configuration: {', '.join(missing_fields)}")

    def get_api_url(self, endpoint: str) -> str:
        """Get full API URL for endpoint"""
        return f"{self.business_api.base_url.rstrip('/')}{endpoint}"


# Global configuration instance
config = StaffBotConfig()
