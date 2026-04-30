"""
Celery application configuration for the Water Business Platform
This file should be placed in business_app/tasks/celery_app.py
"""

import logging
from celery import Celery
from celery.schedules import crontab
from celery.signals import before_task_publish, setup_logging, task_prerun
from flask import g, has_request_context
import os
from shared.constants import DISPLAY_TIMEZONE

logger = logging.getLogger(__name__)


def make_celery(app=None):
    """Create and configure Celery app"""
    if app is None:
        from .. import create_app

        app = create_app()

    celery = Celery(
        app.import_name,
        backend=app.config["CELERY"]["result_backend"],
        broker=app.config["CELERY"]["broker_url"],
        include=[
            "business_app.tasks.payment_tasks",
            "business_app.tasks.notification_tasks",
            "business_app.tasks.delivery_tasks",
            "business_app.tasks.analytics_tasks",
            "business_app.tasks.subscription_tasks",
            "business_app.tasks.order_tasks",
            "business_app.tasks.audit_tasks",
            "business_app.tasks.session_tasks",
            "business_app.tasks.loyalty_tasks",
            "business_app.tasks.tryout_tasks",
            "business_app.tasks.backup_tasks",
            "business_app.tasks.marking_code_tasks",
        ],
    )

    # Update configuration from Flask config
    celery.conf.update(app.config["CELERY"])

    reminder_interval = int(app.config.get("COD_REMINDER_INTERVAL_MINUTES", 60) or 60)
    if reminder_interval <= 0:
        reminder_interval = 60

    if reminder_interval >= 60:
        reminder_schedule = crontab(minute=0)
    else:
        reminder_schedule = crontab(minute=f"*/{reminder_interval}")

    # Configure periodic tasks
    celery.conf.beat_schedule = {
        # Process subscription billing daily at 9 AM
        "process-subscription-billing": {
            "task": "business_app.tasks.subscription_tasks.process_daily_subscription_billing",
            "schedule": crontab(hour=9, minute=0),
        },
        # Expire loyalty points daily at midnight
        "expire-loyalty-points": {
            "task": "business_app.tasks.loyalty_tasks.expire_loyalty_points",
            "schedule": crontab(hour=0, minute=0),
        },
        # Send points expiring soon reminders daily at 10 AM
        "points-expiring-reminders": {
            "task": "business_app.tasks.loyalty_tasks.send_points_expiring_soon_reminders",
            "schedule": crontab(hour=10, minute=0),
        },
        # Update loyalty tiers monthly on 1st at 1 AM
        "update-loyalty-tiers": {
            "task": "business_app.tasks.loyalty_tasks.update_loyalty_tiers",
            "schedule": crontab(hour=1, minute=0, day_of_month=1),
        },
        # Send delivery reminders every 30 minutes
        "delivery-reminders": {
            "task": "business_app.tasks.delivery_tasks.send_delivery_reminders",
            "schedule": crontab(minute="*/30"),
        },
        # Reconcile PENDING payments against the gateway every 15 minutes (PAY-007).
        # Polls payments older than PAYMENT_RECONCILE_AFTER_MINUTES (default 10 min)
        # and auto-cancels ones still unknown to the gateway past PAYMENT_TIMEOUT_MINUTES.
        "reconcile-pending-payments": {
            "task": "business_app.tasks.payment_tasks.reconcile_pending_payments",
            "schedule": crontab(minute="*/15"),
        },
        # Mark prior-day COD reconciliation sessions as overdue every hour.
        "mark-overdue-cod-reconciliation-sessions": {
            "task": "business_app.tasks.payment_tasks.mark_overdue_cod_reconciliation_sessions",
            "schedule": crontab(minute=15),
        },
        # Send COD reconciliation reminders based on configured cadence.
        "send-cod-reconciliation-reminders": {
            "task": "business_app.tasks.payment_tasks.send_cod_reconciliation_reminders",
            "schedule": reminder_schedule,
        },
        # Generate daily analytics report
        "daily-analytics-report": {
            "task": "business_app.tasks.analytics_tasks.generate_daily_analytics_report",
            "schedule": crontab(hour=23, minute=0),
        },
        # Clean up old notifications weekly
        "cleanup-old-notifications": {
            "task": "business_app.tasks.notification_tasks.cleanup_old_notifications",
            "schedule": crontab(hour=2, minute=0, day_of_week=1),  # Monday at 2 AM
        },
        # Auto-confirm orders after 10 minutes
        "auto-confirm-orders": {
            "task": "business_app.tasks.order_tasks.auto_confirm_pending_orders",
            "schedule": crontab(minute="*/10"),
        },
        # Send subscription renewal reminders
        "subscription-renewal-reminders": {
            "task": "business_app.tasks.subscription_tasks.send_renewal_reminders",
            "schedule": crontab(hour=10, minute=0),  # Daily at 10 AM
        },
        # Optimize delivery routes daily
        "optimize-delivery-routes": {
            "task": "business_app.tasks.delivery_tasks.optimize_daily_delivery_routes",
            "schedule": crontab(hour=7, minute=0),  # Daily at 7 AM
        },
        # Process failed payments retry
        "retry-failed-payments": {
            "task": "business_app.tasks.payment_tasks.retry_failed_payments",
            "schedule": crontab(hour=12, minute=0),  # Daily at noon
        },
        # Generate weekly business reports
        "weekly-business-report": {
            "task": "business_app.tasks.analytics_tasks.generate_weekly_business_report",
            "schedule": crontab(hour=8, minute=0, day_of_week=1),  # Monday at 8 AM
        },
        # Update customer segments monthly
        "update-customer-segments": {
            "task": "business_app.tasks.analytics_tasks.update_customer_segments",
            "schedule": crontab(hour=3, minute=0, day_of_month=1),  # 1st day of month at 3 AM
        },
        # Clean up old audit logs weekly (Sunday at 3 AM)
        # Configuration can be overridden via environment variables:
        # - AUDIT_LOG_RETENTION_DAYS (default: 90)
        # - AUDIT_LOG_BATCH_SIZE (default: 1000)
        # - AUDIT_LOG_PRESERVE_CRITICAL (default: true)
        "cleanup-old-audit-logs": {
            "task": "business_app.tasks.audit_tasks.cleanup_old_audit_logs_task",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),  # Sunday at 3 AM
            "kwargs": {
                "retention_days": int(os.getenv("AUDIT_LOG_RETENTION_DAYS", 90)),
                "batch_size": int(os.getenv("AUDIT_LOG_BATCH_SIZE", 1000)),
                "preserve_critical": os.getenv("AUDIT_LOG_PRESERVE_CRITICAL", "true").lower() == "true",
            },
        },
        # Clean up expired sessions daily at 4 AM
        "cleanup-expired-sessions": {
            "task": "business_app.tasks.session_tasks.cleanup_expired_sessions_task",
            "schedule": crontab(hour=4, minute=0),  # Daily at 4 AM
            "kwargs": {"batch_size": 1000},
        },
        # Scan due-soon and overdue try-out bottle returns daily.
        "process-tryout-reminders": {
            "task": "tryouts.process_due_reminders",
            "schedule": crontab(hour=9, minute=30),
        },
        # INF-008: nightly Postgres backup. 02:30 UTC = 07:30 Tashkent —
        # off-peak. pg_dump → gzip → optional rclone offsite copy.
        # Local retention: BACKUP_LOCAL_RETENTION_DAYS (default 14).
        "backup-database": {
            "task": "backup.database",
            "schedule": crontab(hour=2, minute=30),
        },
        # INF-008: weekly uploads backup. Sunday 03:00 UTC = 08:00 Tashkent.
        # Larger payload than DB so weekly cadence keeps storage reasonable;
        # uploads are typically immutable (avatars, fiscalization receipts)
        # so daily diffs would be wasteful.
        "backup-uploads": {
            "task": "backup.uploads",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),
        },
        # Pre-register marking codes with the Tax Committee (Asl Belgisi)
        # daily at 00:00 UTC (05:00 Tashkent). Sizes the per-product pool
        # from the previous 7 days of card+click sales, then fans out a
        # replenish task per fiscalisable product. Card payments during the
        # day allocate from this pool and skip the synchronous TC call.
        "pre-register-marking-codes": {
            "task": "business_app.tasks.marking_code_tasks.pre_register_marking_codes_daily",
            "schedule": crontab(hour=0, minute=0),
        },
    }

    # Set timezone
    celery.conf.timezone = DISPLAY_TIMEZONE

    class ContextTask(celery.Task):
        """Make celery tasks work with Flask app context"""

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery


