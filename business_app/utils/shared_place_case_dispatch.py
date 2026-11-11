"""After-commit publishing of the shared-place case alert (spec 2026-11-10 §4.6).

The detection hook opens a case inside the delivery's transaction and parks the alert here. The Celery task is
published only when the outer transaction really commits: a worker started earlier would read no case, and a rolled
back delivery must alert nobody. Modelled on `payment_confirmation_dispatch.py`: the listeners ignore a SAVEPOINT's
release and rollback, so the park survives the hook's own `begin_nested()`.

Best-effort: a publish failure is logged and never reaches the committing request.
"""

import logging

from sqlalchemy import event

from business_app import db as _db

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_shared_place_case_alerts"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


def queue_case_alert(case_id: int, *, countdown: int) -> None:
    """Park one alert, published iff the current transaction commits; once per case."""
    pending = _db.session.info.setdefault(PENDING_KEY, {})
    pending.setdefault(int(case_id), int(countdown))


def register_shared_place_case_dispatch(db) -> None:
    """Attach after_commit / after_rollback listeners to db.session once."""
    global _REGISTERED
    if _REGISTERED:
        return

    session_cls = db.session

    @event.listens_for(session_cls, "after_commit")
    def _drain_on_commit(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT release: the transaction has not committed yet
        pending = session.info.pop(PENDING_KEY, None)
        if not pending:
            return
        # Import lazily to avoid a circular import at module load time.
        from business_app.tasks.customer_link_tasks import post_shared_place_case_alert_task

        for case_id, countdown in pending.items():
            try:
                post_shared_place_case_alert_task.apply_async(args=[case_id], countdown=countdown)
            except Exception:  # best-effort: the case is saved and the admin list shows it
                logger.exception("Failed to publish the shared-place alert for case %s", case_id)

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT rollback: the outer transaction may still commit what was parked before it
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
