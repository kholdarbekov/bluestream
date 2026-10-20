"""A web-only customer connects our Telegram bot from /my-account (spec 2026-10-19 D2–D4, D11, D13, D16).

The tap issues a one-time, ten-minute deep link; the customer bot previews it (never consuming it)
and redeems it on the customer's Yes. Every request is sent the way its caller sends it: the
storefront's `apiRequest` (access cookie + `X-CSRF-TOKEN`, the page language as `?lang=`) for the
tap, the customer bot's signed POST for preview and confirm. Only Redis outages are simulated.
After the link, the usual phone-link merge (R8) runs through the bot's signed routes too; there the
SMS task is the one thing replaced, by a recorder with its signature.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
import redis
from flask_jwt_extended import decode_token

from business_app.models.loyalty import LoyaltyProgram, LoyaltyTransaction, ReferralProgram
from business_app.models.notification import NotificationPreference
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.cross_platform_sync_service import logger as cross_platform_sync_logger
from business_app.services.customer_telegram_link_service import CustomerTelegramLinkService
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.notification_service import NotificationService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity
from business_app.utils.password_security import hash_password
from business_app.utils.service_factory import get_auth_service
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, TELEGRAM_LINK_API_TRANSLATIONS
from shared.enums import UserRole, UserStatus, UserType
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier
from tests.integration.reorder_reminder_builders import (
    captured_logs,
    customer_headers,
    delivered_order,
    link,
    seed_translations,
)
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

ISSUE = "/api/v1/auth/telegram-link"
PREVIEW = "/api/v1/bot/web-link/preview"
CONFIRM = "/api/v1/bot/web-link/confirm"
TG = 700100200


def _issue(app, token, lang="ru"):
    """The /my-account button: my-account.js posts through apiRequest, the page language in `?lang=`."""
    browser, csrf = _logged_in_browser(app, token)
    return browser.post(f"{ISSUE}?lang={lang}", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf})


def _link_token(resp):
    return resp.get_json()["data"]["telegram_link"].split("?start=web_", 1)[1]


def _confirm(signed_bot_post, token, **over):
    """The customer bot's Yes tap, with what Telegram tells it about the user."""
    body = {"token": token, "telegram_id": TG, "username": "kamola_test", "first_name": "Kamola", "last_name": None}
    return signed_bot_post(CONFIRM, {**body, **over})


def _code(resp):
    return resp.status_code, resp.get_json()["error_code"]


def _link_alive(token):
    return bool(CustomerTelegramLinkService._redis().exists(RedisKeyspace.customer_telegram_link(token)))


def _telegram_id_of(db, user_id):
    db.session.expire_all()
    return db.session.get(User, user_id).telegram_id


class _DownRedis:
    """Redis is unreachable: every command fails the way redis-py fails."""

    def __getattr__(self, name):
        def _fail(*args, **kwargs):
            raise redis.ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

        return _fail


@pytest.fixture
def link_copy(db):
    seed_translations(db, TELEGRAM_LINK_API_TRANSLATIONS)


def test_the_tap_issues_a_one_time_ten_minute_link_to_our_bot(app, db, sample_user, auth_token):
    resp = _issue(app, auth_token)
    token, data = _link_token(resp), resp.get_json()["data"]
    assert resp.status_code == 200 and data["expires_in"] == 600
    assert data["telegram_link"] == f"{app.config['COMPANY_TELEGRAM_BOT_URL']}?start=web_{token}"
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", token) and len(f"web_{token}") <= 64
    r = CustomerTelegramLinkService._redis()
    stored = json.loads(r.get(RedisKeyspace.customer_telegram_link(token)))
    assert (stored["user_id"], stored["language"]) == (sample_user.id, "ru")
    assert 590 < r.ttl(RedisKeyspace.customer_telegram_link(token)) <= 600
    assert (
        KEYSPACE_TIERS["customer_telegram_link"]
        is KEYSPACE_TIERS["customer_telegram_link_pointer"]
        is RedisUsageTier.TIER_SECURITY
    )


