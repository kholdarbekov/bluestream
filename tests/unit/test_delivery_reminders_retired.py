"""The 30-minute delivery reminder emails are retired (honest-ETA spec §4.9).

`send_delivery_reminders` emailed the customer and the driver, so the beat entry, the task,
the three templates and the subject go together. `NotificationType.DELIVERY_REMINDER` stays:
existing notification preference rows store its value.
"""

import pathlib
import sys
from unittest import mock

import pytest

import business_app
from business_app.services.email_template_service import EmailTemplateService
from business_app.tasks import delivery_tasks

EMAIL_TEMPLATES_DIR = pathlib.Path(business_app.__file__).parent / "templates" / "emails"
DELIVERY_TASKS_PREFIX = "business_app.tasks.delivery_tasks."


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import `business_app.tasks.celery_app` once, handing `make_celery()` the pytest app
    (the same workaround as test_admin_report_tasks_wiring.py)."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def test_the_reminder_job_is_gone(celery_app_module):
    celery = celery_app_module.celery

    assert "delivery-reminders" not in celery.conf.beat_schedule
    assert not hasattr(delivery_tasks, "send_delivery_reminders")
    assert DELIVERY_TASKS_PREFIX + "send_delivery_reminders" not in celery.tasks
    templates = [EMAIL_TEMPLATES_DIR / language / "delivery_reminder.html" for language in ("en", "ru", "uz")]
    assert [str(path) for path in templates if path.exists()] == []
    assert "delivery_reminder" not in EmailTemplateService.EMAIL_SUBJECTS


def test_every_delivery_beat_entry_still_resolves(celery_app_module):
    """Deleting the reminder job must not leave a beat entry pointing at a task the worker no longer has."""
    celery = celery_app_module.celery
    delivery_entries = {
        key: entry["task"]
        for key, entry in celery.conf.beat_schedule.items()
        if entry["task"].startswith(DELIVERY_TASKS_PREFIX)
    }

    assert "monitor-delivery-delays" in delivery_entries  # the 12-hour admin alert stays scheduled
    assert {key: task for key, task in delivery_entries.items() if task not in celery.tasks} == {}
