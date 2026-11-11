"""The shared-place alert is published only by the outer transaction's real commit (nested-safe, like
payment_confirmation_dispatch: precedent tests/unit/test_post_payment_dispatch_guards.py)."""

import inspect

import pytest
from sqlalchemy import text

from business_app.tasks import customer_link_tasks
from business_app.utils.shared_place_case_dispatch import PENDING_KEY, queue_case_alert


def _celery_apply_async(
    self, args=None, kwargs=None, task_id=None, producer=None, link=None, link_error=None, shadow=None, **options
):
    """celery.app.task.Task.apply_async's signature (celery 5)."""


@pytest.fixture
def published(monkeypatch):
    """Signature-enforcing recorder of the task's apply_async: (args, kwargs, options) per publish."""
    task = customer_link_tasks.post_shared_place_case_alert_task
    # tests/conftest.py replaces Task.apply_async with a (*args, **kwargs) mock, so the real signature is spelled out
    signature = inspect.signature(_celery_apply_async)
    calls = []

    def fake(*args, **kwargs):
        bound = signature.bind(None, *args, **kwargs)
        calls.append((tuple(bound.arguments.get("args") or ()), dict(bound.arguments.get("kwargs") or {}), dict(bound.arguments.get("options") or {})))

    monkeypatch.setattr(task, "apply_async", fake)
    return calls


def _touch(db):
    db.session.execute(text("SELECT 1"))


def test_queue_inside_released_savepoint_publishes_only_on_outer_commit(app, db, published):
    _touch(db)
    with db.session.begin_nested():
        queue_case_alert(41, countdown=900)
    assert published == []
    assert db.session.info[PENDING_KEY] == {41: 900}

    db.session.commit()

    assert published == [((41,), {}, {"countdown": 900})]
    assert PENDING_KEY not in db.session.info


def test_outer_rollback_publishes_nothing(app, db, published):
    _touch(db)
    with db.session.begin_nested():
        queue_case_alert(42, countdown=0)
    db.session.rollback()
    db.session.commit()

    assert published == []
    assert PENDING_KEY not in db.session.info


def test_savepoint_rollback_keeps_an_earlier_park(app, db, published):
    _touch(db)
    queue_case_alert(43, countdown=0)
    try:
        with db.session.begin_nested():
            raise RuntimeError("the step failed")
    except RuntimeError:
        pass
    assert published == []
    db.session.commit()

    assert published == [((43,), {}, {"countdown": 0})]


def test_countdown_is_kept_and_a_case_is_parked_once(app, db, published):
    _touch(db)
    queue_case_alert(44, countdown=900)
    queue_case_alert(44, countdown=900)
    db.session.commit()

    assert published == [((44,), {}, {"countdown": 900})]
