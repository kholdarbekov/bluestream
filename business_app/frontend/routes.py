"""Frontend routes for Blue Stream Water Business Platform."""

import json
import logging
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from xml.sax.saxutils import escape

from flask import abort, render_template, request, session, current_app, redirect, url_for, g, Response, make_response
from flask_jwt_extended import get_jwt_identity, unset_jwt_cookies, verify_jwt_in_request
from flask_limiter.errors import RateLimitExceeded
from sqlalchemy import case, desc, or_, func

from . import frontend_bp
from business_app.models.product import Product, ProductCategory
from business_app.models.order import Order
from business_app.models.user import User
from business_app.models.blog import BlogPost, BlogStatus
from business_app.models.audit import AuditSeverity
from business_app.services.email_template_service import get_email_template_service
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.public_offer_service import (
    _json_number,
    build_offer_faq,
    build_offer_jsonld,
    get_public_offer_facts,
    home_meta_description,
    price_sentence,
    public_price_range,
    size_search_terms,
    smallest_order_sentence,
)
from business_app.services.public_urls_service import STATIC_PUBLIC_PATHS, public_blog_paths, public_product_paths
from business_app.services.token_service import TokenService
from business_app.services.telegram_login_service import (
    REFUSED_BAD_HASH,
    REFUSED_DECLINED,
    REFUSED_NO_ACCOUNT,
    REFUSED_NOT_CUSTOMER,
    REFUSED_RATE_LIMITED,
    REFUSED_REPLAYED,
    REFUSED_REVOKED,
    REFUSED_STALE,
    REFUSED_UNAVAILABLE,
    TelegramLoginRefused,
    verify_login,
)
from business_app.services.reorder_reminders.tokens import (
    resubscribe_by_token,
    token_user_id,
    unsubscribe_by_token,
)
from business_app.utils.agent_discovery import build_api_catalog_linkset, build_link_header
from business_app.utils.markdown_negotiation import (
    MARKDOWN_MAX_HTML_BYTES,
    MARKDOWN_NEGOTIATION_ENDPOINTS,
    estimate_tokens,
    html_to_markdown,
    wants_markdown,
)
from business_app.utils.audit_logger import audit_login_failure, audit_login_success
from business_app.utils.auth_cookies import set_login_cookies
from business_app.utils.csrf_protection import csrf_protection
from business_app.utils.page_auth import (
    SECRET_PAGE_CACHE_CONTROL,
    customer_page_required,
    page_user_id,
    render_same_site_hop,
)
from business_app.utils.service_factory import get_auth_service, get_order_service
from business_app.utils import display_labels
from business_app.utils.helpers import get_current_language
from business_app.utils.opening_hours import office_hours_line, opening_hours_specs
from business_app.utils.validators import PasswordValidator
from business_app import db
from business_app import limiter
from shared.constants import get_delivery_coverage, get_delivery_statement, get_geoshape_polygon
from shared.payment_methods import CUSTOMER_SELECTABLE_METHODS
from shared.safe_paths import safe_next_path
from shared.telegram_login import TELEGRAM_LOGIN_PATH, is_allowed_login_target
from shared.water_facts import MINERAL_PROFILE, TREATMENT_STAGES, WELL_DEPTH_METRES, mineral_profile_feed
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


def _absolute_public_url(path):
    """Build absolute URL respecting reverse-proxy scheme headers."""
    if not path.startswith("/"):
        path = f"/{path}"
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{scheme}://{request.host}{path}"


def _current_lang_query_param():
    """Return current valid `lang` query param if present."""
    lang = request.args.get("lang")
    supported_languages = current_app.config.get("LANGUAGES", {})
    if lang and lang in supported_languages:
        return lang
    return None


def _resolved_url_language():
    """Resolve language for URL generation without relying on Accept-Language."""
    lang_from_query = _current_lang_query_param()
    if lang_from_query:
        return lang_from_query

    supported_languages = current_app.config.get("LANGUAGES", {})
    lang_from_session = session.get("language")
    if lang_from_session and lang_from_session in supported_languages:
        return lang_from_session

    return current_app.config.get("DEFAULT_LANGUAGE", "uz")


def _build_external_url(endpoint, **params):
    """Build external URL preserving active non-default language."""
    lang = _resolved_url_language()
    default_language = current_app.config.get("DEFAULT_LANGUAGE", "uz")
    if lang and lang != default_language and "lang" not in params:
        params["lang"] = lang
    if params.get("lang") == default_language:
        params.pop("lang", None)
    return url_for(endpoint, _external=True, **params)


def _default_canonical_url():
    """Build a default canonical URL for the current request path."""
    canonical = _absolute_public_url(request.path)
    lang = _resolved_url_language()
    default_language = current_app.config.get("DEFAULT_LANGUAGE", "uz")
    if lang and lang != default_language:
        canonical = f"{canonical}?{urlencode({'lang': lang})}"
    return canonical


def _format_lastmod(value):
    """Format datetime/date values for sitemap lastmod tags."""
    if not value:
        return None
    try:
        if hasattr(value, "date"):
            return value.date().isoformat()
        return str(value)
    except Exception:
        return None


def _as_absolute_url(url_value):
    """Convert relative URLs to absolute public URLs."""
    if not url_value:
        return None
    if url_value.startswith("http://") or url_value.startswith("https://"):
        return url_value
    path = url_value if url_value.startswith("/") else f"/{url_value}"
    return _absolute_public_url(path)


def _static_content_lastmod():
    """Best-effort lastmod for static sitemap entries."""
    configured_lastmod = current_app.config.get("SEO_STATIC_SITEMAP_LASTMOD")
    if configured_lastmod:
        return str(configured_lastmod)

    templates_root = Path(current_app.root_path) / "templates" / "frontend"
    template_candidates = [
        "index.html",
        "shop.html",
        "subscriptions.html",
        "services.html",
        "about.html",
        "contact.html",
        "gallery.html",
        "blog_list.html",
        "terms.html",
        "privacy.html",
        "delivery_policy.html",
        "pricing_policy.html",
        "refund_policy.html",
        "quality_standards.html",
        "water_delivery_faq.html",
    ]

    latest_timestamp = None
    for template_name in template_candidates:
        template_path = templates_root / template_name
        try:
            template_mtime = datetime.fromtimestamp(template_path.stat().st_mtime, UTC)
        except OSError:
            continue
        if latest_timestamp is None or template_mtime > latest_timestamp:
            latest_timestamp = template_mtime

    return _format_lastmod(latest_timestamp or datetime.now(UTC))


def _normalize_feed_gtin(value):
    """Return GTIN if it matches allowed lengths, else None."""
    if value is None:
        return None
    digits_only = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits_only) in {8, 12, 13, 14}:
        return digits_only
    return None


def _format_feed_price(value):
    """Return a Google feed-compatible price string with 2 decimals."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _render_sitemap_urlset(entries):
    """Render a sitemap <urlset> response."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        lines.append("<url>")
        lines.append(f"<loc>{escape(entry['loc'])}</loc>")
        if entry.get("lastmod"):
            lines.append(f"<lastmod>{escape(entry['lastmod'])}</lastmod>")
        if entry.get("changefreq"):
            lines.append(f"<changefreq>{escape(entry['changefreq'])}</changefreq>")
        if entry.get("priority") is not None:
            lines.append(f"<priority>{entry['priority']}</priority>")
        lines.append("</url>")
    lines.append("</urlset>")

    return Response("\n".join(lines), mimetype="application/xml")


