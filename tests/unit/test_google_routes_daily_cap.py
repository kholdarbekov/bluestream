"""The daily cap on Google Routes calls (owner rule 2026-11-03, spec §4.12a).

Google's paid Routes API serves ONE thing: the customer's "driver is on the way" estimate. It is free for the first
5,000 calls a month, so every call goes through `google_routes.get_traffic_aware_leg`, which counts it against the
Tashkent day's cap (`GOOGLE_ROUTES_DAILY_CAP`, 150) in Redis before it sends the request: the 151st call of a day is
not made.

Faked, as external I/O only: Redis (`FakeCallCounterRedis`, so no test writes to the Redis DBs the suite shares) and
the Google HTTP request (`google_routes.request_with_retry`). The clock is frozen on `local_windows.local_now`, the
seam `local_date()` reads.
"""

import logging
from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import redis

from business_app.utils import google_routes as gr
from business_app.utils import local_windows
from shared import business_config
from shared.constants import DISPLAY_TIMEZONE
from shared.redis_keyspace import KEYSPACE_TIERS, RedisKeyspace, RedisUsageTier

pytestmark = pytest.mark.unit

ORIGIN, DESTINATION = (41.30, 69.25), (41.32, 69.27)
A_LEG = {"duration_minutes": 9.0, "distance_km": 4.2}
DAY_CAP = 150

# 17:00 in Tashkent on 2026-11-03: the UTC date and the Tashkent date agree.
NOON_UTC = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
TODAY_KEY = "google_routes:calls:2026-11-03"
TOMORROW_KEY = "google_routes:calls:2026-11-04"
TWO_DAYS = 2 * 24 * 60 * 60


class FakeCallCounterRedis:
    """What the cap sends Redis: one MULTI of `INCR key` then `EXPIRE key seconds NX`, and nothing more.

    `counts` holds each key's counter and `ttls` the expiry it was given (NX: set once, never moved). `error` is
    raised when the transaction runs, the way redis-py raises when the server is down; `transactions` counts the
    times one was run. A test module imports this class to keep its own tests off the shared Redis.
    """

    def __init__(self, counts=None, error=None):
        self.counts = dict(counts or {})
        self.ttls = {}
        self.error = error
        self.transactions = 0

    def pipeline(self, transaction=True):
        return _FakeTransaction(self)


class _FakeTransaction:
    def __init__(self, server):
        self._server = server
        self._commands = []

    def incr(self, key):
        self._commands.append(("incr", key))
        return self

    def expire(self, key, seconds, nx=False):
        self._commands.append(("expire", key, seconds, nx))
        return self

    def execute(self):
        server = self._server
        server.transactions += 1
        if server.error is not None:
            raise server.error
        replies = []
        for command, key, *rest in self._commands:
            if command == "incr":
                server.counts[key] = server.counts.get(key, 0) + 1
                replies.append(server.counts[key])
            else:
                seconds, nx = rest
                applies = not (nx and key in server.ttls)
                if applies:
                    server.ttls[key] = seconds
                replies.append(applies)
        return replies


class FakeGoogle:
    """`google_routes.request_with_retry`: records every request it is sent and answers a 9-minute, 4.2 km leg."""

    def __init__(self):
        self.requests = []

    def __call__(self, **kwargs):
        self.requests.append(kwargs)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"routes": [{"duration": "540s", "distanceMeters": 4200}]}
        response.text = "ok"
        return response


def freeze_clock(monkeypatch, instant_utc):
    """The wall clock `local_date()` reads, frozen at a UTC instant, as the Tashkent time it would be."""
    local = instant_utc.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
    monkeypatch.setattr(local_windows, "local_now", lambda: local)


def _warnings_from(caplog):
    """The WARNING lines `google_routes` itself logged."""
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == gr.logger.name]


@pytest.fixture
def counter(monkeypatch):
    """The day's counter store: an empty fake, in place of the app's Redis client."""
    fake = FakeCallCounterRedis()
    monkeypatch.setattr(gr, "redis_client", fake)
    return fake


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(gr, "request_with_retry", fake)
    return fake


@pytest.fixture
def configured_and_noon(app, monkeypatch):
    """A Google key, the cap set to 150, and a clock at 2026-11-03 17:00 Tashkent."""
    monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
    monkeypatch.setitem(app.config, "GOOGLE_ROUTES_DAILY_CAP", DAY_CAP)
    freeze_clock(monkeypatch, NOON_UTC)


@pytest.fixture
def ask(app):
    """One call, as the caller makes it."""

    def _ask():
        with app.app_context():
            return gr.get_traffic_aware_leg(ORIGIN, DESTINATION)

    return _ask


