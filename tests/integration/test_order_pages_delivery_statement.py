"""The website's order pages say when (plan 2026-11-12, Task 20).

An order for today (or one with no date) prints the delivery statement and no date. An order dated after today
prints "Scheduled for <date>" and no statement. An order that waits for a new date, is delivered or is cancelled
prints neither. Checkout prints the statement before the date field. The tracking page's "Delivery will start soon"
line shows only beside the statement.

Every page is fetched with a fresh client, the customer's own token and the language in the URL.
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from bs4 import BeautifulSoup

from business_app.models.order import Order
from business_app.services import order_schedule_service
from business_app.utils import display_labels
from business_app.utils.local_windows import local_date
from shared.constants import DISPLAY_TIMEZONE, get_delivery_statement
from shared.enums import DeliveryStatus as DS
from shared.enums import OrderStatus as OS
from tests.integration.public_page_builders import flat, seed_copy, shipped
from tests.integration.sales_pay_builders import freeze_local_now
from tests.integration.test_web_pages_show_awaiting_new_date import _seed_order, _stepper
from tests.integration.tier_discount_factory import post_order, verify_phone

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
PAGES = {
    "confirmation": "/order-confirmation?order_id={}&lang={}",
    "tracking": "/order-tracking?order_id={}&lang={}",
}
# The tracking page's Out for Delivery line. Its `|t` key is its English text and no seed ships a row for it, so it
# renders as the key in every language.
START_SOON = "Delivery will start soon"


@pytest.fixture(autouse=True)
def order_copy(app, db):
    seed_copy(db, ("landing.order", "web.display_labels"))
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _get(app, url, headers) -> BeautifulSoup:
    # A ?lang=uz URL answers 301 to the clean URL, so the redirect is followed.
    response = app.test_client().get(url, headers=headers, follow_redirects=True)
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _statement(lang) -> str:
    return flat(" ".join(get_delivery_statement(lang)))


def _statement_on(soup):
    found = soup.select("[data-delivery-statement]")
    assert len(found) <= 1
    return flat(found[0].get_text(" ")) if found else None


def _scheduled_line(day, lang) -> str:
    return f"{shipped('landing.order.scheduled_for', lang)} {display_labels.date_long(day, lang)}"


def _later() -> date:
    return local_date() + timedelta(days=2)


def _text(soup) -> str:
    return flat(soup.get_text(" "))


def _checkout_order(app, db, user, product, address, headers, day) -> Order:
    user.phone_verified_at = datetime.now(ZoneInfo("UTC"))
    db.session.commit()
    body = {
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address_id": address.id,
        "delivery_date": day.isoformat(),
        "delivery_window_start": None,
        "delivery_window_end": None,
        "payment_method": "cash",
        "delivery_notes": None,
        "source": "web",
    }
    response = app.test_client().post("/api/v1/orders/", json=body, headers=headers)
    assert response.status_code in (200, 201), response.get_json()
    order_id = response.get_json()["data"]["order"]["id"]
    return db.session.get(Order, order_id)


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("today", [True, False], ids=["dated-today", "no-date"])
def test_an_order_for_today_shows_the_statement_and_no_date(
    app, db, auth_headers, sample_user, user_address, delivery_driver, name, lang, today
):
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, f"stmt-{name}-{lang}-{today}", OS.CONFIRMED, DS.SCHEDULED,
        delivery_date=local_date() if today else None,
    )

    soup = _get(app, PAGES[name].format(order.id, lang), auth_headers)

    text = _text(soup)
    assert _statement_on(soup) == _statement(lang)
    assert "Estimated" not in text and shipped("landing.order.scheduled_for", lang) not in text
    if name == "tracking":  # the Out for Delivery step: the start-soon line stands beside the statement
        assert _stepper(soup)[3] == ("", START_SOON)


def test_a_today_order_placed_before_the_first_shift_still_shows_the_statement(
    app, db, monkeypatch, auth_headers, sample_user, user_address, delivery_driver
):
    early = datetime(2026, 11, 16, 4, 0, tzinfo=ZoneInfo(DISPLAY_TIMEZONE))  # no shift has opened
    freeze_local_now(monkeypatch, early)
    monkeypatch.setattr(order_schedule_service, "get_utc_now", lambda: early)  # the clock is_awaiting_release reads
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, "stmt-early", OS.CONFIRMED, None, delivery_date=early.date()
    )

    soup = _get(app, PAGES["confirmation"].format(order.id, "en"), auth_headers)

    assert _statement_on(soup) == _statement("en")
    assert shipped("landing.order.scheduled_for", "en") not in _text(soup)


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize(
    "order_status, delivery_status",
    [(OS.CONFIRMED, None), (OS.PENDING, None), (OS.CONFIRMED, DS.RESCHEDULED), (OS.CONFIRMED, DS.SCHEDULED)],
)
def test_an_order_dated_after_today_shows_scheduled_for_and_no_statement(
    app, db, auth_headers, sample_user, user_address, delivery_driver, name, lang, order_status, delivery_status
):
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, f"later-{name}-{lang}-{order_status.value}-{delivery_status}",
        order_status, delivery_status, delivery_date=_later(),
    )

    soup = _get(app, PAGES[name].format(order.id, lang), auth_headers)

    assert _scheduled_line(_later(), lang) in _text(soup)
    assert _statement_on(soup) is None


@pytest.mark.parametrize("name", PAGES)
@pytest.mark.parametrize(
    "order_status, delivery_status",
    [(OS.OUT_FOR_DELIVERY, DS.FAILED), (OS.DELIVERED, DS.DELIVERED), (OS.CANCELLED, None)],
)
def test_a_waiting_delivered_or_cancelled_order_shows_neither(
    app, db, auth_headers, sample_user, user_address, delivery_driver, name, order_status, delivery_status
):
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, f"neither-{name}-{order_status.value}", order_status,
        delivery_status, delivery_date=_later(),
    )

    soup = _get(app, PAGES[name].format(order.id, "en"), auth_headers)

    assert _statement_on(soup) is None
    assert shipped("landing.order.scheduled_for", "en") not in _text(soup)
    if name == "tracking":  # no statement, so no "Delivery will start soon" either (a cancelled order printed it)
        assert START_SOON not in [description for _state, description in _stepper(soup)]


@pytest.mark.parametrize("lang", LANGS)
def test_checkout_shows_the_statement_before_the_date_field(app, db, auth_headers, sample_user, lang):
    soup = _get(app, f"/checkout?lang={lang}", auth_headers)

    statement = soup.select_one("#delivery-time-section [data-delivery-statement]")
    assert statement is not None
    assert flat(statement.get_text(" ")) == _statement(lang) == _statement_on(soup)
    assert statement.find_next(id="delivery-date") is not None


def test_an_order_placed_through_checkout_shows_the_statement(
    app, db, auth_headers, sample_user, sample_product, user_address
):
    order = _checkout_order(app, db, sample_user, sample_product, user_address, auth_headers, local_date())

    soup = _get(app, PAGES["confirmation"].format(order.id, "en"), auth_headers)

    assert _statement_on(soup) == _statement("en")


@pytest.mark.parametrize(
    "payment_method, source, day, shown",
    [
        ("cash", "telegram", None, True),
        ("click", "telegram", None, False),
        ("cash", "web", "later", False),
        ("cash", "web", "today", True),
    ],
    ids=["bot-cash", "bot-click", "web-cash-dated-later", "web-cash-dated-today"],
)
def test_the_created_order_says_whether_its_order_placed_screen_shows_the_statement(
    app, db, auth_headers, sample_user, sample_product, user_address, payment_method, source, day, shown
):
    """`POST /api/v1/orders/` publishes `show_delivery_statement`, and the bot's order-placed screen appends the
    statement only when it is True. A Click customer is sent to pay and reads it in the payment-confirmed message
    instead; an order dated after today shows its date. Only the Tax Committee call is faked (external I/O)."""
    verify_phone(db, sample_user)
    extra = {"source": source}
    if day is not None:
        extra["delivery_date"] = (local_date() if day == "today" else _later()).isoformat()

    with patch(
        "business_app.services.payment_fiscalization_service.PaymentFiscalizationService"
        ".pre_utilise_marking_codes_for_payment",
        return_value={"utilised_at": None, "fast_path": True},
    ):
        response = post_order(
            app, auth_headers, product_id=sample_product.id, address_id=user_address.id,
            payment_method=payment_method, **extra,
        )

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["order"]["show_delivery_statement"] is shown


def test_an_order_placed_through_checkout_for_a_later_day_shows_its_date(
    app, db, auth_headers, sample_user, sample_product, user_address
):
    order = _checkout_order(app, db, sample_user, sample_product, user_address, auth_headers, _later())

    soup = _get(app, PAGES["confirmation"].format(order.id, "en"), auth_headers)

    assert _scheduled_line(_later(), "en") in _text(soup)
    assert _statement_on(soup) is None
