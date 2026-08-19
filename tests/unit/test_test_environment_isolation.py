"""
Regression: pytest must be hermetically sealed from the running dev stack.

2026-07-08 incident: Click-webhook integration tests published REAL Celery
tasks. ``@shared_task`` resolves through Celery's *current app*; in any pytest
process that never imports ``business_app.tasks.celery_app``, that is Celery's
DEFAULT app, which reads ``CELERY_BROKER_URL`` straight from the environment —
in the docker test runner that was ``.env``'s live broker (redis DB 0). The
dev celery_worker then executed the tasks against the dev DB and the staff bot
broadcast "Yangi buyurtma mavjud!" to every delivery person, once per test run.

These tests pin the containment invariants:
- broker/result transports must be in-memory,
- every backend→bot webhook base URL must be unroutable (RFC 2606 ``.invalid``),
  mirroring the existing BUSINESS_APP_URL treatment in
  scripts/precommit-backend-tests.sh,
- no Redis URL a test can flush may ever point at DB 0 (the live broker).
"""

import importlib.util
import os
import pathlib
from urllib.parse import urlparse

import pytest
from celery import Celery


def _hostname(url: str) -> str:
    return urlparse(url).hostname or ""


def _redis_db(url: str) -> str:
    """Return the db-index segment of a redis URL ('' if absent)."""
    rest = url.split("://", 1)[-1]
    return rest.rsplit("/", 1)[1] if "/" in rest else ""


class TestCeleryBrokerContainment:
    """No Celery app reachable from test code may point at a real broker."""

    def test_celery_broker_env_is_in_memory(self):
        assert os.environ.get("CELERY_BROKER_URL", "").startswith("memory://"), (
            "CELERY_BROKER_URL must be memory:// during tests — Celery's default "
            "app (used by @shared_task when business_app.tasks.celery_app is not "
            "imported) reads it from the environment and would publish real tasks"
        )

    def test_celery_result_backend_env_is_in_memory(self):
        assert os.environ.get("CELERY_RESULT_BACKEND", "").startswith("cache+memory://"), (
            "CELERY_RESULT_BACKEND must be cache+memory:// during tests"
        )

    def test_default_style_celery_app_cannot_reach_a_real_broker(self):
        # A bare Celery() resolves its broker exactly like the default app
        # that backs @shared_task .delay() calls in a process that never
        # imported business_app.tasks.celery_app.
        probe = Celery("isolation-probe", set_as_current=False)
        assert str(probe.conf.broker_url).startswith("memory://"), (
            f"default-style Celery app resolved broker "
            f"{probe.conf.broker_url!r}; test-triggered .delay() would publish "
            f"real tasks the dev celery_worker executes against the dev DB"
        )


class TestOutboundWebhookContainment:
    """Backend→bot webhook base URLs must be unroutable from test processes."""

    def test_staff_bot_webhook_url_is_unroutable(self):
        from business_app.tasks import staff_tasks

        assert _hostname(staff_tasks.STAFF_BOT_WEBHOOK_URL).endswith(".invalid"), (
            f"STAFF_BOT_WEBHOOK_URL={staff_tasks.STAFF_BOT_WEBHOOK_URL!r} is "
            f"routable — eagerly-executed staff tasks would POST to the real "
            f"staff bot and broadcast Telegram messages"
        )

    def test_customer_bot_webhook_url_is_unroutable(self):
        assert _hostname(os.environ.get("BOT_WEBHOOK_URL", "")).endswith(".invalid"), (
            "BOT_WEBHOOK_URL must be an .invalid host during tests — the "
            "config default http://telegram_bot:8080 is reachable on the "
            "compose network"
        )

    def test_business_app_url_is_unroutable(self):
        assert _hostname(os.environ.get("BUSINESS_APP_URL", "")).endswith(".invalid"), (
            "BUSINESS_APP_URL must be an .invalid host during tests so "
            "unmocked bot api_client calls fail fast instead of hitting the "
            "live backend"
        )


