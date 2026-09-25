"""/api/v1/staff/sales/visits/* and the due list, driven with the payloads the staff bot sends.

Everything here goes through HTTP with `sales_agent_auth_headers`, because the visit loop's whole
point is that the BOT reads fields the backend published (`overdue_days`, `suggested_qty`,
`open_visit_id`, `confirmation.state`) instead of deriving them (D15). A service-level test cannot
see a route that forgot to publish one, and a green service suite is exactly how the phase-1
`type=bool` and payload-drift defects shipped.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
)
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.models.user import User
from business_app.services.order_service import OrderService
from business_app.services.sales import agent_order_confirmation_service as confirmation_module
from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.services.sales.visit_service import VisitService
from business_app.utils.delivery_window import local_now
from shared.enums import CorporateContractTrackingMode, OrderStatus
from tests.integration.test_staff_sales_outlets_api import BOT_PAYLOAD, OUTLETS
from tests.unit.test_outlet_dedupe import PIN
from tests.integration.tier_discount_factory import seed_program, seed_tier
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

VISITS = "/api/v1/staff/sales/visits"
STOCK_CHECK_PRODUCTS = "/api/v1/staff/sales/stock-check-products"
# The eleven-key superset the bot's stock screen renders: `serialize_stock_check` plus the
# `last_order_qty` the "Same as last order" button is built from. Pinned as a SET so a route that
# drops one -- or quietly adds a twelfth the bot ignores -- fails here rather than in Tashkent.
STOCK_CHECK_ROW_KEYS = {
    "product_id",
    "product_name",
    "on_hand_qty",
    "empties_qty",
    "is_sold_out",
    "is_low",
    "suggested_qty",
    "accepted_qty",
    "rate_per_day",
    "rate_source",
    "last_order_qty",
}
# What `POST /visits/<id>/order` publishes about the order it just wrote: the four fields every
# visit surface shows PLUS the schedule the backend RESOLVED (D15). The agent reads the delivery
# day back off the reply; the bot never re-derives it from the payload it sent, which may have
# carried no date at all or a window the outlet's standing hours supplied.
ORDER_PLACED_KEYS = {
    "id",
    "order_number",
    "status",
    "total_amount",
    "delivery_date",
    "delivery_window_start",
    "delivery_window_end",
}
INDIVIDUAL_PAYLOAD = {
    "name": "Anvar aka uyi",
    "outlet_type": "individual",
    "contact": {"name": "Anvar aka", "phone": "+998901112277", "role": "owner"},
    "latitude": PIN[0],
    "longitude": PIN[1],
    "address_text": "Chilonzor 5-kvartal, 77",
    "district": "chilanzar",
    "class": "C",
}
WORKPLACE_PAYLOAD = {
    "name": "Yashnobod ofis",
    "outlet_type": "workplace",
    "contact": {"name": "Dilnoza opa", "phone": "+998901112299", "role": "owner"},
    "latitude": PIN[0],
    "longitude": PIN[1],
    "address_text": "Chilonzor 5-kvartal, 44",
    "district": "chilanzar",
    "class": "A",
}


@pytest.fixture
def flagged_product(db, sample_product):
    """The one SKU the agent counts on the shelf. `sample_product` ships with no flags set, and
    the admin write path for this column is Task 7 -- so the flag is set on the row directly."""
    sample_product.in_sales_stock_check = True
    db.session.commit()
    return sample_product


def _approved_outlet(client, agent_headers, operator_headers, payload=None):
    """A live outlet: created by the agent, activated by the operator, so it carries BOTH a
    `user_id` and an `address_id` -- the two columns every money route gates on."""
    created = client.post(OUTLETS, json=payload or BOT_PAYLOAD, headers=agent_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    outlet = created.get_json()["data"]["outlet"]
    requested = client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=agent_headers)
    assert requested.status_code == 200, requested.get_data(as_text=True)
    approved = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=operator_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    return approved.get_json()["data"]["outlet"]


def _fund_business_account(db, user_id, product):
    """An active workplace contract that covers `product` and has prepaid units behind it.

    Mirrors tests/integration/test_order_create_business_account_default.py::covered_contract --
    the repo's precedent for "this workplace may really settle on its business account". Outlet
    approval mints the WORKPLACE customer but no contract (only grocery stores get the AMOUNT
    one), so the coverage is set up here exactly as an admin would have.
    """
    contract = CorporateContract(
        user_id=user_id,
        contract_number=f"WP-{uuid4().hex[:10]}",
        name="Workplace coverage",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
        tracking_mode=CorporateContractTrackingMode.UNITS,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=product.id,
            unit_price=Decimal("15000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
    )
    account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
    db.session.add(account)
    db.session.flush()
    db.session.add(
        CorporatePrepaymentBalance(
            account_id=account.id,
            product_id=product.id,
            prepaid_units=Decimal("50.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
    )
    db.session.commit()
    return contract


def _start(client, headers, outlet_id):
    started = client.post(f"{OUTLETS}/{outlet_id}/visits", headers=headers)
    assert started.status_code == 201, started.get_data(as_text=True)
    return started.get_json()["data"]["visit"]


def _at_order_step(client, headers, outlet_id, product, on_hand=3):
    """The three taps that precede the order screen in the staff bot."""
    visit_id = _start(client, headers, outlet_id)["id"]
    checked_in = client.post(f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=headers)
    assert checked_in.status_code == 200, checked_in.get_data(as_text=True)
    stock = client.post(
        f"{VISITS}/{visit_id}/stock-check",
        json={"items": [{"product_id": product.id, "on_hand_qty": on_hand}]},
        headers=headers,
    )
    assert stock.status_code == 200, stock.get_data(as_text=True)
    return visit_id


def test_the_whole_visit_loop_runs_through_http(
    client, sales_agent_auth_headers, operator_auth_headers, sales_agent_user, flagged_product, db
):
    """start -> current -> checkin -> stock-check -> payment-methods -> order-estimate -> order -> close."""
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)

    visit = _start(client, sales_agent_auth_headers, outlet["id"])
    assert visit["current_step"] == "checkin" and visit["status"] == "in_progress"
    assert visit["outlet_id"] == outlet["id"] and visit["agent_user_id"] == sales_agent_user.id
    visit_id = visit["id"]

    current = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers)
    assert current.status_code == 200, current.get_data(as_text=True)
    body = current.get_json()["data"]
    assert body["visit"]["id"] == visit_id and body["visit"]["current_step"] == "checkin"
    assert body["outlet"]["id"] == outlet["id"] and body["outlet"]["open_visit_id"] == visit_id

    checked_in = client.post(
        f"{VISITS}/{visit_id}/checkin",
        json={"latitude": PIN[0], "longitude": PIN[1], "horizontal_accuracy": 12.0},
        headers=sales_agent_auth_headers,
    )
    assert checked_in.status_code == 200, checked_in.get_data(as_text=True)
    visit = checked_in.get_json()["data"]["visit"]
    assert visit["current_step"] == "stock"
    assert visit["in_radius"] is True and visit["distance_m"] < 5
    assert visit["checkin_skipped"] is False and visit["checkin_at"] is not None
    assert visit["checkin_latitude"] == PIN[0] and visit["checkin_accuracy_m"] == 12.0

    stock = client.post(
        f"{VISITS}/{visit_id}/stock-check",
        json={
            "items": [
                {
                    "product_id": flagged_product.id,
                    "on_hand_qty": 3,
                    "empties_qty": 2,
                    "is_sold_out": False,
                    "is_low": True,
                }
            ]
        },
        headers=sales_agent_auth_headers,
    )
    assert stock.status_code == 200, stock.get_data(as_text=True)
    body = stock.get_json()["data"]
    assert body["visit"]["current_step"] == "order"
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert set(row) == STOCK_CHECK_ROW_KEYS
    assert row["product_id"] == flagged_product.id and row["product_name"] == "Pure Water 19L"
    assert row["on_hand_qty"] == 3 and row["empties_qty"] == 2
    assert row["is_low"] is True and row["is_sold_out"] is False
    # First ever check on a store with no delivery history: there is nothing to measure from, so
    # the backend SAYS so (`none`/None) instead of inventing a rate the agent would have to trust.
    assert row["rate_source"] == "none" and row["rate_per_day"] is None
    assert row["suggested_qty"] is None and row["accepted_qty"] is None and row["last_order_qty"] is None

    methods = client.get(f"{OUTLETS}/{outlet['id']}/payment-methods", headers=sales_agent_auth_headers)
    assert methods.status_code == 200, methods.get_data(as_text=True)
    # A grocery store is not a workplace entity, so it has no business account -- and Click/Payme
    # are not an agent's to take at the counter. Exactly one method survives the filter.
    assert [m["method"] for m in methods.get_json()["data"]["methods"]] == ["cash"]

    estimate = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": flagged_product.id, "quantity": 8}]},
        headers=sales_agent_auth_headers,
    )
    assert estimate.status_code == 200, estimate.get_data(as_text=True)
    quote = estimate.get_json()["data"]["estimate"]
    assert quote["client_id"] == outlet["user_id"]
    assert quote["items"][0]["product_id"] == flagged_product.id and quote["items"][0]["quantity"] == 8
    assert quote["items"][0]["unit_price"] == 15000.0
    assert quote["subtotal"] == 120000.0

    tomorrow = local_now().date() + timedelta(days=1)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={
            "items": [{"product_id": flagged_product.id, "quantity": 8}],
            "payment_method": "cash",
            "delivery_date": tomorrow.isoformat(),
            "delivery_notes": "Leave at the counter",
        },
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 201, placed.get_data(as_text=True)
    body = placed.get_json()["data"]
    assert set(body["order"]) == ORDER_PLACED_KEYS
    assert body["order"]["status"] == "confirmed"
    assert body["order"]["total_amount"] > 0
    # The RESOLVED schedule, not an echo of the request: Task 12 prints this line back to the agent
    # so "when is it coming" is answered by the row that was written.
    assert body["order"]["delivery_date"] == tomorrow.isoformat()
    assert body["order"]["delivery_window_start"] is None and body["order"]["delivery_window_end"] is None
    # Nobody to ask: this outlet's customer has no telegram_id, so the backend confirms the order
    # itself and PUBLISHES which of the three states it landed in.
    assert body["confirmation"] == {"state": "confirmed"}
    assert body["visit"]["current_step"] == "close"
    assert body["visit"]["order"]["order_number"] == body["order"]["order_number"]
    assert set(body["visit"]["order"]) == {"id", "order_number", "status", "total_amount"}

    order = Order.query.get(body["order"]["id"])
    assert order.order_source == "sales_agent"
    assert order.created_by_staff_id == sales_agent_user.id
    assert order.visit_id == visit_id

    next_visit = local_now().date() + timedelta(days=7)
    closed = client.post(
        f"{VISITS}/{visit_id}/close",
        json={
            "notes": "Owner asked for a Friday delivery",
            "next_visit_at": next_visit.isoformat(),
            "dm_present": True,
        },
        headers=sales_agent_auth_headers,
    )
    assert closed.status_code == 200, closed.get_data(as_text=True)
    body = closed.get_json()["data"]
    # No outcome in the payload: an order exists, so the backend fills `order_placed` itself.
    assert body["visit"]["status"] == "completed" and body["visit"]["outcome"] == "order_placed"
    assert body["visit"]["next_visit_at"] == next_visit.isoformat()
    assert body["visit"]["ended_at"] is not None and body["visit"]["dm_present"] is True
    assert body["outlet"]["agent_next_visit_at"] == next_visit.isoformat()
    assert body["outlet"]["last_visit_at"] is not None
    assert body["outlet"]["open_visit_id"] is None

    assert client.get(f"{VISITS}/current", headers=sales_agent_auth_headers).status_code == 404


def test_a_workplace_outlet_orders_on_its_business_account(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """The OTHER rail an agent may take at the door (`AGENT_PAYMENT_METHODS`), end to end.

    Only a workplace entity with an active, covered contract is offered `business_account`, so
    proving the menu and the order path with a grocery store alone leaves half the rule untested:
    a route that hard-coded "cash" would still be green everywhere else in this file.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers, WORKPLACE_PAYLOAD)
    _fund_business_account(db, outlet["user_id"], flagged_product)

    methods = client.get(f"{OUTLETS}/{outlet['id']}/payment-methods", headers=sales_agent_auth_headers)
    assert methods.status_code == 200, methods.get_data(as_text=True)
    assert [m["method"] for m in methods.get_json()["data"]["methods"]] == ["cash", "business_account"]

    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={
            "items": [{"product_id": flagged_product.id, "quantity": 4}],
            "payment_method": "business_account",
        },
        headers=sales_agent_auth_headers,
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    body = placed.get_json()["data"]
    # The rail changes the money, not the contract: the reply is the same seven-key block the
    # cash path publishes, carrying the number that was written to the row.
    assert set(body["order"]) == ORDER_PLACED_KEYS
    # `business_account` settles synchronously against the prepaid balance inside
    # `create_order` itself (`PaymentService.initialize_order_payment` ->
    # `_handle_successful_payment` confirms a still-PENDING order the moment its payment
    # completes) -- by the time `open_or_confirm` runs the order is no longer PENDING, so the
    # published state is the "nothing left to ask" one, not the "asked nobody" one. The two
    # share nothing but the underlying order status; the test pins which STATE this rail earns.
    assert body["confirmation"] == {"state": "auto_confirmed"}
    order = Order.query.get(body["order"]["id"])
    assert body["order"]["total_amount"] == float(order.total_amount)
    assert body["order"]["status"] == order.status.value == "confirmed"
    method = order.payment_method
    assert (method.value if hasattr(method, "value") else method) == "business_account"
    assert order.order_source == "sales_agent" and order.visit_id == visit_id


