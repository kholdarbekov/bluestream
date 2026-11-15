"""The company info pages after the redesign (website redesign step 3, group "info", unit "info-company", 2026-11-14):
/about, /about/sources, /quality-standards and /process/11-step-filtration.

Each page is fetched as a signed-out visitor fetches it, in uz, ru and en, with the rows the seeds ship
(scripts/seed_web_ui_info_translations.py, scripts/seed_web_ui_shell_translations.py and the backend seed's landing.*
rows). The copy is read from the seeds, never typed here.

Fixed on these pages, and pinned here:
- /quality-standards was English on ru/uz; /about claimed "10+ years", "leading in Uzbekistan" and Blue Stream Group
  over a damaged stock photo; now every line is a key, the facts are the ones /about/sources documents and the photo
  is the hero product's own image;
- the sources and filtration copy lived in Jinja dicts with the mineral table typed six times; now one set of keys
  serves both pages, and the visible FAQ, the HowTo stages and their JSON-LD read the same keys;
- the sources page printed `**label**` Markdown and claimed a regulator link it never showed;
- the filtration CTA named two bottles but both buttons opened /shop; each now opens its product page;
- the HowTo JSON-LD said the process takes 11 seconds (`totalTime: PT11S`);
- the lab-report address is the configured MAIL_SUPPORT_EMAIL, as a mailto link, not typed into the copy;
- the quality page's policy links dropped the visitor's language.

Fixed after the review (fix-info, 2026-11-14), and pinned here:
- the order band's product links were the raw `/product/…` path and dropped the visitor's language;
- the sources page named Google and Yandex records it never showed: its lead names only the configured records;
- below 992px the sources page had no contents list: a folded <details> repeats the aside's list;
- the filtration stage label typed its word order ("Bosqich 1"): the number is a {n} placeholder ("1-bosqich");
- /about put ozone before mineralisation; the ru/uz comparison table used "немин.", "ОО" and "OO";
- the water facts (mineral numbers in every language, the well depth) are held to the feed's published values.
"""

import re
from urllib.parse import parse_qs, urlsplit

import pytest

from business_app.models.translation import Translation
from business_app.services.public_offer_service import get_public_offer_facts
from scripts.seed_web_ui_info_translations import CATEGORY, TRANSLATIONS as INFO
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from shared.water_facts import MINERAL_PROFILE
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    flat,
    get_page,
    jsonld,
    offer_skus,
    seed_copy,
    shipped,
)
from tests.integration.retired_phrases import find_retired

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_info_company_test_bot"
LAB_EMAIL = "lab@info-company.test"
COMPANY_EMAIL = "hello@info-company.test"
PATHS = {
    "about": "/about",
    "sources": "/about/sources",
    "quality": "/quality-standards",
    "filtration": "/process/11-step-filtration",
}
STAGES, FAQS, MINERAL_ROWS = 11, 10, 9


