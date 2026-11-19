"""The "Open on website" sign-in check (spec 2026-10-27 §9, §17 Q5; step 4 Task 16a).

The bot's button is a Telegram login button: Telegram signs the customer's identity at the tap and opens our
`/auth/telegram-login` with it appended. `verify_login` is the one check of that data. The order is part of the
design: the hash is checked before any lookup, and spent before any refusal that could be retried. The signed data
here is made by Telegram's own documented check (`telegram_signed`), pinned by a known-answer vector.
"""

import hashlib
from datetime import UTC, datetime

import pytest
import redis
from flask import current_app
from sqlalchemy import text

from business_app.models.user import User
from business_app.services import session_revocation_service, telegram_login_service
from business_app.services.session_revocation_service import REVOKED_BY_PASSWORD_RESET, SessionRevocationService
from business_app.services.telegram_login_service import (
    FUTURE_SKEW_SECONDS,
    MAX_AGE_SECONDS,
    PER_TELEGRAM_ID_LIMIT,
    TelegramLoginRefused,
    verify_login,
)
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier
from shared.telegram_login import (
    TELEGRAM_LOGIN_PATH,
    is_allowed_login_target,
    login_url,
    redact_login_query,
    subscription_target,
)
from shared.validators import merge_tombstone_email
from tests.integration.redis_fakes import DownRedis
from tests.integration.telegram_login_builders import telegram_signed

pytestmark = pytest.mark.unit

TELEGRAM_BOT_TOKEN = "123456:TEST-TOKEN"
KAT = {"id": "424242", "first_name": "Dilnoza", "username": "dilnoza_k", "auth_date": "1793000000"}
KAT_HASH = "15d825716776b33196daf7a62664ab869e8d3bf1fc95b44e70cd813144213a99"
AT = datetime(2026, 10, 26, 7, 35, tzinfo=UTC)  # auth_date + 100 s
AUTH_DATE = int(KAT["auth_date"])
PASSWORD = "TestPassword123!"


@pytest.fixture(autouse=True)
def bot_token(app, monkeypatch):
    monkeypatch.setitem(app.config, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)


def _redis():
    return redis.from_url(current_app.config["REDIS_URL"], decode_responses=True)


@pytest.fixture(autouse=True)
def clean_login_keys(app):
    """The replay marks and the per-account counters this module writes, gone before and after each test."""

    def _clean():
        r = _redis()
        for key in list(r.scan_iter(match="bs:telegram_login_*")):
            r.delete(key)

    _clean()
    yield
    _clean()


def _user(db, **fields):
    values = {
        "email": "kamola@example.com",
        "phone": "+998901234567",
        "password_hash": hash_password(PASSWORD),
        "first_name": "Kamola",
        "last_name": "Karimova",
        "user_type": UserType.INDIVIDUAL,
        "role": UserRole.CUSTOMER,
        "status": UserStatus.ACTIVE,
        "telegram_id": KAT["id"],
        "is_verified": True,
        **fields,
    }
    user = User(**values)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def customer_424242(db):
    """A customer whose Telegram account is 424242, with a website password too."""
    return _user(db)


def _outcome(change=None, *, now=AT):
    """The signed-in user, or the refusal's code. `change` replaces fields of the known-answer data (None drops
    one)."""
    params = {key: value for key, value in {**KAT, "hash": KAT_HASH, **(change or {})}.items() if value is not None}
    try:
        return verify_login(params, now=now)
    except TelegramLoginRefused as refused:
        return refused.code


def _code(change=None, *, now=AT):
    outcome = _outcome(change, now=now)
    return None if isinstance(outcome, User) else outcome


def _replay_mark(hash_hex=KAT_HASH):
    """The replay mark's key: the hash's SHA-256, never the hash, so no Redis command (or trace of one) carries it."""
    return RedisKeyspace.telegram_login_hash(hashlib.sha256(hash_hex.encode("ascii")).hexdigest())


