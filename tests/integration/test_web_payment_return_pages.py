"""The payment return pages, the checkout's wait and the cash rescue (plan 2026-10-27 Task 10b; spec 2026-10-27
§4.5 web, §4.7, D7-D9).

- `/payment/success` and `/payment/cancel` work for a customer whose session cookie did not come along. They show
  neutral copy and a way back; they never show an order they were not asked about, and never someone else's.
- A completed payment returns to the order's own page; a payment still in flight shows the waiting page; anything
  else is the cancel page. The cancel page publishes the SAME Pay answer the API does (`customer_actions`), and the
  one script, `order-actions.js`, draws it.
- `POST /api/v1/orders/` on Click says how long to wait before Click opens (`payment_ready_in_seconds`); a
  tax-system (Asl belgisi) outage answers 503 with the customer's cash-debt state; the cash rescue applies the
  cash-debt limit like any other cash order.

Only external I/O is mocked: the Click link and `pre_utilise_marking_codes_for_payment`. The fixtures
`tests/fixtures/customer_order_actions/payment_*.json`, `checkout_*.json` and `rescue_*.json` hold what the real
endpoints answered; `admin_ui/src/__tests__/web/paymentPages.test.js` replays the shipped scripts against them.
Regenerate with `UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1`.
"""

import json
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from business_app.models.order import Order
from business_app.models.translation import Translation
from business_app.utils.exceptions import TaxCommitteeUnavailableError
from business_app.utils.local_windows import local_date
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
from tests.integration.customer_action_matrix import ROW, seed
from tests.integration.test_customer_order_actions_fixtures import (  # noqa: F401  (fixtures)
    CLICK_URL,
    _answer,
    _tidy,
    cups,
    every_day_slot,
    world,
)
from tests.integration.test_my_subscriptions_page import seeded  # noqa: F401  (autouse fixture)
from tests.unit._scope_money_helpers import delivered_cod_order, make_user

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "customer_order_actions"
UPDATE = os.environ.get("UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES") == "1"
SAME = {"Sec-Fetch-Site": "same-origin"}
CROSS = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
NEUTRAL_SUCCESS = BACKEND_TRANSLATIONS["web.payment_return.neutral_success"]["en"]
NEUTRAL_CANCEL = BACKEND_TRANSLATIONS["web.payment_return.neutral_cancel"]["en"]


@pytest.fixture(autouse=True)
def production_cookie_setting(app, monkeypatch):
    """§4.6: dev and tests use Lax; production runs Strict. Every return-page test runs with Strict, or it passes
    here and fails there."""
    monkeypatch.setitem(app.config, "JWT_COOKIE_SAMESITE", "Strict")


@pytest.fixture(autouse=True)
def return_page_words(app, db):
    """The return pages' copy, exactly as the seeder ships it, and the pages compiled afresh against it. Jinja
    writes a plain `{{ 'key'|t }}` into a template when it first compiles it, and `app` lives for the whole
    session (tests/integration/test_loyalty_guide_consecutive_render.py), so without the clear an earlier test
    that rendered these pages with other rows, or none, decides their words here. Cleared again on the way out,
    so no later test reads these rows."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if key.startswith("web.payment_return."):
            for language, value in values.items():
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


@pytest.fixture(autouse=True)
def click_link():
    with patch(
        "business_app.services.payment_service.PaymentService.create_payment_link",
        lambda self, payment_id, **_: {"payment_url": CLICK_URL},
    ):
        yield


@pytest.fixture
def unpaid_click_order(world):
    return seed(world.db, world.user, ROW["click_pending"], now=world.now.astimezone(UTC))


# --------------------------------------------------------------------------- fixtures on disk


def _save(name: str, content: dict) -> None:
    """Write the fixture (when updating), then compare it with what the file holds: a stale file would let the
    Vitest run pass against answers the backend no longer gives."""
    path = FIXTURES / f"{name}.json"
    if UPDATE:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"{path} is missing: run with UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1"
    assert json.loads(path.read_text("utf-8")) == json.loads(json.dumps(content)), (
        f"{path.name} is stale: regenerate it with UPDATE_CUSTOMER_ORDER_ACTIONS_FIXTURES=1 and re-run the suites"
    )


def _en(path: str) -> str:
    """`path` in English: a visit without a language is redirected once to add the session's (Uzbek by default)."""
    return f"{path}{'&' if '?' in path else '?'}lang=en"


