"""The pricing policy page (plan 2026-11-12, "live price and same-day delivery visibility"): a translated title, the
live prices first, and the real delivery fee rule instead of a vague zone sentence."""

from decimal import Decimal

import pytest

from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    LANGS,
    flat,
    offer_skus,
    page,
    set_price,
    shipped,
)

pytestmark = pytest.mark.integration

PRICE = Decimal("17345")


@pytest.mark.parametrize("lang", LANGS)
def test_title_h1_and_breadcrumb_are_translated(app, db, offer_skus, lang):
    soup = page(app, db, "/pricing-policy", lang)
    name = shipped("landing.pricing_policy.title", lang)
    assert flat(soup.title.get_text()).startswith(f"{name} - ")
    assert soup.select_one(".page-title h1").get_text(strip=True) == name
    assert soup.select(".page-title .bread-crumb li")[-1].get_text(strip=True) == name


@pytest.mark.parametrize("lang", LANGS)
def test_the_page_shows_the_live_prices_first(app, db, offer_skus, lang):
    set_price(db, offer_skus.p19, PRICE)
    first = page(app, db, "/pricing-policy", lang).select_one("section.service-details .content-box > *")
    assert first.has_attr("data-offer-facts")
    text = flat(first.get_text(" "))
    assert "17 345" in text and "12 340" in text and "23 450" not in text


def test_the_zone_sentence_is_gone(app, db, offer_skus):
    text = flat(page(app, db, "/pricing-policy", "en").select_one("main").get_text(" "))
    assert "address zone" not in text and "Delivery and Additional Charges" not in text
    assert "Displayed Prices" in text and "Price Updates" in text  # the other sections stay


@pytest.mark.parametrize("lang", LANGS)
def test_a_fee_above_zero_is_printed_as_the_rule(app, db, offer_skus, monkeypatch, lang):
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    main = page(app, db, "/pricing-policy", lang).select_one("main")
    assert "5 000" in flat(main.select_one('[data-offer-line="delivery"]').get_text(" "))
    assert flat(shipped("landing.offer.free_delivery", lang)) not in flat(main.get_text(" "))


def test_the_page_loads_with_no_active_product(app, db):
    soup = page(app, db, "/pricing-policy", "en")  # page() asserts 200
    assert soup.select_one("[data-offer-facts]") is None
