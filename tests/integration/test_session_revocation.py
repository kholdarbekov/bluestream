"""One session-revocation rule (spec 2026-10-19 R9; owner decisions 2026-10-19).

A password reset ends every existing login of the account at once: its refresh tokens stop
refreshing, and its access tokens are refused on their very next request. A login made after that
keeps working, even in the same second (owner decision R9b, 2026-10-20: tokens carry their issue
time in milliseconds). With Redis down, signed-in requests still work (the check fails open), and
revoking fails loudly instead of pretending it worked. There is no "log out of all devices" (owner
decision R10, 2026-10-20): each app logs out only itself.

Every request is sent the way its client sends it: the storefront's sign-in and its
`/auth/refresh-token` body, the httpOnly refresh cookie with its CSRF header on `/auth/refresh-cookie`,
and a Bearer access token on `/auth/profile`; the phone-link merge runs through the customer bot's
signed routes. Only the reset e-mail and SMS tasks and Redis outages are replaced.
"""

import json
import logging
import time

import jwt
import pytest
from flask_jwt_extended import decode_token, get_csrf_token
from flask_jwt_extended.config import config as jwt_config

from business_app.config.production import ProductionConfig
from business_app.models.user import UserSession
from business_app.services.session_revocation_service import ISSUED_AT_MS_CLAIM
from business_app.services.session_revocation_service import logger as revocation_logger
from business_app.services.token_service import TokenService
from business_app.utils.bot_webhook import _get_webhook_signature
from business_app.utils.password_security import hash_password
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401
from tests.integration.test_customer_telegram_link_api import (  # noqa: F401  (three of these are fixtures)
    HOLDER_PASSWORD,
    _bot_signup,
    _claim_by_phone,
    _DownRedis,
    linked_email_only_account,
    program,
    sms,
)
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.test_web_password_reset_and_change import (  # noqa: F401  (emailed_reset_links is a fixture)
    FORGOT_URL,
    NEW_PASSWORD,
    OLD_PASSWORD,
    RESET_URL,
    WEB_HEADERS,
    _logged_in_browser,
    emailed_reset_links,
)

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
PROFILE = "/api/v1/auth/profile"
REFRESH = "/api/v1/auth/refresh-token"  # the admin UI calls it; the bot posts the same body to /auth/refresh
REFRESH_COOKIE = "/api/v1/auth/refresh-cookie"
STAFF_LOGIN = "/api/v1/staff/auth/login"
STAFF_REFRESH = "/api/v1/staff/auth/refresh"


def next_second():
    """Start a fresh second: the logins before it are older than anything after it under either
    rule, the millisecond one and the whole-second one a token without the millisecond claim
    still follows. A test that needs two events inside one second then has nearly all of it."""
    time.sleep(1.01 - time.time() % 1)


def sign_in(app, identifier, password):
    """A device signing in on the website: a fresh browser, never the session client."""
    resp = app.test_client().post(LOGIN, json={"identifier": identifier, "password": password})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["tokens"]


def refresh(app, tokens):
    return app.test_client().post(REFRESH, json={"refresh_token": tokens["refresh_token"]})


def refresh_by_cookie(app, tokens):
    browser = app.test_client()
    browser.set_cookie("refresh_token_cookie", tokens["refresh_token"])
    return browser.post(REFRESH_COOKIE, headers={"X-CSRF-TOKEN": get_csrf_token(tokens["refresh_token"])})


def profile(app, tokens):
    return app.test_client().get(PROFILE, headers={"Authorization": f"Bearer {tokens['access_token']}"})


def doors(app, tokens):
    """What a device holding `tokens` gets from both refresh doors and its next signed-in request."""
    return [door(app, tokens).status_code for door in (refresh, refresh_by_cookie, profile)]


def staff_doors(app, tokens):
    """What a STAFF login gets from the doors staff use: the admin UI's body-token refresh and the profile.
    The storefront cookie door renews a customer's login only (TokenService.storefront_may_renew), so a
    staff login is asked there separately: see `assert_staff_signed_in`."""
    return [door(app, tokens).status_code for door in (refresh, profile)]


def assert_staff_signed_in(app, tokens):
    """A staff login is signed in on its own doors, and the storefront cookie door refuses it by design."""
    assert staff_doors(app, tokens) == STAFF_SIGNED_IN
    assert refresh_by_cookie(app, tokens).status_code == 401


