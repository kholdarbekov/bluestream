"""When an order was delivered: two questions, one helper each.

* `delivered_instants_by_order` is THE delivered instant (spec 2026-09-28 §4.3.2, I-1): the latest
  DELIVERED row in `order_status_history`, per order. `orders` has no delivered column, and
  `update_order_status` is the only writer that always leaves that row; an admin-marked delivery
  leaves `actual_delivery_time` NULL. Sales-agent pay and the outlet stage job read it.
* `delivered_at_utc` anchors the post-delivery EDIT windows (order-item edits and collected-cash
  edits). It answers differently for an admin-marked delivery, and it stays that way: moving the
  edit window onto the history row would change those orders' 72 h window.
"""

from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

from sqlalchemy import func

from business_app import db
from business_app.models.order import OrderStatusHistory
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus

# Ids per `IN` list. A sync over a busy agent's lookback can pass thousands of ids; one statement
# per chunk keeps every bind list well inside the drivers' limits.
DELIVERED_INSTANTS_CHUNK = 500


def delivered_instants_by_order(order_ids: Iterable[int]) -> Dict[int, datetime]:
    """`{order_id: when it was delivered}`, in aware UTC, for each given order with a DELIVERED row.

    The LATEST such row per order (`max(changed_at)`): a re-delivered order carries two, and the
    first would date a delivery the customer no longer has. An order with no DELIVERED row is
    absent, never None. The order's current status is not read, because whether it is STILL
    delivered is the caller's question. `idx_order_status_history_new_status_changed_at` and the
    `order_id` index serve it.
    """
    ids = sorted({int(order_id) for order_id in order_ids})
    instants: Dict[int, datetime] = {}
    for offset in range(0, len(ids), DELIVERED_INSTANTS_CHUNK):
        rows = (
            db.session.query(OrderStatusHistory.order_id, func.max(OrderStatusHistory.changed_at))
            .filter(
                OrderStatusHistory.order_id.in_(ids[offset : offset + DELIVERED_INSTANTS_CHUNK]),
                OrderStatusHistory.new_status == OrderStatus.DELIVERED,
            )
            .group_by(OrderStatusHistory.order_id)
            .all()
        )
        for order_id, changed_at in rows:
            instants[order_id] = ensure_utc(changed_at)
    return instants


def delivered_at_utc(order) -> Optional[datetime]:
    """The anchor of the post-delivery edit windows, as tz-aware UTC. NOT the delivered instant.

    Prefers the Delivery row's actual delivery time and falls back to the order's ``paid_at``
    (set when a cash order is marked DELIVERED), so an admin-marked delivery, which leaves the
    Delivery time NULL, anchors on its payment. The delivered instant is
    `delivered_instants_by_order`. Returns None when neither is available. Naive datetimes are
    interpreted as UTC.
    """
    delivery = getattr(order, "delivery", None)
    if delivery is not None:
        value = getattr(delivery, "actual_delivery", None) or getattr(delivery, "actual_delivery_time", None)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value
    paid_at = getattr(order, "paid_at", None)
    if paid_at is not None:
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=timezone.utc)
        return paid_at
    return None
