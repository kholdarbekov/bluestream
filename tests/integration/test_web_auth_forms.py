"""The sign-in pages after the redesign (website redesign step 3, group "auth", unit "auth-forms", 2026-11-14):
/login, /register, /forgot-password, /reset-password.

Pages are fetched as a signed-out browser fetches them, in uz, ru and en, with the rows the seeds ship (the backend
seed's `landing.*` / `web.forgot_password.*` rows and the group's `scripts/seed_web_ui_auth_translations.py`). The
behaviour fixes are driven through the endpoints the page scripts call, with the payloads they send:

- register.js sends the SMS language the page publishes (`locale`), which is the page's language, not always "uz";
- the phone goes to the server as typed ("90 111 22 33"): the server is the one judge of a phone number;
- after the SMS code the new, signed-in customer goes to the server-checked `next` (`after_register_url`);
- the password checklist is the rule the server applies (PasswordValidator.requirements), and the two never disagree;
- /reset-password decides on the server whether there is a link to use, and publishes the token to the script;
- the answers the scripts branch on: a refused token is a 400 with no password error; a weak password is a 400 with
  `password: ...` errors; a wrong SMS code is a 400 with `otp_code: ...` errors.

Review fixes (fix-auth, 2026-11-14):
- a guest cart survives signing up from checkout: the cookies either sign-up answer sets are enough for the
  /cart/sync call register.js now makes before it leaves the page, by phone and by email;
- an email sign-up keeps the page's language for the account (its letters), as the SMS path already did;
- the login answer names why it refused (`data.error_code`), so the page words wrong details, a switched-off account
  and a bot account in its own copy; a number or address that already has an account is a 409 the page meets with
  the way in (sign in, the bot, a new password), and the email sign-up carries `next` through verify-email;
- the checklist and the server agree on every case in tests/fixtures/password_rule_cases.json, which the jsdom test
  runs through the real auth-form.js.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import redis
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from business_app.models.user import User
from business_app.utils.validators import PasswordValidator
from scripts.seed_web_ui_auth_translations import CATEGORY, TRANSLATIONS as AUTH
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, emailed_reset_links  # noqa: F401
from tests.integration.test_web_phone_signup_e2e import MODERATED, _sent_message, captured_sms  # noqa: F401

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
PAGES = ("/login", "/register", "/forgot-password", "/reset-password", "/reset-password/a-token")


@pytest.fixture
def auth_copy(app, db):
    """The storefront rows the backend seed ships plus the auth group's own rows."""
    seed_copy(db, ("landing.", "web.forgot_password."))
    for language, rows in AUTH.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _get(app, path, lang, client=None):
    client = client or app.test_client()
    separator = "&" if "?" in path else "?"
    response = client.get(f"{path}{separator}lang={lang}", follow_redirects=True)
    return response, BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _page_data(soup):
    """The page's own data island (render_page_data), not base.html's."""
    blocks = [json.loads(s.string) for s in soup.select('script[type="application/json"]') if s.string]
    return next(block for block in blocks if "i18n" in block and "api_base_url" not in block)


# --------------------------------------------------------------------------------------------------- the pages


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PAGES)
def test_every_sign_in_page_renders_in_the_visitors_language(app, db, auth_copy, path, lang):
    response, soup = _get(app, path, lang)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "web.auth." not in html  # no key without a row, in the markup or the page data
    assert soup.select_one("section.page-title.ae-hero--soft h1").get_text(strip=True)
    assert soup.select_one(".ae-auth__card")
    # Every label is tied to its field.
    for label in soup.select("label.ae-field__label[for]"):
        assert soup.find(id=label["for"]) is not None, label


@pytest.mark.parametrize("lang", LANGS)
def test_the_login_page_keeps_its_hooks_and_offers_the_bot(app, db, auth_copy, lang):
    _response, soup = _get(app, "/login?next=/my-orders", lang)
    form = soup.select_one("form#loginForm")

    assert form.select_one('input[name="identifier"][autocomplete="username"]')
    assert form.select_one('input#password[name="password"][autocomplete="current-password"]')
    toggle = form.select_one('button.password-toggle[data-target="password"]')
    assert toggle["aria-pressed"] == "false" and toggle["aria-label"]
    assert form.select_one("button.btn-auth[type=submit]")
    # "Remember me" was dead UI: /auth/login ignores it.
    assert not soup.select('input[name="remember_me"]')
    # The Telegram option prints the seeded hint and links the bot.
    hint = Translation.query.filter_by(key="web.forgot_password.telegram_hint", language=lang).one().value
    assert hint in soup.select_one(".ae-auth__telegram").get_text(" ", strip=True)
    assert soup.select_one(".ae-auth__telegram a.ae-btn")["href"].startswith("https://t.me/")
    # The way to sign-up keeps `next`, and the page publishes the server-checked destination.
    assert soup.select_one(".ae-auth__foot a")["href"] == "/register?next=/my-orders"
    assert _page_data(soup)["after_login_url"] == "/my-orders"