class TestRedisFlushContainment:
    """The autouse reset_redis_state fixture flushes REDIS_URL's DB — that DB
    must never be 0, the live broker/cache the compose stack runs on."""

    def test_redis_url_env_never_points_at_db_0(self):
        url = os.environ.get("REDIS_URL", "")
        db = _redis_db(url)
        assert db.isdigit() and int(db) != 0, (
            f"REDIS_URL={url!r} resolves to redis DB 0 (or no explicit DB) — "
            f"reset_redis_state would flushdb() the live broker/cache"
        )

    def test_per_worker_redis_mapping_never_hits_db_0(self, monkeypatch):
        # Load the root conftest by path: tests/ is not a package, so the
        # helper is not importable the normal way.
        conftest_path = pathlib.Path(__file__).parents[1] / "conftest.py"
        spec = importlib.util.spec_from_file_location("bs_root_conftest", conftest_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for worker_num in range(0, 32):
            monkeypatch.setenv("PYTEST_XDIST_WORKER", f"gw{worker_num}")
            mapped = mod._per_worker_redis_url("redis://redis:6379/15")
            db = _redis_db(mapped)
            assert db.isdigit() and int(db) != 0, (
                f"xdist worker gw{worker_num} maps to {mapped!r} — flushing "
                f"redis DB 0 wipes the live broker"
            )


class TestRedisIsolationReachesTheClientTheAppActuallyUses:
    """The per-worker DB mapping is worthless unless application code lands in
    the database ``reset_redis_state`` flushes.

    ``business_app/__init__.py`` builds its module-level ``redis_client`` at
    IMPORT time, straight from ``os.environ['REDIS_URL']`` — and that client,
    not ``app.config['REDIS_URL']``, is what the dispatch geometry cache, the
    rate limiters and the counters read and write. Mapping only the config left
    the two on different databases: every worker's app wrote to one shared DB
    while the autouse flush cleared a per-worker DB nothing used. Cached values
    then outlived their test and leaked between concurrently running workers.

    It surfaced as order-dependent failures in the dispatch geometry tests —
    one test's cached provider response served to another, so a test that had
    mocked the provider to FAIL was answered from cache and saw success.

    NOTE for anyone re-verifying these: ``gw0`` maps to DB ``15 - 0 = 15``,
    which is also the unmapped default, so on that worker the two URLs agree
    even with the bug present and these assertions pass vacuously. Reproduce on
    any other worker:

        docker run ... -e PYTEST_XDIST_WORKER=gw1 <image> pytest <this file>

    which fails both assertions below (config DB 14 vs client DB 15) until the
    mapping is applied to the environment in conftest.
    """

    def test_app_config_and_module_level_client_share_one_database(self, app):
        from business_app import redis_client

        assert _redis_db(app.config["REDIS_URL"]) == str(redis_client.connection_pool.connection_kwargs["db"])

    def test_the_flushed_database_is_the_one_the_client_writes_to(self, app):
        """The invariant stated as the fixture's own contract: whatever
        `reset_redis_state` flushes must be where a write from application code
        lands. Asserted through a real round trip rather than by comparing URLs.
        """
        import redis as redis_lib

        from business_app import redis_client

        redis_client.set("isolation-probe", "written-by-app-code")
        redis_lib.from_url(app.config["REDIS_URL"]).flushdb()

        assert redis_client.get("isolation-probe") is None

    def test_each_xdist_worker_maps_to_its_own_database(self, monkeypatch):
        """Two workers must never share a DB, or one worker's flush wipes
        another's in-flight state and its cached values answer another's reads.
        """
        from tests.conftest import _per_worker_redis_url

        seen = set()
        for worker in ("gw0", "gw1", "gw2", "gw3"):
            monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
            seen.add(_per_worker_redis_url("redis://redis:6379/15"))
        assert len(seen) == 4
