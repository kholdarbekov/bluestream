"""After-commit publishing of a driver's silent route-card refresh when one of their stops is cancelled.

Owner, 2026-10-22: when an order is cancelled, the pinned route card of the driver who held the
stop is refreshed with no sound and no alert. Every cancel path reaches one choke point,
`OrderService._cancel_delivery_for_cancelled_order`, which parks `(driver_id, delivery_id)` here.
The listeners below publish `refresh_driver_route_card_task` only after the commit that persists
the cancel, and drop it on a rollback. A listener, and not a publish after the cancel's own commit,
because `update_order_status` commits only when its caller asks: some callers pass `commit=False`
and commit later themselves. Modelled on `reorder_reminder_dispatch.py`.

Best-effort: a publish failure is logged and never reaches the committed cancel. Nothing
re-publishes it; the card is refreshed again on the driver's next tap or the next route push.
"""

import logging

from sqlalchemy import event

from business_app import db as _db

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_route_card_refreshes"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


def queue_route_card_refresh(driver_id: int, delivery_id: int) -> None:
    """Park one refresh, published iff the current transaction commits; once per (driver, stop)."""
    pending = _db.session.info.setdefault(PENDING_KEY, [])
    entry = (int(driver_id), int(delivery_id))
    if entry not in pending:
        pending.append(entry)


def _publish_refresh(driver_id: int, delivery_id: int) -> None:
    """Publish one refresh task. No SQL: this runs inside the after-commit hook."""
    from business_app.tasks.delivery_tasks import refresh_driver_route_card_task

    refresh_driver_route_card_task.delay(driver_id, delivery_id)


def register_route_card_refresh_dispatch(db) -> None:
    """Attach after_commit / after_rollback listeners to db.session once."""
    global _REGISTERED
    if _REGISTERED:
        return

    session_cls = db.session

    @event.listens_for(session_cls, "after_commit")
    def _drain_on_commit(session):  # noqa: ANN001
        pending = session.info.pop(PENDING_KEY, None)
        if not pending:
            return
        for driver_id, delivery_id in pending:
            try:
                _publish_refresh(driver_id, delivery_id)
            except Exception:  # best-effort — never break the committed cancel
                logger.exception(
                    "Failed to publish the route-card refresh for driver %s after delivery %s was cancelled",
                    driver_id,
                    delivery_id,
                )

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
