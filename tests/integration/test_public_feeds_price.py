"""The public feeds, the Store price range and the empty-description fallback print the price an order is charged."""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.services.public_offer_service import price_sentence, product_offer_facts
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.constants import get_delivery_statement
from tests.integration.public_page_builders import (  # noqa: F401 -- fixtures
    flat,
    jsonld,
    offer_skus,
    page,
    seed_copy,
    set_price,
)
from tests.integration.sales_pay_builders import freeze_local_now

pytestmark = pytest.mark.integration

SENTENCE = {
    "uz": "Aqua Element 18.9 L, 18,9 l (19 l) — narxi 17 345 so'm.",
    "ru": "Aqua Element 18.9 L, 18,9 л (19 л) — цена 17 345 сум.",
    "en": "Aqua Element 18.9 L, 18.9 L (19 L) — price 17 345 so'm (UZS).",
}


def _feed(app):
    response = app.test_client().get("/api/public/products.json")
    assert response.status_code == 200
    return response, response.get_json()


def _item(feed, product):
    return next(p for p in feed["products"] if p["id"] == product.id)


def _g(xml, product, tag):
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        if f"<g:id>{product.id}</g:id>" in block:
            found = re.search(rf"<g:{tag}>(.*?)</g:{tag}>", block, re.S)
            return found.group(1) if found else None
    return None


def test_products_json_publishes_what_an_order_is_charged(app, db, offer_skus, monkeypatch):
    monkeypatch.setitem(app.config, "MIN_ORDER_AMOUNT", 20000)
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 0)
    freeze_local_now(monkeypatch, datetime(2026, 11, 12, 20, 30, tzinfo=UTC))
    seed_copy(db, ("web.display_labels",))
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    set_price(db, offer_skus.p10, base=Decimal("31230"))  # dearest now, and still second in the feed
    response, feed = _feed(app)
    assert response.headers["Cache-Control"] == "public, no-cache"
    item, other = _item(feed, offer_skus.p19), _item(feed, offer_skus.p10)
    assert (item["offers"]["price"], item["offers"]["listPrice"]) == (15000, 17345) and "salePrice" not in item["offers"]
    assert type(item["offers"]["price"]) is int and "listPrice" not in other["offers"]
    assert item["priceText"] == {"uz": "15\u00a0000\u00a0so'm", "ru": "15\u00a0000\u00a0сум", "en": "15\u00a0000\u00a0so'm"}
    assert (item["minOrderQuantity"], item["minOrderTotal"], item["noDeposit"]) == (2, 30000, True)
    assert item["minOrderTotalText"]["ru"] == "30\u00a0000\u00a0сум" and "noDeposit" not in other
    assert item["size"] == {"label": "18.9L", "litres": 18.9} and other["size"] == {"label": "10L", "litres": 10}
    assert item["url"] == item["offers"]["url"] and item["url"].endswith(f"/product/{offer_skus.p19.id}")
    assert [p["id"] for p in feed["products"]] == [offer_skus.p19.id, offer_skus.p10.id]
    assert (feed["pricesAsOf"], feed["minOrderAmount"]) == ("2026-11-13", 20000)
    assert {k: feed["delivery"][k] for k in ("fee", "currency", "free")} == {"fee": 0, "currency": "UZS", "free": True}


def test_products_json_states_a_fee_above_zero(app, db, offer_skus, monkeypatch):
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    _, feed = _feed(app)
    assert (feed["delivery"]["fee"], feed["delivery"]["free"]) == (5000, False)


def test_products_json_reaches_the_reader_as_the_route_writes_it(app, db, offer_skus):
    # An assistant's fetch tool hands the model the raw body: the Russian and Uzbek text and the no-break spaces must
    # arrive as characters, not \u escapes, and the route's indented layout must survive the after-request hooks.
    seed_copy(db, ("web.display_labels",))
    response, _ = _feed(app)
    raw = response.get_data(as_text=True)
    assert "\\u" not in raw and "\n" in raw
    assert "\u00a0сум" in raw and get_delivery_statement("ru")[0] in raw and get_delivery_statement("uz")[0] in raw


def test_loyalty_json_reaches_the_reader_as_the_route_writes_it(app, db):
    response = app.test_client().get("/api/public/loyalty.json")
    assert response.status_code == 200
    raw = response.get_data(as_text=True)
    assert "\\u" not in raw and "\n" in raw and "— Aqua Club" in raw  # the programme name's em dash


