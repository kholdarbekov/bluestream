"""Sign in and ask for a new password with the phone number in any common format (owner decision 2026-11-14, unit
"phone": "be robust with the phone number input ... we only support Uzbekistan numbers").

Driven through the endpoints the pages call, with the payloads they send (login.js posts {identifier, password} to
/api/v1/auth/login; forgot-password.js and account-security.js post {identifier} to /api/v1/auth/forgot-password):

- what a customer types is read once into one canonical identifier: an address lowercased, anything else through
  the shared phone normaliser (shared/validators.py), so "90 123 45 67", "8 90 123 45 67" or "00998901234567" find
  the account stored as +998901234567;
- the lockout counts every wrong password against the account the identifier names, so changing the format, or
  switching between the address and the number, does not buy more tries, and the admin unlock clears it;
- a phone stored before every writer saved E.164 ("998901234567", "+998 90 123 45 67") is found in any format too,
  and an identifier that matched a stored phone exactly still finds that same account;
- an unknown or unreadable identifier gets exactly the answer a wrong password gets (no enumeration), and the reset
  request answers the same for every identifier.
"""

import json

import pytest

from business_app.models.user import User
from business_app.tasks.notification_tasks import send_account_locked_notification_task
from business_app.utils.password_security import hash_password
from business_app.utils.service_factory import get_auth_service
from shared.enums import UserRole, UserType
from shared.redis_keyspace import RedisKeyspace
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, emailed_reset_links  # noqa: F401

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
FORGOT_URL = "/api/v1/auth/forgot-password"
PASSWORD = "TestPassword123!"  # sample_user's (tests/conftest.py), stored with phone +998901234567
WRONG = "NotThePassword1!"

# The formats customers type for +998901234567, the stored form last.
FORMATS = (
    "+998 90 123 45 67",
    "998901234567",
    "90 123 45 67",
    "(90) 123-45-67",
    "8 90 123 45 67",
    "00998901234567",
    "+998901234567",
)

# Nothing to find: a free valid number (in two formats), an unreadable string, a landline, an unknown address.
UNKNOWN = ("+998 90 000 00 01", "8 90 000 00 01", "12345", "8 71 123 45 67", "nobody@example.com", "not a phone")


def _login(app, identifier, password):
    return app.test_client().post(
        LOGIN_URL, headers=WEB_HEADERS, data=json.dumps({"identifier": identifier, "password": password})
    )


def _forgot(app, identifier):
    return app.test_client().post(FORGOT_URL, headers=WEB_HEADERS, data=json.dumps({"identifier": identifier}))


def _refusal(response):
    body = response.get_json()
    return response.status_code, body["message"], (body.get("data") or {}).get("error_code")


@pytest.fixture
def locked_notices(monkeypatch):
    """The lockout notices the login queues, as their keyword arguments. Patched on the task instance's `__dict__`
    with the task's own signature, so a changed call fails here."""
    sent = []

    def delay(user_id, lockout_until, lockout_minutes):
        sent.append({"user_id": user_id, "lockout_until": lockout_until, "lockout_minutes": lockout_minutes})

    monkeypatch.setitem(vars(send_account_locked_notification_task), "delay", delay)
    return sent


@pytest.fixture
def three_tries(app, monkeypatch):
    """Three wrong passwords lock the account for ten minutes. TestingConfig's one-second lockout would expire
    between the requests; AuthService reads both values from the config per request."""
    monkeypatch.setitem(app.config, "MAX_LOGIN_ATTEMPTS", 3)
    monkeypatch.setitem(app.config, "LOCKOUT_DURATION", 600)


def _attempt_keys(app):
    """Every wrong-password counter in Redis: the accounts' and the unknown identifiers'."""
    patterns = (RedisKeyspace.sign_in_attempts("*"), RedisKeyspace.sign_in_attempts_unknown("*"))
    with app.app_context():
        redis_client = get_auth_service().redis_client
        return sorted(key.decode() for pattern in patterns for key in redis_client.keys(pattern))


def _add_customer(db, phone, email, password=PASSWORD):
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password(password),
        first_name="Other",
        last_name="Customer",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _store_phone(db, user, stored):
    """Store the phone as the old /verify-phone did, exactly as it was typed."""
    user.phone = stored
    db.session.commit()


# ------------------------------------------------------------------------------------------------------- login


@pytest.mark.parametrize("typed", FORMATS)
def test_the_number_signs_in_in_every_common_format(app, db, sample_user, typed):
    response = _login(app, typed, PASSWORD)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["user"]["id"] == sample_user.id
    assert "access_token_cookie" in response.headers.get("Set-Cookie", "")


