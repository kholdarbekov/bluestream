"""/cart after the redesign (website redesign step 3, group "shop", unit "shop-cart", 2026-11-14).

The page is fetched the way a visitor fetches it — signed out and signed in, in uz, ru and en — with the rows the
seeds ship (scripts/seed_web_ui_shop_translations.py, the backend seed's landing.* and money rows) and the two
bottle SKUs of `offer_skus` (minimums 2 and 4, prices no production row has).

cart.js renders the lines in the browser (admin_ui/src/__tests__/web/shopCart.test.js runs the shipped script on
these hooks). Here: the page carries every hook and word it needs, the delivery row is the offer facts' answer, the
empty cart's picks add the smallest order the backend publishes, and the requests cart.js sends — the stepper's
`PUT /api/v1/cart/items/<product_id> {quantity}`, remove, undo and clear, with the access cookie and `X-CSRF-TOKEN`
— reach the database cart. The stepper's floor is the minimum `POST /api/v1/products/bulk` publishes, and the
checkout's estimate refuses a line below it, so a cart held below its minimum elsewhere cannot be ordered.
"""

import json

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token

from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts
from business_app.utils import display_labels
from scripts.seed_web_ui_shop_translations import CATEGORY, TRANSLATIONS as SHOP
from tests.integration.public_page_builders import (  # noqa: F401 -- offer_skus is a fixture
    flat,
    get_page,
    offer_skus,
    seed_copy,
)
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_cart_page_test_bot"

# Every hook cart.js reads or writes (static/js/pages/cart.js); a renamed id breaks the page silently.
HOOKS = (
    "#cart-loading",
    "#cart-content",
    "#empty-cart",
    "#cart-error",
    "#cart-undo",
    "#cart-undo-text",
    "#cart-items-container",
    "#items-count",
    "#summary-units",
    "#summary-subtotal",
    "#summary-total",
    "#min-order-warning",
    "#min-order-text",
    "#min-order-progress",
    "#min-qty-warnings",
    "#min-qty-warning-list",
    "#checkout-btn[data-checkout]",
    "#cart-bar",
    "#cart-bar-total",
    "#cart-clear-btn[data-action='clear-cart']",
    "#cart-clear-confirm [data-action='clear-cart-yes']",
    "#cart-clear-confirm [data-action='clear-cart-no']",
    "[data-action='undo-remove']",
    ".shop-cart__actions",
    "#cart-dropped",
    "#cart-error [data-action='retry-load']",
)
# The English the old page printed in every language (audit "shop" findings 0 and 3); the page's visible text.
OLD_ENGLISH = ("Shopping Cart", "Proceed to Checkout", "Loading cart...", "Your Items", "Cart Total", "UZS")


@pytest.fixture
def cart_copy(app, db, monkeypatch):
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


def _page_data(soup):
    return json.loads(soup.select_one("script#page-data").string)


def _summary_rows(soup):
    """The summary's rows as the visitor reads them: label -> value."""
    return {
        flat(row.select_one("dt").get_text(" ")): flat(row.select_one("dd").get_text(" "))
        for row in soup.select(".shop-cart__dl .ae-dl__row")
    }


def _smallest(product_id):
    """The smallest order the backend publishes for a product (the public offer facts)."""
    return next(p for p in get_public_offer_facts()["products"] if p["id"] == product_id)["smallest_order_quantity"]