@pytest.fixture
def info_copy(app, db, monkeypatch):
    """The storefront rows the backend seed ships (landing.*), the shell's rows and this group's rows, plus a known bot
    link and known addresses."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    monkeypatch.setitem(app.config, "MAIL_SUPPORT_EMAIL", LAB_EMAIL)
    monkeypatch.setitem(app.config, "COMPANY_EMAIL", COMPANY_EMAIL)
    seed_copy(db)
    present = set(db.session.query(Translation.key, Translation.language).all())
    for rows_by_lang in (SHELL, INFO):
        for language, rows in rows_by_lang.items():
            for key, value in rows.items():
                if (key, language) not in present:
                    db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _copy(lang, key, **kwargs):
    """The text a seed ships for `key` in `lang`, formatted like the `t` filter formats it."""
    for rows in (INFO, SHELL):
        if key in rows[lang]:
            return rows[lang][key].format(**kwargs) if kwargs else rows[lang][key]
    return shipped(key, lang)


def _main(soup):
    return soup.select_one("main")


# --- The copy ------------------------------------------------------------------------------------------------------

PHONE_WORDS = re.compile(r"\+998|\btel:|\bcall us\b|\bphone us\b|позвон|звонит|qo‘ng‘iroq|qo'ng'iroq", re.IGNORECASE)
# Claims the owner removed (decisions 2026-11-13): no history, no leadership, no country-wide coverage, no legal entity.
UNBACKED = re.compile(
    r"10\+|over a decade|более десяти лет|o‘n yildan|leading|ведущ|yetakchi|across Uzbekistan|по всему Узбекистану"
    r"|O‘zbekiston bo‘ylab|Blue Stream|24/7|money-back|guarantee",
    re.IGNORECASE,
)


@pytest.mark.unit
@pytest.mark.parametrize("lang", LANGS)
def test_the_seed_ships_every_key_in_every_language_with_clean_copy(lang):
    assert set(INFO[lang]) == set(INFO["en"])
    for key, value in INFO[lang].items():
        assert key.startswith("web.info."), key
        assert value.strip(), key
        assert "**" not in value, key  # Markdown never reaches the page
        assert "@aqua-element.uz" not in value, key  # the address comes from config
        assert find_retired(flat(value)) == [], key
        assert not PHONE_WORDS.search(value), key
        assert not UNBACKED.search(value), key
        if lang == "uz":
            assert "'" not in value and "ʻ" not in value and "ʼ" not in value, key


# --- Every page ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", sorted(PATHS))
def test_every_page_reads_in_the_visitors_language(app, db, offer_skus, info_copy, name, lang):
    soup = get_page(app, PATHS[name], lang)
    html = str(soup)
    assert "web.info." not in html and "web.shell." not in html, "a key reached the page untranslated"
    assert soup.html["lang"] == lang
    main = _main(soup)
    assert main.select('a[href^="tel:"]') == []
    assert soup.select_one('link[href*="css/pages/info.css?v="]') is not None
    h1s = main.select("h1")
    assert len(h1s) == 1 and " - " not in flat(h1s[0].get_text(" "))
    assert not UNBACKED.search(flat(main.get_text(" ")))
    # The page ends with a way to order or ask in the bot.
    assert main.select_one(f'a[href="{BOT_URL}"]') is not None
    # No scroll-in animation classes from the old theme.
    assert not main.select(".wow, .fadeInUp")


# --- /about --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_about_shows_the_real_bottle_and_only_documented_facts(app, db, offer_skus, info_copy, lang):
    offer_skus.p19.images = ["https://cdn.example.test/ae-19.png"]
    db.session.commit()
    soup = get_page(app, "/about", lang)
    main = _main(soup)
    assert flat(main.select_one(".page-title h1").get_text(" ")) == flat(_copy(lang, "web.info.about.title"))
    img = main.select_one(".info-intro__media img")
    assert img["src"] == "https://cdn.example.test/ae-19.png"
    assert "about-2.png" not in str(main) and "about-3.png" not in str(main) and "images/team" not in str(main)
    # The facts strip prints the stage count and the mineral table's own TDS and pH values.
    facts = [flat(dd.get_text(" ")) for dd in main.select(".info-facts dd")]
    assert facts == [
        flat(_copy(lang, "web.info.fact.depth")),
        str(STAGES),
        flat(_copy(lang, "web.info.mineral.row_1.value")),
        flat(_copy(lang, "web.info.mineral.row_2.value")),
    ]
    # Coverage is the delivery-zone SSOT's summary, linked to the map.
    from shared.constants import get_delivery_coverage

    coverage = main.select_one(".info-ticks a")
    assert flat(coverage.get_text(" ")) == flat(get_delivery_coverage(lang)["summary"]) and "/coverage" in coverage["href"]
    # Each fact links to where it is documented.
    hrefs = [a["href"] for a in main.select(".info-links a.info-link")]
    for path in ("/process/11-step-filtration", "/about/sources", "/quality-standards", "/coverage"):
        assert any(path in href for href in hrefs), path


@pytest.mark.parametrize("lang", LANGS)
def test_the_order_band_opens_each_product_page_by_its_live_size(app, db, offer_skus, info_copy, lang):
    """The filtration CTA named two bottles and sent both to /shop; the band now links each product's own page."""
    facts = get_public_offer_facts()
    for path in ("/about", "/about/sources", "/process/11-step-filtration"):
        band = _main(get_page(app, path, lang)).select_one(".info-band")
        products = band.select('a[data-info-cta="product"]')
        # Each product's own page, in the visitor's language (the raw path dropped ?lang: a shared link or a crawler
        # landed on the Uzbek page).
        assert [urlpath(a["href"]) for a in products] == [p["path"] for p in facts["products"]], path
        assert all(in_language(a["href"], lang) for a in products), (path, [a["href"] for a in products])
        assert [flat(a.get_text(" ")) for a in products] == [
            flat(_copy(lang, "web.info.cta.product", size=p["size_text"][lang])) for p in facts["products"]
        ], path
        assert band.select_one(f'a[data-info-cta="bot"][href="{BOT_URL}"]') is not None


