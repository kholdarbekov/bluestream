"""The cabinet after its independent review (website redesign step 3, group "cabinet", review-cabinet.json, 2026-11-14).

Everything is driven the way the customer's browser drives it: pages fetched with the storefront login cookie, API
calls with that cookie and the `X-CSRF-TOKEN` base.js apiRequest copies from `csrf_access_token`. Mocked: nothing
but Celery and notifications (external I/O), as the subscription tests do.

- CAB-R02: "Download my account data" (my-account.js) POSTs /api/v1/auth/export-data and saves the JSON attachment;
  it answered 500 for every customer. Fixed as a bug: same endpoint, same document.
- CAB-R01: the dashboard lists the active subscriptions the API lists (GET /subscriptions/?status=active), with the
  API's published next-delivery labels and per-delivery label; the count tile is that list's total.
- CAB-R03: one name across the cabinet: the greeting name (header, greeting, sidebar); an entity's person under it.
- CAB-R18: the dashboard head drops its bot button when a Telegram panel below already offers the bot.
- CAB-R09: /my-subscriptions names AquaCoins only for a customer the loyalty program serves.
- CAB-R05: an order's district is published by its name in the request's language, never the stored key.
- CAB-R04/R16: the order modal draws the vendored Leaflet through map-helper.js, with translated zoom buttons.
- CAB-R17: the subscriptions crumb names the page as its H1 does, marked aria-current.
"""

import json
from datetime import datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.user import UserAddress
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from shared.constants import get_district_name
from shared.enums import OrderStatus, PaymentMethod, UserType
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_cabinet_frame import _browser, _get, _island, cabinet_copy  # noqa: F401  (fixture)
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser
from tests.integration.test_web_subscription_builder_contract import (  # noqa: F401  (fixtures)
    FIXTURE,
    _body,
    _no_external_io,
    every_day_slot,
)

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
EXPORT_URL = "/api/v1/auth/export-data"


def _order(db, user, number, *, status=OrderStatus.DELIVERED, address=None):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=status,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal("38000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("38000.00"),
        delivery_address_id=address.id if address else None,
    )
    db.session.add(order)
    db.session.commit()
    return order


# --- CAB-R02: the account data download ---------------------------------------------------------------------------------


def test_download_my_account_data_answers_the_json_attachment_my_account_js_saves(
    app, client, db, sample_user, auth_headers, user_address, sample_product, every_day_slot
):
    """my-account.js: apiRequest('/auth/export-data', {method: 'POST'}) with no body, then response.blob().

    A real customer with orders, saved addresses and an active subscription. The fix is the 500 (review CAB-R02); the
    owner added subscriptions and payments on 2026-11-14 (their shapes: test_account_export_subscriptions_payments.py).
    The email is the stored one, as before.
    """
    body = _body(FIXTURE["cases"][0], address=user_address, slot=every_day_slot, product=sample_product)
    assert client.post("/api/v1/subscriptions/", headers=auth_headers, json=body).status_code == 201
    address = UserAddress(user_id=sample_user.id, title="Uy", full_address="Toshkent, Chilonzor 7, 12", is_default=True)
    db.session.add(address)
    db.session.commit()
    _order(db, sample_user, "EXP_000001_26", status=OrderStatus.DELIVERED, address=address)
    _order(db, sample_user, "EXP_000002_26", status=OrderStatus.CANCELLED)
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    browser, csrf = _logged_in_browser(app, token)

    response = browser.post(EXPORT_URL, headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf})

    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    assert response.mimetype == "application/json"
    assert response.headers["Content-Disposition"] == f"attachment; filename=account-data-{sample_user.id}.json"
    document = json.loads(response.get_data(as_text=True))
    assert set(document) == {"user_profile", "orders", "addresses", "subscriptions", "payments", "export_date"}
    profile = document["user_profile"]
    assert (profile["id"], profile["phone"], profile["email"]) == (sample_user.id, sample_user.phone, sample_user.email)
    assert profile["status"] == sample_user.status.value
    statuses = {o["order_number"]: o["status"] for o in document["orders"]}
    assert statuses["EXP_000001_26"] == "delivered" and statuses["EXP_000002_26"] == "cancelled"
    assert len(document["orders"]) == Order.query.filter_by(user_id=sample_user.id).count()
    assert all(isinstance(o["created_at"], str) and isinstance(o["platform"], str) for o in document["orders"])
    saved = {a["title"]: (a["full_address"], a["is_default"]) for a in document["addresses"]}
    assert saved["Uy"] == ("Toshkent, Chilonzor 7, 12", True)
    assert len(document["addresses"]) == UserAddress.query.filter_by(user_id=sample_user.id).count() == 2
    assert datetime.fromisoformat(document["export_date"]).utcoffset().total_seconds() == 0


