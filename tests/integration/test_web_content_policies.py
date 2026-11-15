"""The policy and help pages after the redesign (website redesign step 3, group "content", unit "policies", 2026-11-14):
/terms, /privacy, /refund-policy, /pricing-policy, /delivery-policy and /water-delivery-faq.

Each page is fetched as a signed-out visitor fetches it, in uz, ru and en, with the rows the seeds ship
(scripts/seed_web_ui_content_translations.py, scripts/seed_web_ui_shell_translations.py and the backend seed's landing.*
rows). The copy is read from the seeds, never typed here.

Fixed on these pages, and pinned here:
- the H1 and the last breadcrumb printed "<title> - Aqua Element": base.html sets `page_title` from the title block before
  the content renders, so a page that named its own title `page_title` lost it;
- the legal documents were English on ru/uz and ended with a tel: link; now every section is a key, the contact is the
  Telegram bot and email, and a document of four sections or more opens with a table of contents whose links land on
  its numbered headings;
- the FAQ is a native <details> accordion: no theme accordion classes (the theme's script.js binds .accordion-box
  .acc-btn, which double-toggled the /subscriptions FAQ), the visible list and the FAQPage JSON-LD are one list.
"""

import re
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from flask import render_template_string

from business_app.models.translation import Translation
from scripts.seed_web_ui_content_translations import CATEGORY, TRANSLATIONS as CONTENT
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
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
BOT_URL = "https://t.me/aqua_content_policies_test_bot"
EMAIL = "help@content-policies.test"

# page -> (path, the key of its H1, the policy-nav entry it marks as current)
DOCUMENTS = {
    "terms": ("/terms", "web.content.terms.title", "terms"),
    "privacy": ("/privacy", "web.content.privacy.title", "privacy"),
    "refund": ("/refund-policy", "web.shell.footer.refund_policy", "refund"),
}
NAV = [  # the policy row, in order: (path, label key)
    ("/delivery-policy", "web.shell.footer.delivery_policy"),
    ("/pricing-policy", "landing.pricing_policy.title"),
    ("/refund-policy", "web.shell.footer.refund_policy"),
    ("/terms", "web.content.terms.title"),
    ("/privacy", "web.content.privacy.title"),
    ("/water-delivery-faq", "web.shell.footer.faq"),
]
SECTIONS = {
    "terms": ["acceptance", "account", "orders", "pricing-and-payments", "delivery", "returnable-bottles", "refunds",
              "acceptable-use", "intellectual-property", "liability", "updates", "contact"],
    "privacy": ["information-we-collect", "how-we-use-information", "payment-data", "sharing", "retention", "security",
                "cookies", "your-rights", "updates", "contact"],
    "refund": ["eligibility", "report-an-issue", "review", "timing", "contact"],
}
ALL_PATHS = ["/terms", "/privacy", "/refund-policy", "/pricing-policy", "/delivery-policy", "/water-delivery-faq"]


@pytest.fixture
def content_copy(app, db, monkeypatch):
    """The storefront rows the backend seed ships (landing.*, the money labels), the shell's rows and this group's rows,
    and a known bot link and email."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    monkeypatch.setitem(app.config, "COMPANY_EMAIL", EMAIL)
    seed_copy(db)
    present = set(db.session.query(Translation.key, Translation.language).all())
    for rows_by_lang in (SHELL, CONTENT):
        for language, rows in rows_by_lang.items():
            for key, value in rows.items():
                if (key, language) not in present:
                    db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _copy(lang, key):
    """The text a seed ships for `key` in `lang`: this group's seed, the shell's, or the backend's landing.* rows."""
    for rows in (CONTENT, SHELL):
        if key in rows[lang]:
            return rows[lang][key]
    return shipped(key, lang)


def _main(soup):
    return soup.select_one("main")


# --- The copy ------------------------------------------------------------------------------------------------------


PHONE_WORDS = re.compile(r"\+998|\btel:|\bcall us\b|\bphone us\b|позвон|звонит|qo‘ng‘iroq|qo'ng'iroq", re.IGNORECASE)