def _soup(resp) -> BeautifulSoup:
    return BeautifulSoup(resp.get_data(as_text=True), "html.parser")


def _island(soup: BeautifulSoup) -> dict:
    return json.loads(soup.select_one("script#page-data").string)


def _page(world, path: str):
    resp = world.web._client.get(_en(path), headers=SAME)
    assert resp.status_code == 200, (path, resp.status_code, resp.headers.get("Location"))
    return _soup(resp)


def _page_fixture(world, soup: BeautifulSoup, selector: str, island: dict) -> dict:
    """The page's own section and island, as the Vitest run draws them. Order numbers carry the wall clock."""
    return _tidy(world, {"html": str(soup.select_one(selector)), "island": island})


def _exchange(world, order_id: int, body: dict) -> dict:
    path = f"/api/v1/orders/{order_id}/retry-payment"
    resp = world.web.post(path, body)
    return {"method": "POST", "path": path, "body": body, "status": resp.status_code, "response": _answer(world, resp)["body"]}


# --------------------------------------------------------------------------- the pages without a session


@pytest.mark.parametrize("path", ["/payment/success", "/payment/cancel"])
def test_a_signed_out_return_sees_neutral_copy_and_a_way_back(world, unpaid_click_order, path):
    app = world.app
    """A browser that lost its session cookie (Click's redirect arrives cross-site, then the hop reloads it
    same-site) is not sent to the cart or a login. It reads neutral copy, with no order number or amount."""
    resp = app.test_client().get(_en(f"{path}?order_id={unpaid_click_order.id}"), headers=SAME)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    soup = _soup(resp)
    text = soup.get_text(" ", strip=True)
    content = soup.select_one(".cancelled-content, .pending-content").get_text(" ", strip=True)
    assert (NEUTRAL_SUCCESS if path.endswith("success") else NEUTRAL_CANCEL) in text
    assert unpaid_click_order.order_number not in body and "30,000" not in body
    assert app.config["COMPANY_TELEGRAM_BOT_URL"] in body
    assert 'href="/my-orders"' in body
    assert soup.select_one("#paymentActions") is None
    kind = "pending" if path.endswith("success") else "cancel"
    island = _island(soup)
    assert island.get("order") is None and island.get("payment_id") is None
    _save(
        f"payment_{kind}_neutral",
        {
            "case": f"payment_{kind}_neutral",
            "language": "en",
            "page": _page_fixture(world, soup, f"section.payment-{'pending' if kind == 'pending' else 'cancelled'}-section", island),
        },
    )
    assert not re.search(r"\bcall\b", content, re.IGNORECASE), "customer copy never tells anyone to call"


def test_a_cross_site_arrival_without_the_cookie_first_gets_the_hop(app, unpaid_click_order):
    """A signed-in customer's cookie is dropped on the cross-site hop from Click under Strict. The page hops once
    to reload as a same-site navigation (so the cookie comes along) instead of showing the neutral copy."""
    resp = app.test_client().get(f"/payment/success?order_id={unpaid_click_order.id}", headers=CROSS)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "_hop=1" in body and NEUTRAL_SUCCESS not in body


@pytest.mark.parametrize("path", ["/payment/success", "/payment/cancel"])
def test_another_customers_order_is_never_shown(world, path):
    stranger = make_user(world.db)
    theirs = seed(world.db, stranger, ROW["click_pending"], now=world.now.astimezone(UTC))
    resp = world.web._client.get(_en(f"{path}?order_id={theirs.id}"), headers=SAME)
    assert resp.status_code == 200
    assert theirs.order_number not in resp.get_data(as_text=True)
    assert _soup(resp).select_one("#paymentActions") is None