def test_the_register_page_keeps_its_hooks(app, db, auth_copy):
    _response, soup = _get(app, "/register", "ru")
    form = soup.select_one("form#registerForm")

    for name in ("phone", "email", "first_name", "last_name", "password", "confirm_password", "referral_code", "terms"):
        assert form.select_one(f'input[name="{name}"]'), name
    assert form.select_one("#phone")["autocomplete"] == "tel"
    assert form.select_one("#email").has_attr("disabled")  # phone is the default way; email is the other choice
    assert [b["data-method"] for b in form.select("[data-method]")] == ["phone", "email"]
    # The consent sentence links the real pages, not href="#".
    assert [a["href"] for a in form.select('[data-field="terms"] a')] == ["/terms", "/privacy"]
    assert not soup.select('input[name="newsletter"]')  # dead UI: neither sign-up endpoint reads it
    # The code step: one input the phone can fill from the SMS.
    code = soup.select_one("#otpVerificationSection form#otpForm input#otpCode")
    assert code["autocomplete"] == "one-time-code" and code["inputmode"] == "numeric" and code["maxlength"] == "6"
    for hook in ("maskedPhone", "otpTimerText", "resendOtpBtn", "verifyOtpBtn", "goBackToFormBtn", "registerStep"):
        assert soup.find(id=hook), hook


def test_the_forgot_page_keeps_its_hooks(app, db, auth_copy):
    _response, soup = _get(app, "/forgot-password", "uz")

    assert soup.select_one('form#forgotPasswordForm input[name="identifier"]')
    assert soup.select_one("#successMessage").has_attr("hidden")
    assert soup.select_one("#telegramHint a.ae-btn")


# ------------------------------------------------------------------------------------------------- register


@pytest.mark.parametrize("lang", LANGS)
def test_the_sms_code_goes_out_in_the_page_language(app, db, auth_copy, captured_sms, lang):  # noqa: F811
    """Before: the page published `g.locale or 'uz'` and g.locale is never set, so every code went out in Uzbek."""
    _response, soup = _get(app, "/register", lang)
    locale = _page_data(soup)["locale"]
    assert locale == lang

    init = app.test_client().post(
        "/api/v1/auth/phone/register/init",
        headers=WEB_HEADERS,
        data=json.dumps({"phone": "+998901114400", "preferred_language": locale}),
    )

    assert init.status_code == 200, init.get_json()
    assert _sent_message(captured_sms).startswith(MODERATED[lang])


def test_a_phone_sign_up_with_the_number_as_typed_lands_on_next(app, db, auth_copy, captured_sms):  # noqa: F811
    """register.js no longer keeps its own list of Uzbek prefixes: it sends what the customer typed, and the code
    step, the account and the sign-in all work on the server's normalised number. Then the browser goes where the
    page said."""
    browser = app.test_client()
    _response, soup = _get(app, "/register?next=/my-subscriptions", "ru", client=browser)
    data = _page_data(soup)
    typed = "90 111 22 77"

    init = browser.post(
        "/api/v1/auth/phone/register/init",
        headers=WEB_HEADERS,
        data=json.dumps({"phone": typed, "preferred_language": data["locale"]}),
    )
    assert init.status_code == 200, init.get_json()
    assert init.get_json()["data"]["phone_masked"] == "+998***2277"
    code = re.search(r"(\d{4,8})\s*$", _sent_message(captured_sms)).group(1)

    wrong = browser.post(
        "/api/v1/auth/phone/register/verify",
        headers=WEB_HEADERS,
        data=json.dumps(
            {"phone": typed, "otp_code": "000000" if code != "000000" else "111111", "first_name": "Ali",
             "last_name": "", "password": "Abcdefg1", "referral_code": None}
        ),
    )
    assert wrong.status_code == 400
    assert any(e.startswith("otp_code:") for e in wrong.get_json()["errors"])  # what the page puts under the code

    verify = browser.post(
        "/api/v1/auth/phone/register/verify",
        headers=WEB_HEADERS,
        data=json.dumps(
            {"phone": typed, "otp_code": code, "first_name": "Ali", "last_name": "", "password": "Abcdefg1",
             "referral_code": None}
        ),
    )
    assert verify.status_code == 201, verify.get_json()
    assert "access_token_cookie" in " ".join(verify.headers.getlist("Set-Cookie"))
    assert User.query.filter_by(phone="+998901112277").one().first_name == "Ali"
    assert data["after_register_url"] == "/my-subscriptions"


