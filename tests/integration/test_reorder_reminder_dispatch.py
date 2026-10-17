"""Reorder-reminder sends are published after the commit that queued them, once (spec §12)."""

from sqlalchemy import text

from business_app.utils import reorder_reminder_dispatch
from business_app.utils.reorder_reminder_dispatch import queue_reminder_send
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `sends_fixture` is the `sends` fixture
    captured_logs,
    sends_fixture,
)


def test_a_queued_send_is_published_once_and_only_after_the_commit(db, sends):
    queue_reminder_send(41, 1)
    queue_reminder_send(41, 1)
    queue_reminder_send(42, 2)
    assert sends == []

    db.session.commit()

    assert sends == [(41, 1), (42, 2)]


def test_a_rolled_back_step_publishes_nothing(db, sends):
    db.session.execute(text("SELECT 1"))
    queue_reminder_send(41, 1)

    db.session.rollback()
    db.session.commit()

    assert sends == []


def test_a_publish_failure_is_logged_and_never_reaches_the_committing_run(db, monkeypatch, caplog):
    def publish(episode_id: int, step: int) -> None:
        raise RuntimeError("broker down")

    monkeypatch.setattr(reorder_reminder_dispatch, "_publish_send", publish)
    queue_reminder_send(41, 1)

    with captured_logs(caplog, reorder_reminder_dispatch.logger):
        db.session.commit()

    assert "episode 41 step 1" in caplog.text and "broker down" in caplog.text
