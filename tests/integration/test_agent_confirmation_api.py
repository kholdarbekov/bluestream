"""POST /api/v1/orders/<id>/agent-confirmation — the store answering through its own bot.

Drives the exact request `telegram_bot`'s Confirm/Decline callback sends (Task 8):
the customer's JWT, `{"action": ...}` as the body, nothing else.
"""

import inspect
import logging

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService
from business_app.services.sales.outlet_service import OutletService
from business_app.tasks import sales_agent_tasks
from business_app.utils import error_handlers as error_handlers_module
from shared.enums import EntitySubtype, OrderStatus
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_agent_order_confirmation import _place_order, _spy
from tests.unit.test_outlet_dedupe import PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    user.telegram_id = "777000111"
    db.session.commit()
    return user


@pytest.fixture
def linked_outlet(db, agent):
    customer = _customer(db, "+998901112299", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    customer.telegram_id = "555000111"
    db.session.add(
        UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    )
    db.session.commit()
    return OutletService.create(agent.id, {**GROCERY, "contact": None}, link_user_id=customer.id)


@pytest.fixture
def agent_order(app, db, agent, linked_outlet, sample_product, monkeypatch):
    _spy(monkeypatch, sales_agent_tasks.push_agent_order_confirmation, "delay")
    _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    order, state = _place_order(agent, linked_outlet, sample_product)
    assert state == "pending_confirmation"
    return order


def _customer_headers(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_store_confirms_its_agent_order(client, app, db, linked_outlet, agent_order):
    response = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "confirm"},
        headers=_customer_headers(app, linked_outlet.user_id),
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["confirmation"] == {"status": "confirmed"}
    assert data["order"]["id"] == agent_order.id
    assert data["order"]["status"] == OrderStatus.CONFIRMED.value
    assert OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one().status == "confirmed"
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CONFIRMED


def test_store_declines_its_agent_order_and_the_reason_reaches_the_row(
    client, app, db, linked_outlet, agent_order
):
    """The OTHER arm of the same handler, through HTTP.

    `reason` is the only field of the body the confirm path never exercises: a route
    that dropped `payload.reason` would cancel the order correctly, keep every other
    assertion green, and silently lose the store's own words — which are the agent's
    next action (ruling 26).
    """
    response = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "decline", "reason": "Bugun pul yo'q"},
        headers=_customer_headers(app, linked_outlet.user_id),
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["confirmation"] == {"status": "declined"}
    assert data["order"]["id"] == agent_order.id
    assert data["order"]["status"] == OrderStatus.CANCELLED.value
    row = OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one()
    assert (row.status, row.decline_reason) == ("declined", "Bugun pul yo'q")
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CANCELLED


def test_another_customer_cannot_answer_the_order(client, db, agent_order, auth_headers):
    response = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "confirm"},
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one().status == "pending"
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.PENDING


def test_answering_twice_is_a_409_with_its_code(client, app, db, linked_outlet, agent_order):
    """Ruling 6 through HTTP. `@handle_api_exception` maps ConflictError to 409 and copies
    `e.error_code` to the TOP LEVEL of the body (`business_app/utils/error_handlers.py:97`
    and `:321`) — which is the exact shape the customer bot branches on to render
    `telegram.agent_order.not_pending` (Task 8). Asserted at service level only, this arm
    would be dead in production while its unit test passed."""
    headers = _customer_headers(app, linked_outlet.user_id)
    assert client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation", json={"action": "confirm"}, headers=headers
    ).status_code == 200

    response = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "decline", "reason": "changed my mind"},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.get_json()["error_code"] == "SALES_CONFIRMATION_NOT_PENDING"
    assert OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one().status == "confirmed"
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CONFIRMED