def _spent(hash_hex=KAT_HASH):
    return bool(_redis().exists(_replay_mark(hash_hex)))


# --------------------------------------------------------------------------- the signature


def test_the_signer_matches_the_known_answer():
    assert telegram_signed(KAT, TELEGRAM_BOT_TOKEN)["hash"] == KAT_HASH


def test_telegram_signed_data_names_the_customer(customer_424242):
    signed_in = verify_login({**KAT, "hash": KAT_HASH, "next": "/my-account"}, now=AT)  # `next` is not signed

    assert signed_in == customer_424242


@pytest.mark.parametrize(
    "change",
    [
        {"first_name": "Dilnoza2"},
        {"last_name": "Evil"},
        {"hash": KAT_HASH.upper()},
        {"hash": KAT_HASH[:-1]},
        {"hash": None},
    ],
    ids=["changed-field", "added-field", "uppercase-hash", "short-hash", "no-hash"],
)
def test_a_changed_or_added_field_breaks_the_hash(customer_424242, change):
    assert _code(change) == "bad_hash"
    assert not _spent() and not _spent(KAT_HASH.upper())  # checked before anything is spent
    assert _outcome() == customer_424242  # so the genuine data still signs in


@pytest.mark.parametrize("missing", ["id", "auth_date"])
def test_data_without_its_id_or_its_time_is_refused_as_a_bad_hash(customer_424242, missing):
    signed = telegram_signed({key: value for key, value in KAT.items() if key != missing}, TELEGRAM_BOT_TOKEN)

    with pytest.raises(TelegramLoginRefused) as refused:
        verify_login(signed, now=AT)

    assert refused.value.code == "bad_hash"


@pytest.mark.parametrize("params", [{}, {"next": "/my-account"}, {"next": "/my-account", "lang": "ru"}])
def test_a_url_with_no_hash_and_no_telegram_field_is_a_declined_prompt(customer_424242, monkeypatch, params):
    """Telegram opens the button's URL without the auth fields when the customer declines its prompt. It is told
    apart from a forged link before anything else (even a missing bot token), and spends nothing."""
    monkeypatch.setitem(current_app.config, "TELEGRAM_BOT_TOKEN", "")

    with pytest.raises(TelegramLoginRefused) as refused:
        verify_login(params, now=AT)

    assert refused.value.code == "declined"
    assert not list(_redis().scan_iter(match="bs:telegram_login_*"))


@pytest.mark.parametrize(
    "params",
    [{"id": "424242"}, {"auth_date": KAT["auth_date"]}, {"hash": KAT_HASH}, {"hash": ""}, {"photo_url": "x"}],
    ids=["id-only", "auth-date-only", "hash-only", "empty-hash", "photo-only"],
)
def test_any_hash_or_telegram_field_without_the_rest_is_a_bad_hash(customer_424242, params):
    with pytest.raises(TelegramLoginRefused) as refused:
        verify_login({"next": "/my-account", **params}, now=AT)

    assert refused.value.code == "bad_hash"


def test_data_signed_with_another_bot_token_is_refused(customer_424242):
    signed = telegram_signed(KAT, "654321:ANOTHER-BOT")

    with pytest.raises(TelegramLoginRefused) as refused:
        verify_login(signed, now=AT)

    assert refused.value.code == "bad_hash"


@pytest.mark.parametrize(
    "change",
    [{"first_name": "Ali\nid=515151"}, {"last_name": "Valiyev\r"}, {"username": "ali\r\nx"}],
    ids=["line-feed", "carriage-return", "both"],
)
def test_a_line_break_in_any_field_is_refused_even_when_signed(customer_424242, change):
    """A line break would let one field's value pose as more fields of the data-check string. The data here is
    genuinely signed, so only the explicit refusal stops it."""
    signed = telegram_signed({**KAT, **change}, TELEGRAM_BOT_TOKEN)

    with pytest.raises(TelegramLoginRefused) as refused:
        verify_login(signed, now=AT)

    assert refused.value.code == "bad_hash"
    assert not _spent(signed["hash"])


