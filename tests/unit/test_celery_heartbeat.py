"""The Celery heartbeat: what the worker records, and that beat actually sends it."""

import sys
import time
from unittest import mock

import pytest
from celery.schedules import crontab

import business_app
from business_app.tasks import heartbeat_tasks

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import `business_app.tasks.celery_app` once, handing `make_celery()` the pytest app
    (its bare `create_app()` trips a known bootstrap bug under FLASK_ENV=testing)."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def test_heartbeat_records_when_it_ran(app):
    before = time.time()

    returned = heartbeat_tasks.celery_heartbeat.run()

    stored = float(business_app.redis_client.get(heartbeat_tasks.HEARTBEAT_KEY))
    assert before <= stored <= time.time()
    assert returned == stored


def test_heartbeat_key_is_the_one_monitoring_reads():
    assert heartbeat_tasks.HEARTBEAT_KEY == "bluestream:celery:heartbeat"


def test_beat_sends_the_heartbeat_every_five_minutes(celery_app_module):
    entry = celery_app_module.celery.conf.beat_schedule["celery-heartbeat"]

    assert entry["task"] == "ops.celery_heartbeat"
    assert entry["schedule"] == crontab(minute="*/5")
    # A heartbeat that waited a whole interval is stale; drop it rather than replay a backlog.
    assert entry["options"]["expires"] == 300


def test_heartbeat_task_is_registered_under_its_beat_name(celery_app_module):
    assert "ops.celery_heartbeat" in celery_app_module.celery.tasks


def test_every_task_has_a_hard_time_limit_by_default(celery_app_module):
    # Stays under the broker's 1h visibility timeout, so a hung task is killed
    # (and acked) before it can be re-sent into another worker slot.
    assert celery_app_module.celery.conf.task_time_limit == 1800


def test_an_explicit_task_limit_still_wins_over_the_default():
    from business_app.tasks import analytics_tasks

    assert analytics_tasks.generate_daily_analytics_report.time_limit == 3600
