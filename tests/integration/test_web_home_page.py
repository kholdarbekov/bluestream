"""The home page after the redesign (website redesign step 3, group "home", unit "home-page": everything on / except the
subscription builder's internals, which tests/integration/test_web_home_builder.py covers).

The page is fetched as a browser fetches it, in uz, ru and en, with the rows the seeds ship (the backend seed's
landing.* / web.display_labels.* rows and this group's web.home.* rows). What it checks:
- the hero is built around the 18.9 L bottle's own photo (the placeholder without one), prints today's prices and
  smallest orders from the public offer facts, and offers the Telegram bot first and the website second;
- the retired content is gone: the carousel, the celebrity cut-outs, invented testimonials and news, dead links;
- "Add to cart" on a home product card puts the smallest order the backend publishes in the cart (owner decision 4):
  the card's number, sent the way base.js sends it, is what the cart stores;
- each delivery fact is said where it belongs (the hero's facts and the SSOT offer block), not re-printed in the
  "How to order" steps;
- the Aqua Club teaser promises AquaCoins for delivered, paid orders, not "every order";
- a signed-in customer's Aqua Club tier is named in the page language.
"""

import json
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyTierConfig
from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts
from scripts.seed_web_ui_home_translations import CATEGORY, TRANSLATIONS as HOME
from tests.integration.public_page_builders import (  # noqa: F401 -- offer_skus is a fixture
    flat,
    get_page,
    offer_skus,
    seed_copy,
)
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_home_page_test_bot"
PHOTO_19 = "/uploads/images/products/1/2026/02/hero-test-19.png"
PLACEHOLDER = "/static/images/product-placeholder.svg"


