"""Postgres-only guarantees of migration b4f1c7e9a2d3 (reorder reminders, spec 2026-10-09 §10, §15.8).

The SQLite suite builds its schema from the models; this file runs the real migration on a
throwaway database and asserts each constraint BY NAME (`_rollback_on_named_integrity`), because a
probe row that breaks two rules would otherwise pass against a schema that lacks the one under
test. Postgres fires CHECKs in name order, which is why each probe below breaks exactly one rule.
"""

import itertools
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from business_app.models.order import Order
from business_app.models.reorder_reminder import (
    ACTIVE_EPISODE_STATUSES,
    CHANNELS,
    CLOSE_REASONS,
    EPISODE_STATUSES,
    ESCALATION_FLAGS,
    EVENT_KINDS,
    OUTCOMES,
    SETTING_ACTOR_KINDS,
    SETTING_VIAS,
    SUSPEND_REASONS,
    ReorderReminderEpisode,
    ReorderReminderEvent,
    ReorderReminderSetting,
)
from business_app.models.user import User
from business_app.services.reorder_reminders.events import record_event
from tests.integration.test_sales_agent_pg_constraints import (
    _constraint_def,
    _index_def,
    _quoted_literals,
    _rollback_on_named_integrity,
)

pytestmark = pytest.mark.integration

EPISODES = "reorder_reminder_episodes"
EVENTS = "reorder_reminder_events"
SETTINGS = "reorder_reminder_settings"
NOW = datetime(2026, 10, 15, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 15)
STATE_COLUMNS = {
    "open": {"next_action_on": TODAY},
    "escalated": {"escalated_at": NOW, "escalation_flag": "no_response"},
    "suspended": {"suspended_at": NOW, "suspended_reason": "subscription"},
    "resolved": {"escalated_at": NOW, "escalation_flag": "no_response", "resolved_at": NOW, "outcome": "reordered"},
    "closed": {"closed_at": NOW, "close_reason": "ordered"},
}
_phones = itertools.count(900000201)


def _user(pg_db):
    user = User(phone=f"+998{next(_phones)}", password_hash="not-a-real-hash", first_name="Kamola")
    pg_db.session.add(user)
    pg_db.session.commit()
    return user


def _order(pg_db, owner):
    order = Order(user_id=owner.id, subtotal=Decimal("15000.00"), total_amount=Decimal("15000.00"))
    pg_db.session.add(order)
    pg_db.session.commit()
    return order


def _episode(member, anchor, supply=None, *, status="open", is_dry_run=False, **columns):
    values = {
        "user_id": member.id,
        "anchor_order_id": anchor.id,
        "anchor_delivered_at": NOW,
        "supply_order_id": (supply or anchor).id,
        "supply_delivered_at": NOW,
        "threshold_days": Decimal("30.0"),
        "due_on": TODAY,
        "status": status,
        "is_dry_run": is_dry_run,
        **STATE_COLUMNS.get(status, {}),
        **columns,
    }
    return ReorderReminderEpisode(**values)


def _commits(pg_db, *rows):
    pg_db.session.add_all(rows)
    pg_db.session.commit()


def _delete_user_is_refused(pg_db, user_id, constraint_name):
    with pytest.raises(IntegrityError) as exc_info:
        pg_db.session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        pg_db.session.flush()
    assert exc_info.value.orig.diag.constraint_name == constraint_name, str(exc_info.value)
    pg_db.session.rollback()
    # Refused means refused: the customer is still there.
    assert pg_db.session.execute(text("SELECT count(*) FROM users WHERE id = :id"), {"id": user_id}).scalar() == 1


@pytest.fixture
def owner(pg_db):
    """Owns the anchor orders, so deleting a MEMBER trips only the reminder FKs, never orders.user_id."""
    return _user(pg_db)


def test_the_tables_and_the_users_column_exist(pg_db):
    names = set(
        pg_db.session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).scalars()
    )
    assert {EPISODES, EVENTS, SETTINGS} <= names
    column = pg_db.session.execute(
        text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'telegram_blocked_at'"
        )
    ).one()
    assert tuple(column) == ("timestamp with time zone", "YES")


