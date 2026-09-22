"""Order on behalf: VisitService.place_order through OrderService.create_order.

There is no third order-creation path (D12). The visit posts the ADMIN-SHAPED
payload into `OrderService.create_order`, so the pricing, the COD cap, the
inventory reservation, the payment row and the instant-COD confirmation an
agent order gets are byte-for-byte the ones every other channel gets.
"""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.models.user import User
from business_app.serializers.sales_serializers import (
    OrderEstimatePayload,
    PlaceOrderPayload,
    serialize_order_brief,
)
from business_app.services.sales import agent_order_confirmation_service as confirmation_module
from business_app.services.sales import visit_service as visit_service_module
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.visit_service import VisitService
from business_app.services.staff_service import StaffService
from business_app.utils.constants import ORDER_SOURCE_PREFIXES
from business_app.utils.delivery_window import local_now
from business_app.utils.exceptions import ConflictError, ValidationError
from shared.enums import OrderStatus
from tests.integration.test_outlet_create import GROCERY

pytestmark = pytest.mark.integration


@pytest.fixture
def agent(sales_agent_user):
    """The conftest agent: +998901234577, role SALES_AGENT, districts ['chilanzar']."""
    return sales_agent_user


@pytest.fixture
def stock_product(db, sample_product):
    """"Pure Water 19L", base_price 15000, stock 100 — flagged onto the agent's stock-check list."""
    sample_product.in_sales_stock_check = True
    db.session.commit()
    return sample_product


@pytest.fixture
def approved_outlet(db, agent, admin_user):
    """create -> request_activation -> approve, exactly as tests/integration/test_outlet_approval.py.

    Approval mints the customer User, the UserAddress and (grocery) the AMOUNT contract, which is
    what makes the outlet orderable at all.
    """
    outlet = OutletService.create(agent.id, dict(GROCERY))
    OutletService.request_activation(outlet, agent.id)
    return OutletService.approve(outlet.id, actor_id=admin_user.id)


def _visit_at_order_step(agent, outlet, product, on_hand=2):
    """The three taps that precede the order screen in the staff bot."""
    visit = VisitService.start(agent.id, outlet.id)
    VisitService.checkin(visit, latitude=None, longitude=None, horizontal_accuracy=None, skipped=True)
    VisitService.stock_check(
        visit,
        [
            {
                "product_id": product.id,
                "on_hand_qty": on_hand,
                "empties_qty": 3,
                "is_sold_out": False,
                "is_low": True,
            }
        ],
    )
    return visit


def _order_body(product, quantity=8, payment_method="cash", **extra):
    """The exact JSON body the staff bot POSTs to /staff/sales/visits/<id>/order."""
    body = {
        "items": [{"product_id": product.id, "quantity": quantity}],
        "payment_method": payment_method,
        "delivery_date": None,
        "delivery_window_start": None,
        "delivery_window_end": None,
        "delivery_notes": "Leave at the counter",
    }
    body.update(extra)
    return body


def _place(visit, body, agent_id):
    """Through the real request model, so the service is fed what the route will feed it."""
    return VisitService.place_order(visit, PlaceOrderPayload(**body).model_dump(), agent_id)


def test_place_order_creates_a_sales_agent_order_and_confirms_an_unlinked_store(
    db, agent, approved_outlet, stock_product
):
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    order, state = _place(visit, _order_body(stock_product), agent.id)

    db.session.refresh(order)
    assert order.order_source == "sales_agent"
    assert order.created_by_staff_id == agent.id
    assert order.visit_id == visit.id
    assert order.user_id == approved_outlet.user_id
    assert order.delivery_address_id == approved_outlet.address_id
    assert order.delivery_notes == "Leave at the counter"
    assert [(i.product_id, i.quantity) for i in order.order_items] == [(stock_product.id, 8)]
    # 8 x 15000, delivery free (DEFAULT_DELIVERY_FEE=0), no contract product price on the
    # AMOUNT contract approval created, entity users are loyalty-ineligible so no tier discount.
    assert Decimal(str(order.total_amount)) == Decimal("120000")
    # The number itself is minted by the Postgres order_sequences UPSERT and falls back to WB…
    # under SQLite; Task 1's Postgres test owns the "SA_…" assertion. What THIS test can prove is
    # that the order carries the source that maps to that prefix.
    assert ORDER_SOURCE_PREFIXES[order.order_source] == "SA"

    # Nobody can be asked: the store has no telegram_id, so the order is confirmed on the spot and
    # the driver pool sees it at once (the phone-order behaviour).
    assert state == "confirmed"
    assert order.status == OrderStatus.CONFIRMED
    assert Delivery.query.filter_by(order_id=order.id).count() == 1
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).count() == 0

    # The visit is done with ordering and records what the store actually took.
    assert visit.current_step == "close"
    assert [(row.product_id, row.accepted_qty) for row in visit.stock_checks] == [(stock_product.id, 8)]


