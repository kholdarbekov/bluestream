"""POST /api/v1/orders/<id>/agent-confirmation — the store answering through its own bot.

Drives the exact request `telegram_bot`'s Confirm/Decline callback sends (Task 8):
the customer's JWT, `{"action": ...}` as the body, nothing else.
"""

import inspect
import logging
from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.order import Order
from business_app.models.sales_visits import OrderConfirmationRequest
from business_app.models.translation import Translation
from business_app.models.user import UserAddress
from business_app.services.notification_service import NotificationService
from business_app.services.order_service import OrderService
from business_app.services.sales.agent_order_confirmation_service import AgentOrderConfirmationService
from business_app.services.sales.outlet_service import OutletService
from business_app.tasks import sales_agent_tasks
from business_app.tasks.payment_tasks import process_payment_confirmation
from business_app.utils import error_handlers as error_handlers_module
from business_app.utils.timezone_utils import get_utc_now
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from shared.enums import EntitySubtype, OrderStatus, PaymentStatus
from shared.staff_constants import SALES_EVENT_AGENT_ORDER_DECLINED
from tests.integration.customer_action_matrix import ROW, seed
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
    return OutletService.create(agent.id, dict(GROCERY), link_user_id=customer.id)


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
    assert row.response_channel == "telegram"
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CANCELLED


@pytest.fixture
def order_words(db):
    """The published order words and the refresh sentence, as the seeder ships them."""
    for key, values in BACKEND_TRANSLATIONS.items():
        if key.startswith(("api.orders.", "api.common.")):
            for language, value in values.items():
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


