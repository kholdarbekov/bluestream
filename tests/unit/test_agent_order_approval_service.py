"""AgentOrderApprovalService's own rules (compensation spec §4.18), below the HTTP layer.

The routes are driven in tests/integration/test_sales_agent_order_hold_api.py. What is pinned here
is what a route cannot show: the lock ORDER (T-HOLD-13; the race itself is the Postgres test), the
rescue of a decision that committed before the confirm path raised, and the one spelling of "a
hold is pending" that the batched readers and the beat sweeps' filter share. Orders are placed
through the real service loop (`_place_order`), never hand-built.
"""

import inspect
from datetime import date, timedelta

import pytest

from business_app.models.order import Order
from business_app.models.sales_visits import AgentOrderApproval, Visit
from business_app.services.delivery_service import DeliveryService
from business_app.services.order_service import OrderService
from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService
from business_app.services.sales.agent_order_confirmation_service import AgentOrderConfirmationService
from business_app.services.sales.visit_service import VisitService
from business_app.tasks import sales_agent_tasks
from business_app.utils.audit_logger import audit_logger
from business_app.utils.exceptions import ConflictError
from shared.enums import OrderStatus
from shared.staff_constants import SALES_EVENT_AGENT_ORDER_APPROVED
from tests.unit.test_agent_order_confirmation import _place_order, agent, linked_outlet  # noqa: F401  (fixtures)
from tests.unit.test_order_tasks_auto_confirm import _aged_cash_order


def _record_delays(monkeypatch, task):
    """`task.delay`, bound against the task's own `run`, patched through the instance `__dict__`."""
    signature = inspect.signature(task.run)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append((args, kwargs))

    monkeypatch.setitem(vars(task), "delay", fake)
    return calls


def _two_orders(agent_user, outlet, product):
    """Two same-day agent orders at a Telegram store: the first is the store's to answer, the
    second waits for a manager. The first visit is closed so the agent can start the second."""
    first, first_state = _place_order(agent_user, outlet, product)
    VisitService.close(
        Visit.query.get(first.visit_id),
        outcome=None,
        no_order_reason=None,
        notes=None,
        next_visit_at=None,
        dm_present=None,
    )
    second, second_state = _place_order(agent_user, outlet, product)
    assert (first_state, second_state) == ("pending_confirmation", "awaiting_staff_approval")
    return first, second


@pytest.fixture
def quiet(monkeypatch):
    """No real publish from the placements: the store's question, the managers' alert, pushes."""
    return {
        "questions": _record_delays(monkeypatch, sales_agent_tasks.push_agent_order_confirmation),
        "alerts": _record_delays(monkeypatch, sales_agent_tasks.notify_managers_agent_order_awaiting_approval),
        "pushes": _record_delays(monkeypatch, sales_agent_tasks.push_sales_event),
    }


def test_the_batched_readers_and_the_sweep_filter_share_one_pending_rule(
    db, agent, linked_outlet, sample_product, sample_user, admin_user, quiet
):
    first, held = _two_orders(agent, linked_outlet, sample_product)
    plain = _aged_cash_order(db, sample_user, delivery_date=date.today() + timedelta(days=3))

    owned = {
        order.id
        for order in Order.query.filter(
            Order.id.in_([first.id, held.id, plain.id]), AgentOrderConfirmationService.pending_request_filter()
        )
    }
    assert owned == {plain.id}  # the store's question owns `first`, the staff hold owns `held`
    assert AgentOrderApprovalService.awaiting_ids([first.id, held.id, plain.id]) == {held.id}
    assert AgentOrderApprovalService.awaiting_ids([]) == set()
    assert AgentOrderApprovalService.is_awaiting(held.id) is True
    assert AgentOrderApprovalService.approved_ids([held.id]) == set()

    AgentOrderApprovalService.approve(held.id, actor_id=admin_user.id)

    assert AgentOrderApprovalService.is_awaiting(held.id) is False
    assert AgentOrderApprovalService.awaiting_ids([held.id]) == set()
    assert AgentOrderApprovalService.approved_ids([first.id, held.id]) == {held.id}
    rows = AgentOrderApprovalService.approved_rows([first.id, held.id])
    assert list(rows) == [held.id]
    assert (rows[held.id].status, rows[held.id].decided_by_user_id) == ("approved", admin_user.id)


