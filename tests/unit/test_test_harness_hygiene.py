"""The test harness leaves no credential in test output and nothing behind on a Celery task.

`create_app` prints the config it is given ("!!! CREATE_APP CALLED ... config_class: ..."). The test apps were built
from dicts, so every app creation printed REDIS_URL with the Redis password (and the Postgres app its database URL with
the Postgres password) into failing tests' output and logs/test-runs/*.log.

A fixture that patched `delay` on a task, or on its Celery Proxy, left a bound method in the task's own `__dict__`
when monkeypatch put back the value it had read: it outlived the test and shadowed `Task.delay` for every later test
in the xdist worker.
"""

import inspect
from contextlib import contextmanager

import flask_migrate
import pytest
from flask import current_app

from business_app.tasks import notification_tasks, payment_tasks
from tests import conftest as session_fixtures
from tests.integration import conftest as postgres_fixtures
from tests.integration.celery_task_harness import SECRET_TASKS, patch_task, publish_for_real

PASSWORD = "harness-hygiene-sentinel-password"
REDIS_URL = f"redis://:{PASSWORD}@redis:6379/15"
# Port 1 refuses at once: nothing in create_app may wait on a database that is not there
DATABASE_URL = f"postgresql://pytest:{PASSWORD}@127.0.0.1:1/pytest_never_created"
PHONE = "+998901112288"


def _set_up(fixture, **requested):
    """Run the fixture's own code as pytest does for a test that asks for it, up to its `yield`: (value, teardown)."""
    steps = inspect.unwrap(fixture)(**requested)
    return next(steps), steps.close


@contextmanager
def _a_database_never_created(_name, template=None):
    yield DATABASE_URL


def _session_app(monkeypatch):
    """tests/conftest.py `app`, on the Redis REDIS_URL names."""
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    return _set_up(session_fixtures.app)


def _pg_app(monkeypatch):
    """tests/integration/conftest.py `pg_app`, on the database `ephemeral_pg_database` hands it."""
    return _set_up(postgres_fixtures.pg_app, ephemeral_pg_database=DATABASE_URL)


def _migrated_pg_template(monkeypatch):
    """tests/integration/conftest.py `migrated_pg_template`: the app it migrates the template with. Creating that
    database and migrating it are Postgres I/O, faked; the app's creation is not."""
    migrated_with = []

    def _upgrade(**_kwargs):
        migrated_with.append(current_app._get_current_object())

    monkeypatch.setattr(postgres_fixtures, "transient_pg_database", _a_database_never_created)
    monkeypatch.setattr(flask_migrate, "upgrade", _upgrade)
    _template, teardown = _set_up(postgres_fixtures.migrated_pg_template)
    return migrated_with[0], teardown


@pytest.mark.parametrize(
    "set_up, url_setting, url",
    [
        (_session_app, "REDIS_URL", session_fixtures._per_worker_redis_url(REDIS_URL)),
        (_pg_app, "SQLALCHEMY_DATABASE_URI", DATABASE_URL),
        (_migrated_pg_template, "SQLALCHEMY_DATABASE_URI", DATABASE_URL),
    ],
    ids=["app", "pg_app", "migrated_pg_template"],
)
def test_the_fixtures_create_their_apps_printing_no_password(capsys, monkeypatch, set_up, url_setting, url):
    app, teardown = set_up(monkeypatch)
    try:
        printed = capsys.readouterr()
        password_printed = PASSWORD in printed.out + printed.err
        assert not password_printed, "creating the fixture's app printed its password"
        # ...and the app still gets every setting, the password included
        assert app.config[url_setting] == url
        assert app.config["TESTING"] is True and app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False
    finally:
        teardown()


def _without_a_left_behind_delay(monkeypatch, task):
    """Start from a clean task, whatever an earlier test in this worker left on it (put back after the test)."""
    monkeypatch.delitem(vars(task), "delay", raising=False)


def test_patching_a_task_for_one_test_leaves_nothing_on_the_task(monkeypatch):
    proxy = notification_tasks.send_registration_otp_task
    task = proxy._get_current_object()
    _without_a_left_behind_delay(monkeypatch, task)
    sent = []

    with pytest.MonkeyPatch.context() as one_test:
        patch_task(one_test, proxy, "delay", lambda *args: sent.append(args))
        proxy.delay(PHONE, "482913", "ru")

    assert "delay" not in vars(task)
    assert sent == [(PHONE, "482913", "ru")]


def test_publishing_for_real_for_one_test_leaves_nothing_on_the_tasks(monkeypatch):
    tasks = [proxy._get_current_object() for proxy in SECRET_TASKS]
    for task in tasks:
        _without_a_left_behind_delay(monkeypatch, task)

    with pytest.MonkeyPatch.context() as one_test, publish_for_real(one_test):
        pass

    assert [task.name for task in tasks if "delay" in vars(task)] == []


@pytest.mark.parametrize(
    "proxy, args",
    [
        (notification_tasks.send_verification_sms_task, (7, "482913", PHONE)),
        (payment_tasks.handle_payment_webhook, ({"provider": "payme", "headers": {}}, "payme")),
        (payment_tasks.process_payment_webhook, (4242, {"sign_string": "5d41402abc4b2a76"})),
    ],
    ids=["verification sms", "payme webhook", "click webhook"],
)
def test_publishing_for_real_sees_past_a_delay_another_test_left_on_the_task(monkeypatch, proxy, args):
    task = proxy._get_current_object()

    def left_behind(*_args, **_kwargs):
        raise AssertionError("the delay another test left on the task ran instead of Celery's")

    monkeypatch.setitem(vars(task), "delay", left_behind)

    with pytest.MonkeyPatch.context() as one_test, publish_for_real(one_test) as published:
        proxy.delay(*args)
        assert [message.name for message in published.messages()] == [task.name]

    assert vars(task)["delay"] is left_behind  # found there, so put back: cleaning it up is its owner's job
