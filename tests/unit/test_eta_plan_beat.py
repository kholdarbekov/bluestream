"""The recompute's beat entry, task, queue and publish seam (honest-ETA Phase 2, Task 6; R15 and Ruling P6).

A recompute runs from two places: the beat, every ten minutes at :03, :13 ... :53, and a request
(`request_recompute`), debounced through one Redis marker and published through the ONE seam, `_publish_recompute`.
The task's time limit is the run lock's own seconds, so a run killed at its limit frees the lock as it expires.

Faked, as external I/O only: Celery's publish (`Task.apply_async`, recorded and bound against the task's signature)
and Redis (`eta_redis`), because the suite's lanes share the real Redis DBs. The tests that drive the seam down to
`Task.apply_async` carry `real_eta_publish`, so the suite's autouse no-op of `_publish_recompute` (tests/conftest.py)
leaves the seam to them.
"""

import logging

import pytest
from celery import Celery
from celery import _state as celery_state
from celery.app.task import Task
from celery.schedules import crontab

from business_app.tasks import delivery_eta_tasks  # importing it registers the task
from business_app.tasks.delivery_eta_tasks import recompute_eta_plan
from business_app.utils import eta_recompute_dispatch
from business_app.utils.eta_recompute_dispatch import request_recompute
from shared import business_config
from shared.redis_keyspace import RedisKeyspace
from tests.integration.eta_plan_builders import (  # noqa: F401 -- the fixtures
    celery_app_module_fixture,
    eta_redis_fixture,
    published_fixture,
)
from tests.integration.reorder_reminder_builders import captured_logs

pytestmark = pytest.mark.unit

RECOMPUTE = "business_app.tasks.delivery_eta_tasks.recompute_eta_plan"


def test_the_recompute_beat_entry_and_wiring(celery_app_module):
    celery = celery_app_module.celery

    assert celery.conf.beat_schedule["recompute-eta-plan"] == {
        "task": RECOMPUTE,
        "schedule": crontab(minute="3-53/10"),
    }
    # A run killed at its time limit frees the lock as it expires: the limit is the lock's own seconds (Ruling P6),
    # set once, on the task, so the beat's runs and the requested runs share it.
    task = celery.tasks[RECOMPUTE]
    lock_seconds = business_config.ETA_RECOMPUTE_LOCK_SECONDS
    assert (task.time_limit, task.soft_time_limit) == (lock_seconds, lock_seconds - 20)
    # Prod workers import only the `include` list, and the delivery workers consume the `delivery` queue.
    assert delivery_eta_tasks.__name__ in celery.conf.include
    assert celery.amqp.router.route({}, RECOMPUTE)["queue"].name == "delivery"


@pytest.mark.real_eta_publish
def test_a_request_publishes_one_run_until_that_run_starts(app, eta_redis, published):
    """R15's trailing-edge debounce: a request sets the "recompute queued" marker and publishes through the one seam,
    with a countdown and an expiry ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS after it, so the run outlives its countdown.
    Requests while it is queued publish nothing. The run clears the marker as it starts, and the next request queues
    the next run."""
    ttl = app.config["ETA_RECOMPUTE_SCHEDULED_TTL_SECONDS"]
    countdown = app.config["ETA_RECOMPUTE_COUNTDOWN_SECONDS"]
    marker = RedisKeyspace.eta_recompute_scheduled()

    assert request_recompute() is True
    assert request_recompute(countdown=60) is False

    assert published == [(RECOMPUTE, {"countdown": countdown, "expires": countdown + ttl})]
    assert eta_redis.ttl(marker) == ttl
    eta_redis.delete(marker)  # the queued run starts
    assert request_recompute(countdown=97) is True
    assert published[-1] == (RECOMPUTE, {"countdown": 97, "expires": 97 + ttl})


@pytest.mark.real_eta_publish
def test_a_failed_publish_clears_the_marker(app, eta_redis, monkeypatch):
    """A publish the broker refused queued nothing: the marker goes, so the next request publishes."""

    def broker_down(task, args=None, kwargs=None, **options):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(Task, "apply_async", broker_down)

    assert request_recompute() is False
    assert eta_redis.get(RedisKeyspace.eta_recompute_scheduled()) is None


@pytest.mark.real_eta_publish
def test_an_eager_celery_app_publishes_nothing(app, eta_redis, published, caplog):
    """R15: when the Celery app current at the request runs tasks eagerly (a dev worker or beat), a publish would run
    the recompute inline, inside the caller. No marker is set and nothing is published, the skip is logged, and the
    beat is the backstop."""
    eager = Celery("eager-dev-worker", set_as_current=False)
    eager.conf.task_always_eager = True
    previous = celery_state.get_current_app()
    eager.set_current()
    try:
        with captured_logs(caplog, eta_recompute_dispatch.logger, logging.INFO):
            assert request_recompute() is False
    finally:
        previous.set_current()

    assert published == []
    assert eta_redis.store == {}
    assert [line for line in caplog.records if line.getMessage().startswith("eta_recompute_request_skipped")]


def test_with_the_kill_switch_off_a_run_does_nothing(app, eta_redis):
    """ETA_SHADOW_ENGINE_ENABLED off, as in the tests' config: the beat's run touches neither Redis nor the plan."""
    assert app.config["ETA_SHADOW_ENGINE_ENABLED"] is False

    assert recompute_eta_plan() == {"skipped": "disabled"}
    assert eta_redis.store == {}