def _render_sitemap_index(entries):
    """Render a sitemap index response."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for entry in entries:
        lines.append("<sitemap>")
        lines.append(f"<loc>{escape(entry['loc'])}</loc>")
        if entry.get("lastmod"):
            lines.append(f"<lastmod>{escape(entry['lastmod'])}</lastmod>")
        lines.append("</sitemap>")
    lines.append("</sitemapindex>")

    return Response("\n".join(lines), mimetype="application/xml")


def _offer_context(product=None):
    """The offer facts a storefront page prints, for `render_template`: `offer_facts` (the facts), `offer_by_id`
    (each listed product's facts by its id) and `offer_product` (the facts of `product`, the page's own `Product`
    row, or None without one). Read once per page, never memoised."""
    offer_facts = get_public_offer_facts()
    offer_by_id = {product_facts["id"]: product_facts for product_facts in offer_facts["products"]}
    offer_product = None
    if product is not None:
        offer_product = offer_by_id.get(product.id)
    return {"offer_facts": offer_facts, "offer_product": offer_product, "offer_by_id": offer_by_id}


def _offer_rows(offer_by_id):
    """The `Product` rows of the listed offer facts, by id: the feeds read their text from these and their price from
    the facts, so every row printed has its facts."""
    return Product.query.filter(Product.id.in_(list(offer_by_id))).order_by(Product.id).all()


def _order_channels(company_website):
    """Where an order can be placed, for both public feeds: the website and the Telegram bot. The phone is contact
    data, not an order channel (`llms.txt` names the same two)."""
    return {"web": company_website, "telegramBot": current_app.config["COMPANY_TELEGRAM_BOT_URL"]}


def _render_product_detail_page(product, language):
    """Render product detail template with consistent related products logic."""
    related_products = (
        Product.query.filter(
            Product.category_id == product.category_id, Product.id != product.id, Product.is_active == True
        )
        .limit(4)
        .all()
    )

    canonical_endpoint = "frontend.product_detail_slug" if product.slug else "frontend.product_detail"
    canonical_params = {"slug": product.slug} if product.slug else {"product_id": product.id}
    canonical_url = _build_external_url(canonical_endpoint, **canonical_params)
    product_payload = product.to_dict(language=language)
    product_images = product_payload.get("images") or []
    if isinstance(product_images, list):
        product_payload["images"] = [(_as_absolute_url(image) or image) for image in product_images]
    offer = _offer_context(product)
    # The stock answers the products API publishes (inventory.is_in_stock / is_low_stock, which /cart reads), so the
    # page never re-derives them; stock_quantity stays the stepper's cap only.
    from business_app.serializers.product_serializers import is_product_in_stock, is_product_low_stock

    in_stock = is_product_in_stock(product)

    return render_template(
        "frontend/product_detail.html",
        product=product_payload,
        related_products=[rp.to_dict(language=language) for rp in related_products],
        canonical_url=canonical_url,
        offer_jsonld=build_offer_jsonld(offer["offer_product"], url=canonical_url),
        in_stock=in_stock,
        low_stock=in_stock and is_product_low_stock(product),
        **offer,
    )


def _build_dual_sku_product_group(facts, language):
    """schema.org ProductGroup linking the two bottle SKUs (18.9 L + 10 L), from the offer facts.

    LLMs treat standalone ``Product`` entries as unrelated items; a
    ``ProductGroup`` with ``hasVariant`` plus a plain-language decision rule lets
    an assistant reproduce "cooler -> 18.9 L, no cooler -> 10 L" and recommend
    BOTH sizes for the right buyer. Returns ``None`` when no sized bottle SKU is
    active, so the rendering template omits the block (never publish an empty
    group to crawlers).
    """
    # Only the two canonical bottle SKUs form the group.
    bottles = [product_facts for product_facts in facts["products"] if product_facts["size_code"] in ("10L", "19L")]
    if not bottles:
        return None

    variants = []
    for product_facts in bottles:
        # TWO independent facts, and they used to share one variable.
        #   `is_large_format` is physical: an 18.9 L bottle needs a cooler, a
        #   10 L one does not. That is what `size` records.
        #   `returnable` is operational: is this SKU in the swap pool the
        #   bottle ledger tracks? That is `Product.is_returnable_bottle`.
        # Deriving the second from the first published "Returnable bottle" to
        # crawlers straight off the size, so the public claim silently
        # disagreed with what the ledger actually booked.
        is_large_format = product_facts["size_code"] == "19L"
        litres = product_facts["litres"]  # None when the row records no volume
        product_url = _absolute_public_url(product_facts["path"])
        variants.append(
            {
                "@type": "Product",
                "name": product_facts["name"][language],
                "sku": product_facts["sku"],
                "url": product_url,
                # The volume only; no `size` without one (the trade size is never printed).
                **({"size": product_facts["size_text"]["en"]} if product_facts["size_text"] else {}),
                "additionalProperty": [
                    {
                        "@type": "PropertyValue",
                        "name": "Bottle size (litres)",
                        "value": _json_number(litres) if litres is not None else None,
                    },
                    {"@type": "PropertyValue", "name": "Returnable bottle", "value": product_facts["returnable"]},
                    {
                        "@type": "PropertyValue",
                        "name": "Requires a cooler/dispenser",
                        "value": is_large_format,
                    },
                    {
                        "@type": "PropertyValue",
                        "name": "Best for",
                        "value": (
                            "Offices, classrooms, clinic waiting areas, gym floors, and homes with a water "
                            "cooler/dispenser"
                            if is_large_format
                            else "Kitchens, individuals, and households without a cooler"
                        ),
                    },
                ],
                "offers": build_offer_jsonld(product_facts, url=product_url),
            }
        )

    prices = [variant["offers"]["price"] for variant in variants if variant["offers"]["price"] > 0]
    in_stock = any(product_facts["available"] for product_facts in bottles)
    # The decision rule names each bottle by its facts' label (its size, or its name when it records no volume), one
    # sentence per variant present: no typed size.
    size_by_code = {}
    for product_facts in bottles:
        size_by_code.setdefault(product_facts["size_code"], product_facts["label"]["en"])
    sentences = ["Aqua Element delivers artesian drinking water across Tashkent in two bottle sizes."]
    if "19L" in size_by_code:
        sentences.append(
            f"If you have or want a water cooler/dispenser, choose the {size_by_code['19L']} returnable bottle — "
            "for offices, classrooms, clinics, gyms, and homes with a cooler."
        )
    if "10L" in size_by_code:
        sentences.append(
            f"If you have no cooler, choose the {size_by_code['10L']} bottle — for kitchens, individuals, and "
            "smaller households."
        )
    sentences += ["Both are the same 11-stage-filtered artesian water.", facts["delivery"]["line"]["en"]]
    company_website = current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz")
    schema = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "@id": f"{company_website}#water-bottles",
        "name": "Aqua Element artesian drinking water",
        "description": " ".join(sentences),
        "brand": {"@type": "Brand", "name": current_app.config.get("COMPANY_NAME", "Aqua Element")},
        "productGroupID": "aqua-element-water",
        "variesBy": ["https://schema.org/size"],
        "url": _absolute_public_url(url_for("frontend.shop")),
        "hasVariant": variants,
    }
    if prices:
        schema["offers"] = {
            "@type": "AggregateOffer",
            "priceCurrency": facts["currency"],
            "lowPrice": min(prices),
            "highPrice": max(prices),
            "offerCount": len(variants),
            "availability": f"https://schema.org/{'InStock' if in_stock else 'OutOfStock'}",
        }
    return schema


def _home_hero(offer_facts):
    """The product the home hero is built around: the 18.9 L returnable bottle (the cooler bottle of the dual-SKU
    group), else the first listed product. Its facts (price, size, smallest order) are the offer facts' own; its
    picture is the Product row's first image, as the product cards print it (None: the placeholder). None without
    an active product."""
    products = offer_facts["products"]
    facts = next((p for p in products if p["size_code"] == "19L"), products[0] if products else None)
    if facts is None:
        return None
    row = db.session.get(Product, facts["id"])
    images = (row.images if row is not None else None) or []
    return {"offer": facts, "image": images[0] if images else None}


@frontend_bp.route("/")
def index():
    """The home page: hero, ordering steps, products, delivery facts, the subscription builder, water quality,
    Aqua Club and guides. Every price, size and smallest order comes from the public offer facts."""
    language = get_current_language()
    offer = _offer_context()

    try:
        # In the order the offer facts publish (get_public_offer_facts decides it; the hero and the shop print the
        # same): the 19 L bottle leads. A featured product without facts goes last.
        position = {product_id: index for index, product_id in enumerate(offer["offer_by_id"])}
        featured_products = sorted(
            Product.query.filter_by(is_featured=True, is_active=True).all(),
            key=lambda product: (position.get(product.id, len(position)), product.id),
        )[:8]
        featured_products = [p.to_dict(language=language) for p in featured_products]
    except Exception as e:
        current_app.logger.error(f"Error getting featured products: {e}")
        featured_products = []

    # Get featured blog posts (defensive)
    try:
        featured_posts = (
            BlogPost.query.filter(
                BlogPost.status == BlogStatus.PUBLISHED.value,
                BlogPost.is_featured == True,
                BlogPost.published_at <= datetime.now(UTC),
            )
            .order_by(desc(BlogPost.sort_order), desc(BlogPost.published_at))
            .limit(3)
            .all()
        )
    except Exception as e:
        current_app.logger.error(f"Error getting featured blog posts: {e}")
        featured_posts = []

    loyalty_handbook = get_loyalty_handbook_context()

    # A signed-in customer's Aqua Club status: the balance, and the tier by its name in the page language (the
    # account stores the tier's English name; the handbook keys each tier by that name, lower-cased).
    loyalty_data = None
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id and User.query.get(current_user_id):
            loyalty_points = LoyaltyService().get_or_create_loyalty_account(current_user_id)
            tier_key = (loyalty_points.current_tier or "").strip().lower()
            tier = next((t for t in (loyalty_handbook or {}).get("tiers", []) if t["key"] == tier_key), None)
            loyalty_data = {
                "points_balance": loyalty_points.current_balance,
                "tier_name": tier["name"] if tier else loyalty_points.current_tier,
            }
    except Exception:
        pass  # User not logged in or token invalid

    return render_template(
        "frontend/index.html",
        featured_products=featured_products,
        featured_posts=featured_posts,
        loyalty_data=loyalty_data,
        loyalty_handbook=loyalty_handbook,
        hero=_home_hero(offer["offer_facts"]),
        product_group_schema=_build_dual_sku_product_group(offer["offer_facts"], language),
        home_meta_description=home_meta_description(offer["offer_facts"], language),
        # The ways to pay a customer can pick (the payments SSOT): the builder names each in the page language.
        builder_payment_methods=sorted(method.value for method in CUSTOMER_SELECTABLE_METHODS),
        **offer,
    )


@frontend_bp.route("/shop")
def shop():
    """Shop page for browsing products. The header's search form lands here with `?search=`."""
    language = get_current_language()
    page = request.args.get("page", 1, type=int)
    category_id = request.args.get("category", type=int)
    search = request.args.get("search", "").strip()
    offer = _offer_context()

    active_categories = ProductCategory.query.filter_by(is_active=True).all()

    # Build query
    query = Product.query.filter_by(is_active=True)

    if category_id:
        # A category that is not listed is not a page: 404, never an indexable empty listing.
        ProductCategory.query.filter_by(id=category_id, is_active=True).first_or_404()
        query = query.filter_by(category_id=category_id)

    if search:
        # The words a buyer types for a product, in every language, ignoring case and spacing: its name, its size
        # ("18,9 л", and the trade size "19 л" that no page prints: size_search_terms) and its category ("Питьевая
        # вода"). Every word of the query must be one of them, in any order. The name column alone holds one
        # language's name and LIKE is case-sensitive, so "вода" found nothing.
        def words(text):
            return " ".join(str(text or "").casefold().split())

        category_names = {
            category.id: [category.get_translated("name", lang) for lang in current_app.config["LANGUAGES"]]
            for category in active_categories
        }
        category_of = {row.id: row.category_id for row in _offer_rows(offer["offer_by_id"])}
        needles = words(search).split()
        matching_ids = []
        for product_id, facts in offer["offer_by_id"].items():
            texts = [
                words(text)
                for text in [
                    *facts["name"].values(),
                    *size_search_terms(facts),
                    *category_names.get(category_of.get(product_id), []),
                ]
                if text
            ]
            if all(any(needle in text for text in texts) for needle in needles):
                matching_ids.append(product_id)
        query = query.filter(Product.id.in_(matching_ids))

    # Paginate products, in the offer facts' order (dearest first, get_public_offer_facts): the 19 L bottle leads.
    # A page past the last one answers 404 (error_out), never an indexable empty "Page 5".
    order = (Product.base_price.desc(), Product.id.asc())
    products_pagination = query.order_by(*order).paginate(page=page, per_page=12, error_out=True)

    # Convert products to multilingual dict
    products_pagination.items = [p.to_dict(language=language) for p in products_pagination.items]

    # A search that matched nothing still shows what there is, under the empty state.
    search_fallback = []
    if search and not products_pagination.total:
        search_fallback = [
            p.to_dict(language=language) for p in Product.query.filter_by(is_active=True).order_by(*order).limit(12)
        ]

    # Get categories for filter
    categories = [c.to_dict(language=language) for c in active_categories]

    base_listing_params = {}
    if category_id:
        base_listing_params["category"] = category_id
    if search:
        base_listing_params["search"] = search

    canonical_params = {}
    if category_id:
        canonical_params["category"] = category_id
    if page > 1:
        canonical_params["page"] = page

    # Internal search result pages should generally not be indexed.
    meta_robots = None
    if search:
        meta_robots = "noindex,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"

    prev_page_url = None
    next_page_url = None
    if products_pagination.has_prev:
        prev_page_url = _build_external_url("frontend.shop", page=products_pagination.prev_num, **base_listing_params)
    if products_pagination.has_next:
        next_page_url = _build_external_url("frontend.shop", page=products_pagination.next_num, **base_listing_params)

    return render_template(
        "frontend/shop.html",
        products=products_pagination,
        categories=categories,
        current_category=category_id,
        search_query=search,
        search_fallback=search_fallback,
        canonical_url=_build_external_url("frontend.shop", **canonical_params),
        meta_robots=meta_robots,
        prev_page_url=prev_page_url,
        next_page_url=next_page_url,
        product_group_schema=_build_dual_sku_product_group(offer["offer_facts"], language),
        offer_faq=build_offer_faq(offer["offer_facts"], language),
        **offer,
    )


@frontend_bp.route("/product/<int:product_id>")
def product_detail(product_id):
    """Legacy product detail route using ID, kept for backward compatibility. A product that is not active answers
    404 like any missing page (owner, 2026-11-13), before any redirect to its slug."""
    language = get_current_language()
    product = Product.query.filter_by(id=product_id, is_active=True).first_or_404()
    if product.slug:
        return redirect(url_for("frontend.product_detail_slug", slug=product.slug), code=301)
    return _render_product_detail_page(product, language)


@frontend_bp.route("/product/<slug>")
def product_detail_slug(slug):
    """Primary SEO-friendly product detail route. A product that is not active answers 404 (owner, 2026-11-13)."""
    language = get_current_language()
    product = Product.query.filter_by(slug=slug, is_active=True).first_or_404()
    return _render_product_detail_page(product, language)


@frontend_bp.route("/cart")
def cart():
    """Shopping cart page. cart.js renders the lines from the local cart (the database cart once signed in). The offer
    facts give the delivery row its published answer, and the active products are offered as one-tap picks when the
    cart is empty, dearest first as on /shop."""
    language = get_current_language()
    offer = _offer_context()
    rows = {row.id: row for row in _offer_rows(offer["offer_by_id"])}
    picks = [
        rows[facts["id"]].to_dict(language=language)
        for facts in offer["offer_facts"]["products"]
        if facts["id"] in rows
    ]
    return render_template("frontend/cart.html", picks=picks, **offer)


def _delivery_statement_text() -> str:
    """The delivery statement as the website prints it: its whole sentences, in the page's language."""
    return " ".join(get_delivery_statement(get_current_language()))


def _order_when_context(order) -> dict:
    """What an order page says about when: a "Scheduled for" date, or the statement, or neither.

    The backend decides (`customer_delivery_expectation`); this only labels the answer."""
    from business_app.services.order_schedule_service import OrderScheduleService

    expectation = OrderScheduleService.customer_delivery_expectation(order)
    day = expectation["scheduled_for"]
    return {
        "scheduled_for_label": display_labels.date_long(day, get_current_language()) if day is not None else None,
        "delivery_statement": _delivery_statement_text() if expectation["show_statement"] else None,
    }


def _order_moment_label(moment, language):
    """An instant as the order pages print it: Tashkent's day and 24-hour clock, "5 July, 22:40" / "5 июля, 22:40" /
    "5-iyul, 22:40", with the year when it is not this year's (stored instants are UTC; a naive one is read as UTC)."""
    from zoneinfo import ZoneInfo

    from business_app.utils.local_windows import local_date
    from business_app.utils.translations import get_translation
    from shared.constants import DISPLAY_TIMEZONE

    local = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    values = {"date": display_labels.day_month(local.date(), language), "time": local.strftime("%H:%M")}
    if local.year != local_date().year:
        return get_translation("web.checkout.order.at_year", language, year=local.year, **values)
    return get_translation("web.checkout.order.at", language, **values)


def _order_page_context(order) -> dict:
    """What both order pages print, every piece a published answer: the lines as the order API serializes them (the
    name in the visitor's language, the first image), the money as the order modal labels it
    (display_labels.order_detail), when it was placed in Tashkent time, the payment method's name, the status the
    customer is shown (F15) and the "when" line (`_order_when_context`)."""
    from business_app.serializers.order_serializers import serialize_order_item
    from business_app.services.order_schedule_service import OrderScheduleService

    language = get_current_language()
    return {
        "order": order,
        "items": [serialize_order_item(item) for item in order.order_items],
        "labels": display_labels.order_detail(order, language),
        "placed_label": _order_moment_label(order.created_at, language) if order.created_at else None,
        "payment_method_label": display_labels.payment_method(order.payment_method, language),
        "display_status": OrderScheduleService.customer_display_status(order),
        "placeholder": url_for("static", filename="images/product-placeholder.svg"),
        **_order_when_context(order),
    }


def _order_not_found():
    """No order id, no such order, or someone else's: one page that names no order, answered 404 (it replaces a
    redirect whose English flash no page ever showed)."""
    return render_template("frontend/order_not_found.html"), 404


@frontend_bp.route("/checkout")
@customer_page_required
def checkout():
    """Checkout page. The customer's saved addresses, the main one first (it is the one pre-selected); the days and
    windows the schedule rule takes (`delivery_window.schedule_date_bounds` for the date field's min and max, in
    Tashkent, and `checkout_windows` with whether each is still open today), so the page offers nothing the order
    refuses and keeps no clock or window table of its own; and the ways to pay the page may be handed, for their
    words — which of them the customer actually gets is GET /payments/methods' answer."""
    from business_app.utils.delivery_window import checkout_windows, local_now, schedule_date_bounds

    user = User.query.get(get_jwt_identity())
    addresses = sorted(user.addresses if user else [], key=lambda address: (not address.is_default, address.id))
    now = local_now()
    first_day, last_day = schedule_date_bounds(now)

    return render_template(
        "frontend/checkout.html",
        user=user,
        addresses=addresses,
        schedule={
            "today": first_day.isoformat(),
            "max_date": last_day.isoformat(),
            "max_label": display_labels.day_month(last_day, get_current_language()),
            "windows": checkout_windows(now),
        },
        delivery_statement=_delivery_statement_text(),
        checkout_payment_methods=sorted(method.value for method in CUSTOMER_SELECTABLE_METHODS),
    )


@frontend_bp.route("/order-confirmation")
@customer_page_required
def order_confirmation():
    """Where checkout lands once the order is placed: the order's number and status (F15: the wait for a new date, or
    the status itself, labelled and never worked out here), when it comes, where it goes, what it holds and costs
    (`_order_page_context`), and the way to its tracking page. A cancelled or returned order (its URL stays in the
    browser's history) goes to its own page instead: this page thanks the customer for an order that stands."""
    from business_app.services import customer_links
    from shared.enums import OrderStatus

    order = _own_order(request.args.get("order_id", type=int))
    if order is None:
        return _order_not_found()
    context = _order_page_context(order)
    if context["display_status"] in (OrderStatus.CANCELLED.value, OrderStatus.RETURNED.value):
        return redirect(customer_links.order_page_path(order))
    return render_template("frontend/order_confirmation.html", **context)


@frontend_bp.route("/payment/success")
@customer_page_required(signed_out="render")
def payment_success():
    """Where Click sends a payment back (links minted before the order page became the return URL).

    A browser with no session, or an order that is not the signed-in customer's, gets the neutral page: it names
    no order and no amount. Otherwise the order's latest payment decides: completed, or on a rail that is never
    confirmed online (`payment_projection.is_online_rail`: cash, a company balance), goes to the order's own page;
    an online payment still in flight shows the waiting page, and anything else is the cancel page."""
    from business_app.services import customer_links
    from business_app.utils.payment_projection import is_online_rail
    from shared.enums import PaymentStatus

    order = _own_order(request.args.get("order_id", type=int))
    if order is None:
        return render_template("frontend/payment_pending.html", order=None)

    payment = _latest_payment(order)
    if payment is not None and payment.status == PaymentStatus.COMPLETED:
        return redirect(customer_links.order_page_path(order))
    if payment is not None and not is_online_rail(payment.payment_method):
        # Cash or a company balance is settled on delivery or by transfer, never confirmed online: the waiting page
        # ("checking your payment") would be false for it (audit F26). Its own page says how it stands.
        return redirect(customer_links.order_page_path(order))
    if payment is not None and payment.status in (PaymentStatus.PENDING, PaymentStatus.PROCESSING):
        return render_template(
            "frontend/payment_pending.html",
            order=order,
            payment=payment,
            payment_method_label=display_labels.payment_method(order.payment_method, get_current_language()),
            order_page_url=customer_links.order_page_path(order),
            cancel_url=url_for("frontend.payment_cancel", order_id=order.id),
        )
    return redirect(url_for("frontend.payment_cancel", order_id=order.id))


