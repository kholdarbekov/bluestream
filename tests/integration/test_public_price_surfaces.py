"""Every storefront price prints its unit once, from one money wording (display_labels.money).

The filter used to append " UZS" to a number that the template then suffixed again ("17,345 UZS UZS" on the shop
grid and the product page), and the homepage and blog printed the raw "17,345 UZS". Prices now come out of
`display_labels.money`, so each page reads "17 345 so'm" / "17 345 сум" in its own language.
"""

import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import business_app
from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts, product_offer_facts, product_public_path
from business_app.services.public_urls_service import language_urls
from tests.integration.public_page_builders import (  # noqa: F401 -- fixtures
    LANGS,
    blog_post,
    flat,
    get_once,
    get_page,
    jsonld,
    offer_skus,
    page,
    set_price,
)

pytestmark = pytest.mark.integration

UNITS = [("uz", "so'm"), ("ru", "сум"), ("en", "so'm")]
PRICE_NODES = {  # where each page prints a product's price
    "/": ".shop-section .shop-block .price",  # the shared product card, as on /shop
    "/shop": ".product-grid .shop-block .price",
    "/product/{id}": ".price-box .price-group, .related-section .price",
    "/blog/{slug}": ".recent-posts-widget .post-date [data-price]",  # one post in the fixture: no recent-posts list, only product prices
}


def _url(path, skus, post):
    return path.format(id=skus.p19.id, slug=post.slug)


def _prices(soup, path):
    return [flat(node.get_text(" ")) for node in soup.select(PRICE_NODES[path])]


@pytest.mark.parametrize("lang,unit", UNITS)
@pytest.mark.parametrize("path", list(PRICE_NODES))
def test_each_price_carries_its_unit_exactly_once(app, db, offer_skus, blog_post, lang, unit, path):
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    soup = page(app, db, _url(path, offer_skus, blog_post), lang)
    assert sorted(_prices(soup, path)) == sorted([f"17 345 {unit}", f"12 340 {unit}"])  # both products, nothing after the unit
    assert "17,345" not in str(soup)


def test_the_shop_has_no_price_filter_or_sort(app, db, offer_skus):
    # Owner decision 2026-11-13: the price range and the sort did nothing (the route read neither), so they are gone;
    # the only price on /shop is each card's live one.
    soup = page(app, db, "/shop", "uz")
    gone = ".input-wrapper, .price-range-slider, #slider-range, #price-min, #price-max, [data-action='sort-products']"
    assert soup.select(gone) == []
    assert "UZS" not in soup.select_one("main, body").get_text(" ")


OFFER_COUNT = {"/": 1, "/shop": 1, "/product/{id}": 1, "/blog/{slug}": 0}  # JSON-LD offers for one product, per page
TEMPLATES = Path(business_app.__file__).parent / "templates" / "frontend"


def _offers(soup):
    found = [product["offers"] for product in jsonld(soup, "Product")]
    for group in jsonld(soup, "ProductGroup"):
        found += [variant["offers"] for variant in group["hasVariant"]]
    return found


def _offers_for(soup, product):
    return [o for o in _offers(soup) if urlsplit(o["url"]).path == product_public_path(product)]


def _texts(soup, selector):
    return [flat(node.get_text(" ")) for node in soup.select(selector)]


@pytest.mark.parametrize("lang,unit", UNITS)
def test_a_discount_shows_one_payable_price_on_every_surface(app, db, offer_skus, blog_post, lang, unit):
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    for path in PRICE_NODES:
        soup = page(app, db, _url(path, offer_skus, blog_post), lang)
        assert any(f"15 000 {unit}" in text for text in _prices(soup, path)), path
        assert [o["price"] for o in _offers_for(soup, offer_skus.p19)] == [15000] * OFFER_COUNT[path], path


