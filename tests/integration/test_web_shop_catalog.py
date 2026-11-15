"""/shop and /product/<id> after the redesign (website redesign step 3, group "shop", unit "shop-catalog", 2026-11-14).

Each page is fetched the way a visitor fetches it, in uz, ru and en, with the rows the seeds ship
(scripts/seed_web_ui_shop_translations.py and the backend seed's landing.* rows) and the two bottle SKUs of
`offer_skus`, priced as no production row is, so a price or minimum typed into a template cannot pass.

"Add to cart" (owner decision 2026-11-13) puts the smallest order the backend publishes in the cart, not one bottle
below it: the shop card carries it as `data-qty` (the card's default action, which base.js handles) and the product
page's stepper starts at it (`data-min-qty`). The
cart tests read that number off the rendered page, then send exactly what base.js `addToCart` sends —
`POST /api/v1/cart/items {product_id, quantity}` with the access cookie and `X-CSRF-TOKEN` — and read the stored
cart back through `GET /api/v1/cart/`.
"""

import json
from urllib.parse import quote

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.product import ProductCategory
from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts
from scripts.seed_web_ui_shop_translations import CATEGORY, TRANSLATIONS as SHOP
from tests.integration.public_page_builders import (  # noqa: F401 -- offer_skus is a fixture
    flat,
    get_page,
    jsonld,
    offer_skus,
    seed_copy,
    shipped,
)
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_shop_page_test_bot"


@pytest.fixture
def shop_copy(app, db, monkeypatch):
    """The storefront rows the backend seed ships (landing.*, the money labels) plus this group's own rows."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    seed_copy(db, ("landing.", "web.display_labels"))
    for language, rows in SHOP.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _s(lang, key, **values):
    return SHOP[lang][key].format(**values) if values else SHOP[lang][key]


def _smallest(product_id):
    """The smallest order the backend publishes for a product (the public offer facts)."""
    return next(p for p in get_public_offer_facts()["products"] if p["id"] == product_id)["smallest_order_quantity"]


def _card_button(soup, product_id):
    return soup.select_one(f'.product-grid [data-action="add-to-cart-shop"][data-product-id="{product_id}"]')


def _add_as_the_page_does(app, user, product_id, quantity):
    """base.js addToCart for a signed-in visitor: the cookies, X-CSRF-TOKEN, and the body it builds."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    browser, csrf = _logged_in_browser(app, token)
    added = browser.post(
        "/api/v1/cart/items",
        headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf},
        data=json.dumps({"product_id": product_id, "quantity": quantity}),
    )
    assert added.status_code == 200, added.get_data(as_text=True)[:400]
    cart = browser.get("/api/v1/cart/", headers=WEB_HEADERS)
    assert cart.status_code == 200
    return {item["product_id"]: item["quantity"] for item in cart.get_json()["data"]["cart"]["cart_items"]}


# --- /shop -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_shop_reads_in_the_visitors_language_with_one_card_per_product(app, db, offer_skus, shop_copy, lang):
    soup = get_page(app, "/shop", lang)
    html = str(soup)
    main = soup.select_one("main") or soup

    assert "web.shop." not in html, "a web.shop key reached the page untranslated"
    assert flat(main.select_one(".page-title h1").get_text(" ")) == _s(lang, "web.shop.title")
    assert _s(lang, "web.shop.lead") in flat(main.select_one(".page-title").get_text(" "))
    assert len(soup.select("h1")) == 1
    # Today's two products, the dearer one first (the offer facts' order), each with its cart button.
    ids = [int(b["data-product-id"]) for b in soup.select('.product-grid [data-action="add-to-cart-shop"]')]
    assert ids == [offer_skus.p19.id, offer_skus.p10.id]
    # No decorative widgets, no theme scroll animation, one category so no chips.
    assert soup.select(".shop-sidebar, .wow, .shop-catalog__filters, .nice-select") == []
    # The FAQ the visitor reads is the FAQPage JSON-LD, question for question.
    visible = [flat(li.select_one("h3").get_text(" ")) for li in soup.select(".shop-faq-list > li")]
    (faq_page,) = jsonld(soup, "FAQPage")
    assert visible == [flat(q["name"]) for q in faq_page["mainEntity"]]
    assert _s(lang, "web.shop.faq.subscription_q") in visible
    (item_list,) = jsonld(soup, "ItemList")
    assert item_list["name"] == _s(lang, "web.shop.itemlist_name", company=app.config["COMPANY_NAME"])
    assert item_list["numberOfItems"] == 2