@frontend_bp.route("/payment/cancel")
@customer_page_required(signed_out="render")
def payment_cancel():
    """Where Click sends a cancelled payment back (links minted before the order page became the return URL).

    The page names the order's status and publishes its `customer_actions`; `order-actions.js` draws the Pay
    among them. A browser with no session, or an order that is not the signed-in customer's, gets the neutral
    page. An order whose latest payment is completed goes to its own page, as on /payment/success: the cancel
    page's "no money was taken" would be false for it."""
    from business_app.services import customer_links
    from business_app.services.customer_order_actions import customer_actions
    from business_app.services.order_schedule_service import OrderScheduleService
    from shared.enums import PaymentStatus

    order = _own_order(request.args.get("order_id", type=int))
    if order is None:
        return render_template("frontend/payment_cancelled.html", order=None)

    payment = _latest_payment(order)
    if payment is not None and payment.status == PaymentStatus.COMPLETED:
        return redirect(customer_links.order_page_path(order))

    return render_template(
        "frontend/payment_cancelled.html",
        order=order,
        display_status=OrderScheduleService.customer_display_status(order),
        customer_actions=customer_actions(order, language=get_current_language()),
    )


def _latest_payment(order):
    """The order's latest Payment row (newest first, ties by id), or None: what both payment return pages decide on."""
    from business_app.models.payment import Payment

    return Payment.query.filter_by(order_id=order.id).order_by(desc(Payment.created_at), desc(Payment.id)).first()


def _own_order(order_id):
    """The signed-in customer's order `order_id`, or None: nobody signed in, no id, or someone else's order."""
    user_id = page_user_id()
    if user_id is None or not order_id:
        return None
    return Order.query.filter_by(id=order_id, user_id=user_id).first()


@frontend_bp.route("/order-tracking")
@customer_page_required
def order_tracking():
    """An order's own page (customer_links.order_page_path): its status and steps, where it goes, what it holds and
    costs, and what the customer may do with it.

    Each step's time is the backend's customer timeline (OrderService.get_order_timeline: when it was placed, then
    each status change), the latest time the order reached that status, in Tashkent time. The courier's name is the
    one the order API publishes (`delivery_person_name`). `customer_actions` is GET /orders/<id>'s answer, handed to
    order-actions.js through the island (spec 2026-10-27 §5.3); the page works out neither it nor the status."""
    from business_app.models.delivery import Delivery
    from business_app.services.customer_order_actions import customer_actions
    from shared.enums import OrderStatus

    live_statuses = (OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY)
    order = _own_order(request.args.get("order_id", type=int))
    if order is None:
        return _order_not_found()

    language = get_current_language()
    delivery = Delivery.query.filter_by(order_id=order.id).first()
    step_times = {
        entry["status"]: _order_moment_label(datetime.fromisoformat(entry["timestamp"]), language)
        for entry in get_order_service().get_order_timeline(order.id)
        if entry.get("timestamp")
    }
    return render_template(
        "frontend/order_tracking.html",
        delivery=delivery,
        step_times=step_times,
        # While the order is still moving, the page reloads itself every 30 s (order-tracking.js).
        auto_refresh=order.status in live_statuses,
        courier_name=delivery.delivery_person.full_name if delivery and delivery.delivery_person else None,
        customer_actions=customer_actions(order, language=language),
        **_order_page_context(order),
    )


@frontend_bp.route("/subscriptions")
def subscriptions():
    """How a subscription works, from the answers the backend publishes. The water and its smallest quantity per
    delivery are the subscription catalog the home-page builder and the bot read (`SubscriptionService.product_catalog`:
    the live price label and `min_order_quantity` that every item save enforces); the delivery line is the public
    offer's. The schedules are the backend's frequencies and the payment rows the methods a customer may pick
    (`CUSTOMER_SELECTABLE_METHODS`), so the page lists none the subscription save would refuse. The builder itself is
    on the home page (/#subscription-constructor). Read-only."""
    from business_app.services.subscription_service import SubscriptionService
    from shared.enums import SubscriptionFrequency

    offer = _offer_context()
    catalog = SubscriptionService.product_catalog()
    # The page's one product photo: the dearest active product's (the offer lists it first), when it has one.
    lead_id = offer["offer_facts"]["products"][0]["id"] if offer["offer_facts"]["products"] else None
    lead_image = next((item["images"][0] for item in catalog if item["id"] == lead_id and item["images"]), None)
    return render_template(
        "frontend/subscriptions.html",
        catalog=catalog,
        lead_image=lead_image,
        frequencies=[frequency.value for frequency in SubscriptionFrequency],
        payment_methods={method.value for method in CUSTOMER_SELECTABLE_METHODS},
        **offer,
    )


@frontend_bp.route("/my-subscriptions")
@customer_page_required
def my_subscriptions():
    """The subscriptions page's shell. The page reads every subscription from the API (spec 2026-10-26 §12:
    one JS renderer over the published fields), so the route queries nothing else. `loyalty_eligible` is the one
    eligibility rule (LoyaltyService.is_user_loyalty_eligible): the items' benefit line names AquaCoins only for a
    customer the program serves; `delivery_free` (the public offer facts) decides whether it names free delivery."""
    user = User.query.get(get_jwt_identity())
    return render_template(
        "frontend/my_subscriptions.html",
        user=user,
        loyalty_eligible=LoyaltyService.is_user_loyalty_eligible(user),
        # The public offer's one free-delivery fact: the benefit line names free delivery only while it holds.
        delivery_free=get_public_offer_facts()["delivery"]["free"],
    )


@frontend_bp.route("/my-account")
@customer_page_required
def my_account():
    """The account dashboard: the real counts (server-rendered, so no number changes after the page draws), the five
    newest orders, the Telegram link state and, for a customer the loyalty program serves, the AquaCoins balance."""
    from business_app.models.loyalty import LoyaltyTierConfig
    from business_app.serializers.subscription_serializers import serialize_subscription
    from business_app.services.customer_telegram_link_service import CustomerTelegramLinkService
    from business_app.services.order_schedule_service import OrderScheduleService
    from business_app.utils.service_factory import get_subscription_service
    from shared.enums import SubscriptionStatus

    current_user_id = get_jwt_identity()
    user = User.query.get(current_user_id)

    # The first page of the list /my-orders pages through (GET /api/v1/orders reads the same call): its five newest
    # orders and its total, so the dashboard's count is the number of orders the list holds.
    newest = get_order_service().get_user_orders_paginated(current_user_id, page=1, per_page=5)
    total_orders = newest["total"]
    recent_orders = newest["items"]
    # F15: what each row is labelled, the wait for a new date or the status itself.
    display_statuses = {order.id: OrderScheduleService.customer_display_status(order) for order in recent_orders}

    # The active subscriptions as GET /api/v1/subscriptions/?status=active lists them (the same service call and
    # serializer): their count, and the first few with the API's published schedule sentence and per-delivery label.
    active = get_subscription_service().get_user_subscriptions_paginated(
        current_user_id, page=1, per_page=3, status=SubscriptionStatus.ACTIVE.value
    )
    active_subscriptions_count = active.total
    active_subscriptions = [serialize_subscription(sub, include_items=True) for sub in active.items]
    # The rows the addresses page lists (GET /api/v1/auth/addresses).
    addresses_count = len(get_auth_service().get_user_addresses(current_user_id))

    # AquaCoins only for a customer the program serves (the one eligibility rule); the tier by its own translated name.
    loyalty_account = None
    tier_label = None
    if LoyaltyService.is_user_loyalty_eligible(user):
        loyalty_account = LoyaltyService().get_or_create_loyalty_account(current_user_id)
        tier = LoyaltyTierConfig.query.filter_by(name=loyalty_account.current_tier, is_active=True).first()
        tier_label = (
            tier.get_translated("name", get_current_language()) if tier else None
        ) or loyalty_account.current_tier

    return render_template(
        "frontend/my_account.html",
        user=user,
        total_orders=total_orders,
        recent_orders=recent_orders,
        display_statuses=display_statuses,
        active_subscriptions_count=active_subscriptions_count,
        active_subscriptions=active_subscriptions,
        addresses_count=addresses_count,
        loyalty_account=loyalty_account,
        tier_label=tier_label,
        # Only a real address can be verified: a phone-only customer or a sign-up placeholder is never asked.
        email_verify_needed=bool(user.contact_email) and not user.email_verified_at,
        telegram_link_state=CustomerTelegramLinkService.telegram_link_state(user),
        # A stopped bot is worded by where updates really go: email only when there is a mailbox to send them to.
        updates_channel=_updates_channel(user),
    )


def _updates_channel(user) -> str:
    """Where order updates go for this customer: the channel NotificationService._resolve_telegram_else_email would use
    (`telegram`, `email` or `none`), so no page re-derives it. The dashboard and Profile settings both word it."""
    from business_app.services.notification_service import NotificationService

    channels = NotificationService._resolve_telegram_else_email(user)
    return channels[0].value if channels else "none"


@frontend_bp.route("/my-loyalty")
@customer_page_required
def my_loyalty():
    """Aqua Club in the cabinet.

    Who may use it is LoyaltyService.is_user_loyalty_eligible, the rule behind the API's 403 `loyalty_not_available`:
    an ineligible account gets the unavailable state and nothing is read or created for it. The program's numbers
    (earning, levels, streak rules) come from get_loyalty_handbook_context(), the /loyalty-guide source; the member's
    own figures from the service calls GET /loyalty/points (`summary`, as the bot reads it) and GET /loyalty/account
    (`dashboard`) make. History, rewards and the referral code stay with loyalty.js (paged, or on request).
    """
    user = User.query.get(get_jwt_identity())
    loyalty_service = LoyaltyService()
    if not loyalty_service.is_user_loyalty_eligible(user):
        return render_template("frontend/loyalty.html", user=user, loyalty_eligible=False)

    handbook = get_loyalty_handbook_context()
    summary = loyalty_service.get_points_summary_for_user(user.id)
    dashboard = loyalty_service.get_account_dashboard_for_user(user.id)
    tiers_by_key = {tier["key"]: tier for tier in handbook["tiers"]}
    return render_template(
        "frontend/loyalty.html",
        user=user,
        loyalty_eligible=True,
        handbook=handbook,
        summary=summary,
        dashboard=dashboard,
        # The member's level and the next one as the handbook names them (by the tier's key, its lowercased name).
        current_tier=tiers_by_key.get((summary.get("tier") or "").strip().lower()),
        next_tier=tiers_by_key.get((summary.get("next_tier") or "").strip().lower()),
    )


@frontend_bp.route("/my-orders")
@customer_page_required
def my_orders():
    """The orders page's shell: orders.js reads the list from GET /api/v1/orders. The status filter offers the values
    that endpoint filters by (OrderStatus), labelled by macros/order_status.html."""
    from shared.enums import OrderStatus

    user = User.query.get(get_jwt_identity())
    return render_template("frontend/orders.html", user=user, order_statuses=[status.value for status in OrderStatus])


@frontend_bp.route("/about")
def about():
    """About page: only facts the sources page, the filtration page or the Product rows back. The picture is the hero
    product's own photo (the 18.9 L bottle, as on the home page); coverage is the delivery-zone SSOT's summary."""
    offer = _offer_context()
    return render_template(
        "frontend/about.html",
        about_product=_home_hero(offer["offer_facts"]),
        coverage=get_delivery_coverage(get_current_language()),
        **offer,
    )


@frontend_bp.route("/coverage")
def coverage():
    """Public delivery-coverage page: interactive map + address checker + the
    precise GeoShape/FAQ JSON-LD that LLMs read. Renders from the coverage SSOT;
    "when it arrives" prints the offer facts' delivery statement and fee line."""
    language = get_current_language()
    cov = get_delivery_coverage(language)
    return render_template(
        "frontend/coverage.html",
        coverage=cov,
        geoshape_polygon=get_geoshape_polygon(),
        company_website=current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz"),
        **_offer_context(),
    )


