"""What Telegram appends to a login button's URL at the tap (spec 2026-10-27 §9, §17 Q5; step 4 Task 16a).

"Open on website" is a Telegram login button (`login_url`): when the customer taps it, Telegram opens the button's
URL with the customer's identity and a `hash` appended. `telegram_signed` is Telegram's side of that, written out
in full from Telegram's documented check (core.telegram.org/widgets/login, "Checking authorization") and never
imported from the backend, so a mistake in the backend's check cannot hide in both. The known-answer vector in
tests/unit/test_telegram_login_service.py pins it.
"""

import hashlib
import hmac


def telegram_signed(fields: dict, token: str) -> dict:
    """`fields` plus the `hash` Telegram adds: an HMAC-SHA256 of the data-check string (each field as `key=value`,
    sorted by key, joined by line feeds), keyed with the SHA-256 digest of the bot token, in lowercase hex."""
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(token.encode("utf-8")).digest()
    signature = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**fields, "hash": signature}