def test_each_shop_card_publishes_the_smallest_order_the_backend_publishes(app, db, offer_skus, shop_copy):
    soup = get_page(app, "/shop", "ru")
    for product in (offer_skus.p19, offer_skus.p10):
        button = _card_button(soup, product.id)
        assert int(button["data-qty"]) == _smallest(product.id) == product.min_order_quantity


@pytest.mark.parametrize("which", ["p19", "p10"])
def test_add_to_cart_from_a_shop_card_stores_the_smallest_order(app, db, offer_skus, shop_copy, sample_user, which):
    product = getattr(offer_skus, which)
    soup = get_page(app, "/shop", "ru")
    quantity = int(_card_button(soup, product.id)["data-qty"])  # what base.js add-to-cart-shop hands to addToCart

    stored = _add_as_the_page_does(app, sample_user, product.id, quantity)

    assert stored == {product.id: product.min_order_quantity}


def test_category_chips_show_only_when_there_is_more_than_one_category(app, db, offer_skus, shop_copy):
    assert get_page(app, "/shop", "ru").select(".shop-catalog__filters") == []

    other = ProductCategory(name="Cups", is_active=True)
    db.session.add(other)
    db.session.commit()
    soup = get_page(app, "/shop", "ru")
    chips = soup.select(".shop-catalog__filters .ae-chip")
    assert [flat(c.get_text(" ")) for c in chips][1:] == ["Water", "Cups"]
    assert chips[0].get("aria-current") == "page"  # "All products" is the current view

    filtered = get_page(app, f"/shop?category={other.id}", "ru")
    assert filtered.select(".product-grid .shop-block") == []
    assert _s("ru", "web.shop.category_empty_text") in flat(filtered.select_one(".ae-empty").get_text(" "))


def _found(soup):
    return [int(b["data-product-id"]) for b in soup.select('.product-grid [data-action="add-to-cart-shop"]')]


def test_search_matches_a_name_whatever_its_case_and_says_so_when_nothing_matches(app, db, offer_skus, shop_copy):
    found = get_page(app, "/shop?search=aqua%20element%2010", "ru")
    assert _found(found) == [offer_skus.p10.id]
    assert found.select_one('meta[name="robots"]')["content"].startswith("noindex")

    # Nothing matched: the page says so plainly (no "check the spelling") and shows what there is under it.
    nothing = get_page(app, "/shop?search=zzzz", "ru")
    assert flat(nothing.select_one(".shop-catalog__search-count").get_text(" ")) == _s("ru", "web.shop.search_count", n=0)
    empty = nothing.select_one(".ae-empty")
    assert flat(empty.get_text(" ")) == f'{_s("ru", "web.shop.no_products")} {_s("ru", "web.shop.search_empty_text")}'
    assert _found(nothing) == [offer_skus.p19.id, offer_skus.p10.id]
    assert nothing.select_one('meta[name="robots"]')["content"].startswith("noindex")


@pytest.mark.parametrize(
    "query, expected",
    [
        ("вода", ["p19", "p10"]),  # the category, in Russian
        ("ВОДА 19 л", ["p19"]),  # category + the trade size, any case, any order
        ("19 л", ["p19"]),  # the size as the card prints it ("18,9 л (19 л)")
        ("18,9", ["p19"]),
        ("water", ["p19", "p10"]),  # the category, in English
        ("10 L", ["p10"]),
    ],
)
def test_the_header_search_finds_a_product_by_its_size_and_category_in_every_language(
    app, db, offer_skus, shop_copy, query, expected
):
    """The header's search form sends GET /shop?search=<what was typed>; a water shop finds its water."""
    category = db.session.get(ProductCategory, offer_skus.p19.category_id)
    category.set_translated("name", "Питьевая вода", "ru")
    db.session.commit()

    soup = get_page(app, "/shop?search=" + quote(query), "ru")

    assert _found(soup) == [getattr(offer_skus, which).id for which in expected]
    assert soup.select(".ae-empty") == []
    assert flat(soup.select_one(".shop-catalog__search-count").get_text(" ")) == _s(
        "ru", "web.shop.search_count", n=len(expected)
    )


