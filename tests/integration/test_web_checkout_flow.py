"""Checkout and the payment return pages after the redesign (website redesign step 3, group "checkout", unit
"checkout-flow", 2026-11-14).

Every page is fetched the way a signed-in customer's browser fetches it (the storefront's httpOnly cookie), in uz, ru
and en, with the rows the seeds ship (scripts/seed_web_ui_checkout_translations.py and the backend seed). The API
calls are the ones static/js/pages/checkout.js and the address wizard send, with the bodies they build.

Owner decision 4 (2026-11-13): checkout's "Add new address" opens the real address flow instead of a "coming soon"
dead end. The saved address must be one the checkout can then quote and order to, so the path is driven end to end:
the wizard's POST, the checkout page listing it, the quote for it. Found on the way and covered here: the ways to pay
were named in English in every language (the page now names them with the website's payment-method labels), the
minimum-order notice now states the server's own floor and shortfall (the refused quote's `details`), product names
come in the visitor's language, the main address is the one pre-selected, and the date field opens on Tashkent's day.
"""

import json
import os
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from business_app.utils import display_labels
from business_app.utils.local_windows import local_date
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from scripts.seed_web_ui_checkout_translations import CATEGORY, TRANSLATIONS as CHECKOUT
from scripts.seed_web_ui_shop_translations import TRANSLATIONS as SHOP
from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
from shared.constants import DISPLAY_TIMEZONE
from tests.integration.customer_action_matrix import ROW, seed
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_checkout_addresses import NEW, WEB_HEADERS, _address, _browser

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
METHODS = ("business_account", "cash", "click")
SHELL = Path(__file__).resolve().parents[1] / "fixtures" / "web_checkout" / "shell.html"


@pytest.fixture
def flow_copy(app, db):
    """The rows these pages print: the backend seed's storefront, label, status-free and quote rows, plus the
    checkout, shop (cart crumb, "Your order") and cabinet (order status words) groups' rows."""
    seed_copy(
        db,
        (
            "landing.",
            "web.display_labels",
            "web.payment_return.",
            "web.checkout.window_",
            "api.subscriptions.time.",
            "Discount",
            "Reward discount",
            "{tier} website discount",
            "Pay cash on delivery and save",
        ),
    )
    present = set(db.session.query(Translation.key, Translation.language).all())
    for group in (CHECKOUT, SHOP, CABINET):
        for language, rows in group.items():
            for key, value in rows.items():
                if (key, language) not in present:
                    db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
                    present.add((key, language))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


SAME_SITE = {"Sec-Fetch-Site": "same-origin"}  # a click inside the site (a cross-site arrival first gets the hop)