@pytest.mark.unit
@pytest.mark.parametrize("lang", LANGS)
def test_the_groups_copy_never_asks_for_a_call_and_carries_no_retired_delivery_wording(lang):
    """The policy copy left the backend seed for this group's seed, out of reach of the site-wide retired-wording
    scan (it reads the backend seed): the same detector runs over it here."""
    assert set(CONTENT[lang]) == set(CONTENT["en"])
    for key, value in CONTENT[lang].items():
        assert value.strip(), key
        assert find_retired(flat(value)) == [], key
        assert not PHONE_WORDS.search(value), key
        if lang == "uz":
            assert "'" not in value and "\u02bb" not in value and "\u02bc" not in value, key


# --- Every page --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("path", ALL_PATHS)
def test_every_page_reads_in_the_visitors_language_and_never_offers_the_phone(
    app, db, offer_skus, content_copy, path, lang
):
    soup = get_page(app, path, lang)
    html = str(soup)
    assert "web.content." not in html and "web.shell." not in html, "a key reached the page untranslated"
    assert soup.html["lang"] == lang
    main = _main(soup)
    assert main.select('a[href^="tel:"]') == []
    # The content stylesheet is versioned (asset_url), so a deploy never pairs new markup with yesterday's CSS.
    assert soup.select_one('link[href*="css/pages/content.css?v="]') is not None
    # One H1, and it is the page's own title, never "<title> - Aqua Element".
    h1s = main.select("h1")
    assert len(h1s) == 1
    assert " - " not in flat(h1s[0].get_text(" "))
    assert " - " not in flat(soup.select(".page-title .bread-crumb li")[-1].get_text(" "))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("page_name", sorted(DOCUMENTS))
def test_a_policy_document_has_its_translated_title_in_the_h1_the_crumb_and_the_tab(
    app, db, content_copy, page_name, lang
):
    path, title_key, _ = DOCUMENTS[page_name]
    soup = get_page(app, path, lang)
    title = flat(_copy(lang, title_key))
    assert flat(soup.select_one(".page-title h1").get_text(" ")) == title
    assert flat(soup.select(".page-title .bread-crumb li")[-1].get_text(" ")) == title
    assert flat(soup.title.get_text()).startswith(f"{title} — ")
    assert soup.select_one('meta[name="description"]')["content"] == {
        "terms": _copy(lang, "web.content.terms.meta"),
        "privacy": _copy(lang, "web.content.privacy.meta"),
        "refund": _copy(lang, "web.content.refund.meta"),
    }[page_name]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("current", ["/terms", "/privacy", "/refund-policy", "/pricing-policy", "/delivery-policy",
                                     "/water-delivery-faq"])
def test_the_policy_row_links_every_policy_and_marks_the_page_itself(app, db, offer_skus, content_copy, current, lang):
    links = get_page(app, current, lang).select(".ct-policy-nav a")
    assert [(urlsplit(a["href"]).path, flat(a.get_text())) for a in links] == [(p, flat(_copy(lang, k))) for p, k in NAV]
    marked = [urlsplit(a["href"]).path for a in links if a.get("aria-current") == "page"]
    assert marked == [current]