# --- /about/sources ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_sources_has_no_markdown_a_working_contents_list_and_the_configured_address(
    app, db, offer_skus, info_copy, lang
):
    soup = get_page(app, "/about/sources", lang)
    main = _main(soup)
    text = flat(main.get_text(" "))
    assert "**" not in text
    # Each audience label is its own strong term, followed by its text.
    labels = [flat(dt.get_text(" ")) for dt in main.select(".info-audience dt")]
    assert labels == [flat(_copy(lang, f"web.info.sources.use_{who}_label")) for who in ("journalist", "ai", "customer")]
    # Every contents link lands on a section of the page; the contents list is an aside, never a <nav> (agents drop it).
    anchors = [a["href"] for a in main.select("aside.info-toc a")]
    assert anchors and all(main.select_one(href) is not None for href in anchors)
    # Below 992px the aside is hidden: the same list, folded into a <details> above the lead.
    folded = main.select_one("details.info-toc-m")
    assert flat(folded.select_one("summary").get_text(" ")) == flat(_copy(lang, "web.info.toc"))
    assert [a["href"] for a in folded.select("a")] == anchors
    assert folded.find_next_sibling(class_="info-doc__lead") is not None
    assert main.select_one("nav") is None
    # Lab reports: the configured address as a mailto link, never the typed one.
    assert main.select(f'a[href="mailto:{LAB_EMAIL}"]')
    assert "support@aqua-element.uz" not in str(main)
    # The feed, the pillar page and the offer facts are still there.
    assert main.select_one('a[href$="/api/public/products.json"]') is not None
    assert main.select_one('a[href*="/process/11-step-filtration"]') is not None
    assert main.select_one("[data-offer-facts]") is not None
    article = jsonld(soup, "Article")[0]
    assert article["headline"] == _copy(lang, "web.info.sources.title")
    crumbs = jsonld(soup, "BreadcrumbList")[0]["itemListElement"]
    assert crumbs[-1]["name"] == _copy(lang, "web.info.sources.crumb")


@pytest.mark.parametrize("lang", LANGS)
def test_sources_lists_and_names_only_the_identity_records_that_are_configured(
    app, db, offer_skus, info_copy, monkeypatch, lang
):
    """The lead and the meta description promised Google and Yandex records the page never showed (neither URL is
    set in .env or .env.production): the lead names only the records the page lists."""

    def sources():
        soup = get_page(app, "/about/sources", lang)
        main = _main(soup)
        lead = flat(main.select_one(".info-doc__lead").get_text(" "))
        return soup, main, lead

    monkeypatch.setitem(app.config, "COMPANY_WIKIDATA_ID", "")
    monkeypatch.setitem(app.config, "COMPANY_GOOGLE_BUSINESS_URL", "")
    monkeypatch.setitem(app.config, "COMPANY_YANDEX_BUSINESS_URL", "")
    soup, main, lead = sources()
    assert main.select_one("#identity") is None and main.select_one('aside a[href="#identity"]') is None
    assert flat(_copy(lang, "web.info.sources.lead_2_own")) in lead
    for name in ("Wikidata", "Google", "Yandex"):
        assert name not in flat(main.get_text(" ")), name
        assert name not in soup.select_one('meta[name="description"]')["content"], name

    monkeypatch.setitem(app.config, "COMPANY_WIKIDATA_ID", "Q424242")
    soup, main, lead = sources()
    assert [a["href"] for a in main.select("#identity a.info-ref")] == ["https://www.wikidata.org/wiki/Q424242"]
    assert main.select_one('aside a[href="#identity"]') is not None
    assert flat(_copy(lang, "web.info.sources.lead_2", records="Wikidata")) in lead
    assert "Google" not in flat(main.get_text(" ")) and "Yandex" not in flat(main.get_text(" "))

    monkeypatch.setitem(app.config, "COMPANY_GOOGLE_BUSINESS_URL", "https://maps.example.test/g")
    monkeypatch.setitem(app.config, "COMPANY_YANDEX_BUSINESS_URL", "https://maps.example.test/y")
    soup, main, lead = sources()
    assert len(main.select("#identity a.info-ref")) == 3
    assert flat(_copy(lang, "web.info.sources.lead_2", records="Wikidata, Google, Yandex")) in lead