# Create default celery app
celery = make_celery()


# =========================================================================
# Distributed tracing: propagate X-Request-ID from Flask → Celery tasks
# =========================================================================


@before_task_publish.connect
def propagate_request_id(headers=None, **kwargs):
    """Inject request_id into Celery task headers when publishing from Flask context."""
    if headers is not None and has_request_context():
        request_id = getattr(g, "request_id", None)
        if request_id:
            headers["request_id"] = request_id


@task_prerun.connect
def set_task_request_id(task=None, **kwargs):
    """Extract request_id from task headers and make it available in task context."""
    request_id = getattr(task.request, "request_id", None)
    if not request_id:
        # Check custom headers
        headers = getattr(task.request, "headers", None) or {}
        request_id = headers.get("request_id")
    if request_id:
        # Store on task request for easy access in task code
        task.request.request_id = request_id
        logger.debug(f"Task {task.name} running with request_id={request_id}")


@setup_logging.connect
def keep_app_logging_config(**kwargs):
    # Connecting any receiver here makes celery skip worker_hijack_root_logger,
    # which would otherwise wipe the handlers our setup_enhanced_logging
    # installed on the `celery` logger and silence beat's scheduler logs.
    pass


# Task routing configuration
celery.conf.task_routes = {
    "business_app.tasks.payment_tasks.*": {"queue": "payment"},
    "business_app.tasks.notification_tasks.*": {"queue": "notifications"},
    "business_app.tasks.delivery_tasks.*": {"queue": "delivery"},
    "business_app.tasks.analytics_tasks.*": {"queue": "analytics"},
    "business_app.tasks.subscription_tasks.*": {"queue": "subscriptions"},
    "business_app.tasks.order_tasks.*": {"queue": "orders"},
    "business_app.tasks.audit_tasks.*": {"queue": "maintenance"},
    "business_app.tasks.session_tasks.*": {"queue": "maintenance"},
    "business_app.tasks.loyalty_tasks.*": {"queue": "loyalty"},
    "business_app.tasks.tryout_tasks.*": {"queue": "maintenance"},
    "business_app.tasks.backup_tasks.*": {"queue": "maintenance"},
    "business_app.tasks.marking_code_tasks.*": {"queue": "payment"},
}