SIGNED_OUT = [401, 401, 401]
SIGNED_IN = [200, 200, 200]
STAFF_SIGNED_IN = [200, 200]


def _revocations(events):
    return [event for event in events if event["action"] == "all_sessions_revoked"]


def _ask_for_reset_link(app, user, emailed_reset_links):
    """Forgot-password, from a browser with no login; answers the e-mailed reset token."""
    asked = app.test_client().post(FORGOT_URL, headers=WEB_HEADERS, data=json.dumps({"identifier": user.email}))
    assert asked.status_code == 200, asked.get_json()
    return emailed_reset_links[-1][1]


def _use_reset_link(app, token, new_password=NEW_PASSWORD):
    """The reset page with the e-mailed token: the one request that writes the account's cut-off."""
    body = json.dumps({"token": token, "new_password": new_password})
    reset = app.test_client().post(RESET_URL, headers=WEB_HEADERS, data=body)
    assert reset.status_code == 200, reset.get_json()
    return reset


def _reset_password(app, user, emailed_reset_links, new_password=NEW_PASSWORD):
    _use_reset_link(app, _ask_for_reset_link(app, user, emailed_reset_links), new_password)


def test_a_password_reset_signs_every_device_out_at_once(app, db, sample_user, emailed_reset_links, audit_events):
    other_device = sign_in(app, sample_user.email, OLD_PASSWORD)
    assert doors(app, other_device) == SIGNED_IN
    next_second()

    _reset_password(app, sample_user, emailed_reset_links)

    assert doors(app, other_device) == SIGNED_OUT
    after = sign_in(app, sample_user.email, NEW_PASSWORD)  # a login after the cut-off works
    assert doors(app, after) == SIGNED_IN
    [revocation] = _revocations(audit_events)
    assert (revocation["resource_id"], revocation["additional_data"]) == (
        str(sample_user.id),
        {"reason": "password_reset"},
    )


def test_the_reset_ends_the_login_rows_too(app, db, sample_user, emailed_reset_links):
    """Today's `UserSession` bookkeeping stays: the rows of the logins before the reset end."""
    sign_in(app, sample_user.email, OLD_PASSWORD)
    sign_in(app, sample_user.email, OLD_PASSWORD)
    before = [row.id for row in UserSession.query.filter_by(user_id=sample_user.id, is_active=True)]
    assert before

    _reset_password(app, sample_user, emailed_reset_links)

    db.session.expire_all()
    assert all(not db.session.get(UserSession, row_id).is_active for row_id in before)


@pytest.fixture
def redis_down(monkeypatch):
    """Redis is unreachable for the cut-off and the per-token blacklist alike."""
    import business_app.services.session_revocation_service as revocation_module

    monkeypatch.setattr(revocation_module, "redis_client", _DownRedis())
    monkeypatch.setattr(TokenService(), "redis_client", _DownRedis())
    monkeypatch.setattr(TokenService(), "redis_available", True)


def test_with_redis_down_signed_in_requests_still_work_and_the_failure_is_logged(
    app, db, sample_user, caplog, redis_down
):
    tokens = sign_in(app, sample_user.email, OLD_PASSWORD)

    with captured_logs(caplog, revocation_logger, logging.ERROR):
        resp = profile(app, tokens)

    assert resp.status_code == 200, resp.get_json()
    assert "session cut-off unreadable" in caplog.text


def test_with_redis_down_a_password_reset_fails_loudly(app, db, sample_user, emailed_reset_links, redis_down):
    token = _ask_for_reset_link(app, sample_user, emailed_reset_links)
    old_hash = sample_user.password_hash

    resp = app.test_client().post(
        RESET_URL, headers=WEB_HEADERS, data=json.dumps({"token": token, "new_password": NEW_PASSWORD})
    )

    assert resp.status_code >= 500, resp.get_json()
    db.session.rollback()  # the reset never committed: the cut-off could not be stored
    db.session.refresh(sample_user)
    assert sample_user.password_hash == old_hash


# ===========================================================================
# The phone-link merge that moves the website password onto the phone holder (R8b, R8e)
# ===========================================================================