@pytest.mark.parametrize("next_path, expected", [("/my-orders?page=2", "/my-orders?page=2"), ("//evil.example", "/my-account")])
def test_register_publishes_only_a_safe_next(app, db, auth_copy, next_path, expected):
    _response, soup = _get(app, f"/register?next={next_path}", "en")

    assert _page_data(soup)["after_register_url"] == expected


def test_a_bad_phone_is_refused_under_the_phone_field(app, db, auth_copy):
    init = app.test_client().post(
        "/api/v1/auth/phone/register/init", headers=WEB_HEADERS, data=json.dumps({"phone": "12345", "preferred_language": "ru"})
    )

    assert init.status_code == 400
    assert any(e.startswith("phone:") for e in init.get_json()["errors"])


# --------------------------------------------------------------------------------------------- password rule

def _clear_rate_limit(app, route_name):
    """Every test request comes from 127.0.0.1, so a @rate_limit window (rate_limit:<route>:<ip>) fills up."""
    redis.from_url(app.config["REDIS_URL"]).delete(f"rate_limit:{route_name}:127.0.0.1")


# The server's verdicts the jsdom test runs through the real auth-form.js (admin_ui/src/__tests__/web/authForms.test.js).
PASSWORD_CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "password_rule_cases.json").read_text(encoding="utf-8")
)


def _checklist(soup):
    """The checklist as the page publishes it: [(rule id, min length or None, pattern or None)]."""
    return [
        (li["data-rule"], int(li["data-min-length"]) if li.get("data-min-length") else None, li.get("data-pattern"))
        for li in soup.select("[data-password-rules] [data-rule]")
    ]


def test_the_shared_password_cases_are_the_servers_verdicts(app, db, auth_copy):
    """tests/fixtures/password_rule_cases.json is what the jsdom test checks auth-form.js against, so every verdict in
    it is proved here against the server itself: the rule the register page publishes, which rules the server finds
    unmet (`ticks_all`), and whether POST /auth/register, sent register.js's body, takes the password (`accepted`).
    Before, this test compared a Python copy of the page's check with the server, so it could not see that the page
    counted an emoji as two characters and did not count "٣" as a digit."""
    _response, soup = _get(app, "/register", "ru")
    published = [
        {"id": rule_id, **({"min_length": minimum} if minimum else {}), **({"pattern": pattern} if pattern else {})}
        for rule_id, minimum, pattern in _checklist(soup)
    ]
    assert published == PasswordValidator.requirements() == PASSWORD_CASES["requirements"]
    browser = app.test_client()

    for number, case in enumerate(PASSWORD_CASES["cases"]):
        password = case["password"]
        assert (PasswordValidator.unmet_requirements(password) == []) is case["ticks_all"], password
        _clear_rate_limit(app, "register")  # more cases than the 10-an-hour window of one address
        answer = browser.post(
            "/api/v1/auth/register",
            headers=WEB_HEADERS,
            data=json.dumps(
                {"first_name": "Ali", "last_name": "", "password": password, "referral_code": None,
                 "email": f"checklist{number}@example.com", "preferred_language": "ru"}
            ),
        )
        password_refused = answer.status_code == 400 and any(
            e.startswith("password:") for e in answer.get_json().get("errors", [])
        )
        assert password_refused is not case["accepted"], (password, answer.get_json())


def test_the_reset_page_shows_the_same_checklist(app, db, auth_copy):
    _response, soup = _get(app, "/reset-password/a-token", "en")

    assert _checklist(soup) == [
        (r["id"], r.get("min_length"), r.get("pattern")) for r in PasswordValidator.requirements()
    ]


