"""The footer of every page points to the Telegram bot chat and keeps the phone as contact data (final review W7;
owner, 2026-11-13).

The footer-top prompt was built from three database-only rows around `landing.common.call_us` ("Please Call Us to Take
an Extraordinary Service" / «Позвоните …» / "… qo'ng'iroq qiling"), beside a `tel:` box, so even `/delivery-policy`,
which tells the customer to write to the bot, ended with "call us". The prompt now prints two seeded keys around the
accent span ("Order water or write to us in our Telegram bot"), the box opens the bot, and the contact list keeps the
number under a seeded label. The old rows stay in production's database, unread: the test puts a `call_us` row in, as
production has one, to prove no footer reads it any more.
"""

import pytest

from business_app.models.translation import Translation
from tests.integration.public_page_builders import LANGS, flat, page, shipped

pytestmark = pytest.mark.integration

BOT_URL = "https://t.me/aqua_footer_test_bot"  # no code ships it: the footer must read the configured link
PHONE = "+998900000123"
PAGES = ("/", "/delivery-policy")
PROMPT_KEY = "landing.footer.order_prompt"
PROMPT_BOT_KEY = "landing.footer.order_prompt_bot"
CALL_WORDS = ("call us", "позвоните", "qo'ng'iroq", "qo\u2018ng\u2018iroq")
# What production's database holds for the retired label (dev renders these); the footer must not print them.
RETIRED_CALL_US = {"en": "Call Us", "ru": "Позвоните", "uz": "Bizga qo'ng'iroq qiling"}


def _prompt(lang: str) -> str:
    """The footer prompt as composed: the bot half (the accent span) after the prompt, first in Uzbek word order."""
    halves = [shipped(PROMPT_KEY, lang), shipped(PROMPT_BOT_KEY, lang)]
    return flat(" ".join(reversed(halves) if lang == "uz" else halves))


@pytest.fixture
def footer_config(app, db, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    monkeypatch.setitem(app.config, "COMPANY_PHONE", PHONE)
    for language, value in RETIRED_CALL_US.items():
        row = Translation(key="landing.common.call_us", language=language, value=value, category="landing")
        row.is_active = True
        db.session.add(row)
    db.session.commit()


def test_the_english_prompt_reads_as_approved():
    assert _prompt("en") == "Order water or write to us in our Telegram bot"


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PAGES)
def test_the_footer_points_to_the_bot_and_keeps_the_phone(app, db, footer_config, path, lang):
    footer = page(app, db, path, lang).select_one("footer.main-footer")

    heading = footer.select_one(".footer-top h2")
    assert flat(heading.get_text(" ")) == _prompt(lang)
    assert flat(heading.select_one("span").get_text(" ")) == flat(shipped(PROMPT_BOT_KEY, lang))

    [box] = footer.select(".footer-top .support-box a")
    assert box["href"] == BOT_URL
    assert (box["target"], box["rel"]) == ("_blank", ["noopener", "noreferrer"])
    assert box.select_one("i.fab.fa-telegram-plane") is not None
    assert flat(box.get_text(" ")) == flat(shipped("landing.footer.open_bot", lang))
    assert footer.select('.footer-top a[href^="tel:"]') == []

    [phone] = footer.select(f'.info-list a[href="tel:{PHONE}"]')
    assert phone.get_text(strip=True) == PHONE
    assert flat(phone.parent.get_text(" ")) == flat(f"{shipped('landing.footer.phone_label', lang)} {PHONE}")

    text = flat(footer.get_text(" ")).lower()
    assert not [word for word in CALL_WORDS if word in text], text
    assert "landing.footer.cta" not in text and "landing.common.call_us" not in text
