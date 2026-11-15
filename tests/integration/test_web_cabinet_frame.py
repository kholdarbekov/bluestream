"""The cabinet frame and the account dashboard after the redesign (website redesign step 3, group "cabinet", unit
"cabinet-frame", 2026-11-14).

Pages are fetched the way a signed-in customer's browser fetches them: the httpOnly `access_token_cookie` the
storefront login sets, in uz, ru and en, with the rows the seeds ship (the backend seed's `landing.*` rows and the
group's `scripts/seed_web_ui_cabinet_translations.py`).

- The dashboard's order count is every order the customer has, the number "My orders" pages through
  (GET /api/v1/orders publishes it as `pagination.total`), not the five the dashboard lists (owner decision 4).
- An order's status reads in the page's language, in the tone macros/order_status.html gives it: that file is the one
  mapping, and the islands orders.js and order-detail-modal.js draw from carry exactly that mapping.
- The order list the website draws (GET /api/v1/orders, GET /api/v1/orders/<id>) names each item in the request's
  language and carries its picture (serializers/order_serializers.py `serialize_order_item`).
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token

from business_app.models.order import Order, OrderItem
from business_app.models.translation import Translation
from scripts.seed_web_ui_cabinet_translations import CATEGORY, TRANSLATIONS as CABINET
from shared.enums import OrderStatus, PaymentMethod
from tests.integration.public_page_builders import flat, seed_copy, shipped
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
STATUS_MACRO = "frontend/macros/order_status.html"
# Every page that extends cabinet_base.html, with the tab it marks as current.
CABINET_PAGES = (
    "/my-account",
    "/profile-settings",
    "/my-orders",
    "/addresses",
    "/my-subscriptions",
    "/my-loyalty",
    "/account-security",
)


@pytest.fixture
def cabinet_copy(app, db):
    """The storefront rows the backend seed ships (landing.*, money labels) plus the cabinet group's own rows."""
    seed_copy(db, ("landing.", "web.display_labels", "cabinet."))
    for language, rows in CABINET.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _browser(app, user):
    """The customer's browser after the storefront login (cookies, as base.js apiRequest sends them)."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    browser, _csrf = _logged_in_browser(app, token)
    return browser


def _get(app, db, user, path, lang):
    """GET `path` in `lang` as the customer's browser does. The language is also their stored preference, so the
    `?lang=` redirect of the default language cannot hand the page to another one."""
    user.preferred_language = lang
    db.session.commit()
    response = _browser(app, user).get(f"{path}?lang={lang}", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html[:500]
    return BeautifulSoup(html, "html.parser"), html


def _orders(db, user, count, *, status=OrderStatus.CONFIRMED):
    """`count` orders for `user`, one minute apart, the newest last."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    orders = []
    for n in range(count):
        order = Order(
            user_id=user.id,
            order_number=f"CAB_{n:06d}_26",
            status=status,
            payment_method=PaymentMethod.CASH,
            subtotal=Decimal("19000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("19000.00"),
            created_at=start + timedelta(minutes=n),
        )
        db.session.add(order)
        orders.append(order)
    db.session.commit()
    return orders


def _island(soup, element_id):
    return json.loads(soup.find("script", id=element_id).string)


# --- the dashboard's numbers ------------------------------------------------------------------------------------------


def test_the_dashboard_counts_every_order_my_orders_lists_not_the_five_it_shows(app, db, sample_user, cabinet_copy):
    orders = _orders(db, sample_user, 7)

    soup, _ = _get(app, db, sample_user, "/my-account", "ru")
    listed = _browser(app, sample_user).get("/api/v1/orders/", headers=WEB_HEADERS)

    assert listed.status_code == 200, listed.get_json()
    assert listed.get_json()["data"]["pagination"]["total"] == 7
    assert soup.select_one("#totalOrdersCount").get_text(strip=True) == "7"
    # The tile is a link to the list it counts.
    assert soup.select_one("#totalOrdersCount").find_parent("a")["href"] == "/my-orders"
    # The newest five, newest first, each opening the order modal.
    rows = soup.select('.recent-orders-section [data-action="show-order"]')
    assert [int(row["data-order-id"]) for row in rows] == [order.id for order in reversed(orders)][:5]


def test_the_count_tiles_are_links_with_server_rendered_numbers(app, db, sample_user, user_address, cabinet_copy):
    soup, _ = _get(app, db, sample_user, "/my-account", "en")

    tiles = {a["href"]: flat(a.select_one(".ae-stat__value").get_text()) for a in soup.select(".ae-dash__stats a")}
    assert tiles["/my-orders"] == "0"
    assert tiles["/my-subscriptions"] == "0"
    # The addresses page's rows (GET /api/v1/auth/addresses): drawn by the server, no "0" that changes later.
    assert tiles["/addresses"] == "1"


# --- the dashboard in each language -----------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_dashboard_reads_in_the_visitors_language(app, db, sample_user, cabinet_copy, lang):
    [order] = _orders(db, sample_user, 1, status=OrderStatus.OUT_FOR_DELIVERY)

    soup, html = _get(app, db, sample_user, "/my-account", lang)

    assert "web.cabinet." not in html, "a web.cabinet key reached the page untranslated"
    assert flat(soup.select_one(".page-title h1").get_text()) == shipped("landing.nav.my_account", lang)
    assert flat(soup.select_one(".ae-page-head__sub").get_text()) == CABINET[lang]["web.cabinet.dashboard.lead"]
    [row] = soup.select('.recent-orders-section [data-action="show-order"]')
    assert flat(row.select_one(".ae-orow__num").get_text()) == CABINET[lang]["web.cabinet.order_number"].format(
        number=order.order_number
    )
    pill = row.select_one(".ae-status")
    assert flat(pill.get_text()) == CABINET[lang]["web.cabinet.status.out_for_delivery"]
    assert pill["class"] == ["ae-status", "ae-status--progress", "status-out_for_delivery"]
    # The date: the order's day in Tashkent (base.js rewrites it in words from the datetime attribute).
    assert row.select_one("time")["datetime"] == order.created_at_utc.isoformat()
    # The page's own assets: the group stylesheet and the scripts, each with its ?v= so a deploy never serves stale ones.
    assert any("css/pages/cabinet.css?v=" in link["href"] for link in soup.select('link[rel="stylesheet"]'))
    scripts = [s["src"] for s in soup.select("script[src]")]
    assert any("js/pages/cabinet.js?v=" in src for src in scripts)
    assert any("js/pages/my-account.js?v=" in src for src in scripts)


@pytest.mark.parametrize("lang", ("ru", "en"))
def test_the_home_crumb_keeps_the_language(app, db, sample_user, cabinet_copy, lang):
    soup, _ = _get(app, db, sample_user, "/my-account", lang)

    home, current = soup.select(".page-title .bread-crumb li")
    assert f"lang={lang}" in home.select_one("a")["href"]
    # The dashboard is the current page: its crumb is not a link to itself.
    assert current.select_one("a") is None
    assert current.select_one('[aria-current="page"]') is not None


def test_the_dashboard_with_no_orders_offers_to_order(app, db, sample_user, cabinet_copy):
    soup, _ = _get(app, db, sample_user, "/my-account", "ru")

    empty = soup.select_one(".recent-orders-section .ae-empty")
    assert flat(empty.select_one(".ae-empty__title").get_text()) == CABINET["ru"]["web.cabinet.orders.empty_title"]
    assert empty.select_one("a")["href"] == "/shop"
    assert soup.select('[data-action="show-order"]') == []


# --- the email prompt -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email, verified, prompted",
    [(None, False, False), ("ali@example.com", False, True), ("ali@example.com", True, False)],
    ids=["no-email", "unverified", "verified"],
)
def test_the_email_prompt_is_an_inline_note_only_for_a_real_unverified_email(
    app, db, sample_user, cabinet_copy, email, verified, prompted
):
    sample_user.email = email
    sample_user.email_verified_at = datetime.now(timezone.utc) if verified else None
    db.session.commit()

    soup, _ = _get(app, db, sample_user, "/my-account", "uz")

    alert = soup.select_one(".ae-dash .ae-alert")
    assert (alert is not None) is prompted
    if prompted:
        assert flat(alert.get_text(" ")).startswith(CABINET["uz"]["web.cabinet.dashboard.verify_email"])
        assert alert.select_one("a")["href"] == "/verify-email"