def test_a_telegram_linked_store_is_asked_first_and_the_reply_says_which_state(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db, monkeypatch
):
    """The second of the three `confirmation.state` answers, through the route.

    Task 12 renders a DIFFERENT screen per state, and only `confirmed` was ever observed over
    HTTP. A route that published the request ROW's status ("pending") instead of the service's
    state word -- the two vocabularies deliberately share the word "confirmed" and mean
    different things by it -- would be green in every service test and unrenderable in the bot.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    User.query.get(outlet["user_id"]).telegram_id = "555000111"
    db.session.commit()
    pushed = []
    monkeypatch.setattr(confirmation_module, "_enqueue_push", pushed.append)

    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 4}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    body = placed.get_json()["data"]
    assert body["confirmation"] == {"state": "pending_confirmation"}
    assert set(body["order"]) == ORDER_PLACED_KEYS
    assert body["order"]["status"] == "pending"
    assert body["visit"]["current_step"] == "close"
    row = OrderConfirmationRequest.query.filter_by(order_id=body["order"]["id"]).one()
    assert (row.status, row.outlet_id) == ("pending", outlet["id"])
    assert len(row.request_id) == 16
    assert pushed == [body["order"]["id"]]


def test_a_returning_store_is_auto_confirmed_and_the_reply_says_which_state(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db, monkeypatch
):
    """The third answer -- and the ORDER of the two branches, which only this case proves.

    The store has Telegram, so `needs_request` is true; but `create_order`'s instant-COD block
    has already confirmed the order, and asking a store to confirm a CONFIRMED order puts two
    dead buttons in its chat. `auto_confirmed` must win, and no request row may exist.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    customer = User.query.get(outlet["user_id"])
    customer.telegram_id = "555000112"
    db.session.add(
        Order(
            user_id=customer.id,
            status=OrderStatus.DELIVERED,
            subtotal=Decimal("90000"),
            total_amount=Decimal("90000"),
            delivery_address_id=outlet["address_id"],
            order_source="web",
        )
    )
    db.session.commit()
    pushed = []
    monkeypatch.setattr(confirmation_module, "_enqueue_push", pushed.append)

    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 4}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    body = placed.get_json()["data"]
    assert body["confirmation"] == {"state": "auto_confirmed"}
    assert body["order"]["status"] == "confirmed"
    assert OrderConfirmationRequest.query.filter_by(order_id=body["order"]["id"]).count() == 0
    assert pushed == []


