"""Profile settings (/profile-settings) and account security (/account-security) after the redesign (website redesign
step 3, group "account", unit "account-settings", 2026-11-14).

The pages and the API they call are driven the way a signed-in customer's browser drives them: the login cookies plus
the CSRF header base.js `apiRequest` sends, the payloads profile-settings.js and account-security.js build, in uz, ru
and en with the rows the seeds ship. Owner decision 3 (2026-11-13) is what these pin:
- nothing on either page claims a save that did not happen: the inert preferences form (with its SMS toggle), the 2FA
  card and the "security settings" form are gone, and so is the editable phone field that PUT /auth/profile ignored;
- the phone is added, confirmed or changed on plain /verify-phone, end to end (SMS provider faked, never sent);
- the personal details save for real, every option a customer can pick is one the API accepts, and the surname is
  optional;
- the password change is the real POST /auth/change-password with the server's rule; an account with no website
  password (a bot signup that has not set one: auth_service.has_website_password) gets no form, because no current
  password would work (a bot signup that sets one gets the form: tests/integration/test_website_password_recorded.py).
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import redis
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from business_app.models.user import User
from business_app.utils.password_security import hash_password, verify_password
from business_app.utils.validators import PasswordValidator
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_web_ui_account_translations import CATEGORY, TRANSLATIONS as ACCOUNT
from scripts.seed_web_ui_auth_translations import TRANSLATIONS as AUTH
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from scripts.seed_web_ui_integration_translations import TRANSLATIONS as INTEGRATION
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_auth_verify import _browser, _post, no_rate_limits  # noqa: F401
from tests.integration.test_web_phone_signup_e2e import captured_sms  # noqa: F401

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
LANGS = ("uz", "ru", "en")
PASSWORD = "TestPassword123!"
NEW_PHONE = "+998907654321"


@pytest.fixture
def settings_copy(app, db):
    """The storefront rows the backend seed ships plus the cabinet's, the auth group's and the account group's rows, and
    the site's one full-date format (web.integration.date_year, scripts/seed_web_ui_integration_translations.py)."""
    seed_copy(db, ("landing.", "web.display_labels", "cabinet.", "api.auth.", "Remind me", "Reorder", "Failed to save",
                   "Telegram is connected"))
    for seed in (CABINET, AUTH, ACCOUNT, INTEGRATION):
        for language, rows in seed.items():
            for key, value in rows.items():
                if Translation.query.filter_by(key=key, language=language).first() is None:
                    db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _set(db, user, **fields):
    for name, value in fields.items():
        setattr(user, name, value)
    db.session.commit()


def _as_bot_signup(db, user, **fields):
    """The account as telegram-register stores it: the placeholder password, which nobody chose (no password_set_at)."""
    from business_app.services.auth_service import TELEGRAM_PLACEHOLDER_PASSWORD_HASH

    _set(db, user, registration_source="telegram", password_hash=TELEGRAM_PLACEHOLDER_PASSWORD_HASH,
         password_set_at=None, **fields)


