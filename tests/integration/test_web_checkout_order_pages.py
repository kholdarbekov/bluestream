"""The order confirmation and tracking pages after the redesign (website redesign step 3, group "checkout", unit
"order-pages", 2026-11-14).

Owner decision 4 (2026-11-13): on the order confirmation and tracking pages the delivery address shows, and the
timeline's times are Tashkent's, in the visitor's language. Before, both pages read address columns the Delivery
model does not have (so the section was always blank), and the timeline printed UTC instants in English 12-hour
format, the "confirmed" step dated by the order's last edit. Found on the way and covered here: product names in the
visitor's language, money in the website's label, a cancelled order's steps ending in its cancellation instead of
"Waiting for confirmation", the courier's name on the way, and a missing order answering a designed 404 instead of a
redirect whose English flash no page showed.

Every page is fetched the way a signed-in customer's browser fetches it (the storefront's cookie), in uz, ru and en,
with the rows the seeds ship. The cancel is the request order-actions.js sends from the tracking page.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import OrderItem, OrderStatusHistory
from business_app.utils import display_labels
from business_app.utils.local_windows import local_date
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from scripts.seed_web_ui_checkout_translations import TRANSLATIONS as CHECKOUT
from shared.enums import DeliveryStatus as DS
from shared.enums import OrderStatus as OS
from tests.integration.test_web_checkout_addresses import WEB_HEADERS, _address, _browser
from tests.integration.test_web_checkout_flow import _get, _island, flow_copy  # noqa: F401  (fixture)
from tests.integration.test_web_pages_show_awaiting_new_date import _seed_order, _stepper

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
# 17:40 UTC is 22:40 in Tashkent (UTC+5): the time the old page printed as "05:40 PM".
PLACED_UTC = (7, 5, 17, 40)


def _word(lang, key):
    return CHECKOUT[lang][key]


def _order(db, user, product, tag, status, delivery_status=None, driver=None, *, year=None, history=()):
    """A customer order placed on 5 July at 17:40 UTC (22:40 in Tashkent) with one line of `product`, its address
    pinned, and a status-history row for each (status, minutes after placing) in `history`."""
    address = _address(db, user, title="Uy", delivery_instructions="2-podyezd")
    order = _seed_order(db, user, address, driver, tag, status, delivery_status)
    month, day, hour, minute = PLACED_UTC
    order.created_at = datetime(year or local_date().year, month, day, hour, minute, tzinfo=UTC)
    order.subtotal = order.total_amount = Decimal("38000.00")
    db.session.add(
        OrderItem(order_id=order.id, product_id=product.id, quantity=2, unit_price=Decimal("19000.00"),
                  total_price=Decimal("38000.00"))
    )
    previous = OS.PENDING
    for new_status, minutes in history:
        db.session.add(
            OrderStatusHistory(order_id=order.id, old_status=previous, new_status=new_status,
                               changed_at=order.created_at + timedelta(minutes=minutes))
        )
        previous = new_status
    db.session.commit()
    return order


def _money(app, amount, lang):
    with app.test_request_context():
        return display_labels.money(amount, lang)


def _times(soup):
    return [item.select_one(".timeline-time").get_text(strip=True) if item.select_one(".timeline-time") else None
            for item in soup.select(".status-timeline .timeline-item")]


# ---- /order-confirmation ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_confirmation_names_the_order_its_address_its_lines_and_its_money_in_each_language(
    app, db, sample_user, sample_product, flow_copy, lang
):
    order = _order(db, sample_user, sample_product, f"done-{lang}", OS.CONFIRMED, DS.SCHEDULED)
    browser, _csrf = _browser(app, sample_user)

    soup, html = _get(browser, f"/order-confirmation?order_id={order.id}", lang)

    assert "web.checkout." not in html and "web.cabinet." not in html
    assert soup.h1.get_text(strip=True) == _word(lang, "web.checkout.order.done_title")
    card = soup.select_one(".confirmation-content")
    assert card.select_one("#done-number").get_text(strip=True) == _word(lang, "web.checkout.order.number").format(
        number=order.order_number
    )
    assert card.select_one(".ae-status").get_text(strip=True) == CABINET[lang]["web.cabinet.status.confirmed"]
    # The address the order goes to (it used to be a blank gap): its name, its text and the courier note.
    address = card.select_one(".ae-order-addr").get_text(" ", strip=True)
    assert "Uy" in address and order.delivery_address.full_address in address and "2-podyezd" in address
    # When it was placed: Tashkent's clock, the visitor's month name.
    with app.test_request_context():
        day = display_labels.day_month(local_date(order.created_at), lang)
    assert f"{day}, 22:40" in card.select_one(".ae-done__facts").get_text(" ", strip=True)
    # The line in the visitor's language, and the money in the website's label.
    line = card.select_one(".ae-co-line")
    with app.test_request_context(f"/?lang={lang}"):
        name = sample_product.get_translated("name", lang) or sample_product.name
    assert line.select_one(".ae-co-line__name").get_text(strip=True) == name
    assert line.select_one(".ae-co-line__total").get_text(strip=True) == _money(app, 38000, lang)
    totals = card.select_one(".totals-section .ae-dl__total dd").get_text(strip=True)
    assert totals == _money(app, order.total_amount, lang)
    # The way on: the order's own page, and My orders. No "Payment status" row and no toast on a revisit.
    assert card.select_one(f'a[href="/order-tracking?order_id={order.id}"]') is not None
    assert card.select_one('a[href="/my-orders"]') is not None
    assert "order_success" not in html and "Payment Status" not in html


@pytest.mark.parametrize("status", [OS.CANCELLED, OS.RETURNED])
def test_a_cancelled_or_returned_order_is_not_thanked_but_sent_to_its_own_page(
    app, db, sample_user, sample_product, flow_copy, status
):
    """The confirmation's URL stays in the browser's history after a later cancel (review checkout-r07): it no longer
    shows a tick and "Thank you" beside the "Cancelled" pill, it goes to the order's own page, which ends in it."""
    order = _order(db, sample_user, sample_product, f"done-{status.value}", status)
    browser, _csrf = _browser(app, sample_user)

    response = browser.get(f"/order-confirmation?order_id={order.id}&lang=ru", headers={"Sec-Fetch-Site": "same-origin"})

    assert (response.status_code, response.headers["Location"]) == (302, f"/order-tracking?order_id={order.id}")
    soup, html = _get(browser, response.headers["Location"], "ru")
    assert _word("ru", "web.checkout.order.thanks") not in html
    assert soup.select_one(".ae-track__status .ae-status").get_text(strip=True) == (
        CABINET["ru"][f"web.cabinet.status.{status.value}"]
    )


