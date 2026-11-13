"""FAQ answers that state the facts (plan 2026-11-12, "live price and same-day delivery visibility"): the price, sizes,
smallest order and deposit answers on /shop and /water-delivery-faq are built from the offer facts, and the visible
list and the FAQPage JSON-LD read one list."""

import re
from decimal import Decimal

import pytest

from business_app.services.public_offer_service import build_offer_faq, get_public_offer_facts
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    LANGS,
    faq_answer,
    faqpage_jsonld_answer,
    flat,
    jsonld,
    offer_skus,
    page,
    seed_copy,
    set_price,
    shipped,
)

pytestmark = pytest.mark.integration

PRICE = Decimal("17345")
PAGES = ("/water-delivery-faq", "/shop")
POSITION = {
    "/water-delivery-faq": {"sizes": 1, "minimum": 2, "price": 4, "deposit": 5},  # 8 entries
    "/shop": {"price": 1, "sizes": 2, "minimum": 3, "deposit": 4},  # 6 entries
}
COUNT = {"/water-delivery-faq": 8, "/shop": 6}


def visible(soup):
    return [(flat(li.h3.get_text(" ")), flat(li.p.get_text(" "))) for li in soup.select(".shop-faq-list li")]


def q(entry_id, lang):
    return flat(shipped(f"landing.offer.faq.{entry_id}_q", lang))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PAGES)
def test_the_price_question_is_answered_with_the_price(app, db, offer_skus, lang, path):
    set_price(db, offer_skus.p19, PRICE)
    soup = page(app, db, path, lang)
    answer = flat(faq_answer(soup, shipped("landing.offer.faq.price_q", lang)))
    assert "17 345" in answer and "12 340" in answer and "{" not in answer
    assert flat(shipped("landing.offer.free_delivery", lang)) in answer
    assert flat(faqpage_jsonld_answer(soup, shipped("landing.offer.faq.price_q", lang))) == answer


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PAGES)
def test_the_visible_faq_and_the_faqpage_jsonld_are_the_same_list(app, db, offer_skus, lang, path):
    soup = page(app, db, path, lang)
    [faq] = jsonld(soup, "FAQPage")
    shown = visible(soup)
    assert shown == [(flat(e["name"]), flat(e["acceptedAnswer"]["text"])) for e in faq["mainEntity"]]
    assert len(shown) == COUNT[path]
    assert {i: shown[n][0] for i, n in POSITION[path].items()} == {i: q(i, lang) for i in POSITION[path]}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PAGES)
def test_five_litres_is_gone_and_the_minimum_is_the_smallest_order(app, db, offer_skus, lang, path):
    set_price(db, offer_skus.p19, PRICE)
    soup = page(app, db, path, lang)
    listed = flat(soup.select_one(".shop-faq-list").get_text(" "))
    assert not re.search(r"\b5 ?[LlЛл]\b", listed)
    for gone in ("can vary", "могут зависеть", "bog‘liq bo‘lishi mumkin", "shown on product pages"):
        assert gone not in listed
    sizes = flat(faq_answer(soup, shipped("landing.offer.faq.sizes_q", lang)))
    assert all(flat(p["size_text"][lang]) in sizes for p in get_public_offer_facts()["products"])
    minimum = flat(faq_answer(soup, shipped("landing.offer.faq.minimum_q", lang)))
    assert "34 690" in minimum and "49 360" in minimum  # 2 x 17 345 and 4 x 12 340


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PAGES)
def test_the_deposit_question_is_answered_no(app, db, offer_skus, lang, path):
    soup = page(app, db, path, lang)
    assert flat(faq_answer(soup, shipped("landing.offer.faq.deposit_q", lang))) == flat(
        shipped("landing.offer.no_deposit", lang)
    )


@pytest.mark.parametrize("path", PAGES)
def test_no_returnable_product_means_no_deposit_question(app, db, offer_skus, path):
    offer_skus.p19.is_active = False
    db.session.commit()
    questions = [question for question, _ in visible(page(app, db, path, "en"))]
    assert q("deposit", "en") not in questions and q("price", "en") in questions
    assert len(questions) == COUNT[path] - 1


@pytest.mark.parametrize("path", PAGES)
def test_no_active_product_keeps_the_other_answers(app, db, path):
    shown = visible(page(app, db, path, "en"))  # page() asserts 200
    assert len(shown) == COUNT[path] - 4
    assert not {q(i, "en") for i in ("price", "sizes", "minimum", "deposit")} & {question for question, _ in shown}


@pytest.mark.parametrize("lang", LANGS)
def test_the_shop_faq_and_the_block_say_the_same_sentences(app, db, offer_skus, lang):
    soup = page(app, db, "/shop", lang)
    price = flat(faq_answer(soup, shipped("landing.offer.faq.price_q", lang)))
    minimum = flat(faq_answer(soup, shipped("landing.offer.faq.minimum_q", lang)))
    lines = soup.select("[data-offer-facts] [data-offer-product]")
    assert len(lines) == 2
    for line in lines:
        assert flat(line.select_one('[data-offer-line="price"]').get_text(" ")) in price
        assert flat(line.select_one('[data-offer-line="smallest-order"]').get_text(" ")) in minimum
    assert flat(soup.select_one('[data-offer-facts] [data-offer-line="delivery"]').get_text(" ")) in price


def test_faq_entries_are_plain_strings_in_a_fixed_order(app, db, offer_skus):
    seed_copy(db)
    entries = build_offer_faq(get_public_offer_facts(), "uz")
    assert [e["id"] for e in entries] == ["price", "sizes", "minimum", "deposit"]
    assert all(type(e["q"]) is str and type(e["a"]) is str for e in entries)  # Markup is a str subclass: it fails here
    assert build_offer_faq({**get_public_offer_facts(), "products": []}, "uz") == []


def test_a_fee_above_zero_reaches_the_price_answer(app, db, offer_skus, monkeypatch):
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    answer = flat(faq_answer(page(app, db, "/water-delivery-faq", "en"), shipped("landing.offer.faq.price_q", "en")))
    assert "5 000" in answer and flat(shipped("landing.offer.free_delivery", "en")) not in answer


@pytest.mark.parametrize("path", PAGES)
def test_a_discount_puts_the_payable_price_in_the_price_answer(app, db, offer_skus, path):
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    answer = flat(faq_answer(page(app, db, path, "en"), shipped("landing.offer.faq.price_q", "en")))
    assert "15 000" in answer
    assert "17 345" not in answer