@pytest.mark.parametrize(
    "table, name, values",
    [
        (EPISODES, "ck_reorder_reminder_episodes_status", EPISODE_STATUSES),
        (EPISODES, "ck_reorder_reminder_episodes_last_channel", CHANNELS),
        (EPISODES, "ck_reorder_reminder_episodes_escalation_flag", ESCALATION_FLAGS),
        (EPISODES, "ck_reorder_reminder_episodes_suspended_reason", SUSPEND_REASONS),
        (EPISODES, "ck_reorder_reminder_episodes_outcome", OUTCOMES),
        (EPISODES, "ck_reorder_reminder_episodes_close_reason", CLOSE_REASONS),
        (EVENTS, "ck_reorder_reminder_events_kind", EVENT_KINDS),
        (EVENTS, "ck_reorder_reminder_events_channel", CHANNELS),
        (SETTINGS, "ck_reorder_reminder_settings_changed_by_kind", SETTING_ACTOR_KINDS),
        (SETTINGS, "ck_reorder_reminder_settings_changed_via", SETTING_VIAS),
    ],
)
def test_the_migrations_frozen_vocabulary_matches_the_model(pg_db, table, name, values):
    definition = _constraint_def(pg_db, table, name)
    assert definition is not None, f"{name} is missing on {table}"
    assert _quoted_literals(definition) == set(values)


def test_one_active_lapse_per_customer_and_mode_is_a_partial_unique_index(pg_db):
    definition = _index_def(pg_db, "uq_reorder_reminder_episodes_one_active")
    assert definition is not None
    assert definition.startswith("CREATE UNIQUE INDEX uq_reorder_reminder_episodes_one_active")
    assert "(user_id, is_dry_run)" in definition
    assert _quoted_literals(definition) == set(ACTIVE_EPISODE_STATUSES)


@pytest.mark.parametrize(
    "name, columns",
    [
        ("ix_reorder_reminder_episodes_status_mode_next_action", "(status, is_dry_run, next_action_on)"),
        ("ix_reorder_reminder_episodes_status_mode_escalated", "(status, is_dry_run, escalated_at)"),
        ("ix_reorder_reminder_episodes_mode_digested", "(is_dry_run, digested_at)"),
        ("ix_reorder_reminder_events_episode_created", "(episode_id, created_at)"),
        ("ix_reorder_reminder_events_kind_mode_created", "(kind, is_dry_run, created_at)"),
        ("ix_reorder_reminder_events_user_created", "(user_id, created_at)"),
    ],
)
def test_the_worklist_and_metrics_indexes_exist(pg_db, name, columns):
    definition = _index_def(pg_db, name)
    assert definition is not None and columns in definition, definition


@pytest.mark.parametrize("status", EPISODE_STATUSES)
def test_every_status_commits_in_its_own_shape(pg_db, owner, status):
    member = _user(pg_db)
    _commits(pg_db, _episode(member, _order(pg_db, owner), status=status))

    assert ReorderReminderEpisode.query.filter_by(user_id=member.id).one().status == status


def test_a_suspended_lapse_keeps_the_escalation_it_had(pg_db, owner):
    member = _user(pg_db)
    _commits(pg_db, _episode(member, _order(pg_db, owner), status="suspended", escalated_at=NOW, escalation_flag="stopped"))

    stored = ReorderReminderEpisode.query.filter_by(user_id=member.id).one()
    assert (stored.status, stored.suspended_reason, stored.escalation_flag, stored.escalated_at) == (
        "suspended",
        "subscription",
        "stopped",
        NOW,
    )


