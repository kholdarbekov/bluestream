"""/my-account offers the Telegram link card, or the connected note, in the page language (spec 2026-10-19 D6, D7, D13, D14)."""

import json

import pytest
from bs4 import BeautifulSoup

from scripts.seed_backend_translations import TELEGRAM_LINK_WEB_TRANSLATIONS as T
from shared.enums import UserStatus
from tests.integration.reorder_reminder_builders import customer_headers, seed_translations

pytestmark = pytest.mark.integration

CONNECTED_NOTE = "Telegram connected. Delivery updates now come to Telegram instead of email."


@pytest.fixture
def card_copy(app, db):
    """Seeded once, templates compiled afresh once: ru then en proves `|t(current_language)`."""
    seed_translations(db, list(T))
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _page(app, user, language):
    """A fresh client per language, so no ?lang= session leaks."""
    response = app.test_client().get(f"/my-account?lang={language}", headers=customer_headers(app, user))
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    return BeautifulSoup(html, "html.parser"), html


@pytest.mark.parametrize("status", [UserStatus.ACTIVE, UserStatus.PENDING_VERIFICATION])
def test_a_web_only_customer_sees_the_card_in_the_page_language(app, db, sample_user, card_copy, status):
    sample_user.status = status
    db.session.commit()
    for language in ("ru", "en"):
        soup, html = _page(app, sample_user, language)
        card = soup.select_one("#telegram-link-card")
        assert card.select_one("h5").get_text(strip=True) == T["Get delivery updates in Telegram"][language]
        assert [li.get_text(strip=True) for li in card.select("li")] == [
            T["Order again in a few taps"][language],
            T["Chat with us right in Telegram"][language],
        ]
        assert len(soup.select('[data-action="connect-telegram"]')) == 2
        assert card.find("a") is None
        assert "start=web_" not in html and soup.select_one("#telegram-link-note") is None
        data = json.loads(soup.select_one("script#page-data").string)
        assert data["language"] == language
        assert data["i18n"]["telegram_link_failed"] == T["Something went wrong. Please try again in a minute."][language]
        assert "telegram_coming_soon" not in data["i18n"]


def _connect(db, user, bot_active):
    user.telegram_id = "700100200"
    user.is_bot_active = bot_active
    db.session.commit()


def test_a_connected_customer_sees_the_note_and_no_button(app, db, sample_user, card_copy):
    _connect(db, sample_user, True)
    soup, _ = _page(app, sample_user, "ru")
    note = soup.select_one("#telegram-link-note")
    assert soup.select_one("#telegram-link-card") is None
    assert soup.select('[data-action="connect-telegram"]') == []
    assert "alert-success" in note["class"] and T[CONNECTED_NOTE]["ru"] in note.get_text()
    link = note.select_one(f'a[href="{app.config["COMPANY_TELEGRAM_BOT_URL"]}"]')
    assert link.get_text(strip=True) == T["Open the bot"]["ru"]
    assert "Telegram Bot" in soup.select_one(".welcome-section small").get_text()


def test_a_customer_who_stopped_the_bot_is_told_updates_come_by_email(app, db, sample_user, card_copy):
    _connect(db, sample_user, False)
    blocked = next(k for k in T if k.startswith("Telegram is connected, but our bot is stopped"))
    soup, _ = _page(app, sample_user, "ru")
    note = soup.select_one("#telegram-link-note")
    assert "alert-warning" in note["class"] and T[blocked]["ru"] in note.get_text()
    assert T[CONNECTED_NOTE]["ru"] not in note.get_text()
    assert soup.select_one("#telegram-link-card") is None
    assert soup.select('[data-action="connect-telegram"]') == []