def test_a_second_order_on_the_same_visit_is_refused(db, agent, approved_outlet, stock_product):
    """visit_id is the idempotency key: a double tap must not buy the store two loads of water."""
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)
    first, _state = _place(visit, _order_body(stock_product), agent.id)

    with pytest.raises(ConflictError) as excinfo:
        _place(visit, _order_body(stock_product, quantity=2), agent.id)

    assert excinfo.value.error_code == "SALES_VISIT_ORDER_EXISTS"
    assert excinfo.value.details == {
        "visit_id": visit.id,
        "order_id": first.id,
        # Ruling 25: the bot's double-tap copy names the order, so the 409 has to carry the
        # number — and it carries the SAME block `serialize_visit` publishes, not a copy.
        "order": serialize_order_brief(first),
    }
    assert excinfo.value.details["order"]["order_number"] == first.order_number
    assert Order.query.filter_by(visit_id=visit.id).count() == 1


def test_a_visit_the_sweep_closed_under_the_agent_refuses_the_order(db, agent, approved_outlet, stock_product):
    """#15: `abandon_stale` can close the visit between the bot's order screen and its Confirm tap.

    The route's ownership/openness check ran when the screen was drawn; only the re-read under the
    FOR UPDATE lock can see a visit that ended since. Without it the agent buys the store water on
    a visit nobody will ever close, and `close` is no longer reachable to recompute the outlet.
    """
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)
    VisitService.abandon(visit, agent.id)

    with pytest.raises(ConflictError) as excinfo:
        _place(visit, _order_body(stock_product), agent.id)

    assert excinfo.value.error_code == "SALES_VISIT_NOT_OPEN"
    assert Order.query.filter_by(visit_id=visit.id).count() == 0


def test_an_outlet_with_no_customer_account_cannot_take_an_order(db, agent, stock_product):
    prospect = OutletService.create(agent.id, dict(GROCERY))
    visit = _visit_at_order_step(agent, prospect, stock_product)

    with pytest.raises(ValidationError) as excinfo:
        _place(visit, _order_body(stock_product), agent.id)
    assert excinfo.value.error_code == "SALES_OUTLET_NOT_ACTIVE"

    with pytest.raises(ValidationError) as excinfo:
        VisitService.payment_methods(prospect)
    assert excinfo.value.error_code == "SALES_OUTLET_NOT_ACTIVE"

    with pytest.raises(ValidationError) as excinfo:
        VisitService.order_estimate(prospect, [{"product_id": stock_product.id, "quantity": 8}])
    assert excinfo.value.error_code == "SALES_OUTLET_NOT_ACTIVE"

    assert Order.query.filter_by(visit_id=visit.id).count() == 0


def test_an_electronic_rail_is_refused_before_any_order_is_written(db, agent, approved_outlet, stock_product):
    """An agent collects cash or charges the corporate account. They never take a card."""
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    with pytest.raises(ValidationError) as excinfo:
        _place(visit, _order_body(stock_product, payment_method="click"), agent.id)

    assert excinfo.value.error_code == "SALES_PAYMENT_METHOD_INVALID"
    assert Order.query.filter_by(visit_id=visit.id).count() == 0
    assert visit.current_step == "order"


def test_payment_methods_offers_only_the_two_rails_an_agent_may_take(db, approved_outlet):
    methods = VisitService.payment_methods(approved_outlet)

    # A grocery store has no prepaid business account (get_business_account_balances is
    # workplace-only), so cash is the whole menu here.
    assert [m["method"] for m in methods] == ["cash"]
    assert methods[0]["name"] == "Cash on Delivery"

    # The operator menu the filter runs over really does carry the electronic rails — proving the
    # filter is doing work rather than agreeing with an already-short list.
    raw = StaffService.get_client_payment_methods(
        approved_outlet.user_id, delivery_address_id=approved_outlet.address_id
    )
    assert {"click", "payme"} <= {m["method"] for m in raw["available_methods"]}