# Task priority configuration
celery.conf.task_default_priority = 5
celery.conf.worker_prefetch_multiplier = 1
celery.conf.task_acks_late = True
celery.conf.worker_disable_rate_limits = False

# Per-task rate limits — prevents flooding external services (SMS, email, Telegram API)
celery.conf.task_annotations = {
    # Bulk/promotional notifications — strictest limits
    "business_app.tasks.notification_tasks.send_bulk_promotional_notification": {"rate_limit": "5/m"},
    "business_app.tasks.notification_tasks.send_bulk_notification_task": {"rate_limit": "10/m"},
    "business_app.tasks.notification_tasks.send_emergency_notification": {"rate_limit": "10/m"},
    # Individual notification sends
    "business_app.tasks.notification_tasks.send_verification_sms_task": {"rate_limit": "30/m"},
    "business_app.tasks.notification_tasks.send_verification_email_task": {"rate_limit": "30/m"},
    "business_app.tasks.notification_tasks.send_password_reset_sms_task": {"rate_limit": "20/m"},
    "business_app.tasks.notification_tasks.send_password_reset_email_task": {"rate_limit": "20/m"},
    "business_app.tasks.notification_tasks.send_registration_otp_task": {"rate_limit": "30/m"},
    "business_app.tasks.notification_tasks.send_welcome_sms_task": {"rate_limit": "30/m"},
    # Transactional notifications — higher throughput
    "business_app.tasks.notification_tasks.send_order_notification_task": {"rate_limit": "60/m"},
    "business_app.tasks.notification_tasks.send_delivery_update_task": {"rate_limit": "60/m"},
    "business_app.tasks.notification_tasks.send_payment_confirmation_task": {"rate_limit": "60/m"},
    # Payment processing
    "business_app.tasks.payment_tasks.process_payment_webhook": {"rate_limit": "60/m"},
    "business_app.tasks.payment_tasks.retry_failed_payments": {"rate_limit": "10/m"},
}


# Error handling configuration
celery.conf.task_reject_on_worker_lost = True
celery.conf.task_ignore_result = False
celery.conf.result_expires = 3600  # 1 hour


# Monitoring configuration
celery.conf.worker_send_task_events = True
celery.conf.task_send_sent_event = True


if __name__ == "__main__":
    celery.start()