# --------------------------------------------------------------------------------------------- reset password


@pytest.mark.parametrize("path", ["/reset-password", "/reset-password?token=", "/reset-password/%20"])
def test_without_a_token_the_reset_page_offers_a_new_link_and_no_form(app, db, auth_copy, path):
    """Before: the form rendered and script swapped in an error after load."""
    _response, soup = _get(app, path, "ru")

    assert soup.select_one("form#resetPasswordForm") is None
    invalid = soup.select_one("#invalidLink")
    assert not invalid.has_attr("hidden")
    assert invalid.select_one('a[href="/forgot-password"]')
    assert soup.select_one('script[src*="reset-password.js"]') is None


def test_the_emailed_link_resets_the_password_through_the_page(app, db, auth_copy, sample_user, emailed_reset_links):  # noqa: F811
    browser = app.test_client()
    asked = browser.post("/api/v1/auth/forgot-password", headers=WEB_HEADERS, data=json.dumps({"identifier": sample_user.email}))
    assert asked.status_code == 200
    token = emailed_reset_links[0][1]

    for path in (f"/reset-password/{token}", f"/reset-password?token={token}"):
        _response, soup = _get(app, path, "uz", client=browser)
        assert soup.select_one("#invalidLink").has_attr("hidden")
        assert _page_data(soup)["token"] == token

    weak = browser.post(
        "/api/v1/auth/reset-password", headers=WEB_HEADERS, data=json.dumps({"token": token, "new_password": "abcdefgh1"})
    )
    assert weak.status_code == 400 and any(e.startswith("password:") for e in weak.get_json()["errors"])

    done = browser.post(
        "/api/v1/auth/reset-password", headers=WEB_HEADERS, data=json.dumps({"token": token, "new_password": "Abcdefg1"})
    )
    assert done.status_code == 200, done.get_json()

    used = browser.post(
        "/api/v1/auth/reset-password", headers=WEB_HEADERS, data=json.dumps({"token": token, "new_password": "Abcdefg2"})
    )
    # A refused token: a 400 with no password error, which the page turns into the "link not valid" state.
    assert used.status_code == 400
    assert not any(e.startswith("password:") for e in used.get_json().get("errors") or [])


# ------------------------------------------------------------------------------------------------ review fixes


def _phone_code(captured_sms):  # noqa: F811
    return re.search(r"(\d{4,8})\s*$", _sent_message(captured_sms)).group(1)


def _path_and_query(url):
    parts = urlsplit(url)
    return parts.path, parse_qs(parts.query)


def _cookie_headers(browser):
    """apiRequest's headers once a sign-in answer has set the cookies: the readable CSRF half in X-CSRF-TOKEN."""
    return {**WEB_HEADERS, "X-CSRF-TOKEN": browser.get_cookie("csrf_access_token").value}


def _sync_guest_cart(browser, cart):
    """AEAuth.syncGuestCart: POST /cart/sync {cart_items} with the browser's localStorage cart."""
    return browser.post("/api/v1/cart/sync", headers=_cookie_headers(browser), data=json.dumps({"cart_items": cart}))


def _account_cart(browser):
    answer = browser.get("/api/v1/cart/", headers=_cookie_headers(browser))
    assert answer.status_code == 200, answer.get_json()
    cart = answer.get_json()["data"]
    cart = cart.get("cart", cart) if isinstance(cart, dict) else cart
    return {item["product_id"]: item["quantity"] for item in cart["cart_items"]}


def test_a_phone_sign_up_from_checkout_keeps_the_guest_cart(app, db, auth_copy, captured_sms, sample_product):  # noqa: F811
    """Before: register.js went straight to `next` after the code, base.js then replaced the browser cart with the new
    account's empty one, and checkout bounced to an empty cart. Now the sign-up answer's cookies carry the
    /cart/sync call the page makes before it leaves, so the guest cart is the account's cart on /checkout."""
    browser = app.test_client()
    _response, soup = _get(app, "/register?next=/checkout", "ru", client=browser)
    data = _page_data(soup)
    guest_cart = [{"product_id": sample_product.id, "quantity": 13}]

    init = browser.post(
        "/api/v1/auth/phone/register/init",
        headers=WEB_HEADERS,
        data=json.dumps({"phone": "90 111 22 66", "preferred_language": data["locale"]}),
    )
    assert init.status_code == 200, init.get_json()
    verify = browser.post(
        "/api/v1/auth/phone/register/verify",
        headers=WEB_HEADERS,
        data=json.dumps(
            {"phone": "90 111 22 66", "otp_code": _phone_code(captured_sms), "first_name": "Ali", "last_name": "",
             "password": "Abcdefg1", "referral_code": None}
        ),
    )
    assert verify.status_code == 201, verify.get_json()

    synced = _sync_guest_cart(browser, guest_cart)

    assert synced.status_code == 200, synced.get_json()
    assert _account_cart(browser) == {sample_product.id: 13}
    assert data["after_register_url"] == "/checkout"