def test_a_schedule_the_web_checkout_would_refuse_is_a_400_the_bot_can_read(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """One schedule validator for every write path -- and its refusal has to survive the ROUTE.

    L47(4)/L105: the 400 used to carry NO `error_code`, so the bot had nothing to branch on and
    rendered its generic "Something went wrong" at the confirm screen for an agent who only
    mistyped a date. `SALES_DELIVERY_DATE_INVALID` is what lets Task 7 re-open the day screen;
    `details.validation_errors` still carries the validator's own sentences.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)

    refused = client.post(
        f"{VISITS}/{visit_id}/order",
        json={
            "items": [{"product_id": flagged_product.id, "quantity": 4}],
            "payment_method": "cash",
            "delivery_date": (local_now().date() - timedelta(days=1)).isoformat(),
        },
        headers=sales_agent_auth_headers,
    )

    assert refused.status_code == 400, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error_code"] == "SALES_DELIVERY_DATE_INVALID"
    assert body["message"]
    assert "delivery_date cannot be in the past" in body["details"]["validation_errors"]
    assert Order.query.filter_by(visit_id=visit_id).count() == 0


def test_an_explicit_empty_window_means_anytime_and_the_reply_says_so(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """Ruling 34: the outlet's standing window is a DEFAULT, and "" is the agent overriding it.

    Absence means "use the store's hours"; an explicit empty string means "anytime". The reply
    publishes the resolved schedule, so the bot never has to work out which of the two happened.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    windowed = client.put(
        f"{OUTLETS}/{outlet['id']}",
        json={"delivery_window_start": "09:00", "delivery_window_end": "12:00"},
        headers=sales_agent_auth_headers,
    )
    assert windowed.status_code == 200, windowed.get_data(as_text=True)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)

    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={
            "items": [{"product_id": flagged_product.id, "quantity": 4}],
            "payment_method": "cash",
            "delivery_window_start": "",
            "delivery_window_end": "",
        },
        headers=sales_agent_auth_headers,
    )

    assert placed.status_code == 201, placed.get_data(as_text=True)
    order_block = placed.get_json()["data"]["order"]
    assert order_block["delivery_window_start"] is None and order_block["delivery_window_end"] is None
    order = Order.query.get(order_block["id"])
    assert (order.delivery_window_start, order.delivery_window_end) == (None, None)