def test_the_shop_grid_and_the_product_page_show_the_list_price_and_the_saving(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    shop = page(app, db, "/shop", "uz")
    # Every card's payable price is a .new-price (the shared product card); only the discounted one has a list price.
    discounted = next(
        card for card in shop.select(".shop-block") if card.select_one(f'[data-product-id="{offer_skus.p19.id}"]')
    )
    assert _texts(discounted, ".sale-tag, .new-price, .old-price") == ["-13%", "15 000 so'm", "17 345 so'm"]  # page order
    assert _texts(shop, ".shop-block .old-price") == ["17 345 so'm"]
    assert _texts(shop, ".shop-block .sale-tag") == ["-13%"]
    own = page(app, db, f"/product/{offer_skus.p19.id}", "uz")
    assert _texts(own, ".price-group .price-new, .price-group .price-old, .price-group .save-tag") == [
        "15 000 so'm",
        "17 345 so'm",
        "-13%",
    ]
    other = page(app, db, f"/product/{offer_skus.p10.id}", "uz")  # p19 is the related product here
    assert _texts(other, ".related-section .new-price") == ["15 000 so'm"]
    assert _texts(other, ".related-section .old-price") == ["17 345 so'm"]


@pytest.mark.parametrize("discount, charged", [(Decimal("0"), 17345), (Decimal("20000"), 20000)])  # Review Focus 1
def test_a_discount_that_is_not_one_shows_what_an_order_is_charged(app, db, offer_skus, blog_post, discount, charged):
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=discount)
    assert offer_skus.p19.catalogue_unit_price == charged
    for path in PRICE_NODES:
        soup = page(app, db, _url(path, offer_skus, blog_post), "uz")
        assert f"{charged:,} so'm".replace(",", " ") in _prices(soup, path), path
        assert soup.select(".old-price, .price-old, .save-tag, .sale-tag") == [], path
        assert [o["price"] for o in _offers_for(soup, offer_skus.p19)] == [charged] * OFFER_COUNT[path], path


@pytest.mark.parametrize("slug", [None, "aqua-element-19l"], ids=["no-slug", "slug"])
def test_a_deactivated_product_page_answers_not_found_until_it_is_reactivated(app, db, offer_skus, slug):
    """Owner, 2026-11-13: a retired product's address answers 404 like any missing page, by id and by slug, in every
    language (the URLs IndexNow is told about, so the engines learn it is gone), and the id address no longer
    redirects to the slug. Reactivated, the page comes back with its price."""
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    offer_skus.p19.slug = slug
    offer_skus.p19.is_active = False
    db.session.commit()
    paths = sorted({f"/product/{offer_skus.p19.id}", product_public_path(offer_skus.p19)})
    for url in [url for path in paths for url in language_urls(path)]:  # uz bare, ru and en with ?lang=
        answer = get_once(app, url)
        assert answer.status_code == 404, (url, answer.status_code, answer.headers.get("Location"))
        assert not re.search(r"17\D?345|23\D?450", answer.get_data(as_text=True)), url  # no price, old or new
    assert _offers_for(page(app, db, "/", "uz"), offer_skus.p19) == []

    offer_skus.p19.is_active = True
    db.session.commit()
    for lang in LANGS:
        price = flat(product_offer_facts(offer_skus.p19)["price_text"][lang])
        for path in paths:
            soup = page(app, db, path, lang)  # asserts 200; follows the id address's redirect to the slug
            assert _texts(soup, ".price-group .price-new") == [price], (path, lang)
            assert [o["price"] for o in _offers_for(soup, offer_skus.p19)] == [17345], (path, lang)


