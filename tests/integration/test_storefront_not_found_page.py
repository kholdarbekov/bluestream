"""A browser that opens a missing storefront page gets the branded 404 page, and the shell's assets are versioned
(website redesign step 2, the shared shell; audit shell.json "404 is a bare English text/plain page").

Before: every missing URL, and every retired product (owner, 2026-11-13: those answer 404, so old Telegram and search
links land here), answered `Page not found` as plain English text with no header, footer or way back. Now a request
that accepts HTML gets templates/errors/404.html inside the site shell, in the visitor's language, status 404, not
indexed, with the shop, the home page and the Telegram bot one tap away. API paths keep their JSON answer, and a
request that names no HTML (an <img>, a stylesheet, an agent) keeps the plain one.

The copy is the seed's own rows (scripts/seed_web_ui_shell_translations.py), never a copy kept here.
"""

import json

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from scripts.seed_web_ui_shell_translations import TRANSLATIONS
from tests.integration.public_page_builders import flat, offer_skus, seed_copy  # noqa: F401 -- fixture

pytestmark = pytest.mark.integration

BOT_URL = "https://t.me/aqua_not_found_test_bot"
# What Chrome sends when it opens a page.
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
LANGS = ("uz", "ru", "en")


@pytest.fixture
def shell_copy(app, db, monkeypatch):
    """The storefront copy the seeds ship (landing.*, the money label) plus this step's shell rows."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    seed_copy(db)
    for language, rows in TRANSLATIONS.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category="web", is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _open(app, path, accept=BROWSER_ACCEPT):
    headers = {"Accept": accept} if accept is not None else {}
    return app.test_client().get(path, headers=headers, follow_redirects=True)


def _shell(lang, key):
    return TRANSLATIONS[lang][key]


@pytest.mark.parametrize("lang", LANGS)
def test_a_browser_opening_a_missing_page_gets_the_branded_page_in_its_language(app, db, shell_copy, lang):
    response = _open(app, f"/this-page-does-not-exist?lang={lang}")

    assert response.status_code == 404
    assert response.mimetype == "text/html"
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert soup.html["lang"] == lang
    assert flat(soup.select_one("main .page-title h1").get_text(" ")) == _shell(lang, "web.error.404.title")
    assert flat(soup.select_one(".ae-error__text").get_text(" ")) == _shell(lang, "web.error.404.text")
    [robots] = soup.select('meta[name="robots"]')
    assert robots["content"].startswith("noindex")

    # The way back: the shop (primary), the home page, the Telegram bot, all in the visitor's language.
    actions = soup.select(".ae-error__actions a")
    assert [flat(a.get_text(" ")) for a in actions] == [_shell(lang, "web.error.404.shop"), _shell(lang, "web.error.404.home")]
    assert "/shop" in actions[0]["href"]
    bot = soup.select_one(".ae-error__bot")
    assert bot["href"] == BOT_URL and flat(bot.get_text(" ")) == _shell(lang, "web.error.404.bot")

    # The banner's Home crumb keeps the visitor's language, as the "home" button does.
    home_crumb = soup.select_one(".page-title .bread-crumb li a")
    assert home_crumb["href"] == actions[1]["href"]

    # Inside the whole shell: one header, the nav, the footer that points to the bot.
    assert len(soup.select("header.main-header")) == 1
    assert soup.select_one("nav.ae-nav .ae-nav__list") is not None
    assert soup.select_one("footer.main-footer .footer-top .support-box a")["href"] == BOT_URL
    # Owner, 2026-11-13: the legal entity stays in the copyright, whatever COMPANY_LEGAL_NAME says.
    assert "Blue Stream Group" in soup.select_one("footer .copyright").get_text(" ")


def test_a_retired_product_link_lands_on_the_same_page(app, db, shell_copy, offer_skus):  # noqa: F811
    offer_skus.p10.is_active = False
    db.session.commit()

    response = _open(app, f"/product/{offer_skus.p10.id}?lang=ru")

    assert response.status_code == 404
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert flat(soup.select_one("main h1").get_text(" ")) == _shell("ru", "web.error.404.title")
    assert "data-offer-facts" not in str(soup.select_one("main"))


def test_api_paths_keep_their_json_answer(app, db):
    response = _open(app, "/api/v1/this-route-does-not-exist")

    assert response.status_code == 404
    assert response.is_json and response.get_json()["error"] == "ENDPOINT_NOT_FOUND"


# What Chrome sends for an <img>: no text/html, only */* at the end.
IMAGE_ACCEPT = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"


@pytest.mark.parametrize(
    "path, accept",
    [
        pytest.param("/this-page-does-not-exist", None, id="no Accept header"),
        pytest.param("/this-page-does-not-exist", "text/markdown", id="an agent asking for Markdown"),
        pytest.param("/wp-login.php", "*/*", id="curl or a scanner sending */*"),
        pytest.param("/images/no-such-image.png", IMAGE_ACCEPT, id="an <img> outside /static/"),
        pytest.param("/static/images/no-such-image.png", IMAGE_ACCEPT, id="an <img> under /static/"),
        pytest.param("/uploads/products/no-such-image.jpg", BROWSER_ACCEPT, id="a missing upload opened in a tab"),
        pytest.param("/this-page-does-not-exist", "text/html;q=0", id="text/html refused"),
    ],
)
def test_a_request_that_names_no_html_keeps_the_plain_answer(app, db, path, accept):
    response = _open(app, path, accept)

    assert response.status_code == 404
    assert response.get_data(as_text=True) == "Page not found"


def test_a_toast_title_whose_key_is_not_seeded_is_left_out_not_printed_as_the_key(app, db):
    """Toast titles reach base.js through the page data; an unseeded key comes back as the key itself
    ("web.toast.success"), which base.js would print. Seeded titles pass through as the seed's words."""
    seed_copy(db)  # the storefront copy, but not this step's shell rows
    db.session.add(Translation(key="web.toast.error", language="en", value="Error", category="web", is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()

    soup = BeautifulSoup(_open(app, "/about?lang=en").get_data(as_text=True), "html.parser")
    i18n = json.loads(soup.select_one("script#base-page-data").string)["i18n"]

    assert {kind: i18n[kind] for kind in ("success", "error", "warning", "info")} == {
        "success": "", "error": "Error", "warning": "", "info": "",
    }


def test_every_local_stylesheet_and_script_of_the_shell_is_versioned(app, db, shell_copy):
    """nginx caches CSS and JS for hours: a deploy must never serve yesterday's stylesheet against today's markup."""
    soup = BeautifulSoup(_open(app, "/about?lang=en").get_data(as_text=True), "html.parser")

    local = [tag.get("href") for tag in soup.select('link[rel="stylesheet"]')] + [tag.get("src") for tag in soup.select("script[src]")]
    local = [url for url in local if url and url.startswith("/static/")]
    assert "/static/css/ae-ui.css" in {url.split("?")[0] for url in local}
    assert [url for url in local if "?v=" not in url] == []
