"""/subscriptions and /services after the redesign (website redesign step 3, group "subs", 2026-11-14).

Each page is fetched as a signed-out visitor fetches it, in uz, ru and en, with the rows the seeds ship
(scripts/seed_web_ui_subs_translations.py and the backend seed's landing.* rows) and the two bottle SKUs of
`offer_skus`, priced as no production row is, so a price typed into the template cannot pass. Each page states only
what the backend publishes:
- /subscriptions: the subscription catalog the builder reads (GET /api/v1/subscriptions/products), whose smallest
  quantity is the one POST /api/v1/subscriptions/ enforces, and the public offer's delivery line;
- /services: the public offer facts (price, smallest order, the delivery line and the approved delivery statement).
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts, smallest_order_sentence
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_web_ui_subs_translations import CATEGORY, TRANSLATIONS as SUBS
from shared.constants import get_delivery_coverage, get_delivery_statement
from shared.enums import SubscriptionFrequency
from shared.payment_methods import CUSTOMER_SELECTABLE_METHODS
from tests.integration.public_page_builders import flat, get_page, offer_skus, seed_copy  # noqa: F401 -- fixture
from tests.integration.reorder_reminder_builders import customer_headers
from tests.integration.retired_phrases import find_retired

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "business_app" / "templates" / "frontend"
LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_subs_page_test_bot"
BUILDER_URL = "/#subscription-constructor"
FAQ_IDS = ("how", "customize", "pause", "contract", "get", "payment", "address", "failed")
SERVICE_ROWS = ["water-delivery", "bottle-exchange", "subscription", "delivery-zone"]
BUILDER_BODIES = json.loads((REPO / "tests" / "fixtures" / "web_subscription_builder_bodies.json").read_text("utf-8"))


@pytest.fixture
def subs_copy(app, db, monkeypatch):
    """The storefront rows the backend seed ships (landing.*, the money labels, the cabinet's "My subscriptions")
    plus this page group's own rows, and a known bot link."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    seed_copy(db, ("landing.", "web.display_labels", "cabinet.subs."))
    for language, rows in SUBS.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _s(lang, key, **values):
    return SUBS[lang][key].format(**values) if values else SUBS[lang][key]


def _shipped(key, lang):
    return BACKEND_TRANSLATIONS[key].get(lang) or BACKEND_TRANSLATIONS[key]["en"]


def _template_keys(name):
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    return set(re.findall(r"'([a-z0-9_]+(?:\.[a-z0-9_]+)+)'\s*\|\s*t\b", source))


# --- /subscriptions -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_subscriptions_page_reads_in_the_visitors_language_and_leads_to_the_builder_and_the_bot(
    app, db, offer_skus, subs_copy, lang
):
    soup = get_page(app, "/subscriptions", lang)
    html = str(soup)
    main = soup.select_one("main")

    assert "web.subs." not in html, "a web.subs key reached the page untranslated"
    assert flat(main.select_one(".page-title h1").get_text(" ")) == _s(lang, "web.subs.title")
    assert soup.title.get_text().startswith(_s(lang, "web.subs.title"))
    assert flat(soup.select_one('meta[name="description"]')["content"]) == flat(_s(lang, "web.subs.meta"))
    # The banner carries no lead: the intro right below says what a subscription does (it repeated it).
    assert main.select(".page-title .ae-hero__lead") == []
    assert flat(main.select_one(".page-title .bread-crumb li:last-child").get_text(" ")) == _shipped(
        "landing.nav.subscriptions", lang
    )
    # The two real ways to subscribe, at the top and in the closing band: the home-page builder and the bot.
    builder = main.select('a[data-subs-cta="builder"]')
    bot = main.select('a[data-subs-cta="telegram"]')
    assert [a["href"] for a in builder] == [BUILDER_URL, BUILDER_URL]
    assert [a["href"] for a in bot] == [BOT_URL, BOT_URL]
    assert {flat(a.get_text(" ")) for a in builder} == {_s(lang, "web.subs.cta.builder")}
    assert {flat(a.get_text(" ")) for a in bot} == {_s(lang, "web.subs.cta.telegram")}
    # The frequencies the backend offers (shared.enums.SubscriptionFrequency), in its order, each named as the
    # builder names it (so the page and the builder call a choice the same), then explained.
    frequencies = main.select("[data-subs-frequencies] [data-frequency]")
    assert [row["data-frequency"] for row in frequencies] == [f.value for f in SubscriptionFrequency]
    assert [flat(row.dt.get_text()) for row in frequencies] == [
        _shipped(f"landing.custom_subscription.frequency.{f.value}", lang) for f in SubscriptionFrequency
    ]
    assert all(flat(row.dd.get_text()) for row in frequencies)
    # The time note quotes the builder's own "Any time" option.
    any_time = _shipped("landing.custom_subscription.any_time", lang)
    notes = [flat(p.get_text(" ")) for p in main.select(".ss-note")]
    assert flat(_s(lang, "web.subs.schedule.time", option=any_time)) in notes
    # The payment rows are the methods a customer may pick, no more and no fewer, each named by the display label
    # the builder, checkout and the cabinet show ("Bank transfer", not a page-only "Business account").
    payments = main.select("[data-subs-payments] [data-payment-method]")
    assert {row["data-payment-method"] for row in payments} == {m.value for m in CUSTOMER_SELECTABLE_METHODS}
    assert len(payments) == len(CUSTOMER_SELECTABLE_METHODS)
    for row in payments:
        label = _shipped(f"web.display_labels.payment_method.{row['data-payment-method']}", lang)
        assert flat(row.dt.get_text()) == label
        assert flat(row.dd.get_text())
    # No second name for the company method anywhere on the page (the FAQ says "bank transfer" too).
    assert flat(_s(lang, "web.subs.faq.payment_a")) in flat(main.select_one("#faq").get_text(" "))
    page_text = flat(main.get_text(" ")).lower()
    assert not [name for name in ("business account", "бизнес-счёт", "бизнес-счет", "biznes hisob") if name in page_text]
    # Landmarks: the spec rows are parts of "What you choose", not regions of their own.
    assert main.select("#details section, #details [aria-labelledby]") == []
    # Changes happen in the cabinet: the link goes to /my-subscriptions.
    assert main.select_one('a[href="/my-subscriptions"]') is not None


