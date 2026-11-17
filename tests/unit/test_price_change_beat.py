"""The price-change tick: its beat entry, its time limit and its place in the Celery include list (spec §5.5)."""

import sys
from unittest import mock

import pytest
from celery.schedules import crontab

from business_app.tasks import price_change_tasks  # importing it registers the named task

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import `business_app.tasks.celery_app` once, handing `make_celery()` the pytest app (the
    same guard as tests/unit/test_reorder_reminder_beat.py)."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def test_the_tick_runs_every_five_minutes_and_never_piles_up(celery_app_module):
    entry = celery_app_module.celery.conf.beat_schedule["price-change-tick"]

    assert entry == {
        "task": "price_change.tick",
        "schedule": crontab(minute="*/5"),
        "options": {"expires": 300, "time_limit": price_change_tasks.TICK_TIME_LIMIT_SECONDS},
    }
    assert entry["options"]["time_limit"] == 600
    assert "price_change.tick" in celery_app_module.celery.tasks


def test_the_task_module_is_in_the_celery_include_list(celery_app_module):
    """Prod workers import only the `include` list: a module missing from it registers nothing."""
    assert "business_app.tasks.price_change_tasks" in celery_app_module.celery.conf.include
