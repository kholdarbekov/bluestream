"""
Centralized Error Message Translations

This module provides a unified system for managing error messages across the platform.
All error messages are translated based on user language preference.

Usage:
    from business_app.utils.error_messages import get_error_message, ErrorCode

    message = get_error_message(ErrorCode.USER_NOT_FOUND, language='uz')
"""

from enum import Enum
from typing import Optional, Dict, Any
from business_app.utils.translations import get_translation


class ErrorCode(str, Enum):
    """
    Centralized error codes for the entire platform.
    Each code maps to a translation key.
    """

    # Authentication & Authorization Errors
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    EMAIL_ALREADY_EXISTS = "EMAIL_ALREADY_EXISTS"
    PHONE_ALREADY_EXISTS = "PHONE_ALREADY_EXISTS"
    TELEGRAM_ID_ALREADY_EXISTS = "TELEGRAM_ID_ALREADY_EXISTS"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_INVALID = "TOKEN_INVALID"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
    INSUFFICIENT_PERMISSIONS = "INSUFFICIENT_PERMISSIONS"

    # Validation Errors
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_EMAIL = "INVALID_EMAIL"
    INVALID_PHONE = "INVALID_PHONE"
    INVALID_PASSWORD = "INVALID_PASSWORD"
    PASSWORD_TOO_SHORT = "PASSWORD_TOO_SHORT"
    PASSWORD_TOO_WEAK = "PASSWORD_TOO_WEAK"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    INVALID_INPUT_FORMAT = "INVALID_INPUT_FORMAT"
    INVALID_DATE_FORMAT = "INVALID_DATE_FORMAT"
    INVALID_BOOLEAN = "INVALID_BOOLEAN"

    # Resource Not Found Errors
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    ADDRESS_NOT_FOUND = "ADDRESS_NOT_FOUND"
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    DELIVERY_NOT_FOUND = "DELIVERY_NOT_FOUND"
    NOTIFICATION_NOT_FOUND = "NOTIFICATION_NOT_FOUND"

    # Business Logic Errors
    OUT_OF_STOCK = "OUT_OF_STOCK"
    INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
    ORDER_ALREADY_PAID = "ORDER_ALREADY_PAID"
    ORDER_CANNOT_BE_CANCELLED = "ORDER_CANNOT_BE_CANCELLED"
    PAYMENT_ALREADY_PROCESSED = "PAYMENT_ALREADY_PROCESSED"
    PAYMENT_CANCELLED = "PAYMENT_CANCELLED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    SUBSCRIPTION_ALREADY_ACTIVE = "SUBSCRIPTION_ALREADY_ACTIVE"
    SUBSCRIPTION_ALREADY_CANCELLED = "SUBSCRIPTION_ALREADY_CANCELLED"
    DELIVERY_ALREADY_COMPLETED = "DELIVERY_ALREADY_COMPLETED"
    INVALID_TIME_SLOT = "INVALID_TIME_SLOT"
    TIME_SLOT_UNAVAILABLE = "TIME_SLOT_UNAVAILABLE"
    MINIMUM_ORDER_NOT_MET = "MINIMUM_ORDER_NOT_MET"

    # Payment Errors
    PAYMENT_METHOD_INVALID = "PAYMENT_METHOD_INVALID"
    PAYMENT_GATEWAY_ERROR = "PAYMENT_GATEWAY_ERROR"
    CARD_NUMBER_REQUIRED = "CARD_NUMBER_REQUIRED"
    CARDHOLDER_NAME_REQUIRED = "CARDHOLDER_NAME_REQUIRED"
    INVALID_CARD_EXPIRY = "INVALID_CARD_EXPIRY"
    CARD_EXPIRED = "CARD_EXPIRED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    REFUND_NOT_ALLOWED = "REFUND_NOT_ALLOWED"
    REFUND_ALREADY_PROCESSED = "REFUND_ALREADY_PROCESSED"

    # Rate Limiting & Security
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"
    SUSPICIOUS_ACTIVITY = "SUSPICIOUS_ACTIVITY"
    CSRF_TOKEN_INVALID = "CSRF_TOKEN_INVALID"

    # Server Errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"
    CACHE_ERROR = "CACHE_ERROR"
    FILE_UPLOAD_ERROR = "FILE_UPLOAD_ERROR"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_FILE_TYPE = "INVALID_FILE_TYPE"

    # Conflict Errors
    RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"
    DUPLICATE_ENTRY = "DUPLICATE_ENTRY"
    CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"