# --- /process/11-step-filtration -----------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_filtration_stages_and_faq_are_one_list_with_their_json_ld(app, db, offer_skus, info_copy, lang):
    soup = get_page(app, "/process/11-step-filtration", lang)
    main = _main(soup)
    names = [_copy(lang, f"web.info.filtration.step_{n}.name") for n in range(1, STAGES + 1)]
    assert [flat(h.get_text(" ")) for h in main.select("ol.steps-list > li h3")] == [flat(n) for n in names]
    # The label's word order is the language's: "Stage 1", "Этап 1", "1-bosqich" (it printed "Bosqich 1").
    labels = [flat(s.get_text(" ")) for s in main.select("ol.steps-list > li .info-stages__label")]
    assert labels == [_copy(lang, "web.info.filtration.step_label", n=n) for n in range(1, STAGES + 1)]
    if lang == "uz":
        assert labels[0] == "1-bosqich"
    (howto,) = jsonld(soup, "HowTo")
    assert "totalTime" not in howto  # it said the process takes 11 seconds
    assert [s["name"] for s in howto["step"]] == names
    assert [s["position"] for s in howto["step"]] == list(range(1, STAGES + 1))

    questions = [flat(h.get_text(" ")) for h in main.select(".filtration-faq h3")]
    answers = [flat(p.get_text(" ")) for p in main.select(".filtration-faq p")]
    (faq_page,) = jsonld(soup, "FAQPage")
    assert questions == [flat(e["name"]) for e in faq_page["mainEntity"]]
    assert answers == [flat(e["acceptedAnswer"]["text"]) for e in faq_page["mainEntity"]]
    assert len(questions) == FAQS
    assert answers[-1] == flat(_copy(lang, "web.info.filtration.faq_10.a", email=LAB_EMAIL))
    (speakable,) = [item for item in jsonld(soup, "WebPage") if "speakable" in item]
    assert speakable["speakable"]["cssSelector"] == [".filtration-faq h3", ".filtration-faq p"]


@pytest.mark.parametrize("lang", LANGS)
def test_the_mineral_table_is_one_source_on_both_pages(app, db, offer_skus, info_copy, lang):
    def rows(path):
        table = _main(get_page(app, path, lang)).select_one("table.info-minerals")
        return [(flat(r.select_one("th").get_text(" ")), flat(r.select_one("td").get_text(" "))) for r in table.select("tbody tr")]

    expected = [
        (flat(_copy(lang, f"web.info.mineral.row_{n}.param")), flat(_copy(lang, f"web.info.mineral.row_{n}.value")))
        for n in range(1, MINERAL_ROWS + 1)
    ]
    assert rows("/process/11-step-filtration") == expected == rows("/about/sources")


def test_the_comparison_table_labels_every_cell_for_phones(app, db, offer_skus, info_copy):
    table = _main(get_page(app, "/process/11-step-filtration", "ru")).select_one("table.compare-table")
    columns = [flat(th.get_text(" ")) for th in table.select("thead th")]
    for row in table.select("tbody tr"):
        assert [cell["data-label"] for cell in row.select("th, td")] == columns
    assert "is-highlighted" in table.select("tbody tr")[0]["class"]


