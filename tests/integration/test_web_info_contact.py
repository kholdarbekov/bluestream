"""/contact, /coverage and /gallery after the redesign (website redesign step 3, group "info", unit "info-contact",
2026-11-14).

Each page is fetched as a signed-out visitor fetches it, in uz, ru and en, with the rows the seeds ship
(scripts/seed_web_ui_info_translations.py plus the shell, content and home seeds, and the backend seed's landing.*
rows). The copy is read from the seeds, never typed here.

Fixed on these pages, and pinned here:
- /contact's form posted to /api/v1/contact, a route that never existed, so every message failed; the page now lists
  the channels (the Telegram bot first, then the Telegram channel, Instagram and email, all from config) and the
  working hours, with no phone and no form, and its ContactPage JSON-LD names the same channels;
- /coverage printed an English "could not locate" line on ru/uz and showed "out of zone" on a network error; each
  outcome is now its own translated panel, the input sits in a labelled <form>, the district chips are buttons, the
  page has one translated meta description, its FAQ is visible and the FAQPage JSON-LD repeats it, and "when it
  arrives" is the offer facts' delivery statement, whole;
- /gallery showed 11 missing files over captions claiming 24/7 support and cooler installation; it now shows only the
  products' own photos (none of the theme's stock pictures) and links Instagram and the Telegram channel.

Fixed after the review (fix-info, 2026-11-14), and pinned here:
- coverage-map.js decided dropped pins and "my location" with its own point-in-polygon; every check now asks the
  endpoint (?lat=&lng= for a pin), which counts coordinate checks apart from the geocoded address checks;
- an address of spaces only did nothing and left the previous answer up: a translated error under the field;
- phones get a touch hint (two fingers move the map; one finger scrolls the page);
- the gallery's product links dropped the visitor's language.
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import pytest

from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts
from scripts.seed_web_ui_content_translations import TRANSLATIONS as CONTENT
from scripts.seed_web_ui_home_translations import TRANSLATIONS as HOME
from scripts.seed_web_ui_info_translations import CATEGORY, TRANSLATIONS as INFO
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from shared.constants import TASHKENT_POLYGON, get_delivery_coverage, get_delivery_statement
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

REPO = Path(__file__).resolve().parents[2]
LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_info_contact_test_bot"
CHANNEL_URL = "https://t.me/aqua_info_contact_test_channel"
INSTAGRAM_URL = "https://www.instagram.com/aqua_info_contact_test"
COMPANY_EMAIL = "hello@info-contact.test"
COMPANY_PHONE = "+998 00 000 0000"
# The office's hours: COMPANY_OPENING_HOURS (tests/integration/test_office_hours_footer.py pins the line itself); the
# contact page prints the footer's own lines.
OPENING_HOURS = "Mo-Sa 09:00-18:00"
HOURS_TEXT = {"uz": "Du–Sh 09:00–18:00", "ru": "пн–сб 09:00–18:00", "en": "Mon–Sat 09:00–18:00"}
PATHS = {"contact": "/contact", "coverage": "/coverage", "gallery": "/gallery"}
FAQ_COUNT = 2
RESULT_PANELS = ("checking", "ok", "no", "notfound", "error", "geo")
PHONE_WORDS = re.compile(r"\+998|\bcall us\b|\bphone us\b|позвон|звонит|qo‘ng‘iroq|qo'ng'iroq", re.IGNORECASE)
STOCK_CLAIMS = re.compile(r"24/7|cooler|кулер|sovutgich|climate|климат", re.IGNORECASE)


@pytest.fixture
def contact_copy(app, db, monkeypatch):
    """The rows the seeds ship for these pages, the office's hours, and known channel links."""
    for key, value in (
        ("COMPANY_OPENING_HOURS", OPENING_HOURS),
        ("COMPANY_TELEGRAM_BOT_URL", BOT_URL),
        ("COMPANY_TELEGRAM_CHANNEL_URL", CHANNEL_URL),
        ("COMPANY_INSTAGRAM_URL", INSTAGRAM_URL),
        ("COMPANY_EMAIL", COMPANY_EMAIL),
        ("COMPANY_PHONE", COMPANY_PHONE),
    ):
        monkeypatch.setitem(app.config, key, value)
    seed_copy(db)
    present = set(db.session.query(Translation.key, Translation.language).all())
    for rows_by_lang in (SHELL, INFO, CONTENT, HOME):
        for language, rows in rows_by_lang.items():
            for key, value in rows.items():
                if (key, language) not in present:
                    db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
                    present.add((key, language))
    db.session.commit()
    app.jinja_env.cache.clear()


