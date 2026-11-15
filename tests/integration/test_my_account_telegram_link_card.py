"""/my-account offers the Telegram link card, or the connected note, in the page language (spec 2026-10-19 D6, D7, D13, D14)."""

import json

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, TELEGRAM_LINK_WEB_TRANSLATIONS as T
from scripts.seed_web_ui_account_translations import TRANSLATIONS as ACCOUNT
from shared.enums import UserStatus
from shared.validators import is_deliverable_email
from tests.integration.reorder_reminder_builders import customer_headers, seed_translations

pytestmark = pytest.mark.integration

CONNECTED_NOTE = "Telegram connected. Delivery updates now come to Telegram instead of email."
# Profile settings' line for a stopped bot and no mailbox (scripts/seed_web_ui_account_translations.py).
BLOCKED_NO_EMAIL = "web.account.profile.updates_blocked"
# The site's one wording for opening the bot (header, footer and this note: review CAB-R18, 2026-11-14).
OPEN_BOT = "landing.footer.open_bot"


@pytest.fixture
def card_copy(app, db):
    """Seeded once, templates compiled afresh once: ru then en proves `|t(current_language)`."""
    seed_translations(db, [*T, OPEN_BOT])
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
        # An h3 under the greeting's h2 (review CAB-R13: the outline no longer skips levels).
        assert card.select_one("h3").get_text(strip=True) == T["Get delivery updates in Telegram"][language]
        assert [li.get_text(strip=True) for li in card.select("li")] == [
            T["Order again in a few taps"][language],
            T["Chat with us right in Telegram"][language],
        ]
        # One connect control, inside the card (the redesign dropped the "Account activity" duplicate, 2026-11-14).
        assert len(soup.select('[data-action="connect-telegram"]')) == 1
        assert card.select_one('button[data-action="connect-telegram"]') is not None
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
    assert "ae-dash__tg-note--ok" in note["class"] and T[CONNECTED_NOTE]["ru"] in note.get_text()
    link = note.select_one(f'a[href="{app.config["COMPANY_TELEGRAM_BOT_URL"]}"]')
    assert link.get_text(strip=True) == BACKEND_TRANSLATIONS[OPEN_BOT]["ru"]


def test_a_customer_who_stopped_the_bot_is_told_updates_come_by_email(app, db, sample_user, card_copy):
    _connect(db, sample_user, False)
    assert is_deliverable_email(sample_user.email)
    blocked = next(k for k in T if k.startswith("Telegram is connected, but our bot is stopped"))
    soup, _ = _page(app, sample_user, "ru")
    note = soup.select_one("#telegram-link-note")
    assert "ae-dash__tg-note--warn" in note["class"] and T[blocked]["ru"] in note.get_text()
    assert T[CONNECTED_NOTE]["ru"] not in note.get_text()
    assert soup.select_one("#telegram-link-card") is None
    assert soup.select('[data-action="connect-telegram"]') == []


@pytest.mark.parametrize("language", ["en", "ru", "uz"])
def test_a_customer_who_stopped_the_bot_and_has_no_email_is_promised_no_email(app, db, sample_user, card_copy, language):
    """Review SITE-03: the backend sends a customer with a stopped bot and no mailbox nothing at all
    (NotificationService._resolve_telegram_else_email), so the dashboard says updates cannot reach them, as Profile
    settings does, and never that they come by email."""
    for lang, rows in ACCOUNT.items():
        db.session.add(Translation(key=BLOCKED_NO_EMAIL, language=lang, value=rows[BLOCKED_NO_EMAIL], category="web"))
    db.session.commit()
    app.jinja_env.cache.clear()
    _connect(db, sample_user, False)
    sample_user.email = None
    db.session.commit()
    blocked = next(k for k in T if k.startswith("Telegram is connected, but our bot is stopped"))

    # `?lang=` stores the language and may redirect (uz, the default, to the bare address): follow it on one client.
    response = app.test_client().get(
        f"/my-account?lang={language}", headers=customer_headers(app, sample_user), follow_redirects=True
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    assert soup.html["lang"] == language
    note = soup.select_one("#telegram-link-note")
    assert ACCOUNT[language][BLOCKED_NO_EMAIL] in note.get_text()
    assert T[blocked][language] not in html
    assert "email" not in note.get_text(" ").lower() and "почт" not in note.get_text(" ").lower()
