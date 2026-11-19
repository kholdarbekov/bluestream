"""The bot's "Open on website" button, a Telegram login button, signs a bot customer into the website (spec
2026-10-27 §9, §17 Q5; step 4 Task 16a).

The bot's button is a Telegram login button (`login_url`) whose URL is `/auth/telegram-login?next=<target>`. At the
tap Telegram opens it with the customer's identity and a `hash` appended (`telegram_signed`, Telegram's documented
check). The route answers with the same-site hop to `next`, carrying the same login cookies a password login sets,
so production's SameSite=Strict cookies are in place when the page loads (§4.6). Every request here is the one a
browser sends; only Redis outages are simulated. Every test runs with production's Strict cookies
(`production_cookie_setting`, from the customer page auth tests).
"""

import ast
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_qualified_name
from limits import parse as parse_rate_limit

from business_app import limiter
from business_app.frontend.routes import TELEGRAM_LOGIN_IP_RATE_LIMIT
from business_app.models.audit import AuditEventType, AuditLog, AuditSeverity
from business_app.models.user import User, UserSession
from business_app.services import telegram_login_service
from business_app.services.session_revocation_service import REVOKED_BY_PASSWORD_RESET, SessionRevocationService
from business_app.services.telegram_login_service import MAX_AGE_SECONDS
from business_app.utils.logging_config import SecuritySensitiveFilter, StructuredFormatter
from business_app.utils.password_security import hash_password
from business_app.utils.sentry import before_send, before_send_transaction
from scripts.seed_backend_translations import TELEGRAM_LOGIN_PAGE_TRANSLATIONS
from shared.enums import UserRole, UserStatus, UserType
from shared.telegram_login import TELEGRAM_LOGIN_PATH
from shared.validators import merge_tombstone_email
from tests.integration.redis_fakes import DownRedis
from tests.integration.reorder_reminder_builders import seed_translations
from tests.integration.telegram_login_builders import telegram_signed
from tests.integration.test_customer_page_auth import (  # noqa: F401 - production_cookie_setting: autouse
    CROSS,
    JWT_COOKIES,
    SAME,
    _claims,
    _cookie_value,
    _hop_target,
    _jwt_set_cookies,
    _set_cookies,
    _sign_in,
    production_cookie_setting,
)
from tests.integration.test_customer_telegram_link_api import TG, _bot_signup

pytestmark = pytest.mark.integration

TELEGRAM_BOT_TOKEN = "123456:TEST-TOKEN"
TG_ID = 424242  # sample_user_with_telegram's Telegram account
REPO = Path(__file__).resolve().parents[2]
# A page whose URL holds the signed data is never stored, and never rewritten on the way (an edge's injected script
# would report the URL).
SECRET_PAGE = "no-store, no-transform"


@pytest.fixture(autouse=True)
def bot_token(app, monkeypatch):
    monkeypatch.setitem(app.config, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)


@pytest.fixture
def sample_user_with_telegram(db, sample_user):
    """sample_user (password TestPassword123!) with Telegram account TG_ID connected."""
    sample_user.telegram_id = str(TG_ID)
    db.session.commit()
    return sample_user


@pytest.fixture
def seeded_login_copy(app, db):
    """The refused page's rows exactly as the seeder ships them, the pages compiled afresh."""
    seed_translations(db, [*TELEGRAM_LOGIN_PAGE_TRANSLATIONS, "Open the bot"])
    app.jinja_env.cache.clear()
    yield TELEGRAM_LOGIN_PAGE_TRANSLATIONS
    app.jinja_env.cache.clear()


def _signed(fields, auth_date=None):
    return telegram_signed(
        {**fields, "auth_date": str(int(time.time()) if auth_date is None else auth_date)}, TELEGRAM_BOT_TOKEN
    )


def _url(target, fields, *, auth_date=None, lang=None, signed=None):
    """The button's URL with what Telegram appends at the tap. `lang` is the language hook's own parameter."""
    query = [("next", target)] if target is not None else []
    query += list((signed or _signed(fields, auth_date)).items())
    query += [("lang", lang)] if lang else []
    return f"{TELEGRAM_LOGIN_PATH}?{urlencode(query)}"


