"""The shared public-page builders work on the test database: every price page renders the two SKUs in every site
language, seeding is idempotent, and the FAQ and JSON-LD readers find what the page prints."""

import pytest

from business_app.models.translation import Translation
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` and `blog_post` are fixtures
    blog_post,
    faq_answer,
    faqpage_jsonld_answer,
    flat,
    jsonld,
    offer_skus,
    page,
    seed_copy,
    shipped,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("lang", ["uz", "ru", "en"])
def test_the_fixtures_render_every_price_page(app, db, offer_skus, blog_post, lang):
    for path in ("/", "/shop", f"/product/{offer_skus.p19.id}", f"/blog/{blog_post.slug}"):
        soup = page(app, db, path, lang)  # 200, and a second page() in one test does not raise
        assert offer_skus.p19.name in flat(soup.get_text(" ")), path


def test_seed_copy_is_idempotent(db):
    seed_copy(db)
    seed_copy(db)
    assert Translation.query.filter_by(key="landing.shop.results_count").count() == 3
    assert Translation.query.filter_by(key="web.display_labels.money", language="ru").one().value == shipped(
        "web.display_labels.money", "ru"
    )


def test_the_faq_readers_return_the_same_answer(app, db):
    soup = page(app, db, "/water-delivery-faq", "en")
    question = soup.select_one(".shop-faq-list li h3").get_text(" ")
    assert faq_answer(soup, question) == faqpage_jsonld_answer(soup, question) != ""


def test_jsonld_finds_a_block_by_type(app, db, offer_skus):
    assert [g["@type"] for g in jsonld(page(app, db, "/", "uz"), "ProductGroup")] == ["ProductGroup"]