def test_payment_methods_answers_an_empty_menu_rather_than_an_error(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """"No rail is offered" is an ANSWER (200 + []), not a failure.

    Only a missing customer account or address is `SALES_OUTLET_NOT_ACTIVE`. A live outlet whose
    customer the backend currently offers nothing for -- here a legacy entity with no subtype --
    still gets a well-formed menu, which Task 12 renders as `staff.sales.error.outlet_not_active`
    on the order screen (ruling 24) instead of dropping the agent out of the visit.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    customer = User.query.get(outlet["user_id"])
    customer.entity_subtype = None
    db.session.commit()

    empty = client.get(f"{OUTLETS}/{outlet['id']}/payment-methods", headers=sales_agent_auth_headers)

    assert empty.status_code == 200, empty.get_data(as_text=True)
    assert empty.get_json()["data"]["methods"] == []

    prospect_payload = {
        **BOT_PAYLOAD,
        "name": "Prospekt do'kon",
        "contact": {"name": "Yangi", "phone": "+998901112288"},
        "force": True,
    }
    created = client.post(OUTLETS, json=prospect_payload, headers=sales_agent_auth_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    prospect_id = created.get_json()["data"]["outlet"]["id"]

    refused = client.get(f"{OUTLETS}/{prospect_id}/payment-methods", headers=sales_agent_auth_headers)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_OUTLET_NOT_ACTIVE"


def test_a_second_start_returns_the_open_visit_so_the_bot_can_offer_resume(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    again = client.post(f"{OUTLETS}/{outlet['id']}/visits", headers=sales_agent_auth_headers)

    assert again.status_code == 409, again.get_data(as_text=True)
    body = again.get_json()
    assert body["error_code"] == "SALES_VISIT_ALREADY_OPEN"
    # The bot's Resume / Abandon offer is built FROM this payload, so both ids have to be in it
    # and nothing else may ride along.
    assert body["details"] == {"visit_id": visit_id, "outlet_id": outlet["id"]}


def test_a_double_tap_that_slips_past_the_check_still_answers_the_same_409(
    client, sales_agent_auth_headers, operator_auth_headers, monkeypatch, db
):
    """The pre-check and the INSERT are not one atomic unit.

    `uq_visits_one_open_per_agent` is the real guard, and its IntegrityError has to come back as
    SALES_VISIT_ALREADY_OPEN with the same two ids -- not a 500 -- or an agent whose visit IS open
    reads "Something went wrong" and taps *Start visit* again. The race is simulated by blinding
    the pre-check exactly once, which is precisely what a concurrent request does to it for real.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    real_current = VisitService.current
    calls = {"n": 0}

    def blind_once(agent_user_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_current(agent_user_id)

    monkeypatch.setattr(VisitService, "current", staticmethod(blind_once))

    raced = client.post(f"{OUTLETS}/{outlet['id']}/visits", headers=sales_agent_auth_headers)

    assert raced.status_code == 409, raced.get_data(as_text=True)
    body = raced.get_json()
    assert body["error_code"] == "SALES_VISIT_ALREADY_OPEN"
    # The SAME payload the pre-check arm produces (`VisitService._already_open`), because the bot
    # builds one Resume/Abandon offer and cannot know which arm answered it.
    assert body["details"] == {"visit_id": visit_id, "outlet_id": outlet["id"]}
    # Two calls: the blinded pre-check, then the re-read the IntegrityError arm made.
    assert calls["n"] == 2


def test_the_step_guards_answer_with_their_codes_over_http(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """The bot branches on `error_code`, so every guard has to survive the ROUTE.

    `API_ERROR_CODE_KEY_MAP` (Task 9) turns each of these codes into a specific Uzbek sentence;
    a route that swallowed one, or answered 500, would give the agent "Something went wrong" at
    the counter. Service-level tests cannot see that -- this drives all four through HTTP.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    # (a) stock check before check-in: the step machine refuses, and NAMES the step it wants.
    too_early = client.post(
        f"{VISITS}/{visit_id}/stock-check",
        json={"items": [{"product_id": flagged_product.id, "on_hand_qty": 3}]},
        headers=sales_agent_auth_headers,
    )
    assert too_early.status_code == 400, too_early.get_data(as_text=True)
    assert too_early.get_json()["error_code"] == "SALES_VISIT_STEP_INVALID"
    assert too_early.get_json()["details"]["current_step"] == "checkin"

    checked_in = client.post(
        f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers
    )
    assert checked_in.status_code == 200, checked_in.get_data(as_text=True)

    # (b) checking in twice: the same guard, from the other side of the step.
    twice = client.post(
        f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers
    )
    assert twice.status_code == 400, twice.get_data(as_text=True)
    assert twice.get_json()["error_code"] == "SALES_VISIT_STEP_INVALID"
    assert twice.get_json()["details"]["current_step"] == "stock"

    # (c) a fat-fingered count: refused above SALES_STOCK_QTY_MAX, and nothing is written.
    too_many = client.post(
        f"{VISITS}/{visit_id}/stock-check",
        json={"items": [{"product_id": flagged_product.id, "on_hand_qty": 501}]},
        headers=sales_agent_auth_headers,
    )
    assert too_many.status_code == 400, too_many.get_data(as_text=True)
    assert too_many.get_json()["error_code"] == "SALES_STOCK_QTY_INVALID"

    ok = client.post(
        f"{VISITS}/{visit_id}/stock-check",
        json={"items": [{"product_id": flagged_product.id, "on_hand_qty": 3}]},
        headers=sales_agent_auth_headers,
    )
    assert ok.status_code == 200, ok.get_data(as_text=True)

    body = {"items": [{"product_id": flagged_product.id, "quantity": 4}], "payment_method": "cash"}
    first = client.post(f"{VISITS}/{visit_id}/order", json=body, headers=sales_agent_auth_headers)
    assert first.status_code == 201, first.get_data(as_text=True)

    # (d) the double tap on "Order suggested": one visit, at most one order -- and the 409 names
    # the order that already exists, which is what the bot prints back (ruling 25).
    again = client.post(f"{VISITS}/{visit_id}/order", json=body, headers=sales_agent_auth_headers)

    assert again.status_code == 409, again.get_data(as_text=True)
    assert again.get_json()["error_code"] == "SALES_VISIT_ORDER_EXISTS"
    details = again.get_json()["details"]
    assert details["order"]["order_number"] == first.get_json()["data"]["order"]["order_number"]
    assert Order.query.filter_by(visit_id=visit_id).count() == 1


def test_the_losing_tap_of_the_real_race_gets_the_409_and_leaves_no_orphan_order(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db, monkeypatch
):
    """The double tap the `visit.order` guard CANNOT catch (C02/C00).

    `create_order` commits its own `atomic_transaction`, so the FOR UPDATE lock `place_order`
    holds over the visit row is released BEFORE `orders.visit_id` is written. The losing tap
    therefore unblocks, reads `visit.order is None`, and walks straight into `create_order` --
    where only `uq_orders_visit_id` stands between the shop and a second real load of water.
    The interleaving is reproduced exactly: the winner's `visit_id` write lands after the loser's
    guard and before its INSERT.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    body = {"items": [{"product_id": flagged_product.id, "quantity": 4}], "payment_method": "cash"}

    first = client.post(f"{VISITS}/{visit_id}/order", json=body, headers=sales_agent_auth_headers)
    assert first.status_code == 201, first.get_data(as_text=True)
    winner_id = first.get_json()["data"]["order"]["id"]

    # Rewind to the instant the loser's guard really saw: the winner's order exists, its
    # `visit_id` write has not committed yet. `current_step` rewinds with it -- `place_order`
    # writes "close" in its OWN commit, after `create_order` has already committed `visit_id`,
    # so at the instant this reproduces the visit is still on "order" and the M02 step guard is
    # not what the loser meets.
    db.session.execute(text("UPDATE orders SET visit_id = NULL WHERE id = :id"), {"id": winner_id})
    db.session.execute(
        text("UPDATE visits SET current_step = 'order' WHERE id = :id"), {"id": visit_id}
    )
    db.session.commit()

    real_create_order = OrderService.create_order

    def create_order_after_the_winner_lands(self, user_id, order_data):
        db.session.execute(
            text("UPDATE orders SET visit_id = :visit WHERE id = :id"),
            {"visit": visit_id, "id": winner_id},
        )
        db.session.commit()
        return real_create_order(self, user_id, order_data)

    monkeypatch.setattr(OrderService, "create_order", create_order_after_the_winner_lands)

    loser = client.post(f"{VISITS}/{visit_id}/order", json=body, headers=sales_agent_auth_headers)

    assert loser.status_code == 409, loser.get_data(as_text=True)
    assert loser.get_json()["error_code"] == "SALES_VISIT_ORDER_EXISTS"
    details = loser.get_json()["details"]
    assert details["order"]["order_number"] == first.get_json()["data"]["order"]["order_number"]
    # No orphan: the unique fired INSIDE create_order's transaction, so its order, its items, its
    # payment row and its reservation all rolled back together. The store owns exactly one order.
    assert Order.query.filter_by(user_id=outlet["user_id"]).count() == 1
    assert Order.query.filter_by(visit_id=visit_id).count() == 1


def test_a_sub_minimum_line_is_refused_with_a_code_on_the_quote_and_on_the_order(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """C23/#30: `min_order_quantity` is published to every surface and was enforced on none.

    The only guard lived inside `create_order` as a code-less 400 -- so the agent reached the
    confirm screen, read a total out to the shopkeeper, tapped Place order and got "Something
    went wrong". The refusal now arrives on the screen that can still change the number, and
    NAMES the product, the floor and what was asked for so the bot can say which line to fix.
    """
    flagged_product.min_order_quantity = 2
    db.session.commit()
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    expected_details = {
        "product_id": flagged_product.id,
        "product_name": "Pure Water 19L",
        "min_order_quantity": 2,
        "quantity": 1,
    }

    quoted = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": flagged_product.id, "quantity": 1}]},
        headers=sales_agent_auth_headers,
    )
    assert quoted.status_code == 400, quoted.get_data(as_text=True)
    assert quoted.get_json()["error_code"] == "SALES_ORDER_MIN_QTY"
    # `@handle_api_exception` adds its own `validation_errors: []`; the four keys below are the
    # contract the bot reads.
    assert expected_details.items() <= quoted.get_json()["details"].items()

    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 1}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 400, placed.get_data(as_text=True)
    assert placed.get_json()["error_code"] == "SALES_ORDER_MIN_QTY"
    assert expected_details.items() <= placed.get_json()["details"].items()
    assert Order.query.filter_by(visit_id=visit_id).count() == 0

    # ...and the floor is a floor, not a ban: two is fine on both doors.
    ok_quote = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": flagged_product.id, "quantity": 2}]},
        headers=sales_agent_auth_headers,
    )
    assert ok_quote.status_code == 200, ok_quote.get_data(as_text=True)
    ok_order = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 2}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert ok_order.status_code == 201, ok_order.get_data(as_text=True)


