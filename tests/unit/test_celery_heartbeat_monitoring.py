"""Monitoring that consumes the heartbeat: the Prometheus alert and the worker healthcheck."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import business_app
from business_app.tasks.heartbeat_tasks import HEARTBEAT_KEY

pytestmark = pytest.mark.unit


def _rules():
    doc = yaml.safe_load(Path("monitoring/alert_rules.yml").read_text())
    return {rule["alert"]: rule for group in doc["groups"] for rule in group["rules"] if "alert" in rule}


def test_stale_heartbeat_alert_fires_after_three_missed_beats():
    rule = _rules()["CeleryHeartbeatStale"]

    assert rule["expr"] == (
        "(time() - (max(bluestream_celery_heartbeat_timestamp_seconds) or vector(0))) > 900"
    )
    assert rule["for"] == "5m"
    assert rule["labels"] == {"severity": "critical", "area": "celery"}


def test_dead_task_stuck_rule_is_gone():
    # It queried a metric the exporter never exposes and could not see a task that never finishes.
    assert "CeleryTaskStuck" not in _rules()


def _worker_healthcheck():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    return compose["services"]["celery_worker"]["healthcheck"]


def _run_probe():
    test = _worker_healthcheck()["test"]
    assert test[:3] == ["CMD", "python", "-c"], test
    # Same program Docker runs, against the test Redis (conftest points REDIS_URL at it).
    return subprocess.run([sys.executable, "-c", test[3]], env=dict(os.environ), timeout=30).returncode


def test_healthcheck_passes_on_a_fresh_heartbeat(app):
    business_app.redis_client.set(HEARTBEAT_KEY, time.time() - 60)

    assert _run_probe() == 0


def test_healthcheck_fails_once_the_heartbeat_is_older_than_fifteen_minutes(app):
    business_app.redis_client.set(HEARTBEAT_KEY, time.time() - 901)

    assert _run_probe() == 1


def test_healthcheck_fails_when_no_heartbeat_was_ever_recorded(app):
    assert _run_probe() == 1


def test_healthcheck_waits_long_enough_for_the_first_heartbeat():
    # Beat sends one every 5 minutes; a cold start must not be marked unhealthy before it.
    assert _worker_healthcheck()["start_period"] == "360s"
