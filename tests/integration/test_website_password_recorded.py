"""The website password is recorded when a person chooses it (owner decision 2026-11-14: "bot customers: record users
setting a website password").

`auth_service.has_website_password`, the one rule, used to guess from the stored hash and the registration source, so a
bot signup that set a password through the emailed link still counted as having none: /account-security kept its
change form from it (review ACC-R09), and a merge treated its account as bringing no website login, dropping a real
password. The rule now reads `users.password_set_at`, which every door where someone types a password stamps
(`AuthService._set_chosen_password`) and a merge carries with the password; a door that stores a secret nobody chose
never stamps it, and neither does the bcrypt re-hash at sign-in.

Every request goes the way its caller sends it: the storefront's apiRequest (cookies + `X-CSRF-TOKEN`, JSON,
`X-Platform: web`), the customer bot's signed POSTs, the admin panel's role-claim token. Only I/O is replaced: the
reset email and the verification SMS by recorders with the tasks' signatures, the SMS provider by a fake.
"""

import json
from datetime import UTC, datetime

import bcrypt
import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text

from business_app.models.user import User
from business_app.services.auth_service import has_website_password
from business_app.services.cross_platform_sync_service import CrossPlatformSyncService
from business_app.utils.password_security import hash_password, verify_password
from tests.integration.test_customer_telegram_link_api import (  # noqa: F401  (fixtures: program, sms)
    BOT_SIGNUPS,
    HOLDER_PASSWORD,
    _claim_by_phone,
    _login,
    program,
    sms,
)
from tests.integration.reorder_reminder_builders import customer_headers
from tests.integration.test_web_account_settings import _page, _page_data, settings_copy  # noqa: F401
from tests.integration.test_web_auth_forms import _clear_rate_limit
from tests.integration.test_web_auth_verify import _browser, _post
from tests.integration.test_web_password_reset_and_change import (  # noqa: F401  (fixture: emailed_reset_links)
    RESET_URL,
    WEB_HEADERS,
    emailed_reset_links,
)
from tests.integration.test_web_phone_signup_e2e import _sent_message, captured_sms  # noqa: F401

pytestmark = pytest.mark.integration

BOT_EMAIL = "kamola@example.com"  # the real address an admin typed on the bot signup
CHOSEN = "KamolaSets789!"
CHOSEN_AGAIN = "KamolaChanges321!"
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def _utc(moment):
    """SQLite hands a timestamptz back without its zone; the app writes UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _fresh(db, user_id):
    db.session.expire_all()
    return db.session.get(User, user_id)


def _bot_signup_with_a_real_email(db, signed_bot_post, door):
    signup = BOT_SIGNUPS[door](signed_bot_post)
    signup.email = BOT_EMAIL
    db.session.commit()
    return signup


def _set_password_through_the_emailed_link(app, signup, emailed_reset_links):
    """The no-password panel's button (account-security.js: forgot-password with page data link_email), then the
    reset page from a browser holding no login, with the token from the email."""
    soup, _html = _page(app, signup, "/account-security", "en")
    browser, csrf = _browser(app, signup)
    asked = _post(browser, csrf, "/auth/forgot-password", {"identifier": _page_data(soup)["link_email"]})
    assert asked.status_code == 200, asked.get_json()
    [(user_id, token)] = emailed_reset_links
    assert user_id == signup.id
    reset = app.test_client().post(
        RESET_URL, headers=WEB_HEADERS, data=json.dumps({"token": token, "new_password": CHOSEN})
    )
    assert reset.status_code == 200, reset.get_json()


# ------------------------------------------------------------------------------------- the doors that stamp, or not


def test_an_email_sign_up_records_the_password_it_chose(app, db):
    """register.js's request: POST /api/v1/auth/register with the details and the page's language."""
    _clear_rate_limit(app, "register")

    answer = app.test_client().post(
        "/api/v1/auth/register",
        headers=WEB_HEADERS,
        data=json.dumps({"first_name": "Dilnoza", "last_name": "", "password": "Abcdefg1", "referral_code": None,
                         "email": "dilnoza.pw@example.com", "preferred_language": "uz"}),
    )

    assert answer.status_code == 201, answer.get_json()
    user = User.query.filter_by(email="dilnoza.pw@example.com").one()
    assert user.password_set_at is not None and has_website_password(user) is True


def test_a_phone_sign_up_records_the_password_it_chose(client, db, captured_sms):
    """The phone sign-up page: the code read off the SMS (provider faked), then the details with the password."""
    import re

    phone = "+998901112277"
    init = client.post("/api/v1/auth/phone/register/init", json={"phone": phone, "preferred_language": "ru"})
    assert init.status_code == 200, init.get_json()
    otp_code = re.search(r"(\d{4,8})\s*$", _sent_message(captured_sms)).group(1)

    verify = client.post(
        "/api/v1/auth/phone/register/verify",
        json={"phone": phone, "otp_code": otp_code, "first_name": "Web", "last_name": "", "password": "StrongPass123!"},
    )

    assert verify.status_code in (200, 201), verify.get_json()
    assert has_website_password(User.query.filter_by(phone=phone).one()) is True


def test_the_first_admin_the_cli_creates_records_the_password_typed_for_it(app, db):
    """`python scripts/admin_cli.py create-admin` (and seed_admin.py) go through AuthService.create_admin_user with the
    password the operator typed. The admin panel's admin/create-user reaches it only while no admin exists, which its
    own admin check rules out, so the CLI is the caller to drive."""
    from scripts.admin_cli import create_admin_user

    asked_at = datetime.now(UTC)
    created = create_admin_user(app, "+998901234580", "Owner.Admin@example.com", "OwnerTyped123!", "Owner", "Admin")
    answered_at = datetime.now(UTC)

    assert created is not None
    admin = User.query.filter_by(email="owner.admin@example.com").one()
    assert verify_password("OwnerTyped123!", admin.password_hash)
    assert asked_at <= _utc(admin.password_set_at) <= answered_at
    assert has_website_password(admin) is True


@pytest.mark.parametrize("door", list(BOT_SIGNUPS))
def test_a_bot_signup_records_no_password(db, signed_bot_post, door):
    """telegram-register stores the placeholder, the telegram-login signup the hash of a random token nobody knows."""
    signup = BOT_SIGNUPS[door](signed_bot_post)

    assert signup.password_set_at is None and has_website_password(signup) is False


def test_a_customer_the_admin_panel_creates_records_no_password(app, db, admin_user):
    """The admin panel's "new customer" (call-centre orders): a random secret nobody knows, so no website password."""
    admin_user.status = "active"
    db.session.commit()
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})

    resp = app.test_client().post(
        "/api/v1/admin/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"phone": "+998901234575", "first_name": "Gulnora"},
    )

    assert resp.status_code == 201, resp.get_json()
    created = User.query.filter_by(phone="+998901234575").one()
    assert (created.registration_source, created.password_set_at) == ("admin_created", None)
    assert has_website_password(created) is False


