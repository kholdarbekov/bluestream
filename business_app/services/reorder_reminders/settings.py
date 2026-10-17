"""The reorder-reminder setting: one store, one writer (spec §8; D5, D7, D20).

`reorder_reminder_settings` holds one row per user, and no row means enabled. A customer's
EFFECTIVE setting is the row with the latest `changed_at` across their linked accounts: last
write wins (D7). Off by the customer is customer-off (D5: no more messages, still escalated once
per lapse); off by an admin is admin-off (the customer is out of scope).

`ReorderReminderSettingsService.set` is the only writer. Every route that switches reminders
(the bot's Stop button and settings toggle, the email unsubscribe, the web switch, the admin Users
page, the worklist `do_not_contact` outcome) calls it, so D20 and the audit trail hold on every
route. D20 precisely (contract amendment 16): a CUSTOMER's off over an existing off is a no-op that
only records the attempt; an ADMIN's off always applies, so it upgrades a customer-off to
admin-off. `EffectiveSetting.to_dict` is the one published shape (contract amendment 15).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence

from sqlalchemy import update

from business_app import db
from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.models.reorder_reminder import (
    SETTING_ACTOR_KINDS,
    SETTING_VIAS,
    SUSPENDABLE_EPISODE_STATUSES,
    ReorderReminderEpisode,
    ReorderReminderSetting,
)
from business_app.models.user import User
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.reorder_reminders.events import record_event_at
from business_app.utils.audit_logger import audit_logger
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc, get_utc_now

logger = logging.getLogger(__name__)

ACTOR_CUSTOMER = "customer"
ACTOR_ADMIN = "admin"

# `setting_changed` details. "off" with a customer actor is what the 30-day "switched off by
# customers" metric counts; "already_off" is a D20 attempt that changed nothing.
DETAIL_ON = "on"
DETAIL_OFF = "off"
DETAIL_ALREADY_OFF = "already_off"

INVALID = "REORDER_REMINDER_SETTING_INVALID"
REASON_REQUIRED = "REASON_REQUIRED"

# The staff shape's one word for the effective setting (final review deferred 507): the answer of
# `customer_off` / `admin_off`, published so no page re-derives it from `enabled` + `changed_by_kind`.
STATE_ON = "on"
STATE_CUSTOMER_OFF = "customer_off"
STATE_ADMIN_OFF = "admin_off"
SETTING_STATES = (STATE_ON, STATE_CUSTOMER_OFF, STATE_ADMIN_OFF)

_CHUNK = 500


@dataclass(frozen=True)
class EffectiveSetting:
    enabled: bool
    changed_by_kind: Optional[str]
    changed_via: Optional[str]
    changed_at: Optional[datetime]
    reason: Optional[str]
    source_user_id: Optional[int]

    @property
    def customer_off(self) -> bool:
        return not self.enabled and self.changed_by_kind == ACTOR_CUSTOMER

    @property
    def admin_off(self) -> bool:
        return not self.enabled and self.changed_by_kind == ACTOR_ADMIN

    @property
    def state(self) -> str:
        """One of SETTING_STATES: `customer_off` and `admin_off` as the two properties above answer,
        else `on` (the reading eligibility gives any other setting)."""
        if self.customer_off:
            return STATE_CUSTOMER_OFF
        if self.admin_off:
            return STATE_ADMIN_OFF
        return STATE_ON

    def to_dict(self, include_reason: bool = False) -> dict:
        """THE published shape (§8 "Publishing"; contract amendment 15): every surface that shows the
        setting (customer preferences, admin Users page, Stop response) serializes it here and
        nowhere else. The reason and the `state` are staff-facing only: the Users page offers its
        *Exclude* action on `state == customer_off` and re-derives nothing (deferred 507). The
        customer shape (`include_reason=False`) is unchanged."""
        data = {
            "enabled": self.enabled,
            "changed_by_kind": self.changed_by_kind,
            "changed_via": self.changed_via,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
        }
        if include_reason:
            data["reason"] = self.reason
            data["state"] = self.state
        return data


DEFAULT_SETTING = EffectiveSetting(
    enabled=True, changed_by_kind=None, changed_via=None, changed_at=None, reason=None, source_user_id=None
)


def _recency(row: ReorderReminderSetting):
    """Latest change first. At equal instants an off row wins, then an admin row, then the higher
    user id, so a tie can never quietly re-enable reminders."""
    return (ensure_utc(row.changed_at), not row.enabled, row.changed_by_kind == ACTOR_ADMIN, row.user_id)


def _newest(rows: Sequence[ReorderReminderSetting]) -> ReorderReminderSetting:
    """THE "which row is newer" rule (D7, last write wins), by `_recency`. The effective setting of
    linked accounts and the row an account merge keeps are both this answer, never a second
    comparison."""
    return max(rows, key=_recency)


def _effective(rows: List[ReorderReminderSetting]) -> EffectiveSetting:
    if not rows:
        return DEFAULT_SETTING
    row = _newest(rows)
    return EffectiveSetting(
        enabled=bool(row.enabled),
        changed_by_kind=row.changed_by_kind,
        changed_via=row.changed_via,
        changed_at=ensure_utc(row.changed_at),
        reason=row.reason,
        source_user_id=row.user_id,
    )


def _suspend_for_admin_off(member_ids: Sequence[int], *, at: datetime, actor_user_id: Optional[int]) -> None:
    """What an admin-off suspends in the same transaction (spec §8): every lapse of the cluster that
    is still open or escalated. A suspended row keeps its reason; resolved and closed rows are history.

    ONE conditional UPDATE (§12: every transition is a conditional update), guarded by the model's
    `SUSPENDABLE_EPISODE_STATUSES`, the guard the daily run's suspend uses too. It moves only the rows
    still suspendable when it runs: a lapse the run closed, or a manager resolved, in the meantime is
    left as it is (Postgres re-checks the WHERE on the committed row), so the status-matrix CHECK can
    never fail the admin's save. `suspended` is recorded for exactly the rows it changed (RETURNING).
    """
    episode = ReorderReminderEpisode
    changed = db.session.execute(
        update(episode)
        .where(episode.user_id.in_(list(member_ids)), episode.status.in_(SUSPENDABLE_EPISODE_STATUSES))
        .values(status="suspended", suspended_at=at, suspended_reason="admin_off", updated_at=at)
        .returning(episode.id)
        .execution_options(synchronize_session="fetch")
    ).all()
    for (episode_id,) in sorted(changed):
        row = db.session.get(episode, episode_id)
        record_event_at(
            at,
            user_id=row.user_id,
            kind="suspended",
            episode=row,
            detail="admin_off",
            actor_user_id=actor_user_id,
        )


class ReorderReminderSettingsService:
    @staticmethod
    def effective_for(member_ids: Sequence[int]) -> EffectiveSetting:
        """The effective setting of one customer (all of its linked account ids)."""
        return ReorderReminderSettingsService.effective_by_key({"_": list(member_ids)})["_"]

    @staticmethod
    def effective_by_key(member_ids_by_key: Mapping[str, Sequence[int]]) -> Dict[str, EffectiveSetting]:
        """`{customer key: effective setting}` in one query per 500 accounts."""
        wanted = sorted({int(user_id) for ids in member_ids_by_key.values() for user_id in ids})
        rows_by_user: Dict[int, ReorderReminderSetting] = {}
        for offset in range(0, len(wanted), _CHUNK):
            chunk = wanted[offset : offset + _CHUNK]
            for row in ReorderReminderSetting.query.filter(ReorderReminderSetting.user_id.in_(chunk)).all():
                rows_by_user[row.user_id] = row
        return {
            key: _effective([rows_by_user[int(user_id)] for user_id in ids if int(user_id) in rows_by_user])
            for key, ids in member_ids_by_key.items()
        }

    @staticmethod
    def set(
        user_id: int,
        enabled: bool,
        *,
        actor_kind: str,
        via: str,
        reason: Optional[str] = None,
        actor_user_id: Optional[int] = None,
        episode_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> EffectiveSetting:
        """Switch reorder reminders for `user_id`'s customer, writing that user's row. Commits.

        D20 (contract amendment 16): a CUSTOMER switching off a customer whose effective setting is
        already off changes no row; it only records the attempt (`setting_changed`, detail
        `already_off`), so a Stop can never turn an admin-off into a customer-off. An ADMIN off
        always applies: over a customer-off it upgrades the customer to admin-off. Switching on
        always applies (D7). An admin write needs a reason, and an admin-off suspends the
        customer's open and escalated lapses in this same transaction. A real write records detail
        `on` or `off`. The audit row follows the commit.
        """
        if not isinstance(enabled, bool):
            raise ValidationError("reorder_reminders_enabled must be a boolean", error_code=INVALID)
        if actor_kind not in SETTING_ACTOR_KINDS:
            raise ValidationError(f"Unknown actor kind {actor_kind!r}", error_code=INVALID)
        if via not in SETTING_VIAS:
            raise ValidationError(f"Unknown route {via!r}", error_code=INVALID)
        reason_text = (reason or "").strip() or None
        if actor_kind == ACTOR_ADMIN and reason_text is None:
            raise ValidationError(
                "A reason is required to change a customer's reorder reminders", error_code=REASON_REQUIRED
            )
        if db.session.get(User, user_id) is None:
            raise NotFoundError("User not found", resource_type="user", resource_id=str(user_id))

        at = ensure_utc(now) if now is not None else get_utc_now()
        member_ids = CustomerLinkService().get_cluster_user_ids(user_id)
        before = ReorderReminderSettingsService.effective_for(member_ids)
        payload = {
            "enabled": enabled,
            "changed_by_kind": actor_kind,
            "changed_via": via,
            "episode_id": episode_id,
            "previous": before.to_dict(),
        }
        if not enabled and actor_kind == ACTOR_CUSTOMER and not before.enabled:
            # D20: a customer's off over any off. An admin's off never takes this branch.
            record_event_at(
                at,
                user_id=user_id,
                kind="setting_changed",
                detail=DETAIL_ALREADY_OFF,
                payload=payload,
                actor_user_id=actor_user_id,
            )
            db.session.commit()
            return before

        row = db.session.get(ReorderReminderSetting, user_id)
        if row is None:
            row = ReorderReminderSetting(user_id=user_id)
            db.session.add(row)
        row.enabled = enabled
        row.changed_by_kind = actor_kind
        row.changed_via = via
        row.reason = reason_text
        row.changed_by_user_id = actor_user_id
        row.changed_at = at
        record_event_at(
            at,
            user_id=user_id,
            kind="setting_changed",
            detail=DETAIL_ON if enabled else DETAIL_OFF,
            payload=payload,
            actor_user_id=actor_user_id,
        )
        if not enabled and actor_kind == ACTOR_ADMIN:
            _suspend_for_admin_off(member_ids, at=at, actor_user_id=actor_user_id)
        db.session.commit()

        after = ReorderReminderSettingsService.effective_for(member_ids)
        audit_logger.log_event(
            event_type=AuditEventType.SETTINGS_CHANGED,
            action="reorder_reminders_setting_changed",
            severity=AuditSeverity.MEDIUM,
            resource_type="user",
            resource_id=str(user_id),
            description=f"Reorder reminders {'enabled' if enabled else 'disabled'} by {actor_kind} via {via}",
            old_values=before.to_dict(include_reason=True),
            new_values=after.to_dict(include_reason=True),
            additional_data={"actor_user_id": actor_user_id, "episode_id": episode_id},
        )
        return after

    @staticmethod
    def merge_rows(primary_id: int, secondary_id: int) -> None:
        """An account merge keeps the NEWER of the two rows on the survivor (spec §8 and §10 Merges).

        "Newer" is `_newest`, the rule `_effective` reads linked accounts by: the later
        `changed_at`; at equal instants an off row, then an admin row, then the higher user id. So
        the survivor keeps exactly the setting that was in effect for the two accounts together.
        When the secondary's row wins, its values are copied onto the survivor's row (created if
        missing); the secondary's row is then deleted, and every `changed_by_user_id` stamp naming
        the secondary moves to the survivor.

        Called by `ReorderReminderActions.on_users_merged` before the secondary user is deleted. No
        commit: the merge owns its transaction. The settings user FK is RESTRICT, so a row left
        behind fails the merge loudly instead of losing a customer's Stop.
        """
        primary = db.session.get(ReorderReminderSetting, primary_id)
        secondary = db.session.get(ReorderReminderSetting, secondary_id)
        if secondary is not None:
            if primary is None or _newest([primary, secondary]) is secondary:
                if primary is None:
                    primary = ReorderReminderSetting(user_id=primary_id)
                    db.session.add(primary)
                primary.enabled = secondary.enabled
                primary.changed_by_kind = secondary.changed_by_kind
                primary.changed_via = secondary.changed_via
                primary.reason = secondary.reason
                primary.changed_by_user_id = secondary.changed_by_user_id
                primary.changed_at = secondary.changed_at
            db.session.delete(secondary)
        if primary is not None and primary.changed_by_user_id == secondary_id:
            primary.changed_by_user_id = primary_id
        db.session.flush()
        ReorderReminderSetting.query.filter(ReorderReminderSetting.changed_by_user_id == secondary_id).update(
            {"changed_by_user_id": primary_id}
        )
