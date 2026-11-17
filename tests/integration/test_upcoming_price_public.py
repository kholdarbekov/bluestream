"""The public offer facts state the coming price, and every Offer says until when today's price holds (spec §4.3,
plan Task 35). Pages and the feed are read through the real routes, signed out."""

from datetime import date
from decimal import Decimal

import pytest

from business_app.models.price_change import PriceChangeStatus
from business_app.services.public_offer_service import product_offer_facts
from business_app.utils import display_labels
from tests.integration.price_change_builders import APPLIED_AT, CHANGE_ON, make_admin, make_price_change
from tests.integration.public_page_builders import (  # noqa: F401 -- fixtures
    BLOCK_PAGES,
    LANGS,
    blog_post,
    get_page,
    jsonld,
    offer_skus,
    seed_copy,
    set_price,
    shipped,
)

pytestmark = pytest.mark.integration

UPCOMING_KEY = "landing.offer.upcoming_price"
VALID_UNTIL = "2027-01-31"


@pytest.fixture
def announced(db, offer_skus):
    """p19 at 18,000 with a 20,000 change announced for 2027-02-01; p10 untouched."""
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    make_price_change(db, [(offer_skus.p19, Decimal("20000"), 2, None)], created_by=make_admin(db))
    db.session.commit()
    seed_copy(db, ("web.display_labels", "landing."))
    return offer_skus


def _feed_item(app, product):
    response = app.test_client().get("/api/public/products.json")
    assert response.status_code == 200
    return next(p for p in response.get_json()["products"] if p["id"] == product.id)


def test_products_json_says_until_when_todays_price_holds(app, db, announced):
    offers = _feed_item(app, announced.p19)["offers"]
    assert offers["price"] == 18000
    assert offers["priceValidUntil"] == VALID_UNTIL
    assert "priceValidUntil" not in _feed_item(app, announced.p10)["offers"]


@pytest.mark.parametrize("path", ["/product/{id}", "/", "/shop"])
def test_every_offer_on_the_site_carries_it(app, db, announced, path):
    soup = get_page(app, path.format(id=announced.p19.id), "en")
    offers = [o for kind in ("Product", "ProductGroup") for item in jsonld(soup, kind) for o in _offers_of(item)]
    mine = [o for o in offers if o.get("price") == 18000]
    assert mine, f"{path} carries no Offer at today's price"
    assert all(o["priceValidUntil"] == VALID_UNTIL for o in mine)
    assert all("priceValidUntil" not in o for o in offers if o.get("price") != 18000)


def _offers_of(item):
    found = []
    offers = item.get("offers")
    if isinstance(offers, dict):
        found.append(offers)
    elif isinstance(offers, list):
        found += [o for o in offers if isinstance(o, dict)]
    for variant in item.get("hasVariant") or []:
        found += _offers_of(variant)
    return found


@pytest.mark.parametrize(
    "columns",
    [
        {"status": PriceChangeStatus.SCHEDULED, "announcement_started_at": None},
        {"status": PriceChangeStatus.APPLIED, "applied_at": APPLIED_AT},
    ],
    ids=["scheduled", "applied"],
)
def test_no_valid_until_before_the_announcement_or_after_the_switch(app, db, offer_skus, columns):
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    make_price_change(db, [(offer_skus.p19, Decimal("20000"), 2, None)], created_by=make_admin(db), **columns)
    db.session.commit()
    assert "priceValidUntil" not in _feed_item(app, offer_skus.p19)["offers"]
    facts = product_offer_facts(offer_skus.p19)
    assert facts["price_valid_until"] is None
    assert facts["upcoming_line"] is None


@pytest.mark.parametrize("lang", LANGS)
def test_the_coming_price_line_is_composed_from_the_change_row(app, db, announced, lang):
    facts = product_offer_facts(announced.p19)
    expected = shipped(UPCOMING_KEY, lang).format(
        date=display_labels.day_month(CHANGE_ON, lang), price=display_labels.money(Decimal("20000"), lang)
    )
    assert facts["upcoming_line"][lang] == expected
    assert facts["price_valid_until"] == date(2027, 1, 31)
    assert product_offer_facts(announced.p10)["upcoming_line"] is None
    assert product_offer_facts(announced.p10)["price_valid_until"] is None
    from business_app.services.public_offer_service import upcoming_price_template

    assert upcoming_price_template(lang) == shipped(UPCOMING_KEY, lang)


PATHS = [*BLOCK_PAGES, "/services"]


def _composed_line(lang):
    return shipped(UPCOMING_KEY, lang).format(
        date=display_labels.day_month(CHANGE_ON, lang), price=display_labels.money(Decimal("20000"), lang)
    )


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", PATHS)
def test_every_public_price_surface_states_the_coming_price(app, db, announced, blog_post, path, lang):
    soup = get_page(app, path.format(id=announced.p19.id, slug=blog_post.slug), lang)
    nodes = soup.select(f'[data-upcoming-price="{announced.p19.id}"]')
    assert nodes, f"{path} ({lang}) states no coming price for the 18.9 L bottle"
    assert {node.get_text(strip=True) for node in nodes} == {_composed_line(lang).strip()}
    assert soup.select(f'[data-upcoming-price="{announced.p10.id}"]') == []


def test_services_prints_the_dated_line_beside_the_price_never_inside_it(app, db, announced):
    """Page scripts refresh a `[data-price]` element's text, which would wipe a dated line nested in it."""
    soup = get_page(app, "/services", "en")

    nodes = soup.select(f'[data-upcoming-price="{announced.p19.id}"]')
    assert nodes
    for node in nodes:
        assert node.find_parent(attrs={"data-price": True}) is None
        price = node.find_previous_sibling(attrs={"data-price": True})
        assert price is not None, "the dated line follows the [data-price] span"


def test_the_product_page_states_it_beside_the_price_and_strikes_nothing(app, db, announced):
    soup = get_page(app, f"/product/{announced.p19.id}", "en")
    assert soup.select(".price-box [data-upcoming-price]")
    assert soup.select(".price-old, .old-price, .save-tag, .sale-tag") == []


def test_llms_txt_states_the_coming_price_in_three_languages(app, db, announced):
    app.jinja_env.cache.clear()
    response = app.test_client().get("/llms.txt")
    assert response.status_code == 200
    section = response.get_data(as_text=True).split("## Prices", 1)[1].split("\n## ", 1)[0]
    for lang in LANGS:
        assert section.count(_composed_line(lang).replace("\u00a0", " ")) == 1  # the file prints plain spaces; the 10 L bottle states none


@pytest.mark.parametrize("path", PATHS)
def test_no_page_states_it_before_the_announcement(app, db, offer_skus, blog_post, path):
    set_price(db, offer_skus.p19, base=Decimal("18000"))
    make_price_change(
        db,
        [(offer_skus.p19, Decimal("20000"), 2, None)],
        created_by=make_admin(db),
        status=PriceChangeStatus.SCHEDULED,
        announcement_started_at=None,
    )
    db.session.commit()
    seed_copy(db, ("web.display_labels", "landing."))
    soup = get_page(app, path.format(id=offer_skus.p19.id, slug=blog_post.slug), "en")
    assert soup.select("[data-upcoming-price]") == []
