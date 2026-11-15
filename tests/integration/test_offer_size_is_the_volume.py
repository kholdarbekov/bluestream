"""The size a public page prints is the product's volume and nothing else (owner, 2026-11-15).

Production printed "18,9 л (19 л)" for the bottle whose admin record says Volume 18.9 L: the bracket was the trade size,
`Product.size` ("19L"), a field the admin cannot even edit. The owner's rule: "Nowhere that size field should be
displayed." So the 18.9 l bottle prints "18,9 л" on every surface, and a product that records no volume prints no size
at all while every sentence around it still reads naturally (the price line drops the size, the smallest order and the
photo and link labels name the product, and the FAQ sizes answer is left out rather than list only the other bottles).
Searching is not displaying: "19" still finds the 18.9 l bottle in the shop.

Every page is fetched as a signed-out visitor fetches it, in uz, ru and en, with the rows the seeds ship; the copy is
read from the seeds, never typed here.
"""

import pytest

from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts
from scripts.seed_web_ui_content_translations import TRANSLATIONS as CONTENT
from scripts.seed_web_ui_home_translations import TRANSLATIONS as HOME
from scripts.seed_web_ui_info_translations import TRANSLATIONS as INFO
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from scripts.seed_web_ui_shop_translations import TRANSLATIONS as SHOP
from scripts.seed_web_ui_subs_translations import TRANSLATIONS as SUBS
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    faqpage_jsonld_answer,
    flat,
    get_page,
    jsonld,
    offer_skus,
    seed_copy,
    shipped,
)

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_offer_size_test_bot"
PHOTO = "https://cdn.example.test/ae-office.png"
# The 18.9 l bottle's size as every surface prints it: the volume, never "(19 l)".
VOLUME_18_9 = {"uz": "18,9 l", "ru": "18,9 л", "en": "18.9 L"}
# The trade size of that bottle in each language's litres word: searched, never printed.
TRADE_SIZE_19 = ("19 l", "19 л")  # casefolded: the English "19 L" folds to "19 l"