def _get(browser, path, lang):
    response = browser.get(f"{path}{'&' if '?' in path else '?'}lang={lang}", headers=SAME_SITE, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html[:500]
    return BeautifulSoup(html, "html.parser"), html


def _island(soup):
    return json.loads(soup.select_one("script#page-data").string)


def _call(browser, csrf, method, path, body=None):
    """What base.js apiRequest sends: the cookie, the CSRF header, JSON."""
    kwargs = {"headers": {**WEB_HEADERS, "X-CSRF-TOKEN": csrf}}
    if body is not None:
        kwargs["data"] = json.dumps(body)
    return browser.open(f"/api/v1{path}", method=method, **kwargs)


def _quote_body(product, quantity, address_id, method="cash", day=None, start=None, end=None):
    """The body checkout.js `estimatePayload()` sends: the day (the page's default is today) and the window with it."""
    return {
        "items": [{"product_id": product.id, "quantity": quantity}],
        "delivery_address_id": address_id,
        "delivery_date": day or local_date().isoformat(),
        "delivery_window_start": start,
        "delivery_window_end": end,
        "payment_method": method,
    }


def _order_body(product, quantity, address_id, day, start=None, end=None, method="cash"):
    """The body checkout.js `orderBody(method)` sends."""
    return {
        "items": [{"product_id": product.id, "quantity": quantity}],
        "delivery_address_id": address_id,
        "delivery_date": day,
        "delivery_window_start": start,
        "delivery_window_end": end,
        "payment_method": method,
        "delivery_notes": None,
        "source": "web",
    }


def _freeze_tashkent(monkeypatch, hour, minute=0):
    """The business clock (delivery_window.local_now) at hour:minute of today in Tashkent: what the checkout view,
    the quote and the order all judge the schedule by."""
    import business_app.utils.delivery_window as delivery_window

    frozen = datetime.combine(local_date(), time(hour, minute), tzinfo=ZoneInfo(DISPLAY_TIMEZONE))
    monkeypatch.setattr(delivery_window, "local_now", lambda: frozen)
    return frozen


# ---- /checkout --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_checkout_page_renders_in_each_language_with_the_main_address_selected(
    app, db, sample_user, flow_copy, lang, monkeypatch
):
    _freeze_tashkent(monkeypatch, 8)  # every window still open: the shell fixture is the same at any hour
    home = _address(db, sample_user, title="Uy")
    office = _address(db, sample_user, title="Ofis", is_default=True, delivery_instructions="2-podyezd")
    browser, _csrf = _browser(app, sample_user)

    soup, html = _get(browser, "/checkout", lang)

    assert "web.checkout." not in html and "web.shop." not in html
    assert soup.h1.get_text(strip=True) == CHECKOUT[lang]["web.checkout.co.title"]
    cards = soup.select("#address-grid .address-card[data-action=select-address][data-address-id]")
    assert [card["data-address-id"] for card in cards] == [str(office.id), str(home.id)]
    assert "selected" in cards[0]["class"] and cards[0].select_one("input[type=radio]").has_attr("checked")
    assert not cards[1].select_one("input[type=radio]").has_attr("checked")
    assert cards[0].select_one(".ae-co-addr__main").get("hidden") is None
    assert cards[1].select_one(".ae-co-addr__main").has_attr("hidden")
    assert "2-podyezd" in cards[0].get_text(" ", strip=True)
    # "Add address" opens the shared wizard: its modal and its template card are on the page; the dead legacy
    # form and the "coming soon" toast are gone.
    assert soup.select_one("#address-grid [data-action=show-add-address]") is not None
    assert soup.select_one("#addressWizardModal.ae-modal") is not None
    assert soup.select_one("template#address-card-template") is not None
    assert soup.select_one("#address-modal") is None and soup.select_one("#card-input-container") is None
    assert "coming soon" not in html.lower()
    assert soup.select_one("#address-empty").has_attr("hidden")
    # The hooks checkout.js reads.
    for selector in (
        "#delivery-time-section [data-delivery-statement]",
        "#time-slots-container",
        "#payment-methods-container",
        "#selected-payment-method",
        "#order-notes",
        "#agree-terms",
        "#summary-items",
        "#summary-courtesy[hidden]",
        "#summary-total",
        "#min-order-warning #min-order-title",
        "#checkoutPaymentState[role=status]",
        "#place-order-btn[data-action=place-order]",
        "#checkout-bar [data-action=place-order]",
        "#loading-overlay",
    ):
        assert soup.select_one(selector) is not None, selector
    assert soup.select_one("#delivery-date")["value"] == local_date().isoformat()

    island = _island(soup)
    assert island["method_labels"] == {
        method: BACKEND_TRANSLATIONS[f"web.display_labels.payment_method.{method}"][lang] for method in METHODS
    }
    assert island["method_notes"] == {
        method: CHECKOUT[lang][f"web.checkout.co.method_note.{method}"] for method in METHODS
    }
    for name, value in island["i18n"].items():
        assert value and not value.startswith(("web.", "api.")), (name, value)
    assert island["i18n"]["courtesy_part"] == SHOP[lang]["web.shop.cart.courtesy_part"]
    assert island["i18n"]["courtesy_thanks"] == SHOP[lang]["web.shop.cart.courtesy_thanks"]
    assert island["i18n"]["upcoming_price"] == BACKEND_TRANSLATIONS["landing.offer.upcoming_price"][lang]
    assert "{minimum}" in island["i18n"]["min_amount_title"]
    assert "{remaining}" in island["i18n"]["min_amount_text"]

    if lang == "en":
        _check_shell(soup, {office.id: 101, home.id: 102})


def _check_shell(soup, ids):
    """The markup + data islands admin_ui/src/__tests__/web/checkoutPage.test.js runs the shipped scripts over, with
    the address ids and today's date fixed. UPDATE_WEB_FIXTURES=1 rewrites it."""
    parts = [
        str(soup.select_one(selector))
        for selector in ("div.ae-co", "#addressWizardModal", "#address-wizard-data", "#page-data")
    ]
    shell = re.sub(r"\?v=\d+", "", "\n".join(parts)) + "\n"
    shell = re.sub(r"\d{4}-\d{2}-\d{2}", "2026-01-01", shell)
    shell = re.sub(r'"max_label": "[^"]*"', '"max_label": "16 January"', shell)
    for real, fixed in ids.items():
        shell = re.sub(rf'(data-address-id|value)="{real}"', rf'\1="{fixed}"', shell)
    if os.environ.get("UPDATE_WEB_FIXTURES"):
        SHELL.parent.mkdir(parents=True, exist_ok=True)
        SHELL.write_text(shell, encoding="utf-8")
    assert SHELL.read_text(encoding="utf-8") == shell, "stale shell: rerun with UPDATE_WEB_FIXTURES=1"


