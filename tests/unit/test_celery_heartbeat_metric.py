"""The heartbeat gauge Prometheus alerts on: copied from Redis on each /metrics scrape."""

import pytest

import business_app
from business_app.tasks.heartbeat_tasks import HEARTBEAT_KEY
from business_app.utils import prometheus_metrics as pm

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _sentinel_gauge():
    # -1 distinguishes "left unchanged" from "set to 0" (0 would read as stale forever).
    pm.celery_heartbeat_timestamp.set(-1)
    yield


def _gauge():
    return pm.celery_heartbeat_timestamp._value.get()


def test_gauge_takes_the_timestamp_the_worker_wrote(app):
    business_app.redis_client.set(HEARTBEAT_KEY, 1790000000.5)

    pm._refresh_celery_heartbeat_gauge()

    assert _gauge() == 1790000000.5


def test_missing_key_leaves_the_gauge_untouched(app):
    pm._refresh_celery_heartbeat_gauge()

    assert _gauge() == -1


def test_redis_error_leaves_the_gauge_untouched(app, monkeypatch):
    def boom(*_args, **_kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(business_app.redis_client, "get", boom)

    pm._refresh_celery_heartbeat_gauge()  # must not raise

    assert _gauge() == -1


def test_unparseable_value_leaves_the_gauge_untouched(app):
    business_app.redis_client.set(HEARTBEAT_KEY, "not-a-number")

    pm._refresh_celery_heartbeat_gauge()

    assert _gauge() == -1


def test_metrics_scrape_refreshes_the_gauge(client):
    business_app.redis_client.set(HEARTBEAT_KEY, 1790000123.0)

    client.get("/metrics")

    assert _gauge() == 1790000123.0