def _user(db, **fields):
    values = {
        "email": "kamola@example.com",
        "phone": "+998901112233",
        "password_hash": hash_password("TestPassword123!"),
        "first_name": "Kamola",
        "user_type": UserType.INDIVIDUAL,
        "role": UserRole.CUSTOMER,
        "status": UserStatus.ACTIVE,
        "telegram_id": str(TG_ID),
        **fields,
    }
    user = User(**values)
    db.session.add(user)
    db.session.commit()
    return user


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def app_records(app):
    """Every record the app logger writes (`propagate=False`, so caplog never sees them), from DEBUG up."""
    handler, level = _Records(), app.logger.level
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        app.logger.removeHandler(handler)
        app.logger.setLevel(level)


def _audit_lines(records, action):
    return [
        record
        for record in records
        if record.getMessage().startswith("AUDIT [") and f"- {action} " in record.getMessage()
    ]


def _cookie_shapes(resp):
    """Each login cookie with its attributes (path, domain, SameSite, HttpOnly, Secure, lifetime), value and expiry
    date left out."""
    return {
        name: sorted(attr for attr in header.split("; ")[1:] if not attr.lower().startswith("expires="))
        for name, header in _set_cookies(resp).items()
        if name in JWT_COOKIES
    }


# --------------------------------------------------------------------------- signing in


def test_a_bot_signup_opens_its_subscription_on_the_website(app, db, signed_bot_post):
    user = _bot_signup(signed_bot_post)  # a placeholder email and no password: the bot is its only door
    browser = app.test_client()

    resp = browser.get(_url("/my-subscriptions?sub=7", {"id": str(TG), "first_name": "Kamola"}), headers=CROSS)

    assert _hop_target(resp) == "/my-subscriptions?sub=7"
    assert resp.headers["Cache-Control"] == SECRET_PAGE
    assert _jwt_set_cookies(resp) == sorted(JWT_COOKIES)  # the readable CSRF halves too: the quiet refresh needs them
    assert _claims(app, _cookie_value(resp, "access_token_cookie"))["sub"] == str(user.id)
    assert browser.get("/my-subscriptions?sub=7", headers=SAME).status_code == 200


def test_its_cookies_are_the_ones_a_password_login_sets(app, sample_user_with_telegram):
    _browser, password_login = _sign_in(app, sample_user_with_telegram)

    telegram_login = app.test_client().get(_url("/my-account", {"id": str(TG_ID), "first_name": "Ali"}))

    assert _cookie_shapes(telegram_login) == _cookie_shapes(password_login)
    assert sorted(_cookie_shapes(telegram_login)) == sorted(JWT_COOKIES)


def test_a_sign_in_writes_its_login_row_and_audit_and_leaves_the_account_as_it_was(app, db, sample_user_with_telegram):
    before = (sample_user_with_telegram.first_name, sample_user_with_telegram.telegram_username)

    resp = app.test_client().get(
        _url("/my-orders", {"id": str(TG_ID), "first_name": "Dilnoza", "username": "dilnoza_k"})
    )

    assert _hop_target(resp) == "/my-orders"
    db.session.expire_all()
    assert (sample_user_with_telegram.first_name, sample_user_with_telegram.telegram_username) == before
    assert UserSession.query.filter_by(user_id=sample_user_with_telegram.id, is_active=True).count() == 1
    [audit] = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_SUCCESS).all()
    assert (audit.resource_id, audit.success) == (str(sample_user_with_telegram.id), True)


