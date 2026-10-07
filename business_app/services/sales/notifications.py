"""After-commit dispatch of sales events. Call these ONLY after db.session.commit()."""

import logging
from typing import Optional

from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.sales import Outlet
from business_app.models.user import User
from business_app.tasks.sales_agent_tasks import (
    notify_managers_activation_requested,
    notify_managers_agent_order_awaiting_approval,
    push_sales_event,
)
from business_app.utils import local_windows
from shared.staff_constants import SALES_EVENT_PAY_PENALTY_CONFIRMED, SALES_EVENT_PAY_STATEMENT_APPROVED

logger = logging.getLogger(__name__)


def _agent_for(outlet: Outlet) -> Optional[User]:
    """The agent whose plan the outlet is on (`OutletService.due_owner_id`, the one spelling)."""
    # Imported here: outlet_service imports this module at load time.
    from business_app.services.sales.outlet_service import OutletService

    agent_id = OutletService.due_owner_id(outlet)
    return User.query.get(agent_id) if agent_id else None


def notify_agent_event(outlet: Outlet, event: str) -> None:
    agent = _agent_for(outlet)
    if agent is None or not agent.telegram_id:
        return
    push_sales_event.delay(
        int(agent.telegram_id),
        event,
        {"outlet_id": outlet.id, "outlet_name": outlet.name, "reason": outlet.rejected_reason},
    )


def notify_activation_requested(outlet: Outlet) -> None:
    notify_managers_activation_requested.delay(outlet.id)


def notify_order_awaiting_approval(order) -> None:
    """A held agent order (C14) waits in the queue: alert the admins and managers who may decide
    it. Enqueued, never sent inline: the task reads the hold row `create_order` committed.

    Best-effort (final-review M4): the order and its hold are already committed, so a broker that
    refuses the publish must not answer 500 over them (a retry would get 409 "already ordered").
    """
    try:
        notify_managers_agent_order_awaiting_approval.delay(order.id)
    except Exception:  # noqa: BLE001 - after commit; the queue and the nav badge still show it
        logger.exception("Approval alert for held order %s was not queued", order.id)


def notify_agent_order_event(
    order, outlet: Outlet, event: str, *, reason: Optional[str] = None, event_id: Optional[str] = None
) -> None:
    """The store answered an order this agent placed on its behalf, or a manager decided it (C14).

    `reason` is the store's own words on a decline — it is the agent's next
    action, so it rides in the payload rather than being looked up bot-side.

    The recipient is the agent who PLACED the order, not the outlet's assigned
    one: a covering agent (one onboarded the store, a manager later reassigned
    it) passes `get_for_agent` and can place the order, and routing by the
    outlet then told an agent who placed nothing that "the store confirmed" while
    the one standing at the counter heard nothing at all. `_agent_for` remains the
    fallback for an order with no creator on it.

    `event_id` is the decision's own id when it has one (`order-approval:<id>:approved`), which
    the staff bot dedups on. A store's answer has none; its `.delay` call then keeps its old
    shape and the task keys on its own id.
    """
    agent = User.query.get(order.created_by_staff_id) if order.created_by_staff_id else None
    if agent is None:
        agent = _agent_for(outlet)
    if agent is None or not agent.telegram_id:
        return
    push_sales_event.delay(
        int(agent.telegram_id),
        event,
        {
            "outlet_id": outlet.id,
            "outlet_name": outlet.name,
            "order_id": order.id,
            "order_number": order.order_number,
            "reason": reason,
        },
        **({"event_id": event_id} if event_id else {}),
    )


def pay_penalty_confirmed_payload(penalty) -> dict:
    """What the agent's phone shows for a confirmed penalty (§7.5).

    It carries the reason, never the evidence (§5.4). Money is a whole-UZS int. `month` is
    the month the penalty counts in, and `is_late` says whether that is later than the incident
    month (`SalesPayPeriodService.is_late`, the one "late" rule, S-10).
    """
    from business_app.services.sales.pay_penalty_service import penalty_type_names
    from business_app.services.sales.pay_period_service import SalesPayPeriodService

    period = penalty.period
    return {
        "penalty_id": penalty.id,
        "month": local_windows.format_month(period.month_start),
        "incident_date": penalty.incident_date.isoformat(),
        "type_names": penalty_type_names(penalty.penalty_type),
        "reason": penalty.reason,
        "amount": int(penalty.amount),
        "is_late": SalesPayPeriodService.is_late(local_windows.month_start(penalty.incident_date), period),
    }


def _push_failed(what: str, subject: str) -> None:
    """A pay push that could not be queued, after its write committed: logged, never raised.

    The write is final, so an error here would answer 500 for a decision that stands, and the
    admin's retry would meet a 409 (the Task 9 precedent: "the decision is committed; the push is
    best effort"). The session is rolled back so a failed read cannot poison the next agent's push
    or the route's answer; nothing of ours is pending in it.
    """
    db.session.rollback()
    logger.exception("The %s push for %s was not queued", what, subject)