def test_order_estimate_quotes_the_basket_through_the_operator_estimator(
    db, approved_outlet, stock_product, monkeypatch
):
    """The money the agent reads out is the operator quote, called with the outlet's own customer."""
    calls = []
    real_estimate = StaffService.estimate_phone_order

    def spy(client_id, order_data, *, payment_method=None):
        calls.append((client_id, order_data, payment_method))
        return real_estimate(client_id, order_data, payment_method=payment_method)

    monkeypatch.setattr(StaffService, "estimate_phone_order", staticmethod(spy))

    payload = OrderEstimatePayload(**{"items": [{"product_id": stock_product.id, "quantity": 8}]})
    estimate = VisitService.order_estimate(approved_outlet, payload.model_dump()["items"])

    # No rail named: the quote keeps the rail-less pricing every operator phone-order caller gets.
    assert calls == [(approved_outlet.user_id, {"items": [{"product_id": stock_product.id, "quantity": 8}]}, None)]

    # ...and a rail the agent chose travels all the way down to the estimator (C27).
    with_rail = OrderEstimatePayload(
        **{"items": [{"product_id": stock_product.id, "quantity": 8}], "payment_method": "cash"}
    )
    VisitService.order_estimate(
        approved_outlet, with_rail.model_dump()["items"], with_rail.model_dump()["payment_method"]
    )
    assert calls[-1][2] == "cash"
    assert estimate["client_id"] == approved_outlet.user_id
    assert [(i["product_id"], i["quantity"]) for i in estimate["items"]] == [(stock_product.id, 8)]
    assert isinstance(estimate["total_amount"], float)
    assert estimate["total_amount"] > 0


def test_a_telegram_linked_store_is_asked_to_confirm_before_the_order_is_real(
    app, db, agent, approved_outlet, stock_product, monkeypatch
):
    customer = User.query.get(approved_outlet.user_id)
    customer.telegram_id = 771002003
    db.session.commit()

    pushed = []

    def spy(order_id):
        # D18: the dispatch point must be reached with a CLEAN session — the request row is
        # durable before anything is enqueued, so a retry can never outrun its own row.
        pushed.append((order_id, bool(_db.session.new or _db.session.dirty)))

    monkeypatch.setattr(confirmation_module, "_enqueue_push", spy)

    visit = _visit_at_order_step(agent, approved_outlet, stock_product)
    order, state = _place(visit, _order_body(stock_product), agent.id)

    assert state == "pending_confirmation"
    assert order.status == OrderStatus.PENDING
    assert Delivery.query.filter_by(order_id=order.id).count() == 0

    request = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    assert request.status == "pending"
    assert request.outlet_id == approved_outlet.id
    assert len(request.request_id) == 16
    assert request.expires_at - request.requested_at == timedelta(hours=app.config["SALES_CONFIRMATION_TTL_HOURS"])
    assert pushed == [(order.id, False)]


def test_a_returning_store_whose_order_self_confirms_is_reported_as_auto_confirmed(
    db, agent, approved_outlet, stock_product
):
    """create_order's instant-COD block confirms a returning store's cash order at creation.

    The confirmation service must NOT then open a request nobody would ever answer.
    """
    prior = Order(
        user_id=approved_outlet.user_id,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("90000"),
        total_amount=Decimal("90000"),
        delivery_address_id=approved_outlet.address_id,
        order_source="web",
    )
    db.session.add(prior)
    db.session.commit()

    visit = _visit_at_order_step(agent, approved_outlet, stock_product)
    order, state = _place(visit, _order_body(stock_product), agent.id)

    assert state == "auto_confirmed"
    assert order.status == OrderStatus.CONFIRMED
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).count() == 0


def test_a_dated_order_is_confirmed_but_its_delivery_is_withheld_until_the_morning(
    db, agent, approved_outlet, stock_product
):
    """A "Tomorrow" order is a SCHEDULED order: OrderScheduleService owns when a Delivery exists."""
    tomorrow = local_now().date() + timedelta(days=1)
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    order, state = _place(visit, _order_body(stock_product, delivery_date=tomorrow.isoformat()), agent.id)

    assert state == "confirmed"
    assert order.status == OrderStatus.CONFIRMED
    assert order.delivery_date == tomorrow
    assert Delivery.query.filter_by(order_id=order.id).count() == 0


