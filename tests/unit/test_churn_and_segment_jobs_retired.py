"""D9: the weekly churn_alert report and the dead monthly segments job are retired (spec §9.3).

`predict_customer_churn` stays (the Analytics churn-score tab) and `_get_customer_churn_analysis`
stays (the weekly and monthly reports read it).
"""

import pathlib
import sys
from unittest import mock

import pytest

import business_app
from business_app.services.analytics_service import AnalyticsService
from business_app.services.email_template_service import EmailTemplateService
from business_app.tasks import analytics_tasks

EMAIL_TEMPLATES_DIR = pathlib.Path(business_app.__file__).parent / "templates" / "emails"


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import `business_app.tasks.celery_app` once, handing `make_celery()` the pytest app
    (the same workaround as test_admin_report_tasks_wiring.py)."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def test_the_two_jobs_and_their_beat_entries_are_gone(celery_app_module):
    schedule = celery_app_module.celery.conf.beat_schedule

    assert "generate-churn-prediction-report" not in schedule
    assert "update-customer-segments" not in schedule
    assert not hasattr(analytics_tasks, "generate_churn_prediction_report")
    assert not hasattr(analytics_tasks, "update_customer_segments")
    assert "business_app.tasks.analytics_tasks.generate_churn_prediction_report" not in celery_app_module.celery.tasks
    assert "business_app.tasks.analytics_tasks.update_customer_segments" not in celery_app_module.celery.tasks


def test_the_analytics_tasks_module_stays_scheduled_and_included(celery_app_module):
    """Deleting two tasks must not take the module (and its other report jobs) out of the worker."""
    celery = celery_app_module.celery
    schedule = celery.conf.beat_schedule

    assert "business_app.tasks.analytics_tasks" in celery.conf.include
    for key in ("weekly-agent-performance-report", "generate-demand-forecast", "daily-analytics-report"):
        assert key in schedule, f"missing beat entry: {key}"
        assert schedule[key]["task"] in celery.tasks, f"{schedule[key]['task']} is not registered"
    assert "business_app.tasks.analytics_tasks.generate_weekly_business_report" in celery.tasks


def test_every_analytics_beat_entry_still_resolves_to_a_registered_task(celery_app_module):
    """The deleted functions leave no beat entry pointing at a task the worker no longer has."""
    celery = celery_app_module.celery
    prefix = "business_app.tasks.analytics_tasks."
    analytics_entries = {
        key: entry["task"] for key, entry in celery.conf.beat_schedule.items() if entry["task"].startswith(prefix)
    }

    assert analytics_entries  # the module is still scheduled
    assert {key: task for key, task in analytics_entries.items() if task not in celery.tasks} == {}


def test_churn_alert_has_no_subject_and_no_template():
    assert "churn_alert" not in EmailTemplateService.EMAIL_SUBJECTS
    assert [language for language in ("uz", "ru", "en") if (EMAIL_TEMPLATES_DIR / language / "churn_alert.html").exists()] == []


def test_the_churn_estimate_and_the_report_input_stay():
    assert callable(AnalyticsService.predict_customer_churn)
    assert callable(AnalyticsService._get_customer_churn_analysis)