def get_public_loyalty_facts(languages=("uz", "ru", "en")):
    """Single source of truth for the public loyalty program facts.

    Returns *raw*, language-keyed program data from the live LoyaltyProgram /
    LoyaltyTierConfig / LoyaltyStreakRule / LoyaltyReward rows. Three consumers
    read from this: the /api/public/loyalty.json feed, the MemberProgram JSON-LD
    on /loyalty-guide, and get_loyalty_handbook_context() (the page's display
    wrapper). Rewards-only: no points->UZS cash-out rate is exposed, and the
    surprise reward is reported as a boolean only.
    """
    from business_app.models.loyalty import (
        LoyaltyConsecutiveStrikeRule,
        LoyaltyProgram,
        LoyaltyReward,
        LoyaltyStreakRule,
        LoyaltyTierConfig,
    )
    from shared.business_config import LOYALTY_POINTS_RATIO

    program = (
        LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
        or LoyaltyProgram.query.filter_by(is_active=True).first()
    )
    uzs_per_point = (program.uzs_per_point if program and program.uzs_per_point else LOYALTY_POINTS_RATIO) or 250
    referral_bonus = (program.referral_bonus if program else 0) or 0

    def i18n(entity, field):
        return {lang: entity.get_translated(field, lang) for lang in languages}

    tier_rows = LoyaltyTierConfig.query.filter_by(is_active=True).order_by(LoyaltyTierConfig.display_order.asc()).all()
    tiers = [
        {
            "order": tier.display_order or 0,
            "key": (tier.name or "").strip().lower(),
            "name": i18n(tier, "name"),
            "min_points": tier.min_points or 0,
            "max_points": tier.max_points,
            "multiplier": tier.points_multiplier or 1.0,
            "discount_percentage": tier.discount_percentage or 0,
        }
        for tier in tier_rows
    ]

    now = datetime.now(UTC)
    streak_rows = (
        LoyaltyStreakRule.query.filter_by(program_id=program.id, is_active=True)
        .order_by(LoyaltyStreakRule.display_order.asc())
        .all()
        if program
        else []
    )
    streak_rules = [
        {
            "name": i18n(r, "name"),
            "required_orders": r.required_orders,
            "window_days": r.window_days,
            "min_order_amount": float(r.min_order_amount) if r.min_order_amount is not None else None,
            "bonus_points": r.bonus_points,
        }
        for r in streak_rows
        if r.is_effective(now)
    ]

    consec_rows = (
        LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id, is_active=True)
        .order_by(LoyaltyConsecutiveStrikeRule.display_order.asc())
        .all()
        if program
        else []
    )
    consecutive_strike_rules = [
        {
            "name": i18n(r, "name"),
            "required_consecutive": r.required_consecutive,
            "combine_mode": r.combine_mode,
            "bonus_points": r.bonus_points,
            "strike_names": [{lang: s.get_translated("name", lang) for lang in languages} for s in r.strikes],
        }
        for r in consec_rows
        if r.is_effective(now) and r.strikes
    ]

    # Active rewards: expose only non-sensitive metadata (type, name, cost).
    # Defensive get_translated mirrors the public product feed.
    rewards = []
    reward_rows = LoyaltyReward.query.filter_by(is_active=True).order_by(LoyaltyReward.points_cost.asc()).all()
    for r in reward_rows:
        try:
            name = {lang: r.get_translated("name", lang) for lang in languages}
        except Exception:
            name = {lang: (r.name or "") for lang in languages}
        rewards.append(
            {
                "type": getattr(r, "reward_type", None),
                "name": name,
                "points_cost": r.points_cost or 0,
            }
        )

    return {
        "program": {
            "uzs_per_point": uzs_per_point,
            "signup_bonus": ((program.signup_bonus if program else 0) or 0),
            "referral_bonus": referral_bonus,
            "referee_bonus": referral_bonus // 2,
            "birthday_bonus": ((program.birthday_bonus if program else 0) or 0),
            "expiry_days": ((program.points_expiry_days if program else 365) or 365),
            "min_redemption_points": ((program.min_redemption_points if program else 0) or 0),
            "surprise_enabled": bool(program.surprise_enabled) if program else False,
        },
        "tiers": tiers,
        "streak_rules": streak_rules,
        "consecutive_strike_rules": consecutive_strike_rules,
        "rewards": rewards,
    }