@pytest.mark.parametrize(
    "status, columns",
    [
        ("open", {"next_action_on": None}),
        ("open", {"closed_at": NOW}),
        ("escalated", {"escalation_flag": None}),
        ("escalated", {"resolved_at": NOW}),
        ("suspended", {"suspended_reason": None}),
        ("suspended", {"closed_at": NOW}),
        ("resolved", {"escalated_at": None}),
        ("resolved", {"outcome": None}),
        ("closed", {"close_reason": None}),
    ],
    ids=[
        "open-without-next-action",
        "open-but-closed",
        "escalated-without-flag",
        "escalated-but-resolved",
        "suspended-without-reason",
        "suspended-but-closed",
        "resolved-never-escalated",
        "resolved-without-outcome",
        "closed-without-reason",
    ],
)
def test_a_row_out_of_its_status_shape_is_refused(pg_db, owner, status, columns):
    member = _user(pg_db)
    _rollback_on_named_integrity(
        pg_db, _episode(member, _order(pg_db, owner), status=status, **columns), "ck_reorder_reminder_episodes_status_matrix"
    )


@pytest.mark.parametrize(
    "status, columns, constraint",
    [
        ("paused", {}, "ck_reorder_reminder_episodes_status"),
        ("open", {"last_channel": "sms"}, "ck_reorder_reminder_episodes_last_channel"),
        ("open", {"step": -1}, "ck_reorder_reminder_episodes_counters"),
        ("escalated", {"escalation_flag": "angry"}, "ck_reorder_reminder_episodes_escalation_flag"),
        ("suspended", {"suspended_reason": "holiday"}, "ck_reorder_reminder_episodes_suspended_reason"),
        ("resolved", {"outcome": "lost"}, "ck_reorder_reminder_episodes_outcome"),
        ("closed", {"close_reason": "expired"}, "ck_reorder_reminder_episodes_close_reason"),
    ],
)
def test_values_outside_the_vocabulary_are_refused(pg_db, owner, status, columns, constraint):
    member = _user(pg_db)
    _rollback_on_named_integrity(pg_db, _episode(member, _order(pg_db, owner), status=status, **columns), constraint)


def test_one_lapse_per_anchor_pair_and_mode(pg_db, owner):
    member = _user(pg_db)
    anchor = _order(pg_db, owner)
    _commits(pg_db, _episode(member, anchor, status="closed"))

    _rollback_on_named_integrity(
        pg_db, _episode(member, anchor, status="closed"), "uq_reorder_reminder_episodes_anchor_supply_mode"
    )
    _commits(pg_db, _episode(member, anchor, status="closed", is_dry_run=True))

    stored = ReorderReminderEpisode.query.filter_by(anchor_order_id=anchor.id).all()
    assert sorted(row.is_dry_run for row in stored) == [False, True]


def test_two_members_sharing_one_supply_anchor_get_two_rows(pg_db, owner):
    """§15.2 (D18): Alice and Bob share the place's newest delivery; their own anchors differ."""
    alice, bob = _user(pg_db), _user(pg_db)
    supply = _order(pg_db, owner)

    _commits(pg_db, _episode(alice, _order(pg_db, owner), supply), _episode(bob, _order(pg_db, owner), supply))

    assert ReorderReminderEpisode.query.filter_by(supply_order_id=supply.id).count() == 2


def test_a_customer_has_at_most_one_active_lapse_per_mode(pg_db, owner):
    member = _user(pg_db)
    _commits(
        pg_db,
        _episode(member, _order(pg_db, owner), status="closed"),
        _episode(member, _order(pg_db, owner), status="resolved"),
        _episode(member, _order(pg_db, owner), status="open"),
    )
    _commits(pg_db, _episode(member, _order(pg_db, owner), status="open", is_dry_run=True))

    for status in ACTIVE_EPISODE_STATUSES:
        _rollback_on_named_integrity(
            pg_db, _episode(member, _order(pg_db, owner), status=status), "uq_reorder_reminder_episodes_one_active"
        )

    stored = ReorderReminderEpisode.query.filter_by(user_id=member.id).all()
    assert sorted((row.is_dry_run, row.status) for row in stored) == [
        (False, "closed"),
        (False, "open"),
        (False, "resolved"),
        (True, "open"),
    ]