def test_a_sign_in_over_another_logins_access_cookie_sets_only_the_new_logins_cookies(
    app, db, sample_user_with_telegram
):
    """One cookie writer per response (spec §9). The browser still holds login A's access cookie and has no session
    language (Telegram Desktop opening the default browser is a `Sec-Fetch-Site: none` navigation, which sends the
    Strict cookies), so the language hook verifies A's cookie; A reads the site's default language, so the hook does
    not redirect. A is inside the 30-minute re-mint window, as every production access token is. Signing in as B must
    hand the browser B's cookies only, each once: never B's refresh cookie beside a re-minted access cookie of A's."""
    login_a = _user(
        db,
        email="a.login@example.com",
        phone="+998907776655",
        telegram_id="515151",
        preferred_language=app.config["DEFAULT_LANGUAGE"],
    )
    with app.app_context():
        due = create_access_token(identity=str(login_a.id), expires_delta=timedelta(minutes=10))
    browser = app.test_client()
    browser.set_cookie("access_token_cookie", due)

    resp = browser.get(_url("/my-account", {"id": str(TG_ID), "first_name": "Ali"}))

    assert _hop_target(resp) == "/my-account"
    login_cookies = [
        header.split("=", 1)[0]
        for header in resp.headers.getlist("Set-Cookie")
        if header.split("=", 1)[0] in JWT_COOKIES
    ]
    assert sorted(login_cookies) == sorted(JWT_COOKIES)  # each once
    assert _claims(app, _cookie_value(resp, "access_token_cookie"))["sub"] == str(sample_user_with_telegram.id)
    assert _claims(app, _cookie_value(resp, "refresh_token_cookie"))["sub"] == str(sample_user_with_telegram.id)


# --------------------------------------------------------------------------- refusals


def _arrange(db, case, monkeypatch):
    """The account and the tap behind each refusal; answers the tap's URL (in English)."""
    fields = {"id": str(TG_ID), "first_name": "Kamola"}
    auth_date = None
    if case == "unknown_id":
        _user(db, telegram_id="515151")
    elif case == "inactive":
        _user(db, status=UserStatus.INACTIVE)
    elif case == "tombstone":
        user = _user(db)
        user.email = merge_tombstone_email(user.id)
        db.session.commit()
    elif case == "staff":
        _user(db, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    elif case == "revoked":
        user = _user(db)
        auth_date = int(time.time()) - 5
        SessionRevocationService.revoke_all(user.id, reason=REVOKED_BY_PASSWORD_RESET)  # a password reset since
        db.session.commit()
    else:
        _user(db)
    if case == "stale":
        auth_date = int(time.time()) - MAX_AGE_SECONDS - 1
    if case == "redis_down":
        monkeypatch.setattr(telegram_login_service, "_redis", lambda: DownRedis())
    signed = _signed(fields, auth_date)
    if case == "bad_hash":
        signed["hash"] = signed["hash"][:-1] + ("0" if signed["hash"][-1] != "0" else "1")
    return _url("/my-account", fields, signed=signed, lang="en")


INFO, WARNING, ERROR = logging.INFO, logging.WARNING, logging.ERROR


@pytest.mark.parametrize(
    "case,status,copy_key,code,level",
    [
        ("bad_hash", 403, "web.telegram_login.expired", "bad_hash", WARNING),  # an altered or forged link
        ("stale", 403, "web.telegram_login.expired", "stale", INFO),
        ("unknown_id", 403, "web.telegram_login.no_account", "no_account", INFO),
        ("inactive", 403, "web.telegram_login.no_account", "no_account", INFO),
        ("tombstone", 403, "web.telegram_login.no_account", "no_account", INFO),
        ("staff", 403, "web.telegram_login.staff", "not_customer", INFO),
        ("revoked", 403, "web.telegram_login.expired", "revoked", INFO),
        ("redis_down", 503, "web.telegram_login.unavailable", "unavailable", ERROR),  # our own outage
    ],
)
def test_every_refusal_signs_nobody_in_and_points_to_the_bot(
    app, db, case, status, copy_key, code, level, seeded_login_copy, analytics_configured, monkeypatch, app_records
):
    resp = app.test_client().get(_arrange(db, case, monkeypatch), headers=CROSS)

    html = resp.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    assert resp.status_code == status and "Location" not in resp.headers and not _jwt_set_cookies(resp)
    assert seeded_login_copy["web.telegram_login.title"]["en"] in text
    assert seeded_login_copy[copy_key]["en"] in text
    assert soup.select_one(f'a[href="{app.config["COMPANY_TELEGRAM_BOT_URL"]}"]').get_text(strip=True) == "Open the bot"
    assert "call" not in html.lower()
    assert not any(marker in html for marker in analytics_configured)
    assert 'content="no-referrer"' in html and 'content="noindex,nofollow"' in html
    assert resp.headers["Cache-Control"] == SECRET_PAGE
    [audit] = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILURE).all()
    assert (audit.error_message, audit.success) == (f"telegram_login:{code}", False)
    # Routine outcomes at INFO, tampering at WARNING, only an outage at ERROR (a Sentry error event)
    assert audit.severity is {INFO: AuditSeverity.LOW, WARNING: AuditSeverity.MEDIUM, ERROR: AuditSeverity.HIGH}[level]
    assert [record.levelno for record in _audit_lines(app_records, "login_failure")] == [level]
    assert not [record.getMessage() for record in app_records if record.levelno > level]