def test_a_second_tap_kills_the_first_link(app, db, sample_user, auth_token, signed_bot_post):  # RF1
    first, second = _link_token(_issue(app, auth_token)), _link_token(_issue(app, auth_token))
    assert _code(signed_bot_post(PREVIEW, {"token": first, "telegram_id": TG})) == (400, "WEB_LINK_EXPIRED")
    assert _code(_confirm(signed_bot_post, first)) == (400, "WEB_LINK_EXPIRED")
    assert signed_bot_post(PREVIEW, {"token": second, "telegram_id": TG}).status_code == 200


def test_preview_names_the_account_masked_and_consumes_nothing(app, db, sample_user, auth_token, signed_bot_post):
    token = _link_token(_issue(app, auth_token))
    for _ in range(2):
        resp = signed_bot_post(PREVIEW, {"token": token, "telegram_id": TG})
        assert (resp.status_code, resp.get_json()["data"]) == (
            200,
            {"account_label": "T*** U*** · +998***4567", "language": "ru"},
        )
    assert _link_alive(token)


def test_confirm_connects_the_telegram_once_and_returns_tokens_for_the_bot(
    app, db, sample_user, auth_token, signed_bot_post, audit_events  # noqa: F811
):
    sample_user.status = UserStatus.PENDING_VERIFICATION  # R7 / RF5
    db.session.commit()
    token = _link_token(_issue(app, auth_token))
    data = _confirm(signed_bot_post, token).get_json()["data"]
    assert (data["language"], data["greeting_name"], data["phone_status"], data["phone"]) == (
        "ru",
        "Test",
        "unverified",
        "+998901234567",
    )
    assert data["tokens"]["access_token"] and data["tokens"]["refresh_token"]
    db.session.expire_all()
    row = db.session.get(User, sample_user.id)
    assert (row.telegram_id, row.telegram_username, row.is_bot_active, row.preferred_language) == (
        str(TG),
        "kamola_test",
        True,
        "ru",
    )
    assert (row.first_name, row.registration_source, row.email_verified_at, row.status) == (
        "Test",
        "web",
        None,
        UserStatus.PENDING_VERIFICATION,
    )
    assert row.last_bot_interaction is not None
    assert LoyaltyTransaction.query.filter_by(user_id=row.id).count() == 0 and ReferralProgram.query.count() == 0
    [event] = [e for e in audit_events if e["action"] == "telegram_linked"]
    assert (event["event_type"], event["severity"], event["resource_type"], event["resource_id"], event["new_values"]) == (
        AuditEventType.USER_UPDATED,
        AuditSeverity.HIGH,
        "user",
        str(row.id),
        {"telegram_id": str(TG)},
    )
    assert CustomerTelegramLinkService.telegram_link_state(row) == "connected"
    assert not _link_alive(token)


def test_telegram_names_fill_only_the_empty_ones(app, db, sample_user, auth_token, signed_bot_post):  # RF5
    sample_user.last_name = None
    db.session.commit()
    token = _link_token(_issue(app, auth_token))
    assert _confirm(signed_bot_post, token, first_name="Kamola", last_name="Karimova").status_code == 200
    db.session.expire_all()
    row = db.session.get(User, sample_user.id)
    assert (row.first_name, row.last_name) == ("Test", "Karimova")


def test_a_used_link_is_refused_and_changes_nothing(app, db, sample_user, auth_token, signed_bot_post):
    token = _link_token(_issue(app, auth_token))
    assert _confirm(signed_bot_post, token).status_code == 200
    assert _code(_confirm(signed_bot_post, token)) == (400, "WEB_LINK_EXPIRED")
    assert _code(_confirm(signed_bot_post, token, telegram_id=700100299)) == (400, "WEB_LINK_EXPIRED")
    assert _telegram_id_of(db, sample_user.id) == str(TG)