# --------------------------------------------------------------------------- the cancel page


def test_the_cancel_page_publishes_the_same_pay_answer_as_the_api(world, unpaid_click_order):
    soup = _page(world, f"/payment/cancel?order_id={unpaid_click_order.id}")
    island = _island(soup)
    api = world.web.get(f"/api/v1/orders/{unpaid_click_order.id}").get_json()["data"]["order"]

    assert island["order"]["id"] == unpaid_click_order.id
    assert island["order"]["customer_actions"] == api["customer_actions"]
    assert island["order"]["display_status"] == api["display_status"]
    assert island["order"]["customer_actions"]["pay"]["allowed"] is True
    assert soup.select_one("#paymentActions") is not None
    assert not soup.select_one("[data-action=retry-payment]"), "the old Retry button is gone"
    pay = api["customer_actions"]["pay"]
    pay_body = {"channel": "web", "expected_amount": pay["expected_amount"]}
    _save(
        "payment_cancel",
        {
            "case": "payment_cancel",
            "language": "en",
            "order_id": unpaid_click_order.id,
            "page": _page_fixture(world, soup, "section.payment-cancelled-section", island),
            "exchanges": {"pay_web": _exchange(world, unpaid_click_order.id, pay_body)},
        },
    )


def test_a_dead_order_on_the_cancel_page_offers_no_pay_and_names_its_status(world):
    dead = seed(world.db, world.user, ROW["click_cancelled"], now=world.now.astimezone(UTC))
    soup = _page(world, f"/payment/cancel?order_id={dead.id}")
    island = _island(soup)
    text = soup.get_text(" ", strip=True)

    assert island["order"]["customer_actions"]["pay"]["allowed"] is False
    assert island["order"]["display_status"] == "cancelled"
    assert "Cancelled" in soup.select_one(".order-info").get_text(" ", strip=True)
    assert "Pending Payment" not in text
    assert "can no longer be paid online" in text
    _save(
        "payment_cancel_dead",
        {
            "case": "payment_cancel_dead",
            "language": "en",
            "order_id": dead.id,
            "page": _page_fixture(world, soup, "section.payment-cancelled-section", island),
            "exchanges": {},
        },
    )


@pytest.mark.parametrize(
    "which,expected",
    [("one_off", "/order-tracking?order_id={id}"), ("subscription", "/my-subscriptions?sub={sub}")],
)
def test_a_completed_payment_returns_to_the_orders_own_page(world, which, expected):
    row = ROW["click_pending"] if which == "one_off" else ROW["sub_active_click"]
    order = seed(world.db, world.user, row, now=world.now.astimezone(UTC))
    order.payment.status = PaymentStatus.COMPLETED
    world.db.session.commit()

    resp = world.web._client.get(_en(f"/payment/success?order_id={order.id}"), headers=SAME)

    assert resp.status_code == 302
    assert resp.headers["Location"] == expected.format(id=order.id, sub=order.subscription_id)


@pytest.mark.parametrize("status", [PaymentStatus.FAILED, PaymentStatus.CANCELLED])
def test_a_payment_that_did_not_complete_lands_on_the_cancel_page(world, status):
    order = seed(world.db, world.user, ROW["click_pending"], now=world.now.astimezone(UTC))
    order.payment.status = status
    world.db.session.commit()

    resp = world.web._client.get(_en(f"/payment/success?order_id={order.id}"), headers=SAME)

    assert (resp.status_code, resp.headers["Location"]) == (302, f"/payment/cancel?order_id={order.id}")


# --------------------------------------------------------------------------- the pending page