def test_a_customer_with_no_address_is_offered_the_wizard(app, db, sample_user, flow_copy):
    browser, _csrf = _browser(app, sample_user)

    soup, _html = _get(browser, "/checkout", "ru")

    assert soup.select_one("#address-grid").has_attr("hidden")
    empty = soup.select_one("#address-empty")
    assert empty.get("hidden") is None
    assert CHECKOUT["ru"]["web.checkout.co.address_empty_title"] in empty.get_text(" ", strip=True)
    assert empty.select_one("[data-action=show-add-address]") is not None
    assert soup.select("#address-grid .address-card[data-address-id]") == []
    assert soup.select_one("#addressWizardModal") is not None


def test_an_address_added_from_checkout_is_listed_selectable_and_quoted(app, db, sample_user, sample_product, flow_copy):
    """The wizard's POST, then what checkout.js does with the answer: a card for the row, selected, and a quote for
    it. The checkout page then lists it as one of the customer's addresses."""
    browser, csrf = _browser(app, sample_user)
    _get(browser, "/checkout", "ru")

    created = _call(browser, csrf, "POST", "/auth/addresses", NEW)
    assert created.status_code == 201, created.get_json()
    address = created.get_json()["data"]["address"]
    # The fields addAddressCard() fills the card from.
    for field in ("id", "title", "full_address", "is_default", "delivery_instructions"):
        assert field in address, field

    quote = _call(browser, csrf, "POST", "/orders/cart/estimate", _quote_body(sample_product, 2, address["id"]))
    assert quote.status_code == 200, quote.get_json()
    data = quote.get_json()["data"]
    assert data["items"][0]["quantity"] == 2 and data["items"][0]["unit_price"] > 0
    assert data["pricing"]["final_total"] > 0

    soup, _html = _get(browser, "/checkout", "ru")
    ids = [card["data-address-id"] for card in soup.select("#address-grid .address-card[data-address-id]")]
    assert str(address["id"]) in ids


def test_a_basket_below_the_minimum_is_refused_with_the_figures_the_page_states(
    app, db, sample_user, sample_product, flow_copy
):
    """checkout.js reads the floor and the priced basket off the refusal (`details`) to say how much is missing."""
    floor = float(app.config["MIN_ORDER_AMOUNT"])
    assert float(sample_product.base_price) < floor, "the case needs one bottle below the floor"
    address = _address(db, sample_user)
    browser, csrf = _browser(app, sample_user)

    refused = _call(browser, csrf, "POST", "/orders/cart/estimate", _quote_body(sample_product, 1, address.id))

    assert refused.status_code == 400
    body = refused.get_json()
    assert body["error_code"] == "ORDER_MIN_AMOUNT"
    assert body["details"]["min_amount"] == floor
    assert body["details"]["items_subtotal"] == float(sample_product.base_price)


def test_a_line_below_its_smallest_order_is_refused_and_the_catalogue_names_it_in_the_visitors_language(
    app, db, sample_user, sample_product, flow_copy
):
    """The quote refuses a line under the product's smallest order; the page names the product and the smallest
    order from POST /products/bulk, in the session's language (the summary used to print the Uzbek default)."""
    sample_product.min_order_quantity = 3
    sample_product.set_translated("name", "Чистая вода 19 л", "ru")
    db.session.commit()
    address = _address(db, sample_user)
    browser, csrf = _browser(app, sample_user)
    _get(browser, "/checkout", "ru")  # the visit fixes the session's language, as in the browser

    refused = _call(browser, csrf, "POST", "/orders/cart/estimate", _quote_body(sample_product, 2, address.id))
    assert refused.status_code == 400
    assert refused.get_json().get("error_code") != "ORDER_MIN_AMOUNT"

    bulk = _call(browser, csrf, "POST", "/products/bulk", {"product_ids": [sample_product.id], "language": "ru"})
    assert bulk.status_code == 200, bulk.get_json()
    product = bulk.get_json()["data"]["products"][0]
    assert product["name"] == "Чистая вода 19 л"
    assert (product.get("inventory") or {}).get("min_order_quantity", product.get("min_order_quantity")) == 3


# ---- the day and the window (review checkout-r01/r02) -------------------------------------------------------------