def test_a_line_above_the_quantity_ceiling_is_refused_with_a_code_on_the_quote_and_on_the_order(
    client, app, monkeypatch, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """M30: the ceiling that bounds a COUNTED figure bounds an ORDERED one, on both doors.

    Nothing bounded a quantity: `OrderItemPayload` only demands `ge=1`, so a fabricated
    suggestion, a fat finger or a replayed basket became a real, priced, inventory-reserved
    order in one tap. The refusal arrives on the screen that can still change the number and
    NAMES the product, the ceiling and what was asked for, exactly like the minimum does.
    """
    monkeypatch.setitem(app.config, "SALES_STOCK_QTY_MAX", 5)
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    expected_details = {
        "product_id": flagged_product.id,
        "product_name": "Pure Water 19L",
        "quantity": 6,
        "max_quantity": 5,
    }

    quoted = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": flagged_product.id, "quantity": 6}]},
        headers=sales_agent_auth_headers,
    )
    assert quoted.status_code == 400, quoted.get_data(as_text=True)
    assert quoted.get_json()["error_code"] == "SALES_ORDER_QTY_INVALID"
    assert expected_details.items() <= quoted.get_json()["details"].items()

    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 6}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 400, placed.get_data(as_text=True)
    assert placed.get_json()["error_code"] == "SALES_ORDER_QTY_INVALID"
    assert expected_details.items() <= placed.get_json()["details"].items()
    assert Order.query.filter_by(visit_id=visit_id).count() == 0

    # ...and the ceiling is a ceiling, not a ban: the last allowed unit goes through both doors.
    ok_quote = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": flagged_product.id, "quantity": 5}]},
        headers=sales_agent_auth_headers,
    )
    assert ok_quote.status_code == 200, ok_quote.get_data(as_text=True)
    ok_order = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 5}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert ok_order.status_code == 201, ok_order.get_data(as_text=True)


def test_an_order_below_the_gross_minimum_is_refused_with_the_code_and_the_floor(
    client, app, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """The `OrderService.create_order` gate the customer bot and admin already hit, now coded and
    reached through the sales visit's own order POST (`VisitService.place_order`).

    `flagged_product`'s 15000 UZS base price for a single unit falls under the configured floor
    at the outlet's own pin coordinates -- the same arithmetic
    `tests/unit/test_subscription_order_parity.py` already relies on for the identical refusal,
    and the same one `tests/unit/test_order_service.py` proves at the service level. This is the
    HTTP door the staff bot's sales agent actually posts to.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)

    response = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 1}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert body["error_code"] == "ORDER_MIN_AMOUNT"
    assert isinstance(body["details"]["min_amount"], float)
    assert body["details"]["min_amount"] == float(app.config["MIN_ORDER_AMOUNT"])
    assert Order.query.filter_by(visit_id=visit_id).count() == 0


def test_an_order_over_the_available_stock_is_refused_with_the_code(
    client, app, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """`OrderService._process_order_items`'s own availability check (order_service.py, the
    "Inventory check failed" raise), reached through the sales visit's order POST exactly as the
    customer bot and admin already reach it. `stock_quantity` is the WAREHOUSE column
    `InventoryService.check_product_availability` gates on -- unrelated to the visit's own
    `on_hand_qty` shelf count `_at_order_step` posted above, which is what the AGENT sees at the
    shop, not what the company has to fulfil from.
    """
    flagged_product.stock_quantity = 1
    db.session.commit()
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)

    response = client.post(
        f"{VISITS}/{visit_id}/order",
        # 5 units at 15000 UZS clears MIN_ORDER_AMOUNT (20000) so this refusal is
        # unambiguously the stock check, not the floor above.
        json={"items": [{"product_id": flagged_product.id, "quantity": 5}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "ORDER_STOCK_UNAVAILABLE"
    assert Order.query.filter_by(visit_id=visit_id).count() == 0


def test_a_visit_with_nothing_to_count_still_reaches_the_order_screen(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """B4/#01: until an admin flags a product, `GET /stock-check-products` answers an EMPTY list.

    The shelf screen then has nothing to ask about, and with `items` required to be non-empty the
    only way out of the visit was Abandon -- on every install, for every visit, until someone
    flipped a switch in the admin UI. An empty submission is a legitimate answer ("nothing is on
    the count list here") and advances the server's resume point like any other.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    catalogue = client.get(STOCK_CHECK_PRODUCTS, headers=sales_agent_auth_headers)
    assert catalogue.status_code == 200 and catalogue.get_json()["data"]["items"] == []

    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    client.post(f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers)

    empty = client.post(f"{VISITS}/{visit_id}/stock-check", json={"items": []}, headers=sales_agent_auth_headers)

    assert empty.status_code == 200, empty.get_data(as_text=True)
    body = empty.get_json()["data"]
    assert body["items"] == []
    assert body["visit"]["current_step"] == "order"
    assert body["visit"]["stock_checks"] == []
    # ...and the close path the agent needs from there is reachable.
    closed = client.post(
        f"{VISITS}/{visit_id}/close",
        json={"outcome": "no_order", "no_order_reason": "sufficient_stock"},
        headers=sales_agent_auth_headers,
    )
    assert closed.status_code == 200, closed.get_data(as_text=True)


def test_the_quote_and_the_charge_agree_on_the_cod_tier_discount(
    client, sales_agent_auth_headers, flagged_product, db
):
    """C27: the confirm screen's total is the total the store is charged.

    `order_estimate` replayed pricing through `price_phone_order`, which hard-codes
    `tier_discount = 0`, while `create_order` quotes a LIVE tier discount for the rail. An
    `individual` outlet gets an INDIVIDUAL customer account, which is loyalty-eligible, so the
    agent read one number out to the shopkeeper and the shop was charged another -- and the
    shop's own bot showed the second one. The rail now travels with the quote.
    """
    created = client.post(OUTLETS, json=INDIVIDUAL_PAYLOAD, headers=sales_agent_auth_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    outlet = created.get_json()["data"]["outlet"]
    # An individual outlet activates on creation (it needs no contract), so it is orderable now.
    assert outlet["stage"] == "active"
    seed_tier(db, seed_program(db), name="Base", rate=Decimal("7"))
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    basket = [{"product_id": flagged_product.id, "quantity": 8}]

    quoted = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": basket, "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert quoted.status_code == 200, quoted.get_data(as_text=True)
    estimate = quoted.get_json()["data"]["estimate"]
    assert estimate["tier_discount"] > 0  # the discount is really in play, not a vacuous match

    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": basket, "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 201, placed.get_data(as_text=True)
    assert placed.get_json()["data"]["order"]["total_amount"] == estimate["total_amount"]
    order = Order.query.get(placed.get_json()["data"]["order"]["id"])
    assert float(order.tier_discount) == estimate["tier_discount"]

    # The OTHER rail an agent may take is not a COD rail, so it must quote no discount at all --
    # proving the quote reads the rail rather than always applying the benefit.
    business = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": basket, "payment_method": "business_account"},
        headers=sales_agent_auth_headers,
    )
    assert business.status_code == 200, business.get_data(as_text=True)
    assert business.get_json()["data"]["estimate"]["tier_discount"] == 0.0

    # A rail an agent may never take is refused on the quote by the SAME guard the order uses.
    refused = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": basket, "payment_method": "click"},
        headers=sales_agent_auth_headers,
    )
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_PAYMENT_METHOD_INVALID"


