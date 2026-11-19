"""Wrong passwords and wrong sign-up codes are counted so that the count always runs out on its own.

Two attempt counters lock a sign-in, both driven here through the endpoints the pages call: the wrong passwords of an
account or of an identifier that names none (login.js posts {identifier, password} to /api/v1/auth/login), and the
wrong SMS codes of a phone sign-up (register.js posts the code to /api/v1/auth/phone/register/verify). Each mistake is
counted with INCR and restarts the count's window with EXPIRE. Those were two Redis round trips: a worker killed
between them (gunicorn's timeout, an OOM kill) left the mistake counted with no expiry, so it never ran out, and the
next mistake, whenever it came, found one try already spent.

First, what a customer meets is pinned, unchanged by the fix: the limit, the lock, and that every mistake restarts the
window, so the count lasts as long as the lock it sets. Then the worker is killed at every boundary between two Redis
round trips of a wrong attempt, and the counter must never be left without an expiry.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest
import redis
from redis.client import Pipeline, Redis

from shared import business_config
from shared.redis_keyspace import RedisKeyspace

pytestmark = pytest.mark.integration

LOGIN_URL = "/api/v1/auth/login"
INIT_URL = "/api/v1/auth/phone/register/init"
VERIFY_URL = "/api/v1/auth/phone/register/verify"
WEB_HEADERS = {"Content-Type": "application/json", "X-Platform": "web"}

PASSWORD = "TestPassword123!"  # sample_user's (tests/conftest.py)
WRONG = "NotThePassword1!"
UNKNOWN = "nobody@example.com"
NEW_PHONE = "+998901117744"  # nobody's number: a sign-up

LOGIN_LIMIT, LOGIN_LOCK_SECONDS = 3, 600
CODE_LIMIT, CODE_LOCK_SECONDS = business_config.PHONE_OTP_MAX_ATTEMPTS, business_config.PHONE_OTP_LOCKOUT_DURATION


def _redis(app):
    return redis.from_url(app.config["REDIS_URL"])


def _a_full_window(ttl, seconds):
    """A TTL just set to `seconds` (Redis rounds it to the nearest second; the request takes a moment)."""
    return seconds - 5 <= ttl <= seconds


@pytest.fixture
def three_tries(app, monkeypatch):
    """Three wrong passwords lock for ten minutes, as in production's order of magnitude. TestingConfig's one-second
    lock would run out between the requests; AuthService reads both values from the config per request."""
    monkeypatch.setitem(app.config, "MAX_LOGIN_ATTEMPTS", LOGIN_LIMIT)
    monkeypatch.setitem(app.config, "LOCKOUT_DURATION", LOGIN_LOCK_SECONDS)


def _login(app, identifier, password):
    return app.test_client().post(
        LOGIN_URL, headers=WEB_HEADERS, data=json.dumps({"identifier": identifier, "password": password})
    )


def _login_refusal(response):
    return response.status_code, (response.get_json().get("data") or {}).get("error_code")


def _login_keys(sample_user, who):
    """(attempt counter, lock) the wrong passwords of `who` spend: the account's, or the unknown identifier's own."""
    if who == "account":
        return RedisKeyspace.sign_in_attempts(sample_user.id), RedisKeyspace.sign_in_lockout(sample_user.id)
    return RedisKeyspace.sign_in_attempts_unknown(UNKNOWN), RedisKeyspace.sign_in_lockout_unknown(UNKNOWN)


def _identifier(sample_user, who):
    return sample_user.email if who == "account" else UNKNOWN


def _phone_keys():
    phone_hash = hashlib.sha256(NEW_PHONE.encode()).hexdigest()[:16]
    return f"phone_otp_attempts:{phone_hash}", f"phone_otp_lockout:{phone_hash}", f"phone_reg_otp:{phone_hash}"


def _start_phone_sign_up(app):
    """register.js, step 1: the code is sent (the SMS task is the conftest's no-op). Returns the code stored."""
    response = app.test_client().post(INIT_URL, json={"phone": NEW_PHONE, "preferred_language": "ru"})
    assert response.status_code == 200, response.get_json()
    return _redis(app).get(_phone_keys()[2]).decode()


def _verify(app, code):
    """register.js, step 2: the code typed, with the details the first step collected."""
    payload = {"phone": NEW_PHONE, "otp_code": code, "first_name": "New", "last_name": "Customer", "password": PASSWORD}
    return app.test_client().post(VERIFY_URL, headers=WEB_HEADERS, data=json.dumps(payload))


def _another_code(code):
    return "000000" if code != "000000" else "111111"


# ------------------------------------------------------------------------------------- what a customer meets (pins)


@pytest.mark.parametrize("who", ["account", "unknown"])
def test_the_third_wrong_password_locks_and_every_wrong_password_restarts_the_count(
    app, db, sample_user, three_tries, who
):
    r = _redis(app)
    counter, lock = _login_keys(sample_user, who)
    identifier = _identifier(sample_user, who)

    assert _login_refusal(_login(app, identifier, WRONG)) == (401, "INVALID_CREDENTIALS")
    assert r.get(counter) == b"1" and _a_full_window(r.ttl(counter), LOGIN_LOCK_SECONDS)

    r.expire(counter, 30)  # nine and a half minutes later
    assert _login_refusal(_login(app, identifier, WRONG)) == (401, "INVALID_CREDENTIALS")
    assert r.get(counter) == b"2" and _a_full_window(r.ttl(counter), LOGIN_LOCK_SECONDS)  # the window restarted
    assert not r.exists(lock)

    assert _login_refusal(_login(app, identifier, WRONG)) == (401, "INVALID_CREDENTIALS")
    assert r.get(counter) == b"3"
    # The count lasts as long as the lock it set: the lock holds only while the count is at the limit.
    assert _a_full_window(r.ttl(counter), LOGIN_LOCK_SECONDS) and _a_full_window(r.ttl(lock), LOGIN_LOCK_SECONDS)

    assert _login(app, identifier, PASSWORD).status_code == 423


def test_the_fifth_wrong_code_locks_the_number_and_every_wrong_code_restarts_the_count(app, db):
    assert (CODE_LIMIT, CODE_LOCK_SECONDS) == (5, 600)
    r = _redis(app)
    counter, lock, stored_code = _phone_keys()
    code = _start_phone_sign_up(app)

    for spent, remaining in ((1, 4), (2, 3), (3, 2), (4, 1)):
        response = _verify(app, _another_code(code))
        assert response.status_code == 400, response.get_json()
        assert response.get_json()["message"] == f"Invalid verification code. {remaining} attempts remaining."
        assert r.get(counter) == str(spent).encode() and _a_full_window(r.ttl(counter), CODE_LOCK_SECONDS)
        r.expire(counter, 30)  # the next wrong code restarts the window

    response = _verify(app, _another_code(code))
    assert response.status_code == 429, response.get_json()
    assert response.get_json()["message"] == "Too many incorrect attempts. Please request a new code."
    assert r.get(counter) == b"5" and _a_full_window(r.ttl(counter), CODE_LOCK_SECONDS)
    assert _a_full_window(r.ttl(lock), CODE_LOCK_SECONDS) and not r.exists(stored_code)

    locked = _verify(app, code)
    assert locked.status_code == 429 and locked.get_json()["message"].startswith("Too many attempts.")


# ------------------------------------------------------------------------------------- a counter left without expiry


@pytest.mark.parametrize("who", ["account", "unknown"])
def test_a_password_count_left_without_an_expiry_gets_one_at_the_next_wrong_password(
    app, db, sample_user, three_tries, who
):
    r = _redis(app)
    counter, _lock = _login_keys(sample_user, who)
    r.set(counter, 1)  # what a worker killed between INCR and EXPIRE left
    assert r.ttl(counter) == -1

    assert _login_refusal(_login(app, _identifier(sample_user, who), WRONG)) == (401, "INVALID_CREDENTIALS")

    assert r.get(counter) == b"2" and _a_full_window(r.ttl(counter), LOGIN_LOCK_SECONDS)


def test_a_code_count_left_without_an_expiry_gets_one_at_the_next_wrong_code(app, db):
    r = _redis(app)
    counter, _lock, _stored = _phone_keys()
    code = _start_phone_sign_up(app)
    r.set(counter, 1)
    assert r.ttl(counter) == -1

    assert _verify(app, _another_code(code)).status_code == 400

    assert r.get(counter) == b"2" and _a_full_window(r.ttl(counter), CODE_LOCK_SECONDS)


# ------------------------------------------------------------------------------------ the worker killed mid-attempt


class WorkerKilled(BaseException):
    """The worker is killed mid-request: nothing in the app catches it, and nothing it had still to send reaches
    Redis."""


@pytest.fixture
def kill_the_worker(monkeypatch):
    """Kill the worker between two of its Redis round trips.

    A round trip is one `Redis.execute_command` (one command) or one `Pipeline.execute` (a MULTI/EXEC, written to the
    socket whole), so a kill falls where a real one can. `after(n)` lets n round trips through and kills the worker at
    the next one; once killed, every round trip fails. `spare()` disarms. `count(fn)` is how many round trips fn makes.
    """
    state = SimpleNamespace(left=None, made=0)
    real_execute_command, real_pipeline_execute = Redis.execute_command, Pipeline.execute

    def _round_trip():
        if state.left is None:
            return
        if state.left == 0:
            raise WorkerKilled()
        state.left -= 1
        state.made += 1

    def execute_command(self, *args, **options):
        _round_trip()
        return real_execute_command(self, *args, **options)

    def pipeline_execute(self, *args, **kwargs):
        _round_trip()
        return real_pipeline_execute(self, *args, **kwargs)

    monkeypatch.setattr(Redis, "execute_command", execute_command)
    monkeypatch.setattr(Pipeline, "execute", pipeline_execute)

    def after(n):
        state.left, state.made = n, 0

    def spare():
        state.left = None

    def count(fn):
        after(10**6)
        try:
            fn()
        finally:
            spare()
        return state.made

    return SimpleNamespace(after=after, spare=spare, count=count)


def _expiries_at_every_kill_point(app, kill_the_worker, counter, set_up, wrong_attempt):
    """{n: the counter's TTL after the worker was killed past its n-th round trip of `wrong_attempt`}, each from the
    same start (`set_up` on an empty Redis). The last n is past every round trip: the attempt the worker finished."""
    r = _redis(app)
    r.flushdb()
    set_up()
    round_trips = kill_the_worker.count(wrong_attempt)
    assert round_trips > 1

    expiries = {}
    for n in range(round_trips + 1):
        r.flushdb()
        set_up()
        kill_the_worker.after(n)
        try:
            wrong_attempt()
        except WorkerKilled:
            pass
        finally:
            kill_the_worker.spare()
        expiries[n] = r.ttl(counter)
    # Killed before the mistake was counted (no counter yet), and the attempt finished (counted), or it proves nothing.
    assert expiries[0] == -2 and expiries[round_trips] > 0, expiries
    return expiries


@pytest.mark.parametrize("who", ["account", "unknown"])
def test_a_worker_killed_during_a_wrong_password_never_leaves_the_count_without_an_expiry(
    app, db, sample_user, three_tries, kill_the_worker, who
):
    counter, _lock = _login_keys(sample_user, who)

    expiries = _expiries_at_every_kill_point(
        app, kill_the_worker, counter, lambda: None, lambda: _login(app, _identifier(sample_user, who), WRONG)
    )

    assert [n for n, ttl in expiries.items() if ttl == -1] == [], expiries


def test_a_worker_killed_during_a_wrong_code_never_leaves_the_count_without_an_expiry(app, db, kill_the_worker):
    counter, _lock, _stored = _phone_keys()
    code = {}

    def set_up():
        code["sent"] = _start_phone_sign_up(app)

    expiries = _expiries_at_every_kill_point(
        app, kill_the_worker, counter, set_up, lambda: _verify(app, _another_code(code["sent"]))
    )

    assert [n for n, ttl in expiries.items() if ttl == -1] == [], expiries