def test_json_ld_prices_and_litres_are_numbers(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    home = page(app, db, "/", "uz")
    group = jsonld(home, "ProductGroup")[0]
    assert (group["offers"]["lowPrice"], group["offers"]["highPrice"], group["offers"]["offerCount"]) == (12340, 17345, 2)
    assert all(type(o["price"]) is int for o in _offers(home))
    assert sorted(
        p["value"] for v in group["hasVariant"] for p in v["additionalProperty"] if p["name"] == "Bottle size (litres)"
    ) == [10, 18.9]
    own = page(app, db, f"/product/{offer_skus.p19.id}", "ru")
    offer = jsonld(own, "Product")[0]["offers"]
    assert offer["price"] == 17345 and offer["url"].endswith(f"/product/{offer_skus.p19.id}?lang=ru")
    assert own.find("meta", attrs={"property": "product:price:amount"})["content"] == "17345"


def test_the_product_group_says_free_delivery_only_while_it_is_free(app, db, offer_skus, monkeypatch):  # Review Focus 2
    for key, value in (("landing.offer.free_delivery", "Free-line."), ("landing.offer.delivery_fee", "Fee-line {fee}.")):
        db.session.add(Translation(key=key, language="en", value=value, category="landing", is_active=True))
    db.session.commit()  # Task 5 seeds these two keys; this test brings its own rows, which seed_copy then leaves alone
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 0)
    description = jsonld(page(app, db, "/", "en"), "ProductGroup")[0]["description"]
    assert get_public_offer_facts()["delivery"]["line"]["en"] == "Free-line." and "Free-line." in description
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    description = jsonld(get_page(app, "/", "en"), "ProductGroup")[0]["description"]
    assert get_public_offer_facts()["delivery"]["line"]["en"] in description and "free" not in description.lower()


def _size_en(product):
    return next(p["size_text"]["en"] for p in get_public_offer_facts()["products"] if p["id"] == product.id)


@pytest.mark.parametrize("path", ["/", "/shop"])
def test_the_product_group_description_names_each_bottle_by_its_facts_size(app, db, offer_skus, path):
    description = jsonld(page(app, db, path, "en"), "ProductGroup")[0]["description"]
    assert f"choose the {_size_en(offer_skus.p19)} returnable bottle" in description  # the cooler sentence
    assert f"choose the {_size_en(offer_skus.p10)} bottle" in description  # the no-cooler sentence
    offer_skus.p19.volume = 19  # the dev database's row: the description follows it, as the variant does
    db.session.commit()
    description = jsonld(get_page(app, path, "en"), "ProductGroup")[0]["description"]
    assert _size_en(offer_skus.p19) in description and "18.9" not in description


SIZE_LABELS = {  # each product card's size chip: the size alone, no " - category" (review SITE-29)
    "/": (".shop-section .shop-block .lower-content > span.ae-product__size", "{size}"),  # the shared product card
    "/shop": (".product-grid .shop-block .lower-content > span.ae-product__size", "{size}"),
    "/product/{id}": (".related-section .shop-block .lower-content > span.ae-product__size", "{size}"),  # p10 related
}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", list(SIZE_LABELS))
def test_each_card_prints_the_size_from_the_facts(app, db, offer_skus, blog_post, lang, path):
    soup = page(app, db, _url(path, offer_skus, blog_post), lang)
    sizes = {p["id"]: flat(p["size_text"][lang]) for p in get_public_offer_facts()["products"]}
    selector, label = SIZE_LABELS[path]
    carded = [offer_skus.p10] if path == "/product/{id}" else [offer_skus.p19, offer_skus.p10]
    expected = [label.format(size=sizes[p.id], category=p.category.name) for p in carded]
    assert sorted(_texts(soup, selector)) == sorted(expected)


@pytest.mark.parametrize("lang", LANGS)
def test_the_product_page_feature_box_prints_the_size_from_the_facts(app, db, offer_skus, lang):
    soup = page(app, db, f"/product/{offer_skus.p19.id}", lang)
    size = flat(product_offer_facts(offer_skus.p19)["size_text"][lang])
    assert _texts(soup, ".features-grid .feature-card strong")[0] == size


def test_no_storefront_template_derives_a_price():
    sources = {
        name: (TEMPLATES / name).read_text(encoding="utf-8")
        for name in ("index.html", "shop.html", "product_detail.html", "blog_detail.html")
    }
    for name, source in sources.items():
        for word in ("discount_price", "base_price", "current_price"):
            assert word not in source, (name, word)
    assert {name: source.count("format_price") for name, source in sources.items()} == {
        "index.html": 0,
        "shop.html": 0,
        "product_detail.html": 0,
        "blog_detail.html": 0,
    }  # the home page's unused plan card (the last format_price) is gone with the redesign