def test_the_bcrypt_rehash_at_sign_in_keeps_the_stamp(app, db, sample_user):
    """A sign-in re-hashes a password stored at other bcrypt rounds: the same password, so its date stays."""
    chosen_on = datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    sample_user.password_hash = bcrypt.hashpw(b"TestPassword123!", bcrypt.gensalt(rounds=5)).decode()
    sample_user.password_set_at = chosen_on
    db.session.commit()
    old_hash = sample_user.password_hash

    login = _login(app, sample_user.email, "TestPassword123!")

    assert login.status_code == 200, login.get_json()
    user = _fresh(db, sample_user.id)
    assert user.password_hash != old_hash and verify_password("TestPassword123!", user.password_hash)
    assert user.password_set_at.replace(tzinfo=UTC) == chosen_on


# -------------------------------------------------------------------------------------------- /account-security


@pytest.mark.parametrize("door", list(BOT_SIGNUPS))
def test_a_bot_signup_that_sets_its_password_through_the_emailed_link_gets_the_change_form(
    app, db, settings_copy, signed_bot_post, emailed_reset_links, door
):  # ACC-R09
    signup = _bot_signup_with_a_real_email(db, signed_bot_post, door)

    _set_password_through_the_emailed_link(app, signup, emailed_reset_links)

    signup = _fresh(db, signup.id)
    set_at = signup.password_set_at
    assert set_at is not None and has_website_password(signup) is True
    for lang in ("uz", "ru", "en"):
        soup, _html = _page(app, signup, "/account-security", lang)
        assert soup.select_one("#changePasswordForm") is not None, lang
        assert soup.select_one('[data-password-state="none"]') is None, lang
        assert soup.select_one('[data-signin="password"]') is not None, lang
        assert _page_data(soup)["link_email"] is None, lang

    # The form's request (account-security.js) works with the password just set, and dates the new one: the stamp
    # is moved back first, so only a change that writes a fresh one lands inside the request's window.
    signup.password_set_at = LONG_AGO
    db.session.commit()
    browser, csrf = _browser(app, signup)
    asked_at = datetime.now(UTC)
    changed = _post(browser, csrf, "/auth/change-password", {"current_password": CHOSEN, "new_password": CHOSEN_AGAIN})
    answered_at = datetime.now(UTC)
    assert changed.status_code == 200, changed.get_json()
    signup = _fresh(db, signup.id)
    assert verify_password(CHOSEN_AGAIN, signup.password_hash)
    assert asked_at <= _utc(signup.password_set_at) <= answered_at

    # The platform status (GET /auth/platform-status, the bot's JWT) says the same thing.
    status = app.test_client().get("/api/v1/auth/platform-status", headers=customer_headers(app, signup))
    assert status.get_json()["data"]["platform_status"]["platforms"]["web"]["can_login"] is True


