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

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from celery import shared_task
from celery.utils.log import get_task_logger
from flask import current_app

from business_app import db
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.utils.prometheus_metrics import set_stranded_deliveries
from shared.enums import DeliveryStatus, OrderStatus

logger = get_task_logger(__name__)

# Pool statuses that must never retain a driver assignment.
POOL_STATUSES = (DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING)

# A driverless pool delivery older than this (minutes) is re-offered for
# auto-assignment. Below it, the creation-time auto_assign is still in flight.
STALE_POOL_REENQUEUE_MINUTES_DEFAULT = 10


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


@shared_task(time_limit=120, soft_time_limit=90)
def reenqueue_stale_pool_deliveries() -> Dict[str, Any]:
    """Re-offer long-unassigned pool deliveries for auto-assignment.

    ``auto_assign_delivery_task`` gives up after a few retries (e.g. when no
    driver was on-shift yet) and logs that a "periodic re-enqueue will retry
    later" — this task *is* that re-enqueue. It finds SCHEDULED, driverless
    deliveries whose order is still confirmed/preparing and that have sat in the
    pool past the threshold, and re-queues ``auto_assign_delivery_task`` for
    each. Without it, an order that missed its assignment window (created before
    any driver's shift, or while auto-assign wrongly saw no on-shift driver)
    stays invisibly stuck in the pool indefinitely.
    """
    from business_app.tasks.delivery_tasks import auto_assign_delivery_task

    threshold_minutes = int(
        current_app.config.get(
            "STALE_POOL_REENQUEUE_MINUTES", STALE_POOL_REENQUEUE_MINUTES_DEFAULT
        )
    )
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

    stale = (
        db.session.query(Delivery.id, Delivery.created_at, Order.order_number)
        .join(Order, Order.id == Delivery.order_id)
        .filter(
            Delivery.status == DeliveryStatus.SCHEDULED,
            Delivery.delivery_person_id.is_(None),
            Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.PREPARING]),
            Delivery.created_at < cutoff,
        )
        .order_by(Delivery.created_at.asc())
        .all()
    )

    delivery_ids = [row.id for row in stale]
    for delivery_id in delivery_ids:
        auto_assign_delivery_task.delay(delivery_id)

    if delivery_ids:
        details = "; ".join(
            f"delivery={row.id} order={row.order_number}" for row in stale[:50]
        )
        logger.warning(
            "Re-enqueued %d stale unassigned pool deliveries (older than %d min) "
            "for auto-assign: %s",
            len(delivery_ids),
            threshold_minutes,
            details,
        )
    else:
        logger.info("No stale unassigned pool deliveries to re-enqueue")

    return {
        "reenqueued_count": len(delivery_ids),
        "delivery_ids": delivery_ids,
    }
