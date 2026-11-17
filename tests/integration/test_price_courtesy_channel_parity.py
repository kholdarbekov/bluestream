"""Every channel quotes what it charges, on both sides of the switch (spec §9 risk 3, §11; plan 2026-11-16 Task 30).

One customer, five channels, five states. For each cell the channel's own quote route is read first, the order is
placed through the channel's own create route with that quote's total as `expected_total`, and the stored order must
match the quote line for line and equal the state's expected total. There is no production code here: a failing cell
is a defect in the task that owns that channel, so fix it there. The world comes from `price_courtesy_builders.py`;
the expected totals are the builders' constants, never computed by the code under test.

A second matrix runs every channel with a delivery fee configured, for a regular and a premium customer: the quote
carries the fee the create charges (final review ORDERS-1 / CROSS-1; every row above runs at a fee of 0).
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.serializers.order_serializers import serialize_order_item
from tests.integration.price_courtesy_builders import (
    BASKET,
    COURTESY_TOTAL,
    NEW_TOTAL,
    OLD_TOTAL,
    courtesy_world,
    entry_of,
    water,
)
from tests.integration.reorder_reminder_builders import claim_headers
from tests.integration.test_staff_sales_visits_api import (
    OUTLETS,
    VISITS,
    WORKPLACE_PAYLOAD,
    _approved_outlet,
    _at_order_step,
)

pytestmark = pytest.mark.integration

CHANNELS = ["telegram", "web", "operator", "admin", "sales_agent"]
STATES = {  # state -> (courtesy_world kwargs, expected total, expected courtesy units)
    "before_switch": (dict(switched=False, eligible=True), OLD_TOTAL, None),
    "eligible_after_switch": (dict(switched=True, eligible=True), COURTESY_TOTAL, 2),
    "used_after_switch": (dict(switched=True, eligible=True, used=True), NEW_TOTAL, None),
    "ordered_in_notice_after_switch": (dict(switched=True, eligible=False), NEW_TOTAL, None),
    "new_customer_after_switch": (dict(switched=True, new=True), NEW_TOTAL, None),
}
LINE_KEYS = (
    "unit_price",
    "courtesy_quantity",
    "courtesy_unit_price",
    "regular_quantity",
    "discount_amount",
    "total_price",
)

ORDERS = "/api/v1/orders/"
CART_ESTIMATE = "/api/v1/orders/cart/estimate"
OPERATOR_ESTIMATE = "/api/v1/staff/operator/users/{}/order-estimate"
OPERATOR_CREATE = "/api/v1/staff/operator/orders"
ADMIN_QUOTE = "/api/v1/admin/orders/quote"
ADMIN_CREATE = "/api/v1/admin/orders"


@pytest.fixture
def client(app):
    """A fresh client per test: the session-scoped one carries JWT cookies from test to test."""
    return app.test_client()


def _items(w):
    return [{"product_id": w.product.id, "quantity": BASKET}]


def _line(published: dict) -> dict:
    return {key: published.get(key) for key in LINE_KEYS}


def _order_of(db, order_id) -> Order:
    db.session.expire_all()
    return db.session.get(Order, order_id)


class Rig:
    """The five channels' adapters: each sends the body its real client sends. `quote(channel)(w)` answers
    `(total, first line)`; `place(channel)(w, expected_total=None)` places the order and answers the stored `Order`."""

    def __init__(self, app, client, db, admin, operator_headers, agent_headers):
        self.app, self.client, self.db = app, client, db
        self.admin_headers = claim_headers(app, admin, "admin")
        self.operator_headers, self.agent_headers = operator_headers, agent_headers

    def quote(self, channel):
        return getattr(self, f"_quote_{channel}" if channel not in ("telegram", "web") else "_quote_customer")

    def place(self, channel):
        if channel in ("telegram", "web"):
            return lambda w, expected_total=None: self._place_customer(w, channel, expected_total)
        return getattr(self, f"_place_{channel}")

    # telegram and web: the customer's estimate, then the order route with the self-service source
    def _quote_customer(self, w):
        response = self.client.post(
            CART_ESTIMATE,
            json={"items": _items(w), "payment_method": "cash"},
            headers=w.customer.headers,
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()["data"]
        return data["pricing"]["final_total"], _line(data["items"][0])

    def _place_customer(self, w, source, expected_total):
        body = {
            "items": _items(w),
            "delivery_address_id": w.customer.address.id,
            "payment_method": "cash",
            "source": source,
        }
        if expected_total is not None:
            body["expected_total"] = expected_total
        response = self.client.post(ORDERS, json=body, headers=w.customer.headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        return _order_of(self.db, response.get_json()["data"]["order"]["id"])

    # operator: the phone-order estimate, then the phone-order create
    def _quote_operator(self, w):
        response = self.client.post(
            OPERATOR_ESTIMATE.format(w.customer.user.id),
            json={"items": _items(w), "payment_method": "cash"},
            headers=self.operator_headers,
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()["data"]
        return data["total_amount"], _line(data["items"][0])

    def _place_operator(self, w, expected_total=None):
        body = {
            "client_id": w.customer.user.id,
            "items": _items(w),
            "delivery_address_id": w.customer.address.id,
            "payment_method": "cash",
            "delivery_notes": None,
        }
        if expected_total is not None:
            body["expected_total"] = expected_total
        response = self.client.post(OPERATOR_CREATE, json=body, headers=self.operator_headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        return _order_of(self.db, response.get_json()["data"]["id"])

    # admin: the create modal's quote, then its create
    def _admin_body(self, w):
        return {
            "user_id": w.customer.user.id,
            "items": _items(w),
            "delivery_address_id": w.customer.address.id,
            "payment_method": "cash",
        }

    def _quote_admin(self, w):
        response = self.client.post(ADMIN_QUOTE, json=self._admin_body(w), headers=self.admin_headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        data = response.get_json()["data"]
        return data["total_amount"], _line(data["items"][0])

    def _place_admin(self, w, expected_total=None):
        body = {
            **self._admin_body(w),
            "delivery_notes": "",
            "consume_marking_codes": False,
            "delivery_date": None,
            "delivery_window_start": None,
            "delivery_window_end": None,
        }
        if expected_total is not None:
            body["expected_total"] = expected_total
        response = self.client.post(ADMIN_CREATE, json=body, headers=self.admin_headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        return _order_of(self.db, response.get_json()["data"]["order"]["id"])

    # sales_agent: the outlet's estimate, then the visit's order step
    def _quote_sales_agent(self, w):
        response = self.client.post(
            f"{OUTLETS}/{w.customer.outlet['id']}/order-estimate",
            json={"items": _items(w), "payment_method": "cash"},
            headers=self.agent_headers,
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        estimate = response.get_json()["data"]["estimate"]
        return estimate["total_amount"], _line(estimate["items"][0])

    def _place_sales_agent(self, w, expected_total=None):
        visit_id = _at_order_step(self.client, self.agent_headers, w.customer.outlet["id"], w.product)
        body = {"items": _items(w), "payment_method": "cash"}
        if expected_total is not None:
            body["expected_total"] = expected_total
        response = self.client.post(f"{VISITS}/{visit_id}/order", json=body, headers=self.agent_headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        # The agent closes the visit, so a later order of the same outlet starts a fresh one.
        closed = self.client.post(f"{VISITS}/{visit_id}/close", json={"notes": "done"}, headers=self.agent_headers)
        assert closed.status_code == 200, closed.get_data(as_text=True)
        return _order_of(self.db, response.get_json()["data"]["order"]["id"])


@pytest.fixture
def rig(app, client, db, admin_user, operator_auth_headers, sales_agent_auth_headers):
    return Rig(app, client, db, admin_user, operator_auth_headers, sales_agent_auth_headers)


@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("channel", CHANNELS)
def test_the_quote_is_the_charge(
    app, client, db, rig, sample_product, admin_user, operator_auth_headers, sales_agent_auth_headers, channel, state
):
    world_kwargs, expected_total, expected_courtesy_units = STATES[state]
    sample_product.in_sales_stock_check = True
    product = water(db, sample_product)
    extra = {}
    if channel == "sales_agent":
        # The approved WORKPLACE outlet's user is the customer; the outlet exists before the history and the switch.
        outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers, WORKPLACE_PAYLOAD)
        address = db.session.get(UserAddress, outlet["address_id"])
        extra["customer"] = SimpleNamespace(user=address.user, address=address, headers=None, outlet=outlet)
    w = courtesy_world(
        app, db, product, admin_user, place=rig.place(channel), phone="+998901515001", **world_kwargs, **extra
    )

    quote_total, quote_line = rig.quote(channel)(w)
    order = rig.place(channel)(w, expected_total=quote_total)

    assert Decimal(str(quote_total)) == order.total_amount == expected_total
    (stored,) = order.order_items
    assert _line(serialize_order_item(stored)) == quote_line
    assert quote_line["courtesy_quantity"] == expected_courtesy_units
    if expected_courtesy_units is not None:
        assert entry_of(db, w.customer.user).order_id == order.id


DELIVERY_FEE = 5000
# What each channel's create charges for delivery with `DEFAULT_DELIVERY_FEE` set: `DeliveryService`'s fee, except on
# the operator's phone order, whose quote and charge both take the fee the operator's payload carries (none sent: 0).
CHARGED_FEE = {channel: Decimal(DELIVERY_FEE) for channel in CHANNELS} | {"operator": Decimal("0")}


@pytest.mark.parametrize("premium", [False, True], ids=["regular", "premium"])
@pytest.mark.parametrize("channel", CHANNELS)
def test_the_quote_carries_the_delivery_fee_the_order_is_charged(
    app,
    client,
    db,
    rig,
    monkeypatch,
    sample_product,
    admin_user,
    operator_auth_headers,
    sales_agent_auth_headers,
    channel,
    premium,
):
    """(Final review ORDERS-1 / CROSS-1) With a delivery fee configured, every channel's quote carries the fee its
    create charges, so the order posted with the quote's total is accepted. `DeliveryService` decides the fee once:
    the sales agent's estimate no longer quotes 0, and the cart no longer quotes a premium customer the free delivery
    the order never gave."""
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", DELIVERY_FEE)
    sample_product.in_sales_stock_check = True
    product = water(db, sample_product)
    extra = {}
    if channel == "sales_agent":
        outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers, WORKPLACE_PAYLOAD)
        address = db.session.get(UserAddress, outlet["address_id"])
        extra["customer"] = SimpleNamespace(user=address.user, address=address, headers=None, outlet=outlet)
    w = courtesy_world(app, db, product, admin_user, place=rig.place(channel), phone="+998901515002", **extra)
    w.customer.user.is_premium = premium
    db.session.commit()

    quote_total, quote_line = rig.quote(channel)(w)
    order = rig.place(channel)(w, expected_total=quote_total)

    assert order.delivery_fee == CHARGED_FEE[channel]
    assert Decimal(str(quote_total)) == order.total_amount == COURTESY_TOTAL + CHARGED_FEE[channel]
    (stored,) = order.order_items
    assert _line(serialize_order_item(stored)) == quote_line
