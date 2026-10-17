"""Celery entry points for lapsed-customer reorder reminders (spec §12).

Registered in tasks/celery_app.py `include`. Both are NAMED tasks with no route, so they run on the
default `celery` queue every prod worker consumes.
"""

import logging
import uuid
from dataclasses import asdict

from celery import shared_task

from business_app import redis_client
from business_app.utils.timezone_utils import get_utc_now
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

# The beat entry's time_limit: a crashed run never holds the lock longer than a run may last.
RUN_LOCK_SECONDS = 1800
# Telegram retries back off 60 s, 120 s, 240 s: from a 15:59 run at the latest they end before 18:00.
SEND_RETRY_BASE_SECONDS = 60


@shared_task(name="reminders.run_daily")
def run_daily_reminders() -> dict:
    """The daily run (spec §12) behind a Redis lock, returning the run summary.

    Every transition is a conditional update, so the lock only keeps two runs from doing the same
    work twice. Without Redis the run is skipped (fail closed, TIER_RELIABILITY); the stale guard
    and the next day's run absorb the gap.

    The daily-run seam (contract amendment 14): `cfg`, `now` and `result` are bound once, and after
    the engine run, inside the lock, the morning digest goes out (§12 step 9): the task imports
    `send_digest_after_run(cfg, *, now)` lazily and sets `result["digest_emails"]` and
    `result["digest_failed"]` (the recipients it could not reach); both 0 in any mode but `on`. A
    digest failure, an import error included, is logged and reported as both 0 with `digest_error`
    True; it never fails or undoes the run, whose transitions are already committed.
    """
    from business_app.services.reorder_reminders.config import current_config
    from business_app.services.reorder_reminders.engine import ReorderReminderEngine

    key = RedisKeyspace.reorder_reminders_run_lock()
    token = uuid.uuid4().hex
    try:
        acquired = bool(redis_client.set(key, token, nx=True, ex=RUN_LOCK_SECONDS))
    except Exception:  # noqa: BLE001 - a lock we cannot take is a run we do not start
        logger.exception("reorder_reminders: the run lock is unavailable; run skipped")
        return {"skipped": "lock_unavailable"}
    if not acquired:
        logger.warning("reorder_reminders: another run holds the lock; run skipped")
        return {"skipped": "locked"}
    try:
        cfg = current_config()
        now = get_utc_now()
        summary = ReorderReminderEngine(cfg, now=now).run()
        result = asdict(summary)
        # §12 step 9: the morning digest, in mode `on` only, after the run's transitions are
        # committed and still inside the lock, so a redelivered run cannot digest twice. Imported
        # here, never at module level, so the task module stays light (amendment 14), and inside
        # the `try`, so even a digest module that fails to import cannot fail the run.
        try:
            from business_app.services.reorder_reminders.digest import send_digest_after_run

            digest = send_digest_after_run(cfg, now=now)
            result["digest_emails"], result["digest_failed"] = digest.sent, digest.failed
        except Exception:  # noqa: BLE001 - the digest follows a committed run and must never fail or undo it
            logger.exception("reorder_reminders: the escalation digest failed; the run itself is complete")
            result["digest_emails"], result["digest_failed"] = 0, 0
            result["digest_error"] = True
        return result
    finally:
        _release_lock(key, token)


def _release_lock(key: str, token: str) -> None:
    """Delete the lock only while it is still ours: a run that outlived its TTL must not drop the
    next run's lock."""
    try:
        if redis_client.get(key) == token.encode():
            redis_client.delete(key)
    except Exception:  # noqa: BLE001 - the TTL frees it anyway
        logger.warning("reorder_reminders: could not release the run lock", exc_info=True)


@shared_task(name="reminders.send_reminder", bind=True, max_retries=3, default_retry_delay=SEND_RETRY_BASE_SECONDS)
def send_reorder_reminder(self, episode_id: int, step: int) -> str:
    """Send one queued reminder after re-checking everything at send time (spec §12 "Send task").

    Published after the run's commit, expiring at 18:00 local today. A retryable channel failure
    (a Telegram 5xx) retries with backoff and is recorded once the retries are spent; a block or an
    email failure is recorded at once.
    """
    from business_app.services.reorder_reminders import messaging

    try:
        return messaging.send_reminder(episode_id, step)
    except messaging.RetryableSendError as exc:
        if self.request.retries >= self.max_retries:
            return messaging.record_send_failure(episode_id, step, exc.detail)
        raise self.retry(exc=exc, countdown=SEND_RETRY_BASE_SECONDS * (2**self.request.retries))