# --- /quality-standards --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_quality_is_translated_and_links_keep_the_language(app, db, offer_skus, info_copy, lang):
    soup = get_page(app, "/quality-standards", lang)
    main = _main(soup)
    title = flat(_copy(lang, "web.shell.footer.quality"))
    assert flat(main.select_one(".page-title h1").get_text(" ")) == title
    assert flat(soup.title.get_text()).startswith(f"{title} — ")
    assert any(item.get("name") == _copy(lang, "web.shell.footer.quality") for item in jsonld(soup, "WebPage"))
    assert soup.select_one('meta[name="description"]')["content"] == _copy(lang, "web.info.quality.meta")
    if lang != "en":
        for english in ("Quality Focus", "Storage and Handling", "Verifiable Quality Evidence", "Delivery Policy"):
            assert english not in flat(main.get_text(" "))
    # The minerals fact reads the mineral table's own values.
    assert flat(
        _copy(
            lang,
            "web.info.quality.fact_minerals",
            tds=_copy(lang, "web.info.mineral.row_1.value"),
            ph=_copy(lang, "web.info.mineral.row_2.value"),
        )
    ) in flat(main.get_text(" "))
    hrefs = [a["href"] for a in main.select("a")]
    for path in ("/process/11-step-filtration", "/delivery-policy", "/pricing-policy", "/refund-policy", "/water-delivery-faq"):
        links = [h for h in hrefs if urlpath(h) == path]
        assert links, path
        if lang != "uz":  # uz is the default language: its links carry no ?lang=
            assert all(f"lang={lang}" in h for h in links), (path, links)
    assert main.select(f'a[href="mailto:{LAB_EMAIL}"]') and main.select(f'a[href="mailto:{COMPANY_EMAIL}"]')


def urlpath(href):
    return urlsplit(href).path


def in_language(href, lang):
    """The link carries the visitor's language: ?lang=<lang>, none for uz (the default language)."""
    return parse_qs(urlsplit(href).query).get("lang", []) == ([] if lang == "uz" else [lang])


# --- The water facts, held to the published values (review finding info-r07) ----------------------------------------

NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@pytest.mark.unit
@pytest.mark.parametrize("lang", LANGS)
def test_every_language_prints_the_published_mineral_numbers(lang):
    """tests/integration/test_water_facts_single_source.py holds the English rows to shared/water_facts.py; the ru and
    uz rows are typed too, so their numbers are held to the same values here."""
    for n, row in enumerate(MINERAL_PROFILE, 1):
        value = INFO[lang][f"web.info.mineral.row_{n}.value"]
        numbers = [float(x.replace(",", ".")) for x in NUMBER.findall(value)]
        assert numbers == [v for v in (row.low, row.high) if v is not None], (lang, n, value)


def test_every_language_prints_the_well_depth_the_feed_publishes(app, db, offer_skus, info_copy):
    depth = app.test_client().get("/api/public/products.json").get_json()["sourceWater"]["wellDepthMetres"]
    for lang in LANGS:
        for key in ("web.info.fact.depth", "web.info.filtration.source.depth"):
            assert NUMBER.findall(INFO[lang][key]) == [str(depth)], (lang, key)


@pytest.mark.unit
@pytest.mark.parametrize(
    "lang, uv, minerals, ozone",
    [("en", "UV", "calcium", "ozone"), ("ru", "ультрафиолет", "кальций", "озон"), ("uz", "ultrabinafsha", "kalsiy", "ozon")],
)
def test_about_names_the_stages_in_the_documented_order(lang, uv, minerals, ozone):
    """The filtration page's order: UV (stage 9), mineralisation (10), ozone last (11); /about had ozone before the
    minerals."""
    text = INFO[lang]["web.info.about.step_2.text"]
    assert -1 < text.index(uv) < text.index(minerals) < text.index(ozone), text


@pytest.mark.unit
def test_the_comparison_table_spells_out_its_terms():
    for lang in ("ru", "uz"):
        cells = " ".join(v for k, v in INFO[lang].items() if k.startswith("web.info.filtration.compare."))
        for abbreviation in ("немин.", "ОО ", "ОО)", "OO dan", "OO "):
            assert abbreviation not in cells, (lang, abbreviation)