def _signed_in(app, user):
    """A browser after the storefront login, and the X-CSRF-TOKEN value base.js apiRequest sends."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return _logged_in_browser(app, token)


def _db_cart(browser):
    response = browser.get("/api/v1/cart/", headers=WEB_HEADERS)
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    return {item["product_id"]: item["quantity"] for item in response.get_json()["data"]["cart"]["cart_items"]}


# --- the page -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_cart_reads_in_the_visitors_language_and_carries_every_hook(app, db, offer_skus, cart_copy, lang):
    soup = get_page(app, "/cart", lang)
    html = str(soup)

    assert "web.shop." not in html, "a web.shop key reached the page untranslated"
    visible = " ".join(t for t in soup.body.find_all(string=True) if t.parent.name not in ("script", "style"))
    for english in OLD_ENGLISH:
        assert english not in visible
    assert [flat(h.get_text(" ")) for h in soup.select("h1")] == [_s(lang, "web.shop.cart.title")]
    assert flat(soup.title.get_text()).startswith(_s(lang, "web.shop.cart.title"))
    crumb = soup.select_one(".page-title .bread-crumb li:last-child")
    assert flat(crumb.get_text()) == _s(lang, "web.shop.cart.title")
    for hook in HOOKS:
        assert soup.select_one(hook) is not None, f"cart.js hook {hook} is missing"
    assert soup.select(".wow") == []

    # Signed out: checkout asks to sign in, and the bot is offered beside it and in the empty state.
    assert flat(soup.select_one("#checkout-btn").get_text()) == _s(lang, "web.shop.cart.login_checkout")
    assert soup.select_one(f'.shop-cart__actions a[href="{BOT_URL}"]') is not None
    assert soup.select_one(f'#empty-cart a[href="{BOT_URL}"]') is not None
    assert flat(soup.select_one("#empty-cart .ae-empty__title").get_text()) == _s(lang, "web.shop.cart.empty_title")

    # Every word cart.js prints arrives translated in the page data, with the placeholders it fills.
    data = _page_data(soup)
    assert data["i18n"] == {
        name: _s(lang, key)
        for name, key in {
            "units_one": "web.shop.cart.units_one",
            "units_other": "web.shop.cart.units_other",
            "unit_price": "web.shop.cart.unit_price",
            "qty_label": "web.shop.cart.qty_label",
            "qty_less": "web.shop.qty_less",
            "qty_more": "web.shop.qty_more",
            "remove": "web.shop.cart.remove",
            "removed": "web.shop.cart.removed",
            "stock_low": "web.shop.stock_low",
            "stock_out": "web.shop.stock_out",
            "min_amount_text": "web.shop.cart.min_amount_text",
            "min_qty_line": "web.shop.cart.min_qty_line",
            "min_qty_hint": "web.shop.cart.min_qty_hint",
            "update_error": "web.shop.cart.update_error",
        }.items()
    }
    assert data["min_order_amount"] == app.config["MIN_ORDER_AMOUNT"]
    assert data["max_qty"] == app.config["MAX_QUANTITY_PER_ITEM"]
    assert data["current_language"] == lang

    # Its own description in the visitor's language (not the site-wide English default), and a load error with a way
    # forward: try again, or the bot.
    meta = _s(lang, "web.shop.cart.meta", company=app.config["COMPANY_NAME"])
    assert soup.select_one('meta[name="description"]')["content"] == meta
    error = soup.select_one("#cart-error")
    assert flat(error.select_one("[data-action='retry-load']").get_text(" ")) == _s(lang, "web.shop.cart.retry")
    assert error.select_one(f'a[href="{BOT_URL}"]') is not None
    assert flat(soup.select_one("#cart-dropped").get_text(" ")) == _s(lang, "web.shop.cart.dropped")


@pytest.mark.parametrize("lang", LANGS)
def test_the_delivery_row_is_the_offer_facts_answer(app, db, offer_skus, cart_copy, lang):
    facts = get_public_offer_facts()["delivery"]
    assert facts["free"], "the test config ships free delivery"
    soup = get_page(app, "/cart", lang)
    rows = _summary_rows(soup)

    assert rows[_s(lang, "web.shop.cart.delivery")] == _s(lang, "web.shop.cart.delivery_free")
    assert flat(soup.select_one(".shop-cart__note").get_text(" ")) == flat(facts["every_day"][lang])
    assert _page_data(soup)["delivery_fee"] == 0
    # The old notice contradicted free delivery (audit "shop" finding 2).
    assert "calculated at checkout" not in str(soup)


def test_a_delivery_fee_is_printed_and_added_when_delivery_is_not_free(app, db, offer_skus, cart_copy, monkeypatch):
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    soup = get_page(app, "/cart", "ru")
    rows = _summary_rows(soup)

    with app.test_request_context():
        assert rows[_s("ru", "web.shop.cart.delivery")] == flat(display_labels.money(5000, "ru"))
    assert _page_data(soup)["delivery_fee"] == 5000


def test_the_empty_carts_picks_add_the_smallest_order_the_backend_publishes(app, db, offer_skus, cart_copy):
    soup = get_page(app, "/cart", "ru")
    picks = soup.select('#empty-cart .shop-cart__picks [data-action="add-to-cart"]')

    # Dearest first, as on /shop; each adds its smallest order, never one bottle.
    assert [int(b["data-product-id"]) for b in picks] == [offer_skus.p19.id, offer_skus.p10.id]
    for button in picks:
        assert int(button["data-qty"]) == _smallest(int(button["data-product-id"]))


@pytest.mark.parametrize("lang", LANGS)
def test_the_signed_in_cart_checks_out_without_signing_in_again(app, db, offer_skus, cart_copy, sample_user, lang):
    browser, _csrf = _signed_in(app, sample_user)
    app.jinja_env.cache.clear()
    response = browser.get(f"/cart?lang={lang}", follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    assert "web.shop." not in html
    assert flat(soup.select_one("#checkout-btn").get_text()) == _s(lang, "web.shop.cart.checkout")
    assert flat(soup.select_one("#cart-bar [data-checkout]").get_text()) == _s(lang, "web.shop.cart.checkout")
    assert soup.select(".shop-cart__actions a[href*='t.me']") == []


# --- the requests cart.js sends --------------------------------------------------------------------------------------


def test_the_stepper_floor_is_the_minimum_products_bulk_publishes(app, db, offer_skus):
    """cart.js loadCart: `POST /products/bulk {product_ids, language}`; the stepper stops at
    inventory.min_order_quantity."""
    response = app.test_client().post(
        "/api/v1/products/bulk",
        headers=WEB_HEADERS,
        data=json.dumps({"product_ids": [offer_skus.p19.id, offer_skus.p10.id], "language": "ru"}),
    )
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    published = {p["id"]: p["inventory"]["min_order_quantity"] for p in response.get_json()["data"]["products"]}

    assert published == {offer_skus.p19.id: 2, offer_skus.p10.id: 4}


def test_the_cart_pages_writes_reach_the_database_cart(app, db, offer_skus, cart_copy, sample_user):
    """Signed in, every change on /cart is the request cart.js sends, with the cookie and X-CSRF-TOKEN."""
    browser, csrf = _signed_in(app, sample_user)
    headers = {**WEB_HEADERS, "X-CSRF-TOKEN": csrf}
    p10 = offer_skus.p10.id
    smallest = _smallest(p10)

    # A pick from the empty cart (base.js addToCart): the smallest order.
    line = json.dumps({"product_id": p10, "quantity": smallest})
    assert browser.post("/api/v1/cart/items", headers=headers, data=line).status_code == 200
    assert _db_cart(browser) == {p10: smallest}

    # "+" then "−" (cart.js updateQuantity): PUT /cart/items/<product_id> {quantity}.
    up = browser.put(f"/api/v1/cart/items/{p10}", headers=headers, data=json.dumps({"quantity": smallest + 1}))
    assert up.status_code == 200, up.get_data(as_text=True)[:400]
    assert _db_cart(browser) == {p10: smallest + 1}
    down = browser.put(f"/api/v1/cart/items/{p10}", headers=headers, data=json.dumps({"quantity": smallest}))
    assert down.status_code == 200
    assert _db_cart(browser) == {p10: smallest}

    # Remove, then Undo (POST the same line back), then Clear.
    assert browser.delete(f"/api/v1/cart/items/{p10}", headers=headers).status_code == 200
    assert _db_cart(browser) == {}
    assert browser.post("/api/v1/cart/items", headers=headers, data=line).status_code == 200
    assert _db_cart(browser) == {p10: smallest}
    assert browser.post("/api/v1/cart/clear", headers=headers).status_code == 200
    assert _db_cart(browser) == {}


def test_a_cart_write_without_the_csrf_header_is_refused(app, db, offer_skus, sample_user):
    """The header cart.js sends is what lets the write in: without it the database cart stays as it was."""
    browser, csrf = _signed_in(app, sample_user)
    p19 = offer_skus.p19.id
    line = json.dumps({"product_id": p19, "quantity": 2})
    browser.post("/api/v1/cart/items", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf}, data=line)

    refused = browser.put(f"/api/v1/cart/items/{p19}", headers=WEB_HEADERS, data=json.dumps({"quantity": 3}))

    assert refused.status_code in (400, 401, 403)
    assert _db_cart(browser) == {p19: 2}


def test_checkout_refuses_a_line_below_the_minimum_the_cart_page_holds_to(app, db, offer_skus, cart_copy, sample_user):
    """A cart held below a minimum elsewhere (an older cart, the bot) shows the notice on /cart; the checkout's estimate
    (`POST /api/v1/orders/cart/estimate`, checkout.js) refuses it, and accepts the minimum the stepper steps up to."""
    browser, csrf = _signed_in(app, sample_user)
    headers = {**WEB_HEADERS, "X-CSRF-TOKEN": csrf}
    p19 = offer_skus.p19.id

    def estimate(quantity):
        body = json.dumps({"items": [{"product_id": p19, "quantity": quantity}]})
        return browser.post("/api/v1/orders/cart/estimate", headers=headers, data=body)

    below, at_minimum = estimate(1), estimate(2)

    assert below.status_code == 400, below.get_data(as_text=True)[:400]
    assert "minimum order quantity" in below.get_data(as_text=True)
    assert at_minimum.status_code == 200, at_minimum.get_data(as_text=True)[:400]


def test_a_line_whose_product_is_retired_is_dropped_with_the_requests_cart_js_sends(app, db, offer_skus, sample_user):
    """A product taken off sale stays in a cart until the cart page drops it: POST /products/bulk (cart.js loadCart)
    no longer returns it, and cart.js then sends DELETE /cart/items/<product_id>, which takes it out of the database
    cart even though the product is inactive (the header count follows the stored cart)."""
    browser, csrf = _signed_in(app, sample_user)
    headers = {**WEB_HEADERS, "X-CSRF-TOKEN": csrf}
    p19, p10 = offer_skus.p19.id, offer_skus.p10.id
    for product_id in (p19, p10):
        line = json.dumps({"product_id": product_id, "quantity": _smallest(product_id)})
        assert browser.post("/api/v1/cart/items", headers=headers, data=line).status_code == 200
    offer_skus.p10.is_active = False
    db.session.commit()

    bulk = browser.post(
        "/api/v1/products/bulk", headers=WEB_HEADERS, data=json.dumps({"product_ids": [p19, p10], "language": "ru"})
    )
    assert bulk.status_code == 200, bulk.get_data(as_text=True)[:400]
    assert [p["id"] for p in bulk.get_json()["data"]["products"]] == [p19]
    assert _db_cart(browser) == {p19: 2, p10: 4}  # still there until the page drops it

    dropped = browser.delete(f"/api/v1/cart/items/{p10}", headers=headers)

    assert dropped.status_code == 200, dropped.get_data(as_text=True)[:400]
    assert _db_cart(browser) == {p19: 2}


def test_the_stock_tags_cart_js_prints_are_the_apis_inventory_flags(app, db, offer_skus):
    """cart.js stockTag reads inventory.is_in_stock / is_low_stock from POST /products/bulk (no number of its own)."""
    offer_skus.p19.track_inventory = True
    offer_skus.p19.stock_quantity = 5
    offer_skus.p19.min_stock_level = 10
    offer_skus.p10.track_inventory = True
    offer_skus.p10.stock_quantity = 0
    db.session.commit()

    response = app.test_client().post(
        "/api/v1/products/bulk",
        headers=WEB_HEADERS,
        data=json.dumps({"product_ids": [offer_skus.p19.id, offer_skus.p10.id], "language": "ru"}),
    )

    inventory = {p["id"]: p["inventory"] for p in response.get_json()["data"]["products"]}
    assert inventory[offer_skus.p19.id]["is_in_stock"] is True
    assert inventory[offer_skus.p19.id]["is_low_stock"] is True
    assert inventory[offer_skus.p19.id]["stock_quantity"] == 5
    assert inventory[offer_skus.p10.id]["is_in_stock"] is False
