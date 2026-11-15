"""A storefront page that fails shows the visitor a designed page, not raw JSON (website redesign, site review SITE-19).

Before: `handle_internal_error` and the 429 handler answered every request with the API's JSON body, so an exception
in /my-account, /checkout or any template showed `{"error": "INTERNAL_ERROR", ...}` with no header, footer or way
back. Now a browser's request for a page gets templates/errors/500.html inside the site shell, in the visitor's
language, with the real status (500 or 429), not indexed, offering a retry of the same address, the home page and the
Telegram bot. An API path and a request that names no HTML keep the JSON answer.

The copy is the seed's own rows (scripts/seed_web_ui_shell_translations.py), never a copy kept here.
"""

import pytest
from bs4 import BeautifulSoup
from werkzeug.exceptions import TooManyRequests

from business_app.models.translation import Translation
from scripts.seed_web_ui_shell_translations import TRANSLATIONS
from tests.integration.public_page_builders import flat, seed_copy

pytestmark = pytest.mark.integration

BOT_URL = "https://t.me/aqua_error_page_test_bot"
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
LANGS = ("uz", "ru", "en")


@pytest.fixture
def shell_copy(app, db, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    # Production answers an unhandled exception through the 500 handler; the test app re-raises it unless told not to.
    monkeypatch.setitem(app.config, "PROPAGATE_EXCEPTIONS", False)
    seed_copy(db)
    for language, rows in TRANSLATIONS.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category="web", is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _break(app, monkeypatch, endpoint, exc):
    """Make the real view behind `endpoint` raise `exc`, as a bug or the rate limiter would."""

    def broken(*args, **kwargs):
        raise exc

    monkeypatch.setitem(app.view_functions, endpoint, broken)


def _endpoint_for(app, path):
    return next(rule.endpoint for rule in app.url_map.iter_rules() if rule.rule == path)


def _get(app, path, accept=BROWSER_ACCEPT):
    # `?lang=uz` (the default language) answers 301 to the bare address first: follow it on one client.
    return app.test_client().get(path, headers={"Accept": accept}, follow_redirects=True)


@pytest.mark.parametrize("lang", LANGS)
def test_a_page_that_fails_shows_the_designed_page_in_the_visitors_language(app, db, shell_copy, monkeypatch, lang):
    _break(app, monkeypatch, "frontend.about", RuntimeError("boom"))

    response = _get(app, f"/about?lang={lang}")

    assert response.status_code == 500
    assert response.mimetype == "text/html"
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert soup.html["lang"] == lang
    assert flat(soup.select_one(".page-title h1").get_text(" ")) == TRANSLATIONS[lang]["web.error.500.title"]
    assert flat(soup.select_one(".ae-error__text").get_text(" ")) == TRANSLATIONS[lang]["web.error.500.text"]
    assert soup.select_one('meta[name="robots"]')["content"].startswith("noindex")

    retry, home = soup.select(".ae-error__actions a")
    assert retry["href"] in (f"/about?lang={lang}", "/about")  # the address that failed
    assert flat(retry.get_text(" ")) == TRANSLATIONS[lang]["web.error.500.retry"]
    assert flat(home.get_text(" ")) == TRANSLATIONS[lang]["web.error.404.home"]
    bot = soup.select_one(".ae-error__card a.ae-link")
    assert bot["href"] == BOT_URL and flat(bot.get_text(" ")) == TRANSLATIONS[lang]["web.error.404.bot"]

    # Inside the whole shell, and no raw JSON or key anywhere.
    assert soup.select_one("header#site-header") is not None and soup.select_one("footer.ae-footer") is not None
    html = response.get_data(as_text=True)
    assert "INTERNAL_ERROR" not in html and "web.error." not in html


@pytest.mark.parametrize("lang", LANGS)
def test_a_rate_limited_page_shows_the_designed_page_with_status_429(app, db, shell_copy, monkeypatch, lang):
    _break(app, monkeypatch, "frontend.about", TooManyRequests())

    response = _get(app, f"/about?lang={lang}")

    assert response.status_code == 429
    assert response.mimetype == "text/html"
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert flat(soup.select_one(".page-title h1").get_text(" ")) == TRANSLATIONS[lang]["web.error.429.title"]
    assert flat(soup.select_one(".ae-error__text").get_text(" ")) == TRANSLATIONS[lang]["web.error.429.text"]


def test_a_request_that_names_no_html_keeps_the_json_answer(app, db, shell_copy, monkeypatch):
    _break(app, monkeypatch, "frontend.about", RuntimeError("boom"))

    response = _get(app, "/about", accept="application/json")

    assert response.status_code == 500
    assert response.mimetype == "application/json"
    assert response.get_json()["error"] == "INTERNAL_ERROR"


def test_an_api_path_keeps_the_json_answer_even_for_a_browser(app, db, shell_copy, monkeypatch):
    path = "/api/v1/subscriptions/products"
    _break(app, monkeypatch, _endpoint_for(app, path), RuntimeError("boom"))

    response = _get(app, path)

    assert response.status_code == 500
    assert response.mimetype == "application/json"
    assert response.get_json()["error"] == "INTERNAL_ERROR"


# --- Review SITE-15: a page without its own description prints the translated shell default, never English "Premium".


@pytest.mark.parametrize("lang", LANGS)
def test_a_page_without_its_own_description_prints_the_translated_default(app, db, shell_copy, lang):
    soup = BeautifulSoup(_get(app, f"/forgot-password?lang={lang}").get_data(as_text=True), "html.parser")

    description = soup.select_one('meta[name="description"]')["content"]
    assert description == TRANSLATIONS[lang]["web.shell.meta.default"]
    assert "premium" not in description.lower()


@pytest.mark.parametrize("path, key", [("/login", "web.auth.login.meta"), ("/register", "web.auth.register.meta")])
@pytest.mark.parametrize("lang", LANGS)
def test_sign_in_and_sign_up_print_their_own_description(app, db, shell_copy, path, key, lang):
    from scripts.seed_web_ui_auth_translations import TRANSLATIONS as AUTH

    for language, rows in AUTH.items():
        db.session.add(Translation(key=key, language=language, value=rows[key], category="web", is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()

    soup = BeautifulSoup(_get(app, f"{path}?lang={lang}").get_data(as_text=True), "html.parser")

    assert soup.select_one('meta[name="description"]')["content"] == AUTH[lang][key]