def test_t_hold_13_the_decision_locks_the_order_row_before_the_hold_row(
    db, agent, linked_outlet, sample_product, quiet, count_queries
):
    """The order every cancel path takes (it holds the order before `close_on_cancel` touches the
    hold), so a decision racing a cancel waits instead of deadlocking (R2-3). SQLite renders no
    FOR UPDATE, so the ORDER of the two SELECTs is what this pins; the race is §10.5's."""
    _first, held = _two_orders(agent, linked_outlet, sample_product)

    with count_queries() as counter:
        AgentOrderApprovalService._lock(held.id)

    selects = [statement for statement in counter.statements if statement.lstrip().upper().startswith("SELECT")]
    order_at = next(index for index, statement in enumerate(selects) if "FROM orders" in statement)
    hold_at = next(index for index, statement in enumerate(selects) if "FROM agent_order_approvals" in statement)
    assert order_at < hold_at


def test_t_hold_13_a_decision_after_a_store_cancel_answers_not_pending(
    db, agent, linked_outlet, sample_product, admin_user, quiet
):
    """The cancel won: `_assert_pending` reads the locked, now-CANCELLED order and answers the one
    code the queue page handles, never ORDER_STATUS_CONFLICT."""
    _first, held = _two_orders(agent, linked_outlet, sample_product)
    OrderService().cancel_order_as_customer(held.id, linked_outlet.user_id, reason="Changed our mind")

    with pytest.raises(ConflictError) as refused:
        AgentOrderApprovalService.approve(held.id, actor_id=admin_user.id)

    assert refused.value.error_code == "SALES_ORDER_APPROVAL_NOT_PENDING"
    assert refused.value.details == {"order_id": held.id, "status": "cancelled", "order_status": "cancelled"}
    assert quiet["pushes"] == []


def test_t_hold_13_a_raise_after_the_delivery_commit_still_pushes_and_audits_once(
    db, agent, linked_outlet, sample_product, admin_user, quiet, monkeypatch
):
    """`create_delivery` commits inside the confirm path (delivery_service.py:105), so a raise
    after it leaves the decision committed. A committed decision must never go silent: the order
    is CONFIRMED, the row is `approved`, and the push and the audit row go out exactly once."""
    _first, held = _two_orders(agent, linked_outlet, sample_product)
    audit_signature = inspect.signature(audit_logger.log_event)
    audits = []

    def record_audit(*args, **kwargs):
        audit_signature.bind(*args, **kwargs)
        audits.append(kwargs)
        return "audit-id"

    monkeypatch.setattr(audit_logger, "log_event", record_audit)
    real_create = DeliveryService.create_delivery

    def create_then_fail(self, *args, **kwargs):
        real_create(self, *args, **kwargs)
        raise RuntimeError("broadcast failed after the delivery committed")

    monkeypatch.setattr(DeliveryService, "create_delivery", create_then_fail)

    with pytest.raises(RuntimeError, match="broadcast failed"):
        AgentOrderApprovalService.approve(held.id, actor_id=admin_user.id)

    db.session.rollback()
    order = db.session.get(Order, held.id)
    row = AgentOrderApproval.query.filter_by(order_id=held.id).one()
    assert order.status is OrderStatus.CONFIRMED
    assert (row.status, row.decided_by_user_id) == ("approved", admin_user.id)
    assert [(args[1], kwargs) for args, kwargs in quiet["pushes"]] == [
        (SALES_EVENT_AGENT_ORDER_APPROVED, {"event_id": f"order-approval:{row.id}:approved"})
    ]
    assert [call["action"] for call in audits if call.get("action", "").startswith("sales_agent_order_")] == [
        "sales_agent_order_approved"
    ]
