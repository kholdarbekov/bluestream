"""The verify and utility pages after the redesign (website redesign step 3, group "auth", unit "auth-verify",
2026-11-14): verify email, verify phone (a cabinet page now), the reorder-reminder unsubscribe page, the "Open on
website" refused page and the same-site hop.

Each flow is driven the way its page drives it: the page's own GET, then the API calls its script makes with the
payload it sends and the cookies and `X-CSRF-TOKEN` a signed-in browser has. SMS goes through the real send path to
a fake Eskiz (`captured_sms`); the verification letter's Celery task is captured, never sent.
"""

import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
import redis
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token, get_csrf_token

from business_app.models.translation import Translation
from business_app.models.user import User
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_web_ui_auth_translations import CATEGORY, TRANSLATIONS as AUTH
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_phone_signup_e2e import captured_sms  # noqa: F401

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
SAME = {"Sec-Fetch-Site": "same-origin"}
WEB_HEADERS = {"Content-Type": "application/json", "X-Platform": "web"}
NEW_PHONE = "+998907654321"


@pytest.fixture
def verify_copy(app, db):
    """The storefront rows the backend seed ships, the auth API's sentences, and the auth group's own rows."""
    seed_copy(db, ("landing.", "web.", "reorder_unsubscribe.", "api.auth.", "Open the bot"))
    for language, rows in AUTH.items():
        for key, value in rows.items():
            if Translation.query.filter_by(key=key, language=language).first() is None:
                db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


@pytest.fixture
def no_rate_limits(app):
    """The per-IP @rate_limit windows of the routes these flows call; every test runs from 127.0.0.1."""
    client = redis.from_url(app.config["REDIS_URL"])
    names = ("send_otp", "verify_phone", "resend_email_verification")

    def clear():
        client.delete(*[f"rate_limit:{name}:127.0.0.1" for name in names])

    clear()
    yield clear
    clear()


@pytest.fixture
def queued_letters(monkeypatch):
    """The verification letters the auth service queues: (user_id, token) per `send_verification_email_task.delay`."""
    from business_app.tasks import notification_tasks

    letters = []
    monkeypatch.setattr(notification_tasks.send_verification_email_task, "delay", lambda *args: letters.append(args))
    return letters


def _browser(app, user):
    """A browser after the storefront login: the httpOnly access cookie and the readable CSRF cookie."""
    token = create_access_token(identity=str(user.id))
    browser = app.test_client()
    csrf = get_csrf_token(token)
    browser.set_cookie("access_token_cookie", token)
    browser.set_cookie("csrf_access_token", csrf)
    return browser, csrf


def _post(browser, csrf, url, payload=None):
    """apiRequest's write: JSON body, `X-Platform: web`, and the CSRF cookie's value in `X-CSRF-TOKEN`."""
    return browser.post(
        f"/api/v1{url}", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf}, data=json.dumps(payload or {})
    )


def _get(client, path, lang):
    separator = "&" if "?" in path else "?"
    response = client.get(f"{path}{separator}lang={lang}", headers=SAME, follow_redirects=True)
    return response, BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _page_data(soup):
    """The page's own data island (render_page_data), not base.html's."""
    blocks = [json.loads(s.string) for s in soup.select('script[type="application/json"]') if s.string]
    return next(block for block in blocks if "i18n" in block and "api_base_url" not in block)


def _copy(key, lang):
    return AUTH[lang][key]


def _set(db, user, **fields):
    for name, value in fields.items():
        setattr(user, name, value)
    db.session.commit()


# ------------------------------------------------------------------------------------------------- verify email


