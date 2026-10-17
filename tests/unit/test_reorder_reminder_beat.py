"""The reorder-reminder beat entry, task registration, rate limit and run lock (spec §11, §12)."""

import sys
from unittest import mock

import pytest
from celery.schedules import crontab

from business_app.tasks import reorder_reminder_tasks  # importing it registers the named tasks
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import `business_app.tasks.celery_app` once, handing `make_celery()` the pytest app (the
    same guard as tests/unit/test_sales_beat_entries.py)."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def test_the_daily_run_is_scheduled_at_the_configured_local_time_with_a_time_limit(celery_app_module):
    entry = celery_app_module.celery.conf.beat_schedule["reorder-reminders-daily"]

    assert entry == {
        "task": "reminders.run_daily",
        "schedule": crontab(hour=10, minute=0),
        "options": {"time_limit": 1800},
    }
    # A run killed at its time limit never leaves the lock held longer than a run may last.
    assert entry["options"]["time_limit"] == reorder_reminder_tasks.RUN_LOCK_SECONDS
    registry = celery_app_module.celery.tasks
    assert "reminders.run_daily" in registry and "reminders.send_reminder" in registry


def test_the_task_module_is_in_the_celery_include_list(celery_app_module):
    """Prod workers import only the `include` list: a module missing from it registers nothing."""
    assert "business_app.tasks.reorder_reminder_tasks" in celery_app_module.celery.conf.include


def test_sends_are_rate_limited_to_about_ten_a_second(celery_app_module):
    assert celery_app_module.celery.conf.task_annotations["reminders.send_reminder"] == {"rate_limit": "10/s"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("11:15", crontab(hour=11, minute=15)),
        ("16:30", crontab(hour=10, minute=0)),
        ("8.30", crontab(hour=10, minute=0)),
        ("", crontab(hour=10, minute=0)),
    ],
)
def test_a_bad_run_time_falls_back_to_ten_instead_of_aborting_the_beat_schedule(celery_app_module, raw, expected):
    """The opposite of `_digest_crontab`, on purpose (spec §11): a typo in a reminder knob must not
    take the whole beat schedule, backups included, down with it."""
    assert celery_app_module._reorder_reminder_crontab({"REORDER_REMINDER_LOCAL_TIME": raw}) == expected


def test_the_run_lock_is_a_registered_reliability_key():
    assert RedisKeyspace.reorder_reminders_run_lock() == "bs:reorder_reminders:run_lock"
    assert KEYSPACE_TIERS["reorder_reminders_run_lock"] is RedisUsageTier.TIER_RELIABILITY