def test_a_second_tap_on_the_same_login_link_lands_on_next_and_grants_nothing(
    app, sample_user_with_telegram, app_records
):
    """Review Focus 4: a double tap, or the same link opened again within five minutes. Each replay is audited, at
    INFO: the one trace of a link opened twice or shared."""
    url = _url("/my-account", {"id": str(TG_ID), "first_name": "Ali"})
    browser = app.test_client()
    first = browser.get(url)  # the first tap signs in

    again = browser.get(url)  # the same hash: replayed

    assert _hop_target(again) == "/my-account"
    assert not _jwt_set_cookies(again)
    assert first.headers["Cache-Control"] == again.headers["Cache-Control"] == SECRET_PAGE
    assert browser.get("/my-account", headers=SAME).status_code == 200
    stranger = app.test_client()
    from_a_stranger = stranger.get(url)  # a replay from another browser
    assert _hop_target(from_a_stranger) == "/my-account" and from_a_stranger.headers["Cache-Control"] == SECRET_PAGE
    assert not stranger.get_cookie("access_token_cookie")
    assert stranger.get("/my-account", headers=SAME).headers["Location"].startswith("/login?next=")
    replays = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILURE).all()
    assert [(row.error_message, row.severity) for row in replays] == [
        ("telegram_login:replayed", AuditSeverity.LOW)
    ] * 2
    assert [record.levelno for record in _audit_lines(app_records, "login_failure")] == [INFO, INFO]


def test_a_declined_telegram_prompt_lands_on_next_and_grants_nothing(app, sample_user_with_telegram, app_records):
    """Telegram's LoginUrl contract: a customer who declines the authorization prompt is sent to the button's own
    URL with no auth fields at all. That is a choice, not a forged link: it hops to `next` (a browser already signed
    in lands there; any other is sent to log in), grants nothing, and is audited at INFO."""
    declined = f"{TELEGRAM_LOGIN_PATH}?{urlencode({'next': '/my-account'})}"
    signed_in, _ = _sign_in(app, sample_user_with_telegram)
    stranger = app.test_client()

    for browser in (signed_in, stranger):
        resp = browser.get(declined, headers=CROSS)
        assert _hop_target(resp) == "/my-account"
        assert not _jwt_set_cookies(resp)
        assert resp.headers["Cache-Control"] == SECRET_PAGE

    assert signed_in.get("/my-account", headers=SAME).status_code == 200
    assert stranger.get("/my-account", headers=SAME).headers["Location"].startswith("/login?next=")
    declines = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILURE).all()
    assert [(row.error_message, row.severity) for row in declines] == [
        ("telegram_login:declined", AuditSeverity.LOW)
    ] * 2
    assert [record.levelno for record in _audit_lines(app_records, "login_failure")] == [INFO, INFO]


