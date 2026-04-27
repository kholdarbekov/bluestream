"""
Helper utilities for the Water Business Platform
"""

import hashlib
import secrets
import string
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from flask import g, current_app
from geopy.distance import geodesic
import phonenumbers
from phonenumbers import NumberParseException
from transliterate import translit
import logging

logger = logging.getLogger(__name__)


def generate_random_string(length: int = 32) -> str:
    """Generate a random string of specified length"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_tracking_code() -> str:
    """Generate a unique tracking code for deliveries"""
    random_code = generate_random_string(8).upper()
    return f"TR{random_code}"


def generate_referral_code(user_id: int, username: str) -> str:
    """Generate a referral code for a user"""
    base_string = f"{user_id}{username}{datetime.now(timezone.utc).timestamp()}"
    hash_object = hashlib.md5(base_string.encode())
    return hash_object.hexdigest()[:8].upper()


def hash_password(password: str) -> str:
    """Hash a password using secure bcrypt configuration"""
    from business_app.utils.password_security import hash_password as secure_hash_password

    return secure_hash_password(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash"""
    from business_app.utils.password_security import verify_password as secure_verify_password

    return secure_verify_password(password, hashed)


def validate_email(email: str) -> bool:
    """Validate email format"""
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(email_pattern, email) is not None


def validate_phone_number(phone: str, region: str = "UZ") -> bool:
    """Validate phone number format"""
    try:
        parsed_number = phonenumbers.parse(phone, region)
        return phonenumbers.is_valid_number(parsed_number)
    except NumberParseException:
        return False