@pytest.mark.parametrize("lang", LANGS)
def test_a_signed_out_visitor_can_type_the_code_and_is_offered_sign_in_for_a_new_letter(app, db, verify_copy, lang):
    response, soup = _get(app.test_client(), "/verify-email", lang)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "web.auth." not in html
    assert soup.html["lang"] == lang
    assert soup.select_one('meta[name="robots"]')["content"] == "noindex,nofollow"
    assert soup.select_one(".ae-auth__card")["data-verify-state"] == "form"
    assert soup.select_one("section.page-title h1").get_text(strip=True) == _copy("web.auth.verify_email.hero", lang)
    code = soup.select_one('form#emailVerificationForm input[name="token"]')
    assert soup.select_one(f'label[for="{code["id"]}"]').get_text(strip=True) == _copy("web.auth.verify_email.code_label", lang)
    assert soup.select_one("#resendEmail") is None  # the resend route needs a session
    sign_in = soup.select_one('.ae-auth__foot a[href^="/login"]')
    assert parse_qs(urlsplit(sign_in["href"]).query) == {"next": ["/verify-email"]}
    assert sign_in.get_text(strip=True) == _copy("web.auth.verify_email.sign_in", lang)
    assert _page_data(soup)["token"] is None


def test_the_emailed_link_confirms_the_address_through_the_page(app, db, verify_copy, sample_user, queued_letters, no_rate_limits):
    """Profile's "send again" queues a letter; its link opens the page in the `checking` state with the token in the
    page data; verify-email.js posts it; the address is confirmed and the page then says so."""
    _set(db, sample_user, email="dilnoza@mail.uz", email_verified_at=None)
    browser, csrf = _browser(app, sample_user)

    _response, soup = _get(browser, "/verify-email", "ru")
    assert soup.select_one(".ae-auth__card")["data-verify-state"] == "form"
    assert "dilnoza@mail.uz" in soup.select_one(".ae-auth__lead").get_text()
    # The page invents no pause of its own after a letter goes out (review AUTH-R9): the server's limit answers a 429.
    assert soup.select_one("#resendEmail") is not None and not soup.select_one("#resendEmail").has_attr("data-wait-template")

    resent = _post(browser, csrf, "/auth/resend-email-verification")
    assert resent.status_code == 200, resent.get_json()
    [(user_id, token)] = queued_letters
    assert user_id == sample_user.id

    # The letter's link, opened in a browser that is not signed in.
    _response, soup = _get(app.test_client(), f"/verify-email?token={token}", "ru")
    assert soup.select_one(".ae-auth__card")["data-verify-state"] == "checking"
    assert soup.select_one("#verifyBody").has_attr("hidden")
    assert _page_data(soup)["token"] == token
    assert soup.select_one("#successMessage a.ae-btn--primary")["href"] == "/login"

    verified = app.test_client().post("/api/v1/auth/verify-email", headers=WEB_HEADERS, data=json.dumps({"token": token}))
    assert verified.status_code == 200, verified.get_json()
    db.session.expire_all()
    assert db.session.get(User, sample_user.id).email_verified_at is not None

    _response, soup = _get(browser, "/verify-email", "ru")
    assert soup.select_one(".ae-auth__card")["data-verify-state"] == "already"
    assert soup.select_one("#emailVerificationForm") is None

    # The same link again: a used token is refused, and the page shows the link's error, not a raw key.
    again = app.test_client().post("/api/v1/auth/verify-email", headers=WEB_HEADERS, data=json.dumps({"token": token}))
    assert again.status_code == 400


def test_a_signed_in_customer_without_an_address_is_not_asked_to_confirm_one(app, db, verify_copy, sample_user):
    _set(db, sample_user, email=None, email_verified_at=None)
    browser, _csrf = _browser(app, sample_user)

    _response, soup = _get(browser, "/verify-email", "uz")

    assert soup.select_one(".ae-auth__card")["data-verify-state"] == "no_email"
    assert soup.select_one("#emailVerificationForm") is None and soup.select_one("#resendEmail") is None
    assert soup.select_one(".ae-empty__title").get_text(strip=True) == _copy("web.auth.verify_email.no_email_title", "uz")


# ------------------------------------------------------------------------------------------------- verify phone


