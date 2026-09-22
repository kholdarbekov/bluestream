"""The sales beat entries actually reach a registered task.

`test_celery_task_wiring_cleanup.py` cannot absorb these: it resolves a DOTTED
module path and demands a `time_limit`. Both sales tasks are addressed by an
explicit `name=` string instead (`sales.*`), which beat looks up in the Celery
task REGISTRY — a typo there is invisible to an import-based check and would
simply mean the sweep never runs in production, silently, forever.
"""

import sys
from unittest import mock

import pytest
from celery.schedules import crontab

# Importing the module is what registers the `sales.*` tasks on the app.
from business_app.tasks import sales_agent_tasks  # noqa: F401

pytestmark = pytest.mark.unit

# The name each entry must resolve to, and the cadence ruled in the plan. The hours are
# LOCAL: `celery.conf.timezone` is DISPLAY_TIMEZONE (celery_app.py:381), which is the whole
# reason `SALES_DIGEST_LOCAL_TIME` can be written as the wall time the owner wants.
EXPECTED = {
    "expire-agent-order-confirmations": ("sales.expire_agent_order_confirmations", crontab(minute="*/10")),
    "abandon-stale-sales-visits": ("sales.abandon_stale_visits", crontab(minute=25)),
    "recompute-sales-outlets": ("sales.recompute_all_outlets", crontab(hour=1, minute=0)),
    "update-sales-outlet-stages": ("sales.update_outlet_stages", crontab(hour=1, minute=10)),
    # 01:20, ten minutes behind the stage sweep: the snapshot READS the due date
    # both jobs above rewrite, so running it earlier would file a plan against a
    # column that is still moving.
    "snapshot-sales-agent-day-plans": ("sales.snapshot_agent_day_plans", crontab(hour=1, minute=20)),
    "send-agent-morning-digest": ("sales.send_agent_morning_digest", crontab(hour=8, minute=30)),
    "notify-sales-exception-summary": ("sales.notify_managers_exception_summary", crontab(hour=8, minute=0)),
}


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import `business_app.tasks.celery_app` exactly once, safely.

    Same guard as `test_celery_task_wiring_cleanup.py`: its module-level
    `celery = make_celery()` calls the bare `create_app()`, which trips a
    pre-existing bootstrap bug under FLASK_ENV=testing, so hand `make_celery()`
    the already-initialized pytest app.
    """
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_sales_beat_entry_is_scheduled_and_its_task_name_is_registered(celery_app_module, key):
    expected_task, expected_schedule = EXPECTED[key]
    schedule = celery_app_module.celery.conf.beat_schedule

    assert key in schedule, f"missing beat_schedule entry: {key}"
    entry = schedule[key]
    assert entry["task"] == expected_task
    assert entry["schedule"] == expected_schedule

    # The registry lookup beat itself performs. A typo'd `name=` resolves to
    # nothing here, exactly as it would resolve to nothing on the worker.
    registry = celery_app_module.celery.tasks
    assert entry["task"] in registry, (
        f"beat entry {key!r} points at {entry['task']!r}, which is not a registered Celery task "
        f"(a `name=` typo would strand this sweep in production)"
    )
    assert hasattr(registry[entry["task"]], "run")


def test_the_digest_crontab_is_the_configured_local_time(celery_app_module):
    """"08:30" IS the crontab — no conversion — which is what lets the owner move the digest
    by setting one environment variable to a wall time they can read."""
    assert celery_app_module._digest_crontab("09:05") == crontab(hour=9, minute=5)
    assert celery_app_module._digest_crontab("00:00") == crontab(hour=0, minute=0)


@pytest.mark.parametrize("raw", ["8.30", "0830", "", "08:30:00", None, "25:00", "08:61"])
def test_a_malformed_digest_time_refuses_at_startup(celery_app_module, raw):
    """A typo must not silently move the digest, and it must not be absorbed either: a
    "fall back to 08:30" here would plant a second copy of the default that
    `shared/business_config.py` already owns, and would look exactly like a working config
    while the 09:00 the owner chose never fired.

    "At startup", not "at import": `beat_schedule` is built inside `make_celery(app)`, so
    the refusal lands when the app (and therefore the worker or beat process) is created —
    which is still where a deploy sees it, and still takes every other beat entry with it.
    """
    with pytest.raises(ValueError):
        celery_app_module._digest_crontab(raw)
