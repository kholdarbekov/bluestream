"""The offer block (plan 2026-11-12, "live price and same-day delivery visibility"): every page that prints it states
the live price, the smallest order and the terms, in every site language, from the offer facts."""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.services.public_offer_service import (
    get_public_offer_facts,
    price_sentence,
    product_offer_facts,
    product_public_path,
    smallest_order_sentence,
)
from business_app.services.public_urls_service import language_urls
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` and `blog_post` are fixtures
    BLOCK_PAGES,
    LANGS,
    blog_post,
    flat,
    get_once,
    jsonld,
    offer_skus,
    page,
    set_price,
    shipped,
)
from tests.integration.sales_pay_builders import freeze_local_now

pytestmark = pytest.mark.integration

PRICE = Decimal("17345")  # offer_skus.p19 has minimum 2: 2 x 17 345 = 34 690


def block(app, db, path, lang):
    return page(app, db, path, lang).select_one("[data-offer-facts]")


def line(section, product, name):  # one sentence of one product's line, flattened
    selector = f'[data-offer-product="{product.id}"] [data-offer-line="{name}"]'
    return flat(section.select_one(selector).get_text(" "))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", BLOCK_PAGES)
def test_the_block_states_price_smallest_order_and_terms(app, db, offer_skus, blog_post, lang, path):
    set_price(db, offer_skus.p19, PRICE)
    section = block(app, db, path.format(id=offer_skus.p19.id, slug=blog_post.slug), lang)
    facts, text = product_offer_facts(offer_skus.p19), flat(section.get_text(" "))
    assert line(section, offer_skus.p19, "price") == flat(price_sentence(facts, lang))
    assert line(section, offer_skus.p19, "smallest-order") == flat(smallest_order_sentence(facts, lang))
    assert "17 345" in text and "34 690" in text and "23 450" not in text  # 23 450 is the fixture's first price
    for key in ("heading", "free_delivery", "no_deposit", "every_day"):
        assert flat(shipped(f"landing.offer.{key}", lang)) in text
    assert "{" not in text and "landing.offer" not in text


def test_a_price_change_shows_on_the_next_request(app, db, offer_skus):  # R1; fails with a memo on flask.g
    set_price(db, offer_skus.p19, PRICE)
    assert "17 345" in flat(block(app, db, "/", "en").get_text(" "))
    set_price(db, offer_skus.p19, Decimal("16210"))
    text = flat(block(app, db, "/", "en").get_text(" "))
    assert "16 210" in text and "32 420" in text and "17 345" not in text


def test_no_deposit_and_list_price_are_printed_only_where_they_apply(app, db, offer_skus):
    section = block(app, db, "/", "en")
    assert section.select(f'[data-offer-product="{offer_skus.p19.id}"] [data-offer-line="no-deposit"]')
    assert not section.select(f'[data-offer-product="{offer_skus.p10.id}"] [data-offer-line="no-deposit"]')
    assert not section.select('[data-offer-line="list-price"]')
    set_price(db, offer_skus.p19, PRICE, discount=Decimal("15000"))
    section = block(app, db, "/", "en")
    assert "15 000" in line(section, offer_skus.p19, "price") and "17 345" in line(section, offer_skus.p19, "list-price")
    assert "30 000" in line(section, offer_skus.p19, "smallest-order")


@pytest.mark.parametrize("lang", LANGS)
def test_a_fee_above_zero_prints_the_fee_not_free(app, db, offer_skus, monkeypatch, lang):  # Review Focus 2
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    section = block(app, db, "/", lang)
    delivery = flat(section.select_one('[data-offer-line="delivery"]').get_text(" "))
    assert delivery == flat(get_public_offer_facts()["delivery"]["line"][lang]) and "5 000" in delivery
    assert flat(shipped("landing.offer.free_delivery", lang)) not in flat(section.get_text(" "))


def test_the_block_dates_its_prices_with_todays_tashkent_date(app, db, offer_skus, monkeypatch):
    freeze_local_now(monkeypatch, datetime(2026, 11, 12, 20, 30, tzinfo=UTC))  # 01:30 on the 13th in Tashkent
    # The date sits in <time datetime>: read the line as one text, no separator around the element. The shown date's
    # localized form is tests/integration/test_web_integrate_ui.py's (it seeds the date-format row; here the ISO date
    # stands in).
    as_of = flat(block(app, db, "/", "en").select_one('[data-offer-line="as-of"]').get_text())
    assert as_of == shipped("landing.offer.as_of", "en").format(date="2026-11-13")


def test_the_home_block_sits_in_the_teaser_before_its_button(app, db, offer_skus):
    teaser = page(app, db, "/", "uz").select_one("section.coverage-teaser")
    assert teaser.select_one("[data-offer-facts] ~ a.ae-btn") is not None
    assert teaser.select_one("[data-offer-facts] h3[data-offer-heading]") is not None


def test_no_active_product_renders_no_block_and_the_page_still_loads(app, db):
    soup = page(app, db, "/", "en")  # page() asserts 200
    assert soup.select_one("[data-offer-facts]") is None
    assert soup.select_one("section.coverage-teaser a.ae-btn") is not None


@pytest.mark.parametrize("lang", LANGS)
def test_the_homepage_title_and_meta_description(app, db, offer_skus, lang):
    set_price(db, offer_skus.p19, PRICE)
    soup = page(app, db, "/", lang)
    title = flat(soup.title.get_text())
    assert title == flat(shipped("landing.offer.title_home", lang)) and not re.search(r"\d", title)
    meta = flat(soup.select_one('meta[name="description"]')["content"])
    assert meta.startswith(flat(shipped("landing.offer.meta_home", lang).split("{prices}")[0]))
    assert "17 345" in meta and "12 340" in meta and "{" not in meta


def test_the_blog_block_is_under_the_article_and_before_the_order_actions_and_the_share_row(app, db, offer_skus, blog_post):
    blog_post.tags = "water"
    db.session.commit()
    soup = page(app, db, f"/blog/{blog_post.slug}", "ru")
    assert soup.select_one(".blog-content [data-offer-facts]") is None  # not inside the article body
    assert soup.select_one(".text ~ [data-offer-facts]") is not None
    # The tag row (raw English slugs) is gone since the 2026-11 redesign; the order actions follow the price instead.
    assert soup.select_one("[data-offer-facts] ~ .ct-order") is not None
    assert soup.select_one("[data-offer-facts] ~ .post-share-option") is not None


def test_the_shop_block_is_in_the_seo_card_and_outside_the_faq_list(app, db, offer_skus):
    soup = page(app, db, "/shop", "uz")
    assert soup.select_one(".shop-seo-content .seo-card > [data-offer-facts]") is not None
    assert soup.select_one(".shop-faq-list [data-offer-facts]") is None
    assert soup.select_one("[data-offer-facts] ~ ul.shop-faq-list") is not None


@pytest.mark.parametrize("lang", LANGS)
def test_the_blog_sidebar_price_has_a_label(app, db, offer_skus, blog_post, lang):
    set_price(db, offer_skus.p19, PRICE)
    node = page(app, db, f"/blog/{blog_post.slug}", lang).select_one(f'[data-sidebar-price="{offer_skus.p19.id}"]')
    text = flat(node.get_text(" "))
    assert text.startswith(flat(shipped("landing.offer.price_label", lang))) and "17 345" in text


@pytest.mark.parametrize("path", ["/shop", "/blog/{slug}", "/process/11-step-filtration", "/about/sources"])
def test_a_page_with_no_active_product_has_no_block_and_still_loads(app, db, blog_post, path):
    soup = page(app, db, path.format(slug=blog_post.slug), "en")  # page() asserts 200
    assert soup.select_one("[data-offer-facts]") is None


@pytest.mark.parametrize("lang", LANGS)
def test_the_product_block_shows_only_this_product(app, db, offer_skus, lang):
    section = block(app, db, f"/product/{offer_skus.p10.id}", lang)
    assert [li["data-offer-product"] for li in section.select("[data-offer-product]")] == [str(offer_skus.p10.id)]
    assert not section.select('[data-offer-line="no-deposit"]')  # the 10 L bottle is not returnable
    assert "49 360" in flat(section.get_text(" "))  # 4 x 12 340


@pytest.mark.parametrize("lang", LANGS)
def test_the_product_page_states_the_smallest_order_once_from_the_facts(app, db, offer_skus, monkeypatch, lang):
    # The order floor decides: 2 x 17 345 is under 50 000, so the smallest order is 3, not the product's minimum of 2.
    monkeypatch.setitem(app.config, "MIN_ORDER_AMOUNT", 50000)
    set_price(db, offer_skus.p19, PRICE)
    soup = page(app, db, f"/product/{offer_skus.p19.id}", lang)
    facts = product_offer_facts(offer_skus.p19)
    assert facts["smallest_order_quantity"] == 3 > offer_skus.p19.min_order_quantity
    smallest = line(soup.select_one("[data-offer-facts]"), offer_skus.p19, "smallest-order")
    assert smallest == flat(smallest_order_sentence(facts, lang)) and "52 035" in smallest  # 3 x 17 345
    assert soup.select_one(".stock-min-qty") is None  # the old untranslated label read the product's own minimum
    assert "Minimum order" not in flat(soup.get_text(" "))


@pytest.mark.parametrize("lang", LANGS)
def test_the_product_page_no_longer_says_the_price_is_decided_at_checkout(app, db, offer_skus, lang):
    soup = page(app, db, f"/product/{offer_skus.p19.id}", lang)
    html = str(soup)
    assert "Final payable amount" not in html and "shown in cart/checkout" not in html
    [product] = jsonld(soup, "Product")
    assert "Pricing Information" not in [prop["name"] for prop in product.get("additionalProperty", [])]
    questions = [q["name"] for faq in jsonld(soup, "FAQPage") for q in faq["mainEntity"]]
    assert "How is product pricing determined?" not in questions


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("described", [False, True])
def test_the_product_meta_description_leads_with_the_price(app, db, offer_skus, lang, described):
    set_price(db, offer_skus.p19, PRICE)
    if described:  # owner checklist item 8
        offer_skus.p19.short_description = "Cooler bottle."
        db.session.commit()
    soup = page(app, db, f"/product/{offer_skus.p19.id}", lang)
    sentence = flat(price_sentence(product_offer_facts(offer_skus.p19), lang))
    tags = soup.select('meta[name="description"], meta[property="og:description"]')
    assert len(tags) >= 2
    for tag in tags:
        content = flat(tag["content"])
        assert content.startswith(sentence) and "None" not in content
        assert content.endswith("Cooler bottle.") is described
    if described:  # the written description still wins in JSON-LD
        assert jsonld(soup, "Product")[0]["description"] == "Cooler bottle."


@pytest.mark.parametrize("slug", [None, "aqua-element-10l"], ids=["no-slug", "slug"])
def test_a_deactivated_product_page_answers_not_found_with_no_block_until_it_is_reactivated(app, db, offer_skus, slug):
    """Owner, 2026-11-13: a retired product's address answers 404, by id and by slug, in uz, ru and en, and prints no
    block or price; reactivated, its page states its own block again."""
    offer_skus.p10.slug = slug
    offer_skus.p10.is_active = False
    db.session.commit()
    paths = sorted({f"/product/{offer_skus.p10.id}", product_public_path(offer_skus.p10)})
    for url in [url for path in paths for url in language_urls(path)]:  # uz bare, ru and en with ?lang=
        answer = get_once(app, url)
        body = answer.get_data(as_text=True)
        assert answer.status_code == 404, (url, answer.status_code, answer.headers.get("Location"))
        assert "data-offer-facts" not in body and not re.search(r"12\D?340|49\D?360", body), url  # 4 x 12 340

    offer_skus.p10.is_active = True
    db.session.commit()
    for lang in LANGS:
        for path in paths:
            section = block(app, db, path, lang)  # page() asserts 200 and seeds the copy the facts read
            sentence = flat(price_sentence(product_offer_facts(offer_skus.p10), lang))
            listed = [li["data-offer-product"] for li in section.select("[data-offer-product]")]
            assert listed == [str(offer_skus.p10.id)], (path, lang)
            assert line(section, offer_skus.p10, "price") == sentence, (path, lang)
