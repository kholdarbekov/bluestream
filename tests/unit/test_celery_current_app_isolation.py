"""Pin: no test's result depends on which module ran before it in its worker (Celery's per-thread current app).

Celery keeps its *current app* per thread (`celery._state._tls`). Building the business Celery app
(`business_app/tasks/celery_app.py`: `celery = make_celery()`) makes it current on the importing thread
(`Celery.__init__` calls `set_current()` when `set_as_current`, the default). Several unit modules import that module
with `create_app` patched to the session SQLite app, so from then on, on pytest's main thread, every `@shared_task`
called directly resolved through its Proxy to the business app's task. That task's `ContextTask.__call__` pushes a
second app context, so the task body ran in a session of its own, which the context's teardown then removed: what the
task handed back was detached (or the task used another app's database). In a worker that never imported it, the same
call was a plain function call on Celery's default app.

The autouse `celery_default_app_is_current` fixture in `tests/conftest.py` makes Celery's default app current on the
main thread for each test and puts the previous current app back afterwards. These tests pin that:
(a) a test that runs after the business app was made current starts on the default app;
(b) a `@shared_task` called directly on the main thread runs in the test's own session, not through `ContextTask`;
(c) the app that was current before a test is current again after it.

(a) runs as two tests in a fixed order (run this module with `-n0` to keep them on one thread); the second also asks
for the module fixture, so it holds when it runs alone.
"""

import sys
from unittest import mock

import pytest
from celery import Celery, shared_task
from celery import _state as celery_state

from business_app import db as _db
from business_app.models.user import User


@shared_task
def _load_user(user_id):
    """A `@shared_task` like any production one: it reads a row and hands it back."""
    return _db.session.get(User, user_id)


@pytest.fixture(scope="module")
def celery_app_module(app):
    """The business Celery app, imported the way the unit modules do, and made current on the main thread.

    The same shape as `celery_app_module` in tests/unit/test_order_tasks_abandoned.py: module-scoped, so it is set up
    before any function-scoped fixture of the test that first asks for it, and its `create_app` is patched for the one
    call `make_celery()` makes. A module is imported once per process, so the import may already have happened in this
    worker; `set_current()` is what that import does (`Celery.__init__`), so the leak is in place either way.
    """
    previous = celery_state.get_current_app()
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    celery = sys.modules[module_name].celery
    celery.set_current()
    yield celery
    previous.set_current()


def test_1_a_test_that_needs_the_business_celery_app_uses_it_directly(celery_app_module):
    """The module that leaves the business app current, as a beat-schedule test does. It reads the `celery` object
    itself, never "current", so it does not depend on the fixture either way."""
    assert celery_app_module is not celery_state.default_app
    assert celery_app_module.Task.__name__ == "ContextTask"


def test_2_the_next_test_starts_on_the_default_app(celery_app_module):
    """(a) The business app was made current on this thread before this test (by the previous test's module fixture,
    or by this one's when it runs alone); this test starts on Celery's default app."""
    assert celery_state.get_current_app() is celery_state.default_app


def test_a_shared_task_called_directly_runs_in_the_tests_own_session(celery_app_module, monkeypatch, db, sample_user):
    """(b) A direct call on the main thread does not go through the business app's `ContextTask`: the row the task
    hands back is the test's own instance, still in the test's session."""
    context_task = celery_app_module.Task
    through_context_task = []
    original_call = context_task.__call__

    def _recording_call(task, *args, **kwargs):
        through_context_task.append(task.name)
        return original_call(task, *args, **kwargs)

    monkeypatch.setattr(context_task, "__call__", _recording_call)

    loaded = _load_user(sample_user.id)

    assert through_context_task == []
    assert loaded is sample_user
    db.session.commit()
    assert loaded.email == "test@example.com"


def test_the_app_current_before_a_test_is_current_again_after_it():
    """(c) The fixture's body: the default app is current inside it, and the previous app comes back after it, also
    when the test raises."""
    from tests.conftest import celery_default_app_current

    earlier = celery_state.get_current_app()
    marker = Celery("current-app-marker", set_as_current=False)
    marker.set_current()
    try:
        with celery_default_app_current():
            assert celery_state.get_current_app() is celery_state.default_app
        assert celery_state.get_current_app() is marker

        with pytest.raises(RuntimeError, match="the test failed"):
            with celery_default_app_current():
                raise RuntimeError("the test failed")
        assert celery_state.get_current_app() is marker
    finally:
        earlier.set_current()