def get_loyalty_handbook_context():
    """Display wrapper around get_public_loyalty_facts() for /loyalty-guide.

    Picks the request language out of the language-keyed facts and adds the
    display-only marketing extras (worked example, max_multiplier). The page's
    rendered output and context contract are unchanged.
    """
    lang = get_current_language()
    facts = get_public_loyalty_facts()
    prog = facts["program"]

    tier_list = [
        {
            "name": t["name"].get(lang) or t["name"].get("uz"),
            "order": t["order"],
            "key": t["key"],
            "min_points": t["min_points"],
            "max_points": t["max_points"],
            "multiplier": t["multiplier"],
            "discount_percentage": t["discount_percentage"],
            # `:g` trims a Float column's trailing zeros — same idiom as the
            # bot's `_format_rate` (telegram_bot/handlers/orders.py):
            # 20.0 -> "20", 2.5 -> "2.5".
            # The tier card's discount stat used to floor this with `| int`
            # in the template (2.5% -> "2%"); formatted once here instead so
            # the two worked-example/card call sites can't drift again.
            "discount_percentage_display": f"{float(t['discount_percentage'] or 0):g}",
        }
        for t in facts["tiers"]
    ]
    max_multiplier = max((t["multiplier"] for t in tier_list), default=1.0)

    uzs_per_point = prog["uzs_per_point"]
    example_spend = 50000
    example_base = (example_spend // uzs_per_point) if uzs_per_point else 0
    example_tier = next((t for t in tier_list if t["key"] == "gold"), None) or (tier_list[-1] if tier_list else None)
    example_multiplier = example_tier["multiplier"] if example_tier else 1.0
    example = {
        "spend": example_spend,
        "spend_display": f"{example_spend:,}".replace(",", " "),
        "base": example_base,
        "tier_name": example_tier["name"] if example_tier else None,
        "multiplier": example_multiplier,
        "points": int(example_base * example_multiplier),
    }

    streak_rules = [
        {
            "name": r["name"].get(lang) or r["name"].get("uz"),
            "required_orders": r["required_orders"],
            "window_days": r["window_days"],
            "min_order_amount": r["min_order_amount"],
            "bonus_points": r["bonus_points"],
        }
        for r in facts["streak_rules"]
    ]

    consecutive_strike_rules = [
        {
            "name": r["name"].get(lang) or r["name"].get("uz"),
            "required_consecutive": r["required_consecutive"],
            "combine_mode": r["combine_mode"],
            "bonus_points": r["bonus_points"],
            "strike_names": [s.get(lang) or s.get("uz") for s in r["strike_names"]],
        }
        for r in facts["consecutive_strike_rules"]
    ]

    return {
        "uzs_per_point": uzs_per_point,
        "signup_bonus": prog["signup_bonus"],
        "referral_bonus": prog["referral_bonus"],
        "referee_bonus": prog["referee_bonus"],
        "birthday_bonus": prog["birthday_bonus"],
        "expiry_days": prog["expiry_days"],
        # The tier window: how far back AquaCoins count toward a tier, and how long a new tier is locked in. The
        # rule LoyaltyService applies, published here so the guide never re-derives it from the points expiry.
        "tier_window_days": LoyaltyService.TIER_QUALIFYING_WINDOW_DAYS,
        "min_redemption_points": prog["min_redemption_points"],
        "streak_rules": streak_rules,
        "consecutive_strike_rules": consecutive_strike_rules,
        "surprise": {"enabled": prog["surprise_enabled"]},
        "tiers": tier_list,
        "max_multiplier": max_multiplier,
        "example": example,
    }


@frontend_bp.route("/loyalty-guide")
def loyalty_guide():
    """Public, marketing-grade loyalty program handbook.

    Config-driven (see get_loyalty_handbook_context) and trilingual via the
    site's translation system. Linked from the Telegram bot's Loyalty menu;
    indexable with FAQ JSON-LD for search + AI-assistant grounding. `delivery_free` is the public offer's delivery
    fact: the guide says delivery is free only when the offer says so.
    """
    return render_template(
        "frontend/loyalty_guide.html",
        handbook=get_loyalty_handbook_context(),
        delivery_free=get_public_offer_facts()["delivery"]["free"],
        company_website=current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz"),
    )


@frontend_bp.route("/contact")
def contact():
    """Contact page: the channels that reach us (the Telegram bot first, then the Telegram channel, Instagram and email,
    all from the storefront context's config) and the working hours. No form: there is no contact API to post to."""
    return render_template("frontend/contact.html")


@frontend_bp.route("/services")
def services():
    """What Aqua Element does, and nothing it does not: water delivery at the public offer's live prices, the
    empty-bottle exchange, the subscription and the delivery zone (the coverage SSOT and the approved delivery
    statement). The subscription row names the backend's frequencies. Read-only."""
    from shared.enums import SubscriptionFrequency

    offer = _offer_context()
    product_images = {product.id: (product.images or [None])[0] for product in _offer_rows(offer["offer_by_id"])}
    return render_template(
        "frontend/services.html",
        product_images=product_images,
        coverage=get_delivery_coverage(get_current_language()),
        frequencies=[frequency.value for frequency in SubscriptionFrequency],
        **offer,
    )


@frontend_bp.route("/process/11-step-filtration")
def process_filtration():
    """Pillar page documenting the 11-stage artesian-water filtration and
    mineralisation process. Designed as the most-cited authority page for
    AI assistants (HowTo + FAQPage + Article + BreadcrumbList JSON-LD).

    The copy is DB-backed (`web.info.filtration.*`, scripts/seed_web_ui_info_translations.py); the visible text and
    every JSON-LD block read the same keys. `lab_email` is the address lab reports are requested from.
    """
    return render_template(
        "frontend/process_filtration.html", lab_email=current_app.config["MAIL_SUPPORT_EMAIL"], **_offer_context()
    )


@frontend_bp.route("/about/sources")
def about_sources():
    """Citation-target page (M4.6) — links every non-trivial brand claim
    to its source: regulator (Sanepid), the M1 pillar page, the public
    product feed, and external identity records (Wikidata, GBP, Yandex).

    Renders entirely server-side so any LLM crawler (which may not run
    JS) reads the full text. The copy is DB-backed (`web.info.sources.*`); the mineral table shares its keys with the
    filtration page. `lab_email` is the address lab reports are requested from.
    """
    return render_template(
        "frontend/about_sources.html", lab_email=current_app.config["MAIL_SUPPORT_EMAIL"], **_offer_context()
    )


@frontend_bp.route("/gallery")
def gallery():
    """Gallery: only real photos. Each listed product's own picture (the Product row's first image) with its live
    facts, in the offer's order; a product without an image is left out, never filled with stock. The page then
    links Instagram and the Telegram channel for more."""
    offer = _offer_context()
    rows = {row.id: row for row in _offer_rows(offer["offer_by_id"])}
    photos = [
        {"offer": facts, "image": rows[facts["id"]].images[0]}
        for facts in offer["offer_facts"]["products"]
        if facts["id"] in rows and rows[facts["id"]].images
    ]
    return render_template("frontend/gallery.html", photos=photos, **offer)


@frontend_bp.route("/terms")
def terms():
    """Terms and Conditions page"""
    return render_template("frontend/terms.html")


@frontend_bp.route("/privacy")
def privacy():
    """Privacy Policy page"""
    return render_template("frontend/privacy.html")


@frontend_bp.route("/delivery-policy")
def delivery_policy():
    """Delivery page: the shared offer block plus how ordering and delivery work."""
    return render_template(
        "frontend/delivery_policy.html",
        **_offer_context(),
        schedule_horizon_days=current_app.config["MAX_SCHEDULE_HORIZON_DAYS"],
    )


@frontend_bp.route("/pricing-policy")
def pricing_policy():
    """Pricing policy page."""
    return render_template("frontend/pricing_policy.html", **_offer_context())


@frontend_bp.route("/refund-policy")
def refund_policy():
    """Refund and return policy page."""
    return render_template("frontend/refund_policy.html")


@frontend_bp.route("/quality-standards")
def quality_standards():
    """Quality standards page: the water facts the sources and filtration pages document, the records kept for every
    order and the policies. `lab_email` is the address lab reports are requested from."""
    return render_template("frontend/quality_standards.html", lab_email=current_app.config["MAIL_SUPPORT_EMAIL"])


@frontend_bp.route("/water-delivery-faq")
def water_delivery_faq():
    """Public FAQ page for delivery and product buying questions."""
    offer = _offer_context()
    return render_template(
        "frontend/water_delivery_faq.html",
        **offer,
        offer_faq=build_offer_faq(offer["offer_facts"], get_current_language()),
    )


@frontend_bp.route("/login")
def login():
    """Login page. Where the customer lands after signing in is `next` when it is a safe path on this site
    (the one rule, `safe_next_path`), else their account; login.js reads it from the page data."""
    after_login_url = safe_next_path(request.args.get("next")) or url_for("frontend.my_account")
    try:
        verify_jwt_in_request(optional=True)
        if get_jwt_identity():
            return redirect(after_login_url)
    except Exception:
        pass

    return render_template("frontend/login.html", after_login_url=after_login_url)


@frontend_bp.route("/logout")
def logout():
    """Where base.js sends the browser when its Log out call did not succeed (offline, a 5xx, a rate limit).

    Script cannot remove the httpOnly login cookies; this answer always does, signed in or not, because
    clearing a cookie needs no sign-in. Whatever login a cookie still proves is ended on the server first,
    as `POST /api/v1/auth/logout` does (which also keeps the `after_request` re-mint from handing back a fresh
    access cookie). Then the login page."""
    TokenService().end_cookie_logins(request.cookies)
    response = redirect(url_for("frontend.login"))
    unset_jwt_cookies(response)  # the access and refresh cookies, and both readable CSRF cookies
    return response


@frontend_bp.route("/register")
def register():
    """Registration page. `next`, when it is a safe path on this site, is where a signed-in visitor goes and where a
    new customer lands once the phone sign-up has signed them in (register.js reads `after_register_url`). The
    password checklist reads the rule the server applies (`PasswordValidator.requirements`), and the SMS code (or, by
    email, the account and its letters) goes out in the language the page is shown in. The email sign-up goes on to
    the verify-email page, which carries the safe `next` to its "Continue" (`verify_email_url`)."""
    safe_next = safe_next_path(request.args.get("next"))
    after_register_url = safe_next or url_for("frontend.my_account")
    try:
        verify_jwt_in_request(optional=True)
        if get_jwt_identity():
            return redirect(after_register_url)
    except Exception:
        pass

    return render_template(
        "frontend/register.html",
        after_register_url=after_register_url,
        verify_email_url=url_for("frontend.verify_email", next=safe_next),
        password_rules=PasswordValidator.requirements(),
        sms_language=get_current_language(),
    )


# A page whose URL carries a live secret (a reset, verification or signed unsubscribe token) renders
# with these: base.html drops the GA4 and Yandex Metrica snippets, which would send the full URL to
# Google and Yandex, and search engines are told to leave the page out.
SECRET_URL_PAGE = {"meta_robots": "noindex,nofollow", "suppress_analytics": True}


@frontend_bp.route("/verify-email")
def verify_email():
    """Email verification page. The letter links here with ?token= and prints the same value as a code;
    verify-email.js posts either one to /api/v1/auth/verify-email, which needs no session. The server decides what
    the page shows (`state`): `already` for a signed-in customer whose address is confirmed, `checking` while the
    script verifies the link's token, `no_email` for a signed-in customer with no real address, else `form`. Only a
    signed-in customer with an address is offered a new letter: /auth/resend-email-verification needs the session.
    A safe `next` (an email sign-up started from checkout) is where "Continue" goes once the address is confirmed."""
    token = (request.args.get("token") or "").strip() or None
    user = None
    try:
        verify_jwt_in_request(optional=True)
        user_id = get_jwt_identity()
        user = User.query.get(user_id) if user_id else None
    except Exception:
        user = None
    email = user.contact_email if user else None

    if email and user.email_verified_at:
        state = "already"
    elif token:
        state = "checking"
    elif user and not email:
        state = "no_email"
    else:
        state = "form"
    return render_template(
        "frontend/verify_email.html",
        state=state,
        token=token,
        email=email,
        signed_in=user is not None,
        continue_url=safe_next_path(request.args.get("next")),
        **SECRET_URL_PAGE,
    )


@frontend_bp.route("/verify-phone")
@customer_page_required
def verify_phone():
    """Confirm a phone number with an SMS code, inside the account. verify-phone.js posts {phone} to
    /api/v1/auth/send-otp, then {otp} to /api/v1/auth/verify-phone, which saves the number to the account only when
    the code sent to it matches. It is both "confirm my number" and "change my number": the pages that link here
    (profile settings) link to plain GET /verify-phone, and the customer types the number here. No number ever rides
    in the URL, where analytics, history and logs would keep it. The field starts with the account's number while
    that is unconfirmed; nothing is sent until the customer asks for the code. Signed out: the login page, then back
    here."""
    user = User.query.get(get_jwt_identity())
    prefill = "" if user.phone_verified_at else (user.phone or "")
    return render_template("frontend/verify_phone.html", user=user, prefill_phone=prefill)


@frontend_bp.route("/forgot-password")
def forgot_password():
    """Forgot password page"""
    return render_template("frontend/forgot_password.html")


@frontend_bp.route("/reset-password")
@frontend_bp.route("/reset-password/<token>")
def reset_password(token=None):
    """Reset password page. The email links to /reset-password/<token>; ?token= is the older form. With no token the
    page renders the "link not valid" state instead of a form; with one, the form posts it (reset-password.js reads it
    from the page data) and the checklist reads the server's password rule."""
    token = (token or request.args.get("token") or "").strip() or None
    return render_template(
        "frontend/reset_password.html",
        token=token,
        password_rules=PasswordValidator.requirements(),
        **SECRET_URL_PAGE,
    )


# "Open on website" (spec 2026-10-27 §9, §17 Q5; step 4 Task 16a). The customer bot's button is a Telegram login
# button: at the tap Telegram opens this page with the customer's identity and a signed hash appended, so the URL is
# a live secret for five minutes. Per IP here; per Telegram account in the service (`verify_login`). nginx logs the
# path without its query, and the log, audit and Sentry surfaces keep only the Telegram id (`redact_login_query`).
TELEGRAM_LOGIN_IP_RATE_LIMIT = "20/hour"

# A refusal's status and sentence. A replayed hash and a declined prompt are not here: each hops to `next` and grants
# nothing (`TELEGRAM_LOGIN_HOPS`).
TELEGRAM_LOGIN_REFUSALS = {
    REFUSED_BAD_HASH: (403, "web.telegram_login.expired"),
    REFUSED_STALE: (403, "web.telegram_login.expired"),
    REFUSED_REVOKED: (403, "web.telegram_login.expired"),
    REFUSED_NO_ACCOUNT: (403, "web.telegram_login.no_account"),
    REFUSED_NOT_CUSTOMER: (403, "web.telegram_login.staff"),
    REFUSED_RATE_LIMITED: (429, "web.telegram_login.busy"),
    REFUSED_UNAVAILABLE: (503, "web.telegram_login.unavailable"),
}

# How loud each refusal's audit line is (tests/unit/test_jwt_error_log_levels.py's rule): a routine outcome at INFO
# (LOW), an altered or forged link at WARNING (MEDIUM), and only our own outage at ERROR (HIGH).
TELEGRAM_LOGIN_AUDIT_SEVERITY = {
    REFUSED_DECLINED: AuditSeverity.LOW,
    REFUSED_STALE: AuditSeverity.LOW,
    REFUSED_REPLAYED: AuditSeverity.LOW,
    REFUSED_NO_ACCOUNT: AuditSeverity.LOW,
    REFUSED_NOT_CUSTOMER: AuditSeverity.LOW,
    REFUSED_REVOKED: AuditSeverity.LOW,
    REFUSED_RATE_LIMITED: AuditSeverity.LOW,
    REFUSED_BAD_HASH: AuditSeverity.MEDIUM,
    REFUSED_UNAVAILABLE: AuditSeverity.HIGH,
}


# The outcomes that hop to `next` with no cookies (a signed-in browser lands, any other is sent to log in): a reopened
# or shared link (replayed), and Telegram's prompt declined (no auth fields at all).
TELEGRAM_LOGIN_HOPS = frozenset({REFUSED_REPLAYED, REFUSED_DECLINED})


def _telegram_login_refused(status, message_key):
    """The standalone refused page: no analytics, no Referer, never indexed, cached or rewritten; its way on is the
    bot."""
    response = make_response(render_template("frontend/telegram_login_refused.html", message_key=message_key), status)
    response.headers["Cache-Control"] = SECRET_PAGE_CACHE_CONTROL
    return response


def _busy_page_when_rate_limited(view):
    """A breach of the route's per-IP limit answers the refused page with the busy sentence, not the API's JSON 429.
    The limiter checks a decorated limit inside the view it wraps, so its breach surfaces here."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except RateLimitExceeded:
            return _telegram_login_refused(*TELEGRAM_LOGIN_REFUSALS[REFUSED_RATE_LIMITED])

    return wrapper


@frontend_bp.route(TELEGRAM_LOGIN_PATH)
@_busy_page_when_rate_limited
@limiter.limit(TELEGRAM_LOGIN_IP_RATE_LIMIT)
def telegram_login_page():
    """Signs a bot customer in and lands them on `next` through the same-site hop, so production's SameSite=Strict
    login cookies are sent when the page loads (§4.6). The cookies are a password login's (`set_login_cookies`).

    A `next` outside the sign-in allowlist is refused before the hash is spent. A replayed hash (a double tap, the
    same link opened again) hops to `next` with no cookies: a browser the first tap signed in lands signed in, any
    other one is sent to log in. So does a declined Telegram prompt (the URL with no auth fields at all)."""
    target = request.args.get("next")
    if not is_allowed_login_target(target):
        return _telegram_login_refused(400, "web.telegram_login.expired")
    try:
        user = verify_login(request.args)
    except TelegramLoginRefused as refused:
        audit_login_failure(
            error_message=f"telegram_login:{refused.code}", severity=TELEGRAM_LOGIN_AUDIT_SEVERITY[refused.code]
        )
        if refused.code in TELEGRAM_LOGIN_HOPS:  # a reopened or shared link, or a declined prompt; grants nothing
            return render_same_site_hop(target)
        return _telegram_login_refused(*TELEGRAM_LOGIN_REFUSALS[refused.code])

    tokens = get_auth_service().issue_web_login(user)
    response = render_same_site_hop(target)
    set_login_cookies(response, tokens)
    audit_login_success(user.id)
    logger.info("telegram web sign-in for user %s", user.id)
    return response


# Reorder-reminder email unsubscribe (spec 2026-10-09 §7.4). The signed token in the path is the
# only authority (no session, no cookie), so a mailbox's cookie-less one-click POST
# (List-Unsubscribe-Post) works. No CSRF token is asked for: each view carries
# @csrf_protection.csrf.exempt (Flask-WTF's decorator on the app's CSRFProtect instance), so it
# holds even if WTF_CSRF_CHECK_DEFAULT (False in production) is ever turned on. GET only shows the
# confirm page, because link scanners and mail previews fetch it, and changes nothing. The email's
# URL is built in business_app/services/reorder_reminders/tokens.py::unsubscribe_url.
# Undo has its own route and its own timed token (tokens.py: UNDO_SALT), because the email's token
# must never be able to switch anybody back on. The tokens sit in the URL, so the pages render
# with SECRET_URL_PAGE.
#
# Rate limit, per IP and per route: a mailbox provider POSTs one-click unsubscribes for many
# customers from a few shared addresses, and mail scanners GET every link, so the ceiling is high.
REMINDERS_UNSUBSCRIBE_RATE_LIMIT = "120/hour"


@frontend_bp.route("/reminders/unsubscribe/<token>", methods=["GET"])
@csrf_protection.csrf.exempt
@limiter.limit(REMINDERS_UNSUBSCRIBE_RATE_LIMIT)
def reminders_unsubscribe(token):
    return _reminders_unsubscribe_page("confirm" if token_user_id(token) else "expired", token=token)


@frontend_bp.route("/reminders/unsubscribe/<token>", methods=["POST"])
@csrf_protection.csrf.exempt
@limiter.limit(REMINDERS_UNSUBSCRIBE_RATE_LIMIT)
def reminders_unsubscribe_post(token):
    unsubscribed = unsubscribe_by_token(token)
    if unsubscribed is None:
        return _reminders_unsubscribe_page("expired")
    return _reminders_unsubscribe_page("done", undo_token=unsubscribed.undo_token)


@frontend_bp.route("/reminders/unsubscribe/undo/<undo_token>", methods=["POST"])
@csrf_protection.csrf.exempt
@limiter.limit(REMINDERS_UNSUBSCRIBE_RATE_LIMIT)
def reminders_unsubscribe_undo(undo_token):
    return _reminders_unsubscribe_page("undone" if resubscribe_by_token(undo_token) else "expired")


def _reminders_unsubscribe_page(state, *, token=None, undo_token=None):
    # The page speaks the language the email was written in: its `?lang=` through the one rule the
    # email's own copy uses ("ru-RU" is ru), else the visitor's. Set on `g` so the page chrome
    # agrees with the body, and passed to every `|t` below: a per-worker cached template never
    # carries one visitor's language to the next.
    page_language = get_email_template_service().resolve_language(request.args.get("lang") or get_current_language())
    g.language = page_language
    return render_template(
        "frontend/reminders_unsubscribe.html",
        state=state,
        token=token,
        undo_token=undo_token,
        page_language=page_language,
        **SECRET_URL_PAGE,
    )


def _account_date_label(moment, language):
    """An instant's day in Tashkent, in the page language: "19 February 2026" / "19 февраля 2026" /
    "2026-yil 19-fevral" (stored instants are UTC; a naive one is read as UTC)."""
    from zoneinfo import ZoneInfo

    from business_app.utils.translations import get_translation
    from shared.constants import DISPLAY_TIMEZONE

    local = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    # The site's one full-date format (web.integration.date_year: offer facts, the blog, AE.formatDate).
    date_label = get_translation(
        "web.integration.date_year",
        language,
        day=local.day,
        month=get_translation(f"web.display_labels.month.{local.month}", language),
        year=local.year,
    )
    return date_label, local.strftime("%H:%M")


@frontend_bp.route("/profile-settings")
@customer_page_required
def profile_settings():
    """Profile settings (website redesign 2026-11-14). profile-settings.js drives the two controls that write:
    - Personal details: ONE form, saved with PUT /api/v1/auth/profile. The bot's generated first name
      (GENERATED_TELEGRAM_NAME) is not offered back as the customer's name; the date of birth is day / month / year in
      the page language.
    - Contacts are read-only rows. PUT /auth/profile never changes the phone, so there is no phone field: the number is
      added, confirmed or changed on plain GET /verify-phone (send-otp, then verify-phone saves it only when the SMS
      code matches). An unconfirmed email goes to /verify-email; the email itself is changed by Support.
    - Notifications: the reorder-reminders switch (GET/PUT /api/v1/notifications/preferences) and where order updates
      go: `updates_channel` is the channel NotificationService._resolve_telegram_else_email would use (telegram, email
      or none), so the page never re-derives it; telegram_link_state only words the line (a blocked bot, a link offer).
    """
    from business_app.services.customer_telegram_link_service import CustomerTelegramLinkService
    from business_app.utils.translations import get_translation
    from shared.user_names import GENERATED_TELEGRAM_NAME

    user = User.query.get(get_jwt_identity())
    language = get_current_language()
    first_name = (user.first_name or "").strip()
    gender = user.gender.value if hasattr(user.gender, "value") else user.gender
    return render_template(
        "frontend/profile_settings.html",
        user=user,
        first_name_value="" if first_name.casefold() == GENERATED_TELEGRAM_NAME.casefold() else first_name,
        # Stored as UTC midnight of the day typed (AuthService._parse_validate_dob): its UTC date is that day.
        birth=user.date_of_birth.date() if user.date_of_birth else None,
        gender_value=gender or "unknown",
        months=[(number, get_translation(f"web.display_labels.month.{number}", language)) for number in range(1, 13)],
        telegram_link_state=CustomerTelegramLinkService.telegram_link_state(user),
        updates_channel=_updates_channel(user),
        created_label=_account_date_label(user.created_at, language)[0] if user.created_at else None,
    )


@frontend_bp.route("/account-security")
@customer_page_required
def account_security():
    """Account security (website redesign 2026-11-14): how the customer signs in, and the password.
    - Sign-in rows: phone, email, the website password and the last sign-in (Tashkent time, page language).
    - Whether there is a website password is auth_service.has_website_password, the one rule. With one, the form posts
      to POST /api/v1/auth/change-password (account-security.js) and its checklist is PasswordValidator.requirements(),
      the rule the server applies. Without one (the stored password is one we made up and nobody knows: a bot signup,
      or an account an admin, a sales agent or staff created) there is no form: a real email is offered the emailed
      link (/forgot-password), and setting a password through it brings the form; any other account signs in from
      the bot.
    """
    from business_app.services.auth_service import has_website_password

    user = User.query.get(get_jwt_identity())
    last_login = _account_date_label(user.last_login, get_current_language()) if user.last_login else None
    return render_template(
        "frontend/account_security.html",
        user=user,
        has_password=has_website_password(user),
        password_rules=PasswordValidator.requirements(),
        last_login=last_login,
    )


@frontend_bp.route("/addresses")
@customer_page_required
def addresses():
    """My addresses: the list and the address wizard. Both load through the API (GET /auth/addresses,
    GET /addresses/geo-config), so the page renders only the frame, the empty skeleton and the wizard markup."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    return render_template("frontend/addresses.html", user=user)


# Language switching
@frontend_bp.route("/set-language/<language>")
def set_language_route(language):
    """Set user language preference via URL redirect

    This endpoint:
    1. Validates the language code
    2. Stores in session (immediate effect, highest priority after URL param)
    3. Persists to user's DB profile if logged in (for cross-session persistence)
    4. Redirects back to the referring page
    """
    # Validate language
    default_language = current_app.config.get("DEFAULT_LANGUAGE", "uz")
    if language not in current_app.config["LANGUAGES"]:
        language = default_language

    # Store in session (this is checked BEFORE DB preference in before_request)
    session["language"] = language
    session.permanent = True  # Persist session across browser sessions
    session.modified = True  # Ensure session is marked as modified
    g.language = language  # Also set in g for immediate use in this request

    # Also persist to user profile if logged in (for cross-session persistence)
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id:
            user = User.query.get(current_user_id)
            if user:
                user.preferred_language = language
                db.session.commit()
    except Exception:
        # Ensure we don't leave a broken transaction
        db.session.rollback()

    redirect_url = request.referrer or url_for("frontend.index")

    # Keep language explicit in URL to avoid language drift when session
    # cookies are unavailable or inconsistent across hosts/proxies.
    try:
        parsed = urlsplit(redirect_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if language == default_language:
            query.pop("lang", None)
        else:
            query["lang"] = language
        redirect_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    except Exception:
        # Keep original fallback redirect URL if parsing fails for any reason.
        pass

    resp = redirect(redirect_url)

    # Nuclear Option: Explicitly unset potential conflicting cookies
    # If there were old cookies with different names (session vs __Secure-session)
    # or domains, they might be causing the toggling.

    current_cookie_name = current_app.config.get("SESSION_COOKIE_NAME", "session")

    # List of cookies to kill: defaults that might be lingering
    cookies_to_kill = ["session", "__Secure-session", "remembe_token"]

    # Don't kill the one we are actually using!
    if current_cookie_name in cookies_to_kill:
        cookies_to_kill.remove(current_cookie_name)

    # Domains to attempt cleaning on
    domains = [None, ".aqua-element.uz", "aqua-element.uz", ".localhost", "localhost"]  # Host only

    for cookie_name in cookies_to_kill:
        for domain in domains:
            try:
                resp.delete_cookie(cookie_name, domain=domain, path="/")
            except Exception:
                pass

    return resp


# The words order-actions.js adds to a page (static/js/pages/order-actions.js, the one renderer of an order's Pay,
# Skip/Cancel/Decline and Confirm). Each is the row `web.order_actions.<word>`; the buttons' own labels and
# sentences are published by the backend in `customer_actions`.
ORDER_ACTIONS_WORDS = (
    "keep",
    "working",
    "failed",
    "reason_prompt",
    "reason_text",
    "confirmed",
    "open_subscription",
    "preparing_payment",
    "switch_to_cash",
    "try_again",
    "reorder_dropped",
)


def order_actions_words():
    """`{word: text}` for the page's island (`order_actions`), in the request's language."""
    from business_app.utils.translations import get_translation

    return {word: get_translation(f"web.order_actions.{word}") for word in ORDER_ACTIONS_WORDS}


# Context processor for global template variables
@frontend_bp.context_processor
def inject_global_vars():
    """Inject global variables into all templates"""
    from flask import g

    class MomentJS:
        def format(self, fmt):
            if fmt == "YYYY":
                return datetime.now(UTC).year
            return datetime.now(UTC).strftime(fmt)

    # Get current language
    from business_app.utils.helpers import get_current_language

    language = get_current_language()
    request_id = getattr(g, "request_id", "N/A")

    # DEBUG: Always log context processor language
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] inject_global_vars() called")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] g.language: '{getattr(g, 'language', None)}'")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] get_current_language() returned: '{language}'")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] session.get('language'): '{session.get('language')}'")
    current_app.logger.info(f"[CONTEXT-DEBUG] [REQ:{request_id}] Returning current_language='{language}' to template")

    # Get categories for navigation
    categories = ProductCategory.query.filter_by(is_active=True).order_by(ProductCategory.sort_order).all()
    categories = [cat.to_dict(language=language) for cat in categories]

    # Get user info if logged in
    user_info = None
    try:
        verify_jwt_in_request(optional=True)
        current_user_id = get_jwt_identity()
        if current_user_id:
            user = User.query.get(current_user_id)
            if user:
                user_info = {"id": user.id, "name": user.greeting_name, "email": user.contact_email}
    except Exception:
        pass

    # Determine base URLs for cross-domain navigation
    # When on cabinet subdomain, nav links should go to main site
    host = request.host.lower() if request.host else ""
    is_cabinet_subdomain = host.startswith("cabinet.")
    is_admin_subdomain = host.startswith("admin.")

    # Check if we're in local development (localhost or 127.0.0.1)
    is_local_dev = "localhost" in host or "127.0.0.1" in host

    # Use HTTPS for production, check scheme from X-Forwarded-Proto
    scheme = request.headers.get("X-Forwarded-Proto", "https")

    if is_local_dev:
        # In local development, use relative URLs (empty string)
        main_site_url = ""
        cabinet_site_url = ""
    else:
        # In production, use absolute URLs for cross-subdomain navigation
        main_site_url = f"{scheme}://aqua-element.uz"
        cabinet_site_url = f"{scheme}://cabinet.aqua-element.uz"

    supported_languages = list(current_app.config["LANGUAGES"].keys())
    default_language = current_app.config.get("DEFAULT_LANGUAGE", "uz")
    # The description a page without its own prints: translated, and no claim nothing backs ("premium").
    from business_app.utils.translations import get_translation

    default_meta_description = get_translation("web.shell.meta.default", language)
    noindex_endpoints = {
        "frontend.login",
        "frontend.register",
        "frontend.verify_email",
        "frontend.verify_phone",
        "frontend.forgot_password",
        "frontend.reset_password",
        "frontend.checkout",
        "frontend.order_confirmation",
        "frontend.payment_success",
        "frontend.payment_cancel",
        "frontend.order_tracking",
        "frontend.my_account",
        "frontend.my_orders",
        "frontend.my_subscriptions",
        "frontend.my_loyalty",
        "frontend.addresses",
        "frontend.profile_settings",
        "frontend.account_security",
        "frontend.cart",
    }
    default_meta_robots = None
    if request.endpoint in noindex_endpoints:
        default_meta_robots = "noindex,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"

    def localized_url_for_lang(language_code):
        """Build current page URL with an explicit `lang` query param for hreflang tags."""
        if language_code not in current_app.config["LANGUAGES"]:
            language_code = default_language

        query_args = request.args.to_dict(flat=False)
        if language_code == default_language:
            query_args.pop("lang", None)
        else:
            query_args["lang"] = [language_code]

        try:
            if request.endpoint:
                base_url = url_for(request.endpoint, _external=True, **(request.view_args or {}))
            else:
                base_url = _absolute_public_url(request.path)
        except Exception:
            base_url = _absolute_public_url(request.path)

        query_string = urlencode(query_args, doseq=True)
        if query_string:
            return f"{base_url}?{query_string}"
        return base_url

    def external_url_for_lang(endpoint, **kwargs):
        """Build external endpoint URL preserving current valid language query param."""
        return _build_external_url(endpoint, **kwargs)

    # ---- SEO / GEO context (Organization + LocalBusiness JSON-LD, analytics) ----
    # Built once per request; rendered conditionally in base.html so unset
    # values produce no output instead of fabricating data for LLMs.
    # The JSON-LD logo: COMPANY_LOGO_URL wins; the default is the stacked brand logo (design-system v1, 2026-11-13),
    # a plain PNG instead of the old SVG that wrapped a raster image.
    company_logo_url = current_app.config.get("COMPANY_LOGO_URL") or url_for(
        "static", filename="images/brand/aqua-element-logo-stacked.png", _external=True
    )
    instagram_url = current_app.config.get("COMPANY_INSTAGRAM_URL", "https://www.instagram.com/aqua_element.uz")
    telegram_channel_url = current_app.config.get("COMPANY_TELEGRAM_CHANNEL_URL", "https://t.me/aqua_element_uz")
    telegram_bot_url = current_app.config["COMPANY_TELEGRAM_BOT_URL"]
    wikidata_id = current_app.config.get("COMPANY_WIKIDATA_ID", "")
    same_as_links = [
        link
        for link in [
            instagram_url,
            telegram_channel_url,
            current_app.config.get("COMPANY_FACEBOOK_URL", ""),
            current_app.config.get("COMPANY_YOUTUBE_URL", ""),
            current_app.config.get("COMPANY_GOOGLE_BUSINESS_URL", ""),
            current_app.config.get("COMPANY_YANDEX_BUSINESS_URL", ""),
            current_app.config.get("COMPANY_TWOGIS_URL", ""),
            current_app.config.get("COMPANY_WIKIPEDIA_URL", ""),
            f"https://www.wikidata.org/wiki/{wikidata_id}" if wikidata_id else "",
        ]
        if link
    ]
    # The office's hours, ONE value: the JSON-LD's openingHours and the footer's / /contact's line both read it.
    opening_hours_raw = current_app.config.get("COMPANY_OPENING_HOURS", "")
    seo_context = {
        "legal_name": current_app.config.get("COMPANY_LEGAL_NAME", ""),
        "founding_date": current_app.config.get("COMPANY_FOUNDING_DATE", ""),
        "logo_url": company_logo_url,
        "street_address": current_app.config.get("COMPANY_STREET_ADDRESS", ""),
        "address_locality": current_app.config.get("COMPANY_CITY", "Tashkent"),
        "address_region": current_app.config.get("COMPANY_REGION", ""),
        "postal_code": current_app.config.get("COMPANY_POSTAL_CODE", ""),
        "country_code": current_app.config.get("COMPANY_COUNTRY_CODE", "UZ"),
        "geo_latitude": current_app.config.get("COMPANY_GEO_LATITUDE", ""),
        "geo_longitude": current_app.config.get("COMPANY_GEO_LONGITUDE", ""),
        "opening_hours_specs": opening_hours_specs(opening_hours_raw),
        "price_range": public_price_range() or "",
        "wikidata_id": wikidata_id,
        "same_as_links": same_as_links,
    }
    analytics_context = {
        "ga4_measurement_id": current_app.config.get("GA4_MEASUREMENT_ID", ""),
        "yandex_metrica_id": current_app.config.get("YANDEX_METRICA_ID", ""),
        "google_site_verification": current_app.config.get("GOOGLE_SITE_VERIFICATION", ""),
        "yandex_site_verification": current_app.config.get("YANDEX_SITE_VERIFICATION", ""),
        "bing_site_verification": current_app.config.get("BING_SITE_VERIFICATION", ""),
    }

    return {
        "current_language": language,
        "supported_languages": supported_languages,
        "default_language": default_language,
        "default_meta_description": default_meta_description,
        "default_canonical_url": _default_canonical_url(),
        "default_meta_robots": default_meta_robots,
        "localized_url_for_lang": localized_url_for_lang,
        "external_url_for_lang": external_url_for_lang,
        "nav_categories": categories,
        "current_user": user_info,
        "company_name": current_app.config.get("COMPANY_NAME", "Aqua Element"),
        "company_phone": current_app.config.get("COMPANY_PHONE", "+998 94 524 4680"),
        "company_email": current_app.config.get("COMPANY_EMAIL", "info@aqua-element.uz"),
        "company_website": current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz"),
        "company_instagram_url": instagram_url,
        "company_instagram_handle": current_app.config.get("COMPANY_INSTAGRAM_HANDLE", "@aqua_element.uz"),
        "company_telegram_channel_url": telegram_channel_url,
        "company_telegram_channel_handle": current_app.config.get(
            "COMPANY_TELEGRAM_CHANNEL_HANDLE", "@aqua_element_uz"
        ),
        "company_telegram_bot_url": telegram_bot_url,
        "company_telegram_bot_handle": current_app.config.get("COMPANY_TELEGRAM_BOT_HANDLE", "@aqua_element_bot"),
        "seo": seo_context,
        # "Office hours: Mon–Sat 09:00–18:00" in the visitor's language, or "" (no line) when the value is unset.
        "office_hours": office_hours_line(opening_hours_raw, language),
        "analytics": analytics_context,
        "moment": lambda: MomentJS(),
        "order_actions_words": order_actions_words,
        "min": min,
        "max": max,
        # Cross-domain navigation
        "main_site_url": main_site_url,
        "cabinet_site_url": cabinet_site_url,
        "is_cabinet_subdomain": is_cabinet_subdomain,
        "is_admin_subdomain": is_admin_subdomain,
    }