@pytest.mark.parametrize(
    "partial",
    [
        {"id": str(TG_ID), "first_name": "Ali", "auth_date": "1793000000"},  # Telegram's fields, but no hash
        {"hash": "0" * 64},  # a hash with nothing it signs
        {"hash": ""},
        {"username": "ali"},
    ],
    ids=["fields-without-hash", "hash-without-fields", "empty-hash", "one-field"],
)
def test_a_partial_or_altered_set_is_still_a_forged_link(app, sample_user_with_telegram, partial, app_records):
    """Only a URL with no hash and no Telegram field at all is a declined prompt; anything in between is refused as
    an altered link, at WARNING."""
    resp = app.test_client().get(f"{TELEGRAM_LOGIN_PATH}?{urlencode({'next': '/my-account', **partial})}")

    assert resp.status_code == 403 and not _jwt_set_cookies(resp)
    [audit] = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILURE).all()
    assert (audit.error_message, audit.severity) == ("telegram_login:bad_hash", AuditSeverity.MEDIUM)
    assert [record.levelno for record in _audit_lines(app_records, "login_failure")] == [WARNING]


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example",
        "https://evil.example/x",
        "/admin",
        "/my-subscriptions?sub=7&x=1",
        "/my-subscriptions?sub=0",
        "/\\evil.example",
        None,
    ],
)
def test_a_next_outside_the_allowlist_is_refused_before_the_hash_is_spent(
    app, sample_user_with_telegram, analytics_configured, target
):
    signed = _signed({"id": str(TG_ID), "first_name": "Ali"})

    bad = app.test_client().get(_url(target, {}, signed=signed))
    good = app.test_client().get(_url("/my-account", {}, signed=signed))

    assert bad.status_code == 400 and "Location" not in bad.headers and not _jwt_set_cookies(bad)
    assert bad.headers["Cache-Control"] == SECRET_PAGE
    html = bad.get_data(as_text=True)  # its URL carries the hash too: the standalone page
    assert 'content="no-referrer"' in html and not any(marker in html for marker in analytics_configured)
    assert good.status_code == 200 and _jwt_set_cookies(good)  # the same hash still signs in
    assert _hop_target(good) == "/my-account"


def test_a_browser_in_another_language_signs_in_after_the_language_redirect(app, sample_user_with_telegram):
    """The site first writes a browser's language into the URL (a 302 to the same URL plus `lang=`): that redirect
    spends nothing, and the sign-in completes on the next request."""
    browser = app.test_client()
    url = _url("/my-account", {"id": str(TG_ID), "first_name": "Ali"})

    first = browser.get(url, headers={**CROSS, "Accept-Language": "ru"})

    assert first.status_code == 302 and not _jwt_set_cookies(first)
    location = urlsplit(first.headers["Location"])
    assert location.path == TELEGRAM_LOGIN_PATH and parse_qs(location.query)["lang"] == ["ru"]
    signed_in = browser.get(first.headers["Location"], headers={**CROSS, "Accept-Language": "ru"})
    assert _hop_target(signed_in) == "/my-account"
    assert _jwt_set_cookies(signed_in) == sorted(JWT_COOKIES)


def test_eleven_attempts_an_hour_for_one_account_are_refused(
    app, sample_user_with_telegram, seeded_login_copy, app_records
):
    now = int(time.time())
    taps = [
        _url("/my-account", {"id": str(TG_ID), "first_name": "Ali"}, auth_date=now - n, lang="en") for n in range(11)
    ]

    answers = [app.test_client().get(tap) for tap in taps]

    assert [answer.status_code for answer in answers] == [200] * 10 + [429]
    assert all(_jwt_set_cookies(answer) for answer in answers[:10]) and not _jwt_set_cookies(answers[10])
    text = BeautifulSoup(answers[10].get_data(as_text=True), "html.parser").get_text(" ", strip=True)
    assert seeded_login_copy["web.telegram_login.busy"]["en"] in text
    [audit] = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILURE).all()
    assert (audit.error_message, audit.severity) == ("telegram_login:rate_limited", AuditSeverity.LOW)
    assert [record.levelno for record in _audit_lines(app_records, "login_failure")] == [INFO]


def test_the_route_carries_its_per_ip_limit(app):
    view = app.view_functions["frontend.telegram_login_page"]
    limits = limiter.limit_manager.decorated_limits(get_qualified_name(view))

    assert [limit.limit for limit in limits] == [parse_rate_limit(TELEGRAM_LOGIN_IP_RATE_LIMIT)]
    assert TELEGRAM_LOGIN_IP_RATE_LIMIT == "20/hour"