def test_the_shelf_screen_opens_pre_filled_from_the_last_completed_visit(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """Ruling 29 / spec *Visit* step 2. `previous_stock` rides on the two routes that OPEN a
    screen -- POST /outlets/<id>/visits and GET /visits/current -- so the agent corrects last
    time's numbers instead of retyping them, and the D7 rate rule has an anchor."""
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)

    first_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    client.post(f"{VISITS}/{first_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers)
    client.post(
        f"{VISITS}/{first_id}/stock-check",
        json={"items": [{"product_id": flagged_product.id, "on_hand_qty": 9, "empties_qty": 4}]},
        headers=sales_agent_auth_headers,
    )
    closed = client.post(
        f"{VISITS}/{first_id}/close",
        json={"outcome": "no_order", "no_order_reason": "sufficient_stock"},
        headers=sales_agent_auth_headers,
    )
    assert closed.status_code == 200, closed.get_data(as_text=True)

    started = client.post(f"{OUTLETS}/{outlet['id']}/visits", headers=sales_agent_auth_headers)

    assert started.status_code == 201, started.get_data(as_text=True)
    visit = started.get_json()["data"]["visit"]
    assert visit["stock_checks"] == []
    assert [(r["product_id"], r["on_hand_qty"], r["empties_qty"]) for r in visit["previous_stock"]] == [
        (flagged_product.id, 9, 4)
    ]
    # EXACTLY the ten-key `serialize_stock_check` shape: `last_order_qty` is the stock-check
    # RESPONSE's eleventh key, computed per submission, and is not a column on the row. Pinned as
    # equality so the pre-fill and the live screen stay two known shapes rather than "some subset".
    assert set(visit["previous_stock"][0]) == STOCK_CHECK_ROW_KEYS - {"last_order_qty"}

    resumed = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers).get_json()["data"]["visit"]
    assert resumed["previous_stock"] == visit["previous_stock"]


def test_another_agents_visit_is_not_reachable(
    client, app, db, sales_agent_auth_headers, sales_agent_user, driver_auth_headers
):
    other = make_sales_agent_user(db, phone="+998901234579", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    other_outlet = Outlet(
        name="Yunusobod do'kon",
        outlet_type="grocery_store",
        stage="active",
        district="yunusabad",
        assigned_agent_user_id=other.id,
    )
    db.session.add(other_outlet)
    db.session.commit()
    with app.app_context():
        other_headers = {
            "Authorization": f"Bearer {create_access_token(identity=str(other.id))}",
            "Content-Type": "application/json",
        }

    foreign_visit_id = _start(client, other_headers, other_outlet.id)["id"]

    poached = client.post(
        f"{VISITS}/{foreign_visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers
    )

    assert poached.status_code == 403, poached.get_data(as_text=True)
    assert poached.get_json()["error_code"] == "SALES_VISIT_NOT_OWNED"
    # And the visit surface is agent-only, exactly like the outlet surface it hangs off.
    assert client.get(f"{VISITS}/current", headers=driver_auth_headers).status_code == 403


def test_abandon_frees_the_agent_to_start_again(client, sales_agent_auth_headers, operator_auth_headers, db):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    abandoned = client.post(f"{VISITS}/{visit_id}/abandon", headers=sales_agent_auth_headers)

    assert abandoned.status_code == 200, abandoned.get_data(as_text=True)
    visit = abandoned.get_json()["data"]["visit"]
    assert visit["status"] == "abandoned" and visit["ended_at"] is not None

    missing = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers)
    assert missing.status_code == 404
    assert missing.get_json()["error_code"] == "SALES_VISIT_NOT_FOUND"

    restarted = client.post(f"{OUTLETS}/{outlet['id']}/visits", headers=sales_agent_auth_headers)
    assert restarted.status_code == 201, restarted.get_data(as_text=True)


def test_abandoning_an_ordered_visit_closes_it_and_republishes_the_outlet(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """Ruling 74: Abandon is drawn on the POST-order screens too, and the tap must answer the
    way the hourly sweep does -- close the visit, not strand it.

    Through HTTP because that is where the defect lived: the endpoint had no guard at all, and
    its reply carried no `outlet`, so even a service that closed the visit left the bot with
    nothing to print the recomputed due date from.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={
            "items": [{"product_id": flagged_product.id, "quantity": 8}],
            "payment_method": "cash",
            "delivery_date": (local_now().date() + timedelta(days=1)).isoformat(),
        },
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 201, placed.get_data(as_text=True)

    abandoned = client.post(f"{VISITS}/{visit_id}/abandon", headers=sales_agent_auth_headers)

    assert abandoned.status_code == 200, abandoned.get_data(as_text=True)
    body = abandoned.get_json()["data"]
    assert body["visit"]["status"] == "completed"
    assert body["visit"]["outcome"] == "order_placed" and body["visit"]["no_order_reason"] is None
    assert body["visit"]["ended_at"] is not None
    # The same CARD the close route answers with -- the bot renders the close receipt off it,
    # and `next_visit_due_at` is the figure only `close` republishes.
    assert body["outlet"]["id"] == outlet["id"]
    assert body["outlet"]["next_visit_due_at"] is not None
    assert body["outlet"]["last_visit_at"] is not None
    assert body["outlet"]["open_visit_id"] is None

    # And the agent is free again.
    assert client.get(f"{VISITS}/current", headers=sales_agent_auth_headers).status_code == 404


def test_current_visit_resolves_the_open_visit_once(
    client, sales_agent_auth_headers, operator_auth_headers, db, count_queries
):
    """`GET /visits/current` asked "which visit is open for this agent" twice.

    The route resolves it to render the screen, and then `OutletService.card` asked the
    database the identical question again to fill `open_visit_id` — on the one screen the
    bot opens at every resume, every restart and every menu tap. The card still publishes
    the field (the bot derives nothing); it is handed the mapping the route already built.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    # The root conftest's counter (`_QueryCounter`), not a local listener: "how many
    # statements did that cost" has one implementation in this repo, and the sibling budget
    # test written in this same cycle (tests/unit/test_replenishment_service.py) uses it.
    with count_queries() as counted:
        current = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers)

    assert current.status_code == 200, current.get_data(as_text=True)
    body = current.get_json()["data"]
    # The two fields the bot gates Start-visit vs Resume on are unchanged ...
    assert body["visit"]["id"] == visit_id
    assert body["outlet"]["open_visit_id"] == visit_id
    # ... and they cost ONE "open visit for this agent" query. The predicate is what
    # identifies it: every visits SELECT lists the column, only `current()` filters on it.
    by_agent = [s for s in counted.statements if "FROM visits" in s and "visits.agent_user_id = " in s]
    assert len(by_agent) == 1, by_agent


