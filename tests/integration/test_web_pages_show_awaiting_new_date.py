"""The customer web shows the wait for a new date, and draws Cancel from the backend's answer.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.3
("Web clients") and §4.2 ("Web"), rulings F5 and F15.

A failed delivery never changes the order's status (F1). So every page that read
`order.status` kept telling the customer the order was on its way, dated with the attempt
that had failed. The server-rendered pages now label `display_status`, which their routes
take from `OrderScheduleService.customer_display_status`.

The tracking page draws Cancel from `can_customer_cancel`, taken from
`OrderService.customer_cancel_block_code`. Until now it drew Cancel for any pending or
confirmed order. It held its own copy of the rule, the same kind of copy the bot used
when order 1277 (TG_000508_26) was cancelled while it waited for a new date.

`orders.js` and `order-detail-modal.js` read the same two fields from `GET /orders` and
`GET /orders/<id>`. The storefront has no JS runner, so their half of the contract is
pinned in two places:
- here, through the PAGE_DATA they label from;
- in tests/unit/test_web_order_pages_read_published_answers.py, through their source.

The storefront's Cancel requests are sent here as base.js `apiRequest` sends them: the
session cookie, the CSRF header and, from the tracking page, the typed reason. The old
tracking-page request, with its "Bearer null" header, is kept as the control.

Every page is requested with the customer's own token, in Russian. The test DB holds no
translation rows except the ones a test seeds, so any other `|t` text renders as its
English key.
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple, Optional

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import get_csrf_token

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.translation import Translation
from business_app.utils.state_validators import DELIVERY_DRIVERLESS_STATES
from shared.constants import ORDER_DISPLAY_AWAITING_NEW_DATE as AWAITING
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration

TRACKING_URL = "/order-tracking?order_id={order_id}&lang=ru"
CONFIRMATION_URL = "/order-confirmation?order_id={order_id}&lang=ru"
ACCOUNT_URL = "/my-account?lang=ru"
ORDERS_PAGE_URL = "/my-orders?lang=ru"
ORDERS_API_URL = "/api/v1/orders/"
ORDER_API_URL = "/api/v1/orders/{order_id}"
CANCEL_API_URL = "/api/v1/orders/{order_id}/cancel"

# Storefront `|t` keys are their English source text (see the seeder's web checkout keys).
LABEL_KEY = "Awaiting a new delivery date"
# Task 7's web copy for a cancel refused while the order waits for a new date.
REFUSAL_KEY = "api.orders.cancel_refused.awaiting_new_date"
TRACKING_LINE = "Tracking: We will notify you when your order is on the way"

# Short names keep each case on one line.
OS, DS = OrderStatus, DeliveryStatus


def _seed_translation(db, key):
    """One key's rows exactly as the canonical seeder ships them. Returns {language: text}.

    Only the keys a test asks for are loaded. A page or route that looked up a key the
    seeder spells differently would get the bare key back, and the test would fail."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    texts = BACKEND_TRANSLATIONS[key]
    for language, value in texts.items():
        db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()
    return texts


@pytest.fixture
def awaiting_label(app, db):
    """The label's seeded rows, with templates compiled afresh. Yields the Russian label.

    Without its rows, `|t` would hand back the key, which is the English label. The pages
    would then pass even if the templates and the seed spelled the key differently.

    Jinja folds a plain `{{ 'key'|t }}` into a constant when it compiles a template, and the
    `app` is session-scoped. So whichever test first renders a page on a worker bakes its
    translations in. The cache is cleared on both sides of this test, as
    tests/integration/test_loyalty_guide_consecutive_render.py does."""
    texts = _seed_translation(db, LABEL_KEY)
    app.jinja_env.cache.clear()
    yield texts["ru"]
    app.jinja_env.cache.clear()


@pytest.fixture
def awaiting_refusal_copy(db):
    """The refusal's seeded web copy, {language: text}. Without its rows, `get_translation`
    hands back the bare key, which carries no link."""
    return _seed_translation(db, REFUSAL_KEY)