# Mapping of error codes to translation keys
ERROR_MESSAGE_MAP: Dict[ErrorCode, str] = {
    # Authentication & Authorization
    ErrorCode.INVALID_CREDENTIALS: "error.auth.invalid_credentials",
    ErrorCode.USER_NOT_FOUND: "error.auth.user_not_found",
    ErrorCode.EMAIL_ALREADY_EXISTS: "error.auth.email_already_exists",
    ErrorCode.PHONE_ALREADY_EXISTS: "error.auth.phone_already_exists",
    ErrorCode.TELEGRAM_ID_ALREADY_EXISTS: "error.auth.telegram_id_already_exists",
    ErrorCode.UNAUTHORIZED: "error.auth.unauthorized",
    ErrorCode.FORBIDDEN: "error.auth.forbidden",
    ErrorCode.TOKEN_EXPIRED: "error.auth.token_expired",
    ErrorCode.TOKEN_INVALID: "error.auth.token_invalid",
    ErrorCode.ACCOUNT_LOCKED: "error.auth.account_locked",
    ErrorCode.ACCOUNT_INACTIVE: "error.auth.account_inactive",
    ErrorCode.INSUFFICIENT_PERMISSIONS: "error.auth.insufficient_permissions",
    # Validation
    ErrorCode.VALIDATION_ERROR: "error.validation.generic",
    ErrorCode.INVALID_EMAIL: "error.validation.invalid_email",
    ErrorCode.INVALID_PHONE: "error.validation.invalid_phone",
    ErrorCode.INVALID_PASSWORD: "error.validation.invalid_password",
    ErrorCode.PASSWORD_TOO_SHORT: "error.validation.password_too_short",
    ErrorCode.PASSWORD_TOO_WEAK: "error.validation.password_too_weak",
    ErrorCode.REQUIRED_FIELD_MISSING: "error.validation.required_field_missing",
    ErrorCode.INVALID_INPUT_FORMAT: "error.validation.invalid_input_format",
    ErrorCode.INVALID_DATE_FORMAT: "error.validation.invalid_date_format",
    ErrorCode.INVALID_BOOLEAN: "error.validation.invalid_boolean",
    # Resource Not Found
    ErrorCode.RESOURCE_NOT_FOUND: "error.not_found.generic",
    ErrorCode.ORDER_NOT_FOUND: "error.not_found.order",
    ErrorCode.PRODUCT_NOT_FOUND: "error.not_found.product",
    ErrorCode.CATEGORY_NOT_FOUND: "error.not_found.category",
    ErrorCode.PAYMENT_NOT_FOUND: "error.not_found.payment",
    ErrorCode.ADDRESS_NOT_FOUND: "error.not_found.address",
    ErrorCode.SUBSCRIPTION_NOT_FOUND: "error.not_found.subscription",
    ErrorCode.DELIVERY_NOT_FOUND: "error.not_found.delivery",
    ErrorCode.NOTIFICATION_NOT_FOUND: "error.not_found.notification",
    # Business Logic
    ErrorCode.OUT_OF_STOCK: "error.business.out_of_stock",
    ErrorCode.INSUFFICIENT_INVENTORY: "error.business.insufficient_inventory",
    ErrorCode.ORDER_ALREADY_PAID: "error.business.order_already_paid",
    ErrorCode.ORDER_CANNOT_BE_CANCELLED: "error.business.order_cannot_be_cancelled",
    ErrorCode.PAYMENT_ALREADY_PROCESSED: "error.business.payment_already_processed",
    ErrorCode.PAYMENT_CANCELLED: "error.business.payment_cancelled",
    ErrorCode.PAYMENT_FAILED: "error.business.payment_failed",
    ErrorCode.SUBSCRIPTION_ALREADY_ACTIVE: "error.business.subscription_already_active",
    ErrorCode.SUBSCRIPTION_ALREADY_CANCELLED: "error.business.subscription_already_cancelled",
    ErrorCode.DELIVERY_ALREADY_COMPLETED: "error.business.delivery_already_completed",
    ErrorCode.INVALID_TIME_SLOT: "error.business.invalid_time_slot",
    ErrorCode.TIME_SLOT_UNAVAILABLE: "error.business.time_slot_unavailable",
    ErrorCode.MINIMUM_ORDER_NOT_MET: "error.business.minimum_order_not_met",
    # Payment
    ErrorCode.PAYMENT_METHOD_INVALID: "error.payment.method_invalid",
    ErrorCode.PAYMENT_GATEWAY_ERROR: "error.payment.gateway_error",
    ErrorCode.CARD_NUMBER_REQUIRED: "error.validation.card_number_required",
    ErrorCode.CARDHOLDER_NAME_REQUIRED: "error.validation.cardholder_name_required",
    ErrorCode.INVALID_CARD_EXPIRY: "error.validation.invalid_card_expiry",
    ErrorCode.CARD_EXPIRED: "error.payment.card_expired",
    ErrorCode.INSUFFICIENT_FUNDS: "error.payment.insufficient_funds",
    ErrorCode.PAYMENT_DECLINED: "error.payment.declined",
    ErrorCode.REFUND_NOT_ALLOWED: "error.payment.refund_not_allowed",
    ErrorCode.REFUND_ALREADY_PROCESSED: "error.payment.refund_already_processed",
    # Rate Limiting & Security
    ErrorCode.RATE_LIMIT_EXCEEDED: "error.security.rate_limit_exceeded",
    ErrorCode.TOO_MANY_REQUESTS: "error.security.too_many_requests",
    ErrorCode.SUSPICIOUS_ACTIVITY: "error.security.suspicious_activity",
    ErrorCode.CSRF_TOKEN_INVALID: "error.security.csrf_invalid",
    # Server Errors
    ErrorCode.INTERNAL_ERROR: "error.server.internal_error",
    ErrorCode.DATABASE_ERROR: "error.server.database_error",
    ErrorCode.EXTERNAL_SERVICE_ERROR: "error.server.external_service_error",
    ErrorCode.CACHE_ERROR: "error.server.cache_error",
    ErrorCode.FILE_UPLOAD_ERROR: "error.server.file_upload_error",
    ErrorCode.FILE_TOO_LARGE: "error.validation.file_too_large",
    ErrorCode.INVALID_FILE_TYPE: "error.validation.invalid_file_type",
    # Conflict
    ErrorCode.RESOURCE_ALREADY_EXISTS: "error.conflict.resource_already_exists",
    ErrorCode.DUPLICATE_ENTRY: "error.conflict.duplicate_entry",
    ErrorCode.CONCURRENT_MODIFICATION: "error.conflict.concurrent_modification",
}


