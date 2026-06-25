"""
Application constants for the Water Business Platform
"""

from enum import Enum

from shared.constants import DISPLAY_TIMEZONE


class PaymentMethodType(Enum):
    """Payment method type enumeration"""

    INSTANT = "instant"  # Cash, immediate payment
    CARD_PAYMENT = "card_payment"  # Credit/debit cards
    DIGITAL_WALLET = "digital_wallet"  # Payme, Click
    POINTS = "points"  # Loyalty points
    ACCOUNT_BALANCE = "account_balance"  # Business account


class NotificationType(Enum):
    """Notification type enumeration"""

    # Order notification types
    ORDER_CONFIRMATION = "order_confirmation"
    ORDER_STATUS_UPDATE = "order_status_update"
    ORDER_UPDATE = "order_update"
    ORDER_EDITED = "order_edited"

    # Delivery notification types
    DELIVERY_UPDATE = "delivery_update"
    DELIVERY_REMINDER = "delivery_reminder"

    # Payment notification types
    PAYMENT_CONFIRMATION = "payment_confirmation"

    # Subscription notification types
    SUBSCRIPTION_REMINDER = "subscription_reminder"
    SUBSCRIPTION_CREATED = "subscription_created"
    SUBSCRIPTION_RENEWAL = "subscription_renewal"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    SUBSCRIPTION_CANCELLATION_SCHEDULED = "subscription_cancellation_scheduled"

    # Promotional notification types
    PROMOTIONAL = "promotional"

    # System notification types
    SYSTEM = "system"
    SYSTEM_ALERT = "system_alert"

    # Authentication notification types
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"

    # Security notification types
    SECURITY = "security"  # Security alerts (password changes, suspicious activity)

    # Loyalty notification types
    LOYALTY_REWARD = "loyalty_reward"
    REWARD_REDEEMED = "reward_redeemed"


class NotificationChannel(Enum):
    """Notification channel enumeration"""

    EMAIL = "email"
    SMS = "sms"
    TELEGRAM = "telegram"
    PUSH = "push"
    IN_APP = "in_app"


class ProductCategory(Enum):
    """Product category enumeration"""

    DRINKING_WATER = "drinking_water"
    SPARKLING_WATER = "sparkling_water"
    FLAVORED_WATER = "flavored_water"
    ALKALINE_WATER = "alkaline_water"
    DISTILLED_WATER = "distilled_water"
    SPRING_WATER = "spring_water"


class ProductSize(Enum):
    """Product size enumeration"""

    SMALL = "0.5L"
    MEDIUM = "1L"
    LARGE = "1.5L"
    EXTRA_LARGE = "5L"
    BULK = "19L"


class Priority(Enum):
    """Priority enumeration"""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class DeliveryType(Enum):
    """Delivery type enumeration"""

    STANDARD = "standard"
    EXPRESS = "express"
    SCHEDULED = "scheduled"
    EMERGENCY = "emergency"


class DiscountType(Enum):
    """Discount type enumeration"""

    PERCENTAGE = "percentage"
    FIXED_AMOUNT = "fixed_amount"
    FREE_DELIVERY = "free_delivery"
    BUY_ONE_GET_ONE = "bogo"


class LoyaltyActionType(Enum):
    """Loyalty action type enumeration"""

    PURCHASE = "purchase"
    REFERRAL = "referral"
    REVIEW = "review"
    SOCIAL_SHARE = "social_share"
    BIRTHDAY_BONUS = "birthday_bonus"
    WELCOME_BONUS = "welcome_bonus"
    STREAK_BONUS = "streak_bonus"
    CONSECUTIVE_STREAK_BONUS = "consecutive_streak_bonus"
    SURPRICE_REWARD = "surprise_reward"


class LoyaltyTransactionType(Enum):
    """Loyalty transaction type enumeration"""

    EARNED = "earned"
    REDEEMED = "redeemed"
    EXPIRED = "expired"
    BONUS = "bonus"
    ADJUSTMENT = "adjustment"


class RewardStatus(Enum):
    """Reward status enumeration"""

    AVAILABLE = "available"
    CLAIMED = "claimed"
    EXPIRED = "expired"
    USED = "used"
    CANCELLED = "cancelled"