def test_a_breach_of_the_per_ip_limit_answers_the_busy_page_and_spends_nothing(
    app, sample_user_with_telegram, seeded_login_copy, analytics_configured, monkeypatch
):
    """The limiter is off in tests, so its own check is made to find the route's limit breached: it raises from the
    decorated view, as it does in production. The customer sees the refused page, never the API's JSON 429."""
    view = app.view_functions["frontend.telegram_login_page"]
    [route_limit] = limiter.limit_manager.decorated_limits(get_qualified_name(view))

    def _breached(*args, **kwargs):
        raise RateLimitExceeded(route_limit)

    url = _url("/my-account", {"id": str(TG_ID), "first_name": "Ali"}, lang="en")
    with monkeypatch.context() as breach:
        breach.setattr(limiter, "_check_request_limit", _breached)
        resp = app.test_client().get(url, headers=CROSS)

    html = resp.get_data(as_text=True)
    assert (resp.status_code, resp.mimetype) == (429, "text/html") and not _jwt_set_cookies(resp)
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    assert seeded_login_copy["web.telegram_login.busy"]["en"] in text
    assert resp.headers["Cache-Control"] == SECRET_PAGE and 'content="no-referrer"' in html
    assert not any(marker in html for marker in analytics_configured)
    assert _jwt_set_cookies(app.test_client().get(url))  # the hash was never spent


# --------------------------------------------------------------------------- what is never written down