# HTTP status code mapping for each error code
ERROR_STATUS_MAP: Dict[ErrorCode, int] = {
    # 401 Unauthorized
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.TOKEN_INVALID: 401,
    # 403 Forbidden
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.ACCOUNT_LOCKED: 403,
    ErrorCode.ACCOUNT_INACTIVE: 403,
    ErrorCode.INSUFFICIENT_PERMISSIONS: 403,
    # 404 Not Found
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.USER_NOT_FOUND: 404,
    ErrorCode.ORDER_NOT_FOUND: 404,
    ErrorCode.PRODUCT_NOT_FOUND: 404,
    ErrorCode.CATEGORY_NOT_FOUND: 404,
    ErrorCode.PAYMENT_NOT_FOUND: 404,
    ErrorCode.ADDRESS_NOT_FOUND: 404,
    ErrorCode.SUBSCRIPTION_NOT_FOUND: 404,
    ErrorCode.DELIVERY_NOT_FOUND: 404,
    ErrorCode.NOTIFICATION_NOT_FOUND: 404,
    # 409 Conflict
    ErrorCode.EMAIL_ALREADY_EXISTS: 409,
    ErrorCode.PHONE_ALREADY_EXISTS: 409,
    ErrorCode.TELEGRAM_ID_ALREADY_EXISTS: 409,
    ErrorCode.RESOURCE_ALREADY_EXISTS: 409,
    ErrorCode.DUPLICATE_ENTRY: 409,
    ErrorCode.CONCURRENT_MODIFICATION: 409,
    ErrorCode.ORDER_ALREADY_PAID: 409,
    ErrorCode.PAYMENT_ALREADY_PROCESSED: 409,
    ErrorCode.SUBSCRIPTION_ALREADY_ACTIVE: 409,
    ErrorCode.SUBSCRIPTION_ALREADY_CANCELLED: 409,
    ErrorCode.DELIVERY_ALREADY_COMPLETED: 409,
    # 422 Unprocessable Entity
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.INVALID_EMAIL: 422,
    ErrorCode.INVALID_PHONE: 422,
    ErrorCode.INVALID_PASSWORD: 422,
    ErrorCode.PASSWORD_TOO_SHORT: 422,
    ErrorCode.PASSWORD_TOO_WEAK: 422,
    ErrorCode.REQUIRED_FIELD_MISSING: 422,
    ErrorCode.INVALID_INPUT_FORMAT: 422,
    ErrorCode.INVALID_DATE_FORMAT: 422,
    ErrorCode.INVALID_BOOLEAN: 422,
    ErrorCode.CARD_NUMBER_REQUIRED: 422,
    ErrorCode.CARDHOLDER_NAME_REQUIRED: 422,
    ErrorCode.INVALID_CARD_EXPIRY: 422,
    ErrorCode.FILE_TOO_LARGE: 422,
    ErrorCode.INVALID_FILE_TYPE: 422,
    # 400 Bad Request (Business Logic)
    ErrorCode.OUT_OF_STOCK: 400,
    ErrorCode.INSUFFICIENT_INVENTORY: 400,
    ErrorCode.ORDER_CANNOT_BE_CANCELLED: 400,
    ErrorCode.PAYMENT_CANCELLED: 400,
    ErrorCode.PAYMENT_FAILED: 400,
    ErrorCode.INVALID_TIME_SLOT: 400,
    ErrorCode.TIME_SLOT_UNAVAILABLE: 400,
    ErrorCode.MINIMUM_ORDER_NOT_MET: 400,
    ErrorCode.PAYMENT_METHOD_INVALID: 400,
    ErrorCode.CARD_EXPIRED: 400,
    ErrorCode.INSUFFICIENT_FUNDS: 400,
    ErrorCode.PAYMENT_DECLINED: 400,
    ErrorCode.REFUND_NOT_ALLOWED: 400,
    ErrorCode.REFUND_ALREADY_PROCESSED: 400,
    # 429 Too Many Requests
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.TOO_MANY_REQUESTS: 429,
    # 403 Security
    ErrorCode.SUSPICIOUS_ACTIVITY: 403,
    ErrorCode.CSRF_TOKEN_INVALID: 403,
    # 500 Internal Server Error
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.DATABASE_ERROR: 500,
    ErrorCode.EXTERNAL_SERVICE_ERROR: 500,
    ErrorCode.CACHE_ERROR: 500,
    ErrorCode.FILE_UPLOAD_ERROR: 500,
    ErrorCode.PAYMENT_GATEWAY_ERROR: 500,
}