@pytest.mark.parametrize("door", list(BOT_SIGNUPS))
def test_a_bot_signup_that_never_set_a_password_still_gets_the_set_a_password_panel(
    app, db, settings_copy, signed_bot_post, door
):
    signup = _bot_signup_with_a_real_email(db, signed_bot_post, door)

    soup, _html = _page(app, signup, "/account-security", "ru")

    assert soup.select_one("#changePasswordForm") is None
    panel = soup.select_one('[data-password-state="none"]')
    assert panel is not None and panel.select_one("#passwordLinkForm") is not None
    assert _page_data(soup)["link_email"] == BOT_EMAIL


# ---------------------------------------------------------------------------------------------------------- merges


def test_a_bot_signup_brings_the_password_it_set_into_the_phone_link_merge(
    app, db, program, settings_copy, second_sample_user, signed_bot_post, sms, emailed_reset_links
):  # R8b: an account with a website login brings it, now also a bot signup that set one
    signup = _bot_signup_with_a_real_email(db, signed_bot_post, "telegram-register")
    _set_password_through_the_emailed_link(app, signup, emailed_reset_links)
    set_at = _fresh(db, signup.id).password_set_at
    holder = second_sample_user
    holder.password_hash = hash_password(HOLDER_PASSWORD)
    db.session.commit()
    holder_id, holder_email, signup_id = holder.id, holder.email, signup.id

    _claim_by_phone(signed_bot_post, sms, holder.phone)

    survivor = _fresh(db, holder_id)
    assert db.session.get(User, signup_id) is None
    assert (survivor.email, survivor.password_set_at) == (BOT_EMAIL, set_at)
    login = _login(app, BOT_EMAIL, CHOSEN)
    assert (login.status_code, login.get_json()["data"]["user"]["id"]) == (200, holder_id)
    assert _login(app, holder_email, HOLDER_PASSWORD).status_code == 401
    soup, _html = _page(app, survivor, "/account-security", "en")
    assert soup.select_one("#changePasswordForm") is not None


def test_a_telegram_account_that_takes_a_website_accounts_password_takes_its_stamp(
    app, db, second_sample_user, signed_bot_post
):
    """The merge that keeps the Telegram account and deletes the website one. No HTTP route reaches it today (the
    phone-link merge always keeps the phone holder), so the service is called the way auto_link_accounts is."""
    signup = BOT_SIGNUPS["telegram-register"](signed_bot_post)
    web = _fresh(db, second_sample_user.id)
    web_email, web_set_at, signup_id = web.email, web.password_set_at, signup.id
    signup = db.session.get(User, signup_id)

    result = CrossPlatformSyncService().auto_link_accounts(primary_user=signup, secondary_user=web, link_type="merge")

    assert result["success"] is True, result
    survivor = _fresh(db, signup_id)
    assert (survivor.email, survivor.password_set_at) == (web_email, web_set_at)
    assert has_website_password(survivor) is True
    login = _login(app, web_email, "TestPassword123!")
    assert (login.status_code, login.get_json()["data"]["user"]["id"]) == (200, signup_id)


# ------------------------------------------------------------------------------------------------- the migration


MIGRATION = "290c1500d908"
BEFORE = "e3b7c1d9a5f4"


def test_the_migration_stamps_exactly_the_accounts_the_old_rule_counted(pg_app, pg_db):
    """Postgres, the real migration: down to the revision before it, then up through it. The old rule counted a
    non-empty hash that is not the bot placeholder, on an account whose source is not 'telegram' (NULL included); each
    such row is stamped with its created_at, every other row stays NULL."""
    from flask_migrate import downgrade, upgrade

    secret = hash_password("random-token-nobody-knows")
    rows = {  # email: (registration_source, password_hash, stamped by the backfill)
        "web@example.com": ("web", secret, True),
        "nosource@example.com": (None, secret, True),
        "admin-created@example.com": ("admin_created", secret, True),
        "agent@example.com": ("sales_agent", secret, True),
        "bot-register@example.com": ("telegram", "telegram_user", False),
        "bot-login@example.com": ("telegram", secret, False),
        "merged-placeholder@example.com": ("email", "telegram_user", False),
        "empty@example.com": ("web", "", False),
    }
    for index, (email, (source, password_hash, _stamped)) in enumerate(rows.items()):
        pg_db.session.add(
            User(
                email=email,
                phone=f"+99890000010{index}",  # NOT NULL in the migrated schema
                password_hash=password_hash,
                registration_source=source,
                created_at=datetime(2026, 1, 1 + index, 8, 0, tzinfo=UTC),
            )
        )
    pg_db.session.commit()
    pg_db.session.remove()

    downgrade(revision=BEFORE)
    upgrade(revision=MIGRATION)

    with pg_db.engine.connect() as conn:
        found = {
            email: (set_at, created)
            for email, set_at, created in conn.execute(
                text("SELECT email, password_set_at, created_at FROM users WHERE email = ANY(:emails)"),
                {"emails": list(rows)},
            )
        }
    assert set(found) == set(rows)
    for email, (_source, _hash, stamped) in rows.items():
        set_at, created = found[email]
        assert (set_at == created) if stamped else (set_at is None), email