@pytest.mark.parametrize("lang", LANGS)
def test_the_subscriptions_faq_is_eight_native_accordions_with_no_theme_or_page_script(
    app, db, offer_skus, subs_copy, lang
):
    """The old FAQ was bound twice (subscriptions.js and the theme's script.js), so an open answer could not be
    closed. It is now <details>/<summary>, which needs no script; the fake frequency preview went with the script."""
    soup = get_page(app, "/subscriptions", lang)
    main = soup.select_one("main")

    items = main.select(".ae-faq > details.ae-faq__item")
    assert [flat(item.summary.get_text(" ")) for item in items] == [_s(lang, f"web.subs.faq.{i}_q") for i in FAQ_IDS]
    assert all(item.select_one(".ae-faq__a p") for item in items)
    assert main.select(".accordion-box, .acc-btn, .frequency-option, #preview-frequency, .wow") == []
    assert not [s for s in soup.select("script[src]") if "pages/subscriptions.js" in s["src"]]
    assert soup.select_one('link[href*="css/pages/subs.css?v="]') is not None


@pytest.mark.parametrize("lang", LANGS)
def test_the_subscriptions_page_lists_the_catalog_the_builder_reads(app, db, offer_skus, subs_copy, lang):
    """The water rows are GET /api/v1/subscriptions/products as the builder reads it: the same products, names,
    live price labels and smallest quantities, in the visitor's language."""
    api = app.test_client().get(f"/api/v1/subscriptions/products?lang={lang}", follow_redirects=True)
    assert api.status_code == 200, api.get_data(as_text=True)[:300]
    items = api.get_json()["data"]["items"]
    assert {item["id"] for item in items} == {offer_skus.p19.id, offer_skus.p10.id}

    rows = get_page(app, "/subscriptions", lang).select("[data-subs-catalog] > [data-product-id]")

    assert [int(row["data-product-id"]) for row in rows] == [item["id"] for item in items]
    for row, item in zip(rows, items):
        assert flat(row.select_one("[data-price]").get_text(" ")) == flat(item["unit_price_label"])
        minimum = row.select_one("[data-min-qty]")
        assert int(minimum["data-min-qty"]) == item["min_order_quantity"]
        assert flat(minimum.get_text(" ")) == _s(lang, "web.subs.water.min", qty=item["min_order_quantity"])
        assert flat(item["product_name"]) in flat(row.get_text(" "))
    prices = {int(row["data-product-id"]): re.sub(r"\D", "", row.select_one("[data-price]").get_text()) for row in rows}
    assert prices == {offer_skus.p19.id: "23450", offer_skus.p10.id: "12340"}