def test_a_telegram_that_already_has_an_account_is_refused_and_nothing_is_written(
    app, db, sample_user, second_sample_user, auth_token, signed_bot_post, audit_events  # noqa: F811
):
    second_sample_user.telegram_id = str(TG)
    db.session.commit()
    token = _link_token(_issue(app, auth_token))
    assert _code(signed_bot_post(PREVIEW, {"token": token, "telegram_id": TG})) == (409, "TELEGRAM_ALREADY_HAS_ACCOUNT")
    assert _link_alive(token)  # preview never consumes
    assert _code(_confirm(signed_bot_post, token)) == (409, "TELEGRAM_ALREADY_HAS_ACCOUNT")
    assert _telegram_id_of(db, sample_user.id) is None
    assert User.query.filter_by(telegram_id=str(TG)).count() == 1
    assert [e for e in audit_events if e["action"] == "telegram_linked"] == []


def test_the_unique_id_race_answers_the_same_code(
    monkeypatch, app, db, sample_user, second_sample_user, auth_token, signed_bot_post
):
    second_sample_user.telegram_id = str(TG)
    db.session.commit()
    token = _link_token(_issue(app, auth_token))
    # The other row's insert lands between the check and the commit: only the unique index sees it.
    monkeypatch.setattr(CustomerTelegramLinkService, "_telegram_id_taken", staticmethod(lambda _id: False))
    assert _code(_confirm(signed_bot_post, token)) == (409, "TELEGRAM_ALREADY_HAS_ACCOUNT")
    assert _telegram_id_of(db, sample_user.id) is None


def test_an_account_connected_meanwhile_is_no_longer_available(app, db, sample_user, auth_token, signed_bot_post):
    token = _link_token(_issue(app, auth_token))
    sample_user.telegram_id = "700100999"
    db.session.commit()
    assert _code(signed_bot_post(PREVIEW, {"token": token, "telegram_id": TG})) == (409, "WEB_LINK_NOT_AVAILABLE")
    assert _code(_confirm(signed_bot_post, token)) == (409, "WEB_LINK_NOT_AVAILABLE")
    assert _telegram_id_of(db, sample_user.id) == "700100999"


def test_a_connected_customer_gets_no_link(app, db, sample_user, auth_token, link_copy):
    sample_user.telegram_id = "700100999"
    db.session.commit()
    resp = _issue(app, auth_token)
    assert _code(resp) == (409, "TELEGRAM_LINK_NOT_AVAILABLE")
    assert resp.get_json()["message"] == TELEGRAM_LINK_API_TRANSLATIONS["api.auth.telegram_link_not_available"]["ru"]
    assert CustomerTelegramLinkService._redis().get(RedisKeyspace.customer_telegram_link_pointer(sample_user.id)) is None


@pytest.mark.parametrize("path", [PREVIEW, CONFIRM])
def test_unsigned_or_wrongly_signed_bot_calls_are_401_and_write_nothing(
    app, db, sample_user, auth_token, client, signed_bot_post, path
):
    token = _link_token(_issue(app, auth_token))
    body = {"token": token, "telegram_id": TG}
    assert client.post(path, json=body).status_code == 401
    assert signed_bot_post(path, body, signature="0" * 64).status_code == 401
    assert _link_alive(token)
    assert _telegram_id_of(db, sample_user.id) is None


def test_redis_down_fails_closed(monkeypatch, app, db, sample_user, auth_token, signed_bot_post, link_copy):  # RF4
    monkeypatch.setattr(CustomerTelegramLinkService, "_redis", staticmethod(lambda: _DownRedis()))
    resp = _issue(app, auth_token)
    assert _code(resp) == (503, "TELEGRAM_LINK_STORE_UNAVAILABLE")
    assert (
        resp.get_json()["message"] == TELEGRAM_LINK_API_TRANSLATIONS["api.auth.telegram_link_store_unavailable"]["ru"]
    )
    assert resp.get_json().get("data") is None  # no link: the link lives under `data`
    assert _code(signed_bot_post(PREVIEW, {"token": "x" * 32, "telegram_id": TG})) == (
        503,
        "TELEGRAM_LINK_STORE_UNAVAILABLE",
    )
    assert _code(_confirm(signed_bot_post, "x" * 32)) == (503, "TELEGRAM_LINK_STORE_UNAVAILABLE")


def test_an_empty_token_is_an_expired_link(db, signed_bot_post):  # RF2
    assert _code(signed_bot_post(PREVIEW, {"token": "", "telegram_id": TG})) == (400, "WEB_LINK_EXPIRED")