# --- Legal documents ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("page_name", sorted(DOCUMENTS))
def test_every_section_is_translated_numbered_by_css_and_listed_in_the_contents(app, db, content_copy, page_name, lang):
    path, _, _ = DOCUMENTS[page_name]
    soup = get_page(app, path, lang)
    body = soup.select_one(".ct-legal__body")
    headings = body.select("h2")
    assert [h["id"] for h in headings] == SECTIONS[page_name]
    # The numbers come from a CSS counter: a heading's text is exactly its translation, with no "1." typed in.
    for heading in headings:
        assert heading.get_text(strip=True)[:1].isdigit() is False
    toc = soup.select(".ct-toc .ct-toc__list a")
    assert [a["href"] for a in toc] == [f"#{h['id']}" for h in headings]
    assert [flat(a.get_text()) for a in toc] == [flat(h.get_text(" ")) for h in headings]
    # Every heading and paragraph is the visitor's language: the seed's text for that section.
    prefix = {"terms": "web.content.terms", "privacy": "web.content.privacy", "refund": "web.content.refund"}[page_name]
    own = [h for h in headings if h["id"] not in ("returnable-bottles",)]
    for n, heading in enumerate(own, start=1):
        assert flat(heading.get_text(" ")) == flat(_copy(lang, f"{prefix}.s{n}_h"))
        assert flat(heading.find_next_sibling("p").get_text(" ")) == flat(_copy(lang, f"{prefix}.s{n}_p"))


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("page_name", sorted(DOCUMENTS))
def test_a_document_ends_with_the_bot_and_email_never_the_phone(app, db, content_copy, page_name, lang):
    soup = get_page(app, DOCUMENTS[page_name][0], lang)
    contact = soup.select_one(".ct-legal .ct-contact")
    bot = contact.select_one(f'a[href="{BOT_URL}"]')
    assert flat(bot.get_text()) == flat(_copy(lang, "web.content.contact.bot"))
    assert bot["target"] == "_blank" and "noopener" in bot["rel"]
    assert contact.select_one(f'a[href="mailto:{EMAIL}"]') is not None
    last = soup.select(".ct-legal__body h2")[-1]
    assert last["id"] == "contact"
    text = flat(last.find_next_sibling("p").get_text(" "))
    assert "+998" not in text and "tel" not in text.lower().split()


@pytest.mark.parametrize("lang", LANGS)
def test_the_privacy_reminders_purpose_keeps_its_anchor_in_section_two(app, db, content_copy, lang):
    soup = get_page(app, "/privacy", lang)
    purpose = soup.select_one("p#privacy-reorder-reminders")
    assert purpose is not None
    assert purpose.find_previous_sibling("h2")["id"] == "how-we-use-information"
    assert flat(purpose.get_text(" ")) == flat(shipped("landing.privacy.reorder_reminders_purpose", lang))


@pytest.mark.parametrize("lang", LANGS)
def test_the_terms_charge_no_delivery_fee_and_the_privacy_policy_names_the_analytics_it_loads(
    app, db, content_copy, lang
):
    """Review C-10/C-11: delivery is free (offer facts), so §4 of the terms names no delivery fee; the site loads Google
    Analytics and Yandex Metrica (base.html), so the privacy policy names both where it lists who receives data (§4)
    and in its cookie section (§7), with the way to turn them off."""
    terms = flat(_main(get_page(app, "/terms", lang)).select_one("#pricing-and-payments + p").get_text(" "))
    assert not re.search(r"delivery fee|стоимость доставки|yetkazib berish narxi", terms, re.IGNORECASE)
    privacy = _main(get_page(app, "/privacy", lang))
    for anchor in ("#sharing + p", "#cookies + p"):
        text = flat(privacy.select_one(anchor).get_text(" "))
        assert "Google Analytics" in text and re.search(r"Yandex Metri|Яндекс Метрик", text), (lang, anchor)
    cookies = flat(privacy.select_one("#cookies + p").get_text(" "))
    assert re.search(r"browser settings|настройках браузера|brauzer sozlamalarida", cookies), lang


@pytest.mark.parametrize("count, has_toc", [(3, False), (4, True)])
def test_the_contents_list_appears_only_from_four_sections(app, db, count, has_toc):
    sections = [{"id": f"s{n}", "h": f"Heading {n}", "p": [f"Text {n}"]} for n in range(1, count + 1)]
    with app.test_request_context("/terms"):
        html = render_template_string(
            "{% from 'frontend/partials/content_ui.html' import legal_document with context %}"
            "{{ legal_document(sections) }}",
            sections=sections,
            current_language="en",
        )
    soup = BeautifulSoup(html, "html.parser")
    assert (soup.select_one(".ct-toc") is not None) is has_toc
    assert [h["id"] for h in soup.select(".ct-legal__body h2")] == [s["id"] for s in sections]
    assert ("ct-legal--plain" in soup.select_one(".ct-legal")["class"]) is not has_toc


