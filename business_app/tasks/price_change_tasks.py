"""The price-change tasks (spec 2026-11-16): the tick (§5.5) and the send of one announcement or reminder (§5.3).

Registered in tasks/celery_app.py `include`; the tick is in beat. Each step of the tick runs on its own: a failing step
is rolled back, logged and reported as "failed" in the result, and the next step still runs. There is no run lock: the
switch locks each change and re-checks it, so two overlapping ticks cannot apply one change twice.

`price_change.send_notice` is a NAMED task with no route (the default `celery` queue every prod worker consumes),
rate-limited in celery_app.py `task_annotations`.
"""

import logging
from typing import Any, Callable

from celery import shared_task

from business_app import db
from business_app.utils.timezone_utils import get_utc_now

logger = logging.getLogger(__name__)

# The beat entry's time_limit: a tick killed at its limit is re-run by the next five-minute beat.
TICK_TIME_LIMIT_SECONDS = 600
# A send's retries back off 60 s, 120 s, 240 s (as `reminders.send_reminder`'s).
SEND_RETRY_BASE_SECONDS = 60


@shared_task(name="price_change.tick")
def price_change_tick() -> dict:
    """One tick: the switch, then every later step of the price-change plan with the same `now`, reported in the
    returned dict. Nothing is raised: a failing step is reported as "failed" and the tick ends normally."""
    from business_app.services.price_change import notices
    from business_app.services.price_change.switch import apply_due_price_changes

    now = get_utc_now()
    result: dict = {}
    _run_step(result, "applied", lambda: apply_due_price_changes(now))
    _run_step(result, "announcements_queued", lambda: notices.start_due_announcements(now))
    _run_step(result, "reminders_queued", lambda: notices.start_due_reminders(now))
    _run_step(result, "notices_republished", lambda: notices.republish_unsent_notices(now))
    # Extension point: later price-change steps are added here as `_run_step(result, "<name>", lambda: ...)`, each
    # with the same `now`.
    return result


def _run_step(result: dict, name: str, step: Callable[[], Any]) -> None:
    """Run one step into `result[name]`. A failure rolls the session back, is logged, and is reported as "failed"."""
    try:
        result[name] = step()
    except Exception:  # noqa: BLE001 - one step's failure never skips the next
        db.session.rollback()
        logger.exception("price_change.tick: step %s failed", name)
        result[name] = "failed"


@shared_task(name="price_change.send_notice", bind=True, max_retries=3, default_retry_delay=SEND_RETRY_BASE_SECONDS)
def send_price_change_notice(self, price_change_id: int, user_id: int, kind: str) -> str:
    """Send one queued announcement or reminder and record its channel (`notices.send_notice`), returning its outcome.

    Published after the commit that queued its row. A bot that could not be reached retries with backoff, under the one
    notice id the bot sends once; once the retries are spent the row stays queued with the error (`record_unsent`) and
    the tick's republish sends it again.
    """
    from business_app.services.customer_notices import RetryableNoticeError
    from business_app.services.price_change import notices

    try:
        return notices.send_notice(price_change_id, user_id, kind)
    except RetryableNoticeError as exc:
        if self.request.retries >= self.max_retries:
            return notices.record_unsent(price_change_id, user_id, kind, str(exc))
        logger.warning("price_change.send_notice %s/%s/%s will be retried: %s", price_change_id, user_id, kind, exc)
        raise self.retry(exc=exc, countdown=SEND_RETRY_BASE_SECONDS * 2**self.request.retries)