# ===========================================================================
# After the link, the usual phone-link merge (D12 'missing', owner decision R8, R8a, R8b)
# ===========================================================================

CHECK_PHONE = "/api/v1/auth/check-phone-availability"
LINK_SEND_OTP = "/api/v1/auth/link-phone-account/send-otp"
LINK_VERIFY = "/api/v1/auth/link-phone-account/verify"
LOGIN = "/api/v1/auth/login"
PREFERENCES = "/api/v1/notifications/preferences"  # what /profile-settings and the bot's settings read
WEB_PASSWORD = "TestPassword123!"  # sample_user's: the website login the customer just used
HOLDER_PASSWORD = "PhoneHolder456!"
NOT_OFFERED = {"available": False, "can_link": False, "existing_user_masked": None}


@pytest.fixture
def program(db):
    """The default loyalty program: every sign-up gets a welcome bonus, which a merge must not double."""
    row = LoyaltyProgram(name="Aqua Club", is_active=True, is_default=True, signup_bonus=200, points_expiry_days=365)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def sms(monkeypatch):
    """Every verification SMS the backend sends, recorded by the task's own signature (the only I/O mocked)."""
    sent = []

    def _sms_task(user_id, otp_code, phone_number=None):
        sent.append({"user_id": user_id, "otp": otp_code, "phone": phone_number})

    monkeypatch.setattr("business_app.services.auth_service.send_verification_sms_task", _sms_task)
    return sent


@pytest.fixture
def linked_email_only_account(app, db, sample_user, second_sample_user, auth_token, signed_bot_post):
    """An email-only web account connected to TG by the real link (W), and the account holding the number
    Telegram then shares (P): ACTIVE, no Telegram, source 'web', with a password of its own."""
    sample_user.phone = None
    second_sample_user.password_hash = hash_password(HOLDER_PASSWORD)
    db.session.commit()
    confirmed = _confirm(signed_bot_post, _link_token(_issue(app, auth_token))).get_json()["data"]
    assert confirmed["phone_status"] == "missing"
    return sample_user, second_sample_user


def _bot_signup(signed_bot_post):
    """TG signs up in the bot (the real signed route): a placeholder email, no password, a welcome bonus."""
    body = {"telegram_id": TG, "first_name": "Kamola", "language_code": "uz"}
    resp = signed_bot_post("/api/v1/auth/telegram-register", body)
    assert resp.status_code == 201, resp.get_json()
    return User.query.filter_by(telegram_id=str(TG)).one()


def _bot_signin_signup(signed_bot_post):
    """TG's first contact is the bot's sign-in (telegram-login creates the account): its password is a
    hash of a random token nobody knows."""
    resp = signed_bot_post("/api/v1/auth/telegram-login", {"telegram_id": TG, "first_name": "Kamola"})
    assert resp.status_code == 200, resp.get_json()
    return User.query.filter_by(telegram_id=str(TG)).one()


BOT_SIGNUPS = {"telegram-register": _bot_signup, "telegram-login": _bot_signin_signup}


def _claim_by_phone(signed_bot_post, sms, phone):
    """The bot's usual phone-link journey for TG: the offer, one SMS code to the number, then that code."""
    offer = signed_bot_post(CHECK_PHONE, {"phone": phone, "telegram_id": TG}).get_json()["data"]
    assert (offer["available"], offer["can_link"]) == (False, True)
    assert signed_bot_post(LINK_SEND_OTP, {"phone": phone, "telegram_id": TG}).status_code == 200
    [code] = [sent["otp"] for sent in sms if sent["phone"] == phone]
    resp = signed_bot_post(LINK_VERIFY, {"telegram_id": TG, "otp": code})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _login(app, identifier, password):
    """The website's sign-in, from a fresh browser (never the session client: /login sets cookies)."""
    return app.test_client().post(LOGIN, json={"identifier": identifier, "password": password})


def _signed_in_as(app, tokens):
    with app.app_context():
        return int(decode_token(tokens["access_token"])["sub"])