def test_deleting_a_customer_with_lapse_history_fails_loudly(pg_db, owner):
    member = _user(pg_db)
    member_id = member.id
    _commits(pg_db, _episode(member, _order(pg_db, owner), status="closed"))

    _delete_user_is_refused(pg_db, member_id, "fk_reorder_reminder_episodes_user_id")


def test_deleting_a_customer_with_reminder_events_fails_loudly(pg_db):
    member = _user(pg_db)
    member_id = member.id
    _commits(pg_db, ReorderReminderEvent(user_id=member_id, kind="blocked", detail="my_chat_member"))

    _delete_user_is_refused(pg_db, member_id, "fk_reorder_reminder_events_user_id")


def test_deleting_a_customer_with_a_reminder_setting_fails_loudly(pg_db):
    member = _user(pg_db)
    member_id = member.id
    _commits(
        pg_db,
        ReorderReminderSetting(
            user_id=member_id, enabled=False, changed_by_kind="customer", changed_via="telegram_button", changed_at=NOW
        ),
    )

    _delete_user_is_refused(pg_db, member_id, "fk_reorder_reminder_settings_user_id")


@pytest.mark.parametrize(
    "columns, constraint",
    [
        ({"kind": "clicked"}, "ck_reorder_reminder_events_kind"),
        ({"channel": "sms"}, "ck_reorder_reminder_events_channel"),
        ({"is_dry_run": True}, "ck_reorder_reminder_events_unlinked_is_real"),
    ],
)
def test_events_refuse_unknown_values_and_unlinked_dry_runs(pg_db, columns, constraint):
    member = _user(pg_db)
    _rollback_on_named_integrity(pg_db, ReorderReminderEvent(**{"user_id": member.id, "kind": "stop", **columns}), constraint)


def test_an_episode_linked_dry_run_event_is_fine(pg_db, owner):
    member = _user(pg_db)
    episode = _episode(member, _order(pg_db, owner), is_dry_run=True)
    _commits(pg_db, episode)

    _commits(pg_db, ReorderReminderEvent(user_id=member.id, episode_id=episode.id, is_dry_run=True, kind="reminder_simulated"))

    stored = ReorderReminderEvent.query.filter_by(user_id=member.id).one()
    assert (stored.episode_id, stored.is_dry_run, stored.kind) == (episode.id, True, "reminder_simulated")


@pytest.mark.parametrize(
    "columns, constraint",
    [
        ({"changed_by_kind": "admin", "changed_via": "admin_ui", "reason": None}, "ck_reorder_reminder_settings_admin_reason"),
        ({"changed_by_kind": "admin", "changed_via": "admin_ui", "reason": "   "}, "ck_reorder_reminder_settings_admin_reason"),
        ({"changed_by_kind": "bot", "changed_via": "telegram_button"}, "ck_reorder_reminder_settings_changed_by_kind"),
        ({"changed_by_kind": "customer", "changed_via": "sms_link"}, "ck_reorder_reminder_settings_changed_via"),
    ],
    ids=["admin-without-reason", "admin-blank-reason", "unknown-actor", "unknown-via"],
)
def test_settings_refuse_an_unexplained_admin_change_and_unknown_sources(pg_db, columns, constraint):
    member = _user(pg_db)
    _rollback_on_named_integrity(
        pg_db, ReorderReminderSetting(user_id=member.id, enabled=False, changed_at=NOW, **columns), constraint
    )


def test_a_customer_turning_reminders_off_needs_no_reason(pg_db):
    member = _user(pg_db)
    _commits(
        pg_db,
        ReorderReminderSetting(
            user_id=member.id, enabled=False, changed_by_kind="customer", changed_via="email_link", changed_at=NOW
        ),
    )

    stored = ReorderReminderSetting.query.filter_by(user_id=member.id).one()
    assert (stored.enabled, stored.changed_by_kind, stored.changed_via, stored.reason, stored.changed_at) == (
        False,
        "customer",
        "email_link",
        None,
        NOW,
    )