def test_the_smallest_quantity_the_page_states_is_the_one_a_subscription_save_enforces(
    app, db, client, auth_headers, user_address, sample_product, subs_copy
):
    """A widened promise must be one the write path honours: the page's "from N per delivery" for a product is the
    smallest quantity POST /api/v1/subscriptions/ accepts, with the body the home-page builder posts."""
    sample_product.min_order_quantity = 3
    key = "api.subscriptions.error.below_min_order_quantity"
    for lang in LANGS:
        db.session.add(Translation(key=key, language=lang, value=_shipped(key, lang), category="api", is_active=True))
    db.session.commit()
    row = get_page(app, "/subscriptions", "en").select_one(f'[data-subs-catalog] [data-product-id="{sample_product.id}"]')
    stated = int(row.select_one("[data-min-qty]")["data-min-qty"])
    assert stated == 3

    case = next(case for case in BUILDER_BODIES["cases"] if case.get("any_time"))
    body = {**case["body"], "delivery_address_id": user_address.id}
    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        body["items"] = [{"product_id": sample_product.id, "quantity": stated - 1}]
        refused = client.post("/api/v1/subscriptions/", headers=auth_headers, json=body)
        body["items"] = [{"product_id": sample_product.id, "quantity": stated}]
        accepted = client.post("/api/v1/subscriptions/", headers=auth_headers, json=body)

    assert refused.status_code == 400, refused.get_json()
    # The refusal names the product and the same smallest quantity (the endpoint publishes the message, no code).
    message = refused.get_json()["message"]
    expected = {
        _shipped(key, lang).format(product=sample_product.name, minimum=stated, quantity=stated - 1) for lang in LANGS
    }
    assert message in expected, message
    assert accepted.status_code == 201, accepted.get_json()


def test_with_a_delivery_fee_the_page_promises_no_free_delivery(app, db, offer_skus, subs_copy, monkeypatch):
    """Free delivery is the public offer's fact, not the page's: with a fee configured, the "always free" perk goes
    and the FAQ states the fee line the offer publishes."""
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    with app.test_request_context():
        fee_line = get_public_offer_facts()["delivery"]["line"]["en"]
    assert "5" in fee_line and "free" not in fee_line.lower()

    main = get_page(app, "/subscriptions", "en").select_one("main")

    assert _s("en", "web.subs.perk.delivery") not in flat(main.get_text(" "))
    answers = {flat(i.summary.get_text(" ")): flat(i.select_one(".ae-faq__a").get_text(" ")) for i in main.select(".ae-faq__item")}
    get_answer = answers[_s("en", "web.subs.faq.get_q")]
    assert get_answer == flat(" ".join([_s("en", "web.subs.faq.get_a1"), fee_line, _s("en", "web.subs.faq.get_a2")]))