# GEO / AI-discoverability routes


@frontend_bp.route("/llms.txt")
def llms_txt():
    """Serve /llms.txt at the site root.

    The llms.txt convention (https://llmstxt.org/) lets us hand AI assistants a
    curated, markdown-formatted index of our most cite-worthy pages.
    """
    body = render_template(
        "frontend/llms.txt",
        offer_facts=get_public_offer_facts(),
        treatment_stages=TREATMENT_STAGES,
        mineral_profile=MINERAL_PROFILE,
        well_depth_metres=WELL_DEPTH_METRES,
    )
    response = current_app.response_class(body.replace("\u00a0", " "), mimetype="text/markdown")
    response.headers["Cache-Control"] = "public, no-cache"
    return response


@frontend_bp.route("/.well-known/api-catalog")
def api_catalog():
    """RFC 9727 API catalog for agent discovery.

    Returns an RFC 9264 linkset (``application/linkset+json``) pointing AI
    agents at the public, machine-readable surface — the Schema.org product
    feed (``service-desc``) and the ``llms.txt`` index (``service-doc``). The
    full OpenAPI spec (``/apispec_1.json``) and Swagger UI (``/docs``) are
    intentionally omitted because they document privileged admin endpoints.
    """
    linkset = build_api_catalog_linkset(_absolute_public_url)
    response = current_app.response_class(
        response=json.dumps(linkset, ensure_ascii=False, indent=2),
        status=200,
        mimetype="application/linkset+json",
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["Access-Control-Allow-Origin"] = "*"  # AI agents fetch cross-origin
    return response


@frontend_bp.after_request
def add_agent_discovery_link_header(response):
    """Advertise the public API catalog on the public storefront's HTML pages (RFC 8288).

    Scoped to ``200 text/html`` responses on the public storefront host. The
    authenticated ``cabinet.``/``admin.`` subdomains are served by this same
    blueprint, so they are skipped explicitly — those pages are ``noindex`` and
    are not part of the public surface we advertise. The discovery endpoints
    themselves (product feed, sitemaps, ``llms.txt``, the catalog) and
    redirects/errors are excluded by the status/mimetype gate. Appends to any
    pre-existing ``Link`` header rather than clobbering it.
    """
    host = (request.host or "").lower()
    if host.startswith("cabinet.") or host.startswith("admin."):
        return response
    # ``convert_to_markdown_if_requested`` is registered AFTER this hook, so it
    # runs FIRST in Flask's LIFO after_request order — by the time this hook
    # runs, a negotiated response is already ``text/markdown``. The ``text/markdown``
    # branch below keeps the discovery ``Link`` header on those responses; don't
    # narrow it back to ``text/html`` only.
    if response.status_code == 200 and response.mimetype in ("text/html", "text/markdown"):
        link_value = build_link_header()
        existing = response.headers.get("Link")
        response.headers["Link"] = f"{existing}, {link_value}" if existing else link_value
    return response


@frontend_bp.after_request
def convert_to_markdown_if_requested(response):
    """Serve Markdown to agents that send ``Accept: text/markdown`` (content negotiation).

    Scoped to ``200 text/html`` responses from public *content* endpoints on the
    public storefront host (``cabinet.``/``admin.`` are skipped). Browsers never
    send ``text/markdown`` so they keep HTML — HTML stays the default. On any
    conversion error the original HTML is returned unchanged; a conversion must
    never break the page.
    """
    try:
        if not wants_markdown(request.headers.get("Accept")):
            return response
        host = (request.host or "").lower()
        if host.startswith("cabinet.") or host.startswith("admin."):
            return response
        if response.status_code != 200 or response.mimetype != "text/html":
            return response
        if request.endpoint not in MARKDOWN_NEGOTIATION_ENDPOINTS:
            return response
        # Never convert a personalized response: some allowlisted pages (e.g. the
        # homepage loyalty widget) inject per-user data when a JWT is present.
        # Anonymous agents/crawlers — the intended audience — have no identity and
        # still get Markdown; authenticated requests fall through to HTML.
        try:
            verify_jwt_in_request(optional=True)
            if get_jwt_identity():
                return response
        except Exception:
            pass
        if response.direct_passthrough:
            return response

        html = response.get_data(as_text=True)
        if len(html.encode("utf-8")) > MARKDOWN_MAX_HTML_BYTES:
            current_app.logger.warning(
                "Markdown negotiation skipped: %s exceeds %d bytes", request.path, MARKDOWN_MAX_HTML_BYTES
            )
            return response

        markdown = html_to_markdown(html)
        response.set_data(markdown)
        response.headers["Content-Type"] = "text/markdown; charset=utf-8"
        response.headers["x-markdown-tokens"] = str(estimate_tokens(markdown))

        # Union (do not overwrite) so Accept survives the app-level Vary hook
        # regardless of after_request execution order, and caches never cross-serve
        # Markdown to a browser or HTML to an agent.
        vary_parts = {p.strip() for p in response.headers.get("Vary", "").split(",") if p.strip()}
        vary_parts.add("Accept")
        response.headers["Vary"] = ", ".join(sorted(vary_parts))
    except Exception as exc:  # never break the page over a conversion failure
        current_app.logger.warning("Markdown negotiation failed for %s: %s", request.path, exc, exc_info=True)
    return response


# Sitemap routes


@frontend_bp.route("/sitemap.xml")
def sitemap_index():
    """Sitemap index that points to segmented sitemap files."""
    static_lastmod = _static_content_lastmod()
    try:
        product_lastmod_raw = db.session.query(func.max(func.coalesce(Product.updated_at, Product.created_at))).scalar()
        blog_lastmod_raw = (
            db.session.query(func.max(BlogPost.published_at))
            .filter(BlogPost.status == BlogStatus.PUBLISHED, BlogPost.published_at <= datetime.now(UTC))
            .scalar()
        )
    except Exception:
        product_lastmod_raw = None
        blog_lastmod_raw = None

    product_lastmod = _format_lastmod(product_lastmod_raw) or static_lastmod
    blog_lastmod = _format_lastmod(blog_lastmod_raw) or static_lastmod
    entries = [
        {"loc": _absolute_public_url("/sitemap-static.xml"), "lastmod": static_lastmod},
        {"loc": _absolute_public_url("/sitemap-products.xml"), "lastmod": product_lastmod},
        {"loc": _absolute_public_url("/sitemap-blog.xml"), "lastmod": blog_lastmod},
    ]
    return _render_sitemap_index(entries)


@frontend_bp.route("/sitemap-static.xml")
def sitemap_static():
    """Sitemap for static/public marketing pages."""
    static_lastmod = _static_content_lastmod()
    entries = [
        {
            "loc": _absolute_public_url(path),
            "lastmod": static_lastmod,
            "changefreq": "weekly",
            "priority": "0.8" if path == "/" else "0.6",
        }
        for path in STATIC_PUBLIC_PATHS
    ]
    return _render_sitemap_urlset(entries)


@frontend_bp.route("/sitemap-products.xml")
def sitemap_products():
    """Sitemap for active product pages."""
    entries = [
        {
            "loc": _absolute_public_url(product_path),
            "lastmod": _format_lastmod(getattr(product, "updated_at", None) or getattr(product, "created_at", None)),
            "changefreq": "weekly",
            "priority": "0.7",
        }
        for product_path, product in public_product_paths()
    ]
    return _render_sitemap_urlset(entries)


@frontend_bp.route("/sitemap-blog.xml")
def sitemap_blog():
    """Sitemap for published blog pages."""
    entries = [
        {
            "loc": _absolute_public_url(post_path),
            "lastmod": _format_lastmod(post.published_at or getattr(post, "updated_at", None)),
            "changefreq": "monthly",
            "priority": "0.6",
        }
        for post_path, post in public_blog_paths()
    ]
    return _render_sitemap_urlset(entries)


@frontend_bp.route("/feeds/google-products.xml")
def google_products_feed():
    """Google Merchant-compatible product feed (XML RSS)."""
    language = current_app.config.get("DEFAULT_LANGUAGE", "uz")
    offer = _offer_context()
    offer_by_id = offer["offer_by_id"]
    currency = offer["offer_facts"]["currency"]
    products = _offer_rows(offer_by_id)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
        "<channel>",
        f'<title>{escape("Aqua Element Product Feed")}</title>',
        f'<link>{escape(_absolute_public_url("/shop"))}</link>',
        f'<description>{escape("Aqua Element catalog for shopping discovery and product search.")}</description>',
    ]

    for product in products:
        product_data = product.to_dict(language=language)
        product_name = product_data.get("name") or f"Product {product.id}"
        product_description = (
            product_data.get("meta_description")
            or product_data.get("short_description")
            or product_data.get("description")
            or product_name
        )
        facts = offer_by_id[product.id]
        list_price = facts["list_price"]
        # Googles pair: the list price and, when discounted, the price an order is charged.
        feed_price = f"{_format_feed_price(list_price or facts['unit_price'])} {currency}"
        feed_sale_price = f"{_format_feed_price(facts['unit_price'])} {currency}"
        product_availability = "in stock" if facts["available"] else "out of stock"
        product_link = _absolute_public_url(facts["path"])

        images = product_data.get("images") or []
        image_link = (
            _as_absolute_url(images[0]) if images else _as_absolute_url(url_for("static", filename="images/logo.png"))
        )
        additional_images = []
        for image in images[1:11]:
            absolute_image = _as_absolute_url(image)
            if absolute_image:
                additional_images.append(absolute_image)
        mpn = (product.sku or "").strip() if getattr(product, "sku", None) else None
        gtin = _normalize_feed_gtin(getattr(product, "barcode", None))
        identifier_exists = bool(mpn or gtin)

        lines.extend(
            [
                "<item>",
                f"<g:id>{product.id}</g:id>",
                f"<title>{escape(str(product_name))}</title>",
                f"<description>{escape(str(product_description))}</description>",
                f"<link>{escape(product_link)}</link>",
                f"<g:price>{feed_price}</g:price>",
                f"<g:availability>{product_availability}</g:availability>",
                "<g:condition>new</g:condition>",
                f'<g:brand>{escape("Aqua Element")}</g:brand>',
                f'<g:google_product_category>{escape("Food, Beverages & Tobacco > Beverages > Water")}</g:google_product_category>',  # noqa: E501
            ]
        )

        if list_price:
            lines.append(f"<g:sale_price>{feed_sale_price}</g:sale_price>")

        if image_link:
            lines.append(f"<g:image_link>{escape(image_link)}</g:image_link>")

        for additional_image in additional_images:
            lines.append(f"<g:additional_image_link>{escape(additional_image)}</g:additional_image_link>")

        if product_data.get("category") and product_data["category"].get("name"):
            lines.append(f'<g:product_type>{escape(str(product_data["category"]["name"]))}</g:product_type>')

        if mpn:
            lines.append(f"<g:mpn>{escape(mpn)}</g:mpn>")
        if gtin:
            lines.append(f"<g:gtin>{escape(gtin)}</g:gtin>")
        lines.append(f'<g:identifier_exists>{"yes" if identifier_exists else "no"}</g:identifier_exists>')

        lines.append("</item>")

    lines.extend(["</channel>", "</rss>"])
    return Response("\n".join(lines), mimetype="application/xml")


