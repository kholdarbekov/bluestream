"""The delivery statement on the data surfaces: the facts, the offer block, the products feed and `llms.txt`
(plan 2026-11-12, "live price and same-day delivery visibility", Task 14).

Every surface prints the sentences of `get_delivery_statement()` whole; none re-words them."""

import json
import re

import pytest

from business_app.services.public_offer_service import get_public_offer_facts
from shared.constants import DELIVERY_STATEMENT_REVIEWED_ON, get_delivery_statement
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` and `blog_post` are fixtures
    BLOCK_PAGES,
    blog_post,
    flat,
    get_page,
    offer_skus,
    seed_copy,
    shipped,
)

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")


def _whole(lang):
    return flat(" ".join(get_delivery_statement(lang)))


def _section(body, heading):  # the text under one "## heading" of llms.txt
    return body.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]


def test_the_facts_carry_the_constant(app, db, offer_skus):
    delivery = get_public_offer_facts()["delivery"]
    assert delivery["statement"] == {lang: get_delivery_statement(lang) for lang in LANGS}
    assert delivery["statement_reviewed_on"] == DELIVERY_STATEMENT_REVIEWED_ON
    assert set(get_public_offer_facts(languages=("ru",))["delivery"]["statement"]) == {"ru"}


@pytest.mark.parametrize("lang", LANGS)
def test_the_block_prints_the_statement_whole_on_every_block_page(app, db, offer_skus, blog_post, lang):
    seed_copy(db)  # once; then GET many
    assert len(BLOCK_PAGES) >= 7
    for path in BLOCK_PAGES:
        url = path.format(id=offer_skus.p19.id, slug=blog_post.slug)
        block = get_page(app, url, lang).select_one("[data-offer-facts]")
        assert block is not None, url
        assert [flat(p.get_text(" ")) for p in block.select("[data-delivery-statement]")] == [_whole(lang)], url


def test_the_feed_carries_the_statement_and_its_review_date(app, db, offer_skus):
    raw = app.test_client().get("/api/public/products.json").get_data(as_text=True)
    delivery = json.loads(raw)["delivery"]
    assert delivery["statement"] == {lang: list(get_delivery_statement(lang)) for lang in LANGS}
    assert delivery["statementReviewedOn"] == DELIVERY_STATEMENT_REVIEWED_ON
    assert "deliveryTime" not in raw


def test_the_feed_carries_every_day(app, db, offer_skus):
    seed_copy(db)
    delivery = json.loads(app.test_client().get("/api/public/products.json").get_data(as_text=True))["delivery"]
    assert delivery["everyDay"] == {lang: shipped("landing.offer.every_day", lang) for lang in LANGS}


def test_llms_txt_has_a_delivery_section_with_all_nine_sentences(app, db, offer_skus):
    seed_copy(db)
    body = app.test_client().get("/llms.txt").get_data(as_text=True)
    section = _section(body, "Delivery")
    for lang in LANGS:
        assert " ".join(get_delivery_statement(lang)) in section
    assert DELIVERY_STATEMENT_REVIEWED_ON in section
    assert "same-day delivery across Tashkent" not in body
    assert "delivery windows, fees" not in body and "delivery zones, scheduling" not in body
    assert get_delivery_statement("en")[0] not in _section(body, "Aqua Club loyalty programme")
    assert ", phone" not in body


def test_llms_txt_prints_the_configured_bot_link(app, db, offer_skus, monkeypatch):
    link = "https://t.me/aqua_link_ssot_test_bot"
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", link)
    body = app.test_client().get("/llms.txt").get_data(as_text=True)
    assert link in body
    assert "t.me/aqua_element_bot" not in body


def test_llms_txt_states_delivery_with_no_product(app, db):
    assert "\n## Delivery\n" in app.test_client().get("/llms.txt").get_data(as_text=True)


def test_the_llms_txt_delivery_section_follows_the_fee(app, db, offer_skus, monkeypatch):
    seed_copy(db)
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    body = app.test_client().get("/llms.txt").get_data(as_text=True)
    fee_line = get_public_offer_facts()["delivery"]["line"]["en"].replace("\u00a0", " ")
    assert fee_line in _section(body, "Delivery")
    outside_loyalty = body.replace(_section(body, "Aqua Club loyalty programme"), "")
    assert not re.search(r"\bfree\b|bepul|бесплатн", outside_loyalty, re.IGNORECASE)