# ---- /order-tracking --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_tracking_steps_carry_tashkent_times_from_the_status_history_in_each_language(
    app, db, sample_user, sample_product, delivery_driver, flow_copy, lang
):
    """Confirmed one minute after placing: the step shows 22:41, not the order's last edit, and not UTC."""
    order = _order(db, sample_user, sample_product, f"track-{lang}", OS.CONFIRMED, DS.ASSIGNED, delivery_driver,
                   history=[(OS.CONFIRMED, 1)])
    browser, _csrf = _browser(app, sample_user)

    soup, html = _get(browser, f"/order-tracking?order_id={order.id}", lang)

    assert "web.checkout." not in html and "web.cabinet." not in html
    with app.test_request_context():
        day = display_labels.day_month(local_date(order.created_at), lang)
    assert soup.h1.get_text(strip=True) == _word(lang, "web.checkout.order.number").format(number=order.order_number)
    assert soup.select_one(".ae-hero__lead").get_text(strip=True) == _word(lang, "web.checkout.track.placed_lead").format(
        when=f"{day}, 22:40"
    )
    assert [state for state, _description in _stepper(soup)] == ["completed", "active", "", "", ""]
    assert _times(soup) == [f"{day}, 22:40", f"{day}, 22:41", None, None, None]
    pill = soup.select_one(".ae-track__status .ae-status")
    assert pill.get_text(strip=True) == CABINET[lang]["web.cabinet.status.confirmed"]
    # The address shows, and the map is handed its pin.
    address = soup.select_one(".ae-track__address").get_text(" ", strip=True)
    assert "Uy" in address and order.delivery_address.full_address in address
    island = _island(soup)
    assert (island["map"]["lat"], island["map"]["lng"]) == (order.delivery_address.latitude, order.delivery_address.longitude)
    assert island["auto_refresh"] is True
    assert soup.select_one("div.action-buttons #orderActions[data-order-id]")["data-order-id"] == str(order.id)


def test_an_order_from_an_earlier_year_names_the_year(app, db, sample_user, sample_product, delivery_driver, flow_copy):
    year = local_date().year - 1
    order = _order(db, sample_user, sample_product, "track-last-year", OS.DELIVERED, DS.DELIVERED, delivery_driver,
                   year=year,
                   history=[(OS.CONFIRMED, 1), (OS.OUT_FOR_DELIVERY, 30), (OS.DELIVERED, 75)])
    browser, _csrf = _browser(app, sample_user)

    soup, _html = _get(browser, f"/order-tracking?order_id={order.id}", "ru")

    assert _times(soup) == [
        f"5 июля {year} г., 22:40", f"5 июля {year} г., 22:41", None, f"5 июля {year} г., 23:10",
        f"5 июля {year} г., 23:55",
    ]
    assert [state for state, _description in _stepper(soup)][-1] == "active completed"
    assert _island(soup)["auto_refresh"] is False