def format_phone_number(phone: str, region: str = "UZ") -> Optional[str]:
    """Format phone number to international format"""
    try:
        parsed_number = phonenumbers.parse(phone, region)
        if phonenumbers.is_valid_number(parsed_number):
            return phonenumbers.format_number(parsed_number, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException:
        pass
    return None


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two coordinates in kilometers"""
    return geodesic((lat1, lon1), (lat2, lon2)).kilometers


def is_within_delivery_radius(
    customer_lat: float, customer_lon: float, store_lat: float, store_lon: float, radius_km: float = None
) -> bool:
    """Check if customer is within delivery radius"""
    if radius_km is None:
        radius_km = current_app.config["DELIVERY_RADIUS_KM"]

    distance = calculate_distance(customer_lat, customer_lon, store_lat, store_lon)
    return distance <= radius_km


def estimate_delivery_time(distance_km: float, traffic_factor: float = 1.0) -> int:
    """Estimate delivery time in minutes"""
    # Base time: 30 minutes + 2 minutes per km
    base_time = 30
    travel_time = distance_km * 2 * traffic_factor

    return int(base_time + travel_time)


def format_currency(amount: int, currency: str = "UZS") -> str:
    """Format currency amount"""
    if currency == "UZS":
        return f"{amount:,} so'm"
    elif currency == "USD":
        return f"${amount:,.2f}"
    return f"{amount:,} {currency}"


def parse_currency(amount_str: str) -> int:
    """Parse currency string to integer amount"""
    # Remove currency symbols and spaces
    cleaned = re.sub(r"[^\d.]", "", amount_str)
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """Truncate text to specified length"""
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to URL-friendly slug"""
    # Transliterate non-Latin characters
    try:
        text = translit(text, "ru", reversed=True)
    except:  # noqa: E722
        pass

    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    slug = re.sub(r"[-\s]+", "-", slug)

    return slug[:max_length]


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage"""
    # Remove path separators and other dangerous characters
    filename = re.sub(r'[/\\:*?"<>|]', "_", filename)
    return filename.strip()


def get_file_extension(filename: str) -> str:
    """Get file extension from filename"""
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def is_allowed_file(filename: str, allowed_extensions: set = None) -> bool:
    """Check if file extension is allowed"""
    if allowed_extensions is None:
        allowed_extensions = current_app.config["ALLOWED_EXTENSIONS"]

    return "." in filename and get_file_extension(filename) in allowed_extensions


def generate_file_path(user_id: int, filename: str, folder: str = "general") -> str:
    """Generate organized file path"""
    sanitized_filename = sanitize_filename(filename)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name, ext = sanitized_filename.rsplit(".", 1) if "." in sanitized_filename else (sanitized_filename, "")

    new_filename = f"{name}_{timestamp}.{ext}" if ext else f"{name}_{timestamp}"
    return f"{folder}/{user_id}/{new_filename}"


def paginate_query(query, page: int = 1, per_page: int = 20, max_per_page: int = 100):
    """Paginate SQLAlchemy query"""
    per_page = min(per_page, max_per_page)
    return query.paginate(page=page, per_page=per_page, error_out=False)


def get_current_language() -> str:
    """
    Get current language from request context.

    The language is already detected and set in g.language by the @app.before_request hook,
    which follows this priority order:
    1. URL parameter (?lang=uz)
    2. JWT user preferred_language
    3. Session language
    4. Accept-Language header
    5. Default language (uz)

    This function simply returns the already-detected language from g.language.
    """
    # Get request ID for tracing
    getattr(g, "request_id", "N/A")

    # Return the language already set by before_request hook
    if hasattr(g, "language") and g.language:
        return g.language

    # Fallback: If called outside request context or before_request hasn't run yet
    default_language = current_app.config.get("DEFAULT_LANGUAGE", "uz")
    return default_language


def set_language(language: str):
    """Set language in request context"""
    g.language = language


def translate_text(key: str, language: str = None, **kwargs) -> str:
    """Translate text using key and current language"""
    if language is None:
        language = get_current_language()

    from .translations import get_translation

    result = get_translation(key, language, **kwargs)

    return result


def format_datetime(dt: datetime, format_type: str = "full", language: str = None) -> str:
    """Format datetime according to language and format type"""
    if language is None:
        language = get_current_language()

    if format_type == "date":
        if language == "uz":
            return dt.strftime("%d.%m.%Y")
        elif language == "ru":
            return dt.strftime("%d.%m.%Y")
        else:  # English
            return dt.strftime("%m/%d/%Y")

    elif format_type == "time":
        return dt.strftime("%H:%M")

    elif format_type == "datetime":
        date_str = format_datetime(dt, "date", language)
        time_str = format_datetime(dt, "time", language)
        return f"{date_str} {time_str}"

    else:  # full
        if language == "uz":
            months = [
                "Yanvar",
                "Fevral",
                "Mart",
                "Aprel",
                "May",
                "Iyun",
                "Iyul",
                "Avgust",
                "Sentabr",
                "Oktabr",
                "Noyabr",
                "Dekabr",
            ]
            return f"{dt.day} {months[dt.month-1]} {dt.year}, {dt.strftime('%H:%M')}"
        elif language == "ru":
            months = [
                "Января",
                "Февраля",
                "Марта",
                "Апреля",
                "Мая",
                "Июня",
                "Июля",
                "Августа",
                "Сентября",
                "Октября",
                "Ноября",
                "Декабря",
            ]
            return f"{dt.day} {months[dt.month-1]} {dt.year}, {dt.strftime('%H:%M')}"
        else:  # English
            return dt.strftime("%B %d, %Y at %H:%M")


def get_time_slots(start_hour: int = 9, end_hour: int = 21, interval_minutes: int = 60) -> List[str]:
    """Generate available time slots for delivery"""
    slots = []
    current_time = datetime.now(timezone.utc).replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end_time = datetime.now(timezone.utc).replace(hour=end_hour, minute=0, second=0, microsecond=0)

    while current_time < end_time:
        next_time = current_time + timedelta(minutes=interval_minutes)
        slots.append(f"{current_time.strftime('%H:%M')}-{next_time.strftime('%H:%M')}")
        current_time = next_time

    return slots


def calculate_loyalty_points(amount: int) -> int:
    """
    Calculate loyalty points earned from purchase amount.

    DEPRECATED: This function uses a hardcoded ratio from Flask config and does not
    apply tier-based multipliers or LoyaltyProgram.points_per_uzs configuration.

    Use LoyaltyService.calculate_points_for_purchase(user_id, amount) instead for
    proper program-aware and tier-based point calculations.

    This function is kept for backward compatibility but will be removed in future versions.
    """
    import warnings

    warnings.warn(
        "calculate_loyalty_points() is deprecated. Use LoyaltyService.calculate_points_for_purchase() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    points_ratio = current_app.config["LOYALTY_POINTS_RATIO"]
    return amount // points_ratio


def calculate_discount_from_points(points: int) -> int:
    """Calculate discount amount from loyalty points"""
    redemption_ratio = current_app.config["LOYALTY_REDEMPTION_RATIO"]
    return points * redemption_ratio


def mask_phone_number(phone: str) -> str:
    """Mask phone number for privacy"""
    if len(phone) < 4:
        return phone
    return phone[:-4] + "****"


def mask_email(email: str) -> str:
    """Mask email for privacy"""
    if "@" not in email:
        return email

    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]

    return f"{masked_local}@{domain}"


def generate_otp(length: int = 6) -> str:
    """Generate numeric OTP code"""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def is_business_hours(dt: datetime = None) -> bool:
    """Check if current time is within business hours"""
    if dt is None:
        dt = datetime.now(timezone.utc)

    # Business hours: 9 AM to 9 PM
    return 9 <= dt.hour < 21


def get_next_business_day(dt: datetime = None) -> datetime:
    """Get next business day (Monday-Sunday, we deliver every day)"""
    if dt is None:
        dt = datetime.now(timezone.utc)

    # If it's past business hours, move to next day
    if dt.hour >= 21:
        dt = dt.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    elif dt.hour < 9:
        dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)

    return dt


def format_file_size(size_bytes: int) -> str:
    """Format file size in human readable format"""
    if size_bytes == 0:
        return "0 B"

    size_names = ["B", "KB", "MB", "GB"]
    import math

    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)

    return f"{s} {size_names[i]}"


def clean_phone_number(phone: str) -> str:
    """Clean phone number by removing non-digit characters"""
    return re.sub(r"\D", "", phone)


def validate_uzbek_phone(phone: str) -> bool:
    """Validate Uzbek phone number format"""
    cleaned = clean_phone_number(phone)
    # Uzbek mobile numbers: +998XXXXXXXXX (9 digits after country code)
    return len(cleaned) == 12 and cleaned.startswith("998")


def generate_invoice_number() -> str:
    """Generate invoice number"""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    random_suffix = generate_random_string(3).upper()
    return f"INV{timestamp}{random_suffix}"


def to_ms(dt: datetime) -> int:
    """Convert datetime to milliseconds timestamp"""
    if not dt:
        return 0
    from datetime import timezone

    # Ensure timezone aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_analytics_date_range(days: int = 1):
    """Get (start_date, end_date) for analytics tasks looking back N days from now."""
    from datetime import timezone, timedelta

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)
    return start_date, end_date