def _seed_order(db, customer, address, driver, name, order_status, delivery_status, *, delivery_date=None, is_paid=False):
    """One of the customer's orders, left the way production leaves it.

    A failed delivery keeps the driver who attempted the door (spec §1). A driverless one
    never carries a driver."""
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-WEB-{name}",
        status=order_status,
        payment_method=PaymentMethod.CASH,
        is_paid=is_paid,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id,
        delivery_date=delivery_date,
    )
    db.session.add(order)
    db.session.flush()
    if delivery_status is not None:
        failed = delivery_status is DeliveryStatus.FAILED
        db.session.add(
            Delivery(
                order_id=order.id,
                status=delivery_status,
                delivery_person_id=None if delivery_status in DELIVERY_DRIVERLESS_STATES else driver.id,
                scheduled_date=datetime.now(timezone.utc),
                scheduled_time_slot="anytime",
                delivery_attempts=1 if failed else 0,
                failed_delivery_reason="customer_unavailable" if failed else None,
            )
        )
    db.session.commit()
    return order


def _page(client, url, headers):
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)[:500]
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


def _published(client, headers, order_id):
    """The order as `GET /orders/<id>` publishes it: what the JS pages draw from."""
    response = client.get(ORDER_API_URL.format(order_id=order_id), headers=headers)
    assert response.status_code == 200, response.get_json()
    order = response.get_json()["data"]["order"]
    assert "error" not in order, f"the degraded fallback answered, so its fields prove nothing: {order}"
    return order


def _on_page(day):
    """A date the way the pages print it."""
    return day.strftime("%B %d, %Y")


def _stepper(soup):
    """The tracking page's steps, in order, as (state classes, description or None)."""
    steps = []
    for item in soup.select(".status-timeline .timeline-item"):
        state = " ".join(name for name in item["class"] if name != "timeline-item")
        description = item.select_one(".timeline-description")
        steps.append((state, description.get_text(" ", strip=True) if description else None))
    return steps


def _cancel_drawn(soup):
    return bool(soup.select('[data-action="cancel-order"]'))


@pytest.mark.parametrize(
    "order_status, steps",
    [
        (OS.PENDING, ["completed", "", "", "", ""]),
        (OS.CONFIRMED, ["completed", "completed", "", "", ""]),
        (OS.PREPARING, ["completed", "completed", "completed", "", ""]),
        (OS.OUT_FOR_DELIVERY, ["completed", "completed", "completed", "", ""]),
    ],
    ids=["pending", "confirmed", "preparing", "out-for-delivery"],
)
def test_the_tracking_page_shows_the_wait_not_the_attempt_that_failed(
    client, db, auth_headers, sample_user, user_address, delivery_driver, awaiting_label, order_status, steps
):
    """A delivery can fail from `assigned` while the order is still confirmed or preparing
    (shared/status_transitions.py), and F1 counts every live status, pending included. The
    steps the order reached stay ticked, its own step included: what failed was the
    delivery. Out for Delivery, whose attempt failed, is neither, even when it is the
    order's own status."""
    failed_on = date.today() - timedelta(days=3)
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, f"track-awaiting-{order_status.value}", order_status,
        DS.FAILED, delivery_date=failed_on,
    )

    soup = _page(client, TRACKING_URL.format(order_id=order.id), auth_headers)

    badge = soup.select_one(".status-badge")
    assert (badge.get_text(strip=True), badge["class"]) == (awaiting_label, ["status-badge", AWAITING])
    # No step is current. "Delivery will start soon" and the failed attempt's
    # "Estimated delivery" are gone.
    assert [state for state, _description in _stepper(soup)] == steps
    assert _stepper(soup)[3:] == [("", None), ("", None)]
    assert _on_page(failed_on) not in soup.get_text(" ")
    # Waiting for a new date is not the customer's to cancel (order 1277).
    assert not _cancel_drawn(soup)