@pytest.mark.parametrize("lang", ("ru", "uz"))
def test_the_page_offers_only_the_days_and_windows_the_schedule_rule_takes(
    app, db, sample_user, flow_copy, lang, monkeypatch
):
    """At 15:00 in Tashkent the morning (09:00–12:00) is over: the page publishes it as closed for today, and the date
    field runs from the server's today to the last day of the horizon (the browser's clock decides neither)."""
    _freeze_tashkent(monkeypatch, 15)
    _address(db, sample_user)
    browser, _csrf = _browser(app, sample_user)

    soup, _html = _get(browser, "/checkout", lang)

    today = local_date()
    last_day = today + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS)
    field = soup.select_one("#delivery-date")
    assert (field["min"], field["max"], field["value"]) == (today.isoformat(), last_day.isoformat(), today.isoformat())
    schedule = _island(soup)["schedule"]
    assert schedule["today"] == today.isoformat() and schedule["max_date"] == last_day.isoformat()
    with app.test_request_context():
        assert schedule["max_label"] == display_labels.day_month(last_day, lang)
    assert [(w["key"], w["start"], w["end"], w["open_today"]) for w in schedule["windows"]] == [
        ("anytime", None, None, True),
        ("morning", "09:00", "12:00", False),
        ("afternoon", "12:00", "18:00", True),
        ("evening", "18:00", "21:00", True),
    ]
    # The words the page says a refusal with, at the field, in the visitor's language.
    i18n = _island(soup)["i18n"]
    assert i18n["schedule_window_passed"] == CHECKOUT[lang]["web.checkout.co.schedule_window_passed"]
    assert "{date}" in i18n["schedule_date_far"]
    assert soup.select_one("#delivery-window-error[role=alert]") is not None


def _verified_browser(app, db, user):
    user.phone_verified_at = datetime.now(UTC)
    db.session.commit()
    return _browser(app, user)


@pytest.mark.parametrize("lang", ("ru", "uz"))
def test_a_window_over_today_is_refused_alike_by_the_quote_and_the_order_with_a_code(
    app, db, sample_user, sample_product, flow_copy, lang, monkeypatch
):
    """The reviewer's case: 15:00, today, the morning window. The quote used to price it and the order then answered
    an English "Validation failed"; both now refuse it with SCHEDULE_WINDOW_PASSED, which the page words at the
    window group. A later window today is quoted (preview = apply)."""
    _freeze_tashkent(monkeypatch, 15)
    address = _address(db, sample_user)
    browser, csrf = _verified_browser(app, db, sample_user)
    today = local_date().isoformat()

    quote = _call(browser, csrf, "POST", f"/orders/cart/estimate?lang={lang}",
                  _quote_body(sample_product, 10, address.id, day=today, start="09:00", end="12:00"))
    assert quote.status_code == 400
    assert quote.get_json()["error_code"] == "SCHEDULE_WINDOW_PASSED"

    order = _call(browser, csrf, "POST", f"/orders/?lang={lang}",
                  _order_body(sample_product, 10, address.id, today, "09:00", "12:00"))
    assert order.status_code == 400
    body = order.get_json()
    assert body["data"]["error_code"] == "SCHEDULE_WINDOW_PASSED"
    assert body["errors"] == ["delivery window has already passed for today"]

    later = _call(browser, csrf, "POST", f"/orders/cart/estimate?lang={lang}",
                  _quote_body(sample_product, 10, address.id, day=today, start="18:00", end="21:00"))
    assert later.status_code == 200, later.get_json()


@pytest.mark.parametrize(
    "offset,code",
    [(-1, "SCHEDULE_DATE_IN_PAST"), (MAX_SCHEDULE_HORIZON_DAYS + 1, "SCHEDULE_BEYOND_HORIZON"), (MAX_SCHEDULE_HORIZON_DAYS, None)],
)
def test_a_day_outside_the_horizon_is_refused_alike_by_the_quote_and_the_order(
    app, db, sample_user, sample_product, flow_copy, offset, code, monkeypatch
):
    """A typed past day or one past the horizon used to be priced and then refused by the order. The last day of the
    horizon is quoted."""
    _freeze_tashkent(monkeypatch, 10)
    address = _address(db, sample_user)
    browser, csrf = _verified_browser(app, db, sample_user)
    day = (local_date() + timedelta(days=offset)).isoformat()

    quote = _call(browser, csrf, "POST", "/orders/cart/estimate?lang=ru", _quote_body(sample_product, 10, address.id, day=day))

    if code is None:
        assert quote.status_code == 200, quote.get_json()
        return
    assert (quote.status_code, quote.get_json()["error_code"]) == (400, code)
    order = _call(browser, csrf, "POST", "/orders/?lang=ru", _order_body(sample_product, 10, address.id, day))
    assert (order.status_code, order.get_json()["data"]["error_code"]) == (400, code)