def _copy(lang, key, **kwargs):
    """The text a seed ships for `key` in `lang`, formatted like the `t` filter formats it."""
    for rows in (INFO, SHELL, CONTENT, HOME):
        if key in rows[lang]:
            return rows[lang][key].format(**kwargs) if kwargs else rows[lang][key]
    return shipped(key, lang)


def _main(soup):
    return soup.select_one("main")


def _text(node):
    return flat(node.get_text(" "))


# --- Every page ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", sorted(PATHS))
def test_every_page_reads_in_the_visitors_language(app, db, offer_skus, contact_copy, name, lang):
    soup = get_page(app, PATHS[name], lang)
    html = str(soup)
    for prefix in ("web.info.", "web.shell.", "web.content.", "web.home.", "landing.coverage.", "landing.gallery."):
        assert prefix not in html, f"a {prefix} key reached the page untranslated"
    assert soup.html["lang"] == lang
    main = _main(soup)
    assert soup.select_one('link[href*="css/pages/info.css?v="]') is not None
    h1s = main.select("h1")
    assert len(h1s) == 1
    # One meta description, in the visitor's language (coverage printed the English default as a second one).
    metas = soup.select('meta[name="description"]')
    assert [m["content"] for m in metas] == [_copy(lang, f"web.info.{name}.meta")]
    # Never "call us": no tel: link and no phone wording in the page body.
    assert main.select('a[href^="tel:"]') == []
    assert not PHONE_WORDS.search(_text(main))
    # The old theme's scroll-in animations and stock gallery pictures are gone.
    assert not main.select(".wow, .fadeInUp")
    assert "images/gallery/" not in str(main)
    # The page offers the bot.
    assert soup.select_one(f'a[href="{BOT_URL}"]') is not None


# --- /contact ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_contact_lists_the_channels_from_config_and_has_no_dead_form(app, db, offer_skus, contact_copy, lang):
    soup = get_page(app, "/contact", lang)
    main = _main(soup)
    # The form posted to /api/v1/contact, which never existed: no form, no fields, no script left to post it.
    assert main.select("form, input, textarea") == []
    assert soup.select_one('script[src*="pages/contact.js"]') is None
    assert not (REPO / "business_app/static/js/pages/contact.js").exists()
    # The bot leads, then the channel, Instagram and email, each from config.
    bot = main.select_one('[data-contact-channel="bot"]')
    assert bot.select_one(f'a[href="{BOT_URL}"]') is not None
    assert _text(bot.select_one("h2")) == _copy(lang, "web.info.contact.bot_h")
    channels = main.select(".info-channels a[data-contact-channel]")
    assert [(a["data-contact-channel"], a["href"]) for a in channels] == [
        ("channel", CHANNEL_URL),
        ("instagram", INSTAGRAM_URL),
        ("email", f"mailto:{COMPANY_EMAIL}"),
    ]
    assert COMPANY_EMAIL in _text(channels[2])
    # Working hours are the footer's own rows, so the two never disagree.
    hours = [_text(li) for li in main.select("[data-contact-hours] li")]
    footer_hours = [_text(li) for li in soup.select(".ae-footer__hours li")]
    assert hours == footer_hours
    assert hours[0] == flat(_copy(lang, "web.shell.footer.office_hours", hours=HOURS_TEXT[lang]))
    # The side links keep the visitor's language.
    hrefs = [a["href"] for a in main.select("a.info-link")]
    assert any("/coverage" in h for h in hrefs) and any("/water-delivery-faq" in h for h in hrefs)
    if lang != "uz":  # uz is the default language: its URLs carry no ?lang
        assert all(f"lang={lang}" in h for h in hrefs)