def test_an_admin_off_suspends_with_one_conditional_update_on_postgres(pg_db, owner):
    """Final review A2: the admin-off suspend is one `UPDATE ... WHERE status IN (open, escalated)
    RETURNING id` on the real database. It moves the live open lapse and the dry-run escalated one,
    records `suspended` for exactly those two, and leaves the closed lapse closed."""
    from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService

    member = _user(pg_db)
    first, second = _order(pg_db, owner), _order(pg_db, owner)
    live = _episode(member, second)
    simulated = _episode(member, second, status="escalated", is_dry_run=True)
    history = _episode(member, first, status="closed")
    _commits(pg_db, live, simulated, history)

    ReorderReminderSettingsService.set(member.id, False, actor_kind="admin", via="admin_ui", reason="Moved away")

    pg_db.session.expire_all()
    assert [(row.id, row.status, row.suspended_reason) for row in (live, simulated, history)] == [
        (live.id, "suspended", "admin_off"),
        (simulated.id, "suspended", "admin_off"),
        (history.id, "closed", None),
    ]
    events = ReorderReminderEvent.query.filter_by(user_id=member.id, kind="suspended").order_by("episode_id").all()
    assert [(row.episode_id, row.detail, row.is_dry_run) for row in events] == [
        (live.id, "admin_off", False),
        (simulated.id, "admin_off", True),
    ]


def test_a_row_that_omits_the_counters_and_the_mode_gets_the_server_defaults(pg_db, owner):
    """The model's server_default and the migration's must agree: raw SQL sees only the migration's."""
    member = _user(pg_db)
    anchor = _order(pg_db, owner)
    pg_db.session.execute(
        text(
            f"INSERT INTO {EPISODES} (user_id, anchor_order_id, anchor_delivered_at, supply_order_id, "
            "supply_delivered_at, threshold_days, due_on, status, next_action_on, created_at, updated_at) "
            "VALUES (:user_id, :order_id, :at, :order_id, :at, 30.0, :today, 'open', :today, :at, :at)"
        ),
        {"user_id": member.id, "order_id": anchor.id, "at": NOW, "today": TODAY},
    )
    pg_db.session.commit()

    row = pg_db.session.execute(
        text(f"SELECT step, reminders_sent, is_dry_run FROM {EPISODES} WHERE user_id = :id"), {"id": member.id}
    ).one()
    assert tuple(row) == (0, 0, False)


def test_an_event_recorded_without_a_payload_stores_sql_null_not_json_null(pg_db):
    """Spec §10 `payload JSON NULL`: a JSON `null` literal would defeat `WHERE payload IS NULL`."""
    member = _user(pg_db)
    event = record_event(user_id=member.id, kind="blocked", detail="my_chat_member")
    pg_db.session.commit()

    stored_as_sql_null = pg_db.session.execute(
        text(f"SELECT payload IS NULL FROM {EVENTS} WHERE id = :id"), {"id": event.id}
    ).scalar()
    assert stored_as_sql_null is True


def test_an_event_payload_round_trips_as_json(pg_db):
    member = _user(pg_db)
    event = record_event(user_id=member.id, kind="setting_changed", payload={"enabled": False, "n": 2})
    pg_db.session.commit()

    assert ReorderReminderEvent.query.filter_by(id=event.id).one().payload == {"enabled": False, "n": 2}


def test_the_migrated_schema_matches_the_models_one_for_one(pg_db):
    """Nothing else compares models to migrations (the SQLite suite is built from the models).

    Scoped to the three new tables and the one new `users` column, so unrelated drift elsewhere in
    the schema never fails this. Alembic does not compare CHECK constraints: the by-name tests above
    pin those.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    ours = {EPISODES, EVENTS, SETTINGS}

    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return name in ours
        owner = obj.table.name
        if owner == "users":
            return type_ == "column" and name == "telegram_blocked_at"
        return owner in ours

    context = MigrationContext.configure(
        pg_db.session.connection(),
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )

    assert compare_metadata(context, pg_db.metadata) == []
