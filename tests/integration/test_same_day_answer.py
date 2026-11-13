"""The same-day answer, the FAQ phone prompts and the product page's delivery lines (plan 2026-11-12, "live price and
same-day delivery visibility"): the FAQ lists answer the same-day question with the whole delivery statement, the FAQ
page points to the bot, and the product page states delivery only through the offer block."""

import json
import re

import pytest

from shared.constants import get_delivery_statement
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    LANGS,
    faq_answer,
    faqpage_jsonld_answer,
    flat,
    jsonld,
    offer_skus,
    page,
    shipped,
)

pytestmark = pytest.mark.integration

BOT_URL = "https://t.me/aqua_link_ssot_test_bot"
OLD_QUESTIONS = (
    "How fast is water delivery?",
    "Как быстро вы доставляете воду?",
    "Как быстро осуществляется доставка воды?",
    "Suv yetkazib berish qanchalik tez amalga oshiriladi?",
    "Suv yetkazib berish qancha vaqtda amalga oshiriladi?",
)
PHONE = re.compile(r"по телефону|by phone|telefon", re.IGNORECASE)


@pytest.mark.parametrize("path", ["/water-delivery-faq", "/shop"])
@pytest.mark.parametrize("lang", LANGS)
def test_the_same_day_question_is_answered_with_the_whole_statement(app, db, offer_skus, lang, path):
    soup = page(app, db, path, lang)
    question = shipped("landing.offer.faq.same_day_q", lang)
    expected = flat(" ".join(get_delivery_statement(lang)))
    assert flat(faq_answer(soup, question)) == expected
    assert flat(faqpage_jsonld_answer(soup, question)) == expected
    assert flat(faq_answer(soup, question)).split()[0].rstrip(",.!") not in ("Yes", "Ha", "Да")  # never opens with a bare yes
    assert not any(old in str(soup) for old in OLD_QUESTIONS)


@pytest.mark.parametrize("lang", LANGS)
def test_the_faq_page_points_to_the_bot_not_the_phone(app, db, offer_skus, monkeypatch, lang):
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    soup = page(app, db, "/water-delivery-faq", lang)
    main = soup.select_one("main")
    text = flat(main.get_text(" "))
    assert main.select('a[href^="tel:"]') == [] and not PHONE.search(text)
    assert not PHONE.search(json.dumps(jsonld(soup, "FAQPage"), ensure_ascii=False))
    assert flat(shipped("landing.faq.delayed_a", lang)) in text
    assert flat(main.select_one(f'a[href="{BOT_URL}"]').get_text()) == flat(shipped("landing.faq.help", lang))


@pytest.mark.parametrize("lang", LANGS)
def test_the_faq_page_says_only_our_own_bottles_are_exchanged(app, db, offer_skus, lang):
    soup = page(app, db, "/water-delivery-faq", lang)
    question = shipped("landing.offer.faq.empties_q", lang)
    answer = flat(shipped("landing.offer.own_bottles", lang))
    assert flat(faq_answer(soup, question)) == answer == flat(faqpage_jsonld_answer(soup, question))


@pytest.mark.parametrize("lang", ("uz", "ru"))
def test_the_product_page_states_delivery_in_the_page_language(app, db, offer_skus, lang):
    soup = page(app, db, f"/product/{offer_skus.p19.id}", lang)
    for gone in ("Fast Delivery", "Same day delivery available", "Delivery windows are shown at checkout", "Read delivery policy"):
        assert gone not in str(soup)
    assert flat(get_delivery_statement(lang)[0]) in flat(soup.select_one("main [data-offer-facts]").get_text(" "))
    properties = [p["name"] for p in jsonld(soup, "Product")[0].get("additionalProperty", [])]
    assert "Delivery Information" not in properties
    questions = [q["name"] for faq in jsonld(soup, "FAQPage") for q in faq["mainEntity"]]
    assert "How is delivery scheduled for this product?" not in questions