def test_an_email_sign_up_keeps_the_page_language_the_guest_cart_and_next(app, db, auth_copy, sample_product):
    """The email path: register.js sends the page's language with the details (before, every email account was
    created in English, so its verification letter was English on a Russian page), syncs the guest cart on the
    cookies the answer set, and goes to verify-email carrying the safe `next`, whose "Continue" leads back there."""
    browser = app.test_client()
    _response, soup = _get(app, "/register?next=/checkout", "ru", client=browser)
    data = _page_data(soup)
    assert data["locale"] == "ru"
    assert _path_and_query(data["verify_email_url"]) == ("/verify-email", {"next": ["/checkout"]})

    answer = browser.post(
        "/api/v1/auth/register",
        headers=WEB_HEADERS,
        data=json.dumps(
            {"first_name": "Dilnoza", "last_name": "", "password": "Abcdefg1", "referral_code": None,
             "email": "dilnoza.ru@example.com", "preferred_language": data["locale"]}
        ),
    )
    assert answer.status_code == 201, answer.get_json()
    assert User.query.filter_by(email="dilnoza.ru@example.com").one().preferred_language == "ru"

    assert _sync_guest_cart(browser, [{"product_id": sample_product.id, "quantity": 2}]).status_code == 200
    assert _account_cart(browser) == {sample_product.id: 2}

    _response, verify_page = _get(app, data["verify_email_url"], "ru", client=browser)
    assert verify_page.select_one(".ae-auth__card")["data-verify-state"] == "form"
    done = verify_page.select_one("#successMessage a.ae-btn--primary")
    assert done["href"] == "/checkout"
    assert done.get_text(strip=True) == AUTH["ru"]["web.auth.verify_email.continue"]


@pytest.mark.parametrize("sent, kept", [("uz", "uz"), ("en", "en"), ("xx", "en"), (None, "en")])
def test_an_email_sign_up_takes_only_a_supported_language(app, db, sent, kept):
    """Only uz/ru/en is taken; anything else leaves the account's default, as before the fix."""
    body = {"first_name": "Ali", "last_name": "", "password": "Abcdefg1", "referral_code": None,
            "email": f"lang-{sent}@example.com"}
    if sent is not None:
        body["preferred_language"] = sent

    _clear_rate_limit(app, "register")
    answer = app.test_client().post("/api/v1/auth/register", headers=WEB_HEADERS, data=json.dumps(body))

    assert answer.status_code == 201, answer.get_json()
    assert User.query.filter_by(email=f"lang-{sent}@example.com".lower()).one().preferred_language == kept


def test_verify_email_continues_only_to_a_safe_next(app, db, auth_copy):
    browser = app.test_client()
    signed_up = browser.post(
        "/api/v1/auth/register",
        headers=WEB_HEADERS,
        data=json.dumps({"first_name": "Ali", "last_name": "", "password": "Abcdefg1", "referral_code": None,
                         "email": "next-check@example.com", "preferred_language": "en"}),
    )
    assert signed_up.status_code == 201

    _response, soup = _get(app, "/verify-email?next=//evil.example", "en", client=browser)

    assert soup.select_one("#successMessage a.ae-btn--primary")["href"] == "/my-account"
    _response, soup = _get(app, "/register", "en")
    assert _page_data(soup)["verify_email_url"] == "/verify-email"


def _login(identifier, password, app):
    return app.test_client().post(
        "/api/v1/auth/login", headers=WEB_HEADERS, data=json.dumps({"identifier": identifier, "password": password})
    )