@pytest.mark.parametrize("older", ["phone holder", "website account"])
def test_after_a_web_link_the_usual_merge_keeps_the_phone_holder_and_the_website_login(
    app, db, program, linked_email_only_account, signed_bot_post, sms, older
):  # R8, R8a, R8b
    web, holder = linked_email_only_account
    young, old = (web, holder) if older == "phone holder" else (holder, web)
    old.created_at = young.created_at - timedelta(days=30)
    holder.email = None  # a phone-only account: the website email is simply copied over
    db.session.commit()
    for account in (web, holder):  # both signed up, so both hold a welcome bonus
        LoyaltyService().grant_welcome_bonus(account.id)
    LoyaltyService().award_points(web.id, 120, "Order bonus")
    order = delivered_order(db, web, datetime.now(UTC) - timedelta(days=2))
    web_id, holder_id, holder_phone, order_id = web.id, holder.id, holder.phone, order.id

    data = _claim_by_phone(signed_bot_post, sms, holder_phone)

    assert data["user"]["id"] == holder_id and _signed_in_as(app, data["tokens"]) == holder_id
    db.session.expire_all()
    assert db.session.get(User, web_id) is None
    survivor = db.session.get(User, holder_id)
    assert (survivor.phone, survivor.telegram_id, survivor.email, survivor.preferred_language) == (
        holder_phone,
        str(TG),
        "test@example.com",
        "ru",  # the page's language, saved by the link (D11)
    )
    assert db.session.get(Order, order_id).user_id == holder_id
    assert UserAddress.query.filter_by(user_id=holder_id).count() == 1  # W's address came along
    assert LoyaltyTransaction.query.filter_by(user_id=holder_id, description="Order bonus").count() == 1
    assert len(LoyaltyService.get_welcome_bonus_lots(holder_id)) == 1
    login = _login(app, "test@example.com", WEB_PASSWORD)
    assert (login.status_code, login.get_json()["data"]["user"]["id"]) == (200, holder_id)


def test_the_website_email_wins_over_the_phone_holders_own(
    app, db, caplog, linked_email_only_account, signed_bot_post, sms
):  # R8b
    _web, holder = linked_email_only_account  # the holder keeps its own real address, coworker@example.com
    holder_id = holder.id

    with captured_logs(caplog, cross_platform_sync_logger, logging.INFO):
        _claim_by_phone(signed_bot_post, sms, holder.phone)

    db.session.expire_all()
    assert db.session.get(User, holder_id).email == "test@example.com"
    assert _login(app, "coworker@example.com", HOLDER_PASSWORD).status_code == 401
    login = _login(app, "test@example.com", WEB_PASSWORD)
    assert (login.status_code, login.get_json()["data"]["user"]["id"]) == (200, holder_id)
    # The dropped address is logged, masked by the one email masker.
    assert "co***@example.com" in caplog.text and "coworker@example.com" not in caplog.text


def test_an_older_bot_signup_merges_into_the_agents_customer_who_keeps_its_own_login(
    app, db, program, admin_user, signed_bot_post, sms
):  # R8a: the phone holder survives even when younger; a placeholder email carries nothing (R8b)
    signup = _bot_signup(signed_bot_post)
    holder = get_auth_service().create_user_by_admin(
        phone="+998901234571",
        first_name="Dilnoza",
        created_by_admin_id=admin_user.id,
        email="dilnoza@example.com",
        registration_source="sales_agent",
    )
    signup.created_at = holder.created_at - timedelta(days=30)
    db.session.commit()
    signup_id, holder_id = signup.id, holder.id
    login_before = (holder.email, holder.password_hash, holder.phone)

    data = _claim_by_phone(signed_bot_post, sms, holder.phone)

    assert data["user"]["id"] == holder_id and _signed_in_as(app, data["tokens"]) == holder_id
    db.session.expire_all()
    assert db.session.get(User, signup_id) is None
    survivor = db.session.get(User, holder_id)
    assert (survivor.email, survivor.password_hash, survivor.phone) == login_before
    assert (survivor.telegram_id, survivor.registration_source) == (str(TG), "sales_agent")
    assert len(LoyaltyService.get_welcome_bonus_lots(holder_id)) == 1  # the signup's bonus, never two