# Free-delivery reward constants removed (loyalty SSOT, Phase 2): the free-delivery
# reward is a LoyaltyReward DB row (is_system_reward), not a hardcoded constant.


# Membership tiers are owned entirely by the LoyaltyTierConfig model (DB, single
# source of truth) — the former MembershipTier enum and MEMBERSHIP_TIERS fallback
# dict were removed (loyalty SSOT, Unit D). Use LoyaltyService.get_tiers() /
# LoyaltyTierConfig.get_all_tiers().


class NotificationStatus(Enum):
    """Notification status enumeration"""

    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    READ = "read"


class NotificationChannelType(Enum):
    """Notification channel type enumeration"""

    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    IN_APP = "in_app"
    TELEGRAM = "telegram"


class PriceRuleType(Enum):
    BULK_DISCOUNT = "bulk_discount"
    VIP_DISCOUNT = "vip_discount"
    LOYALTY_DISCOUNT = "loyalty_discount"
    SEASONAL_DISCOUNT = "seasonal_discount"
    TIME_BASED = "time_based"


class FileType(Enum):
    """File type enumeration"""

    IMAGE = "image"
    DOCUMENT = "document"
    VIDEO = "video"
    AUDIO = "audio"


class LogLevel(Enum):
    """Log level enumeration"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Payme Integration Constants
class PaymeState(Enum):
    """Payme Transaction States"""

    CREATED = 1
    COMPLETED = 2
    CANCELLED = -1
    REFUNDED = -2


class PaymeErrors:
    """Payme Error Codes"""

    INTERNAL_ERROR = -32400
    INVALID_AMOUNT = -31001
    TRANSACTION_NOT_FOUND = -31003
    UNABLE_TO_CANCEL = -31007
    OPERATION_NOT_ALLOWED = -31008
    ORDER_NOT_FOUND = -31050
    ORDER_ALREADY_PAID = -31051
    ORDER_HAS_PENDING_PAYMENT = -31052
    AUTH_FAILED = -32504
    METHOD_NOT_FOUND = -32601
    NON_POST_REQUEST = -32300
    JSON_PARSE_ERROR = -32700
    JSON_VALIDATION_ERROR = -32600


# API Response Messages
API_MESSAGES = {
    "SUCCESS": "Operation completed successfully",
    "CREATED": "Resource created successfully",
    "UPDATED": "Resource updated successfully",
    "DELETED": "Resource deleted successfully",
    "NOT_FOUND": "Resource not found",
    "UNAUTHORIZED": "Authentication required",
    "FORBIDDEN": "Access denied",
    "VALIDATION_ERROR": "Validation failed",
    "CONFLICT": "Resource already exists",
    "RATE_LIMIT": "Rate limit exceeded",
    "INTERNAL_ERROR": "Internal server error",
}

# Business Rules Constants
# NOTE: env-driven / money / auth values (MIN_ORDER_AMOUNT, MAX_ORDER_ITEMS,
# MAX_LOGIN_ATTEMPTS, OTP expiry, ...) live in shared/business_config.py (the
# single source of truth) — do NOT re-add them here. The values below are
# non-env operational reference defaults.
BUSINESS_RULES = {
    "MAX_DELIVERY_DISTANCE": 20,  # km
    "DEFAULT_DELIVERY_TIME": 60,  # minutes
    "MAX_DELIVERY_TIME": 240,  # minutes
    # LOYALTY_POINTS_EXPIRY_DAYS removed — the points-expiry window is DB-driven
    # via LoyaltyProgram.points_expiry_days (single source of truth).
    "MAX_REFERRALS_PER_DAY": 5,
    "SESSION_TIMEOUT_MINUTES": 30,
    "PASSWORD_RESET_EXPIRY_HOURS": 24,
    "ACCOUNT_LOCKOUT_MINUTES": 30,
    "PAYME_TIMEOUT_HOURS": 12,  # Payme transaction timeout
}

DELIVERY_ZONES = {
    "CENTRAL": {"name": "Central Tashkent", "fee": 0, "radius": 5},
    "INNER": {"name": "Inner Districts", "fee": 3000, "radius": 10},
    "OUTER": {"name": "Outer Districts", "fee": 5000, "radius": 20},
}


# Time Constants
BUSINESS_HOURS = {
    "start": 9,  # 9 AM
    "end": 21,  # 9 PM
}

DELIVERY_TIME_SLOTS = [
    "09:00-11:00",
    "11:00-13:00",
    "13:00-15:00",
    "15:00-17:00",
    "17:00-19:00",
    "19:00-21:00",
]

# Pagination Constants
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# File Upload Constants
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_DOCUMENT_EXTENSIONS = {"pdf", "doc", "docx", "txt"}

# Cache Keys
CACHE_KEYS = {
    "PRODUCTS": "products:all",
    "CATEGORIES": "categories:all",
    "USER_PROFILE": "user:profile:{}",
    "ORDER_HISTORY": "user:orders:{}",
    "DELIVERY_ZONES": "delivery:zones",
    "PRICING": "pricing:current",
    "ANALYTICS": "analytics:{}",
}

# Cache Timeouts (in seconds)
CACHE_TIMEOUTS = {
    "SHORT": 300,  # 5 minutes
    "MEDIUM": 1800,  # 30 minutes
    "LONG": 3600,  # 1 hour
    "DAILY": 86400,  # 24 hours
}

# Rate Limiting
RATE_LIMITS = {
    "API_GENERAL": "100/hour",
    "API_AUTH": "10/minute",
    "API_ORDERS": "50/hour",
    "API_PAYMENTS": "20/hour",
    "TELEGRAM_BOT": "30/minute",
    "FILE_UPLOAD": "10/hour",
}

# Format: {PREFIX}_{SEQUENCE}_{YY} e.g., TG_000042_26
ORDER_SOURCE_PREFIXES = {
    "telegram": "TG",  # Telegram bot orders
    "web": "WB",  # Web application orders
    "phone": "CC",  # Contact center / phone orders
    "admin": "AD",  # Admin-created orders
    "api": "AP",  # Direct API orders
    "mobile": "MB",  # Mobile app orders (future)
}

# Regex Patterns
PATTERNS = {
    "PHONE_UZ": r"^\+998[0-9]{9}$",
    "EMAIL": r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    "PASSWORD": r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[a-zA-Z\d@$!%*?&]{8,}$",
    "ORDER_NUMBER": r"^(TG|WB|CC|AD|AP|MB)_\d{6}_\d{2}$",  # e.g., TG_000042_26
    "TRACKING_CODE": r"^TR[A-Z0-9]{8}$",
}

# Error Codes
ERROR_CODES = {
    "VALIDATION_FAILED": "E001",
    "RESOURCE_NOT_FOUND": "E002",
    "UNAUTHORIZED_ACCESS": "E003",
    "PAYMENT_FAILED": "E004",
    "DELIVERY_FAILED": "E005",
    "SUBSCRIPTION_ERROR": "E006",
    "FILE_UPLOAD_ERROR": "E007",
    "EXTERNAL_SERVICE_ERROR": "E008",
    "RATE_LIMIT_EXCEEDED": "E009",
    "CONFIGURATION_ERROR": "E010",
}

# Default Values
DEFAULTS = {
    "LANGUAGE": "en",
    "CURRENCY": "UZS",
    "TIMEZONE": DISPLAY_TIMEZONE,
    "PAGE_SIZE": DEFAULT_PAGE_SIZE,
    # DELIVERY_FEE and LOYALTY_POINTS_RATIO removed — both live in
    # shared/business_config.py (single source of truth).
    "SESSION_DURATION": 3600,
}

# Feature Flags
FEATURES = {
    "LOYALTY_PROGRAM": True,
    "SUBSCRIPTION_SERVICE": True,
    "REAL_TIME_TRACKING": True,
    "VOICE_ORDERING": True,
    "AI_RECOMMENDATIONS": True,
    "MULTI_LANGUAGE": True,
    "PAYMENT_LINKS": True,
    "BULK_ORDERS": True,
    "CORPORATE_ACCOUNTS": True,
    "ANALYTICS_DASHBOARD": True,
}
