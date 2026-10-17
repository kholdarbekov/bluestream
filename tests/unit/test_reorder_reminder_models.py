"""Reminder tables on the SQLite suite schema (spec 2026-10-09 §10) and the event writer.

SQLite enforces CHECKs and partial unique indexes built from the models, so the shape rules are
visible here too; the Postgres-only proof (named constraints, RESTRICT, the migration's frozen
vocabulary) is tests/integration/test_reorder_reminder_pg_constraints.py.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from business_app.models.order import Order
from business_app.models.reorder_reminder import (
    ACTIVE_EPISODE_STATUSES,
    EPISODE_STATUSES,
    ReorderReminderEpisode,
    ReorderReminderEvent,
)
from business_app.services.reorder_reminders.events import record_event
from shared.enums import LIVE_SUBSCRIPTION_STATUSES, OrderStatus, SubscriptionStatus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 15)


def _order(db, user, number):
    order = Order(
        user_id=user.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        total_amount=Decimal("15000.00"),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _episode(user, anchor, *, is_dry_run=False, **columns):
    values = {
        "user_id": user.id,
        "anchor_order_id": anchor.id,
        "anchor_delivered_at": NOW,
        "supply_order_id": anchor.id,
        "supply_delivered_at": NOW,
        "threshold_days": Decimal("30.0"),
        "due_on": TODAY,
        "status": "open",
        "next_action_on": TODAY,
        "is_dry_run": is_dry_run,
    }
    values.update(columns)
    return ReorderReminderEpisode(**values)


def test_live_subscriptions_are_active_and_paused():
    """§5 E4: one named set; a cancelled, expired or trial subscription does not exclude anyone."""
    assert LIVE_SUBSCRIPTION_STATUSES == frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED})


def test_the_active_statuses_are_the_first_three():
    assert ACTIVE_EPISODE_STATUSES == ("open", "escalated", "suspended")
    assert set(ACTIVE_EPISODE_STATUSES) < set(EPISODE_STATUSES)


def test_an_event_on_an_episode_copies_the_episodes_mode(db, sample_user):
    episode = _episode(sample_user, _order(db, sample_user, "TG_000801_26"), is_dry_run=True)
    db.session.add(episode)
    db.session.commit()

    event = record_event(user_id=sample_user.id, kind="reminder_simulated", episode=episode, step=1, channel="telegram")
    db.session.flush()

    assert (event.episode_id, event.is_dry_run, event.kind, event.step, event.channel) == (
        episode.id,
        True,
        "reminder_simulated",
        1,
        "telegram",
    )


def test_an_event_without_an_episode_is_a_real_customer_action(db, sample_user):
    event = record_event(user_id=sample_user.id, kind="blocked", detail="my_chat_member", payload={"at": NOW.isoformat()})
    db.session.flush()

    assert (event.episode_id, event.is_dry_run, event.detail, event.payload) == (
        None,
        False,
        "my_chat_member",
        {"at": "2026-10-15T05:00:00+00:00"},
    )


def test_an_event_on_a_live_episode_is_a_real_event(db, sample_user):
    episode = _episode(sample_user, _order(db, sample_user, "TG_000806_26"))
    db.session.add(episode)
    db.session.commit()

    event = record_event(user_id=sample_user.id, kind="reminder_sent", episode=episode, step=1, channel="email")
    db.session.commit()

    stored = ReorderReminderEvent.query.filter_by(id=event.id).one()
    assert (stored.episode_id, stored.is_dry_run) == (episode.id, False)


@pytest.mark.parametrize("episode_is_dry_run", [False, True], ids=["live-episode", "dry-run-episode"])
def test_an_event_cannot_contradict_its_episodes_mode(db, sample_user, episode_is_dry_run):
    """The mode is the episode's. Passing the opposite one is a programming error, not a quiet override."""
    episode = _episode(sample_user, _order(db, sample_user, "TG_000807_26"), is_dry_run=episode_is_dry_run)
    db.session.add(episode)
    db.session.commit()

    with pytest.raises(ValueError, match="episode"):
        record_event(user_id=sample_user.id, kind="stop", episode=episode, is_dry_run=not episode_is_dry_run)
    db.session.rollback()
    assert ReorderReminderEvent.query.count() == 0


def test_an_event_may_restate_its_episodes_mode(db, sample_user):
    episode = _episode(sample_user, _order(db, sample_user, "TG_000808_26"), is_dry_run=True)
    db.session.add(episode)
    db.session.commit()

    event = record_event(user_id=sample_user.id, kind="stop", episode=episode, is_dry_run=True)

    assert event.is_dry_run is True