def test_both_accounts_notification_settings_merge_into_one_per_kind_and_the_holders_choices_stay(
    app, db, linked_email_only_account, signed_bot_post, sms
):  # both opened the notification settings: the website's and the bot's read writes the full default set
    web, holder = linked_email_only_account
    for account in (web, holder):
        resp = app.test_client().get(PREFERENCES, headers=customer_headers(app, account))
        assert resp.status_code == 200, resp.get_json()
    holder_rows = NotificationPreference.query.filter_by(user_id=holder.id).order_by(NotificationPreference.id).all()
    web_rows = {
        (row.notification_type, row.channel): row for row in NotificationPreference.query.filter_by(user_id=web.id)
    }
    kinds = set(web_rows)
    assert len(holder_rows) > 1 and {(row.notification_type, row.channel) for row in holder_rows} == kinds
    switched_off, missing = holder_rows[0], holder_rows[1]
    switched_off.is_enabled = False  # the holder turned one off; the website account left it on
    web_rows[(switched_off.notification_type, switched_off.channel)].is_enabled = True
    moved_id = web_rows[(missing.notification_type, missing.channel)].id  # a kind only the website account has
    db.session.delete(missing)
    db.session.commit()
    web_id, holder_id, switched_off_id = web.id, holder.id, switched_off.id

    _claim_by_phone(signed_bot_post, sms, holder.phone)

    db.session.expire_all()
    assert db.session.get(User, web_id) is None
    rows = NotificationPreference.query.filter_by(user_id=holder_id).all()
    assert len(rows) == len(kinds) and {(row.notification_type, row.channel) for row in rows} == kinds
    assert db.session.get(NotificationPreference, switched_off_id).is_enabled is False
    assert db.session.get(NotificationPreference, moved_id).user_id == holder_id


def test_a_merge_refused_after_the_code_answers_the_seeded_message_never_its_reason(
    db, linked_email_only_account, signed_bot_post, sms
):  # the holder became one person across phones between the SMS and the code
    web, holder = linked_email_only_account
    web_id, holder_id, phone = web.id, holder.id, holder.phone
    seed_translations(db, ["api.auth.accounts_linking_failed"])
    assert signed_bot_post(CHECK_PHONE, {"phone": phone, "telegram_id": TG}).get_json()["data"]["can_link"] is True
    assert signed_bot_post(LINK_SEND_OTP, {"phone": phone, "telegram_id": TG}).status_code == 200
    [code] = [sent["otp"] for sent in sms if sent["phone"] == phone]
    link(db, holder)

    resp = signed_bot_post(LINK_VERIFY, {"telegram_id": TG, "otp": code})

    assert resp.status_code == 400
    assert resp.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.accounts_linking_failed"].values()
    db.session.expire_all()
    assert (db.session.get(User, web_id).telegram_id, db.session.get(User, holder_id).telegram_id) == (str(TG), None)


@pytest.mark.parametrize("refusal", ["its own Telegram", "inactive", "staff", "one person across phones"])
def test_a_number_whose_account_cannot_be_claimed_is_never_offered(
    db, linked_email_only_account, signed_bot_post, refusal
):  # the refusals the bot answers with R6 (`_number_taken`)
    _web, holder = linked_email_only_account
    if refusal == "its own Telegram":
        holder.telegram_id = "700100999"
    elif refusal == "inactive":
        holder.status = UserStatus.INACTIVE
    elif refusal == "staff":
        holder.registration_source = "admin"
    else:
        link(db, holder)
    db.session.commit()
    assert signed_bot_post(CHECK_PHONE, {"phone": holder.phone, "telegram_id": TG}).get_json()["data"] == NOT_OFFERED


@pytest.mark.parametrize("caller", ["web link", "bot signup"])
def test_a_canonical_linked_phone_holder_is_refused_before_any_sms(
    request, db, program, second_sample_user, signed_bot_post, sms, caller
):  # the merge refuses such an account, so nothing up front may promise it
    if caller == "web link":
        request.getfixturevalue("linked_email_only_account")
    else:
        _bot_signup(signed_bot_post)
    holder = second_sample_user
    link(db, holder)  # one person across phones (CustomerLinkService)
    seed_translations(db, ["api.auth.accounts_linking_failed"])

    assert signed_bot_post(CHECK_PHONE, {"phone": holder.phone, "telegram_id": TG}).get_json()["data"] == NOT_OFFERED
    resp = signed_bot_post(LINK_SEND_OTP, {"phone": holder.phone, "telegram_id": TG})
    assert resp.status_code == 409
    assert resp.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.accounts_linking_failed"].values()
    assert sms == [] and not get_auth_service().redis_client.exists(f"phone_link:{TG}")