def notify_pay_penalty_confirmed(penalty) -> None:
    """After commit: tell the penalised agent, and nobody else (C6, T-VISIB-4).

    The event id is the penalty's own, so a double-clicked confirm or a retried task is one
    message (§7.5). Best effort: a push that cannot be queued is logged, never raised.
    """
    penalty_id = penalty.id
    try:
        agent = penalty.agent
        if agent is None or not agent.telegram_id:
            return
        push_sales_event.delay(
            int(agent.telegram_id),
            SALES_EVENT_PAY_PENALTY_CONFIRMED,
            pay_penalty_confirmed_payload(penalty),
            event_id=f"penalty-confirmed:{penalty_id}",
        )
    except Exception:  # noqa: BLE001 -- the penalty is committed; the push is best effort
        _push_failed("penalty-confirmed", f"penalty {penalty_id}")


def _pushed_product(product: dict) -> dict:
    """A8's product as the approval push carries it (§7.5, §10.2): the same keys, `next_tier` null
    as a frozen statement publishes it (V5-B9), pay amounts as whole-UZS ints, the rate and the
    order money as numbers."""
    upcoming = product["next_tier"]
    return {
        "product_id": product["product_id"],
        "product_name": product["product_name"],
        "uses_default_tiers": product["uses_default_tiers"],
        "units": product["units"],
        "total": int(product["total"]),
        "tiers": [
            {
                "from_unit": tier["from_unit"],
                "to_unit": tier["to_unit"],
                "units": tier["units"],
                "mode": tier["mode"],
                "value": float(tier["value"]),
                "net": float(tier["net"]),
                "amount_full": int(tier["amount_full"]),
                "amount": int(tier["amount"]),
            }
            for tier in product["tiers"]
        ],
        "next_tier": None if upcoming is None else {**upcoming, "value": float(upcoming["value"])},
    }


def pay_statement_approved_payload(statement) -> dict:
    """What the agent's phone shows when their month is approved (§7.5).

    The figures are A8's own `summary` of this statement (`statement_detail`, read from the
    frozen columns and `inputs`), so the push, the drawer and the bot's summary screen cannot
    disagree. `commission` is A8's `summary.commission` gross and products (D-BANDS);
    `commission_after_gate` is the current-month group and `late` the gated late groups, the split
    the summary screen prints; `total` is the paid total (C1); `carry_in` is
    `carry_in_view`'s object, the one A8 and S1 publish (T-OWED-3 (e)). Money is a whole-UZS
    int: every stored pay amount is whole already, so `int` changes only the type.
    """
    from business_app.services.sales.pay_statement_service import SalesPayStatementService

    period = statement.period
    summary = SalesPayStatementService.statement_detail(period.month_start, statement.agent_user_id)["summary"]
    carry_in = summary["carry_in"]
    return {
        "statement_id": statement.id,
        "month": local_windows.format_month(period.month_start),
        "is_shadow": bool(period.is_shadow),
        "base": int(summary["base_amount"]),
        "commission": {
            "gross": int(summary["commission"]["gross"]),
            "products": [_pushed_product(product) for product in summary["commission"]["products"]],
        },
        "commission_after_gate": int(summary["commission"]["after_gate"]),
        "late": int(sum(group["after_gate"] for group in summary["late"])),
        "new_outlets": int(summary["new_outlets"]["amount"]),
        "adjustments": int(summary["adjustments"]),
        "penalties": int(summary["penalties"]),
        "carry_in": {
            "amount": int(carry_in["amount"]),
            "from_month": carry_in["from_month"],
            "source": carry_in["source"],
        },
        "total": int(summary["total"]),
        "carry_out": int(summary["carry_out"]),
        "owed": int(summary["owed"]),
    }


def notify_pay_statements_approved(period) -> None:
    """After `approve` commits: one push per statement of the month, each to its own agent and
    nobody else (C6, T-VISIB-4).

    An agent without a chat is skipped, and so is one `StaffService.assert_staff_active`
    refuses: a deactivated leaver cannot open "My earnings", and the admin surfaces (A2's tag,
    the drawer, A16 `owed_to_date`) stay the authoritative ones (§7.5, R34). The event id is the
    statement's own, so a retried task or a re-enqueue is one message.

    Best effort, each agent on its own: a payload that cannot be built or a push the broker
    refuses is logged and the next agent is still told, and nothing raises out of the approval,
    which has already committed.
    """
    from business_app.models.sales_pay import SalesPayStatement

    period_id = period.id
    try:
        statements = (
            SalesPayStatement.query.options(joinedload(SalesPayStatement.agent))
            .filter(SalesPayStatement.period_id == period_id)
            .order_by(SalesPayStatement.agent_user_id)
            .all()
        )
    except Exception:  # noqa: BLE001 -- the month is approved; its pushes are best effort
        _push_failed("statement-approved", f"period {period_id}")
        return
    for statement in statements:
        statement_id = statement.id
        try:
            _push_statement_approved(statement)
        except Exception:  # noqa: BLE001 -- one agent's push must not silence the others
            _push_failed("statement-approved", f"statement {statement_id}")


def _push_statement_approved(statement) -> None:
    from business_app.services.staff_service import StaffService
    from business_app.utils.exceptions import ForbiddenError

    agent = statement.agent
    if agent is None or not agent.telegram_id:
        return
    try:
        StaffService.assert_staff_active(agent)
    except ForbiddenError:
        return
    push_sales_event.delay(
        int(agent.telegram_id),
        SALES_EVENT_PAY_STATEMENT_APPROVED,
        pay_statement_approved_payload(statement),
        event_id=f"statement-approved:{statement.id}",
    )