def get_error_message(error_code: ErrorCode, language: Optional[str] = None, **params: Any) -> str:
    """
    Get translated error message for given error code.

    Args:
        error_code: The error code enum value
        language: Language code (uz, en, ru). If None, uses current request language.
        **params: Parameters for string formatting (e.g., field_name="email")

    Returns:
        Translated error message string

    Example:
        >>> get_error_message(ErrorCode.USER_NOT_FOUND, language='uz')
        "Foydalanuvchi topilmadi"

        >>> get_error_message(ErrorCode.REQUIRED_FIELD_MISSING, field_name='email')
        "Email field is required"
    """
    translation_key = ERROR_MESSAGE_MAP.get(error_code, "error.server.internal_error")

    return get_translation(translation_key, language=language, **params)


def get_error_status_code(error_code: ErrorCode) -> int:
    """
    Get HTTP status code for given error code.

    Args:
        error_code: The error code enum value

    Returns:
        HTTP status code (default 500 if not found)

    Example:
        >>> get_error_status_code(ErrorCode.USER_NOT_FOUND)
        404

        >>> get_error_status_code(ErrorCode.INVALID_CREDENTIALS)
        401
    """
    return ERROR_STATUS_MAP.get(error_code, 500)


def create_error_response(error_code: ErrorCode, language: Optional[str] = None, **params: Any) -> tuple[dict, int]:
    """
    Create standardized error response with translated message.

    Args:
        error_code: The error code enum value
        language: Language code (uz, en, ru). If None, uses current request language.
        **params: Parameters for string formatting

    Returns:
        Tuple of (response_dict, status_code)

    Example:
        >>> response, status = create_error_response(ErrorCode.USER_NOT_FOUND)
        >>> print(response)
        {
            'success': False,
            'error_code': 'USER_NOT_FOUND',
            'message': 'User not found'
        }
        >>> print(status)
        404
    """
    message = get_error_message(error_code, language=language, **params)
    status_code = get_error_status_code(error_code)

    return {"success": False, "error_code": error_code.value, "message": message}, status_code