def test_a_live_order_keeps_its_current_step_its_date_and_its_cancel(
    client, db, auth_headers, sample_user, user_address, delivery_driver
):
    """The control: the same page, for a confirmed order still in the pool. Everything the
    test above finds missing is on the page when the order is not waiting, so its absence
    there is the wait's doing."""
    next_date = date.today() + timedelta(days=3)
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, "track-live", OS.CONFIRMED, DS.SCHEDULED,
        delivery_date=next_date,
    )

    soup = _page(client, TRACKING_URL.format(order_id=order.id), auth_headers)

    badge = soup.select_one(".status-badge")
    assert (badge.get_text(strip=True), badge["class"]) == ("Confirmed", ["status-badge", "confirmed"])
    assert [state for state, _description in _stepper(soup)] == ["completed", "active", "", "", ""]
    assert _stepper(soup)[3:] == [
        ("", "Delivery will start soon"),
        ("", f"Estimated delivery: {_on_page(next_date)}"),
    ]
    assert _cancel_drawn(soup)


class CancelCase(NamedTuple):
    name: str
    order_status: OrderStatus
    delivery_status: Optional[DeliveryStatus]
    is_paid: bool
    customer_may_cancel: bool


CANCEL_CASES = [
    CancelCase("pending-unpaid", OS.PENDING, None, False, True),
    CancelCase("confirmed-in-the-pool", OS.CONFIRMED, DS.PENDING, False, True),
    # The old page drew Cancel for the next three. Pending or confirmed was all it asked.
    CancelCase("confirmed-driver-accepted", OS.CONFIRMED, DS.ASSIGNED, False, False),
    CancelCase("confirmed-awaiting-new-date", OS.CONFIRMED, DS.FAILED, False, False),
    CancelCase("pending-paid", OS.PENDING, None, True, False),
    CancelCase("out-for-delivery", OS.OUT_FOR_DELIVERY, DS.IN_TRANSIT, False, False),
]


@pytest.mark.parametrize("case", CANCEL_CASES, ids=[case.name for case in CANCEL_CASES])
def test_the_tracking_page_draws_cancel_only_when_the_backend_allows_it(
    client, db, auth_headers, sample_user, user_address, delivery_driver, case
):
    """The page's Cancel and the API's `can_customer_cancel` are one answer. The full F5
    matrix, and the endpoint enforcing it, is in tests/integration/test_customer_cancel_order.py."""
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, case.name, case.order_status, case.delivery_status,
        is_paid=case.is_paid,
    )

    published = _published(client, auth_headers, order.id)["can_customer_cancel"]
    drawn = _cancel_drawn(_page(client, TRACKING_URL.format(order_id=order.id), auth_headers))

    assert (drawn, published) == (case.customer_may_cancel, case.customer_may_cancel)


class WebCancel(NamedTuple):
    name: str
    body: Optional[dict]
    history_note: Optional[str]  # what the cancel's history row keeps


# What the storefront's Cancel buttons send once Step 9 is in. All three pages go through
# base.js `apiRequest`.
WEB_CANCELS = [
    # order-tracking.js sends the reason the customer typed into its prompt.
    WebCancel("tracking-page", {"reason": "Ordered twice by mistake"}, "Ordered twice by mistake"),
    # orders.js and order-detail-modal.js send no body.
    WebCancel("orders-page-and-modal", None, None),
]


def _cancel_from_the_web(client, token, order_id, body):
    """`POST /orders/<id>/cancel` as base.js `apiRequest` sends it from the storefront.

    The login leaves two cookies: the httpOnly `access_token_cookie`, and the readable
    `csrf_access_token`, which apiRequest copies into `X-CSRF-TOKEN`. There is no
    Authorization header, because the storefront's scripts never hold the token."""
    csrf = get_csrf_token(token)
    client.set_cookie("access_token_cookie", token)
    client.set_cookie("csrf_access_token", csrf)
    return client.post(
        CANCEL_API_URL.format(order_id=order_id),
        headers={"Content-Type": "application/json", "X-Platform": "web", "X-CSRF-TOKEN": csrf},
        data=None if body is None else json.dumps(body),
    )