def test_the_address_signs_in_whatever_its_case(app, db, sample_user):
    response = _login(app, "  Test@Example.COM ", PASSWORD)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["user"]["id"] == sample_user.id


@pytest.mark.parametrize("typed", UNKNOWN)
def test_an_unknown_or_unreadable_identifier_gets_the_wrong_password_answer(app, db, sample_user, typed):
    wrong_password = _login(app, sample_user.phone, WRONG)
    unknown = _login(app, typed, PASSWORD)

    assert _refusal(wrong_password)[0] == 401
    assert _refusal(wrong_password)[2] == "INVALID_CREDENTIALS"
    assert _refusal(unknown) == _refusal(wrong_password)


# ------------------------------------------------------------------------------------------------------ lockout


def test_wrong_passwords_in_mixed_formats_count_against_one_lockout(app, db, sample_user, three_tries, locked_notices):
    """Before, each format had its own counter: three formats were three fresh sets of tries."""
    for typed in ("+998 90 123 45 67", "8 90 123 45 67", "(90) 123-45-67"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401

    assert _attempt_keys(app) == [RedisKeyspace.sign_in_attempts(sample_user.id)]

    # Locked now: the right password in a fourth format, or the account's address, is refused.
    assert _login(app, "998901234567", PASSWORD).status_code == 423
    assert _login(app, sample_user.email, PASSWORD).status_code == 423

    db.session.expire_all()
    assert db.session.get(User, sample_user.id).account_locked_until is not None
    assert [notice["user_id"] for notice in locked_notices] == [sample_user.id]
    assert locked_notices[0]["lockout_minutes"] == 10


def test_an_address_in_any_case_counts_against_one_lockout(app, db, sample_user, three_tries, locked_notices):
    for typed in ("TEST@example.com", "test@EXAMPLE.com", " Test@Example.Com"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401

    assert _attempt_keys(app) == [RedisKeyspace.sign_in_attempts(sample_user.id)]
    assert _login(app, "test@example.com", PASSWORD).status_code == 423


def test_the_address_and_the_number_share_one_budget(app, db, sample_user, three_tries, locked_notices):
    """Review finding phone-2: the address and the number each had their own counter, so with a limit of three a
    caller got four wrong tries before any lock (eight with the default five)."""
    answers = [
        _login(app, typed, WRONG).status_code
        for typed in ("+998 90 123 45 67", "8 90 123 45 67", "TEST@example.com", "test@EXAMPLE.com")
    ]

    assert answers == [401, 401, 401, 423]
    assert _login(app, "90 123 45 67", PASSWORD).status_code == 423
    assert _login(app, "test@example.com", PASSWORD).status_code == 423
    assert [notice["user_id"] for notice in locked_notices] == [sample_user.id]


def test_a_right_password_by_phone_clears_the_tries_made_with_the_address(app, db, sample_user, three_tries):
    for typed in ("TEST@example.com", "test@example.com"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401

    assert _login(app, "90 123 45 67", PASSWORD).status_code == 200
    assert _attempt_keys(app) == []

    # A fresh budget: two more wrong tries, by address and by number, do not lock the account.
    for typed in ("Test@Example.com", "8 90 123 45 67"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401
    assert _login(app, "test@example.com", PASSWORD).status_code == 200


def test_an_unknown_identifier_spends_its_own_budget_never_an_accounts(
    app, db, sample_user, three_tries, locked_notices
):
    """Nothing typed names an account's counter, not even that counter's own key name; an unknown identifier still
    locks like a known one, so the lock tells nothing about whether an account exists."""
    account_key = RedisKeyspace.sign_in_attempts(sample_user.id)
    for _ in range(3):
        assert _refusal(_login(app, account_key, WRONG))[0] == 401
    for typed in ("+998 90 000 00 01", "8 90 000 00 01"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401

    assert _login(app, account_key, WRONG).status_code == 423
    assert _attempt_keys(app) == sorted(
        [RedisKeyspace.sign_in_attempts_unknown(account_key), RedisKeyspace.sign_in_attempts_unknown("+998900000001")]
    )
    assert _login(app, "+998 90 123 45 67", PASSWORD).status_code == 200
    assert locked_notices == []


def test_a_right_password_in_any_format_clears_the_count(app, db, sample_user, three_tries):
    for typed in ("+998 90 123 45 67", "8 90 123 45 67"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401

    assert _login(app, "90 123 45 67", PASSWORD).status_code == 200
    assert _attempt_keys(app) == []

    # A fresh count: two more wrong tries do not lock the account.
    for typed in ("00998901234567", "(90) 123-45-67"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401
    assert _login(app, "+998901234567", PASSWORD).status_code == 200


def test_the_admin_unlock_clears_a_lockout_earned_with_the_phone(
    app, db, sample_user, admin_claim_headers, three_tries, locked_notices
):
    """The unlock cleared only the account's email counters, so a customer locked out by phone stayed locked."""
    for typed in ("+998 90 123 45 67", "8 90 123 45 67", "90 123 45 67"):
        _login(app, typed, WRONG)
    assert _login(app, "+998901234567", PASSWORD).status_code == 423

    unlocked = app.test_client().post(f"/api/v1/admin/users/{sample_user.id}/unlock", headers=admin_claim_headers)

    assert unlocked.status_code == 200, unlocked.get_json()
    assert _attempt_keys(app) == []
    assert _login(app, "(90) 123-45-67", PASSWORD).status_code == 200


# --------------------------------------------------------------------------------- phones stored before E.164

# Review finding phone-1: before ce286c6 the /verify-phone page saved the number exactly as typed (any form its
# validator took: the 998 code with any spacing), and no migration rewrote those rows.
LEGACY_STORED = ("998901234567", "+998 90 123 45 67")


@pytest.mark.parametrize("stored", LEGACY_STORED)
@pytest.mark.parametrize("typed", FORMATS + LEGACY_STORED)
def test_a_phone_stored_as_typed_signs_in_in_every_common_format(app, db, sample_user, stored, typed):
    _store_phone(db, sample_user, stored)

    response = _login(app, typed, PASSWORD)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["user"]["id"] == sample_user.id


def test_wrong_passwords_lock_a_phone_stored_as_typed_whatever_the_format(
    app, db, sample_user, three_tries, locked_notices
):
    _store_phone(db, sample_user, "+998 90 123 45 67")

    for typed in ("90 123 45 67", "8 90 123 45 67", "+998 90 123 45 67"):
        assert _refusal(_login(app, typed, WRONG))[0] == 401

    assert _login(app, "+998901234567", PASSWORD).status_code == 423
    assert [notice["user_id"] for notice in locked_notices] == [sample_user.id]


def test_a_number_typed_exactly_as_stored_still_finds_that_account_first(app, db, sample_user):
    """Two accounts on one number, one stored E.164 and one as typed, predate the phone-holder checks. The exact
    match, the only match before any format was read, keeps finding the account it found; other formats find the
    E.164 one."""
    other = _add_customer(db, "+998 90 123 45 67", "other@example.com", password="OtherPassword123!")

    exact = _login(app, "+998 90 123 45 67", "OtherPassword123!")
    assert exact.status_code == 200, exact.get_json()
    assert exact.get_json()["data"]["user"]["id"] == other.id

    for typed in ("90 123 45 67", "8 90 123 45 67", "+998901234567"):
        response = _login(app, typed, PASSWORD)
        assert response.status_code == 200, (typed, response.get_json())
        assert response.get_json()["data"]["user"]["id"] == sample_user.id


@pytest.mark.parametrize("stored", LEGACY_STORED)
def test_the_reset_link_goes_out_for_a_phone_stored_as_typed(
    app, db, sample_user, emailed_reset_links, stored  # noqa: F811
):
    _store_phone(db, sample_user, stored)

    for typed in ("90 123 45 67", "8 90 123 45 67", "+998901234567"):
        assert _forgot(app, typed).status_code == 200

    assert [user_id for user_id, _token in emailed_reset_links] == [sample_user.id] * 3


# ------------------------------------------------------------------------------------------------ password reset


@pytest.mark.parametrize("typed", FORMATS + ("TEST@Example.com",))
def test_the_reset_link_goes_out_for_the_number_in_every_common_format(
    app, db, sample_user, emailed_reset_links, typed  # noqa: F811
):
    response = _forgot(app, typed)

    assert response.status_code == 200, response.get_json()
    assert [user_id for user_id, _token in emailed_reset_links] == [sample_user.id]


@pytest.mark.parametrize("typed", UNKNOWN)
def test_the_reset_request_answers_the_same_for_an_unknown_identifier(
    app, db, sample_user, emailed_reset_links, typed  # noqa: F811
):
    known = _forgot(app, sample_user.phone)
    unknown = _forgot(app, typed)

    assert (unknown.status_code, unknown.get_json()["message"]) == (known.status_code, known.get_json()["message"])
    assert [user_id for user_id, _token in emailed_reset_links] == [sample_user.id]  # the known request's only
