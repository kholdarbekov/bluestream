"""Create Order's quote panel against the real admin routes (plan 2026-11-16, Task 56).

The page's Vitest fixtures declare the key Sets they build the quote from; each is diffed here with the real HTTP
answer, and the modal's exact body (the one `orderPricingPayload` builds, plus the create's own fields) is sent to the
real routes. The world is `courtesy_world`: the change applied, a cap of 2, the window open, an automatic entry.
"""

import pathlib
import re

import pytest

from business_app.models.order import Order
from tests.integration.price_change_builders import make_admin
from tests.integration.price_courtesy_builders import BASKET, courtesy_world, entry_of, switch, water
from tests.integration.reorder_reminder_builders import claim_headers
from tests.unit.test_lapsed_customers_ui_contract import _declared_key_set

pytestmark = pytest.mark.integration

ADMIN_UI = pathlib.Path(__file__).resolve().parents[2] / "admin_ui" / "src"
PAGE_TEST = ADMIN_UI / "__tests__" / "pages" / "Orders.priceQuote.test.js"
PANEL = ADMIN_UI / "components" / "orders" / "OrderQuotePanel.jsx"
QUOTE = "/api/v1/admin/orders/quote"
CREATE = "/api/v1/admin/orders"


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


@pytest.fixture
def product(db, sample_product):
    return water(db, sample_product)


def _modal_body(w, **extra):
    """The create body the modal sends: its form fields, the rows reduced to `{product_id, quantity}`."""
    return {
        "user_id": w.customer.user.id,
        "delivery_address_id": w.customer.address.id,
        "payment_method": "cash",
        "delivery_notes": "",
        "consume_marking_codes": False,
        "items": [{"product_id": w.product.id, "quantity": BASKET}],
        **extra,
    }


def _quote_body(w):
    """What `orderPricingPayload` builds for the quote: customer, address, method and the reduced rows."""
    body = _modal_body(w)
    return {key: body[key] for key in ("user_id", "delivery_address_id", "payment_method", "items")}


def _orders_of(db, user):
    db.session.expire_all()
    return Order.query.filter(Order.user_id == user.id).all()


def test_the_quote_answers_the_body_the_modal_sends_with_the_fixture_keys(app, db, client, product, operator_user):
    w = courtesy_world(app, db, product, make_admin(db))
    headers = claim_headers(app, operator_user, "operator")

    response = client.post(QUOTE, json=_quote_body(w), headers=headers)

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert set(data) == _declared_key_set(PAGE_TEST, "QUOTE_KEYS")
    [line] = data["items"]
    assert set(line) == _declared_key_set(PAGE_TEST, "QUOTE_ITEM_KEYS")
    assert line["courtesy_quantity"] == 2
    assert line["courtesy_unit_price"] == 18000.0
    assert data["total_amount"] == 76000.0
    assert entry_of(db, w.customer.user).order_id is None


def test_the_shown_total_is_accepted_as_expected_total(app, db, client, product, operator_user):
    w = courtesy_world(app, db, product, make_admin(db))
    headers = claim_headers(app, operator_user, "operator")
    shown = client.post(QUOTE, json=_quote_body(w), headers=headers).get_json()["data"]["total_amount"]
    before = len(_orders_of(db, w.customer.user))

    response = client.post(CREATE, json=_modal_body(w, expected_total=shown), headers=headers)

    assert response.status_code == 201, response.get_json()
    assert len(_orders_of(db, w.customer.user)) == before + 1
    assert entry_of(db, w.customer.user).order_id is not None


def test_a_stale_total_is_refused_with_a_quote_of_the_same_shape(app, db, client, product, operator_user):
    w = courtesy_world(app, db, product, make_admin(db), switched=False)
    headers = claim_headers(app, operator_user, "operator")
    shown = client.post(QUOTE, json=_quote_body(w), headers=headers).get_json()["data"]
    assert shown["total_amount"] == 72000.0
    switch(db)
    before = len(_orders_of(db, w.customer.user))

    refused = client.post(CREATE, json=_modal_body(w, expected_total=shown["total_amount"]), headers=headers)

    assert refused.status_code == 409, refused.get_json()
    body = refused.get_json()
    assert body["error_code"] == "STATE_CHANGED"
    assert body["details"]["reason"] == "price_changed"
    assert set(body["details"]["quote"]) == _declared_key_set(PAGE_TEST, "QUOTE_KEYS")
    assert body["details"]["quote"]["total_amount"] == 76000.0
    assert len(_orders_of(db, w.customer.user)) == before
    assert entry_of(db, w.customer.user).order_id is None


def test_the_panel_reads_only_published_fields():
    source = PANEL.read_text(encoding="utf-8")
    quote_keys = _declared_key_set(PAGE_TEST, "QUOTE_KEYS")
    item_keys = _declared_key_set(PAGE_TEST, "QUOTE_ITEM_KEYS")

    assert set(re.findall(r"\bquote\??\.(\w+)", source)) <= quote_keys
    assert set(re.findall(r"\b(?:line|item)\??\.(\w+)", source)) <= item_keys
    # The panel computes no money: a figure it prints is a field the server sent.
    assert not re.search(r"[\w)\]]\s[-+*/]\s[\w(]", re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`|//[^\n]*", "", source).replace("</", "").replace("/>", "")), (
        "OrderQuotePanel does arithmetic"
    )
