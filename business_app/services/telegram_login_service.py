"""The "Open on website" sign-in check (spec 2026-10-27 §9, §17 Q5; step 4 Task 16a).

The customer bot's button is a Telegram login button (`login_url`, `shared/telegram_login.py`). At the tap Telegram
opens our `/auth/telegram-login` with the customer's identity and a `hash` appended: an HMAC-SHA256 of those fields,
keyed with the SHA-256 of the customer bot's token. We mint nothing, so an old or forwarded message carries nothing
that can be replayed later. `verify_login` is the one check of that data, and its order is part of the design: the
hash is checked before any lookup, and spent before any refusal that could be retried. It writes nothing to the user;
the route signs the customer in (`AuthService.issue_web_login`).

Redis holds the single-use marks, keyed on the hash's SHA-256 so the hash itself never appears in a Redis command
(or in a trace of one), and the per-account counter (tier SECURITY): Redis down signs nobody in, and so does a
missing bot token, whose empty key anyone could sign with.
"""

import hashlib
import hmac
import re
from datetime import datetime
from typing import Mapping, Optional

import redis
from flask import current_app

from business_app.models.user import User
from business_app.services.auth_service import may_sign_in
from business_app.services.session_revocation_service import SessionRevocationService
from business_app.services.token_service import TokenService
from business_app.utils.timezone_utils import get_utc_now
from shared.redis_keyspace import RedisKeyspace
from shared.telegram_login import TELEGRAM_LOGIN_FIELDS
from shared.validators import is_merge_tombstone_email

# Fresh means -FUTURE_SKEW_SECONDS <= now - auth_date <= MAX_AGE_SECONDS (a clock a little ahead of ours is allowed).
MAX_AGE_SECONDS = 300
FUTURE_SKEW_SECONDS = 30
# Sign-ins per Telegram account per hour (the route adds a per-IP limit).
PER_TELEGRAM_ID_LIMIT = 10
PER_TELEGRAM_ID_WINDOW_SECONDS = 3600

# Telegram opened the button's URL with no auth fields at all: the customer declined its prompt (LoginUrl). A choice,
# not a forged link.
REFUSED_DECLINED = "declined"
REFUSED_BAD_HASH = "bad_hash"
REFUSED_STALE = "stale"
REFUSED_REPLAYED = "replayed"
REFUSED_NO_ACCOUNT = "no_account"
REFUSED_NOT_CUSTOMER = "not_customer"
REFUSED_REVOKED = "revoked"
REFUSED_RATE_LIMITED = "rate_limited"
REFUSED_UNAVAILABLE = "unavailable"

_HASH = re.compile(r"[0-9a-f]{64}")
_WHOLE_NUMBER = re.compile(r"[0-9]{1,20}")


class TelegramLoginRefused(Exception):
    """`verify_login` signs nobody in; `.code` is one of the REFUSED_* values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _redis() -> redis.Redis:
    return redis.from_url(current_app.config["REDIS_URL"], decode_responses=True)


def _signature(fields: Mapping[str, str], bot_token: str) -> str:
    """Telegram's check: the data-check string (each field as `key=value`, sorted, joined by line feeds), signed
    with HMAC-SHA256 keyed by the SHA-256 digest of the customer bot's token."""
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    return hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _well_formed(hash_hex: Optional[str], fields: Mapping[str, str]) -> bool:
    """A 64-hex hash, a numeric id and time, and no line break in any field: a line break would let one field's
    value pose as further fields of the data-check string."""
    return (
        bool(hash_hex)
        and _HASH.fullmatch(hash_hex) is not None
        and _WHOLE_NUMBER.fullmatch(fields.get("id", "")) is not None
        and _WHOLE_NUMBER.fullmatch(fields.get("auth_date", "")) is not None
        and not any("\n" in value or "\r" in value for value in fields.values())
    )


def verify_login(params: Mapping[str, str], *, now: Optional[datetime] = None) -> User:
    """The customer a Telegram login button's data names, or `TelegramLoginRefused`.

    `params` is the sign-in URL's query (`next` and anything else Telegram does not sign is ignored). In order:
    no hash and no Telegram field at all (declined: the customer declined Telegram's prompt), the hash (bad_hash for
    anything partial, malformed or altered; unavailable without a bot token), freshness (stale), single use
    (replayed; unavailable when Redis is down), ten sign-ins an hour per Telegram account (rate_limited), exactly one
    account that may sign in and is no merge tombstone (no_account), a customer's login (not_customer, the
    storefront's one rule), and no session cut-off after the tap (revoked)."""
    fields = {key: params[key] for key in TELEGRAM_LOGIN_FIELDS if key in params}
    if "hash" not in params and not fields:
        raise TelegramLoginRefused(REFUSED_DECLINED)
    hash_hex = params.get("hash")
    if not _well_formed(hash_hex, fields):
        raise TelegramLoginRefused(REFUSED_BAD_HASH)
    bot_token = current_app.config.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:  # sha256 of an empty token is a key anyone has
        raise TelegramLoginRefused(REFUSED_UNAVAILABLE)
    if not hmac.compare_digest(_signature(fields, bot_token), hash_hex):
        raise TelegramLoginRefused(REFUSED_BAD_HASH)

    telegram_id = int(fields["id"])
    auth_date = int(fields["auth_date"])
    age = (now or get_utc_now()).timestamp() - auth_date
    if not -FUTURE_SKEW_SECONDS <= age <= MAX_AGE_SECONDS:
        raise TelegramLoginRefused(REFUSED_STALE)

    hash_digest = hashlib.sha256(hash_hex.encode("ascii")).hexdigest()
    try:
        r = _redis()
        replay_mark = RedisKeyspace.telegram_login_hash(hash_digest)
        if not r.set(replay_mark, 1, nx=True, ex=MAX_AGE_SECONDS + FUTURE_SKEW_SECONDS):
            raise TelegramLoginRefused(REFUSED_REPLAYED)
        # One transaction, so the counter never lives on without its hour: the window starts at the first sign-in (NX).
        attempts_key = RedisKeyspace.telegram_login_attempts(telegram_id)
        pipe = r.pipeline(transaction=True)
        pipe.incr(attempts_key)
        pipe.expire(attempts_key, PER_TELEGRAM_ID_WINDOW_SECONDS, nx=True)
        attempts, _ = pipe.execute()
    except redis.RedisError:
        raise TelegramLoginRefused(REFUSED_UNAVAILABLE) from None
    if attempts > PER_TELEGRAM_ID_LIMIT:
        raise TelegramLoginRefused(REFUSED_RATE_LIMITED)

    rows = User.query.filter(User.telegram_id == str(telegram_id)).limit(2).all()
    if len(rows) != 1 or is_merge_tombstone_email(rows[0].email) or not may_sign_in(rows[0]):
        raise TelegramLoginRefused(REFUSED_NO_ACCOUNT)
    user = rows[0]
    if not TokenService.storefront_may_renew(user):
        raise TelegramLoginRefused(REFUSED_NOT_CUSTOMER)
    if SessionRevocationService.is_revoked({"sub": str(user.id), "iat": auth_date}):
        raise TelegramLoginRefused(REFUSED_REVOKED)
    return user
