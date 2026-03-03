"""Scheduled tasks for try-out bottle return reminders."""

from celery import shared_task
from celery.utils.log import get_task_logger

from business_app.services.tryout_service import AdminTryoutService


logger = get_task_logger(__name__)


@shared_task(name="tryouts.process_due_reminders", bind=True, max_retries=2, default_retry_delay=300)
def process_due_tryout_reminders(self):
    """Collect due-soon and overdue try-outs for reminder/reporting workflows."""
    try:
        reminders = AdminTryoutService.get_due_reminder_candidates()
        logger.info(
            "Try-out reminder scan complete: due_soon=%s overdue=%s",
            len(reminders["due_soon"]),
            len(reminders["overdue"]),
        )
        return {
            "due_soon_count": len(reminders["due_soon"]),
            "overdue_count": len(reminders["overdue"]),
            "due_soon_tryout_ids": [row["id"] for row in reminders["due_soon"]],
            "overdue_tryout_ids": [row["id"] for row in reminders["overdue"]],
        }
    except Exception as exc:
        logger.exception("Try-out reminder scan failed")
        raise self.retry(exc=exc)