# ---- /payment/success and /payment/cancel -----------------------------------------------------------------------


@pytest.fixture
def unpaid_click_order(db, sample_user):
    return seed(db, sample_user, ROW["click_pending"], now=datetime.now(UTC))


@pytest.mark.parametrize("lang", LANGS)
def test_the_pending_page_reads_in_each_language(app, db, sample_user, flow_copy, unpaid_click_order, lang):
    browser, _csrf = _browser(app, sample_user)

    soup, html = _get(browser, f"/payment/success?order_id={unpaid_click_order.id}", lang)

    assert "web.checkout." not in html
    assert soup.h1.get_text(strip=True) == CHECKOUT[lang]["web.checkout.pay.pending_title"]
    values = [dd.get_text(strip=True) for dd in soup.select("section.payment-pending-section .order-info .info-value")]
    with app.test_request_context():
        money = display_labels.money(unpaid_click_order.total_amount, lang)
        method = display_labels.payment_method(unpaid_click_order.payment_method, lang)
    assert values == [unpaid_click_order.order_number, money, method]
    assert "UZS" not in money or lang == "en"
    assert soup.select_one("[data-action=check-payment-status]") is not None
    assert soup.select_one("#pending-timeout").has_attr("hidden")
    assert _island(soup)["payment_id"] is not None


@pytest.mark.parametrize("lang", LANGS)
def test_the_cancel_page_reads_in_each_language_and_offers_pay(app, db, sample_user, flow_copy, unpaid_click_order, lang):
    browser, _csrf = _browser(app, sample_user)

    soup, html = _get(browser, f"/payment/cancel?order_id={unpaid_click_order.id}", lang)

    assert "web.checkout." not in html
    assert soup.h1.get_text(strip=True) == CHECKOUT[lang]["web.checkout.pay.cancel_title"]
    island = _island(soup)
    status = island["order"]["display_status"]
    pill = soup.select_one(".order-info .ae-status")
    assert pill.get_text(strip=True) == CABINET[lang][f"web.cabinet.status.{status}"]
    assert island["order"]["customer_actions"]["pay"]["allowed"] is True
    notice = soup.select_one(".ae-pay__notice").get_text(" ", strip=True)
    assert notice == CHECKOUT[lang]["web.checkout.pay.cancel_retry_note"]
    assert soup.select_one("#paymentActions[data-order-id]")["data-order-id"] == str(unpaid_click_order.id)


@pytest.mark.parametrize("lang", LANGS)
def test_the_signed_out_cancel_page_names_no_order(app, db, flow_copy, unpaid_click_order, lang):
    soup, html = _get(app.test_client(), f"/payment/cancel?order_id={unpaid_click_order.id}", lang)

    assert "web.checkout." not in html
    assert soup.h1.get_text(strip=True) == CHECKOUT[lang]["web.checkout.pay.cancel_title"]
    assert BACKEND_TRANSLATIONS["web.payment_return.neutral_cancel"][lang] in soup.get_text(" ", strip=True)
    assert unpaid_click_order.order_number not in html
    assert soup.select_one("#paymentActions") is None


# ---- every word these pages print has a seed row ----------------------------------------------------------------

_KEY_RE = re.compile(r"""(['"])((?:\\.|(?!\1).)+?)\1\s*\|\s*(?:t|translate)\b""")
PAGES = (
    "checkout.html",
    "payment_pending.html",
    "payment_cancelled.html",
    # unit "order-pages"
    "order_confirmation.html",
    "order_tracking.html",
    "order_not_found.html",
    "partials/order_parts.html",
)


def _seed_row(key):
    """The group seed that writes `key`, else the backend seed (which still owns the older `web.checkout.window_*`)."""
    for group in (CHECKOUT, SHOP, CABINET):
        if any(key in rows for rows in group.values()):
            return {language: rows.get(key) for language, rows in group.items()}
    return BACKEND_TRANSLATIONS.get(key)


@pytest.mark.parametrize("page", PAGES)
def test_every_literal_key_these_pages_render_is_seeded_in_three_languages(page):
    """The guard the audit asked for (F1): a key with no row prints itself, in English, to ru and uz visitors."""
    text = (Path(__file__).resolve().parents[2] / "business_app" / "templates" / "frontend" / page).read_text("utf-8")
    keys = {match.group(2) for match in _KEY_RE.finditer(text)}
    assert keys, page
    for key in sorted(keys):
        row = _seed_row(key)
        assert row is not None, f"{page}: {key!r} has no seed row"
        for language in LANGS:
            assert row.get(language), f"{page}: {key!r} has no {language} value"