@pytest.mark.parametrize("web", WEB_CANCELS, ids=[web.name for web in WEB_CANCELS])
def test_a_web_cancel_reaches_the_backend_and_ends_the_order(
    client, db, auth_token, sample_user, user_address, delivery_driver, web
):
    """The request Step 9 gives the tracking page, and the one the order list and the modal
    already send, authenticate by cookie and carry what the customer typed."""
    order = _seed_order(db, sample_user, user_address, delivery_driver, f"web-cancel-{web.name}", OS.PENDING, None)

    response = _cancel_from_the_web(client, auth_token, order.id, web.body)

    body = response.get_json()
    assert response.status_code == 200, body
    assert body["data"]["order"]["status"] == "cancelled"
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OS.CANCELLED
    history = OrderStatusHistory.query.filter_by(order_id=order.id, new_status=OS.CANCELLED).one()
    assert history.notes == web.history_note


@pytest.mark.parametrize("web", WEB_CANCELS, ids=[web.name for web in WEB_CANCELS])
def test_a_web_cancel_while_the_order_awaits_a_new_date_is_refused_with_the_bot_link(
    app, client, db, auth_token, sample_user, user_address, delivery_driver, awaiting_refusal_copy, web
):
    """Order 1277's path, taken from the web. Every storefront page shows the refusal's
    `message` as it comes, so the message itself must say where to reach us."""
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, f"web-refused-{web.name}", OS.CONFIRMED, DS.FAILED
    )

    response = _cancel_from_the_web(client, auth_token, order.id, web.body)

    body = response.get_json()
    assert response.status_code == 400, body
    assert body["data"] == {
        "error_code": "ORDER_NOT_CUSTOMER_CANCELLABLE",
        "reason_code": "AWAITING_NEW_DATE",
        "can_customer_cancel": False,
    }
    bot_url = app.config["COMPANY_TELEGRAM_BOT_URL"]
    assert body["message"] in {text.format(bot_url=bot_url) for text in awaiting_refusal_copy.values()}
    assert bot_url in body["message"]
    # Refused before anything moved.
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OS.CONFIRMED
    assert Delivery.query.filter_by(order_id=order.id).one().status is DS.FAILED


def test_the_old_tracking_page_request_never_reached_the_cancel(
    client, db, auth_token, sample_user, user_address, delivery_driver
):
    """The control, and the bug Step 9 removes. The old script built `Authorization: Bearer `
    plus localStorage's `access_token`, which the storefront never stores, so the header read
    "Bearer null". The browser still sent the session cookie, but the header is read before
    the cookie (JWT_TOKEN_LOCATION), and "null" is not a token."""
    order = _seed_order(db, sample_user, user_address, delivery_driver, "web-cancel-old", OS.PENDING, None)
    client.set_cookie("access_token_cookie", auth_token)
    client.set_cookie("csrf_access_token", get_csrf_token(auth_token))

    response = client.post(
        CANCEL_API_URL.format(order_id=order.id),
        headers={"Authorization": "Bearer null", "Content-Type": "application/json"},
        data=json.dumps({"reason": "Customer request"}),
    )

    assert (response.status_code, response.get_json()["error"]) == (401, "Invalid Token")
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OS.PENDING


def test_the_account_page_labels_the_wait_in_recent_orders(
    client, db, auth_headers, sample_user, user_address, delivery_driver, awaiting_label
):
    waiting = _seed_order(db, sample_user, user_address, delivery_driver, "acct-awaiting", OS.CONFIRMED, DS.FAILED)
    live = _seed_order(db, sample_user, user_address, delivery_driver, "acct-live", OS.CONFIRMED, DS.SCHEDULED)

    soup = _page(client, ACCOUNT_URL, auth_headers)

    rows = {}
    for row in soup.select(".recent-orders-section tbody tr"):
        badge = row.select_one(".badge")
        rows[row.select_one("td strong").get_text(strip=True)] = (badge.get_text(strip=True), badge["class"])
    # Both orders are confirmed. Only the one whose delivery failed reads as waiting.
    assert rows == {
        f"#{waiting.order_number}": (awaiting_label, ["badge", "badge-warning"]),
        f"#{live.order_number}": ("Confirmed", ["badge", "badge-primary"]),
    }