def test_the_phone_page_is_a_cabinet_page(app, db, verify_copy):
    response = app.test_client().get("/verify-phone", headers=SAME)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=/verify-phone")
    assert getattr(app.view_functions["frontend.verify_phone"], "customer_page", False)


@pytest.mark.parametrize("lang", LANGS)
def test_the_phone_page_renders_in_the_cabinet_with_its_hooks(app, db, verify_copy, sample_user, lang):
    _set(db, sample_user, phone="+998901234567", phone_verified_at=None)
    browser, _csrf = _browser(app, sample_user)

    response, soup = _get(browser, "/verify-phone", lang)

    assert response.status_code == 200
    assert "web.auth." not in response.get_data(as_text=True)
    assert soup.select_one(".ae-cabinet-nav li.active a")["href"] == "/profile-settings"
    for hook in ("form#phoneForm", "form#otpForm", 'input[name="phone"]', 'input[name="otp"]', "#sentToPhone",
                 "#timer", "#resendCode", "#changePhone", "#successMessage"):
        assert soup.select_one(hook) is not None, hook
    otp = soup.select_one('input[name="otp"]')
    assert (otp["autocomplete"], otp["inputmode"], otp["maxlength"]) == ("one-time-code", "numeric", "6")
    # An unconfirmed account number fills the field; nothing is sent until the customer asks.
    assert soup.select_one('input[name="phone"]')["value"] == "+998901234567"
    assert soup.select_one("#currentPhone") is None
    assert _page_data(soup)["i18n"]["wrong_code"] == _copy("web.auth.otp.wrong", lang)


def test_a_confirmed_number_is_shown_and_the_field_waits_for_the_new_one(app, db, verify_copy, sample_user):
    """The page is also "change my number": profile settings links to plain /verify-phone, and no number rides in
    the URL (analytics, history and logs would keep it)."""
    _set(db, sample_user, phone="+998901234567", phone_verified_at=datetime.now(UTC))
    browser, _csrf = _browser(app, sample_user)

    _response, soup = _get(browser, "/verify-phone?phone=%2B998907654321", "ru")

    assert not soup.select_one('input[name="phone"]').has_attr("value")
    assert "+998901234567" in soup.select_one("#currentPhone").get_text()
    assert "907654321" not in str(soup.select_one("#phoneForm"))


def test_a_customer_confirms_a_new_number_through_the_page(app, db, verify_copy, sample_user, captured_sms, no_rate_limits):  # noqa: F811
    """verify-phone.js: send-otp with the number as typed, then verify-phone with the code from the SMS."""
    _set(db, sample_user, phone="+998901234567", phone_verified_at=None)
    browser, csrf = _browser(app, sample_user)

    sent = _post(browser, csrf, "/auth/send-otp", {"phone": "+998 90 765 43 21"})

    assert sent.status_code == 200, sent.get_json()
    body = sent.get_json()
    assert body["data"]["expires_in"] == app.config["OTP_EXPIRY_SECONDS"]
    assert body["message"] in BACKEND_TRANSLATIONS["api.auth.otp_sent"].values()  # "code sent", not "phone verified"
    assert captured_sms.send_sms.call_args.kwargs["mobile_phone"] == NEW_PHONE.lstrip("+")
    code = redis.from_url(app.config["REDIS_URL"]).get(f"sms_verification:{sample_user.id}").decode()
    assert code in captured_sms.send_sms.call_args.kwargs["message"]

    wrong = _post(browser, csrf, "/auth/verify-phone", {"otp": "000000" if code != "000000" else "111111"})
    assert wrong.status_code == 400
    db.session.expire_all()
    assert db.session.get(User, sample_user.id).phone == "+998901234567"

    confirmed = _post(browser, csrf, "/auth/verify-phone", {"otp": code})

    assert confirmed.status_code == 200, confirmed.get_json()
    assert confirmed.get_json()["data"] == {"phone": NEW_PHONE, "phone_verified": True}
    db.session.expire_all()
    user = db.session.get(User, sample_user.id)
    assert (user.phone, user.phone_verified_at is not None) == (NEW_PHONE, True)