@pytest.fixture
def site_copy(app, db, monkeypatch):
    """The rows the seeds ship for the storefront and the info pages, and a known bot URL."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    seed_copy(db, ("landing.", "web.display_labels"))
    present = set(db.session.query(Translation.key, Translation.language).all())
    for rows_by_lang in (SHELL, HOME, SHOP, INFO, CONTENT, SUBS):
        for language, rows in rows_by_lang.items():
            for key, value in rows.items():
                if (key, language) not in present:
                    db.session.add(Translation(key=key, language=language, value=value, category="web", is_active=True))
                    present.add((key, language))
    db.session.commit()
    app.jinja_env.cache.clear()


@pytest.fixture
def no_volume(db, offer_skus, site_copy):
    """The 19L-coded bottle with no volume recorded (a name with no number in it), with its own photo; the 10 l
    bottle as it is."""
    offer_skus.p19.volume = None
    offer_skus.p19.name = "Aqua Element Office"
    offer_skus.p19.images = [PHOTO]
    db.session.commit()
    return offer_skus


def _facts(product) -> dict:
    return next(p for p in get_public_offer_facts()["products"] if p["id"] == product.id)


def _text(node) -> str:
    return flat(node.get_text(" "))


def _shop_card(soup, product):
    return next(
        card
        for card in soup.select(".product-grid .shop-block")
        if card.select_one(f'[data-action="add-to-cart-shop"][data-product-id="{product.id}"]')
    )


def _faqpage_questions(soup) -> list[str]:
    return [flat(entry["name"]) for faq_page in jsonld(soup, "FAQPage") for entry in faq_page.get("mainEntity", [])]


def _no_trade_size(text: str) -> bool:
    folded = flat(text).casefold()
    return not any(term in folded for term in TRADE_SIZE_19) and "(19" not in folded


@pytest.mark.parametrize("lang", LANGS)
def test_the_18_9_l_bottle_prints_its_volume_and_never_its_trade_size(app, db, offer_skus, site_copy, lang):
    p19 = offer_skus.p19
    size = VOLUME_18_9[lang]

    home = get_page(app, "/", lang)
    assert _text(home.select_one(f'[data-hero-product="{p19.id}"] .ae-home-prices__size')) == size
    assert _no_trade_size(home.select_one(f'[data-hero-product="{p19.id}"]').get_text(" "))
    assert _no_trade_size(home.select_one(".ae-home-hero__img")["alt"])
    assert _no_trade_size(home.select_one('meta[name="description"]')["content"])
    variant = next(v for v in jsonld(home, "ProductGroup")[0]["hasVariant"] if v["sku"] == p19.sku)
    assert flat(variant["size"]) == "18.9 L"

    shop = get_page(app, "/shop", lang)
    card = _shop_card(shop, p19)
    assert _text(card.select_one(".ae-product__size")) == size and _no_trade_size(card.get_text(" "))
    sizes_answer = faqpage_jsonld_answer(shop, shipped("landing.offer.faq.sizes_q", lang))
    assert size in sizes_answer and _no_trade_size(sizes_answer)

    product = get_page(app, f"/product/{p19.id}", lang)
    volume_fact = product.select_one(".shop-pd__facts .fa-tint").find_parent(class_="feature-card")
    assert _text(volume_fact.strong) == size
    block = product.select_one(f'[data-offer-product="{p19.id}"]')
    assert size in _text(block) and _no_trade_size(block.get_text(" "))

    services = get_page(app, "/services", lang)
    assert _text(services.select_one(f'[data-services-prices] [data-product-id="{p19.id}"] .ss-price__meta')) == size

    about = get_page(app, "/about", lang)
    cta = about.select_one(f'a[data-info-cta="product"][data-product-id="{p19.id}"]')
    assert size in _text(cta) and _no_trade_size(cta.get_text(" "))

    feed = app.test_client().get("/api/public/products.json").get_json()
    assert next(p for p in feed["products"] if p["id"] == p19.id)["size"] == {"label": "18.9L", "litres": 18.9}


@pytest.mark.parametrize("lang", LANGS)
def test_a_product_without_a_volume_prints_no_size_and_every_sentence_still_reads(app, db, no_volume, lang):
    p19, p10 = no_volume.p19, no_volume.p10
    facts = _facts(p19)
    assert facts["size_text"] is None
    name, price = facts["name"][lang], facts["price_text"][lang]
    assert name == "Aqua Element Office"
    # Its label, the one name copy gives a product by its size, is its name; the 10 l bottle's is its volume.
    assert facts["label"] == facts["name"] and _facts(p10)["label"] == _facts(p10)["size_text"]
    price_line = flat(shipped("landing.offer.price_line_no_size", lang).format(name=name, price=price))
    smallest = flat(
        shipped("landing.offer.smallest_order", lang).format(
            qty=facts["smallest_order_quantity"], size=name, total=facts["smallest_order_total_text"][lang]
        )
    )
    ten = _facts(p10)["size_text"][lang]

    # Home: no size chip in its price row, the smallest order names it, the hero photo is described by its name, the
    # meta description pairs its name with its price; the 10 l bottle keeps its size.
    home = get_page(app, "/", lang)
    row = home.select_one(f'[data-hero-product="{p19.id}"]')
    assert row.select(".ae-home-prices__size") == []
    assert _text(row.select_one(".ae-home-prices__note")) == smallest
    assert _text(home.select_one(f'[data-hero-product="{p10.id}"] .ae-home-prices__size')) == flat(ten)
    assert home.select_one(".ae-home-hero__img")["alt"] == name
    assert flat(f"{name} — {price}") in flat(home.select_one('meta[name="description"]')["content"])
    block = home.select_one(f'[data-offer-product="{p19.id}"]')
    assert _text(block.select_one('[data-offer-line="price"]')) == price_line
    assert _text(block.select_one('[data-offer-line="smallest-order"]')) == smallest
    group = jsonld(home, "ProductGroup")[0]
    variant = next(v for v in group["hasVariant"] if v["sku"] == p19.sku)
    assert "size" not in variant
    assert f"choose the {facts['name']['en']} returnable bottle" in group["description"]

    # Shop: no size chip on its card. The sizes answer is left out, on the shop and on the FAQ page: listing the 10 l
    # alone would say it is the only bottle there is, while the price answer beside it names this one too.
    shop = get_page(app, "/shop", lang)
    card = _shop_card(shop, p19)
    assert card.select(".ae-product__size") == []
    assert _text(card.select_one(".ae-product__note")) == smallest
    sizes_q, price_q = shipped("landing.offer.faq.sizes_q", lang), shipped("landing.offer.faq.price_q", lang)
    faq = get_page(app, "/water-delivery-faq", lang)
    for page in (shop, faq):
        assert flat(sizes_q) not in _faqpage_questions(page)
        assert flat(sizes_q) not in _text(page.select_one("main"))
        assert price_line in faqpage_jsonld_answer(page, price_q)
    # Searching is not displaying: the trade size still finds it.
    found = get_page(app, "/shop?search=19", lang)
    assert [int(b["data-product-id"]) for b in found.select('.product-grid [data-action="add-to-cart-shop"]')] == [
        p19.id
    ]

    # Product page: no volume fact; the description falls back to the sizeless price line.
    product = get_page(app, f"/product/{p19.id}", lang)
    assert product.select(".shop-pd__facts .fa-tint") == []
    assert flat(jsonld(product, "Product")[0]["description"]) == price_line
    assert _text(product.select_one(f'[data-offer-product="{p19.id}"] [data-offer-line="price"]')) == price_line

    # Services: no size line above its name.
    services = get_page(app, "/services", lang)
    price_row = services.select_one(f'[data-services-prices] [data-product-id="{p19.id}"]')
    assert price_row.select(".ss-price__meta") == []
    assert _text(price_row.select_one(".ss-price__min")) == smallest

    # About and gallery: the photo and the product link are named by the product.
    about = get_page(app, "/about", lang)
    assert about.select_one(".info-intro__media img")["alt"] == name
    assert _text(about.select_one(f'a[data-info-cta="product"][data-product-id="{p19.id}"]')) == name
    gallery = get_page(app, "/gallery", lang)
    photo = gallery.select_one(f'a.info-photo[data-product-id="{p19.id}"]')
    assert photo.select_one("img")["alt"] == name
    assert _text(photo.select_one(".info-photo__title")) == name

    for soup in (home, shop, faq, product, services, about, gallery):
        assert "None" not in _text(soup.select_one("main")), "an absent size printed as None"


def test_the_feeds_publish_no_size_for_a_product_without_a_volume(app, db, no_volume):
    p19 = no_volume.p19
    facts = _facts(p19)

    feed = app.test_client().get("/api/public/products.json").get_json()
    assert next(p for p in feed["products"] if p["id"] == p19.id)["size"] == {"label": None, "litres": None}

    llms = flat(app.test_client().get("/llms.txt").get_data(as_text=True))
    for lang in LANGS:
        line = shipped("landing.offer.price_line_no_size", lang)
        assert flat(line.format(name=facts["name"][lang], price=facts["price_text"][lang])) in llms