@pytest.mark.parametrize("path", ["/shop?page=5", "/shop?category=999999", "/shop?category={inactive}&page=1"])
def test_a_page_past_the_last_or_an_unlisted_category_is_not_found(app, db, offer_skus, shop_copy, path):
    """No indexable empty "Page 5" or unknown-category listing: both answer 404."""
    inactive = ProductCategory(name="Retired", is_active=False)
    db.session.add(inactive)
    db.session.commit()
    app.jinja_env.cache.clear()

    response = app.test_client().get(path.format(inactive=inactive.id) + "&lang=ru", follow_redirects=True)

    assert response.status_code == 404
    assert get_page(app, "/shop?page=1", "ru").select(".product-grid .shop-block")  # page 1 still lists them


# --- /product/<id> ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_product_page_reads_in_the_visitors_language_with_one_title(app, db, offer_skus, shop_copy, lang):
    soup = get_page(app, f"/product/{offer_skus.p10.id}", lang)
    html = str(soup)

    assert "web.shop." not in html, "a web.shop key reached the page untranslated"
    assert [flat(h.get_text(" ")) for h in soup.select("h1")] == ["Aqua Element 10 L"]
    # The filler the audit found is gone: wishlist, reviews tab, "100% secure", the empty-description line.
    page_text = flat(soup.select_one("main").get_text(" "))
    for gone in ("wishlist", "Reviews", "100%", "No description available", "Verified Buying Facts", "SONI"):
        assert gone.lower() not in page_text.lower(), gone
    assert soup.select('[data-action="add-to-wishlist"], .product-tabs, #reviews') == []
    assert soup.select_one(".shop-pd__about") is None  # no description, no section
    # Order in the bot, beside "Add to cart".
    bot = soup.select_one(f'.shop-pd__actions a[href="{BOT_URL}"]')
    assert flat(bot.get_text(" ")) == _s(lang, "web.shop.order_in_bot")
    # The other size, as a card that adds its own smallest order.
    related = soup.select_one(f'.related-section [data-action="add-to-cart-shop"][data-product-id="{offer_skus.p19.id}"]')
    assert int(related["data-qty"]) == _smallest(offer_skus.p19.id)
    # Structured data in the page's language.
    # The BreadcrumbList names are the visible crumbs (Home, Shop, ... the product), in the page's language.
    (crumbs,) = jsonld(soup, "BreadcrumbList")
    visible = [flat(li.get_text(" ")) for li in soup.select(".page-title .bread-crumb li")]
    assert [c["name"] for c in crumbs["itemListElement"]] == [visible[0], visible[1], "Aqua Element 10 L"]
    assert visible[0] == shipped("landing.nav.home", lang)
    (faq_page,) = jsonld(soup, "FAQPage")
    assert faq_page["mainEntity"][0]["name"] == _s(lang, "web.shop.product.refund_q")
    # ... and the question is on the page too, above its answer (FAQ markup only for what a visitor can read).
    assert flat(soup.select_one(".shop-pd__assure-q").get_text(" ")) == _s(lang, "web.shop.product.refund_q")
    assert _s(lang, "web.shop.product.refund_a") in flat(soup.select_one(".shop-pd__assure-qa").get_text(" "))
    (product_ld,) = jsonld(soup, "Product")
    assert product_ld["offers"]["eligibleQuantity"]["minValue"] == _smallest(offer_skus.p10.id)


def test_the_product_stepper_starts_at_the_smallest_order_and_adding_it_stores_that(
    app, db, offer_skus, shop_copy, sample_user
):
    product = offer_skus.p10
    soup = get_page(app, f"/product/{product.id}", "ru")
    qty = soup.select_one("#qtyInput")
    page_data = json.loads(soup.select_one("script#page-data").string)

    assert int(qty["value"]) == int(qty["data-min-qty"]) == page_data["min_order_quantity"] == _smallest(product.id)
    assert soup.select_one('[data-action="change-qty"][data-delta="-1"]').has_attr("disabled")
    assert flat(soup.select_one("#shop-pd-qty-hint").get_text(" ")) == _s("ru", "web.shop.product.qty_from", n=4)
    assert flat(soup.select_one('label[for="qtyInput"]').get_text(" ")) == _s("ru", "web.shop.product.quantity")

    stored = _add_as_the_page_does(app, sample_user, product.id, int(qty["value"]))  # product-detail.js: qty

    assert stored == {product.id: product.min_order_quantity}