def _check_delivery_response(payload, status=200):
    """JSON response for the public check-delivery endpoint (CORS + short cache)."""
    resp = current_app.response_class(
        response=json.dumps(payload, ensure_ascii=False),
        status=status,
        mimetype="application/json; charset=utf-8",
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"  # AI bots / cross-origin checker
    if status == 200:
        resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


CHECK_DELIVERY_ADDRESS_RATE_LIMIT = "60/hour"  # each address is an upstream geocoder call
CHECK_DELIVERY_COORDINATES_RATE_LIMIT = "600/hour"  # a point-in-polygon test, no upstream call


def _check_delivery_rate_limit():
    """Coordinates and addresses count in separate buckets (the limiter keys a bucket by its limit). The /coverage map
    asks for every pin a visitor drops (the zone is decided here, not in the page's JS), and those cheap checks must
    not use up the geocoded address checks of the visitors behind the same IP."""
    if request.args.get("lat") is not None and request.args.get("lng") is not None:
        return CHECK_DELIVERY_COORDINATES_RATE_LIMIT
    return CHECK_DELIVERY_ADDRESS_RATE_LIMIT


@frontend_bp.route("/api/public/check-delivery")
@limiter.limit(_check_delivery_rate_limit)
def public_check_delivery():
    """Public 'do you deliver to X?' check — powers the /coverage checker AND
    gives AI agents an authoritative address->deliverable answer.

    Query params (one form):
      - ``address=<text>``        : geocoded server-side via MapsService, then zone-checked.
      - ``lat=<float>&lng=<float>`` : checked directly.

    Always returns 200 for resolvable input and a graceful 200 (is_deliverable
    null + reason) when geocoding finds nothing or fails — never a 500 to agents.
    Missing input -> 200 with is_deliverable null (agent-friendly discovery).
    Invalid coordinates -> 400 with is_deliverable null.
    """
    from shared.constants import is_within_tashkent  # get_delivery_coverage is imported at module top

    language = get_current_language()
    coverage = get_delivery_coverage(language)
    coverage_block = {
        "summary": coverage["summary"],
        "city": coverage["city"],
        "regionNote": coverage["region_note"],
        "coverageUrl": "/coverage",
    }

    address = (request.args.get("address") or "").strip()[:256]  # cap length on a public endpoint
    lat_raw = request.args.get("lat")
    lng_raw = request.args.get("lng")

    latitude = longitude = None
    formatted_address = None
    reason = None

    if lat_raw is not None and lng_raw is not None:
        try:
            latitude, longitude = float(lat_raw), float(lng_raw)
        except (TypeError, ValueError):
            return _check_delivery_response(
                {
                    "query": {"lat": lat_raw, "lng": lng_raw},
                    "latitude": None,
                    "longitude": None,
                    "is_deliverable": None,
                    "reason": "invalid_coordinates",
                    "coverage": coverage_block,
                },
                status=400,
            )
    elif address:
        from business_app.services.maps_service import MapsService

        try:
            result = MapsService().geocode_address(address, city="Tashkent")
        except Exception as exc:  # geocoder/provider failure must not 500
            current_app.logger.warning("check-delivery geocode failed for %r: %s", address, exc)
            result = None
        if result and result.get("latitude") is not None and result.get("longitude") is not None:
            latitude, longitude = float(result["latitude"]), float(result["longitude"])
            formatted_address = result.get("formatted_address")
        else:
            reason = "not_geocoded"
    else:
        return _check_delivery_response(
            {
                "query": {},
                "latitude": None,
                "longitude": None,
                "is_deliverable": None,
                "reason": "missing_input",
                "coverage": coverage_block,
            },
            status=200,
        )

    is_deliverable = None
    if latitude is not None and longitude is not None:
        is_deliverable = bool(is_within_tashkent(latitude, longitude))

    payload = {
        "query": {"address": address or None, "lat": latitude, "lng": longitude},
        "latitude": latitude,
        "longitude": longitude,
        "is_deliverable": is_deliverable,
        "formatted_address": formatted_address,
        "coverage": coverage_block,
    }
    if reason:
        payload["reason"] = reason
    return _check_delivery_response(payload, status=200)


@frontend_bp.route("/api/public/products.json")
def public_products_feed():
    """Public, machine-readable product feed (Schema.org-flavoured JSON).

    Stable URL referenced from /llms.txt so AI assistants (ChatGPT, Gemini,
    Perplexity, Claude) can ingest the full SKU list with mineral profile
    + brand authority context in a single request, without scraping HTML.

    Differs from /feeds/google-products.xml in that:
      - It is JSON, not RSS-shopping XML.
      - It bundles brand authority (Wikidata Q-id, regulator) and source-water
        / mineral-profile context alongside the products themselves, so an
        LLM can answer "is Aqua Element water mineralised?" from this single
        feed without follow-up fetches.
      - All translatable fields are emitted in all three languages
        (uz/ru/en) so an assistant can quote in the user's language.

    Caching: `public, no-cache` -- the feed is revalidated on every request
    so a price change shows at once, and the word `public` keeps the
    login-cookie refresh off it. Prices, sizes and availability come from
    the public offer facts (`get_public_offer_facts`), which list only the
    active products, so deactivated SKUs are never exposed.
    """
    offer = _offer_context()
    offer_facts = offer["offer_facts"]
    offer_by_id = offer["offer_by_id"]
    products = _offer_rows(offer_by_id)

    # Resolve site-wide identity from the same context the JSON-LD blocks
    # use, so the feed stays consistent with what's emitted on every page.
    company_website = current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz")
    wikidata_id = current_app.config.get("COMPANY_WIKIDATA_ID", "")
    same_as = []
    if wikidata_id:
        same_as.append(f"https://www.wikidata.org/wiki/{wikidata_id}")
    for env_key in ("COMPANY_GOOGLE_BUSINESS_URL", "COMPANY_YANDEX_BUSINESS_URL", "COMPANY_WIKIPEDIA_URL"):
        val = current_app.config.get(env_key, "")
        if val:
            same_as.append(val)

    # The 11-stage process and mineral profile are brand facts, published here so a single feed fetch grounds an LLM
    # answer about water quality without a follow-up scrape of the pillar page. One source (shared/water_facts.py)
    # for this feed and llms.txt, held to the pages' rows by tests/integration/test_water_facts_single_source.py.
    treatment_stages = [stage.name for stage in TREATMENT_STAGES]
    mineral_profile = mineral_profile_feed()

    products_payload: list[dict] = []  # type: ignore[name-defined]
    for product in products:
        # Fetch translations across all three languages so AI assistants can
        # quote whichever matches the user's locale.
        i18n_description = {lang: product.get_translated("description", lang) for lang in ("uz", "ru", "en")}
        i18n_short_description = {}
        # ``short_description`` is translatable on Product per current schema;
        # fall back gracefully if the field happens to be missing on a row.
        for lang in ("uz", "ru", "en"):
            try:
                i18n_short_description[lang] = product.get_translated("short_description", lang)
            except Exception:
                i18n_short_description[lang] = None

        facts = offer_by_id[product.id]
        product_url = _absolute_public_url(facts["path"])

        product_dict = product.to_dict(language="uz")
        images = product_dict.get("images") or []
        absolute_images = [_as_absolute_url(img) for img in images if img]
        absolute_images = [img for img in absolute_images if img]

        litres = facts["litres"]
        offers = build_offer_jsonld(facts, url=product_url)
        if facts["list_price"] is not None:
            offers["listPrice"] = _json_number(facts["list_price"])
        products_payload.append(
            {
                "@type": "Product",
                "id": product.id,
                "sku": getattr(product, "sku", None),
                "name": facts["name"],
                "description": i18n_description,
                "shortDescription": i18n_short_description,
                # The volume only: no label without one (the trade size is never published as a size).
                "size": {
                    "label": f"{_json_number(litres)}L" if litres is not None else None,
                    "litres": _json_number(litres) if litres is not None else None,
                },
                # From the ledger's own rule, not from the size string — see
                # `_build_dual_sku_product_group` for why the two questions are
                # kept apart.
                "returnable": facts["returnable"],
                "url": product_url,
                "images": absolute_images,
                "offers": offers,
                "priceText": facts["price_text"],
                "minOrderQuantity": facts["smallest_order_quantity"],
                "minOrderTotal": _json_number(facts["smallest_order_total"]),
                "minOrderTotalText": facts["smallest_order_total_text"],
                # Only when true: a `false` would read as "a deposit applies", and no bottle carries one.
                **({"noDeposit": True} if facts["no_deposit"] else {}),
                "brand": "Aqua Element",
                "category": "Bottled artesian water",
            }
        )

    coverage_en = get_delivery_coverage("en")
    feed = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Aqua Element — public product feed",
        "description": (
            "Stable, machine-readable feed of all active Aqua Element SKUs "
            "with brand authority, source-water context, mineral profile, "
            "and per-language descriptions. Linked from /llms.txt for AI-"
            "assistant ingestion."
        ),
        "url": _absolute_public_url("/api/public/products.json"),
        "lastUpdated": datetime.now(UTC).isoformat(),
        "brand": {
            "@type": "Brand",
            "name": current_app.config.get("COMPANY_NAME", "Aqua Element"),
            "url": company_website,
            "sameAs": same_as,
        },
        "regulator": {
            "name": (
                "Committee of the Republic of Uzbekistan for Sanitary and "
                "Epidemiological Wellbeing and Public Health"
            ),
            "shortName": "Sanepid",
            "country": "Uzbekistan",
        },
        "sourceWater": {
            "type": "Artesian water from a confined aquifer",
            "location": "Quyi Chirchiq District, Tashkent Region, Uzbekistan",
            "wellDepthMetres": WELL_DEPTH_METRES,
            "treatmentStages": treatment_stages,
        },
        "mineralProfile": mineral_profile,
        "serviceArea": {
            "country": "UZ",
            "city": "Tashkent",
            "regionNote": coverage_en["region_note"],
            "districts": [d["name"] for d in coverage_en["districts"]],
            "coverageUrl": "/coverage",
        },
        "orderChannels": _order_channels(company_website),
        "supportedLanguages": ["uz", "ru", "en"],
        "pricesAsOf": offer_facts["as_of"],
        "minOrderAmount": _json_number(offer_facts["min_order_amount"]),
        "delivery": {
            "fee": _json_number(offer_facts["delivery"]["fee"]),
            "currency": offer_facts["currency"],
            "free": offer_facts["delivery"]["free"],
            "everyDay": offer_facts["delivery"]["every_day"],
            "statement": {lang: list(sentences) for lang, sentences in offer_facts["delivery"]["statement"].items()},
            "statementReviewedOn": offer_facts["delivery"]["statement_reviewed_on"],
        },
        "totalProducts": len(products_payload),
        "products": products_payload,
    }

    response = current_app.response_class(
        response=__import__("json").dumps(feed, ensure_ascii=False, indent=2),
        status=200,
        mimetype="application/json; charset=utf-8",
    )
    response.headers["Cache-Control"] = "public, no-cache"
    response.headers["Access-Control-Allow-Origin"] = "*"  # AI bots fetch cross-origin
    return response