@pytest.mark.parametrize("token", ["", None], ids=["empty", "missing"])
def test_without_a_bot_token_nobody_signs_in(customer_424242, monkeypatch, token):
    """The key would be the SHA-256 of an empty token, which anyone can compute: fail closed."""
    monkeypatch.setitem(current_app.config, "TELEGRAM_BOT_TOKEN", token)
    forged = telegram_signed(KAT, "")

    with pytest.raises(TelegramLoginRefused) as refused:
        verify_login(forged, now=AT)

    assert refused.value.code == "unavailable"
    assert not _spent(forged["hash"])


# --------------------------------------------------------------------------- fresh, once


@pytest.mark.parametrize("seconds,code", [(300, None), (301, "stale"), (-30, None), (-31, "stale")])
def test_fresh_means_five_minutes_with_thirty_seconds_of_skew(customer_424242, seconds, code):
    assert (MAX_AGE_SECONDS, FUTURE_SKEW_SECONDS) == (300, 30)

    assert _code(now=datetime.fromtimestamp(AUTH_DATE + seconds, UTC)) == code
    assert _spent() is (code is None)  # a stale tap spends nothing


def test_a_hash_is_good_once(customer_424242):
    assert _outcome() == customer_424242
    assert _code() == "replayed"

    ttl = _redis().ttl(_replay_mark())
    assert 0 < ttl <= MAX_AGE_SECONDS + FUTURE_SKEW_SECONDS == 330
    assert not [key for key in _redis().scan_iter(match="*") if KAT_HASH in key]
    assert KEYSPACE_TIERS["telegram_login_hash"] is RedisUsageTier.TIER_SECURITY


def test_redis_down_refuses(customer_424242, monkeypatch):
    monkeypatch.setattr(telegram_login_service, "_redis", lambda: DownRedis())

    assert _code() == "unavailable"


# --------------------------------------------------------------------------- who may sign in


def _account(db, account):
    """The account behind Telegram 424242 in each case."""
    if account == "none":
        return _user(db, telegram_id="515151")  # somebody else's Telegram
    if account == "two_matches":
        # The unique index makes two impossible today; the check must not depend on it.
        db.session.execute(text("DROP INDEX ix_users_telegram_id"))
        _user(db, email="other@example.com", phone="+998907654321")
        return _user(db)
    fields = {
        "inactive": {"status": UserStatus.INACTIVE},
        "banned": {"status": UserStatus.BANNED},
        "pending_verification": {"status": UserStatus.PENDING_VERIFICATION},
        "staff_driver": {"role": UserRole.DELIVERY_DRIVER, "user_type": UserType.STAFF},
        "role_admin": {"role": UserRole.ADMIN},
        "customer_with_staff_roles_typed_individual": {"staff_roles": ["delivery_driver"]},
        "entity_customer": {"user_type": UserType.ENTITY},
    }.get(account, {})
    user = _user(db, **fields)
    if account == "tombstone":  # what an account merge leaves on the account it emptied
        user.email = merge_tombstone_email(user.id)
        db.session.commit()
    return user


@pytest.mark.parametrize(
    "account,code",
    [
        ("none", "no_account"),
        ("inactive", "no_account"),
        ("banned", "no_account"),
        ("tombstone", "no_account"),
        ("two_matches", "no_account"),
        ("staff_driver", "not_customer"),
        ("pending_verification", None),
        ("role_admin", "not_customer"),
        ("customer_with_staff_roles_typed_individual", "not_customer"),
        ("entity_customer", None),
    ],
)
def test_who_may_sign_in(db, account, code):
    """The not_customer rows follow `TokenService.storefront_may_renew`, the rule the quiet refresh uses."""
    user = _account(db, account)

    outcome = _outcome()

    assert outcome == (code or user)