def test_the_outlets_own_delivery_window_is_used_when_the_agent_names_none(
    db, agent, approved_outlet, stock_product, monkeypatch
):
    """The store's standing window is a default the agent never has to retype.

    The clock is frozen at 10:00 because the window is only a default while it is still
    REACHABLE (M03): an undated order is worked today, so after 12:00 a 09:00-12:00 store gets
    "anytime" instead -- which is the sibling test below.
    """
    _outlet_with_morning_window(approved_outlet, agent)
    morning = local_now().replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(visit_service_module, "local_now", lambda: morning)
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    order, _state = _place(visit, _order_body(stock_product), agent.id)

    assert order.delivery_date is None
    assert (order.delivery_window_start, order.delivery_window_end) == (time(9, 0), time(12, 0))


def test_an_undated_order_does_not_inherit_a_window_that_has_already_passed(
    db, agent, approved_outlet, stock_product, monkeypatch
):
    """M03: ruling 34 applies the store's window only while that window is still reachable.

    Feasibility is the validator's answer FOR A DATE, and an order with no date is worked today:
    `OrderScheduleService.is_awaiting_release` returns False the moment `delivery_date is None`
    (order_schedule_service.py:117), so its Delivery is created at once. Asking the validator
    with `None` skipped the "delivery window has already passed for today" rule altogether, so a
    09:00-12:00 store ordering at 14:00 with no date promised the driver a window that had ended
    two hours earlier.
    """
    _outlet_with_morning_window(approved_outlet, agent)
    afternoon = local_now().replace(hour=14, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(visit_service_module, "local_now", lambda: afternoon)
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    order, _state = _place(visit, _order_body(stock_product), agent.id)

    assert order.delivery_date is None
    assert (order.delivery_window_start, order.delivery_window_end) == (None, None)


def _outlet_with_morning_window(outlet, agent):
    """A store whose standing window is 09:00-12:00 — reachable in the morning, gone by 14:00."""
    OutletService.update(outlet, {"delivery_window_start": "09:00", "delivery_window_end": "12:00"}, agent.id)
    return outlet


def test_a_stale_outlet_window_becomes_anytime_rather_than_refusing_todays_order(
    db, agent, approved_outlet, stock_product, monkeypatch
):
    """The standing window is a default the agent never typed; it may never cost them the order.

    A 09:00-12:00 store tapping "Today" at 14:00 used to hit the validator's "delivery window has
    already passed for today", so the button was dead at that store every afternoon.
    """
    _outlet_with_morning_window(approved_outlet, agent)
    afternoon = local_now().replace(hour=14, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(visit_service_module, "local_now", lambda: afternoon)
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    order, _state = _place(visit, _order_body(stock_product, delivery_date=afternoon.date().isoformat()), agent.id)

    assert order.delivery_date == afternoon.date()
    assert (order.delivery_window_start, order.delivery_window_end) == (None, None)


def test_the_outlet_window_still_applies_to_a_today_order_placed_while_it_is_reachable(
    db, agent, approved_outlet, stock_product, monkeypatch
):
    """Same store, same button, 10:00 — the window is still reachable, so it is used."""
    _outlet_with_morning_window(approved_outlet, agent)
    morning = local_now().replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(visit_service_module, "local_now", lambda: morning)
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    order, _state = _place(visit, _order_body(stock_product, delivery_date=morning.date().isoformat()), agent.id)

    assert (order.delivery_window_start, order.delivery_window_end) == (time(9, 0), time(12, 0))


def test_a_schedule_the_web_checkout_would_refuse_is_refused_here_too(
    db, agent, approved_outlet, stock_product
):
    """One schedule validator for every write path — a sales order gets no laxer rule."""
    yesterday = local_now().date() - timedelta(days=1)
    assert isinstance(yesterday, date)
    visit = _visit_at_order_step(agent, approved_outlet, stock_product)

    with pytest.raises(ValidationError) as excinfo:
        _place(visit, _order_body(stock_product, delivery_date=yesterday.isoformat()), agent.id)

    # L47(4): was `is None`, which WAS the defect -- a code-less 400 reaches the bot as the
    # generic "Something went wrong" for an agent who only mistyped a date.
    assert excinfo.value.error_code == "SALES_DELIVERY_DATE_INVALID"
    assert "delivery_date cannot be in the past" in excinfo.value.validation_errors
    assert Order.query.filter_by(visit_id=visit.id).count() == 0