def test_the_pending_page_polls_with_the_cookie(world, unpaid_click_order, monkeypatch):
    soup = _page(world, f"/payment/success?order_id={unpaid_click_order.id}")
    island = _island(soup)
    payment = unpaid_click_order.payment

    assert island["payment_id"] == payment.id
    assert island["order_page_url"] == f"/order-tracking?order_id={unpaid_click_order.id}"
    assert island["cancel_url"] == f"/payment/cancel?order_id={unpaid_click_order.id}"
    assert unpaid_click_order.order_number in soup.get_text(" ", strip=True)

    # The poll is the cookie's, exactly as base.js apiRequest sends it. The provider lookup is external I/O.
    monkeypatch.setattr(
        "business_app.services.payment_service.PaymentService.update_payment_status", lambda self, payment: payment
    )
    polls = {}
    for name, status in (("pending", PaymentStatus.PENDING), ("completed", PaymentStatus.COMPLETED), ("failed", PaymentStatus.FAILED)):
        payment.status = status
        world.db.session.commit()
        resp = world.web.get(f"/api/v1/payments/{payment.id}/status")
        assert resp.status_code == 200, resp.get_json()
        # The payment's public id is random per run.
        polls[name] = json.loads(
            re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", json.dumps(_answer(world, resp)))
        )
        assert polls[name]["body"]["data"]["payment"]["status"] == status.value
    _save(
        "payment_pending",
        {
            "case": "payment_pending",
            "language": "en",
            "order_id": unpaid_click_order.id,
            "page": _page_fixture(world, soup, "section.payment-pending-section", island),
            "polls": polls,
        },
    )


# --------------------------------------------------------------------------- checkout


def _click_order_body(world) -> dict:
    """The body checkout.js `placeOrderWithClick` sends."""
    return {
        "items": [{"product_id": world.water.id, "quantity": 2}],
        "delivery_address_id": world.address.id,
        "delivery_date": local_date(world.now.astimezone(UTC)).isoformat(),
        "delivery_window_start": None,
        "delivery_window_end": None,
        "payment_method": "click",
        "delivery_notes": None,
        "source": "web",
    }


@contextmanager
def _pre_utilisation(result=None, raises=None):
    method = "business_app.services.payment_fiscalization_service.PaymentFiscalizationService.pre_utilise_marking_codes_for_payment"
    with patch(method, side_effect=raises, return_value=result) as mocked:
        yield mocked


def _place(world, body=None):
    world.user.phone_verified_at = datetime.now(UTC)
    world.db.session.commit()
    return world.web.post("/api/v1/orders/", _click_order_body(world) if body is None else body)


def test_a_click_checkout_says_how_long_to_wait(world):
    """Slow path: Click opens ~45 s after the items were registered. The wire carries the seconds, so no client
    works the wait out from `payment_ready_at`."""
    with _pre_utilisation({"utilised_at": datetime.now(UTC), "fast_path": False}):
        resp = _place(world)
    data = resp.get_json()["data"]
    assert resp.status_code == 201, resp.get_json()
    assert data["payment_url"] == CLICK_URL
    assert 44 <= data["payment_ready_in_seconds"] <= 45
    _save("checkout_click_slow", {"case": "checkout_click_slow", "exchange": _checkout_exchange(world, resp)})


def test_a_fast_path_click_checkout_has_no_wait(world):
    with _pre_utilisation({"utilised_at": datetime.now(UTC), "fast_path": True}):
        resp = _place(world)
    data = resp.get_json()["data"]
    assert resp.status_code == 201, resp.get_json()
    assert data["payment_ready_in_seconds"] == 0
    _save("checkout_click_fast", {"case": "checkout_click_fast", "exchange": _checkout_exchange(world, resp)})


def test_a_cash_checkout_publishes_no_wait(world):
    body = {**_click_order_body(world), "payment_method": "cash"}
    resp = _place(world, body)
    assert resp.status_code == 201, resp.get_json()
    assert "payment_ready_in_seconds" not in resp.get_json()["data"]
    assert "payment_ready_at" not in resp.get_json()["data"]


