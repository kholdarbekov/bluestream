"""A driver's silent route-card refresh is published after the commit that cancelled the stop, once.

Owner, 2026-10-22. The order-cancel cascade queues `(driver_id, delivery_id)`; the after-commit
listener publishes it and the after-rollback listener drops it. Modelled on
test_reorder_reminder_dispatch.py.
"""

import pytest
from sqlalchemy import text

from business_app.utils import route_card_refresh_dispatch
from business_app.utils.route_card_refresh_dispatch import (
    queue_route_card_refresh,
    register_route_card_refresh_dispatch,
)
from tests.integration.reorder_reminder_builders import captured_logs


@pytest.fixture
def refreshes(monkeypatch):
    """Every `(driver_id, delivery_id)` the after-commit listener published, in order; the
    recorder keeps the real publisher's signature, and no task is published."""
    published = []

    def publish(driver_id: int, delivery_id: int) -> None:
        published.append((driver_id, delivery_id))

    monkeypatch.setattr(route_card_refresh_dispatch, "_publish_refresh", publish)
    return published


def test_a_queued_refresh_is_published_once_and_only_after_the_commit(db, refreshes):
    queue_route_card_refresh(7, 70)
    queue_route_card_refresh(7, 70)
    queue_route_card_refresh(8, 80)
    assert refreshes == []

    db.session.commit()

    assert refreshes == [(7, 70), (8, 80)]
    db.session.commit()
    assert refreshes == [(7, 70), (8, 80)], "a published refresh is not published again"


def test_each_cancelled_stop_of_one_driver_is_its_own_refresh(db, refreshes):
    """The push names the stop, so the bot can take back a screen showing exactly that one."""
    queue_route_card_refresh(7, 70)
    queue_route_card_refresh(7, 71)

    db.session.commit()

    assert refreshes == [(7, 70), (7, 71)]


def test_a_rolled_back_cancel_publishes_nothing(db, refreshes):
    db.session.execute(text("SELECT 1"))
    queue_route_card_refresh(7, 70)

    db.session.rollback()
    db.session.commit()

    assert refreshes == []


def test_registering_again_never_publishes_twice(db, refreshes):
    """`create_app` runs once per test app; the listeners must not stack."""
    register_route_card_refresh_dispatch(db)
    register_route_card_refresh_dispatch(db)
    queue_route_card_refresh(7, 70)

    db.session.commit()

    assert refreshes == [(7, 70)]


def test_a_publish_failure_is_logged_and_never_reaches_the_committed_cancel(db, monkeypatch, caplog):
    """The cancel has committed; a broker failure for one refresh is logged, and the next
    refresh is still published."""
    published = []

    def publish(driver_id: int, delivery_id: int) -> None:
        if driver_id == 7:
            raise RuntimeError("broker down")
        published.append((driver_id, delivery_id))

    monkeypatch.setattr(route_card_refresh_dispatch, "_publish_refresh", publish)
    queue_route_card_refresh(7, 70)
    queue_route_card_refresh(8, 80)

    with captured_logs(caplog, route_card_refresh_dispatch.logger):
        db.session.commit()

    assert published == [(8, 80)]
    assert "driver 7" in caplog.text and "delivery 70" in caplog.text and "broker down" in caplog.text
