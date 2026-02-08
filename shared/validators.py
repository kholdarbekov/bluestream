"""
Shared validation and sanitization utilities.
Used by both backend (business_app) and telegram bot.
"""
import re
from typing import Optional, Tuple


# Valid Uzbekistan mobile operator prefixes
_UZ_OPERATOR_PREFIXES = {
    '90', '91', '93', '94', '95', '97', '98', '99',  # Standard mobile
    '33', '55', '71', '77', '78', '88',  # Additional operators
}

_PHONE_CLEAN_RE = re.compile(r'[\s\-\(\)]')
_NON_DIGIT_RE = re.compile(r'[^\d+]')


def validate_uzbekistan_phone(phone: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate and normalize an Uzbekistan phone number.

    Accepts formats: +998901234567, 998901234567, 901234567, 90 123 45 67

    Returns:
        (is_valid, message, normalized_phone_or_None)
    """
    normalized = normalize_phone_number(phone)
    if normalized and len(normalized) == 13 and normalized.startswith('+998'):
        return True, "Phone is valid", normalized
    return False, "Phone number must be a valid Uzbekistan number (+998XXXXXXXXX)", None


def normalize_phone_number(phone: str) -> str:
    """
    Normalize a phone number to +998XXXXXXXXX format.

    Returns the cleaned number (best-effort) even if it doesn't fully validate,
    so callers can inspect it or show it back to the user.
    """
    clean = _NON_DIGIT_RE.sub('', phone)

    if clean.startswith('+998'):
        clean = clean[1:]  # remove leading +
    if clean.startswith('998') and len(clean) == 12:
        return f'+{clean}'
    if clean.startswith('8') and len(clean) == 10:
        return f'+99{clean}'
    if len(clean) == 9:
        return f'+998{clean}'

    # Already in full format or unrecognised – return with +
    if clean.startswith('998'):
        return f'+{clean}'
    return clean


def validate_phone_number(phone: str) -> bool:
    """Simple boolean check for valid Uzbekistan phone."""
    is_valid, _, _ = validate_uzbekistan_phone(phone)
    return is_valid


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