def _inventory(app, product_id):
    """What /cart reads for a product: POST /api/v1/products/bulk {product_ids, language} (cart.js loadCart)."""
    response = app.test_client().post(
        "/api/v1/products/bulk",
        data=json.dumps({"product_ids": [product_id], "language": "ru"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    (product,) = response.get_json()["data"]["products"]
    return product["inventory"]


def test_the_product_page_shows_a_description_only_when_there_is_one_and_a_low_stock_in_words(
    app, db, offer_skus, shop_copy
):
    product = offer_skus.p19
    product.description = "<p>Artesian water.</p>"
    product.track_inventory = True
    product.stock_quantity = 5
    product.min_stock_level = 10  # the restock threshold the products API rates "low" by
    db.session.commit()

    soup = get_page(app, f"/product/{product.id}", "ru")

    assert flat(soup.select_one(".shop-pd__about .ae-prose").get_text(" ")) == "Artesian water."
    assert _inventory(app, product.id)["is_low_stock"] is True
    assert flat(soup.select_one(".shop-pd__tags .ae-status").get_text(" ")) == _s("ru", "web.shop.stock_low", n=5)


@pytest.mark.parametrize(
    "stock, min_stock_level, tag",
    [(5, 0, None), (5, 10, "low"), (11, 10, None), (0, 0, "out")],
)
def test_the_product_pages_stock_tag_is_the_products_apis_answer(
    app, db, offer_skus, shop_copy, stock, min_stock_level, tag
):
    """The page and /cart say the same thing: in stock / low stock are the API's inventory flags, never a number the
    template picks (it used to call anything up to 10 "low" while the API, rating by min_stock_level, did not)."""
    product = offer_skus.p19
    product.track_inventory = True
    product.stock_quantity = stock
    product.min_stock_level = min_stock_level
    db.session.commit()

    inventory = _inventory(app, product.id)
    soup = get_page(app, f"/product/{product.id}", "ru")
    status = soup.select_one(".shop-pd__tags .ae-status")
    add = soup.select_one('#shop-pd-actions [data-action="add-to-cart-qty"]')

    if tag == "out":
        assert inventory["is_in_stock"] is False
        assert flat(status.get_text(" ")) == _s("ru", "web.shop.stock_out")
        assert add.has_attr("disabled")
        assert soup.select_one("#shop-pd-bar") is None
    elif tag == "low":
        assert inventory["is_in_stock"] and inventory["is_low_stock"]
        assert flat(status.get_text(" ")) == _s("ru", "web.shop.stock_low", n=stock)
        assert not add.has_attr("disabled")
    else:
        assert inventory["is_in_stock"] and not inventory["is_low_stock"]
        assert status is None
        assert not add.has_attr("disabled")


def test_the_product_pages_link_preview_and_sticky_bar(app, db, offer_skus, shop_copy):
    """Link previews (og:*) name the product type and show its photo; the phone bar prints the stepper's quantity
    and its sum, which is what its button adds (product-detail.js keeps both in step)."""
    product = offer_skus.p19
    product.images = ["https://cdn.example.test/p19.png"]
    db.session.commit()
    facts = next(p for p in get_public_offer_facts()["products"] if p["id"] == product.id)

    soup = get_page(app, f"/product/{product.id}", "ru")

    og_types = [m["content"] for m in soup.select('meta[property="og:type"]')]
    og_images = [m["content"] for m in soup.select('meta[property="og:image"]')]
    # One Open Graph set (review SITE-22): the shell prints the page's og_type / og_image, no second pair.
    assert og_types == ["product"]
    assert og_images == ["https://cdn.example.test/p19.png"]
    assert [m["content"] for m in soup.select('meta[name="twitter:image"]')] == ["https://cdn.example.test/p19.png"]
    assert len(soup.select('meta[property="og:title"]')) == 1
    assert len(soup.select('meta[property="og:url"]')) == 1
    bar = soup.select_one("#shop-pd-bar")
    assert flat(bar.select_one("#shop-pd-bar-qty").get_text(" ")) == _s(
        "ru", "web.shop.product.units", n=facts["smallest_order_quantity"]
    )
    assert flat(bar.select_one("#shop-pd-bar-sum").get_text(" ")) == flat(facts["smallest_order_total_text"]["ru"])
    page_data = json.loads(soup.select_one("script#page-data").string)
    assert page_data["units_label"] == _s("ru", "web.shop.product.units")
    # The quantity can be typed (a numeric keypad on phones), between the published minimum and the cap.
    qty = soup.select_one("#qtyInput")
    assert not qty.has_attr("readonly")
    assert qty["inputmode"] == "numeric"
    assert int(qty["data-min-qty"]) == facts["smallest_order_quantity"]
    assert int(qty["data-max-qty"]) == page_data["max_qty"]
