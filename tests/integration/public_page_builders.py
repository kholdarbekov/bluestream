"""Builders for the public price and delivery pages (plan 2026-11-12, "live price and same-day delivery visibility").

Every test of a public page GETs the real page in a site language, signed out, with the rows the seed ships in the
database. Rows come from `BACKEND_TRANSLATIONS`, never from a copy kept in a test. The two SKUs below carry prices
no production row has, so a price typed into a template, a seed value or a blog module cannot pass a test.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup

from business_app.models.blog import BlogPost, BlogStatus
from business_app.models.product import Product, ProductCategory
from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

LANGS = ("uz", "ru", "en")
PREFIXES = ("web.display_labels", "landing.")
# The pages that print the offer block (Task 5 starts the list; later tasks append).
BLOCK_PAGES: list[str] = ["/", "/shop", "/blog/{slug}", "/process/11-step-filtration", "/about/sources", "/product/{id}", "/pricing-policy", "/delivery-policy"]

# `shop.html` %-formats this key, and no seed ships it: without the row `/shop` raises TypeError.
_RESULTS_COUNT_KEY = "landing.shop.results_count"
_RESULTS_COUNT_VALUE = "%s-%s of %s"


def flat(text: str) -> str:
    """The text as a visitor reads it: NBSP, tabs and newlines folded into single plain spaces."""
    return " ".join(text.split())


def shipped(key: str, lang: str) -> str:
    """The text the seed ships for `key`; a key the seed does not own fails here, not in a missing-row render."""
    row = BACKEND_TRANSLATIONS.get(key)
    assert row is not None, f"{key} is DB-only: the seed must ship it, or a deploy never replaces it"
    return row.get(lang) or row["en"]


def seed_copy(db, prefixes: tuple[str, ...] = PREFIXES) -> None:
    """Insert the seed's rows whose key starts with one of `prefixes`, in uz, ru and en, plus the `/shop` results
    count. A `(key, language)` pair already in the database is left as it is, so a second call adds nothing."""
    present = set(db.session.query(Translation.key, Translation.language).all())

    def add(key: str, language: str, value: str, category: str) -> None:
        if (key, language) in present:
            return
        db.session.add(Translation(key=key, language=language, value=value, category=category, is_active=True))
        present.add((key, language))

    for key, row in BACKEND_TRANSLATIONS.items():
        if key.startswith(prefixes):
            for language in LANGS:
                add(key, language, row.get(language) or row["en"], _category_for(key))
    for language in LANGS:
        add(_RESULTS_COUNT_KEY, language, _RESULTS_COUNT_VALUE, "landing")
    db.session.commit()


def get_page(app, path: str, lang: str) -> BeautifulSoup:
    """GET `path` in `lang` as a signed-out visitor does, on a fresh client, and parse it."""
    app.jinja_env.cache.clear()
    separator = "&" if "?" in path else "?"
    # `?lang=` stores the language in the session and redirects: follow it on ONE fresh client.
    response = app.test_client().get(f"{path}{separator}lang={lang}", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html[:500]
    return BeautifulSoup(html, "html.parser")


def page(app, db, path: str, lang: str, prefixes: tuple[str, ...] = PREFIXES) -> BeautifulSoup:
    """`get_page` with the seed's rows for `prefixes` in the database."""
    seed_copy(db, prefixes)
    return get_page(app, path, lang)


def get_once(app, url: str):
    """The answer to a signed-out GET of `url` on a fresh client, with no redirect followed, so a redirect is seen as
    one. `url` may be absolute (`language_urls` builds those: the default language bare, the others with `?lang=`,
    which the site answers without a language redirect); only its path and query are sent."""
    parts = urlsplit(url)
    return app.test_client().get(f"{parts.path}?{parts.query}" if parts.query else parts.path)


def set_price(db, product, base, discount=None) -> None:
    product.base_price = base
    product.discount_price = discount
    db.session.commit()


def jsonld(soup, type_name: str) -> list[dict]:
    """Every JSON-LD object on the page whose `@type` is `type_name`. A block that does not parse is skipped, and a
    block holding a top-level list counts each of its objects."""
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == type_name:
                found.append(item)
    return found


def faq_answer(soup, question: str) -> str:
    """The visible answer to `question` in the page's `.shop-faq-list`."""
    asked = []
    for item in soup.select(".shop-faq-list li"):
        heading = item.select_one("h3")
        if heading is None:
            continue
        text = flat(heading.get_text(" "))
        if text == flat(question):
            return flat(item.select_one("p").get_text(" "))
        asked.append(text)
    raise AssertionError(f"no visible FAQ question {question!r}; the page asks {asked}")


def faqpage_jsonld_answer(soup, question: str) -> str:
    """The FAQPage JSON-LD answer to `question`."""
    asked = []
    for faq_page in jsonld(soup, "FAQPage"):
        for entry in faq_page.get("mainEntity", []):
            name = flat(entry.get("name", ""))
            if name == flat(question):
                return flat(entry["acceptedAnswer"]["text"])
            asked.append(name)
    raise AssertionError(f"no FAQPage question {question!r}; the JSON-LD asks {asked}")


@pytest.fixture
def offer_skus(db):
    """The two bottle SKUs, at prices no production row has. No description of any kind, so a page can only show
    what the offer facts give it."""
    category = ProductCategory(name="Water", is_active=True)
    db.session.add(category)
    db.session.flush()
    common = dict(
        category_id=category.id,
        is_active=True,
        is_featured=True,
        slug=None,
        stock_quantity=100,
        volume_unit="L",
    )
    p19 = Product(
        name="Aqua Element 18.9 L",
        sku="AE-19",
        size="19L",
        volume=18.9,
        base_price=Decimal("23450.00"),
        min_order_quantity=2,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal("1.00"),
        **common,
    )
    p10 = Product(
        name="Aqua Element 10 L",
        sku="AE-10",
        size="10L",
        volume=10.0,
        base_price=Decimal("12340.00"),
        min_order_quantity=4,
        tracks_returnable_bottles=False,
        returnable_bottles_per_unit=Decimal("0.00"),
        **common,
    )
    db.session.add_all([p19, p10])
    db.session.commit()
    return SimpleNamespace(p19=p19, p10=p10)


@pytest.fixture
def blog_post(db):
    """One published post, titled and written in uz, ru and en."""
    post = BlogPost(
        title="Suv haqida",
        slug=f"suv-haqida-{uuid4().hex[:8]}",
        content="<p>Matn</p>",
        status=BlogStatus.PUBLISHED,
        published_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.session.add(post)
    # The translation keys are built from the post's id.
    db.session.flush()
    post.set_translated("title", "О воде", "ru")
    post.set_translated("title", "About water", "en")
    post.set_translated("content", "<p>Текст</p>", "ru")
    post.set_translated("content", "<p>Text</p>", "en")
    db.session.commit()
    return post