def test_a_merge_that_moves_the_website_password_signs_the_holders_other_devices_out(
    app, db, linked_email_only_account, signed_bot_post, sms, audit_events
):  # R8e: e.g. the previous owner of a recycled number, still signed in to the holder's account
    _web, holder = linked_email_only_account
    holder_id = holder.id
    previous_owner = sign_in(app, holder.email, HOLDER_PASSWORD)
    next_second()

    data = _claim_by_phone(signed_bot_post, sms, holder.phone)

    assert doors(app, previous_owner) == SIGNED_OUT
    assert doors(app, data["tokens"]) == SIGNED_IN  # the bot's tokens are issued after the cut-off
    assert profile(app, data["tokens"]).get_json()["data"]["id"] == holder_id
    [revocation] = _revocations(audit_events)
    assert (revocation["resource_id"], revocation["additional_data"]) == (
        str(holder_id),
        {"reason": "account_merge"},
    )


def test_a_bot_signup_merge_moves_no_password_and_leaves_the_holders_devices_signed_in(
    app, db, program, second_sample_user, signed_bot_post, sms, audit_events
):  # R8b: a placeholder email brings no website login, so nothing about the holder's login changes
    holder = second_sample_user
    holder.password_hash = hash_password(HOLDER_PASSWORD)
    db.session.commit()
    holders_device = sign_in(app, holder.email, HOLDER_PASSWORD)
    _bot_signup(signed_bot_post)
    next_second()

    _claim_by_phone(signed_bot_post, sms, holder.phone)

    assert doors(app, holders_device) == SIGNED_IN
    assert _revocations(audit_events) == []


# ===========================================================================
# Production's 30-minute access tokens. The app re-mints a signed-in access cookie once it is
# within 30 minutes of expiring (business_app/__init__.py `after_request`), so in production that
# is every signed-in request; TestingConfig's hour-long token never gets there. A re-mint never
# carries a login past its own logout or its account's cut-off (a password reset writes one).
# ===========================================================================


@pytest.fixture
def production_access_lifetime(app, monkeypatch):
    """Access tokens live as long as in production, so every signed-in request re-mints the cookie."""
    monkeypatch.setitem(app.config, "JWT_ACCESS_TOKEN_EXPIRES", ProductionConfig.JWT_ACCESS_TOKEN_EXPIRES)


def handed_back_login(resp):
    """The access cookie a response hands the browser, or None: the last `access_token_cookie` it
    sets (a logout unsets it, so an empty value is none)."""
    values = [
        header.split(";", 1)[0].split("=", 1)[1]
        for header in resp.headers.getlist("Set-Cookie")
        if header.startswith("access_token_cookie=")
    ]
    return values[-1] if values and values[-1] else None


def test_with_production_tokens_logging_out_signs_this_browser_out(app, db, sample_user, production_access_lifetime):
    """Plain "Log out" too: it unset the access cookie, and the re-mint then set a fresh one after it."""
    browser, csrf = _logged_in_browser(app, sign_in(app, sample_user.email, OLD_PASSWORD)["access_token"])

    resp = browser.post(LOGOUT, headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf})

    assert resp.status_code == 200, resp.get_json()
    assert handed_back_login(resp) is None
    assert browser.get(PROFILE).status_code == 401
    assert browser.get("/login").status_code == 200


def test_with_production_tokens_a_login_from_the_cut_offs_own_second_is_refused_and_so_is_its_re_mint(
    app, db, sample_user, production_access_lifetime, emailed_reset_links
):
    """R9b: a login made earlier in the same second as a password reset was let through
    until it expired, because `iat` has whole seconds. Tokens and the cut-off now carry
    milliseconds: the login is refused at once, and so is the cookie the app re-minted for it.
    (The login's own pair is also blacklisted by the session bookkeeping; the re-minted cookie
    belongs to no tracked session, so only the cut-off refuses it.)"""
    reset_token = _ask_for_reset_link(app, sample_user, emailed_reset_links)
    next_second()
    second = int(time.time())
    login = sign_in(app, sample_user.email, OLD_PASSWORD)
    device, _csrf = _logged_in_browser(app, login["access_token"])
    busy = device.get(PROFILE)
    assert busy.status_code == 200 and handed_back_login(busy)  # every signed-in request re-mints

    _use_reset_link(app, reset_token)

    assert int(time.time()) == second, "the logins, the re-mint and the cut-off must share one second"
    assert doors(app, login) == SIGNED_OUT
    assert device.get(PROFILE).status_code == 401  # the browser now holds the re-minted cookie


