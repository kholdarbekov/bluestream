"""After-commit publishing of reorder-reminder sends (spec §6 "Reminder step k", §12).

The daily run parks each `(episode_id, step)` here while it writes the `reminder_queued` row, and
the listeners below publish the send task only after that row commits. A worker that started
before the commit would read the episode still at the previous step and refuse it as
`not_active`, and a rolled-back step must send nothing. Modelled on
`payment_confirmation_dispatch.py`, which exists for the same torn-read reason.

Best-effort: a publish failure is logged and never reaches the committing run. The run's
re-publish step (§12 step 8) publishes it again the next day.
"""

import logging

from sqlalchemy import event

from business_app import db as _db

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_reorder_reminder_sends"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False


def queue_reminder_send(episode_id: int, step: int) -> None:
    """Park one send, published iff the current transaction commits; once per (episode, step)."""
    pending = _db.session.info.setdefault(PENDING_KEY, [])
    entry = (int(episode_id), int(step))
    if entry not in pending:
        pending.append(entry)


def _publish_send(episode_id: int, step: int) -> None:
    """Publish one send task, expiring at 18:00 local today: past it, PP-265 forbids the message."""
    from business_app.services.reorder_reminders.config import send_window_end
    from business_app.tasks.reorder_reminder_tasks import send_reorder_reminder
    from business_app.utils.timezone_utils import get_utc_now

    send_reorder_reminder.apply_async(args=[episode_id, step], expires=send_window_end(get_utc_now()))


def register_reorder_reminder_dispatch(db) -> None:
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
        for episode_id, step in pending:
            try:
                _publish_send(episode_id, step)
            except Exception:  # best-effort — never break the committed run
                logger.exception("Failed to publish reorder reminder send for episode %s step %s", episode_id, step)

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
