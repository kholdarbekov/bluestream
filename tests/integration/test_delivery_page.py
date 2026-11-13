"""Task 15: /delivery-policy is the delivery page. Driven through the real HTTP route."""

import pytest

from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
from shared.constants import get_delivery_statement
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    flat,
    get_page,
    offer_skus,
    page,
    seed_copy,
    shipped,
)

LANGS = ("uz", "ru", "en")
PAGE = "/delivery-policy"
BOT_URL = "https://t.me/aqua_link_ssot_test_bot"
FACT_KEYS = ("landing.delivery_page.heading_how", "landing.delivery_page.order_anytime",
             "landing.delivery_page.live_arrival", "landing.offer.own_bottles", "landing.delivery_page.payment")


def _main_text(soup):
    return flat(soup.select_one("main").get_text(" "))


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_is_translated_and_states_how_delivery_works(app, db, offer_skus, monkeypatch, lang):
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 0)
    soup = page(app, db, PAGE, lang)
    title = flat(shipped("landing.delivery_page.title", lang) + shipped("landing.delivery_page.title_free_suffix", lang))
    assert flat(soup.title.get_text()).startswith(title)
    assert flat(soup.select_one("main h1").get_text()) == title
    assert flat(soup.select_one('meta[name="description"]')["content"]) == flat(get_delivery_statement(lang)[0])
    text = _main_text(soup)
    for key in FACT_KEYS:
        assert flat(shipped(key, lang)) in text, key
    block = soup.select_one("main [data-offer-facts]")
    assert flat(" ".join(get_delivery_statement(lang))) in flat(block.get_text(" "))
    for gone in ("Delivery Policy", "Time Slots", "slots remain open", "Order Processing", "Failed Delivery"):
        assert gone not in text


@pytest.mark.parametrize("lang", LANGS)
def test_the_date_sentence_follows_the_configured_horizon(app, db, offer_skus, monkeypatch, lang):
    sentence = shipped("landing.delivery_page.choose_date", lang)
    assert flat(sentence.format(days=MAX_SCHEDULE_HORIZON_DAYS)) in _main_text(page(app, db, PAGE, lang))
    monkeypatch.setitem(app.config, "MAX_SCHEDULE_HORIZON_DAYS", 9)
    assert flat(sentence.format(days=9)) in _main_text(get_page(app, PAGE, lang))


@pytest.mark.parametrize("lang", LANGS)
def test_with_a_fee_the_title_drops_the_free_word(app, db, offer_skus, monkeypatch, lang):
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    soup = page(app, db, PAGE, lang)
    assert flat(soup.select_one("main h1").get_text()) == flat(shipped("landing.delivery_page.title", lang))
    assert flat(shipped("landing.delivery_page.title_free_suffix", lang)) not in flat(soup.title.get_text())


@pytest.mark.parametrize("lang", LANGS)
def test_support_points_to_the_bot_and_never_to_the_phone(app, db, offer_skus, monkeypatch, lang):
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    soup = page(app, db, PAGE, lang)
    assert soup.select('main a[href^="tel:"]') == [] and soup.select('main a[href^="mailto:"]') == []
    assert flat(soup.select_one(f'main a[href="{BOT_URL}"]').get_text()) == flat(shipped("landing.delivery_page.support", lang))


@pytest.mark.parametrize("lang", LANGS)
def test_the_bottle_line_links_to_the_terms_clause(app, db, offer_skus, lang):
    link = page(app, db, PAGE, lang).select_one('main a[href$="#returnable-bottles"]')
    assert "/terms" in link["href"]
    assert flat(link.get_text()) == flat(shipped("landing.delivery_page.bottle_terms", lang))


def test_an_agent_asking_for_markdown_reads_the_statement(app, db, offer_skus):
    seed_copy(db)
    response = app.test_client().get(PAGE, headers={"Accept": "text/markdown"})
    assert response.status_code == 200 and response.mimetype == "text/markdown"
    assert flat(get_delivery_statement("uz")[0]) in flat(response.get_data(as_text=True))


def test_the_page_loads_with_no_product_and_no_copy(app, db):
    assert app.test_client().get(PAGE).status_code == 200
