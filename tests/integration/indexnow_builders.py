"""Fixtures of the IndexNow submission: the debounce marker's Redis and the publish seam, both faked. A test module
imports `indexnow_redis_fixture` and `submission_requests_fixture` to use them."""

from typing import List

import pytest

from tests.integration.reorder_reminder_builders import FakeRedis


@pytest.fixture(name="indexnow_redis")
def indexnow_redis_fixture(monkeypatch) -> FakeRedis:
    """The Redis of the queued marker, faked: the suite's lanes share the real Redis DBs."""
    from business_app.utils import indexnow_dispatch

    fake = FakeRedis()
    monkeypatch.setattr(indexnow_dispatch, "redis_client", fake)
    return fake


@pytest.fixture(name="submission_requests")
def submission_requests_fixture(monkeypatch) -> List[int]:
    """The countdown of each submission published through the one seam, `_publish_submission`, in order. Nothing
    reaches Celery."""
    from business_app.utils import indexnow_dispatch

    published: List[int] = []

    def publish(countdown: int) -> None:  # `_publish_submission`'s own signature
        published.append(countdown)

    monkeypatch.setattr(indexnow_dispatch, "_publish_submission", publish)
    return published
