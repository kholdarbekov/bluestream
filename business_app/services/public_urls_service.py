"""The one list of public page paths: the sitemaps and the IndexNow submission both read it.

A path is the page's address on the site ("/shop", "/product/12", "/blog/<slug>"); an absolute URL is built from
it with `website_url`. Plain functions over the models and the config, so a Celery task (an app context, no
request) can call them. This module never touches `request` or `url_for`.
"""

from datetime import UTC, datetime
from urllib.parse import quote

from flask import current_app
from sqlalchemy import desc

from business_app.models.blog import BlogPost, BlogStatus
from business_app.models.product import Product
from business_app.services.customer_links import website_url
from business_app.services.public_offer_service import LANGUAGES, SLUG_SAFE, product_public_path

STATIC_PUBLIC_PATHS = (
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
)
LLMS_TXT_PATH = "/llms.txt"


def blog_post_path(post) -> str:
    """The post's public page; `SLUG_SAFE` (beside `product_public_path`) makes it equal `url_for` for any slug."""
    return f"/blog/{quote(post.slug, safe=SLUG_SAFE)}"


def public_product_paths() -> list[tuple[str, Product]]:
    """Active products with their public paths, ordered by id."""
    products = Product.query.filter_by(is_active=True).order_by(Product.id).all()
    return [(product_public_path(product), product) for product in products]


def public_blog_paths() -> list[tuple[str, BlogPost]]:
    """Published posts that are not dated in the future, newest first."""
    posts = (
        BlogPost.query.filter(BlogPost.status == BlogStatus.PUBLISHED, BlogPost.published_at <= datetime.now(UTC))
        .order_by(desc(BlogPost.published_at))
        .all()
    )
    return [(blog_post_path(post), post) for post in posts]


def language_urls(path) -> list[str]:
    """The page at `path` as its absolute URLs, one per site language: the bare URL (the default language), then
    `?lang=` for each other language. The site declares each as its own self-canonical page."""
    url = website_url(path)
    default_language = current_app.config["DEFAULT_LANGUAGE"]
    return [url] + [f"{url}?lang={language}" for language in LANGUAGES if language != default_language]


def indexnow_urls() -> list[str]:
    """Every public page in every site language (`language_urls`), then `/llms.txt` once with no language variant."""
    paths = [
        *STATIC_PUBLIC_PATHS,
        *(path for path, _product in public_product_paths()),
        *(path for path, _post in public_blog_paths()),
    ]
    urls = [url for path in paths for url in language_urls(path)]
    urls.append(website_url(LLMS_TXT_PATH))
    return urls


def recently_deactivated_product_paths(since: datetime) -> list[str]:
    """Paths of products switched off at or after `since` (every deactivation moves `updated_at`)."""
    products = (
        Product.query.filter(Product.is_active.is_(False), Product.updated_at >= since).order_by(Product.id).all()
    )
    return [product_public_path(product) for product in products]