def test_the_download_without_the_csrf_header_is_refused(app, db, sample_user):
    """Control: the POST another site could make the browser send carries the cookies but not the header."""
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    browser, _csrf = _logged_in_browser(app, token)

    response = browser.post(EXPORT_URL, headers=WEB_HEADERS)

    assert response.status_code in (401, 403)


# --- CAB-R01: the dashboard's active subscriptions ----------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_dashboard_lists_the_active_subscriptions_the_api_lists(
    app, client, db, auth_headers, user_address, sample_product, every_day_slot, sample_user, cabinet_copy, lang
):
    seed_copy(db, ("api.subscriptions.",))  # the schedule sentence's words
    body = _body(FIXTURE["cases"][0], address=user_address, slot=every_day_slot, product=sample_product)
    assert client.post("/api/v1/subscriptions/", headers=auth_headers, json=body).status_code == 201

    soup, _ = _get(app, db, sample_user, "/my-account", lang)
    listed = _browser(app, sample_user).get("/api/v1/subscriptions/?status=active", headers=WEB_HEADERS).get_json()
    (sub,) = listed["data"]["items"]

    section = soup.select_one(".active-subscriptions-section")
    assert section.select_one("h2").get_text(strip=True) == CABINET[lang]["web.cabinet.dashboard.stat_subscriptions"]
    (row,) = section.select("a.ae-srow")
    assert row["href"] == "/my-subscriptions"
    item = sub["subscription_items"][0]
    assert row.select_one(".ae-orow__num").get_text(strip=True) == f"{item['quantity']} × {item['product_name']}"
    # When: the next delivery's published date label, in the strip card's words ("Next: {date}").
    when = sub["upcoming_delivery"]["label_long"]
    assert row.select_one(".ae-orow__date").get_text(strip=True) == BACKEND_TRANSLATIONS["cabinet.subs.strip.next"][
        lang
    ].replace("{date}", when)
    assert "api.subscriptions" not in when and "{" not in when
    frequency = BACKEND_TRANSLATIONS.get(f"cabinet.subs.frequency.{sub['frequency']}", {}).get(lang)
    amount = row.select_one(".ae-orow__sum").get_text(" ", strip=True)
    assert amount.startswith(sub["estimated_per_delivery_label"]) and "UZS" not in amount
    if frequency:
        assert amount.endswith(f"/ {frequency}")
    # The tile counts what the list holds.
    tile = soup.select_one('a.ae-dash__stat[href="/my-subscriptions"] .ae-stat__value')
    assert tile.get_text(strip=True) == str(listed["meta"]["total"]) == "1"


def test_no_active_subscriptions_section_without_one(app, db, sample_user, cabinet_copy):
    soup, _ = _get(app, db, sample_user, "/my-account", "ru")

    assert soup.select_one(".active-subscriptions-section") is None
    assert soup.select_one('a.ae-dash__stat[href="/my-subscriptions"] .ae-stat__value').get_text(strip=True) == "0"


# --- CAB-R03 / R13: one name, and the sidebar is not a heading ----------------------------------------------------------


def test_the_sidebar_names_a_person_by_the_greeting_name_the_header_and_greeting_use(app, db, sample_user, cabinet_copy):
    sample_user.first_name, sample_user.last_name = "Umar", "Xoldarbekov"
    db.session.commit()

    soup, _ = _get(app, db, sample_user, "/my-account", "en")

    assert soup.select_one(".ae-cabinet-nav__name").name == "p"
    assert soup.select_one(".ae-cabinet-nav__name").get_text(strip=True) == "Umar"
    assert soup.select_one(".ae-cabinet-nav__avatar").get_text(strip=True) == "U"
    assert soup.select_one(".ae-cabinet-nav__sub").get_text(strip=True) == (sample_user.contact_email or sample_user.phone)
    assert "Umar!" in soup.select_one(".ae-dash__hello h2").get_text(strip=True)
    # The page outline: the banner's H1, then the content's H2s (no H4 from the sidebar).
    assert soup.select(".ae-cabinet-nav h1, .ae-cabinet-nav h2, .ae-cabinet-nav h3, .ae-cabinet-nav h4") == []
    # The <nav> names the landmark; the <aside> around it no longer repeats the name.
    assert not soup.select_one("aside.ae-cabinet-nav").has_attr("aria-label")
    assert soup.select_one("aside.ae-cabinet-nav nav")["aria-label"] == CABINET["en"]["web.cabinet.nav.label"]


