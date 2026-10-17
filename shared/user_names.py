"""How an automated message names the customer (spec 2026-10-09 §7.3 `name`).

One rule for the customer bot and the email: the first name for a person, the company name for an
entity. Nothing (None) when the only name is empty or the placeholder the Telegram sign-up writes,
so the copy uses its no-name greeting instead of "Hi Telegram User" or "Hi None" (Review Focus 3).
"""

from typing import Optional

# What `AuthService` writes as first_name when Telegram gives none (both Telegram sign-up paths).
GENERATED_TELEGRAM_NAME = "Telegram User"


def joined_name(first_name: Optional[str], last_name: Optional[str]) -> str:
    """"First Last" with the missing parts dropped (never "None"): `User.full_name`'s rule, for a
    caller that holds only the published parts (the customer bot reads `User.to_dict()`, whose own
    `full_name` prints a missing part as "None")."""
    return " ".join(part for part in (first_name, last_name) if part)


def _usable(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if not text or text.casefold() == GENERATED_TELEGRAM_NAME.casefold():
        return None
    return text


def greeting_name(
    *,
    first_name: Optional[str],
    full_name: Optional[str],
    company_name: Optional[str],
    is_entity: bool,
) -> Optional[str]:
    """The name to greet with, or None to greet without one.

    An entity is greeted by its company name; with none on file it falls back to the person rule.
    A person is greeted by first name, else by the full name.
    """
    if is_entity:
        company = _usable(company_name)
        if company:
            return company
    return _usable(first_name) or _usable(full_name)