def test_a_duplicate_answer_is_logged_as_a_warning_not_an_error(
    client, app, db, linked_outlet, agent_order, caplog
):
    """A second Confirm tap is a thumb, not an incident.

    `handle_api_exception` graded 400/401/403/404 as WARNING and everything else ERROR, so
    every duplicate tap — the single most likely thing a shopkeeper does to a message with
    two buttons on it — raised an ERROR line with a stack-trace-shaped payload next to the
    real 500s. A 409 is a refusal the client is expected to meet and re-render; it is
    graded like the other refusals.

    `business_app.utils.error_handlers`'s logger is a descendant of Flask's own
    ``app.logger`` ("business_app"), which carries its own handlers AND `propagate=False`
    (`business_app/utils/logging_config.py`), so a record raised here never reaches root
    and caplog's own root-attached handler never sees it — the same trap
    `test_jwt_exception_passthrough.py`'s module docstring names for this exact logger.
    Attaching caplog's handler directly to the module logger, exactly as
    `test_notification_service_fixes.py` already does for the Celery task logger, is what
    makes the records visible.
    """
    headers = _customer_headers(app, linked_outlet.user_id)
    assert client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation", json={"action": "confirm"}, headers=headers
    ).status_code == 200

    error_handlers_module.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="business_app.utils.error_handlers"):
            again = client.post(
                f"/api/v1/orders/{agent_order.id}/agent-confirmation",
                json={"action": "confirm"},
                headers=headers,
            )
    finally:
        error_handlers_module.logger.removeHandler(caplog.handler)

    assert again.status_code == 409, again.get_data(as_text=True)
    assert again.get_json()["error_code"] == "SALES_CONFIRMATION_NOT_PENDING"
    lines = [r for r in caplog.records if r.name == "business_app.utils.error_handlers"]
    assert [r.levelname for r in lines] == ["WARNING"], [(r.levelname, r.getMessage()) for r in lines]
    assert "This order is not awaiting your confirmation" in lines[0].getMessage()


def test_an_unknown_action_is_a_400_with_its_error_code(client, app, db, linked_outlet, agent_order):
    response = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "maybe"},
        headers=_customer_headers(app, linked_outlet.user_id),
    )

    assert response.status_code == 400
    assert response.get_json()["error_code"] == "SALES_CONFIRMATION_ACTION_INVALID"
    assert OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one().status == "pending"


def test_a_failing_order_action_leaves_the_store_able_to_answer_again(
    client, app, db, linked_outlet, agent_order, monkeypatch
):
    """L57: the request row is the QUESTION, and it may only be closed once the order has moved.

    It was committed as answered BEFORE `update_order_status`/`cancel_order` ran, so a raising
    order action left a durable "declined" beside a PENDING order: the store's next tap got 409
    SALES_CONFIRMATION_NOT_PENDING, the agent was never pushed (the notification is after the
    action too), and the expiry sweep skipped the row because it was no longer pending. Nobody
    could move that order again. Failing before the row is touched leaves the question open,
    which the store, the sweep and a retry can all still resolve.
    """
    real_cancel = OrderService.cancel_order

    def boom(*args, **kwargs):
        inspect.signature(real_cancel).bind(*args, **kwargs)
        raise RuntimeError("payment gateway down")

    monkeypatch.setattr(OrderService, "cancel_order", boom)
    headers = _customer_headers(app, linked_outlet.user_id)

    failed = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "decline", "reason": "Bugun pul yo'q"},
        headers=headers,
    )

    assert failed.status_code == 500, failed.get_data(as_text=True)
    row = OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one()
    assert (row.status, row.responded_at, row.decline_reason) == ("pending", None, None)
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.PENDING

    # ...and the same tap, once the order path is healthy again, still answers.
    monkeypatch.setattr(OrderService, "cancel_order", real_cancel)
    retried = client.post(
        f"/api/v1/orders/{agent_order.id}/agent-confirmation",
        json={"action": "decline", "reason": "Bugun pul yo'q"},
        headers=headers,
    )

    assert retried.status_code == 200, retried.get_data(as_text=True)
    assert retried.get_json()["data"]["confirmation"] == {"status": "declined"}
    row = OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one()
    assert (row.status, row.decline_reason) == ("declined", "Bugun pul yo'q")
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CANCELLED