@pytest.fixture
def home_copy(app, db, monkeypatch):
    """The storefront rows the backend seed ships plus this group's web.home.* rows, and a known bot URL."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    seed_copy(db, ("landing.", "web.display_labels"))
    for language, rows in HOME.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _h(lang, key, **values):
    return HOME[lang][key].format(**values) if values else HOME[lang][key]


def _facts():
    return get_public_offer_facts()["products"]


@pytest.mark.parametrize("lang", LANGS)
def test_the_hero_is_the_real_bottle_with_todays_prices_and_both_ways_to_order(app, db, offer_skus, home_copy, lang):
    offer_skus.p19.images = [PHOTO_19]
    db.session.commit()
    soup = get_page(app, "/", lang)
    hero = soup.select_one(".ae-home-hero")

    assert "web.home." not in str(soup), "a web.home key reached the page untranslated"
    assert flat(hero.select_one("h1#home-title").get_text()) == flat(_h(lang, "web.home.hero.title"))
    assert len(soup.select("h1")) == 1

    # The bottle: the 18.9 L product's own first image, named in the page language, falling back to the placeholder.
    img = hero.select_one(".ae-home-hero__img")
    size19 = next(p for p in _facts() if p["id"] == offer_skus.p19.id)["size_text"][lang]
    assert img["src"] == PHOTO_19 and img["data-fallback"] == PLACEHOLDER
    assert img["alt"] == _h(lang, "web.home.hero.image_alt", size=size19)

    # Today's prices: one row per product, in the offer facts' order, with the facts' own words.
    rows = hero.select(".ae-home-prices__row")
    assert [int(r["data-hero-product"]) for r in rows] == [p["id"] for p in _facts()]
    for row, facts in zip(rows, _facts()):
        assert flat(row.select_one(".ae-home-prices__size").get_text()) == flat(facts["size_text"][lang])
        assert flat(row.select_one(".ae-home-prices__price").get_text()) == flat(facts["price_text"][lang])
        assert str(facts["smallest_order_quantity"]) in row.select_one(".ae-home-prices__note").get_text()

    # The bot first, the website second.
    primary, secondary = hero.select(".ae-home-hero__actions a")
    assert primary["href"] == BOT_URL and "ae-btn--primary" in primary["class"]
    assert flat(primary.get_text()) == _h(lang, "web.home.hero.order_bot")
    assert urlsplit(secondary["href"]).path == "/shop"
    assert flat(secondary.get_text()) == _h(lang, "web.home.hero.order_site")


def test_without_a_product_photo_the_hero_shows_the_placeholder(app, db, offer_skus, home_copy):
    soup = get_page(app, "/", "ru")
    img = soup.select_one(".ae-home-hero__img")
    assert img["src"] == PLACEHOLDER and img["data-fallback"] == PLACEHOLDER


@pytest.mark.parametrize("lang", LANGS)
def test_the_retired_home_content_is_gone(app, db, offer_skus, home_copy, lang):
    soup = get_page(app, "/", lang)
    html = str(soup).lower()
    for gone in ("elon-musk", "banner-carousel", "owl-carousel", "three-item-carousel", "testimonial",
                 "news-block", "custom-index.js", "wow fadeinup", "pricing-section"):
        assert gone not in html, gone
    assert not soup.select('main a[href="#"], .ae-home a[href="#"]'), "a dead link is back"
    # The sections, in order, each with its heading.
    order = [s.get("aria-labelledby") for s in soup.select(".ae-home > section")]
    assert order == ["home-title", "home-how-title", "home-products-title", "home-offer-title",
                     "home-builder-title", "home-quality-title", "home-club-title"]
    assert soup.select_one('#home-quality-title ~ .ae-home-quality__links a[href*="/process/11-step-filtration"]')
    assert soup.select_one('#home-quality-title ~ .ae-home-quality__links a[href*="/about/sources"]')
    assert soup.select_one('#aqua-club-public a[href*="/loyalty-guide"]')


@pytest.mark.parametrize("which", ["p19", "p10"])
def test_add_to_cart_on_a_home_card_stores_the_smallest_order(app, db, offer_skus, home_copy, sample_user, which):
    product = getattr(offer_skus, which)
    soup = get_page(app, "/", "uz")
    # The cards lead with the dearer bottle, in the order the offer facts publish (the shop and the hero print it).
    buttons = soup.select('.ae-home-products [data-action="add-to-cart-shop"]')
    assert [int(b["data-product-id"]) for b in buttons] == [p["id"] for p in _facts()]
    # The card's default button, which base.js handles (addToCart(id, data-qty)) on every page: no second handler.
    scripts = [s.get("src", "").split("?")[0] for s in soup.select("script[src]")]
    assert "/static/js/pages/base.js" in scripts
    assert "/static/js/pages/shop.js" not in scripts
    assert not soup.select('.ae-home [data-action="add-to-cart"]')

    button = soup.select_one(f'.ae-home-products [data-action="add-to-cart-shop"][data-product-id="{product.id}"]')
    quantity = int(button["data-qty"])
    smallest = next(p for p in _facts() if p["id"] == product.id)["smallest_order_quantity"]
    assert quantity == smallest == product.min_order_quantity > 1

    # What base.js addToCart sends for a signed-in visitor, with that number.
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    browser, csrf = _logged_in_browser(app, token)
    added = browser.post("/api/v1/cart/items", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf},
                         data=json.dumps({"product_id": product.id, "quantity": quantity}))
    assert added.status_code == 200, added.get_data(as_text=True)[:400]
    cart = browser.get("/api/v1/cart/", headers=WEB_HEADERS).get_json()["data"]["cart"]["cart_items"]
    assert {item["product_id"]: item["quantity"] for item in cart} == {product.id: smallest}


@pytest.mark.parametrize("lang", LANGS)
def test_how_to_order_step_two_does_not_reprint_the_delivery_terms(app, db, offer_skus, home_copy, lang):
    soup = get_page(app, "/", lang)
    facts = get_public_offer_facts()["delivery"]
    steps = soup.select(".ae-home-how .ae-steps > li")
    assert flat(steps[1].select_one(".ae-steps__text").get_text()) == _h(lang, "web.home.how.deliver_text")
    how = flat(soup.select_one(".ae-home-how").get_text(" "))
    assert flat(facts["line"][lang]) not in how
    assert flat(facts["every_day"][lang]) not in how
    # The lead is the home seed's own sentence; the old call to action (fleet, region) is not printed.
    assert flat(soup.select_one(".ae-home-how .ae-section-head__lead").get_text()) == _h(lang, "web.home.how.lead")


@pytest.mark.parametrize("lang", LANGS)
def test_the_aqua_club_teaser_promises_coins_for_delivered_paid_orders(app, db, home_copy, lang):
    club = get_page(app, "/", lang).select_one("#aqua-club-public")
    assert flat(club.select_one("#home-club-title").get_text()) == _h(lang, "web.home.club.title")
    assert flat(club.select_one(".ae-section-head__lead").get_text()) == _h(lang, "web.home.club.lead")
    text = flat(club.get_text(" ")).lower()
    for every_order in ("every order", "every delivered order", "с каждого заказа", "каждый доставленный заказ",
                        "har bir buyurtma", "har bir yetkazilgan buyurtma"):
        assert every_order not in text, every_order


def test_a_signed_in_customers_tier_is_named_in_the_page_language(app, db, home_copy, sample_user):
    program = LoyaltyProgram(name="Aqua Club", is_active=True, is_default=True, uzs_per_point=250, signup_bonus=200,
                             referral_bonus=500, birthday_bonus=200, points_expiry_days=365, min_redemption_points=200)
    db.session.add(program)
    db.session.flush()
    silver = LoyaltyTierConfig(program_id=program.id, name="Silver", display_order=1, min_points=0, max_points=None,
                               points_multiplier=1.5, discount_percentage=0, is_active=True)
    db.session.add(silver)
    db.session.add(LoyaltyPoints(user_id=sample_user.id, program_id=program.id, current_tier="Silver"))
    db.session.flush()
    silver.set_translated("name", "Серебро", "ru")
    db.session.commit()

    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    browser = app.test_client()
    browser.set_cookie("access_token_cookie", token)
    response = browser.get("/?lang=ru", follow_redirects=True)
    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    status = soup.select_one("#aqua-club-public .ae-home-club__status")
    assert flat(status.select_one(".ae-badge").get_text()) == "Серебро"
    assert status.select_one('a[href="/my-loyalty"]') is not None
