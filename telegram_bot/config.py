"""
Configuration settings for the Telegram Bot
"""
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

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
        db = os.environ.get('REDIS_DB', '1')
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
    webhook_ssl_cert: Optional[str] = None
    webhook_ssl_priv: Optional[str] = None
    
    # Rate limiting
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 30
    rate_limit_window: int = 60  # seconds
    
    # Admin settings
    admin_chat_ids: List[int] = None
    support_chat_id: Optional[int] = None
    
    def __post_init__(self):
        if self.admin_chat_ids is None:
            admin_ids = os.getenv('ADMIN_CHAT_IDS', '')
            self.admin_chat_ids = [int(id_) for id_ in admin_ids.split(',') if id_.strip().isdigit()]


@dataclass
class DatabaseConfig:
    """Database configuration"""
    url: str
    pool_size: int = 20
    max_overflow: int = 30
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
    ssl_verify: bool = True  # Enable SSL verification by default
    ssl_cert_path: Optional[str] = None  # Path to custom SSL certificate
    
    # API endpoints
    auth_endpoint: str = "/api/auth"
    products_endpoint: str = "/api/products"
    orders_endpoint: str = "/api/orders"
    payments_endpoint: str = "/api/payments"
    delivery_endpoint: str = "/api/delivery"
    subscriptions_endpoint: str = "/api/subscriptions"
    loyalty_endpoint: str = "/api/loyalty"
    analytics_endpoint: str = "/api/analytics"


@dataclass
class PaymentConfig:
    """Payment providers configuration"""
    # Payme
    payme_merchant_id: Optional[str] = None
    payme_secret_key: Optional[str] = None
    payme_test_mode: bool = True
    
    # Click
    click_merchant_id: Optional[str] = None
    click_service_id: Optional[str] = None
    click_secret_key: Optional[str] = None
    click_test_mode: bool = True
    
    # Telegram Payments
    telegram_provider_token: Optional[str] = None
    
    # Supported currencies
    supported_currencies: List[str] = None
    
    def __post_init__(self):
        if self.supported_currencies is None:
            self.supported_currencies = ['UZS', 'USD']


@dataclass
class NotificationConfig:
    """Notification services configuration"""
    # SendGrid
    sendgrid_api_key: Optional[str] = None
    sendgrid_from_email: Optional[str] = None
    
    # Twilio
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_phone_number: Optional[str] = None
    
    # Push notifications
    fcm_server_key: Optional[str] = None


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
class FeatureConfig:
    """Feature flags configuration"""
    # Core features
    enable_registration: bool = True
    enable_guest_orders: bool = True
    enable_voice_messages: bool = True
    enable_location_sharing: bool = True
    
    # Payment features
    enable_cash_on_delivery: bool = True
    enable_online_payments: bool = True
    enable_loyalty_payments: bool = True
    enable_business_accounts: bool = True
    
    # Delivery features
    enable_express_delivery: bool = True
    enable_scheduled_delivery: bool = True
    enable_delivery_tracking: bool = True
    enable_live_location: bool = True
    
    # Advanced features
    enable_subscriptions: bool = True
    enable_loyalty_program: bool = True
    enable_referrals: bool = True
    enable_analytics: bool = True
    enable_admin_panel: bool = True
    
    # AI features
    enable_ai_recommendations: bool = True
    enable_chatbot_mode: bool = True
    enable_smart_reorder: bool = True


@dataclass
class SecurityConfig:
    """Security configuration"""
    jwt_secret_key: str
    jwt_expiry_hours: int = 24
    jwt_refresh_expiry_days: int = 30
    
    # Rate limiting
    max_login_attempts: int = 5
    login_lockout_duration: int = 300  # seconds
    
    # Data encryption
    encrypt_personal_data: bool = True
    encryption_key: Optional[str] = None


class BotConfig:
    """Main bot configuration"""
    
    def __init__(self):
        # Load environment variables using secrets manager
        self.telegram = TelegramConfig(
            bot_token=get_secret('telegram_bot_token', 'TELEGRAM_BOT_TOKEN', required=True),
            webhook_url=os.getenv('WEBHOOK_URL'),
            webhook_port=int(os.getenv('WEBHOOK_PORT', '8443')),
        )
        
        self.database = DatabaseConfig(
            url=get_database_url(),
            pool_size=int(os.getenv('DB_POOL_SIZE', '20')),
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
        
        self.payments = PaymentConfig(
            payme_merchant_id=os.getenv('PAYME_MERCHANT_ID'),
            payme_secret_key=get_secret('payme_secret_key', 'PAYME_SECRET_KEY', required=False),
            payme_test_mode=os.getenv('PAYME_TEST_MODE', 'true').lower() == 'true',
            click_merchant_id=os.getenv('CLICK_MERCHANT_ID'),
            click_service_id=os.getenv('CLICK_SERVICE_ID'),
            click_secret_key=get_secret('click_secret_key', 'CLICK_SECRET_KEY', required=False),
            click_test_mode=os.getenv('CLICK_TEST_MODE', 'true').lower() == 'true',
            telegram_provider_token=os.getenv('TELEGRAM_PROVIDER_TOKEN'),
        )
        
        self.notifications = NotificationConfig(
            sendgrid_api_key=get_secret('sendgrid_api_key', 'SENDGRID_API_KEY', required=False),
            sendgrid_from_email=os.getenv('SENDGRID_FROM_EMAIL'),
            twilio_account_sid=os.getenv('TWILIO_ACCOUNT_SID'),
            twilio_auth_token=get_secret('twilio_auth_token', 'TWILIO_AUTH_TOKEN', required=False),
            twilio_phone_number=os.getenv('TWILIO_PHONE_NUMBER'),
            fcm_server_key=os.getenv('FCM_SERVER_KEY'),
        )
        
        self.localization = LocalizationConfig(
            default_language=os.getenv('DEFAULT_LANGUAGE', 'en'),
        )
        
        self.features = FeatureConfig()
        
        self.security = SecurityConfig(
            jwt_secret_key=os.environ.get('JWT_SECRET_KEY') or os.environ.get('SECRET_KEY'),
            encryption_key=os.environ.get('ENCRYPTION_KEY'),
        )
        
        # Validate required configuration
        self._validate_config()
    
    def _validate_config(self):
        """Validate required configuration values"""
        required_fields = [
            (self.telegram.bot_token, "TELEGRAM_BOT_TOKEN"),
            (self.database.url, "DATABASE_URL"),
            (self.business_api.base_url, "BUSINESS_APP_URL"),
            (self.security.jwt_secret_key, "JWT_SECRET_KEY"),
        ]
        
        missing_fields = [field_name for field_value, field_name in required_fields if not field_value]
        
        if missing_fields:
            raise ValueError(f"Missing required configuration: {', '.join(missing_fields)}")
    
    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        return user_id in self.telegram.admin_chat_ids
    
    def get_api_url(self, endpoint: str) -> str:
        """Get full API URL for endpoint"""
        return f"{self.business_api.base_url.rstrip('/')}{endpoint}"


# Global configuration instance
config = BotConfig()