@pytest.mark.parametrize("status", [UserStatus.BANNED, UserStatus.INACTIVE], ids=lambda s: s.value)
def test_a_banned_or_deactivated_website_account_is_never_merged(
    db, linked_email_only_account, signed_bot_post, sms, status
):  # R8d: its email and password would otherwise sign in again on the phone holder
    web, holder = linked_email_only_account
    web.status = status
    db.session.commit()
    web_id, holder_id = web.id, holder.id
    seed_translations(db, ["api.auth.accounts_linking_failed"])

    assert signed_bot_post(CHECK_PHONE, {"phone": holder.phone, "telegram_id": TG}).get_json()["data"] == NOT_OFFERED
    resp = signed_bot_post(LINK_SEND_OTP, {"phone": holder.phone, "telegram_id": TG})

    assert resp.status_code == 409
    assert resp.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.accounts_linking_failed"].values()
    assert sms == [] and not get_auth_service().redis_client.exists(f"phone_link:{TG}")
    db.session.expire_all()
    assert (db.session.get(User, web_id).telegram_id, db.session.get(User, holder_id).telegram_id) == (str(TG), None)


@pytest.mark.parametrize("status", [UserStatus.BANNED, UserStatus.INACTIVE], ids=lambda s: s.value)
def test_a_website_account_banned_after_the_sms_is_refused_at_the_code(
    db, linked_email_only_account, signed_bot_post, sms, status
):  # R8d, verify's re-assertion
    web, holder = linked_email_only_account
    web_id, holder_id, phone = web.id, holder.id, holder.phone
    seed_translations(db, ["api.auth.accounts_linking_failed"])
    assert signed_bot_post(CHECK_PHONE, {"phone": phone, "telegram_id": TG}).get_json()["data"]["can_link"] is True
    assert signed_bot_post(LINK_SEND_OTP, {"phone": phone, "telegram_id": TG}).status_code == 200
    [code] = [sent["otp"] for sent in sms if sent["phone"] == phone]
    web.status = status
    db.session.commit()

    resp = signed_bot_post(LINK_VERIFY, {"telegram_id": TG, "otp": code})

    assert _code(resp) == (409, "ACCOUNTS_LINKING_FAILED")
    assert resp.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.accounts_linking_failed"].values()
    assert not get_auth_service().redis_client.exists(f"phone_link:{TG}")
    db.session.expire_all()
    assert (db.session.get(User, web_id).telegram_id, db.session.get(User, holder_id).telegram_id) == (str(TG), None)


@pytest.mark.parametrize("door", list(BOT_SIGNUPS))
def test_a_bot_signup_with_an_admin_set_email_never_carries_its_unusable_password(
    app, db, program, second_sample_user, signed_bot_post, sms, door
):  # T6-M3: one "has a real website password" rule
    signup = BOT_SIGNUPS[door](signed_bot_post)
    signup.email = "kamola@example.com"  # an admin typed a real address on the bot signup
    holder = second_sample_user
    holder.password_hash = hash_password(HOLDER_PASSWORD)
    db.session.commit()
    holder_id, holder_email = holder.id, holder.email

    _claim_by_phone(signed_bot_post, sms, holder.phone)

    db.session.expire_all()
    assert db.session.get(User, holder_id).email == holder_email
    login = _login(app, holder_email, HOLDER_PASSWORD)
    assert (login.status_code, login.get_json()["data"]["user"]["id"]) == (200, holder_id)


