"""The one list of public page paths: the sitemaps and the IndexNow submission read it."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta

from flask import has_request_context, url_for

from business_app.models.blog import BlogPost, BlogStatus
from business_app.models.product import Product
from business_app.services.public_urls_service import (
    STATIC_PUBLIC_PATHS,
    blog_post_path,
    indexnow_urls,
    public_blog_paths,
    public_product_paths,
    recently_deactivated_product_paths,
)
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` and `blog_post` are fixtures
    blog_post,
    offer_skus,
)

STATIC = [
    "/",
    "/shop",
    "/subscriptions",
    "/services",
    "/coverage",
    "/loyalty-guide",
    "/about",
    "/contact",
    "/gallery",
    "/blog",
    "/terms",
    "/privacy",
    "/delivery-policy",
    "/pricing-policy",
    "/refund-policy",
    "/quality-standards",
    "/water-delivery-faq",
    "/process/11-step-filtration",
    "/about/sources",
]
_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _entries(app, path):
    response = app.test_client().get(path)
    assert response.status_code == 200
    root = ET.fromstring(response.data)
    return [
        tuple(url.findtext(f"{_NS}{tag}") for tag in ("loc", "lastmod", "changefreq", "priority"))
        for url in root.findall(f"{_NS}url")
    ]


def test_the_three_sitemaps_list_what_they_listed_before(app, db, monkeypatch, offer_skus, blog_post):
    monkeypatch.setitem(app.config, "SEO_STATIC_SITEMAP_LASTMOD", "2026-11-01")
    offer_skus.p10.slug = "aqua-element-10l"
    db.session.commit()
    static = _entries(app, "/sitemap-static.xml")
    assert [entry[0] for entry in static] == [f"http://localhost{path}" for path in STATIC]
    assert static[0] == ("http://localhost/", "2026-11-01", "weekly", "0.8")
    assert static[1] == ("http://localhost/shop", "2026-11-01", "weekly", "0.6")
    assert sorted(_entries(app, "/sitemap-products.xml")) == sorted(
        [  # today's query has no ORDER BY
            (
                f"http://localhost/product/{offer_skus.p19.id}",
                offer_skus.p19.updated_at.date().isoformat(),
                "weekly",
                "0.7",
            ),
            ("http://localhost/product/aqua-element-10l", offer_skus.p10.updated_at.date().isoformat(), "weekly", "0.7"),
        ]
    )
    assert _entries(app, "/sitemap-blog.xml") == [
        (f"http://localhost/blog/{blog_post.slug}", blog_post.published_at.date().isoformat(), "monthly", "0.6")
    ]


def test_paths_equal_what_url_for_builds(app, db, offer_skus, blog_post):
    offer_skus.p10.slug = "вода 10л"  # free text on the admin API: needs percent-encoding, the space too
    blog_post.slug = "вода-19-литров"  # a slug that needs percent-encoding
    db.session.commit()
    listed = public_product_paths()
    assert [product.id for _path, product in listed] == sorted([offer_skus.p19.id, offer_skus.p10.id])
    with app.test_request_context():
        assert {product.id: path for path, product in listed} == {
            offer_skus.p19.id: url_for("frontend.product_detail", product_id=offer_skus.p19.id),
            offer_skus.p10.id: url_for("frontend.product_detail_slug", slug="вода 10л"),
        }
        assert blog_post_path(blog_post) == url_for("frontend.blog_detail", slug=blog_post.slug)
    assert public_blog_paths() == [(blog_post_path(blog_post), blog_post)]


def test_indexnow_urls_are_every_page_in_three_languages_plus_llms_txt(app, db, monkeypatch, offer_skus, blog_post):
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", "https://aqua-element.uz/")  # trailing slash on purpose
    urls, site = indexnow_urls(), "https://aqua-element.uz"
    assert urls[:3] == [f"{site}/", f"{site}/?lang=ru", f"{site}/?lang=en"]
    for page in ("/shop", f"/product/{offer_skus.p19.id}", f"/blog/{blog_post.slug}"):
        assert {f"{site}{page}", f"{site}{page}?lang=ru", f"{site}{page}?lang=en"} <= set(urls)
    assert urls[-1] == f"{site}/llms.txt" and urls.count(f"{site}/llms.txt") == 1
    assert not any("llms.txt?" in url or "lang=uz" in url or ".uz//" in url for url in urls)
    assert len(urls) == len(set(urls)) == 3 * (len(STATIC_PUBLIC_PATHS) + 2 + 1) + 1


def test_inactive_products_and_unpublished_or_future_posts_are_not_listed(app, db, offer_skus, blog_post):
    offer_skus.p10.is_active = False
    db.session.add(BlogPost(title="Draft", slug="draft-post", content="x", status=BlogStatus.DRAFT))
    db.session.add(
        BlogPost(
            title="Future",
            slug="future-post",
            content="x",
            status=BlogStatus.PUBLISHED,
            published_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    db.session.commit()
    assert [p.id for _path, p in public_product_paths()] == [offer_skus.p19.id]
    assert [post.id for _path, post in public_blog_paths()] == [blog_post.id]


def test_a_product_deactivated_today_is_recent_and_one_deactivated_last_week_is_not(app, db, offer_skus):
    now = datetime.now(UTC)
    offer_skus.p19.is_active = False
    offer_skus.p10.is_active = False
    db.session.commit()
    Product.query.filter_by(id=offer_skus.p10.id).update({"updated_at": now - timedelta(days=8)})
    db.session.commit()
    assert recently_deactivated_product_paths(now - timedelta(days=1)) == [f"/product/{offer_skus.p19.id}"]


def test_the_helper_works_with_no_request(app, db, offer_skus, blog_post):
    assert not has_request_context()
    assert indexnow_urls()


def test_the_static_tuple_is_the_sitemaps_list():
    assert list(STATIC_PUBLIC_PATHS) == STATIC
