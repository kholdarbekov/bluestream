"""Reorder reminders: lapse episodes, their event log and the customer's reminder setting.

Spec: docs/superpowers/specs/2026-10-09-lapsed-customer-reorder-reminders-design.md §10 (v2.2).
Every constraint is named and mirrors migration b4f1c7e9a2d3 one for one. The vocabulary CHECKs
are built with `_quoted` over the tuples below, so the model and the services offer the same
values; the migration carries frozen copies, pinned equal to these by
tests/integration/test_reorder_reminder_pg_constraints.py.

- An episode is one lapse: `(anchor_order_id, supply_order_id, is_dry_run)` is its identity (§4
  "Lapse episode"), and a customer has at most one ACTIVE (open/escalated/suspended) episode per
  mode. `ck_reorder_reminder_episodes_status_matrix` pins what each status requires and forbids;
  it is named after `..._status` so Postgres, which fires CHECKs in name order, reports a bogus
  status as the vocabulary error rather than as a shape error.
- Every `user_id` FK is ON DELETE RESTRICT: users are hard-deleted only by merges, which re-point
  these rows first (`ReorderReminderActions.on_users_merged`), so a missed re-point fails loudly
  instead of erasing lapse history.
- Events are append-only (`created_at` only) and are the metrics source. An event with no episode
  records a real customer action (a setting change, a block) and is never dry-run.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    and_,
    false,
    or_,
    text,
)

from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.sales import _quoted
from business_app.utils.timezone_utils import get_utc_now

EPISODE_STATUSES = ("open", "escalated", "suspended", "resolved", "closed")
# The statuses of a lapse still in progress; at most one per (customer, mode).
ACTIVE_EPISODE_STATUSES = ("open", "escalated", "suspended")
# The active statuses a lapse can be suspended FROM (§6): every one but `suspended` itself. The ONE
# spelling: the daily run's suspend and the admin-off suspend both guard their UPDATE with it.
SUSPENDABLE_EPISODE_STATUSES = tuple(status for status in ACTIVE_EPISODE_STATUSES if status != "suspended")
# The outlet path's flag (D21): the run, the worklist and the digest name it here, once.
FLAG_OUTLET_DORMANT = "outlet_dormant"
ESCALATION_FLAGS = (
    "no_response",
    "no_channel",
    "stopped",
    "blocked",
    "snoozed",
    "replied",
    "stuck_order",
    "long_lapsed",
    FLAG_OUTLET_DORMANT,
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
# §8 writer table: the routes a customer switches reminders through (Stop, the bot and web toggles,
# the email unsubscribe); the other two are staff routes.
CUSTOMER_SETTING_VIAS = ("telegram_button", "telegram_settings", "email_link", "web_settings")
SETTING_VIAS = CUSTOMER_SETTING_VIAS + ("admin_ui", "worklist")

_EPISODES = "reorder_reminder_episodes"
_EVENTS = "reorder_reminder_events"
_SETTINGS = "reorder_reminder_settings"

# §10 state matrix: what each status requires and forbids. A suspended row may keep the
# escalation it had (a suspended-escalated row is still resolvable from the worklist).
_STATUS_MATRIX = (
    "(status = 'open' AND next_action_on IS NOT NULL AND escalated_at IS NULL AND suspended_at IS NULL "
    "AND resolved_at IS NULL AND closed_at IS NULL) "
    "OR (status = 'escalated' AND escalated_at IS NOT NULL AND escalation_flag IS NOT NULL "
    "AND suspended_at IS NULL AND resolved_at IS NULL AND closed_at IS NULL) "
    "OR (status = 'suspended' AND suspended_at IS NOT NULL AND suspended_reason IS NOT NULL "
    "AND resolved_at IS NULL AND closed_at IS NULL) "
    "OR (status = 'resolved' AND escalated_at IS NOT NULL AND resolved_at IS NOT NULL AND outcome IS NOT NULL) "
    "OR (status = 'closed' AND closed_at IS NOT NULL AND close_reason IS NOT NULL)"
)


def _optional_in(column, values):
    return f"{column} IS NULL OR {column} IN ({_quoted(values)})"


def _user_fk(table, column, *, ondelete):
    return ForeignKey("users.id", name=f"fk_{table}_{column}", ondelete=ondelete)


class ReorderReminderEpisode(db.Model, TimestampMixin):
    """One lapse of one customer: its reminder ladder and, if needed, its escalation."""

    __tablename__ = _EPISODES
    __table_args__ = (
        UniqueConstraint(
            "anchor_order_id", "supply_order_id", "is_dry_run", name="uq_reorder_reminder_episodes_anchor_supply_mode"
        ),
        Index(
            "uq_reorder_reminder_episodes_one_active",
            "user_id",
            "is_dry_run",
            unique=True,
            postgresql_where=text(f"status IN ({_quoted(ACTIVE_EPISODE_STATUSES)})"),
            sqlite_where=text(f"status IN ({_quoted(ACTIVE_EPISODE_STATUSES)})"),
        ),
        CheckConstraint(f"status IN ({_quoted(EPISODE_STATUSES)})", name="ck_reorder_reminder_episodes_status"),
        CheckConstraint(_STATUS_MATRIX, name="ck_reorder_reminder_episodes_status_matrix"),
        CheckConstraint("step >= 0 AND reminders_sent >= 0", name="ck_reorder_reminder_episodes_counters"),
        CheckConstraint(_optional_in("last_channel", CHANNELS), name="ck_reorder_reminder_episodes_last_channel"),
        CheckConstraint(
            _optional_in("escalation_flag", ESCALATION_FLAGS), name="ck_reorder_reminder_episodes_escalation_flag"
        ),
        CheckConstraint(
            _optional_in("suspended_reason", SUSPEND_REASONS), name="ck_reorder_reminder_episodes_suspended_reason"
        ),
        CheckConstraint(_optional_in("outcome", OUTCOMES), name="ck_reorder_reminder_episodes_outcome"),
        CheckConstraint(_optional_in("close_reason", CLOSE_REASONS), name="ck_reorder_reminder_episodes_close_reason"),
        Index("ix_reorder_reminder_episodes_status_mode_next_action", "status", "is_dry_run", "next_action_on"),
        Index("ix_reorder_reminder_episodes_status_mode_escalated", "status", "is_dry_run", "escalated_at"),
        Index("ix_reorder_reminder_episodes_mode_digested", "is_dry_run", "digested_at"),
    )

    id = Column(Integer, primary_key=True)
    # The CURRENT recipient; re-pointed by reconciliation and merges.
    user_id = Column(Integer, _user_fk(_EPISODES, "user_id", ondelete="RESTRICT"), nullable=False)
    anchor_order_id = Column(
        Integer,
        ForeignKey("orders.id", name="fk_reorder_reminder_episodes_anchor_order_id", ondelete="CASCADE"),
        nullable=False,
    )
    anchor_delivered_at = Column(DateTime(timezone=True), nullable=False)
    supply_order_id = Column(
        Integer,
        ForeignKey("orders.id", name="fk_reorder_reminder_episodes_supply_order_id", ondelete="CASCADE"),
        nullable=False,
    )
    supply_delivered_at = Column(DateTime(timezone=True), nullable=False)
    usual_gap_days = Column(SmallInteger, nullable=True)  # NULL = the preset applied
    threshold_days = Column(Numeric(5, 1), nullable=False)
    due_on = Column(Date, nullable=False)
    status = Column(String(16), nullable=False)
    step = Column(SmallInteger, nullable=False, default=0, server_default=text("0"))
    reminders_sent = Column(SmallInteger, nullable=False, default=0, server_default=text("0"))
    next_action_on = Column(Date, nullable=True)
    last_reminder_at = Column(DateTime(timezone=True), nullable=True)
    last_channel = Column(String(16), nullable=True)
    escalated_at = Column(DateTime(timezone=True), nullable=True)
    escalation_flag = Column(String(16), nullable=True)
    digested_at = Column(DateTime(timezone=True), nullable=True)
    suspended_at = Column(DateTime(timezone=True), nullable=True)
    suspended_reason = Column(String(16), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id = Column(
        Integer, _user_fk(_EPISODES, "resolved_by_user_id", ondelete="SET NULL"), nullable=True
    )
    outcome = Column(String(24), nullable=True)
    outcome_note = Column(Text, nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_reason = Column(String(16), nullable=True)
    is_dry_run = Column(Boolean, nullable=False, default=False, server_default=false())

    def __repr__(self):
        return f"<ReorderReminderEpisode {self.id} user={self.user_id} {self.status} dry_run={self.is_dry_run}>"


def is_resolvable(episode: ReorderReminderEpisode) -> bool:
    """Can staff resolve this lapse now (spec §6 Resolve; contract amendment 4)?

    `escalated`, or `suspended` when it had been escalated first. The ONE expression of the rule,
    in two forms: this predicate (the worklist's `can_resolve`) and its SQL twin
    `resolvable_clause()` (the guard of `ReorderReminderActions.resolve`'s conditional UPDATE).
    tests/integration/test_reorder_reminder_actions.py pins that the two agree on every state.
    """
    return episode.status == "escalated" or (episode.status == "suspended" and episode.escalated_at is not None)


def resolvable_clause():
    """`is_resolvable` as a SQL condition on `reorder_reminder_episodes`."""
    return or_(
        ReorderReminderEpisode.status == "escalated",
        and_(ReorderReminderEpisode.status == "suspended", ReorderReminderEpisode.escalated_at.isnot(None)),
    )


class ReorderReminderEvent(db.Model):
    """One thing that happened to a lapse or to a customer's reachability. Append-only."""

    __tablename__ = _EVENTS
    __table_args__ = (
        CheckConstraint(f"kind IN ({_quoted(EVENT_KINDS)})", name="ck_reorder_reminder_events_kind"),
        CheckConstraint(_optional_in("channel", CHANNELS), name="ck_reorder_reminder_events_channel"),
        CheckConstraint("episode_id IS NOT NULL OR NOT is_dry_run", name="ck_reorder_reminder_events_unlinked_is_real"),
        Index("ix_reorder_reminder_events_episode_created", "episode_id", "created_at"),
        Index("ix_reorder_reminder_events_kind_mode_created", "kind", "is_dry_run", "created_at"),
        Index("ix_reorder_reminder_events_user_created", "user_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, _user_fk(_EVENTS, "user_id", ondelete="RESTRICT"), nullable=False)
    episode_id = Column(
        Integer,
        ForeignKey(f"{_EPISODES}.id", name="fk_reorder_reminder_events_episode_id", ondelete="CASCADE"),
        nullable=True,
    )
    is_dry_run = Column(Boolean, nullable=False, default=False, server_default=false())
    kind = Column(String(24), nullable=False)
    step = Column(SmallInteger, nullable=True)
    channel = Column(String(16), nullable=True)
    detail = Column(String(32), nullable=True)
    # none_as_null: an event with no payload is SQL NULL (spec §10), not the JSON literal `null`.
    payload = Column(JSON(none_as_null=True), nullable=True)
    actor_user_id = Column(Integer, _user_fk(_EVENTS, "actor_user_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=get_utc_now)

    # Lookup only: no backref, no cascade beyond the FK's own.
    episode = db.relationship("ReorderReminderEpisode")

    def __repr__(self):
        return f"<ReorderReminderEvent {self.id} {self.kind} user={self.user_id} episode={self.episode_id}>"


class ReorderReminderSetting(db.Model):
    """The customer's reorder-reminder switch: last change wins (D7), admin-off needs a reason."""

    __tablename__ = _SETTINGS
    __table_args__ = (
        CheckConstraint(
            f"changed_by_kind IN ({_quoted(SETTING_ACTOR_KINDS)})", name="ck_reorder_reminder_settings_changed_by_kind"
        ),
        CheckConstraint(f"changed_via IN ({_quoted(SETTING_VIAS)})", name="ck_reorder_reminder_settings_changed_via"),
        CheckConstraint(
            "changed_by_kind <> 'admin' OR (reason IS NOT NULL AND TRIM(reason) <> '')",
            name="ck_reorder_reminder_settings_admin_reason",
        ),
    )

    user_id = Column(Integer, _user_fk(_SETTINGS, "user_id", ondelete="RESTRICT"), primary_key=True)
    enabled = Column(Boolean, nullable=False)
    changed_by_kind = Column(String(8), nullable=False)
    changed_via = Column(String(20), nullable=False)
    reason = Column(Text, nullable=True)
    changed_by_user_id = Column(Integer, _user_fk(_SETTINGS, "changed_by_user_id", ondelete="SET NULL"), nullable=True)
    changed_at = Column(DateTime(timezone=True), nullable=False)

    def __repr__(self):
        return f"<ReorderReminderSetting user={self.user_id} enabled={self.enabled} by={self.changed_by_kind}>"
