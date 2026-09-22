"""After-commit dispatch of sales events. Call these ONLY after db.session.commit()."""

from typing import Optional

from business_app.models.sales import Outlet
from business_app.models.user import User
from business_app.tasks.sales_agent_tasks import notify_managers_activation_requested, push_sales_event


def _agent_for(outlet: Outlet) -> Optional[User]:
    agent_id = outlet.assigned_agent_user_id or outlet.onboarded_by_user_id
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


def notify_agent_order_event(order, outlet: Outlet, event: str, *, reason: Optional[str] = None) -> None:
    """The store answered an order this agent placed on its behalf.

    `reason` is the store's own words on a decline — it is the agent's next
    action, so it rides in the payload rather than being looked up bot-side.

    The recipient is the agent who PLACED the order, not the outlet's assigned
    one: a covering agent (one onboarded the store, a manager later reassigned
    it) passes `get_for_agent` and can place the order, and routing by the
    outlet then told an agent who placed nothing that "the store confirmed" while
    the one standing at the counter heard nothing at all. `_agent_for` remains the
    fallback for an order with no creator on it.
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
    )