# --- the frame around every cabinet page ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", CABINET_PAGES)
def test_every_cabinet_page_sits_in_the_frame_with_its_own_tab_current(app, db, sample_user, cabinet_copy, path):
    soup, html = _get(app, db, sample_user, path, "ru")

    nav = soup.select_one(".ae-cabinet .ae-cabinet-nav")
    current = nav.select('[aria-current="page"]')
    assert [a["href"] for a in current] == [path]
    assert "web.cabinet." not in html
    # The eight links (seven pages and the logout base.js binds), and the content column beside them.
    assert len(nav.select(".account-menu li")) == 8
    assert nav.select_one('[data-action="logout"]') is not None
    assert soup.select_one(".ae-cabinet > .ae-cabinet__main") is not None
    assert any("css/pages/cabinet.css?v=" in link["href"] for link in soup.select('link[rel="stylesheet"]'))


# --- order status: one mapping ----------------------------------------------------------------------------------------


def _status_mapping(app):
    with app.app_context():
        return app.jinja_env.get_template(STATUS_MACRO).module.ORDER_STATUS_TONES


def test_every_order_status_has_one_tone_and_words_in_all_three_languages(app):
    tones = _status_mapping(app)

    assert set(tones) == {status.value for status in OrderStatus} | {"awaiting_new_date"}
    for code in set(tones) - {"awaiting_new_date"}:  # the wait keeps its own, older key
        for language in LANGS:
            assert CABINET[language].get(f"web.cabinet.status.{code}"), (code, language)