@pytest.mark.parametrize("path", ["/api/public/products.json", "/api/public/loyalty.json"])
def test_neither_feed_names_the_phone_as_an_order_channel(app, db, offer_skus, monkeypatch, path):
    # Orders are taken in the Telegram bot and on the website; the phone is contact data, not an order channel.
    monkeypatch.setitem(app.config, "COMPANY_PHONE", "+998900000000")
    response = app.test_client().get(path)
    assert response.status_code == 200
    channels = response.get_json()["orderChannels"]
    assert "phone" not in channels and set(channels) == {"web", "telegramBot"}


def test_google_feed_keeps_googles_pair_from_the_same_source(app, db, offer_skus):
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=Decimal("15000"))
    xml = app.test_client().get("/feeds/google-products.xml").get_data(as_text=True)
    assert (_g(xml, offer_skus.p19, "price"), _g(xml, offer_skus.p19, "sale_price")) == ("17345.00 UZS", "15000.00 UZS")
    assert (_g(xml, offer_skus.p10, "price"), _g(xml, offer_skus.p10, "sale_price")) == ("12340.00 UZS", None)
    assert f"/product/{offer_skus.p19.id}</link>" in xml


@pytest.mark.parametrize("discount, charged", [(Decimal("0"), "17345.00"), (Decimal("20000"), "20000.00")])
def test_google_feed_never_lists_a_price_that_is_not_charged(app, db, offer_skus, discount, charged):
    set_price(db, offer_skus.p19, base=Decimal("17345"), discount=discount)
    xml = app.test_client().get("/feeds/google-products.xml").get_data(as_text=True)
    assert (_g(xml, offer_skus.p19, "price"), _g(xml, offer_skus.p19, "sale_price")) == (f"{charged} UZS", None)


def test_the_store_price_range_comes_from_the_products(app, db, offer_skus, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_STREET_ADDRESS", "1 Test St")  # no address, no Store block (base.html:72)
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    for path in ("/", "/contact"):  # /contact has no offer context
        assert jsonld(page(app, db, path, "uz"), "Store")[0]["priceRange"] == "UZS 12340-17345", path
    assert "COMPANY_PRICE_RANGE" not in app.config


def test_no_active_product_prints_no_price_range(app, db, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_STREET_ADDRESS", "1 Test St")
    assert "priceRange" not in jsonld(page(app, db, "/contact", "uz"), "Store")[0]


@pytest.mark.parametrize("lang", ["uz", "ru", "en"])
def test_an_empty_description_falls_back_to_the_price_sentence(app, db, offer_skus, lang):
    set_price(db, offer_skus.p19, base=Decimal("17345"))
    soup = page(app, db, f"/product/{offer_skus.p19.id}", lang)
    assert flat(jsonld(soup, "Product")[0]["description"]) == SENTENCE[lang]
    assert {flat(tag["content"]) for tag in soup.find_all("meta", attrs={"property": "og:description"})} == {SENTENCE[lang]}
    assert flat(soup.find("meta", attrs={"name": "description"})["content"]) == SENTENCE[lang]


def test_a_written_description_wins(app, db, offer_skus):
    offer_skus.p19.short_description = "Kuler uchun suv"
    db.session.commit()
    soup = page(app, db, f"/product/{offer_skus.p19.id}", "uz")
    assert jsonld(soup, "Product")[0]["description"] == "Kuler uchun suv"


def test_the_price_sentence_without_its_row_still_names_the_price(app, db, offer_skus):  # no row seeded
    assert flat(price_sentence(product_offer_facts(offer_skus.p19), "uz")) == "Aqua Element 18.9 L — 23 450 UZS"


def test_the_price_line_is_seeded_with_one_set_of_placeholders_and_no_figure():
    row = BACKEND_TRANSLATIONS["landing.offer.price_line"]
    placeholders = {lang: sorted(re.findall(r"{(\w+)}", row[lang])) for lang in ("uz", "ru", "en")}
    assert placeholders == dict.fromkeys(("uz", "ru", "en"), ["name", "price", "size"])
    assert not re.search(r"\d", "".join(row.values()))  # R1: no figure in a translation value