def _stable(world, body: dict) -> dict:
    """A recorded answer without the wall clock: the stamps, the "Pay by" line of an order created now and the
    seconds still left of the wait (it is 45 less the few milliseconds the request took)."""
    text = json.dumps(_tidy(world, {"body": body})["body"], ensure_ascii=False)
    text = re.sub(r"Pay by \d\d:\d\d [^\"]*?\.", "Pay by <deadline>.", text)
    stable = json.loads(text)
    data = stable.get("data") or {}
    if "payment_ready_in_seconds" in data:
        assert data["payment_ready_in_seconds"] in (44, 45, 0), data["payment_ready_in_seconds"]
        data["payment_ready_in_seconds"] = data["payment_ready_in_seconds"] and 45
    return stable


def _checkout_exchange(world, resp) -> dict:
    return {
        "method": "POST",
        "path": "/api/v1/orders/",
        "body": _click_order_body(world),
        "status": resp.status_code,
        "response": _stable(world, resp.get_json()),
    }


# --------------------------------------------------------------------------- the cash rescue


def _outage(world):
    with _pre_utilisation(raises=TaxCommitteeUnavailableError("down")):
        return _place(world)


def _rescue_exchange(world, order_id: int, resp) -> dict:
    return {
        "method": "POST",
        "path": f"/api/v1/orders/{order_id}/retry-with-cash",
        "body": {},
        "status": resp.status_code,
        "response": _stable(world, resp.get_json()),
    }


def test_an_asl_belgisi_outage_answers_the_cash_rescue_state(world):
    resp = _outage(world)
    body = resp.get_json()
    assert resp.status_code == 503
    assert body["data"]["error_code"] == "ASL_BELGISI_UNAVAILABLE"
    assert body["data"]["cancelled_order_id"] == Order.query.one().id
    assert body["data"]["payment_restrictions"]["cod_restricted"] is False
    assert body["message"] == BACKEND_TRANSLATIONS["api.orders.tax_committee_unavailable"]["en"]
    _save("rescue_asl_down", {"case": "rescue_asl_down", "exchange": _checkout_exchange(world, resp)})


def _cap_the_customer(world):
    delivered_cod_order(world.db, world.user)
    delivered_cod_order(world.db, world.user)


def test_a_capped_customer_is_told_cash_is_restricted_during_an_outage(world):
    _cap_the_customer(world)
    resp = _outage(world)
    assert resp.status_code == 503
    assert resp.get_json()["data"]["payment_restrictions"]["cod_restricted"] is True
    # No cash button is offered, so the sentence does not tell the customer to choose cash.
    message = resp.get_json()["message"]
    assert message == BACKEND_TRANSLATIONS["api.orders.tax_committee_unavailable_retry_only"]["en"]
    assert "cash" not in message.lower()
    _save("rescue_asl_down_capped", {"case": "rescue_asl_down_capped", "exchange": _checkout_exchange(world, resp)})


def test_the_cash_rescue_applies_the_cash_debt_limit(world):
    outage = _outage(world)
    cancelled_id = outage.get_json()["data"]["cancelled_order_id"]

    ok = world.web.post(f"/api/v1/orders/{cancelled_id}/retry-with-cash", {})
    assert ok.status_code == 201, ok.get_json()
    assert Order.query.filter_by(payment_method=PaymentMethod.CASH, status=OrderStatus.PENDING).count() == 1
    _save("rescue_ok", {"case": "rescue_ok", "exchange": _rescue_exchange(world, cancelled_id, ok)})


def test_the_cash_rescue_is_refused_over_the_limit(world):
    outage = _outage(world)
    cancelled_id = outage.get_json()["data"]["cancelled_order_id"]
    _cap_the_customer(world)

    resp = world.web.post(f"/api/v1/orders/{cancelled_id}/retry-with-cash", {})

    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["message"]
    assert Order.query.filter_by(payment_method=PaymentMethod.CASH).filter(Order.status == OrderStatus.PENDING).count() == 0
    _save("rescue_cod_limit", {"case": "rescue_cod_limit", "exchange": _rescue_exchange(world, cancelled_id, resp)})