def test_the_login_answer_names_why_it_refused_and_the_page_words_each(app, db, auth_copy, sample_user):
    """login.js picks its own sentence by `data.error_code` (before, it showed the server's "Invalid email or
    password" to a customer who typed a phone, and uz with straight apostrophes)."""
    wrong = _login(sample_user.phone, "NotThePassword1", app)
    unknown = _login("+998900000001", "NotThePassword1", app)
    assert (wrong.status_code, wrong.get_json()["data"]["error_code"]) == (401, "INVALID_CREDENTIALS")
    assert (unknown.status_code, unknown.get_json()["data"]["error_code"]) == (401, "INVALID_CREDENTIALS")

    sample_user.status = "inactive"
    db.session.commit()
    off = _login(sample_user.phone, "TestPassword123!", app)
    assert (off.status_code, off.get_json()["data"]["error_code"]) == (401, "ACCOUNT_DISABLED")

    bot = User(email="telegram_777@bot.internal", phone="+998901117777", first_name="Bot", last_name="",
               registration_source="telegram", password_hash="x")
    db.session.add(bot)
    db.session.commit()
    bot_answer = _login("telegram_777@bot.internal", "anything1A", app)
    assert (bot_answer.status_code, bot_answer.get_json()["data"]["error_code"]) == (401, "TELEGRAM_PASSWORD_REQUIRED")

    for lang in LANGS:
        i18n = _page_data(_get(app, "/login", lang)[1])["i18n"]
        assert i18n["login_failed"] == AUTH[lang]["web.auth.login.failed"]
        assert i18n["disabled"] == AUTH[lang]["web.auth.login.disabled"]
        assert i18n["telegram_only"] and not i18n["telegram_only"].startswith("web.")


def test_a_number_that_has_an_account_gets_the_way_in(app, db, auth_copy, sample_user):
    """/auth/phone/register/init answers 409 for a number that already has an account (every bot customer). The page
    has the way in ready under the phone field: sign in (keeping `next`) or the bot, and the same for an address."""
    init = app.test_client().post(
        "/api/v1/auth/phone/register/init",
        headers=WEB_HEADERS,
        data=json.dumps({"phone": "90 123 45 67", "preferred_language": "ru"}),
    )
    assert init.status_code == 409

    email = app.test_client().post(
        "/api/v1/auth/register",
        headers=WEB_HEADERS,
        data=json.dumps({"first_name": "Ali", "last_name": "", "password": "Abcdefg1", "referral_code": None,
                         "email": sample_user.email, "preferred_language": "ru"}),
    )
    assert email.status_code == 409

    _response, soup = _get(app, "/register?next=/checkout", "ru")
    data = _page_data(soup)
    assert data["i18n"]["phone_taken"] == AUTH["ru"]["web.auth.register.phone_taken"]
    assert data["i18n"]["email_taken"] == AUTH["ru"]["web.auth.register.email_taken"]
    phone_panel = soup.select_one('[data-method-panel="phone"] #phoneTaken[hidden]')
    assert _path_and_query(phone_panel.select_one("a.ae-btn--primary")["href"]) == ("/login", {"next": ["/checkout"]})
    assert phone_panel.select_one("a[target=_blank]")["href"] == soup.select_one(".ae-auth__telegram a")["href"]  # the bot
    email_panel = soup.select_one('[data-method-panel="email"] #emailTaken[hidden]')
    assert email_panel.select_one('a[href="/forgot-password"]') is not None


# ----------------------------------------------------------------------------------------------------- seed


def test_the_seed_covers_every_key_the_pages_use_in_three_languages():
    templates = [
        "business_app/templates/frontend/login.html",
        "business_app/templates/frontend/register.html",
        "business_app/templates/frontend/forgot_password.html",
        "business_app/templates/frontend/reset_password.html",
        "business_app/templates/frontend/macros/auth.html",
    ]
    used = set()
    for path in templates:
        with open(path, encoding="utf-8") as handle:
            used |= set(re.findall(r"'(web\.auth\.[a-z0-9_.]*[a-z0-9_])'", handle.read()))
    used |= {f"web.auth.password_rule.{r['id']}" for r in PasswordValidator.requirements()}

    for lang in LANGS:
        assert used - set(AUTH[lang]) == set(), lang
    assert set(AUTH["en"]) == set(AUTH["ru"]) == set(AUTH["uz"])
    for value in AUTH["uz"].values():
        assert "'" not in value and "ʻ" not in value, value