def test_the_contact_api_the_old_form_posted_to_does_not_exist(app, db, contact_copy):
    """The page no longer posts anywhere; the route it used to call is still not there (no hidden fallback)."""
    response = app.test_client().post("/api/v1/contact", json={"name": "x", "message": "y"})
    assert response.status_code in (404, 405)


@pytest.mark.parametrize("lang", LANGS)
def test_contact_jsonld_names_the_same_channels(app, db, offer_skus, contact_copy, lang):
    soup = get_page(app, "/contact", lang)
    [page] = jsonld(soup, "ContactPage")
    assert page["name"] == f'{_copy(lang, "web.info.contact.title")} — {app.config.get("COMPANY_NAME", "Aqua Element")}'
    org = page["mainEntity"]
    assert org["@type"] == "Organization" and org["@id"].endswith("#organization")
    assert (org["email"], org["telephone"]) == (COMPANY_EMAIL, COMPANY_PHONE)
    assert org["sameAs"] == [INSTAGRAM_URL, CHANNEL_URL]
    assert org["contactPoint"] == {
        "@type": "ContactPoint",
        "contactType": "customer support",
        "url": BOT_URL,
        "email": COMPANY_EMAIL,
    }


# --- /coverage -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_coverage_checker_is_a_labelled_form_with_a_translated_panel_per_outcome(app, db, offer_skus, contact_copy, lang):
    soup = get_page(app, "/coverage", lang)
    main = _main(soup)
    form = main.select_one("form#coverage-form")
    address = form.select_one("input#coverage-address")
    assert address.has_attr("required") and form.select_one('label[for="coverage-address"]') is not None
    assert form.select_one("button#coverage-check-btn")["type"] == "submit"
    assert form.select_one("button#coverage-locate-btn")["type"] == "button"
    results = main.select_one(".info-cov__results")
    assert results["aria-live"] == "polite"
    for state in RESULT_PANELS:
        panel = results.select_one(f"#coverage-result-{state}")
        assert panel is not None and panel.has_attr("hidden"), state
        assert _text(panel), state
    assert _copy(lang, "web.info.coverage.notfound") == _text(results.select_one("#coverage-result-notfound"))
    # An address of spaces only passes `required`: the script shows this translated line under the field.
    empty = form.select_one("#coverage-address-error")
    assert empty.has_attr("hidden") and _text(empty) == _copy(lang, "web.info.coverage.address_empty")
    # Phones swap the map hint: one finger scrolls the page, two fingers move the map.
    hints = {h["data-map-hint"]: h for h in main.select("[data-map-hint]")}
    assert _text(hints["pointer"]) == _copy(lang, "web.info.coverage.map_hint") and not hints["pointer"].has_attr("hidden")
    assert _text(hints["touch"]) == _copy(lang, "web.info.coverage.map_hint_touch") and hints["touch"].has_attr("hidden")
    assert _copy(lang, "web.info.coverage.error") == _text(results.select_one("#coverage-result-error"))
    assert "Could not locate" not in str(soup)
    # In the zone: order in the bot or on the site. Outside it: the bot.
    ok = results.select_one("#coverage-result-ok")
    assert ok.select_one(f'a[href="{BOT_URL}"]') is not None and ok.select_one('a[href*="/shop"]') is not None
    assert results.select_one("#coverage-result-no").select_one(f'a[href="{BOT_URL}"]') is not None
    # The chips are buttons, one per district of the SSOT, in the visitor's language.
    coverage = get_delivery_coverage(lang)
    chips = main.select("button.ae-chip[data-district]")
    assert [(c["data-district"], _text(c)) for c in chips] == [(d["key"], d["name"]) for d in coverage["districts"]]
    assert _text(main.select_one(".info-cov__districts .info-note")) == _copy(
        lang, "web.info.coverage.region_plus", region=coverage["region_note"]
    )


