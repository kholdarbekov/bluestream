"""Periodic monitoring for delivery data-integrity anomalies.

Currently watches for "stranded" deliveries: rows in a pool status
(scheduled/pending) that still carry a ``delivery_person_id``. Such rows are
invisible to both the driver's active list (status filter excludes
scheduled/pending) and the unassigned pool (which only lists driverless rows),
so they silently fall out of every operational screen.

The supported way to avoid creating these is ``StaffService.return_delivery_to_pool``
(which clears the driver) plus the ``assert_unassigned_for_pool_status``
invariant; this task is the runtime backstop that surfaces any that slip through
(e.g. raw DB edits).
"""

from typing import Any, Dict

from celery import shared_task
from celery.utils.log import get_task_logger

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.utils.prometheus_metrics import set_stranded_deliveries
from shared.enums import DeliveryStatus

logger = get_task_logger(__name__)

# Pool statuses that must never retain a driver assignment.
POOL_STATUSES = (DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING)


@shared_task(time_limit=120, soft_time_limit=90)
def monitor_stranded_deliveries() -> Dict[str, Any]:
    """Find deliveries stuck in a pool status while still assigned to a driver.

    Emits a Prometheus gauge (`stranded_deliveries`) and a structured warning
    log (picked up by Loki/Grafana) so the anomaly is observable and alertable.
    """
    stranded = (
        db.session.query(
            Delivery.id,
            Delivery.status,
            Delivery.delivery_person_id,
            Order.order_number,
        )
        .join(Order, Order.id == Delivery.order_id)
        .filter(
            Delivery.status.in_(POOL_STATUSES),
            Delivery.delivery_person_id.isnot(None),
        )
        .order_by(Delivery.id.asc())
        .all()
    )

    count = len(stranded)
    set_stranded_deliveries(count)

    if count:
        details = "; ".join(
            f"delivery={row.id} order={row.order_number} "
            f"status={row.status.value if hasattr(row.status, 'value') else row.status} "
            f"driver={row.delivery_person_id}"
            for row in stranded[:50]
        )
        logger.warning(f"Found {count} stranded deliveries (pool status + assigned driver): {details}")
    else:
        logger.info("No stranded deliveries found")

    return {
        "stranded_count": count,
        "delivery_ids": [row.id for row in stranded],
    }