def test_an_event_recorded_without_a_payload_stores_sql_null(db, sample_user):
    """Spec §10 `payload JSON NULL`: JSON `null` would defeat `WHERE payload IS NULL`."""
    event = record_event(user_id=sample_user.id, kind="blocked", detail="my_chat_member")
    db.session.commit()

    stored_as_sql_null = db.session.execute(
        text("SELECT payload IS NULL FROM reorder_reminder_events WHERE id = :id"), {"id": event.id}
    ).scalar()
    assert bool(stored_as_sql_null) is True


def test_a_row_that_omits_the_counters_gets_the_models_server_defaults(db, sample_user):
    """Raw SQL bypasses the Python-side `default=`, so this sees only the model's `server_default`."""
    anchor = _order(db, sample_user, "TG_000809_26")
    db.session.execute(
        text(
            "INSERT INTO reorder_reminder_episodes (user_id, anchor_order_id, anchor_delivered_at, "
            "supply_order_id, supply_delivered_at, threshold_days, due_on, status, next_action_on, "
            "created_at, updated_at) VALUES (:user_id, :order_id, :at, :order_id, :at, 30.0, :today, "
            "'open', :today, :at, :at)"
        ),
        {"user_id": sample_user.id, "order_id": anchor.id, "at": "2026-10-15 05:00:00.000000", "today": "2026-10-15"},
    )
    db.session.commit()

    row = db.session.execute(
        text("SELECT step, reminders_sent, is_dry_run FROM reorder_reminder_episodes WHERE user_id = :id"),
        {"id": sample_user.id},
    ).one()
    assert tuple(row) == (0, 0, 0)


def test_an_unlinked_event_can_never_be_dry_run(db, sample_user):
    with pytest.raises(ValueError, match="never dry-run"):
        record_event(user_id=sample_user.id, kind="stop", is_dry_run=True)


@pytest.mark.parametrize(
    "columns, message",
    [({"kind": "clicked"}, "event kind"), ({"kind": "reminder_sent", "channel": "sms"}, "channel")],
    ids=["unknown-kind", "unknown-channel"],
)
def test_unknown_vocabulary_is_refused_at_the_call_site(db, sample_user, columns, message):
    with pytest.raises(ValueError, match=message):
        record_event(user_id=sample_user.id, **columns)


def test_record_event_never_commits(db, sample_user):
    record_event(user_id=sample_user.id, kind="setting_changed", payload={"enabled": False})

    db.session.rollback()

    assert ReorderReminderEvent.query.count() == 0


def test_an_open_episode_needs_its_next_action_date_on_sqlite_too(db, sample_user):
    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000802_26"), next_action_on=None))

    with pytest.raises(IntegrityError, match="ck_reorder_reminder_episodes_status_matrix"):
        db.session.commit()
    db.session.rollback()


def test_one_active_episode_per_customer_and_mode_on_sqlite_too(db, sample_user):
    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000803_26")))
    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000804_26"), is_dry_run=True))
    db.session.commit()

    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000805_26")))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        db.session.commit()
    db.session.rollback()


@pytest.mark.parametrize(
    "status, columns",
    [
        ("closed", {"closed_at": NOW, "close_reason": "ordered"}),
        ("resolved", {"escalated_at": NOW, "resolved_at": NOW, "outcome": "reordered"}),
    ],
)
def test_a_finished_lapse_does_not_block_the_next_one(db, sample_user, status, columns):
    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000810_26"), status=status, **columns))
    db.session.commit()

    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000811_26")))
    db.session.commit()

    stored = ReorderReminderEpisode.query.filter_by(user_id=sample_user.id).all()
    assert sorted(row.status for row in stored) == sorted([status, "open"])


def test_a_suspended_lapse_still_blocks_the_next_one(db, sample_user):
    """Suspended is ACTIVE: the lapse is paused, not over."""
    db.session.add(
        _episode(
            sample_user,
            _order(db, sample_user, "TG_000812_26"),
            status="suspended",
            suspended_at=NOW,
            suspended_reason="subscription",
        )
    )
    db.session.commit()

    db.session.add(_episode(sample_user, _order(db, sample_user, "TG_000813_26")))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        db.session.commit()
    db.session.rollback()
    assert ReorderReminderEpisode.query.filter_by(user_id=sample_user.id).count() == 1


def test_one_lapse_per_anchor_supply_pair_and_mode_on_sqlite_too(db, sample_user):
    anchor = _order(db, sample_user, "TG_000814_26")
    closed = {"status": "closed", "closed_at": NOW, "close_reason": "ordered", "next_action_on": None}
    db.session.add(_episode(sample_user, anchor, **closed))
    db.session.commit()

    db.session.add(_episode(sample_user, anchor, **closed))
    with pytest.raises(IntegrityError, match="anchor_order_id"):
        db.session.commit()
    db.session.rollback()

    db.session.add(_episode(sample_user, anchor, is_dry_run=True, **closed))
    db.session.commit()
    assert ReorderReminderEpisode.query.filter_by(anchor_order_id=anchor.id).count() == 2