def test_the_js_islands_carry_the_macros_mapping(app, db, sample_user, cabinet_copy):
    """orders.js and order-detail-modal.js colour and word a status from their islands: the same mapping and words
    the server-drawn pills use, so the dashboard, the list and the modal never disagree."""
    tones = _status_mapping(app)

    orders_page, _ = _get(app, db, sample_user, "/my-orders", "ru")
    account_page, _ = _get(app, db, sample_user, "/my-account", "ru")

    for soup, element_id in (
        (orders_page, "page-data"),
        (orders_page, "order-detail-modal-data"),
        (account_page, "order-detail-modal-data"),
    ):
        data = _island(soup, element_id)
        assert data["status_tones"] == tones, element_id
        labels = data["i18n"]["status_labels"]
        assert {code: labels[code] for code in tones if code != "awaiting_new_date"} == {
            code: CABINET["ru"][f"web.cabinet.status.{code}"] for code in tones if code != "awaiting_new_date"
        }, element_id


# --- what the order list publishes for each item ----------------------------------------------------------------------


def test_the_orders_api_names_each_item_in_the_request_language_with_its_picture(
    app, db, sample_user, sample_product, cabinet_copy
):
    sample_product.images = ["/uploads/images/products/bottle_19.png", "/uploads/images/products/bottle_19_back.png"]
    Translation.set_entity_translation("Product", sample_product.id, "name", "ru", "Вода 19 литров")
    [order] = _orders(db, sample_user, 1)
    db.session.add(
        OrderItem(order_id=order.id, product_id=sample_product.id, quantity=2,
                  unit_price=Decimal("9500.00"), total_price=Decimal("19000.00"))
    )
    db.session.commit()
    browser = _browser(app, sample_user)

    # What orders.js asks for, then what order-detail-modal.js asks for.
    listed = browser.get("/api/v1/orders/?page=1&per_page=10&lang=ru", headers=WEB_HEADERS)
    opened = browser.get(f"/api/v1/orders/{order.id}?lang=ru", headers=WEB_HEADERS)

    assert listed.status_code == 200 and opened.status_code == 200, (listed.get_json(), opened.get_json())
    [row] = listed.get_json()["data"]["orders"]
    for item in (row["order_items"][0], opened.get_json()["data"]["order"]["order_items"][0]):
        assert item["product_display_name"] == "Вода 19 литров"
        # The stored name stays as it was for the bots that read it.
        assert item["product_name"] == "Pure Water 19L"
        assert item["product_image"] == "/uploads/images/products/bottle_19.png"


def test_an_item_whose_product_has_no_picture_or_translation_falls_back(app, db, sample_user, sample_product):
    sample_product.images = []
    [order] = _orders(db, sample_user, 1)
    db.session.add(
        OrderItem(order_id=order.id, product_id=sample_product.id, quantity=1,
                  unit_price=Decimal("19000.00"), total_price=Decimal("19000.00"))
    )
    db.session.commit()

    listed = _browser(app, sample_user).get("/api/v1/orders/?lang=en", headers=WEB_HEADERS)

    [item] = listed.get_json()["data"]["orders"][0]["order_items"]
    assert (item["product_display_name"], item["product_image"]) == ("Pure Water 19L", None)