@pytest.mark.parametrize("lang", LANGS)
def test_coverage_says_when_it_arrives_with_the_offer_facts_whole(app, db, offer_skus, contact_copy, lang):
    main = _main(get_page(app, "/coverage", lang))
    statement = main.select("[data-delivery-statement]")
    assert [_text(p) for p in statement] == [flat(" ".join(get_delivery_statement(lang)))]
    lines = [_text(li) for li in main.select(".info-when li")]
    assert lines == [flat(get_public_offer_facts()["delivery"]["line"][lang]), flat(shipped("landing.offer.every_day", lang))]


@pytest.mark.parametrize("lang", LANGS)
def test_coverage_faq_is_visible_and_the_faqpage_jsonld_repeats_it(app, db, offer_skus, contact_copy, lang):
    soup = get_page(app, "/coverage", lang)
    items = _main(soup).select(".info-cov__faq details")
    assert len(items) == FAQ_COUNT
    for n, item in enumerate(items, 1):
        question, answer = _text(item.select_one("summary")), _text(item.select_one(".ae-faq__a"))
        assert (question, answer) == (_copy(lang, f"web.info.coverage.faq_{n}_q"), _copy(lang, f"web.info.coverage.faq_{n}_a"))
        assert faqpage_jsonld_answer(soup, question) == answer
    # The GeoShape stays the enforced polygon.
    [service] = jsonld(soup, "Service")
    assert service["areaServed"]["@type"] == "GeoShape"