def test_with_production_tokens_a_login_after_the_cut_off_in_its_own_second_is_kept_and_extended(
    app, db, sample_user, production_access_lifetime, emailed_reset_links
):
    """R9b: a login right after a password reset, in the same second, was let through but
    never extended, so on production's 30-minute tokens it signed itself out within the half hour.
    It is now an ordinary login."""
    sign_in(app, sample_user.email, OLD_PASSWORD)
    reset_token = _ask_for_reset_link(app, sample_user, emailed_reset_links)
    next_second()
    second = int(time.time())
    _use_reset_link(app, reset_token)

    after = sign_in(app, sample_user.email, NEW_PASSWORD)
    assert int(time.time()) == second, "the cut-off and the new login must share one second"

    busy = profile(app, after)
    assert busy.status_code == 200
    assert handed_back_login(busy) is not None  # extended like any other login
    assert doors(app, after) == SIGNED_IN


def reminted_before_the_millisecond_claim(app, tokens):
    """A cookie the app re-minted before the deploy that added the millisecond issue time: a re-mint's
    claims without that one, signed with the app's own key. A re-mint belongs to no tracked session,
    so the session bookkeeping never blacklists it; only the cut-off can refuse it."""
    reminted = handed_back_login(profile(app, tokens))
    with app.app_context():
        claims = decode_token(reminted)
        del claims[ISSUED_AT_MS_CLAIM]
        return jwt.encode(claims, jwt_config.encode_key, algorithm=jwt_config.algorithm)


def test_with_production_tokens_a_token_from_before_the_millisecond_claim_keeps_the_seconds_rule(
    app, db, sample_user, production_access_lifetime, emailed_reset_links
):
    """A token minted before the deploy has whole seconds only. One from an earlier second than the
    cut-off is refused; one from the cut-off's own second is let through until it expires, never
    extended, as before."""
    older = reminted_before_the_millisecond_claim(app, sign_in(app, sample_user.email, OLD_PASSWORD))
    reset_token = _ask_for_reset_link(app, sample_user, emailed_reset_links)
    next_second()
    second = int(time.time())
    same_second = reminted_before_the_millisecond_claim(app, sign_in(app, sample_user.email, OLD_PASSWORD))

    _use_reset_link(app, reset_token)

    assert int(time.time()) == second, "the token and the cut-off must share one second"
    assert profile(app, {"access_token": older}).status_code == 401
    let_through = profile(app, {"access_token": same_second})
    assert let_through.status_code == 200 and handed_back_login(let_through) is None


def staff_sign_in(app, telegram_id):
    """The staff bot's signed `/staff/auth/login` for an already-linked Telegram id."""
    body = json.dumps({"telegram_id": telegram_id}).encode()
    signature = _get_webhook_signature(body, app.config["WEBHOOK_SECRET"])
    resp = app.test_client().post(
        STAFF_LOGIN, data=body, headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": signature}
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def test_every_door_that_hands_out_a_token_stamps_its_issue_time_in_milliseconds(
    app, db, sample_user, delivery_driver, production_access_lifetime
):
    """R9b: the cut-off compares milliseconds, so every token the app mints carries one claim with
    its issue time in milliseconds: a login's pair, the refresh by body and by cookie, the re-minted
    cookie, and the staff bot's signed login and its refresh."""
    delivery_driver.telegram_id = "800100299"
    db.session.commit()
    started = int(time.time() * 1000)
    login = sign_in(app, sample_user.email, OLD_PASSWORD)
    staff = staff_sign_in(app, delivery_driver.telegram_id)
    minted = {
        "login access": login["access_token"],
        "login refresh": login["refresh_token"],
        "refresh by body": refresh(app, login).get_json()["data"]["access_token"],
        "refresh by cookie": handed_back_login(refresh_by_cookie(app, login)),
        "re-minted cookie": handed_back_login(profile(app, login)),
        "staff login access": staff["access_token"],
        "staff login refresh": staff["refresh_token"],
        "staff refresh": app.test_client()
        .post(STAFF_REFRESH, headers={"Authorization": f"Bearer {staff['refresh_token']}"})
        .get_json()["data"]["access_token"],
    }
    ended = int(time.time() * 1000)

    with app.app_context():
        stamped = {door: decode_token(token).get(ISSUED_AT_MS_CLAIM) for door, token in minted.items()}
    assert [door for door, at in stamped.items() if not (isinstance(at, int) and started <= at <= ended)] == []