def test_a_visit_that_ended_names_itself_in_the_409(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """`_not_open` is the ONE shape of SALES_VISIT_NOT_OPEN, and it read none of the visit.

    Its sibling `_already_open` publishes `details` the bot builds its Resume/Abandon offer
    from; this arm published an empty dict, so neither the agent's dead-end screen nor the
    operator's log line could say WHICH visit ended or how it ended — and the parameter the
    signature demanded was proof of nothing. Driven through the route because `get_owned`
    is the gate on every visit screen; a service-level assertion cannot see a body.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    ended = client.post(f"{VISITS}/{visit_id}/abandon", headers=sales_agent_auth_headers)
    assert ended.status_code == 200, ended.get_data(as_text=True)

    refused = client.post(
        f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers
    )

    assert refused.status_code == 409, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error_code"] == "SALES_VISIT_NOT_OPEN"
    assert body.get("details") == {"visit_id": visit_id, "status": "abandoned"}


def test_an_outlet_becomes_due_the_moment_it_is_activated(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """Spec :91 — a serving outlet that has never been visited is DUE NOW (C03, Task 13 finding).

    `outlets.next_visit_due_at` is the published SSOT the due scope reads, and its only writer was
    a CLOSED visit: every outlet activated today sat at NULL, NULL reads as "not due", and the
    agent's *Due today & overdue* list shipped empty on day one of every install. The stage is an
    INPUT to the D7 rule, so the column is republished wherever the stage is written -- while a
    prospect, which is not due until an agent says so, stays out.
    """
    created = client.post(OUTLETS, json=BOT_PAYLOAD, headers=sales_agent_auth_headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    outlet_id = created.get_json()["data"]["outlet"]["id"]

    def _due_ids():
        listed = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": "due"})
        assert listed.status_code == 200, listed.get_data(as_text=True)
        return listed.get_json()["data"]["items"]

    # A prospect is not due: nobody has promised the shop anything yet.
    assert Outlet.query.get(outlet_id).next_visit_due_at is None
    assert _due_ids() == []

    requested = client.post(f"{OUTLETS}/{outlet_id}/request-activation", headers=sales_agent_auth_headers)
    assert requested.status_code == 200, requested.get_data(as_text=True)
    assert Outlet.query.get(outlet_id).next_visit_due_at is None
    assert _due_ids() == []

    approved = client.post(f"{OUTLETS}/{outlet_id}/approve", json={}, headers=operator_auth_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)

    assert Outlet.query.get(outlet_id).next_visit_due_at is not None
    rows = _due_ids()
    assert [row["id"] for row in rows] == [outlet_id]
    assert rows[0]["overdue_days"] == 0


def test_due_scope_lists_only_what_is_due_and_publishes_overdue_days(
    client, sales_agent_auth_headers, operator_auth_headers, sales_agent_user, db
):
    due = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)  # "Bahor market"
    Outlet.query.get(due["id"]).next_visit_due_at = datetime.now(UTC) - timedelta(days=3)
    # Sorts FIRST by name and SECOND by due date, so the two orderings disagree: this row is the
    # only thing standing between `order_by(Outlet.next_visit_due_at.asc())` and the alphabetical
    # default every other scope uses. A work list sorted by name buries the most overdue store.
    less_overdue = Outlet(
        name="Alisher do'kon",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=sales_agent_user.id,
        next_visit_due_at=datetime.now(UTC) - timedelta(days=1),
    )
    later = Outlet(
        name="Navruz market",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=sales_agent_user.id,
        next_visit_due_at=datetime.now(UTC) + timedelta(days=5),
    )
    never = Outlet(
        name="Anor market",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=sales_agent_user.id,
        next_visit_due_at=None,
    )
    # C14: due LATER TODAY. The scope's boundary is the end of the agent's LOCAL day, and every
    # other row here sits a whole day either side of it -- so a regression to `<= now` (or a
    # `local_day_end_utc` that lost its `+1 day`) would keep them all green while dropping this
    # shop off the work list every single evening.
    due_later_today = ReplenishmentService.local_day_end_utc() - timedelta(minutes=1)
    assert due_later_today > datetime.now(UTC), "the boundary case must lie in the future"
    later_today = Outlet(
        name="Zafar market",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=sales_agent_user.id,
        next_visit_due_at=due_later_today,
    )
    # Past due AND assigned to this agent, but a prospect: it belongs in *Prospects*, never at the
    # top of today's work list. `DUE_STAGES` is what excludes it -- an outlet with no customer
    # account cannot take the order the visit exists to place.
    prospect_due = Outlet(
        name="Zomin prospekt",
        outlet_type="grocery_store",
        stage="prospect",
        assigned_agent_user_id=sales_agent_user.id,
        next_visit_due_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.session.add_all([less_overdue, later, never, prospect_due, later_today])
    db.session.commit()

    listed = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": "due"})

    assert listed.status_code == 200, listed.get_data(as_text=True)
    rows = listed.get_json()["data"]["items"]
    # Most overdue first, NOT alphabetical -- and the prospect is absent despite being past due.
    assert [row["id"] for row in rows] == [due["id"], less_overdue.id, later_today.id]
    # Not yet overdue is 0, never negative -- the button label reads "due today".
    assert [row["overdue_days"] for row in rows] == [3, 1, 0]
    assert prospect_due.id not in [row["id"] for row in rows]
    assert rows[0]["has_open_visit"] is False and rows[0]["open_visit_id"] is None

    # Every scope answers in the SAME shape, so the bot has one row renderer -- and an outlet with
    # no due date says null, not 0, which the button label would print as "+0d / due today".
    everything = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": "all"})
    assert everything.status_code == 200, everything.get_data(as_text=True)
    by_id = {row["id"]: row for row in everything.get_json()["data"]["items"]}
    assert by_id[due["id"]]["overdue_days"] == 3
    assert by_id[later.id]["overdue_days"] == 0
    assert by_id[never.id]["overdue_days"] is None

    # `has_open_visit` is the flag the row is filtered on; `open_visit_id` is what the Resume
    # button is BUILT from, and every row so far has answered False/None -- so a route that
    # published the flag with a null id (or resolved the map for the card only) would still be
    # green while the bot offered Resume on a visit it cannot open. One open visit per agent,
    # so the other two rows must not inherit it.
    visit_id = _start(client, sales_agent_auth_headers, due["id"])["id"]

    with_open = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": "due"})
    assert with_open.status_code == 200, with_open.get_data(as_text=True)
    reread = {
        row["id"]: (row["has_open_visit"], row["open_visit_id"]) for row in with_open.get_json()["data"]["items"]
    }
    assert reread[due["id"]] == (True, visit_id)
    assert reread[less_overdue.id] == (False, None)
    assert reread[later_today.id] == (False, None)


def test_the_card_carries_the_open_visit_and_then_the_last_one(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    card_url = f"{OUTLETS}/{outlet['id']}"

    before = client.get(card_url, headers=sales_agent_auth_headers).get_json()["data"]["outlet"]
    assert before["open_visit_id"] is None and before["last_visit"] is None
    assert before["rate_per_day"] is None and before["suggested_now"] is None
    # Activation itself published the due date (spec :91 — never visited means due NOW), so the
    # card says "due today", not "no date". `None` here would mean the outlet had fallen off the
    # work list between being approved and being visited.
    assert before["overdue_days"] == 0

    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    during = client.get(card_url, headers=sales_agent_auth_headers).get_json()["data"]["outlet"]
    # Start vs Resume is the BACKEND's call, published as a field: the bot never guesses it from
    # its own conversation state, which a restart or a menu tap has already thrown away.
    assert during["open_visit_id"] == visit_id and during["last_visit"] is None

    client.post(f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers)
    closed = client.post(
        f"{VISITS}/{visit_id}/close",
        json={"outcome": "closed", "notes": "Owner restocked last week"},
        headers=sales_agent_auth_headers,
    )
    assert closed.status_code == 200, closed.get_data(as_text=True)

    after = client.get(card_url, headers=sales_agent_auth_headers).get_json()["data"]["outlet"]
    assert after["open_visit_id"] is None
    assert after["last_visit"] == {
        "id": visit_id,
        "outcome": "closed",
        "notes": "Owner restocked last week",
        "ended_at": after["last_visit"]["ended_at"],
    }
    assert after["last_visit"]["ended_at"] is not None

    # Ruling 18: the card publishes the SAME number the due-list button prints, so the bot's
    # "3 days overdue" line and its "+3d" label can never be computed two different ways.
    Outlet.query.get(outlet["id"]).next_visit_due_at = datetime.now(UTC) - timedelta(days=4)
    db.session.commit()

    overdue = client.get(card_url, headers=sales_agent_auth_headers).get_json()["data"]["outlet"]
    assert overdue["overdue_days"] == 4


def test_stock_check_products_lists_only_the_flagged_active_ones(
    client, sales_agent_auth_headers, flagged_product, sample_category, db
):
    unflagged = Product(
        name="Sparkling 0.5L",
        category_id=sample_category.id,
        size="0.5L",
        base_price=Decimal("6000.00"),
        stock_quantity=50,
        is_active=True,
    )
    retired = Product(
        name="Retired 19L",
        category_id=sample_category.id,
        size="19L",
        base_price=Decimal("15000.00"),
        stock_quantity=0,
        is_active=False,
        in_sales_stock_check=True,
    )
    db.session.add_all([unflagged, retired])
    db.session.commit()

    listed = client.get(STOCK_CHECK_PRODUCTS, headers=sales_agent_auth_headers)

    assert listed.status_code == 200, listed.get_data(as_text=True)
    items = listed.get_json()["data"]["items"]
    assert [item["id"] for item in items] == [flagged_product.id]
    assert set(items[0]) == {"id", "name", "is_returnable_bottle", "min_order_quantity"}
    assert items[0]["name"] == "Pure Water 19L"
    assert items[0]["min_order_quantity"] == 1


def test_an_order_posted_before_the_shelf_count_is_refused_by_the_step_guard(
    client, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """M02: `order` is the step that spends money, and it was the one step with no guard.

    `checkin` (visit_service.py:195) and `stock_check` (:244) both call `_require_step` with the
    stated rationale that "a stale keyboard cannot skip check-in"; `place_order` called none, so
    anything holding a visit id could POST straight after `start` and buy the store a load of
    water with no check-in and no shelf count behind it. The refusal is the same coded 400 the
    other two steps raise, so the bot re-renders the step the server names.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    refused = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 4}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )

    assert refused.status_code == 400, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error_code"] == "SALES_VISIT_STEP_INVALID"
    assert body["details"]["current_step"] == "checkin"
    assert body["details"]["expected"] == ["order"]
    assert Order.query.filter_by(visit_id=visit_id).count() == 0

    # ...and the identical POST lands once the agent has actually walked the script.
    checked_in = client.post(
        f"{VISITS}/{visit_id}/checkin", json={"skipped": True}, headers=sales_agent_auth_headers
    )
    assert checked_in.status_code == 200, checked_in.get_data(as_text=True)
    counted = client.post(
        f"{VISITS}/{visit_id}/stock-check",
        json={"items": [{"product_id": flagged_product.id, "on_hand_qty": 3}]},
        headers=sales_agent_auth_headers,
    )
    assert counted.status_code == 200, counted.get_data(as_text=True)
    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 4}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 201, placed.get_data(as_text=True)