# --- Pricing and delivery ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_pricing_page_keeps_the_live_offer_first_then_its_policy_then_the_club_and_the_bot(
    app, db, offer_skus, content_copy, lang
):
    soup = get_page(app, "/pricing-policy", lang)
    box = soup.select_one("section.service-details .content-box")
    assert box.find(recursive=False).has_attr("data-offer-facts")
    prose = [flat(h.get_text(" ")) for h in box.select(".ct-info__prose h2")]
    assert prose == [flat(_copy(lang, f"web.content.pricing.{k}_h")) for k in ("displayed", "discounts", "updates")]
    club = box.select_one('.ct-info__pair a.ct-linkcard[href*="/loyalty-guide"]')
    assert flat(club.select_one(".ct-linkcard__title").get_text()) == flat(_copy(lang, "web.content.pricing.club_title"))
    help_link = box.select_one(f'.ct-info__pair a[href="{BOT_URL}"]')
    assert flat(help_link.get_text()) == flat(_copy(lang, "web.content.pricing.help"))


@pytest.mark.parametrize("lang", LANGS)
def test_the_delivery_page_links_the_coverage_map_and_asks_questions_in_the_bot(app, db, offer_skus, content_copy, lang):
    soup = get_page(app, "/delivery-policy", lang)
    main = _main(soup)
    card = main.select_one('a.ct-linkcard[href*="/coverage"]')
    assert flat(card.select_one(".ct-linkcard__title").get_text()) == flat(_copy(lang, "web.content.delivery.coverage_title"))
    assert flat(card.select_one(".ct-linkcard__cta").get_text()) == flat(_copy(lang, "web.content.delivery.coverage_cta"))
    assert main.select('a[href^="mailto:"]') == []
    # The SSOT title stays whole in the H1; the crumb is the short page name.
    crumb = flat(soup.select(".page-title .bread-crumb li")[-1].get_text(" "))
    assert crumb == flat(_copy(lang, "web.shell.footer.delivery_policy"))
    assert flat(main.select_one("h1").get_text(" ")).startswith(flat(shipped("landing.delivery_page.title", lang)))


# --- FAQ ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_faq_is_a_native_accordion_and_the_jsonld_is_the_visible_list(app, db, offer_skus, content_copy, lang):
    soup = get_page(app, "/water-delivery-faq", lang)
    main = _main(soup)
    assert flat(main.select_one(".page-title h1").get_text(" ")) == flat(_copy(lang, "web.content.faq.title"))
    # No theme accordion markup: nothing for script.js to bind a second click handler to.
    assert main.select(".accordion-box, .acc-btn, .acc-content") == []
    items = main.select(".shop-faq-list > li")
    assert len(items) == 8
    for n, li in enumerate(items):
        details = li.select_one("details.ae-faq__item")
        assert details.select_one("summary.ae-faq__q > h3") is not None
        assert details.select_one(".ae-faq__a > p") is not None
        assert details.has_attr("open") is (n == 0)
    visible = [(flat(li.h3.get_text(" ")), flat(li.p.get_text(" "))) for li in items]
    (faq_page,) = jsonld(soup, "FAQPage")
    assert visible == [(flat(e["name"]), flat(e["acceptedAnswer"]["text"])) for e in faq_page["mainEntity"]]
    # The two questions this page owns read in the visitor's language.
    questions = [q for q, _ in visible]
    assert flat(_copy(lang, "web.content.faq.recurring_q")) in questions
    assert flat(_copy(lang, "web.content.faq.delayed_q")) in questions
    assert dict(visible)[flat(_copy(lang, "web.content.faq.recurring_q"))] == flat(_copy(lang, "web.content.faq.recurring_a"))
    (speakable,) = [item for item in jsonld(soup, "WebPage") if "speakable" in item]
    assert speakable["speakable"]["cssSelector"] == [".shop-faq-list h3", ".shop-faq-list p"]
    assert speakable["name"] == _copy(lang, "web.content.faq.title")