@frontend_bp.route("/api/public/loyalty.json")
def public_loyalty_feed():
    """Public, machine-readable loyalty-program feed (Schema.org MemberProgram).

    Stable URL referenced from /llms.txt and the api-catalog so AI assistants
    (ChatGPT, Gemini, Perplexity, Claude, Yandex) can ingest the Aqua Club
    program — tiers, earn rules, streak bonuses, reward types — in one request,
    in all three languages, with brand-authority context. Rewards-only: no
    points->cash rate; the surprise reward is a boolean.
    """
    facts = get_public_loyalty_facts()
    company_website = current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz")

    wikidata_id = current_app.config.get("COMPANY_WIKIDATA_ID", "")
    same_as = []
    if wikidata_id:
        same_as.append(f"https://www.wikidata.org/wiki/{wikidata_id}")
    for env_key in ("COMPANY_GOOGLE_BUSINESS_URL", "COMPANY_YANDEX_BUSINESS_URL", "COMPANY_WIKIPEDIA_URL"):
        val = current_app.config.get(env_key, "")
        if val:
            same_as.append(val)

    prog = facts["program"]
    feed = {
        "@context": "https://schema.org",
        "@type": "MemberProgram",
        "@id": f"{company_website}#aquaclub",
        "name": f"{current_app.config.get('COMPANY_NAME', 'Aqua Element')} — Aqua Club",
        "description": (
            "Aqua Club is the Aqua Element customer loyalty programme. Customers "
            "earn AquaCoins on every delivered, paid order, climb tiers for higher "
            "earn multipliers and tier discounts on orders placed in our Telegram bot or "
            "on our website, and redeem AquaCoins for rewards. "
            "AquaCoins are reward points, not cash, and are not exchangeable for money."
        ),
        "url": _absolute_public_url("/api/public/loyalty.json"),
        "guideUrl": _absolute_public_url("/loyalty-guide"),
        "lastUpdated": datetime.now(UTC).isoformat(),
        "brand": {
            "@type": "Brand",
            "name": current_app.config.get("COMPANY_NAME", "Aqua Element"),
            "url": company_website,
            "sameAs": same_as,
        },
        "currencyDisplayName": "AquaCoins",
        "earnRules": {
            "uzsPerAquaCoin": prog["uzs_per_point"],
            "signupBonus": prog["signup_bonus"],
            "referralBonusReferrer": prog["referral_bonus"],
            "referralBonusReferee": prog["referee_bonus"],
            "birthdayBonus": prog["birthday_bonus"],
            "expiryDays": prog["expiry_days"],
            "minRedemptionAquaCoins": prog["min_redemption_points"],
            "surpriseRewardEnabled": prog["surprise_enabled"],
        },
        "tiers": [
            {
                "@type": "MemberProgramTier",
                "key": t["key"],
                "order": t["order"],
                "name": t["name"],
                "minAquaCoins": t["min_points"],
                "maxAquaCoins": t["max_points"],
                "earnMultiplier": t["multiplier"],
                "tierDiscountPercent": t["discount_percentage"],
            }
            for t in facts["tiers"]
        ],
        "streakBonuses": facts["streak_rules"],
        "consecutiveStrikeBonuses": facts["consecutive_strike_rules"],
        "rewards": facts["rewards"],
        "supportedLanguages": ["uz", "ru", "en"],
        "orderChannels": _order_channels(company_website),
    }

    response = current_app.response_class(
        response=json.dumps(feed, ensure_ascii=False, indent=2),
        status=200,
        mimetype="application/json; charset=utf-8",
    )
    response.headers["Cache-Control"] = "public, max-age=900"  # 15 min
    response.headers["Access-Control-Allow-Origin"] = "*"  # AI bots fetch cross-origin
    return response


# Custom template filters


# One composer per fact sentence: templates call the service's functions, they never fill the keys themselves.
frontend_bp.add_app_template_global(price_sentence, name="offer_price_sentence")
frontend_bp.add_app_template_global(smallest_order_sentence, name="offer_smallest_order_sentence")


@frontend_bp.app_template_filter("format_price")
def format_price(amount):
    """Format price with proper formatting"""
    return display_labels.money(amount)


# The number the Offer JSON-LD prints, for a template that states a price as a plain number (`llms.txt`).
frontend_bp.add_app_template_filter(_json_number, "json_number")


@frontend_bp.route("/docs/api")
def api_documentation():
    """Serve API documentation"""
    from pathlib import Path

    docs_path = Path(__file__).parent.parent.parent / "docs" / "API_DOCUMENTATION.md"

    if docs_path.exists():
        with open(docs_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Convert markdown to HTML for better display
        try:
            import markdown

            html_content = markdown.markdown(content, extensions=["codehilite", "tables", "toc"])
        except ImportError:
            # Fallback to plain text if markdown not available
            html_content = f"<pre>{content}</pre>"

        return render_template("frontend/api_docs.html", content=html_content)
    else:
        return "API Documentation not found", 404


# ============================================================================
# BLOG ROUTES
# ============================================================================


def _blog_category_label(category, language):
    """A blog category's display name, from the one source cards and article pages read (get_category_display)."""
    return BlogPost(category=category).get_category_display(language)


def _blog_categories(published, language):
    """The categories that have a published post, in enum order, each with its label and post count (the filter)."""
    from business_app.models.blog import BlogCategory

    counts = dict(
        db.session.query(BlogPost.category, func.count(BlogPost.id))
        .filter(*published)
        .group_by(BlogPost.category)
        .all()
    )
    return [
        {"value": c.value, "label": _blog_category_label(c, language), "count": counts[c]}
        for c in BlogCategory
        if counts.get(c)
    ]


@frontend_bp.route("/blog")
def blog_list():
    """Blog listing page with filtering"""
    from business_app.models.blog import BlogCategory

    language = get_current_language()
    page = request.args.get("page", 1, type=int)
    category = request.args.get("category", None)
    tag = request.args.get("tag", None)
    per_page = 9

    # Build query for published posts
    published = (BlogPost.status == BlogStatus.PUBLISHED, BlogPost.published_at <= datetime.now(UTC))
    query = BlogPost.query.filter(*published)

    # Apply filters. An unknown category is ignored, and dropped from the page's links and canonical.
    category_label = None
    if category:
        try:
            category_enum = BlogCategory(category)
            query = query.filter(BlogPost.category == category_enum)
            category_label = _blog_category_label(category_enum, language)
        except ValueError:
            category = None

    if tag:
        query = query.filter(BlogPost.tags.ilike(f"%{tag}%"))

    # Order by published date
    query = query.order_by(desc(BlogPost.published_at))

    # Paginate. A page past the last one (or below 1) does not exist: 404, not an empty listing that reads as an empty
    # category. Page 1 of an empty category stays a 200 with its empty state (noindex).
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    if page < 1 or (page > 1 and page > pagination.pages):
        abort(404)

    base_listing_params = {}
    if category:
        base_listing_params["category"] = category
    if tag:
        base_listing_params["tag"] = tag

    canonical_params = {}
    if category:
        canonical_params["category"] = category
    if tag:
        canonical_params["tag"] = tag
    if page > 1:
        canonical_params["page"] = page

    prev_page_url = None
    next_page_url = None
    if pagination.has_prev:
        prev_page_url = _build_external_url("frontend.blog_list", page=pagination.prev_num, **base_listing_params)
    if pagination.has_next:
        next_page_url = _build_external_url("frontend.blog_list", page=pagination.next_num, **base_listing_params)

    return render_template(
        "frontend/blog_list.html",
        posts=pagination.items,
        pagination=pagination,
        current_category=category,
        current_category_label=category_label,
        blog_categories=_blog_categories(published, language),
        current_tag=tag,
        canonical_url=_build_external_url("frontend.blog_list", **canonical_params),
        prev_page_url=prev_page_url,
        next_page_url=next_page_url,
    )


@frontend_bp.route("/blog/<slug>")
def blog_detail(slug):
    """Blog detail page"""
    language = get_current_language()

    # Find post by slug
    post = BlogPost.query.filter(
        BlogPost.slug == slug, BlogPost.status == BlogStatus.PUBLISHED, BlogPost.published_at <= datetime.now(UTC)
    ).first_or_404()

    # Increment view count
    try:
        post.increment_views()
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Error incrementing blog view count: {e}")
        db.session.rollback()

    # Related posts: three others, the same category's newest first, then the newest of the rest, so the row stays full
    recent_posts = (
        BlogPost.query.filter(
            BlogPost.status == BlogStatus.PUBLISHED,
            BlogPost.published_at <= datetime.now(UTC),
            BlogPost.id != post.id,
        )
        .order_by(case((BlogPost.category == post.category, 0), else_=1), desc(BlogPost.published_at))
        .limit(3)
        .all()
    )

    # Add contextual product recommendations for stronger blog-to-product linking.
    recommended_products = []
    raw_tags = post.tags.split(",") if post.tags else []
    normalized_tags = [tag.strip() for tag in raw_tags if tag.strip()]

    if normalized_tags:
        tag_conditions = []
        for tag in normalized_tags[:6]:
            pattern = f"%{tag}%"
            tag_conditions.extend(
                [
                    Product.name.ilike(pattern),
                    Product.short_description.ilike(pattern),
                    Product.description.ilike(pattern),
                ]
            )
        if tag_conditions:
            recommended_products = (
                Product.query.filter(Product.is_active == True, or_(*tag_conditions))
                .order_by(desc(Product.is_featured), desc(Product.created_at))
                .limit(4)
                .all()
            )

    if len(recommended_products) < 4:
        existing_ids = {product.id for product in recommended_products}
        fallback_query = Product.query.filter(Product.is_active == True)
        if existing_ids:
            fallback_query = fallback_query.filter(~Product.id.in_(existing_ids))
        fallback_products = (
            fallback_query.order_by(desc(Product.is_featured), desc(Product.created_at))
            .limit(4 - len(recommended_products))
            .all()
        )
        recommended_products.extend(fallback_products)

    return render_template(
        "frontend/blog_detail.html",
        post=post,
        recent_posts=recent_posts,
        recommended_products=[product.to_dict(language=language) for product in recommended_products],
        canonical_url=_build_external_url("frontend.blog_detail", slug=post.slug),
        **_offer_context(),
    )