@pytest.mark.parametrize("who,can_login", [*((door, False) for door in BOT_SIGNUPS), ("website account", True)])
def test_the_platform_status_says_who_can_sign_in_on_the_website(app, db, sample_user, signed_bot_post, who, can_login):
    account = sample_user if who == "website account" else BOT_SIGNUPS[who](signed_bot_post)

    resp = app.test_client().get("/api/v1/auth/platform-status", headers=customer_headers(app, account))

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["platform_status"]["platforms"]["web"]["can_login"] is can_login


def test_every_refusal_of_the_code_carries_the_code_the_bot_branches_on(
    db, linked_email_only_account, signed_bot_post, sms
):  # T6-M8: the bot reads `error_code`, never the (translated) message
    web, holder = linked_email_only_account
    web_id, phone = web.id, holder.phone
    verify = lambda otp: signed_bot_post(LINK_VERIFY, {"telegram_id": TG, "otp": otp})  # noqa: E731

    assert _code(verify("123456")) == (404, "OTP_EXPIRED")  # no link request: never made, or its 10 minutes are up
    signed_bot_post(CHECK_PHONE, {"phone": phone, "telegram_id": TG})
    assert signed_bot_post(LINK_SEND_OTP, {"phone": phone, "telegram_id": TG}).status_code == 200
    [code] = [sent["otp"] for sent in sms if sent["phone"] == phone]
    assert _code(verify(f"{(int(code) + 1) % 1_000_000:06d}")) == (400, "INVALID_OTP")  # a wrong code: type it again
    # The code's own 3 minutes run out while the 10-minute link request lives on.
    get_auth_service().redis_client.delete(f"sms_verification:{web_id}", f"sms_verification_phone:{web_id}")
    assert _code(verify(code)) == (400, "OTP_EXPIRED")


STATES = [  # (user_type, role, status, telegram_id, is_bot_active) -> state   (in-memory User rows, D10/D13/R7)
    ((UserType.INDIVIDUAL, UserRole.CUSTOMER, UserStatus.ACTIVE, None, False), "available"),
    ((UserType.INDIVIDUAL, UserRole.CUSTOMER, UserStatus.PENDING_VERIFICATION, None, False), "available"),
    ((UserType.ENTITY, UserRole.CUSTOMER, UserStatus.ACTIVE, None, False), "available"),
    ((UserType.INDIVIDUAL, UserRole.CUSTOMER, UserStatus.BANNED, None, False), "unavailable"),
    ((UserType.INDIVIDUAL, UserRole.CUSTOMER, UserStatus.INACTIVE, None, False), "unavailable"),
    ((UserType.STAFF, UserRole.OPERATOR, UserStatus.ACTIVE, None, False), "unavailable"),
    ((UserType.INDIVIDUAL, UserRole.CUSTOMER, UserStatus.ACTIVE, "700100300", True), "connected"),
    ((UserType.INDIVIDUAL, UserRole.CUSTOMER, UserStatus.ACTIVE, "700100300", False), "connected_blocked"),
    ((UserType.STAFF, UserRole.OPERATOR, UserStatus.ACTIVE, "700100300", True), "connected"),
]


@pytest.mark.parametrize("fields,expected", STATES)
def test_one_published_state_and_connected_is_the_delivery_routing_rule(app, fields, expected):
    user = User(user_type=fields[0], role=fields[1], status=fields[2], telegram_id=fields[3], is_bot_active=fields[4])
    assert CustomerTelegramLinkService.telegram_link_state(user) == expected
    assert (expected == "connected") is NotificationService._user_has_connected_telegram(user)


def test_a_missing_account_is_unavailable():
    assert CustomerTelegramLinkService.telegram_link_state(None) == "unavailable"


@pytest.mark.parametrize(
    "first,last,phone,email,label",
    [
        ("Zarina", "Karimova", "+998901112233", None, "Z*** K*** · +998***2233"),
        ("Dilnoza", None, None, "dilnoza@example.com", "D*** · di***@example.com"),
        ("Dilnoza", None, None, "telegram_1@bot.internal", "D***"),  # D16: a placeholder never shows
        (None, None, None, "x@bluestream.local", "***"),
    ],
)
def test_the_label_reuses_the_existing_maskers(first, last, phone, email, label):
    user = User(first_name=first, last_name=last, phone=phone, email=email)
    assert CustomerTelegramLinkService.account_label(user) == label