def test_a_courier_on_the_way_is_named(app, db, sample_user, sample_product, delivery_driver, flow_copy):
    order = _order(db, sample_user, sample_product, "track-on-the-way", OS.OUT_FOR_DELIVERY, DS.IN_TRANSIT,
                   delivery_driver, history=[(OS.CONFIRMED, 1), (OS.OUT_FOR_DELIVERY, 20)])
    browser, _csrf = _browser(app, sample_user)

    soup, _html = _get(browser, f"/order-tracking?order_id={order.id}", "ru")

    step = soup.select(".status-timeline .timeline-item")[3]
    assert "active" in step["class"]
    assert step.select_one(".timeline-description").get_text(strip=True) == _word("ru", "web.checkout.track.on_the_way_text")
    assert step.select_one(".timeline-note").get_text(strip=True) == _word("ru", "web.checkout.track.courier").format(
        name=delivery_driver.full_name
    )


def test_a_cancel_from_the_tracking_page_ends_its_steps_with_the_cancellation(
    app, db, sample_user, sample_product, flow_copy
):
    """The tracking page's Cancel end to end: the island publishes it, order-actions.js posts what the screen showed
    (`expected_via`) through apiRequest (cookie + CSRF), and the reloaded page ends with the cancellation and its time
    instead of four grey steps saying "Waiting for confirmation"."""
    order = _order(db, sample_user, sample_product, "track-cancel", OS.PENDING)
    browser, csrf = _browser(app, sample_user)
    soup, _html = _get(browser, f"/order-tracking?order_id={order.id}", "ru")
    cancel = _island(soup)["order"]["customer_actions"]["cancel"]
    assert cancel["allowed"] is True

    response = browser.open(
        f"/api/v1/orders/{order.id}/cancel", method="POST", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf},
        data=json.dumps({"expected_via": cancel["via"]}),
    )
    assert response.status_code == 200, response.get_json()
    # The cancel's history row, pinned five minutes after placing (17:45 UTC on 5 July), so the label is a literal.
    history = OrderStatusHistory.query.filter_by(order_id=order.id, new_status=OS.CANCELLED).one()
    history.changed_at = order.created_at + timedelta(minutes=5)
    db.session.commit()

    soup, html = _get(browser, f"/order-tracking?order_id={order.id}", "ru")
    steps = _stepper(soup)
    assert [state for state, _description in steps] == ["completed", "cancelled"]
    final = soup.select(".status-timeline .timeline-item")[-1]
    assert final.select_one(".timeline-title").get_text(strip=True) == _word("ru", "web.checkout.track.cancelled")
    assert final.select_one(".timeline-time").get_text(strip=True) == "5 июля, 22:45"
    assert _word("ru", "web.checkout.track.confirm_wait") not in html
    assert soup.select_one(".ae-track__live") is None and _island(soup)["auto_refresh"] is False
    assert soup.select_one(".ae-track__status .ae-status").get_text(strip=True) == CABINET["ru"]["web.cabinet.status.cancelled"]


# ---- an order that is not there --------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/order-confirmation", "/order-tracking"])
@pytest.mark.parametrize("which", ["none", "missing", "someone-else"])
def test_an_order_that_is_not_the_customers_answers_a_designed_404_that_names_no_order(
    app, db, sample_user, sample_product, delivery_driver, flow_copy, path, which
):
    other = _order(db, delivery_driver, sample_product, f"other-{path[1:]}-{which}", OS.CONFIRMED)
    query = {"none": "", "missing": "?order_id=999999", "someone-else": f"?order_id={other.id}"}[which]
    browser, _csrf = _browser(app, sample_user)

    response = browser.get(f"{path}{query}{'&' if query else '?'}lang=ru", headers={"Sec-Fetch-Site": "same-origin"})

    html = response.get_data(as_text=True)
    assert response.status_code == 404, html[:300]
    assert "web.checkout." not in html and other.order_number not in html
    assert _word("ru", "web.checkout.order.not_found_heading") in html
    assert 'href="/my-orders"' in html


def test_the_step_words_the_template_builds_from_the_status_are_seeded():
    """order_tracking.html names a closed order's last step `'web.checkout.track.' ~ display_status`, which the
    literal-key guard (test_web_checkout_flow) cannot see."""
    for status in ("cancelled", "returned"):
        for lang in LANGS:
            assert CHECKOUT[lang][f"web.checkout.track.{status}"], (status, lang)
