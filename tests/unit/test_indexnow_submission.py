"""The IndexNow submission task and the debounced request that queues it."""

import inspect
import logging
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from business_app.services.public_urls_service import indexnow_urls
from business_app.tasks import seo_tasks
from business_app.tasks.seo_tasks import submit_public_urls
from business_app.utils import indexnow_dispatch
from business_app.utils.indexnow_dispatch import request_submission
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier
from tests.integration.eta_plan_builders import celery_app_module_fixture as celery_app_module  # noqa: F401
from tests.integration.eta_plan_builders import published_fixture as published  # noqa: F401
from tests.integration.indexnow_builders import (  # noqa: F401 -- fixtures
    indexnow_redis_fixture as indexnow_redis,
    submission_requests_fixture as submission_requests,
)
from tests.integration.public_page_builders import offer_skus  # noqa: F401 -- fixture
from tests.integration.redis_fakes import DownRedis
from tests.integration.reorder_reminder_builders import captured_logs
from tests.unit.test_sales_agent_tasks import _spy

MARKER = RedisKeyspace.indexnow_submit_scheduled()


def _on(app, monkeypatch):
    monkeypatch.setitem(app.config, "INDEXNOW_ENABLED", True)
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", "https://aqua-element.uz")


def _retries(monkeypatch) -> list:
    """`submit_public_urls.retry`, replaced by a fake that binds against the real signature and raises Retry."""
    real = inspect.signature(submit_public_urls.retry)
    retries = []

    def fake_retry(*args, **kwargs):
        real.bind(*args, **kwargs)
        retries.append(kwargs)
        raise Retry()

    monkeypatch.setattr(submit_public_urls, "retry", fake_retry)
    return retries


def test_the_task_submits_every_public_url_then_recently_deactivated_products(
    app, db, monkeypatch, offer_skus, indexnow_redis
):
    offer_skus.p10.is_active = False
    db.session.commit()
    _on(app, monkeypatch)
    sent = _spy(monkeypatch, seo_tasks, "submit_urls", result=200)
    gone = [f"https://aqua-element.uz/product/{offer_skus.p10.id}{lang}" for lang in ("", "?lang=ru", "?lang=en")]
    assert not set(gone) & set(indexnow_urls())
    expected = indexnow_urls() + gone  # the retired page in all three languages, as the live pages are listed
    assert submit_public_urls.run() == {"status": 200, "urls": len(expected)}
    assert sent == [((expected,), {})]


@pytest.mark.parametrize("status", [None, 429, 503])
def test_a_retryable_answer_is_retried(app, db, monkeypatch, indexnow_redis, status):
    _on(app, monkeypatch)
    _spy(monkeypatch, seo_tasks, "submit_urls", result=status)
    retries = _retries(monkeypatch)
    with pytest.raises(Retry):
        submit_public_urls.run()
    assert len(retries) == 1 and isinstance(retries[0]["exc"], RuntimeError)
    assert (submit_public_urls.max_retries, submit_public_urls.default_retry_delay) == (3, 600)


@pytest.mark.parametrize("status", [400, 403, 422])
def test_a_permanent_answer_is_logged_and_not_retried(app, db, monkeypatch, indexnow_redis, caplog, status):
    _on(app, monkeypatch)
    _spy(monkeypatch, seo_tasks, "submit_urls", result=status)
    retries = _retries(monkeypatch)
    with captured_logs(caplog, seo_tasks.logger, logging.ERROR):
        result = submit_public_urls.run()
    assert result["status"] == status
    assert retries == []
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_the_task_clears_the_marker_before_it_sends(app, db, monkeypatch, indexnow_redis):
    _on(app, monkeypatch)
    indexnow_redis.set(MARKER, "1", nx=True, ex=120)
    seen = []

    def submit(urls: list) -> int:
        seen.append(dict(indexnow_redis.store))
        return 202

    monkeypatch.setattr(seo_tasks, "submit_urls", submit)
    submit_public_urls.run()
    assert seen == [{}]


def test_switched_off_the_task_clears_the_marker_and_sends_nothing(app, db, monkeypatch, indexnow_redis):
    indexnow_redis.set(MARKER, "1", nx=True, ex=120)
    sent = _spy(monkeypatch, seo_tasks, "submit_urls", result=200)
    assert submit_public_urls.run() == {"skipped": "disabled"}
    assert sent == []
    assert indexnow_redis.store == {}


def test_two_requests_publish_once(indexnow_redis, submission_requests):
    assert (request_submission(), request_submission()) == (True, False)
    assert submission_requests == [60] and indexnow_redis.expiries == {MARKER: 120}


def test_redis_down_still_publishes(monkeypatch, submission_requests):
    monkeypatch.setattr(indexnow_dispatch, "redis_client", DownRedis())
    assert request_submission() is True
    assert submission_requests == [60]


def test_a_failed_publish_clears_the_marker(monkeypatch, indexnow_redis):
    def publish(countdown: int) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(indexnow_dispatch, "_publish_submission", publish)
    assert request_submission() is False
    assert indexnow_redis.store == {}


def test_an_eager_celery_app_publishes_nothing(monkeypatch, indexnow_redis, submission_requests):
    monkeypatch.setattr(
        indexnow_dispatch, "current_celery_app", SimpleNamespace(conf=SimpleNamespace(task_always_eager=True))
    )
    assert request_submission() is False
    assert submission_requests == []
    assert indexnow_redis.store == {}


def test_the_seam_publishes_the_task_with_its_countdown_and_no_expiry(published):  # noqa: F811
    indexnow_dispatch._publish_submission(60)
    assert published == [("seo.indexnow_submit", {"countdown": 60})]


def test_the_marker_key_and_its_tier():
    assert MARKER == "bs:indexnow:scheduled"
    assert KEYSPACE_TIERS["indexnow_submit_scheduled"] is RedisUsageTier.TIER_CACHE


def test_the_task_module_is_in_the_celery_include_list(celery_app_module):  # noqa: F811
    assert "business_app.tasks.seo_tasks" in celery_app_module.celery.conf.include
    assert "seo.indexnow_submit" in celery_app_module.celery.tasks
