"""add reorder reminders: lapse episodes, their event log, the customer setting, users.telegram_blocked_at

Revision ID: b4f1c7e9a2d3
Revises: e7b2c4d9a1f6
Create Date: 2026-10-15 12:00:00.000000

Spec: docs/superpowers/specs/2026-10-09-lapsed-customer-reorder-reminders-design.md §10 (v2.2).

Added:
  * reorder_reminder_episodes  one row per lapse, keyed (anchor_order_id, supply_order_id,
                               is_dry_run); at most one ACTIVE (open/escalated/suspended) row per
                               (user_id, is_dry_run); a status-matrix CHECK pins what each status
                               requires and forbids.
  * reorder_reminder_events    append-only log and metrics source; an event with no episode is
                               never dry-run.
  * reorder_reminder_settings  the customer's reminder switch (PK user_id); an admin change needs
                               a non-blank reason.
  * users.telegram_blocked_at  written only by TelegramReachabilityService.

Every users FK that holds a customer is ON DELETE RESTRICT: users are hard-deleted only by merges,
which re-point these rows first, so a missed re-point fails loudly instead of erasing history.
The vocabulary tuples below are FROZEN copies of business_app/models/reorder_reminder.py;
tests/integration/test_reorder_reminder_pg_constraints.py pins them equal to the model's.

Rollback strategy:
    downgrade() drops the three tables (events first) and users.telegram_blocked_at. LOSSY: every
    lapse episode, every reminder event, every customer/admin reminder setting and every recorded
    bot block is discarded. Safe while REORDER_REMINDERS_MODE is off and nothing has been recorded;
    otherwise export the three tables and the column first.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b4f1c7e9a2d3"
down_revision = "e7b2c4d9a1f6"
branch_labels = None
depends_on = None

EPISODES = "reorder_reminder_episodes"
EVENTS = "reorder_reminder_events"
SETTINGS = "reorder_reminder_settings"

# Frozen copies (CONVENTIONS.md): never import the live tuples into a migration.
EPISODE_STATUSES = ("open", "escalated", "suspended", "resolved", "closed")
ACTIVE_EPISODE_STATUSES = ("open", "escalated", "suspended")
ESCALATION_FLAGS = (
    "no_response",
    "no_channel",
    "stopped",
    "blocked",
    "snoozed",
    "replied",
    "stuck_order",
    "long_lapsed",
    "outlet_dormant",
)
SUSPEND_REASONS = ("subscription", "outlet", "admin_off", "no_recipient")
CLOSE_REASONS = ("ordered", "place_resupplied", "superseded")
OUTCOMES = (
    "reordered",
    "away",
    "moved",
    "other_supplier",
    "price",
    "service_issue",
    "unreachable",
    "do_not_contact",
    "other",
)
CHANNELS = ("telegram", "email")
EVENT_KINDS = (
    "opened",
    "reminder_queued",
    "reminder_sent",
    "reminder_failed",
    "reminder_skipped",
    "reminder_simulated",
    "later",
    "stop",
    "replied",
    "escalated",
    "suspended",
    "resumed",
    "resolved",
    "closed",
    "setting_changed",
    "blocked",
    "unblocked",
)
SETTING_ACTOR_KINDS = ("customer", "admin")
SETTING_VIAS = ("telegram_button", "telegram_settings", "email_link", "web_settings", "admin_ui", "worklist")

STATUS_MATRIX = (
    "(status = 'open' AND next_action_on IS NOT NULL AND escalated_at IS NULL AND suspended_at IS NULL "
    "AND resolved_at IS NULL AND closed_at IS NULL) "
    "OR (status = 'escalated' AND escalated_at IS NOT NULL AND escalation_flag IS NOT NULL "
    "AND suspended_at IS NULL AND resolved_at IS NULL AND closed_at IS NULL) "
    "OR (status = 'suspended' AND suspended_at IS NOT NULL AND suspended_reason IS NOT NULL "
    "AND resolved_at IS NULL AND closed_at IS NULL) "
    "OR (status = 'resolved' AND escalated_at IS NOT NULL AND resolved_at IS NOT NULL AND outcome IS NOT NULL) "
    "OR (status = 'closed' AND closed_at IS NOT NULL AND close_reason IS NOT NULL)"
)


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _optional_in(column, values):
    return f"{column} IS NULL OR {column} IN ({_quoted(values)})"


def _user_fk(table, column, *, ondelete, nullable):
    return sa.Column(
        column,
        sa.Integer(),
        sa.ForeignKey("users.id", name=f"fk_{table}_{column}", ondelete=ondelete),
        nullable=nullable,
    )


def _order_fk(table, column):
    return sa.Column(
        column,
        sa.Integer(),
        sa.ForeignKey("orders.id", name=f"fk_{table}_{column}", ondelete="CASCADE"),
        nullable=False,
    )


def _ts(column, nullable=True):
    return sa.Column(column, sa.DateTime(timezone=True), nullable=nullable)


def upgrade():
    op.add_column("users", sa.Column("telegram_blocked_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        EPISODES,
        sa.Column("id", sa.Integer(), primary_key=True),
        _user_fk(EPISODES, "user_id", ondelete="RESTRICT", nullable=False),
        _order_fk(EPISODES, "anchor_order_id"),
        _ts("anchor_delivered_at", nullable=False),
        _order_fk(EPISODES, "supply_order_id"),
        _ts("supply_delivered_at", nullable=False),
        sa.Column("usual_gap_days", sa.SmallInteger(), nullable=True),
        sa.Column("threshold_days", sa.Numeric(5, 1), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("step", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("reminders_sent", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_action_on", sa.Date(), nullable=True),
        _ts("last_reminder_at"),
        sa.Column("last_channel", sa.String(16), nullable=True),
        _ts("escalated_at"),
        sa.Column("escalation_flag", sa.String(16), nullable=True),
        _ts("digested_at"),
        _ts("suspended_at"),
        sa.Column("suspended_reason", sa.String(16), nullable=True),
        _ts("resolved_at"),
        _user_fk(EPISODES, "resolved_by_user_id", ondelete="SET NULL", nullable=True),
        sa.Column("outcome", sa.String(24), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        _ts("closed_at"),
        sa.Column("close_reason", sa.String(16), nullable=True),
        sa.Column("is_dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint(
            "anchor_order_id", "supply_order_id", "is_dry_run", name=f"uq_{EPISODES}_anchor_supply_mode"
        ),
        sa.CheckConstraint(f"status IN ({_quoted(EPISODE_STATUSES)})", name=f"ck_{EPISODES}_status"),
        sa.CheckConstraint(STATUS_MATRIX, name=f"ck_{EPISODES}_status_matrix"),
        sa.CheckConstraint("step >= 0 AND reminders_sent >= 0", name=f"ck_{EPISODES}_counters"),
        sa.CheckConstraint(_optional_in("last_channel", CHANNELS), name=f"ck_{EPISODES}_last_channel"),
        sa.CheckConstraint(_optional_in("escalation_flag", ESCALATION_FLAGS), name=f"ck_{EPISODES}_escalation_flag"),
        sa.CheckConstraint(_optional_in("suspended_reason", SUSPEND_REASONS), name=f"ck_{EPISODES}_suspended_reason"),
        sa.CheckConstraint(_optional_in("outcome", OUTCOMES), name=f"ck_{EPISODES}_outcome"),
        sa.CheckConstraint(_optional_in("close_reason", CLOSE_REASONS), name=f"ck_{EPISODES}_close_reason"),
    )
    active = sa.text(f"status IN ({_quoted(ACTIVE_EPISODE_STATUSES)})")
    op.create_index(
        f"uq_{EPISODES}_one_active",
        EPISODES,
        ["user_id", "is_dry_run"],
        unique=True,
        postgresql_where=active,
        sqlite_where=active,
    )
    op.create_index(f"ix_{EPISODES}_status_mode_next_action", EPISODES, ["status", "is_dry_run", "next_action_on"])
    op.create_index(f"ix_{EPISODES}_status_mode_escalated", EPISODES, ["status", "is_dry_run", "escalated_at"])
    op.create_index(f"ix_{EPISODES}_mode_digested", EPISODES, ["is_dry_run", "digested_at"])

    op.create_table(
        EVENTS,
        sa.Column("id", sa.Integer(), primary_key=True),
        _user_fk(EVENTS, "user_id", ondelete="RESTRICT", nullable=False),
        sa.Column(
            "episode_id",
            sa.Integer(),
            sa.ForeignKey(f"{EPISODES}.id", name=f"fk_{EVENTS}_episode_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("is_dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("step", sa.SmallInteger(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("detail", sa.String(32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        _user_fk(EVENTS, "actor_user_id", ondelete="SET NULL", nullable=True),
        _ts("created_at", nullable=False),
        sa.CheckConstraint(f"kind IN ({_quoted(EVENT_KINDS)})", name=f"ck_{EVENTS}_kind"),
        sa.CheckConstraint(_optional_in("channel", CHANNELS), name=f"ck_{EVENTS}_channel"),
        sa.CheckConstraint("episode_id IS NOT NULL OR NOT is_dry_run", name=f"ck_{EVENTS}_unlinked_is_real"),
    )
    op.create_index(f"ix_{EVENTS}_episode_created", EVENTS, ["episode_id", "created_at"])
    op.create_index(f"ix_{EVENTS}_kind_mode_created", EVENTS, ["kind", "is_dry_run", "created_at"])
    op.create_index(f"ix_{EVENTS}_user_created", EVENTS, ["user_id", "created_at"])

    op.create_table(
        SETTINGS,
        _user_fk(SETTINGS, "user_id", ondelete="RESTRICT", nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("changed_by_kind", sa.String(8), nullable=False),
        sa.Column("changed_via", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _user_fk(SETTINGS, "changed_by_user_id", ondelete="SET NULL", nullable=True),
        _ts("changed_at", nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=f"pk_{SETTINGS}"),
        sa.CheckConstraint(
            f"changed_by_kind IN ({_quoted(SETTING_ACTOR_KINDS)})", name=f"ck_{SETTINGS}_changed_by_kind"
        ),
        sa.CheckConstraint(f"changed_via IN ({_quoted(SETTING_VIAS)})", name=f"ck_{SETTINGS}_changed_via"),
        sa.CheckConstraint(
            "changed_by_kind <> 'admin' OR (reason IS NOT NULL AND TRIM(reason) <> '')",
            name=f"ck_{SETTINGS}_admin_reason",
        ),
    )


def downgrade():
    op.drop_table(EVENTS)
    op.drop_table(EPISODES)
    op.drop_table(SETTINGS)
    op.drop_column("users", "telegram_blocked_at")