def _published_actions(client, order_id, headers):
    response = client.get(f"/api/v1/orders/{order_id}?lang=en", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["order"]["customer_actions"]


def test_a_store_cancel_of_an_awaiting_agent_order_is_a_decline_and_tells_the_agent(
    client, app, db, linked_outlet, agent_order, order_words, monkeypatch
):
    """D13: the store's own Cancel of an order its agent placed, still awaiting its answer, is the bot's Decline:
    one record, and the agent is told (spec 2026-10-27 §5.2)."""
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    headers = _customer_headers(app, linked_outlet.user_id)

    actions = _published_actions(client, agent_order.id, headers)
    cancel, confirm = actions["cancel"], actions["confirm"]

    assert cancel == {
        "allowed": True,
        "refusal": None,
        "via": "decline",
        "label": "Decline order",
        "confirm_text": "Decline this order? We'll let your sales agent know.",
        "subscription_id": None,
        "reasons": [],
    }
    assert confirm == {"allowed": True, "label": "Confirm order"}
    resp = client.post(
        f"/api/v1/orders/{agent_order.id}/cancel?lang=en", headers=headers, json={"expected_via": "decline"}
    )
    assert (resp.status_code, resp.get_json()["message"]) == (200, "Declined. We've let your sales agent know.")
    assert (resp.get_json()["data"]["via"], resp.get_json()["data"]["order"]["status"]) == ("decline", "cancelled")
    row = OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one()
    assert (row.status, row.response_channel) == ("declined", "customer_cancel")
    assert [args[1] for args, _kwargs in pushes] == [SALES_EVENT_AGENT_ORDER_DECLINED]
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CANCELLED


def test_an_expired_question_turns_decline_back_into_cancel(
    client, app, db, linked_outlet, agent_order, order_words
):
    """Once the question expires, the order is confirmed and is the store's to cancel like any other; a screen
    still showing Decline is out of date."""
    AgentOrderConfirmationService.expire_due(now=get_utc_now() + timedelta(days=30))
    headers = _customer_headers(app, linked_outlet.user_id)

    actions = _published_actions(client, agent_order.id, headers)

    assert (actions["cancel"]["via"], actions["confirm"]) == ("cancel", {"allowed": False, "label": None})
    stale = client.post(
        f"/api/v1/orders/{agent_order.id}/cancel?lang=en", headers=headers, json={"expected_via": "decline"}
    )
    assert (stale.status_code, stale.get_json()["data"]) == (409, {"reason_code": "STATE_CHANGED"})
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CONFIRMED


def test_a_store_cannot_decline_through_the_proposal_an_agent_order_it_has_paid_for(
    client, app, db, sample_user, order_words, monkeypatch
):
    """The proposal's Decline removes the order, so the one Cancel rule decides it (spec 2026-10-27 §5.1), on both
    routes that decline. The store paid the Click link of its agent's order, and the payment confirmed the order
    while the store's question was still pending (the matrix's `agent_pending_click`, an order an admin moved to
    Click). The order's payload refuses its removal as PAID, and the Decline the proposal still carries answers that
    same refusal and writes nothing: the paid order stays, the question stays open, and the agent hears nothing.
    Confirm is still the store's to send."""
    order = seed(db, sample_user, ROW["agent_pending_click"], now=get_utc_now())
    payment = order.payment
    # The gateway's word (external I/O): the store paid the link.
    payment.status = PaymentStatus.COMPLETED
    payment.paid_at = get_utc_now()
    db.session.commit()
    _spy(monkeypatch, NotificationService, "send_payment_notification")
    assert process_payment_confirmation.run(payment.id) == {"success": True, "payment_id": payment.id}
    db.session.refresh(order)
    assert order.status is OrderStatus.CONFIRMED
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    headers = _customer_headers(app, sample_user.id)

    actions = _published_actions(client, order.id, headers)

    cancel = actions["cancel"]
    assert (cancel["allowed"], cancel["via"], cancel["refusal"]) == (False, None, "PAID")
    assert actions["confirm"] == {"allowed": True, "label": "Confirm order"}
    declined = client.post(
        f"/api/v1/orders/{order.id}/agent-confirmation",
        json={"action": "decline", "reason": "Bugun pul yo'q"},
        headers=headers,
    )
    assert declined.status_code == 400, declined.get_data(as_text=True)
    body = declined.get_json()
    assert (body["error_code"], body["details"]["reason_code"]) == ("ORDER_NOT_CUSTOMER_CANCELLABLE", "PAID")
    db.session.expire_all()
    assert db.session.get(Order, order.id).status is OrderStatus.CONFIRMED
    row = OrderConfirmationRequest.query.filter_by(order_id=order.id).one()
    assert (row.status, row.responded_at, row.decline_reason) == ("pending", None, None)
    assert pushes == []

    confirmed = client.post(
        f"/api/v1/orders/{order.id}/agent-confirmation", json={"action": "confirm"}, headers=headers
    )
    assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
    assert OrderConfirmationRequest.query.filter_by(order_id=order.id).one().status == "confirmed"


def test_an_agent_order_staff_cancelled_asks_the_store_nothing_more(
    client, app, db, linked_outlet, agent_order, order_words, admin_claim_headers, monkeypatch
):
    """Staff cancel an agent order while the store's question is still pending (its row stays "pending" until the
    expiry sweep). The question closed with the order: the payload publishes no Confirm, and the proposal's
    Confirm and Decline are both answered "not awaiting your confirmation", so a cancelled order is never stamped
    confirmed and the agent is never told so."""
    cancelled = client.put(
        f"/api/v1/admin/orders/{agent_order.id}/status",
        json={"status": "cancelled", "reason": "Store phoned to cancel"},
        headers=admin_claim_headers,
    )
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)
    pushes = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    headers = _customer_headers(app, linked_outlet.user_id)

    actions = _published_actions(client, agent_order.id, headers)

    assert actions["confirm"] == {"allowed": False, "label": None}
    assert (actions["cancel"]["allowed"], actions["cancel"]["refusal"]) == (False, "NOT_CANCELLABLE")
    for action in ("confirm", "decline"):
        answer = client.post(
            f"/api/v1/orders/{agent_order.id}/agent-confirmation", json={"action": action}, headers=headers
        )
        assert answer.status_code == 409, answer.get_data(as_text=True)
        assert answer.get_json()["error_code"] == "SALES_CONFIRMATION_NOT_PENDING"
    row = OrderConfirmationRequest.query.filter_by(order_id=agent_order.id).one()
    assert (row.status, row.responded_at) == ("pending", None)
    db.session.refresh(agent_order)
    assert agent_order.status is OrderStatus.CANCELLED
    assert pushes == []


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
    client, app, db, linked_outlet, agent_order, caplog, order_words
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
    assert "This order is no longer waiting for your answer." in lines[0].getMessage()
    assert again.get_json()["message"] == "This order is no longer waiting for your answer."


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