def _page(app, user, path, lang):
    """The page in `lang`, as the customer's browser asks for it."""
    browser, _csrf = _browser(app, user)
    response = browser.get(f"{path}?lang={lang}", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html[:400]
    return BeautifulSoup(html, "html.parser"), html


def _page_data(soup):
    return json.loads(soup.select_one("script#page-data").string)


def _put_profile(app, user, payload):
    """profile-settings.js's save: PUT /api/v1/auth/profile through apiRequest (cookies + X-CSRF-TOKEN)."""
    browser, csrf = _browser(app, user)
    return browser.put(
        "/api/v1/auth/profile",
        headers={"Content-Type": "application/json", "X-Platform": "web", "X-CSRF-TOKEN": csrf},
        data=json.dumps(payload),
    )


def _js_payload(**over):
    """The body profile-settings.js builds from the form: every field, the phone never."""
    payload = {"first_name": "Dilnoza", "last_name": "", "date_of_birth": "1994-03-07", "gender": "unknown",
               "preferred_language": "ru"}
    payload.update(over)
    return payload


def _copy(key, lang):
    return ACCOUNT[lang][key]


# ------------------------------------------------------------------------------------------------- the profile page


@pytest.mark.parametrize("lang", LANGS)
def test_the_profile_page_has_one_real_form_and_no_fake_controls(app, db, settings_copy, sample_user, lang):
    soup, html = _page(app, sample_user, "/profile-settings", lang)

    form = soup.select_one("#personalInfoForm")
    names = {field.get("name") for field in form.select("input, select")}
    assert names == {"first_name", "last_name", "dob_day", "dob_month", "dob_year", "gender", "preferred_language"}
    assert len(soup.select('.ae-cabinet__main button[type="submit"]')) == 1 and form.select_one('button[type="submit"]')
    # The surname is optional (a bot customer often has none): nothing blocks the save without it.
    assert not form.select_one('input[name="last_name"]').has_attr("required")
    assert form.select_one('input[name="first_name"]').has_attr("required")
    # Native selects the theme's nice-select never replaces.
    assert all("ignore" in select.get("class", []) for select in form.select("select"))
    # Gone: the inert preferences form (its SMS toggle), the contact form and its editable phone field.
    for gone in ("#preferencesForm", "#contactInfoForm", 'input[name="phone"]', 'input[name="sms_notifications"]',
                 'input[name="email_notifications"]', 'input[name="marketing_emails"]'):
        assert soup.select_one(gone) is None, gone
    assert "web.account." not in html and "web.auth." not in html
    assert _page_data(soup)["i18n"]["saved"] == _copy("web.account.profile.saved", lang)
    for script in ("js/pages/auth-form.js", "js/pages/profile-settings.js"):
        assert soup.select_one(f'script[src*="{script}"]') is not None


@pytest.mark.parametrize("lang", LANGS)
def test_the_phone_row_links_to_the_verify_phone_flow_in_each_state(app, db, settings_copy, sample_user, lang):
    cases = [
        ({"phone_verified_at": datetime.now(UTC)}, "web.account.contact.phone_change", "web.account.contact.phone_confirmed"),
        ({"phone_verified_at": None}, "web.account.contact.phone_confirm", "web.account.contact.phone_unconfirmed"),
        ({"phone": None, "phone_verified_at": None}, "web.account.contact.phone_add", "web.account.contact.phone_none"),
    ]
    for fields, action, state in cases:
        _set(db, sample_user, **fields)
        soup, _ = _page(app, sample_user, "/profile-settings", lang)
        row = soup.select_one('[data-contact="phone"]')
        link = row.select_one('a[data-action="verify-phone"]')
        # Plain /verify-phone: the number is typed there, never carried in the URL (the auth group's contract).
        assert link["href"] == "/verify-phone"
        assert link.get_text(strip=True) == _copy(action, lang)
        assert _copy(state, lang) in row.get_text(" ", strip=True)


@pytest.mark.parametrize("lang", LANGS)
def test_the_email_row_tells_the_truth_about_the_address(app, db, settings_copy, sample_user, lang):
    _set(db, sample_user, email="dilnoza@example.com", email_verified_at=None)
    soup, _ = _page(app, sample_user, "/profile-settings", lang)
    row = soup.select_one('[data-contact="email"]')
    assert "dilnoza@example.com" in row.get_text()
    assert _copy("web.account.contact.email_unconfirmed", lang) in row.get_text(" ", strip=True)
    assert row.select_one('a[data-action="verify-email"]')["href"] == "/verify-email"

    _set(db, sample_user, email="telegram_42@bluestream.local")  # a sign-up placeholder is never shown
    soup, html = _page(app, sample_user, "/profile-settings", lang)
    row = soup.select_one('[data-contact="email"]')
    assert "telegram_42" not in html
    assert row.get_text(" ", strip=True).endswith(_copy("web.account.contact.email_none", lang))
    assert row.select_one("a") is None


def test_the_generated_bot_name_is_not_offered_as_the_customers_name(app, db, settings_copy, sample_user):
    _set(db, sample_user, first_name="Telegram User")
    soup, _ = _page(app, sample_user, "/profile-settings", "en")
    assert not soup.select_one('input[name="first_name"]').has_attr("value")


BLOCKED_WITH_EMAIL = (
    "Telegram is connected, but our bot is stopped in your Telegram, so updates come by email. Open the bot and press "
    "Start to get them in Telegram again."
)


@pytest.mark.parametrize(
    "telegram_id, bot_active, email, channel, expected, link",
    [
        ("7001", True, "dilnoza@example.com", "telegram", "web.account.profile.updates_telegram", None),
        ("7001", False, "dilnoza@example.com", "email", BLOCKED_WITH_EMAIL, "bot"),
        # The review's case: a stopped bot and no email. NotificationService sends nothing, so the page says so.
        ("7001", False, None, "none", "web.account.profile.updates_blocked", "bot"),
        ("7001", False, "telegram_7001@bluestream.local", "none", "web.account.profile.updates_blocked", "bot"),
        (None, False, "dilnoza@example.com", "email", "web.account.profile.updates_email", "connect"),
        (None, False, None, "none", "web.account.profile.updates_none", "connect"),
    ],
)
def test_the_page_says_where_order_updates_go(
    app, db, settings_copy, sample_user, telegram_id, bot_active, email, channel, expected, link
):
    """The line names the channel NotificationService would use for this account (Telegram, else a real email, else
    nothing), never one the template works out on its own."""
    from business_app.services.notification_service import NotificationService

    _set(db, sample_user, telegram_id=telegram_id, is_bot_active=bot_active, email=email)
    resolved = NotificationService._resolve_telegram_else_email(sample_user)
    assert (resolved[0].value if resolved else "none") == channel

    for lang in LANGS:
        soup, html = _page(app, sample_user, "/profile-settings", lang)
        line = soup.select_one("[data-updates-channel]")
        assert line["data-updates-channel"] == channel
        text = line.get_text(" ", strip=True)
        if expected == BLOCKED_WITH_EMAIL:
            sentence = Translation.query.filter_by(key=BLOCKED_WITH_EMAIL, language=lang).first().value
        else:
            sentence = _copy(expected, lang).replace("{email}", email or "")
        assert sentence in text, (lang, text)
        if channel == "none":
            assert "email" not in text.lower() and "почт" not in text.lower() and "pochta" not in text.lower(), text
        bot = line.select_one('a[data-action="open-bot"]')
        connect = line.select_one('a[href="/my-account#telegram-link-title"]')
        assert (bot is not None) == (link == "bot")
        assert (connect is not None) == (link == "connect")
        assert "web.account." not in html


def test_the_account_date_is_the_tashkent_day_in_the_page_language(app, db, settings_copy, sample_user):
    # 22:30 UTC on 19 February is already 20 February in Tashkent.
    _set(db, sample_user, created_at=datetime(2026, 2, 19, 22, 30, tzinfo=UTC))
    for lang, date in (("en", "20 February 2026"), ("ru", "20 февраля 2026"), ("uz", "2026-yil 20-fevral")):
        soup, _ = _page(app, sample_user, "/profile-settings", lang)
        assert soup.select_one(".ae-acp__since").get_text(strip=True) == _copy("web.account.profile.created", lang).replace("{date}", date)


# ------------------------------------------------------------------------------------- saving the personal details


def test_the_personal_details_save_and_come_back_on_the_page(app, db, settings_copy, sample_user):
    response = _put_profile(app, sample_user, _js_payload(gender="female"))

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    user = db.session.get(User, sample_user.id)
    assert (user.first_name, user.last_name, user.preferred_language) == ("Dilnoza", "", "ru")
    soup, _ = _page(app, user, "/profile-settings", "en")
    assert soup.select_one('input[name="first_name"]')["value"] == "Dilnoza"
    assert (soup.select_one("#dob_day")["value"], soup.select_one("#dob_year")["value"]) == ("7", "1994")
    assert soup.select_one("#dob_month option[selected]")["value"] == "3"
    assert soup.select_one("#gender option[selected]")["value"] == "female"
    assert soup.select_one("#preferred_language option[selected]")["value"] == "ru"


def test_every_gender_and_language_the_page_offers_is_accepted(app, db, settings_copy, sample_user):
    soup, _ = _page(app, sample_user, "/profile-settings", "en")
    genders = [option["value"] for option in soup.select("#gender option")]
    languages = [option["value"] for option in soup.select("#preferred_language option")]
    assert sorted(genders) == ["female", "male", "unknown"] and sorted(languages) == ["en", "ru", "uz"]

    for gender in genders:
        for language in languages:
            response = _put_profile(app, sample_user, _js_payload(gender=gender, preferred_language=language))
            assert response.status_code == 200, (gender, language, response.get_json())
            db.session.expire_all()
            user = db.session.get(User, sample_user.id)
            assert (user.gender.value, user.preferred_language) == (gender, language)


def test_an_empty_date_of_birth_clears_it(app, db, settings_copy, sample_user):
    _put_profile(app, sample_user, _js_payload())
    response = _put_profile(app, sample_user, _js_payload(date_of_birth=None))

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(User, sample_user.id).date_of_birth is None


def test_a_date_the_server_refuses_comes_back_as_a_date_of_birth_error(app, db, settings_copy, sample_user):
    response = _put_profile(app, sample_user, _js_payload(date_of_birth="2999-01-01"))

    assert response.status_code == 400
    # The answer names the field in `details` ({field: [messages]}), which AEAuth.fieldErrors reads; the page then puts
    # its own copy under the date.
    assert response.get_json()["details"]["date_of_birth"]
    # (Nothing is committed: the service raises before its commit and the request's session is removed at teardown.
    # This test shares the app context, so its session still holds the unflushed name; the page shows no success.)


# ------------------------------------------------------------------------------------- changing the phone, end to end


def test_a_customer_changes_a_confirmed_number_from_the_profile_page(
    app, db, settings_copy, sample_user, captured_sms, no_rate_limits  # noqa: F811
):
    """Profile → /verify-phone → send-otp (the SMS provider is faked) → verify-phone → the profile shows the number."""
    _set(db, sample_user, phone_verified_at=datetime.now(UTC))
    soup, _ = _page(app, sample_user, "/profile-settings", "en")
    href = soup.select_one('[data-contact="phone"] a[data-action="verify-phone"]')["href"]
    browser, csrf = _browser(app, sample_user)
    assert browser.get(f"{href}?lang=en", follow_redirects=True).status_code == 200

    sent = _post(browser, csrf, "/auth/send-otp", {"phone": NEW_PHONE})
    assert sent.status_code == 200, sent.get_json()
    assert captured_sms.send_sms.call_args.kwargs["mobile_phone"] == NEW_PHONE.lstrip("+")
    code = redis.from_url(app.config["REDIS_URL"]).get(f"sms_verification:{sample_user.id}").decode()
    assert code in captured_sms.send_sms.call_args.kwargs["message"]
    confirmed = _post(browser, csrf, "/auth/verify-phone", {"otp": code})
    assert confirmed.status_code == 200, confirmed.get_json()

    db.session.expire_all()
    soup, _ = _page(app, db.session.get(User, sample_user.id), "/profile-settings", "en")
    row = soup.select_one('[data-contact="phone"]')
    assert NEW_PHONE in row.get_text() and _copy("web.account.contact.phone_confirmed", "en") in row.get_text()


# ------------------------------------------------------------------------------------------------- the security page


@pytest.mark.parametrize("lang", LANGS)
def test_the_security_page_has_the_real_password_form_and_nothing_fake(app, db, settings_copy, sample_user, lang):
    soup, html = _page(app, sample_user, "/account-security", lang)

    form = soup.select_one("#changePasswordForm")
    assert [field["name"] for field in form.select("input")] == ["current_password", "new_password", "confirm_password"]
    assert [field["autocomplete"] for field in form.select("input")] == ["current-password", "new-password", "new-password"]
    # The checklist is the rule the server applies, item for item.
    items = form.select("#passwordRules [data-rule]")
    assert [item["data-rule"] for item in items] == [rule["id"] for rule in PasswordValidator.requirements()]
    for gone in ('[data-action="setup-2fa"]', '[data-action="verify-phone-first"]', "#securitySettingsForm",
                 "#show_passwords", ".account-activity-section"):
        assert soup.select_one(gone) is None, gone
    assert "web.account." not in html and "web.auth." not in html
    assert soup.select_one('[data-signin="password"]') is not None
    assert _page_data(soup)["i18n"]["wrong_current"] == _copy("web.account.security.wrong_current", lang)


def test_the_last_sign_in_is_shown_in_tashkent_time(app, db, settings_copy, sample_user):
    _set(db, sample_user, last_login=datetime(2026, 3, 7, 7, 14, tzinfo=UTC))
    soup, _ = _page(app, sample_user, "/account-security", "ru")
    assert soup.select_one('[data-signin="last"] dd').get_text(strip=True) == "7 марта 2026, 12:14"


def _change(app, user, current, new):
    """account-security.js's request: POST /api/v1/auth/change-password through apiRequest."""
    browser, csrf = _browser(app, user)
    return _post(browser, csrf, "/auth/change-password", {"current_password": current, "new_password": new})


def test_the_password_changes_through_the_page_request(app, db, settings_copy, sample_user):
    response = _change(app, sample_user, PASSWORD, "NewPassword456")

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    assert verify_password("NewPassword456", db.session.get(User, sample_user.id).password_hash)


def test_a_wrong_current_password_is_a_401_and_changes_nothing(app, db, settings_copy, sample_user):
    """The 401 the page maps to "That isn't your current password" (not a lapsed login: no `reauth`)."""
    response = _change(app, sample_user, "NotMyPassword1", "NewPassword456")

    assert response.status_code == 401
    assert not response.get_json().get("reauth")
    db.session.expire_all()
    assert verify_password(PASSWORD, db.session.get(User, sample_user.id).password_hash)


def test_a_password_the_rule_refuses_comes_back_under_the_password_field(app, db, settings_copy, sample_user):
    response = _change(app, sample_user, PASSWORD, "short")

    assert response.status_code == 400
    assert any(entry.startswith("password:") for entry in response.get_json()["errors"])
    db.session.expire_all()
    assert verify_password(PASSWORD, db.session.get(User, sample_user.id).password_hash)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("email", ["dilnoza@example.com", None])
def test_a_bot_signup_gets_no_password_form_but_a_way_forward(app, db, settings_copy, sample_user, lang, email):
    _as_bot_signup(db, sample_user, email=email)
    soup, html = _page(app, sample_user, "/account-security", lang)

    assert soup.select_one("#changePasswordForm") is None
    panel = soup.select_one('[data-password-state="none"]')
    assert panel.select_one("h3").get_text(strip=True) == _copy("web.account.security.nopass_title", lang)
    if email:
        # The link is sent from this page, to the account's own email (no empty "Forgot your password?" form).
        assert panel.select_one('a[href="/forgot-password"]') is None
        form = panel.select_one("#passwordLinkForm")
        assert form.select_one('button[type="submit"]').get_text(strip=True) == _copy("web.account.security.send_link", lang)
        assert _copy("web.account.security.nopass_email", lang) in panel.get_text()
        sent = panel.select_one("#passwordLinkSent")
        assert sent.has_attr("hidden")
        assert _copy("web.account.security.link_sent", lang).replace("{email}", email) in sent.get_text()
        assert _page_data(soup)["link_email"] == email
        assert soup.select_one('script[src*="account-security.js"]') is not None
    else:
        assert soup.select_one('script[src*="account-security.js"]') is None
        bot = panel.select_one('a[target="_blank"]')
        assert bot is not None and bot.get_text(strip=True) == BACKEND_TRANSLATIONS["landing.footer.open_bot"][lang]
        assert _copy("web.account.security.nopass_bot", lang) in panel.get_text()
    assert soup.select_one('[data-signin="password"]') is None
    assert "web.account." not in html


def test_the_set_password_button_sends_the_link_to_the_accounts_email(app, db, settings_copy, sample_user, monkeypatch):
    """account-security.js's request: POST /api/v1/auth/forgot-password {identifier: page data link_email}, through
    apiRequest (cookies + CSRF header). The emailed-link task is queued for this account (Celery mocked)."""
    from business_app.tasks import notification_tasks

    _as_bot_signup(db, sample_user, email="dilnoza@example.com")
    soup, _ = _page(app, sample_user, "/account-security", "ru")
    identifier = _page_data(soup)["link_email"]
    queued = []
    monkeypatch.setattr(notification_tasks.send_password_reset_email_task, "delay",
                        lambda user_id, token: queued.append((user_id, bool(token))))

    browser, csrf = _browser(app, sample_user)
    response = browser.post(
        "/api/v1/auth/forgot-password",
        headers={"Content-Type": "application/json", "X-Platform": "web", "X-CSRF-TOKEN": csrf},
        data=json.dumps({"identifier": identifier}),
    )

    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    assert queued == [(sample_user.id, True)]


def test_an_account_with_a_password_gets_no_link_email_in_the_page_data(app, db, settings_copy, sample_user):
    soup, _ = _page(app, sample_user, "/account-security", "en")

    assert soup.select_one("#changePasswordForm") is not None
    assert soup.select_one("#passwordLinkForm") is None
    assert _page_data(soup)["link_email"] is None


def test_the_profile_form_labels_the_empty_month_and_says_what_the_language_sets(app, db, settings_copy, sample_user):
    for lang in LANGS:
        soup, _ = _page(app, sample_user, "/profile-settings", lang)
        first = soup.select_one("#dob_month option")
        assert first["value"] == "" and first.get_text(strip=True) == _copy("web.account.profile.dob_month_unset", lang)
        select = soup.select_one("#preferred_language")
        assert soup.select_one("label[for=preferred_language]").get_text(strip=True) == _copy("web.account.profile.language", lang)
        help_id = select["aria-describedby"]
        assert soup.select_one(f"#{help_id}").get_text(strip=True) == _copy("web.account.profile.language_help", lang)


def test_the_form_a_bot_signup_does_not_get_would_have_been_a_dead_end(app, db, settings_copy, sample_user):
    """Why the form is withheld: a bot signup's stored password is one nobody knows, so no current password works."""
    from business_app.services.auth_service import has_website_password

    _as_bot_signup(db, sample_user)
    _set(db, sample_user, password_hash=hash_password("generated-token-nobody-knows"))  # the telegram-login signup's
    assert has_website_password(sample_user) is False
    assert _change(app, sample_user, PASSWORD, "NewPassword456").status_code == 401


# ---------------------------------------------------------------------------------------------------------- the seed


def test_the_seed_covers_every_account_settings_key_in_three_languages_with_native_uzbek():
    used = set()
    for name in ("profile_settings.html", "account_security.html"):
        text = (ROOT / "business_app" / "templates" / "frontend" / name).read_text(encoding="utf-8")
        used |= set(re.findall(r"'(web\.account\.[a-z0-9_.]+)'", text))
    assert used, "the templates name no web.account keys"
    for key in sorted(used):
        for lang in LANGS:
            assert ACCOUNT[lang].get(key), (key, lang)
    for key, value in ACCOUNT["uz"].items():
        if key.startswith(("web.account.contact.", "web.account.profile.", "web.account.security.")):
            assert "'" not in value and "ʻ" not in value, key
    # The page never asks a customer to phone.
    for lang in LANGS:
        for key, value in ACCOUNT[lang].items():
            if key.startswith(("web.account.profile.", "web.account.security.")):
                assert not re.search(r"\b(call|звоните|позвоните|qo‘ng‘iroq)", value, re.I), (key, lang)


@pytest.mark.parametrize(
    "lang, label", [("en", "30 September 2026"), ("ru", "30 сентября 2026"), ("uz", "2026-yil 30-sentabr")]
)
def test_the_server_full_date_matches_the_script_one(app, db, settings_copy, lang, label):
    """Review SITE-07: one full-date format. The server's label (routes._account_date_label, web.integration.date_year)
    is the string base.js AE.formatDate writes for the same day (admin_ui web/baseFormatDate.test.js pins the script)."""
    from business_app.frontend.routes import _account_date_label

    with app.test_request_context():
        assert _account_date_label(datetime(2026, 9, 29, 20, 30, tzinfo=UTC), lang)[0] == label