@pytest.mark.parametrize("lang", LANGS)
def test_with_a_delivery_fee_no_subscription_surface_says_delivery_is_free(
    app, db, offer_skus, subs_copy, sample_user, monkeypatch, lang
):
    """Review SITE-02: free delivery is ONE fact (the public offer's `delivery.free`), so /subscriptions, /services and
    /my-subscriptions drop it together once a fee is configured, as /delivery-policy and /loyalty-guide already do."""
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", 5000)
    free_words = {"en": "free", "ru": "бесплат", "uz": "bepul"}[lang]

    for path in ("/subscriptions", "/services"):
        main = flat(get_page(app, path, lang).select_one("main").get_text(" ")).lower()
        assert free_words not in main, f"{path}?lang={lang} still says delivery is free"

    response = app.test_client().get(
        f"/my-subscriptions?lang={lang}", headers=customer_headers(app, sample_user), follow_redirects=True
    )
    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    island = json.loads(soup.select_one("script#page-data").string)
    # The renderer prints `items.benefit_free` only while the island says delivery is free (mySubscriptions.test.js).
    assert island["delivery_free"] is False
    assert free_words not in flat(soup.select_one("main").get_text(" ")).lower()
    assert free_words not in island["i18n"]["empty.body"].lower()


def test_without_a_delivery_fee_my_subscriptions_publishes_free_delivery(app, db, offer_skus, subs_copy, sample_user):
    response = app.test_client().get("/my-subscriptions?lang=en", headers=customer_headers(app, sample_user))
    island = json.loads(BeautifulSoup(response.get_data(as_text=True), "html.parser").select_one("script#page-data").string)
    assert island["delivery_free"] is True
    assert island["i18n"]["items.benefit_free"] == _shipped("cabinet.subs.items.benefit_free", "en") == "Delivery is free"


# --- /services ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_services_page_lists_only_what_aqua_element_does_at_live_prices(app, db, offer_skus, subs_copy, lang):
    soup = get_page(app, "/services", lang)
    html = str(soup)
    main = soup.select_one("main")
    with app.test_request_context():
        facts = get_public_offer_facts()
        smallest = {p["id"]: flat(smallest_order_sentence(p, lang)) for p in facts["products"]}

    assert "web.subs." not in html, "a web.subs key reached the page untranslated"
    assert flat(main.select_one(".page-title h1").get_text(" ")) == _s(lang, "web.subs.services.title")
    assert flat(soup.select_one('meta[name="description"]')["content"]) == flat(_s(lang, "web.subs.services.meta"))
    assert [row["id"] for row in main.select(".ss-spec > .ss-spec__row")] == SERVICE_ROWS
    assert main.select(".ss-spec section, .ss-spec [aria-labelledby]") == []
    # Water delivery: every active product at its live price and smallest order, linked to its page.
    rows = main.select("[data-services-prices] > [data-product-id]")
    assert [int(row["data-product-id"]) for row in rows] == [p["id"] for p in facts["products"]]
    for row, product in zip(rows, facts["products"]):
        assert flat(row.select_one("[data-price]").get_text(" ")) == flat(product["price_text"][lang])
        assert smallest[product["id"]] in flat(row.get_text(" "))
        link = row.select_one("a")
        assert link["href"] == product["path"] and "ss-price__link" in link["class"]
        assert flat(link.get_text(" ")) == flat(product["name"][lang])
    assert {re.sub(r"\D", "", row.select_one("[data-price]").get_text()) for row in rows} == {"23450", "12340"}
    # The bottle exchange and the no-deposit sentence (the 18.9 L SKU is returnable).
    exchange = flat(main.select_one("#bottle-exchange").get_text(" "))
    assert _shipped("landing.offer.own_bottles", lang) in exchange
    assert _shipped("landing.offer.no_deposit", lang) in exchange
    # The delivery zone: the coverage SSOT and the approved statement, printed whole.
    zone = main.select_one("#delivery-zone")
    assert get_delivery_coverage(lang)["summary"] in flat(zone.get_text(" "))
    assert [flat(p.get_text(" ")) for p in main.select("[data-delivery-statement]")] == [
        flat(" ".join(get_delivery_statement(lang)))
    ]
    assert main.select_one('#delivery-zone a[href*="/coverage"]') is not None
    assert main.select_one('#subscription a[href*="/subscriptions"]') is not None
    # The subscription row names the backend's frequencies as the builder does; delivery ends with the two ways
    # to order.
    ticks = main.select("[data-services-frequencies] [data-frequency]")
    assert [li["data-frequency"] for li in ticks] == [f.value for f in SubscriptionFrequency]
    assert [flat(li.get_text(" ")) for li in ticks] == [
        _shipped(f"landing.custom_subscription.frequency.{f.value}", lang) for f in SubscriptionFrequency
    ]
    order_links = [a["href"] for a in main.select("#water-delivery .ss-actions a")]
    assert len(order_links) == 2 and order_links[0].split("?")[0].endswith("/shop") and order_links[1] == BOT_URL
    # The test-pinned blurbs stay, and the invented office "solutions" (dispensers) are gone.
    text = flat(main.get_text(" "))
    assert flat(_shipped("landing.services.home_delivery.description", lang)) in text
    assert flat(_shipped("landing.services.subscription_plans.description", lang)) in text
    for invented in ("dispenser", "диспенсер", "premium", "премиум", "24/7"):
        assert invented not in text.lower()