class _Lines(logging.Handler):
    """The lines a production handler writes: the security filter, then the structured formatter."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.addFilter(SecuritySensitiveFilter())
        self.setFormatter(StructuredFormatter())
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def log_lines(app):
    handler = _Lines()
    loggers = [logging.getLogger(), app.logger]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        for logger, level in zip(loggers, levels):
            logger.removeHandler(handler)
            logger.setLevel(level)


# Names werkzeug decodes into `request.url` that would end a pattern early: an apostrophe (Uzbek Latin: O'tkir), a
# double quote, angle brackets, a no-break space. `marker` is the part that must never be written down.
AWKWARD_NAMES = [
    pytest.param("O'tkir", "tkir", id="apostrophe"),
    pytest.param('Zu"lfiya', "lfiya", id="double-quote"),
    pytest.param("<b>Shahnoza</b>", "Shahnoza", id="angle-brackets"),
    pytest.param("Gulnora\u00a0Karimova", "Karimova", id="no-break-space"),
]


@pytest.mark.parametrize("first_name,marker", AWKWARD_NAMES)
def test_the_query_string_never_reaches_a_log_an_audit_row_or_sentry(
    app, db, sample_user_with_telegram, log_lines, first_name, marker
):
    photo = "https://t.me/i/userpic/320/otkir.jpg"
    signed = _signed(
        {"id": str(TG_ID), "first_name": first_name, "last_name": "Valiyeva", "username": "otkir_t", "photo_url": photo}
    )
    url = _url("/my-account", {}, signed=signed)
    tampered = _url("/my-account", {}, signed={**signed, "last_name": "Valiyev"})
    browser = app.test_client()

    assert _hop_target(browser.get(url)) == "/my-account"  # signed in
    assert _hop_target(browser.get(url)) == "/my-account"  # replayed, and audited
    assert browser.get(tampered).status_code == 403  # refused, and audited

    secrets = (signed["hash"], marker, "Valiyev", "otkir_t", "userpic", "auth_date", signed["auth_date"])
    records = [json.loads(line) for line in log_lines]
    urls = {record["url"] for record in records if "telegram-login" in record.get("url", "")}
    assert urls == {f"http://localhost/auth/telegram-login?id={TG_ID}"}  # by structure: the id and nothing else
    assert not [line for line in log_lines if any(secret in line for secret in secrets)]
    rows = AuditLog.query.all()
    assert {row.event_type for row in rows} == {AuditEventType.LOGIN_SUCCESS, AuditEventType.LOGIN_FAILURE}
    columns = [column.name for column in AuditLog.__table__.columns]
    written = [str(getattr(row, column)) for row in rows for column in columns]
    assert not [value for value in written if any(secret in value for secret in secrets)]
    event = {"request": {"url": f"https://aqua-element.uz{url}", "query_string": urlsplit(url).query}}
    assert before_send(event)["request"] == {
        "url": f"https://aqua-element.uz/auth/telegram-login?id={TG_ID}",
        "query_string": "",
    }


AUDIT_DETAILS = "AUDIT DETAILS ["


def test_a_sign_ins_audit_details_line_is_json_that_keeps_the_id_and_nothing_else(
    app, db, sample_user_with_telegram, log_lines
):
    """Live drive D3: the DEBUG "AUDIT DETAILS" line writes the audit entry as JSON, its `url` and `referer` already cut
    down to the Telegram id. The security filter's pattern pass over the whole line read the JSON's closing quote and
    comma as part of that URL's query, so the line was no longer JSON and had lost the id. Each value now passes the
    redaction on its own, before the JSON is written, and the line is left whole."""
    photo = "https://t.me/i/userpic/320/otkir.jpg"
    signed = _signed(
        {"id": str(TG_ID), "first_name": "O'tkir", "last_name": "Valiyeva", "username": "otkir_t", "photo_url": photo}
    )
    url = _url("/my-account", {}, signed=signed)

    resp = app.test_client().get(url, headers={"Referer": f"https://aqua-element.uz{url}"})

    assert _hop_target(resp) == "/my-account"
    messages = [json.loads(line)["message"] for line in log_lines]
    details = [json.loads(message.split("]: ", 1)[1]) for message in messages if message.startswith(AUDIT_DETAILS)]
    (signed_in,) = [entry for entry in details if entry["action"] == AuditEventType.LOGIN_SUCCESS.value]
    assert signed_in["url"] == f"http://localhost/auth/telegram-login?id={TG_ID}"
    assert signed_in["referer"] == f"https://aqua-element.uz/auth/telegram-login?id={TG_ID}"
    assert signed_in["resource_id"] == str(sample_user_with_telegram.id)
    secrets = (signed["hash"], "tkir", "Valiyev", "otkir_t", "userpic", signed["auth_date"])
    assert not [message for message in messages if any(secret in message for secret in secrets)]


def test_a_traced_sign_in_sends_sentry_no_query_either(app):
    """Transactions (performance traces, sampled in production and staging) skip `before_send`, and the SDK writes the
    raw query string on them too: `before_send_transaction` redacts the request the same way and touches nothing else.
    """
    query = urlencode({"next": "/my-account", **_signed({"id": str(TG_ID), "first_name": "O'tkir"})})
    spans = [{"op": "db.redis", "description": "SET bs:telegram_login_hash:<sha256> 1"}]
    trace = {"trace_id": "a" * 32, "span_id": "b" * 16, "op": "http.server"}
    transaction = {
        "type": "transaction",
        "transaction": "frontend.telegram_login_page",
        "request": {
            "url": "https://aqua-element.uz/auth/telegram-login",
            "method": "GET",
            "query_string": query,
            "headers": {"Host": "aqua-element.uz"},
        },
        "contexts": {"trace": dict(trace)},
        "spans": [dict(span) for span in spans],
    }

    sent = before_send_transaction(transaction, {})

    assert sent["request"] == {
        "url": f"https://aqua-element.uz/auth/telegram-login?id={TG_ID}",
        "method": "GET",
        "query_string": "",
        "headers": {"Host": "aqua-element.uz"},
    }
    assert (sent["contexts"], sent["spans"], sent["transaction"]) == (
        {"trace": trace},
        spans,
        "frontend.telegram_login_page",
    )
    other = {"type": "transaction", "request": {"url": "https://aqua-element.uz/my-orders", "query_string": "page=2"}}
    assert before_send_transaction(other, {})["request"] == {
        "url": "https://aqua-element.uz/my-orders",
        "query_string": "page=2",
    }


@pytest.mark.parametrize("config", ["production.py", "staging.py"])
def test_every_sentry_init_redacts_transactions_too(config):
    """Both inits that trace requests pass both hooks from business_app.utils.sentry."""
    tree = ast.parse((REPO / "business_app" / "config" / config).read_text(encoding="utf-8"))
    imported = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "business_app.utils.sentry"
        for alias in node.names
    }
    [init] = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == "sentry_sdk.init"
    ]
    hooks = {kw.arg: imported.get(ast.unparse(kw.value)) for kw in init.keywords if kw.arg.startswith("before_send")}

    assert hooks == {"before_send": "before_send", "before_send_transaction": "before_send_transaction"}


def test_sentry_keeps_the_query_of_every_other_page(app):
    event = {"request": {"url": "https://aqua-element.uz/my-orders?page=2", "query_string": "page=2"}}

    assert before_send(event)["request"] == {
        "url": "https://aqua-element.uz/my-orders?page=2",
        "query_string": "page=2",
    }