def test_the_account_page_still_renders_for_a_view_that_publishes_no_display_status(
    client, db, auth_headers, sample_user, user_address, delivery_driver, monkeypatch
):
    """The deploy window (spec §9, "Rolling safety"). Templates are bind-mounted, so the new
    `my_account.html` goes live at `git pull`, while the old `my_account()` still runs and
    passes no `display_statuses`. The page keeps rendering, each order under its own status,
    instead of a 500 for every customer with a recent order."""
    from business_app.frontend import routes

    real_render_template = routes.render_template

    def _old_view(template_name, **context):
        context.pop("display_statuses", None)
        return real_render_template(template_name, **context)

    monkeypatch.setattr(routes, "render_template", _old_view)
    live = _seed_order(db, sample_user, user_address, delivery_driver, "acct-old-view", OS.CONFIRMED, DS.SCHEDULED)

    soup = _page(client, ACCOUNT_URL, auth_headers)

    [row] = soup.select(".recent-orders-section tbody tr")
    badge = row.select_one(".badge")
    assert (row.select_one("td strong").get_text(strip=True), badge.get_text(strip=True), badge["class"]) == (
        f"#{live.order_number}",
        "Confirmed",
        ["badge", "badge-primary"],
    )


@pytest.mark.parametrize(
    "order_status, delivery_status, days_from_today, awaiting",
    [
        (OS.OUT_FOR_DELIVERY, DS.FAILED, -3, True),
        (OS.CONFIRMED, DS.SCHEDULED, 3, False),
    ],
    ids=["awaiting-a-new-date", "live"],
)
def test_the_confirmation_page_names_the_wait_and_drops_the_old_date(
    client,
    db,
    auth_headers,
    sample_user,
    user_address,
    delivery_driver,
    awaiting_label,
    order_status,
    delivery_status,
    days_from_today,
    awaiting,
):
    delivery_date = date.today() + timedelta(days=days_from_today)
    order = _seed_order(
        db, sample_user, user_address, delivery_driver, f"confirm-{order_status.value}", order_status,
        delivery_status, delivery_date=delivery_date,
    )

    soup = _page(client, CONFIRMATION_URL.format(order_id=order.id), auth_headers)

    lines = [p.get_text(" ", strip=True) for p in soup.select(".delivery-info-box p")]
    if awaiting:
        assert lines == [f"Status: {awaiting_label}", TRACKING_LINE]
        assert _on_page(delivery_date) not in soup.get_text(" ")
    else:
        # The status itself. Before, the page printed the enum's repr ("Orderstatus.confirmed").
        assert lines == [f"Estimated Delivery: {_on_page(delivery_date)}", "Status: Confirmed", TRACKING_LINE]


def test_the_js_pages_carry_a_label_for_the_display_status_the_api_publishes(
    client, db, auth_headers, sample_user, user_address, delivery_driver, awaiting_label
):
    """`orders.js` and `order-detail-modal.js` label `display_status` from
    `PAGE_DATA.i18n.status_labels` and fall back to the status itself. So the value
    `GET /orders` publishes while an order waits must be a key there, on every page that
    loads one of those scripts:
    - the order list, which loads both;
    - the account page, which loads the modal's."""
    waiting = _seed_order(db, sample_user, user_address, delivery_driver, "js-awaiting", OS.CONFIRMED, DS.FAILED)
    listed = client.get(ORDERS_API_URL, headers=auth_headers).get_json()["data"]["orders"]
    (row,) = [row for row in listed if row["id"] == waiting.id]
    assert (row["display_status"], row["can_customer_cancel"]) == (AWAITING, False)

    orders_page = _page(client, ORDERS_PAGE_URL, auth_headers)
    account_page = _page(client, ACCOUNT_URL, auth_headers)

    for page, element_id in (
        (orders_page, "page-data"),
        (orders_page, "order-detail-modal-data"),
        (account_page, "order-detail-modal-data"),
    ):
        page_data = json.loads(page.find("script", id=element_id).string)
        assert page_data["i18n"]["status_labels"].get(row["display_status"]) == awaiting_label, element_id