def test_the_line_ceiling_is_the_one_create_order_will_actually_enforce(
    client, app, sales_agent_auth_headers, operator_auth_headers, flagged_product, db
):
    """Ruling 71: two ceilings already existed, and the SMALLER one is the only honest answer.

    `SALES_STOCK_QTY_MAX` is 500, but `OrderService._process_order_items` refuses any line over
    `MAX_QUANTITY_PER_ITEM` (100) -- as a code-less 400 raised INSIDE `create_order`, after the
    agent has read a total out to the shopkeeper. That is the exact generic refusal M30 exists
    to remove, so a guard reading only 500 let a line of 150 sail through the quote AND the
    order door and die at the bottom anyway. NOTHING is patched here on purpose: this is the
    bound as shipped, and `max_quantity` names the number that will really be enforced.
    """
    assert (app.config["SALES_STOCK_QTY_MAX"], app.config["MAX_QUANTITY_PER_ITEM"]) == (500, 100)
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _at_order_step(client, sales_agent_auth_headers, outlet["id"], flagged_product)
    expected_details = {
        "product_id": flagged_product.id,
        "product_name": "Pure Water 19L",
        "quantity": 150,
        "max_quantity": 100,
    }

    quoted = client.post(
        f"{OUTLETS}/{outlet['id']}/order-estimate",
        json={"items": [{"product_id": flagged_product.id, "quantity": 150}]},
        headers=sales_agent_auth_headers,
    )
    assert quoted.status_code == 400, quoted.get_data(as_text=True)
    assert quoted.get_json()["error_code"] == "SALES_ORDER_QTY_INVALID"
    assert expected_details.items() <= quoted.get_json()["details"].items()

    placed = client.post(
        f"{VISITS}/{visit_id}/order",
        json={"items": [{"product_id": flagged_product.id, "quantity": 150}], "payment_method": "cash"},
        headers=sales_agent_auth_headers,
    )
    assert placed.status_code == 400, placed.get_data(as_text=True)
    assert placed.get_json()["error_code"] == "SALES_ORDER_QTY_INVALID"
    assert expected_details.items() <= placed.get_json()["details"].items()
    assert Order.query.filter_by(visit_id=visit_id).count() == 0