# --- The copy itself ------------------------------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("name", ["subscriptions.html", "services.html"])
def test_every_key_the_pages_ask_for_ships_in_a_seed_in_three_languages(name):
    shipped = {key: row for key, row in BACKEND_TRANSLATIONS.items()}
    for lang, rows in SUBS.items():
        for key, value in rows.items():
            shipped.setdefault(key, {})[lang] = value
    keys = _template_keys(name)
    assert keys
    assert sorted(keys - set(shipped)) == [], f"{name} asks for keys no seed ships"
    for key in keys:
        assert all(shipped[key].get(lang) for lang in LANGS), f"{key} misses a language"


@pytest.mark.unit
def test_the_seed_is_trilingual_truthful_and_writes_uzbek_with_its_own_apostrophes():
    assert set(SUBS["en"]) == set(SUBS["ru"]) == set(SUBS["uz"])
    forbidden = {
        "en": ["discount", "savings", "bonus", "sms", "2x", "%", "24/7", "money back", "guarantee", "premium"],
        "ru": ["скидк", "эконом", "бонус", "sms", "%", "24/7", "гарант", "премиум"],
        "uz": ["chegirma", "tejash", "tejay", "bonus", "sms", "%", "24/7", "kafolat", "premium"],
    }
    for lang in LANGS:
        for key, value in SUBS[lang].items():
            assert key.startswith("web.subs.") and value.strip(), key
            assert find_retired(flat(value)) == [], f"{lang} {key}: a retired delivery claim"
            assert not re.search(r"\bcall|позвон|qo.ng.iroq|телефон|telefon", value, re.I), f"{lang} {key}: says call"
            # No typed number: prices, quantities and fees are printed live from the backend.
            assert not re.search(r"\d", value), f"{lang} {key}: a typed number"
            for word in forbidden[lang]:
                assert word not in value.lower(), f"{lang} {key}: {word!r}"
            # A dash never starts a line: the space before it is a no-break space.
            assert " —" not in value, f"{lang} {key}: a breakable space before a dash"
    for key, value in SUBS["uz"].items():
        assert "'" not in value and "ʻ" not in value, f"uz {key}: write ‘ (o‘, g‘) and ’, not ' or ʻ"
