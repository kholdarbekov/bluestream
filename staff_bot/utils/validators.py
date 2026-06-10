"""
Input validators for Staff Bot
"""
from typing import Optional, Tuple


def normalize_phone(phone: str) -> Optional[str]:
    """
    Normalize phone number to +998XXXXXXXXX format.
    Handles various input formats.
    """
    # Remove spaces, dashes, parentheses
    from shared.validators import normalize_phone_number as _normalize
    return _normalize(phone)


def validate_phone(phone: str) -> Tuple[bool, str]:
    """
    Validate Uzbekistan phone number.
    Returns (is_valid, normalized_phone_or_error).
    """
    normalized = normalize_phone(phone)
    if not normalized:
        return False, "Invalid phone format. Expected: +998XXXXXXXXX"
    return True, normalized


def validate_name(name: str) -> Tuple[bool, str]:
    """
    Validate a person's name.
    Returns (is_valid, error_message_or_empty).
    """
    if not name or len(name.strip()) < 2:
        return False, "Name must be at least 2 characters"
    if len(name.strip()) > 100:
        return False, "Name must be less than 100 characters"
    return True, ""


def validate_quantity(quantity_str: str, max_qty: int = 100) -> Tuple[bool, int]:
    """
    Validate order quantity.
    Returns (is_valid, quantity_or_zero).
    """
    try:
        qty = int(quantity_str)
        if qty < 1:
            return False, 0
        if qty > max_qty:
            return False, 0
        return True, qty
    except (ValueError, TypeError):
        return False, 0