@pytest.mark.parametrize(
    "geocoded, deliverable",
    [({"latitude": 41.3111, "longitude": 69.2797, "formatted_address": "Tashkent"}, True),
     ({"latitude": 40.9, "longitude": 69.9, "formatted_address": "Far away"}, False),
     (None, None)],
)
def test_the_checker_endpoint_answers_in_the_shape_coverage_map_reads(app, db, offer_skus, contact_copy, geocoded, deliverable):
    """coverage-map.js GETs `checkUrl?address=…` from #coverage-data. A boolean is_deliverable with coordinates places
    the pin (ok / no); anything else is the "not found" panel."""
    soup = get_page(app, "/coverage", "ru")
    data = json.loads(soup.select_one("script#coverage-data").string)
    assert data["polygon"] == [list(point) for point in TASHKENT_POLYGON]
    with patch("business_app.services.maps_service.MapsService.geocode_address", return_value=geocoded):
        response = app.test_client().get(data["checkUrl"], query_string={"address": "улица Амира Темура, 12"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["is_deliverable"] is deliverable
    if deliverable is None:
        assert (body["latitude"], body["reason"]) == (None, "not_geocoded")
    else:
        assert (body["latitude"], body["longitude"]) == (geocoded["latitude"], geocoded["longitude"])


@pytest.mark.parametrize(
    "lat, lng, deliverable",
    [("41.311081", "69.240562", True), ("40.5", "68.5", False)],  # central Tashkent; far outside the polygon
)
def test_a_dropped_pin_is_decided_by_the_endpoint_not_the_page(app, db, offer_skus, contact_copy, lat, lng, deliverable):
    """coverage-map.js no longer runs a point-in-polygon of its own: a pin (and "my location") GETs
    `checkUrl?lat=&lng=`, and the answer is shared.constants.is_within_tashkent's, with no geocoder call."""
    from shared.constants import is_within_tashkent

    data = json.loads(get_page(app, "/coverage", "ru").select_one("script#coverage-data").string)
    with patch("business_app.services.maps_service.MapsService.geocode_address") as geocode:
        response = app.test_client().get(f"{data['checkUrl']}?lat={lat}&lng={lng}")
    geocode.assert_not_called()
    assert response.status_code == 200
    body = response.get_json()
    assert (body["latitude"], body["longitude"]) == (float(lat), float(lng))
    assert body["is_deliverable"] is deliverable is is_within_tashkent(float(lat), float(lng))
    assert "pointInPolygon" not in (REPO / "business_app/static/js/coverage-map.js").read_text(encoding="utf-8")


def test_pins_count_apart_from_geocoded_addresses(app, monkeypatch):
    """Every dropped pin is now a request: coordinate checks have their own bucket, so a visitor exploring the map
    never uses up the address checks (each one an upstream geocoder call) of the visitors behind the same IP.

    The test app runs with the limiter off (RATELIMIT_ENABLED=False skips its set-up), so the endpoint's own limit is
    driven on a small app with a live in-memory limiter, with the payloads coverage-map.js sends, after checking the
    endpoint is wired to that limit."""
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    from business_app import limiter
    from business_app.frontend import routes

    endpoint = f"{routes.public_check_delivery.__module__}.{routes.public_check_delivery.__name__}"
    wired = [name for name in limiter.limit_manager._decorated_limits if name.startswith(endpoint)]
    assert [g.limit_provider for name in wired for g in limiter.limit_manager._decorated_limits[name]] == [
        routes._check_delivery_rate_limit
    ]
    assert routes.CHECK_DELIVERY_ADDRESS_RATE_LIMIT == "60/hour"  # the geocoded checks keep their limit

    monkeypatch.setattr(routes, "CHECK_DELIVERY_ADDRESS_RATE_LIMIT", "2/hour")
    monkeypatch.setattr(routes, "CHECK_DELIVERY_COORDINATES_RATE_LIMIT", "3/hour")
    probe = Flask("check_delivery_buckets")
    probe.config.update(RATELIMIT_ENABLED=True, RATELIMIT_STORAGE_URI="memory://")
    live = Limiter(key_func=get_remote_address, app=probe)

    @probe.route("/api/public/check-delivery")
    @live.limit(routes._check_delivery_rate_limit)
    def check():
        return "checked"

    client = probe.test_client()
    url = "/api/public/check-delivery"
    address = [client.get(url, query_string={"address": "Chilonzor 1"}).status_code for _ in range(3)]
    pins = [client.get(url, query_string={"lat": "41.31", "lng": "69.24"}).status_code for _ in range(4)]
    assert address == [200, 200, 429]
    assert pins == [200, 200, 200, 429]


# --- /gallery ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_gallery_shows_only_the_products_own_photos(app, db, offer_skus, contact_copy, lang):
    offer_skus.p19.images = ["https://cdn.example.test/ae-19.png"]
    offer_skus.p10.images = []  # no photo: left out, never filled with a stock picture
    db.session.commit()
    soup = get_page(app, "/gallery", lang)
    main = _main(soup)
    facts = {p["id"]: p for p in get_public_offer_facts()["products"]}
    photos = main.select("a.info-photo")
    # The product's own page in the visitor's language (the raw path dropped ?lang).
    assert [(a["data-product-id"], urlsplit(a["href"]).path) for a in photos] == [
        (str(offer_skus.p19.id), facts[offer_skus.p19.id]["path"])
    ]
    assert parse_qs(urlsplit(photos[0]["href"]).query).get("lang", []) == ([] if lang == "uz" else [lang])
    img = photos[0].select_one("img")
    size = facts[offer_skus.p19.id]["size_text"][lang]
    assert img["src"] == "https://cdn.example.test/ae-19.png" and img["loading"] == "lazy"
    assert flat(img["alt"]) == flat(_copy(lang, "web.home.hero.image_alt", size=size))
    assert _text(photos[0].select_one(".info-photo__title")) == flat(_copy(lang, "web.info.cta.product", size=size))
    # More photos: Instagram and the Telegram channel.
    more = main.select_one("[data-gallery-more]")
    assert more.select_one(f'a[href="{INSTAGRAM_URL}"]') is not None
    assert more.select_one(f'a[href="{CHANNEL_URL}"]') is not None
    # No stock pictures, no isotope filters, no invented captions.
    assert soup.select_one('script[src*="isotope"], script[src*="pages/gallery.js"]') is None
    assert main.select(".filter-btns, .masonry-item") == []
    assert not STOCK_CLAIMS.search(_text(main))


def test_gallery_without_a_product_photo_shows_only_the_instagram_state(app, db, offer_skus, contact_copy):
    main = _main(get_page(app, "/gallery", "ru"))
    assert main.select("a.info-photo") == []
    assert main.select_one("#gallery-title") is None
    more = main.select_one("[data-gallery-more]")
    assert _text(more.select_one("h2")) == _copy("ru", "web.info.gallery.more_h")