def test_an_entity_is_named_by_its_company_with_the_person_under_it(app, db, sample_user, cabinet_copy):
    sample_user.user_type = UserType.ENTITY
    sample_user.company_name = "Aqua Element"
    sample_user.first_name, sample_user.last_name = "Umar", "Xoldarbekov"
    db.session.commit()

    soup, _ = _get(app, db, sample_user, "/my-orders", "ru")

    assert soup.select_one(".ae-cabinet-nav__name").get_text(strip=True) == "Aqua Element"
    assert soup.select_one(".ae-cabinet-nav__avatar").get_text(strip=True) == "A"
    assert soup.select_one(".ae-cabinet-nav__sub").get_text(strip=True) == "Umar Xoldarbekov"


# --- CAB-R18: one way to the bot on the dashboard -----------------------------------------------------------------------


def test_the_dashboard_head_has_no_bot_button_while_the_telegram_promo_offers_it(app, db, sample_user, cabinet_copy):
    soup, _ = _get(app, db, sample_user, "/my-account", "ru")
    bot = app.config["COMPANY_TELEGRAM_BOT_URL"]

    assert soup.select_one("#telegram-link-card") is not None
    assert soup.select(f'.ae-page-head__actions a[href="{bot}"]') == []
    assert len(soup.select(".ae-page-head__actions a")) == 1  # "Order water"


# --- CAB-R09 / R17: /my-subscriptions -----------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_subscriptions_page_tells_its_script_whether_aquacoins_apply(app, db, sample_user, cabinet_copy, lang):
    soup, _ = _get(app, db, sample_user, "/my-subscriptions", lang)
    island = _island(soup, "page-data")

    assert island["loyalty_eligible"] is True  # an individual: LoyaltyService.is_user_loyalty_eligible
    assert island["benefit_delivery"] == CABINET[lang]["web.cabinet.subs.benefit_delivery"]
    # The crumb names the page as the H1 does, and is the current page.
    current = soup.select_one(".bread-crumb [aria-current='page']")
    assert current.get_text(strip=True) == soup.select_one("h1").get_text(strip=True)
    assert current.get_text(strip=True) == BACKEND_TRANSLATIONS["cabinet.subs.title"][lang]


def test_an_entity_without_an_eligible_contract_gets_no_aquacoins_promise(app, db, sample_user, cabinet_copy):
    sample_user.user_type = UserType.ENTITY
    sample_user.company_name = "Aqua Element"
    db.session.commit()

    soup, _ = _get(app, db, sample_user, "/my-subscriptions", "uz")

    assert _island(soup, "page-data")["loyalty_eligible"] is False


# --- CAB-R05: the district by its name ----------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_an_orders_district_is_published_by_its_name_in_the_request_language(app, db, sample_user, lang):
    pinned = UserAddress(user_id=sample_user.id, title="Drive", full_address="Toshkent, Shayxontohur tumani",
                         district="shaykhontohur", latitude=41.32, longitude=69.23)
    typed = UserAddress(user_id=sample_user.id, title=None, full_address="Toshkent", district="Eski shahar")
    db.session.add_all([pinned, typed])
    db.session.commit()
    keyed = _order(db, sample_user, "DST_000001_26", address=pinned)
    free = _order(db, sample_user, "DST_000002_26", address=typed)
    bare = _order(db, sample_user, "DST_000003_26")
    sample_user.preferred_language = lang
    db.session.commit()
    browser = _browser(app, sample_user)

    detail = browser.get(f"/api/v1/orders/{keyed.id}", headers=WEB_HEADERS).get_json()["data"]["order"]
    listed = {
        o["order_number"]: o.get("delivery_address_district_label")
        for o in browser.get("/api/v1/orders/", headers=WEB_HEADERS).get_json()["data"]["orders"]
    }

    expected = get_district_name("shaykhontohur", lang)
    assert expected != "shaykhontohur"
    assert detail["delivery_address_district_label"] == expected
    assert listed == {keyed.order_number: expected, free.order_number: "Eski shahar", bare.order_number: None}


# --- CAB-R04 / R16: the modal's map --------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_order_modal_draws_the_vendored_leaflet_with_translated_zoom_buttons(app, db, sample_user, cabinet_copy, lang):
    soup, html = _get(app, db, sample_user, "/my-account", lang)
    island = _island(soup, "order-detail-modal-data")

    assert island["leaflet"] == {
        "js": "/static/vendor/leaflet/leaflet.js",
        "css": "/static/vendor/leaflet/leaflet.css",
        "images": "/static/vendor/leaflet/images/",
    }
    assert island["i18n"]["zoom_in"] == CABINET[lang]["web.cabinet.modal.zoom_in"]
    assert island["i18n"]["zoom_out"] == CABINET[lang]["web.cabinet.modal.zoom_out"]
    assert "unpkg.com" not in html
    scripts = [s["src"].split("?")[0] for s in soup.select("script[src]")]
    assert scripts.index("/static/js/map-helper.js") < scripts.index("/static/js/pages/order-detail-modal.js")