# --------------------------------------------------------------------------- the session cut-off


def _cut_off_at(monkeypatch, db, user, ms):
    """Every login of `user` ended at `ms` (a password reset), through the one writer."""
    monkeypatch.setattr(session_revocation_service, "_now_ms", lambda: ms)
    SessionRevocationService.revoke_all(user.id, reason=REVOKED_BY_PASSWORD_RESET)
    db.session.commit()


def test_a_session_cut_off_newer_than_the_tap_refuses_it(db, customer_424242, monkeypatch):
    _cut_off_at(monkeypatch, db, customer_424242, (AUTH_DATE + 1) * 1000)

    assert _code() == "revoked"


def test_a_cut_off_in_the_taps_own_second_does_not(db, customer_424242, monkeypatch):
    _cut_off_at(monkeypatch, db, customer_424242, AUTH_DATE * 1000 + 999)

    assert _outcome() == customer_424242


# --------------------------------------------------------------------------- the per-account limit


def test_ten_sign_ins_an_hour_per_telegram_account(customer_424242):
    assert PER_TELEGRAM_ID_LIMIT == 10
    taps = [telegram_signed({**KAT, "auth_date": str(AUTH_DATE - second)}, TELEGRAM_BOT_TOKEN) for second in range(11)]

    outcomes = []
    for tap in taps:
        try:
            outcomes.append(verify_login(tap, now=AT))
        except TelegramLoginRefused as refused:
            outcomes.append(refused.code)

    assert outcomes == [customer_424242] * 10 + ["rate_limited"]
    ttl = _redis().ttl(RedisKeyspace.telegram_login_attempts(424242))
    assert 0 < ttl <= 3600
    assert KEYSPACE_TIERS["telegram_login_attempts"] is RedisUsageTier.TIER_SECURITY


def test_the_counter_always_has_its_hour(customer_424242):
    """The count and its expiry are written together, and the expiry is set whenever the counter has none: a counter
    left without one (a write cut short) would refuse the account for good once it passed ten."""
    attempts = RedisKeyspace.telegram_login_attempts(424242)
    _redis().set(attempts, 3)  # no expiry

    assert _outcome() == customer_424242

    assert _redis().get(attempts) == "4"
    assert 0 < _redis().ttl(attempts) <= 3600


# --------------------------------------------------------------------------- the shared words


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "https://aqua-element.uz/auth/telegram-login?next=%2Fmy-account&id=424242&first_name=Dilnoza&hash=ab",
            "https://aqua-element.uz/auth/telegram-login?id=424242",
        ),
        ('"GET /auth/telegram-login?hash=ab&username=x HTTP/1.1" 200', '"GET /auth/telegram-login HTTP/1.1" 200'),
        (
            "Referer: https://aqua-element.uz/auth/telegram-login?id=424242&first_name=O'tkir&hash=ab; next",
            "Referer: https://aqua-element.uz/auth/telegram-login?id=424242 next",
        ),
        ("/my-orders?page=2", "/my-orders?page=2"),
        (None, None),
    ],
)
def test_redact_login_query(text, expected):
    assert redact_login_query(text) == expected


def test_the_bot_builds_its_button_url_from_the_allowlist():
    assert subscription_target(7) == "/my-subscriptions?sub=7"
    assert login_url("https://aqua-element.uz/", subscription_target(7)) == (
        f"https://aqua-element.uz{TELEGRAM_LOGIN_PATH}?next=%2Fmy-subscriptions%3Fsub%3D7"
    )
    assert all(is_allowed_login_target(target) for target in ("/my-subscriptions", "/my-orders", "/my-account"))
    for refused in ("/admin", "/my-subscriptions?sub=0", "/my-subscriptions?sub=7\n", "//evil.example", None):
        assert not is_allowed_login_target(refused)
        with pytest.raises(ValueError):
            login_url("https://aqua-element.uz", refused)