def test_a_number_another_account_holds_is_refused_with_the_phone_sentence(
    app, db, verify_copy, sample_user, admin_user, no_rate_limits
):
    _set(db, admin_user, phone=NEW_PHONE)
    browser, csrf = _browser(app, sample_user)

    refused = _post(browser, csrf, "/auth/send-otp", {"phone": "+998 90 765 43 21"})

    assert refused.status_code == 409
    assert refused.get_json()["message"] in BACKEND_TRANSLATIONS["api.auth.phone_already_registered"].values()


def test_a_number_the_server_cannot_read_is_a_400(app, db, verify_copy, sample_user, no_rate_limits):
    browser, csrf = _browser(app, sample_user)

    refused = _post(browser, csrf, "/auth/send-otp", {"phone": "12345"})

    assert refused.status_code == 400


# ------------------------------------------------------------------------------------------ unsubscribe, refused, hop


@pytest.mark.parametrize("lang", LANGS)
def test_the_unsubscribe_page_opens_under_the_banner(app, db, verify_copy, lang):
    response, soup = _get(app.test_client(), "/reminders/unsubscribe/not-a-token", lang)

    assert response.status_code == 200
    title = BACKEND_TRANSLATIONS["reorder_unsubscribe.title"][lang]
    assert soup.select_one("section.page-title h1").get_text(strip=True) == title
    card = soup.select_one(".ae-auth__card[data-unsubscribe-state]")
    assert card["data-unsubscribe-state"] == "expired"
    assert card.select("form") == []
    bot = card.select_one("a.ae-btn[target=_blank]")
    assert bot.get_text(strip=True) == BACKEND_TRANSLATIONS["landing.footer.open_bot"][lang]


def test_the_refused_page_is_branded_and_offers_a_password_sign_in(app, db, verify_copy):
    response = app.test_client().get("/auth/telegram-login?next=//evil.example&lang=ru")

    assert response.status_code == 400
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")
    assert soup.find("script") is None and soup.find("link", rel="stylesheet") is None
    assert not re.search(r"""(src|srcset)=["']https?://""", html)  # nothing loaded from another host
    assert soup.select_one("img")["alt"]
    links = soup.select("main a[href]")
    assert links[-1]["href"] == "/login"
    assert links[-1].get_text(strip=True) == _copy("web.auth.refused.password_login", "ru")
    assert "call" not in html.lower()


def test_the_hop_page_waits_on_the_brand_and_keeps_its_one_script(app, db, verify_copy):
    response = app.test_client().get("/my-orders?lang=en", headers={"Sec-Fetch-Site": "cross-site"})

    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert [s["src"].split("?")[0] for s in soup.find_all("script")] == ["/static/js/pages/same-site-hop.js"]
    assert soup.select_one('[role="status"]')["aria-label"] == _copy("web.auth.hop.wait", "en")
    assert soup.find("link", rel="stylesheet") is None


# ------------------------------------------------------------------------------------------------------- seed


def test_the_seed_covers_every_key_these_pages_use_in_three_languages():
    templates = [
        "business_app/templates/frontend/verify_email.html",
        "business_app/templates/frontend/verify_phone.html",
        "business_app/templates/frontend/telegram_login_refused.html",
        "business_app/templates/frontend/same_site_hop.html",
        "business_app/templates/frontend/macros/auth.html",
    ]
    used = set()
    for path in templates:
        with open(path, encoding="utf-8") as handle:
            used |= set(re.findall(r"'(web\.auth\.[a-z0-9_.]*[a-z0-9_])'", handle.read()))

    assert used, "no web.auth key found"
    for lang in LANGS:
        assert used - set(AUTH[lang]) == set(), lang
    assert set(AUTH["en"]) == set(AUTH["ru"]) == set(AUTH["uz"])
    for value in AUTH["uz"].values():
        assert "'" not in value and "ʻ" not in value, value
