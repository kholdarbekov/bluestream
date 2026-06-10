"""
Shared validation and sanitization utilities.
Used by both backend (business_app) and telegram bot.
"""
import re
from typing import Optional, Tuple

import phonenumbers


# --- Uzbekistan phone validation: single source of truth (phonenumbers) ---

_DEFAULT_REGION = "UZ"
_UZ_COUNTRY_CODE = 998

# Number types accepted for registration / SMS-OTP. libphonenumber reports some
# valid UZ mobile ranges as FIXED_LINE_OR_MOBILE, so we accept both. Pure
# FIXED_LINE is rejected (can't receive an SMS OTP).
_ACCEPTED_NUMBER_TYPES = (
    phonenumbers.PhoneNumberType.MOBILE,
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
)


def _parse(phone):
    """Best-effort parse to a libphonenumber object. Returns None, never raises."""
    if not phone:
        return None
    candidate = str(phone).strip()
    digits = re.sub(r"\D", "", candidate)
    if candidate.startswith("+"):
        to_parse = candidate
    elif digits.startswith("998") and len(digits) == 12:
        to_parse = "+" + digits          # full international, missing the '+'
    elif len(digits) == 9:
        to_parse = digits                # bare national number, region supplies +998
    else:
        to_parse = candidate
    try:
        return phonenumbers.parse(to_parse, _DEFAULT_REGION)
    except phonenumbers.NumberParseException:
        return None


def normalize_phone_number(phone: str) -> Optional[str]:
    """
    Normalize to E.164 (+998XXXXXXXXX) via libphonenumber.
    Returns None if the input is not a valid Uzbekistan mobile number.
    """
    parsed = _parse(phone)
    if parsed is None or not phonenumbers.is_valid_number(parsed):
        return None
    # Restrict to Uzbekistan (+998). A bare/foreign number that libphonenumber
    # happens to consider valid for another region must never be accepted.
    if parsed.country_code != _UZ_COUNTRY_CODE:
        return None
    if phonenumbers.number_type(parsed) not in _ACCEPTED_NUMBER_TYPES:
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def validate_uzbekistan_phone(phone: str):
    """Returns (is_valid, message, normalized_phone_or_None)."""
    normalized = normalize_phone_number(phone)
    if normalized:
        return True, "Phone is valid", normalized
    return False, "Phone number must be a valid Uzbekistan mobile number (+998XXXXXXXXX)", None


def validate_phone_number(phone: str) -> bool:
    """Boolean check for a valid Uzbekistan mobile phone."""
    return normalize_phone_number(phone) is not None


def mask_phone_number(phone: str) -> str:
    """Mask for display: +998901234567 -> +998***4567."""
    normalized = normalize_phone_number(phone)
    target = normalized or (str(phone).strip() if phone else "")
    # >= 7 keeps the 4-char prefix and 4-char suffix from overlapping
    # (a valid E.164 UZ number is 13 chars, well above this floor).
    if len(target) >= 7:
        return f"{target[:4]}***{target[-4:]}"
    return "***"


# --- Password validation ---

_WEAK_PASSWORD_PATTERNS = ['password', '123456', 'qwerty', 'admin', 'user', 'test']


def validate_password_strength(password: str) -> Tuple[bool, str]:
    """
    Validate password meets security requirements.

    Returns:
        (is_valid, message)
    """
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters long"

    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one digit"

    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password must contain at least one special character"

    if any(weak in password.lower() for weak in _WEAK_PASSWORD_PATTERNS):
        return False, "Password contains common weak patterns"

    return True, "Password is strong"


# --- Email validation ---

_EMAIL_PATTERN = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')


def validate_email(email: Optional[str]) -> Tuple[bool, str]:
    """
    Validate email format.

    Returns:
        (is_valid, message)
    """
    if not email:
        return True, "Email is optional"

    if not _EMAIL_PATTERN.match(email):
        return False, "Invalid email format"

    if email != email.lower():
        return False, "Email must be lowercase"

    return True, "Email is valid"


# --- Input sanitization ---

_DANGEROUS_CHARS_RE = re.compile(r'[<>"\'\`&;|$(){}[\]\\]')


def sanitize_user_input(input_text: Optional[str]) -> Optional[str]:
    """
    Sanitize user input to prevent XSS and injection.

    Returns sanitized string or None if empty after sanitization.
    """
    if not input_text:
        return input_text

    sanitized = _DANGEROUS_CHARS_RE.sub('', input_text)
    sanitized = sanitized.strip()

    return sanitized if sanitized else None