@pytest.mark.usefixtures("configured_and_noon")
class TestTheDailyCap:
    def test_the_daily_cap_stops_google_calls(self, counter, google, ask):
        counter.counts[TODAY_KEY] = DAY_CAP  # 150 calls made today, cap 150

        assert ask() is None
        assert google.requests == []

    def test_under_the_cap_the_call_is_made_and_counted(self, counter, google, ask):
        assert counter.counts == {}

        assert ask() == A_LEG

        assert len(google.requests) == 1
        assert counter.counts == {TODAY_KEY: 1}
        assert counter.ttls == {TODAY_KEY: TWO_DAYS}  # the key expires, two days after it was created

    def test_the_expiry_is_set_once_on_the_first_call_of_the_day(self, counter, google, ask):
        ask()
        counter.ttls[TODAY_KEY] = 100_000  # as if time had passed since the first call

        ask()

        assert counter.counts == {TODAY_KEY: 2}
        assert counter.ttls == {TODAY_KEY: 100_000}  # a later call does not push the expiry out

    def test_the_call_is_counted_before_it_is_made(self, counter, monkeypatch, ask):
        seen_when_sent = []

        def google_that_looks_at_the_counter(**kwargs):
            seen_when_sent.append(counter.counts.get(TODAY_KEY))
            return FakeGoogle()(**kwargs)

        monkeypatch.setattr(gr, "request_with_retry", google_that_looks_at_the_counter)

        assert ask() == A_LEG
        assert seen_when_sent == [1]

    def test_the_last_call_under_the_cap_is_made(self, counter, google, ask):
        counter.counts[TODAY_KEY] = DAY_CAP - 1  # 149 made: the 150th is the last one allowed

        assert ask() == A_LEG
        assert len(google.requests) == 1

    def test_the_151st_call_of_the_day_is_not_made(self, counter, google, ask):
        answers = [ask() for _ in range(DAY_CAP + 1)]

        assert answers[:DAY_CAP] == [A_LEG] * DAY_CAP
        assert answers[DAY_CAP] is None  # the 151st
        assert len(google.requests) == DAY_CAP  # 150 requests reached Google, the 151st never did

    def test_a_spent_cap_is_logged_not_silent(self, counter, google, ask, caplog):
        """From the day the cap runs out, "Google stopped answering" has to be findable in the logs."""
        counter.counts[TODAY_KEY] = DAY_CAP

        # The app configures this logger with propagate=False, so caplog's root handler never sees it: attach
        # caplog's handler directly (the established pattern, tests/unit/test_distance_matrix.py).
        gr.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger=gr.logger.name):
                ask()
        finally:
            gr.logger.removeHandler(caplog.handler)

        warnings = _warnings_from(caplog)
        assert len(warnings) == 1
        assert "google_routes_daily_cap_reached" in warnings[0] and "calls_today=151" in warnings[0]

    def test_a_cap_of_zero_switches_google_off(self, app, counter, google, monkeypatch, ask):
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_DAILY_CAP", 0)

        assert ask() is None
        assert google.requests == []

    def test_a_new_tashkent_day_starts_a_new_count(self, counter, google, ask, monkeypatch):
        """The day is the Tashkent day, not the UTC day: at 23:59:59 local the old count still rules; one second
        later, at local midnight (19:00Z), the new day starts from zero. The UTC date is still 11-03 at both
        instants, so a UTC-dated key would have refused the second call."""
        counter.counts[TODAY_KEY] = DAY_CAP

        freeze_clock(monkeypatch, datetime(2026, 11, 3, 18, 59, 59, tzinfo=timezone.utc))
        assert ask() is None
        assert google.requests == []

        freeze_clock(monkeypatch, datetime(2026, 11, 3, 19, 0, 0, tzinfo=timezone.utc))
        assert ask() == A_LEG
        assert len(google.requests) == 1
        assert counter.counts[TOMORROW_KEY] == 1

    def test_without_a_key_nothing_is_sent_or_counted(self, app, counter, google, monkeypatch, ask):
        monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)

        assert ask() is None
        assert google.requests == []
        assert counter.transactions == 0  # no key, so no call to count: Redis is not even asked
        assert counter.counts == {}


@pytest.mark.usefixtures("configured_and_noon")
class TestRedisFailsOpen:
    """A cap outage must not silence a working route: the counter is a bill guard, never a gate."""

    @pytest.mark.parametrize(
        "error",
        [
            redis.ConnectionError("Error 111 connecting to redis:6379. Connection refused."),
            redis.TimeoutError("Timeout reading from socket"),
            redis.ResponseError("MISCONF Redis is configured to save RDB snapshots, but is unable to persist"),
        ],
        ids=["down", "timeout", "misconf"],
    )
    def test_a_redis_error_does_not_block_the_call(self, counter, google, ask, error):
        counter.error = error

        assert ask() == A_LEG
        assert counter.transactions == 1  # Redis was asked, and failed: this is not a call that skipped the cap
        assert len(google.requests) == 1

    def test_a_redis_error_is_logged_not_silent(self, counter, google, ask, caplog):
        counter.error = redis.ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

        # The app configures this logger with propagate=False, so caplog's root handler never sees it: attach
        # caplog's handler directly (the established pattern, tests/unit/test_distance_matrix.py).
        gr.logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger=gr.logger.name):
                ask()
        finally:
            gr.logger.removeHandler(caplog.handler)

        warnings = _warnings_from(caplog)
        assert len(warnings) == 1 and "google_routes_cap_unavailable" in warnings[0]


def test_the_cap_defaults_to_150_a_day(app):
    """150 x 31 days = 4,650, inside Google's 5,000-a-month free allowance. Defined once, in business_config; the
    app config only derives it. (No `configured_and_noon` here: it would set the key this test reads.)"""
    assert business_config.GOOGLE_ROUTES_DAILY_CAP == 150
    assert app.config["GOOGLE_ROUTES_DAILY_CAP"] == business_config.GOOGLE_ROUTES_DAILY_CAP


def test_the_counter_key_is_registered_in_the_redis_keyspace():
    """`GET google_routes:calls:<day>` is how an operator reads a day's count, so the key's shape is a contract. It
    fails open (Redis down means the call goes ahead), the failure semantics of the CACHE tier."""
    assert RedisKeyspace.google_routes_calls(date(2026, 11, 3)) == TODAY_KEY
    assert KEYSPACE_TIERS["google_routes_calls"] is RedisUsageTier.TIER_CACHE
