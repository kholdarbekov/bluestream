"""What a customer or a staff member does to a lapse (spec §6 Customer actions and Resolve, §8, §10).

* `snooze`            — *Later* on reminder 1 (D13);
* `stop`              — *Stop these reminders*, in any state, through the one setting writer (D5, D20);
* `on_customer_reply` — any inbound support message while a lapse is open escalates it (D14);
* `resolve`           — staff record the outcome; `do_not_contact` on a live row writes admin-off,
                        which upgrades a customer-off (contract amendment 16);
* `on_users_merged`   — an account merge keeps settings, episodes and events (§10 Merges).

Every action takes `now`, so it shares its caller's clock, and stamps its events with it
(`record_event_at`). Customer actions act only on the caller's own customer: a lapse is reached
through the caller's linked accounts, never by id alone. The actions commit themselves (they are
never wrapped in `@transactional`), except `on_users_merged`, which runs inside the merge's
transaction.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from business_app import db
from business_app.models.reorder_reminder import (
    ACTIVE_EPISODE_STATUSES,
    CUSTOMER_SETTING_VIAS,
    OUTCOMES,
    ReorderReminderEpisode,
    ReorderReminderEvent,
    resolvable_clause,
)
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.reorder_reminders.config import MODE_OFF, current_config
from business_app.services.reorder_reminders.events import record_event_at
from business_app.services.reorder_reminders.settings import (
    ACTOR_ADMIN,
    ACTOR_CUSTOMER,
    INVALID,
    EffectiveSetting,
    ReorderReminderSettingsService,
)
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc, get_utc_now

logger = logging.getLogger(__name__)

Episode = ReorderReminderEpisode
Event = ReorderReminderEvent

NOT_ACTIVE = "REORDER_REMINDER_NOT_ACTIVE"
NOT_RESOLVABLE = "REORDER_REMINDER_NOT_RESOLVABLE"
OUTCOME_INVALID = "REORDER_REMINDER_OUTCOME_INVALID"
DO_NOT_CONTACT = "do_not_contact"


def _clock(now: Optional[datetime]) -> datetime:
    return ensure_utc(now) if now is not None else get_utc_now()


def _own_live_episode(user_id: int, episode_id: int) -> Optional[Episode]:
    """The caller's live (not dry-run) episode, or None. Another customer's lapse reads as missing,
    so an id leaks nothing."""
    episode = db.session.get(Episode, episode_id)
    if episode is None or episode.is_dry_run:
        return None
    if episode.user_id not in CustomerLinkService().get_cluster_user_ids(user_id):
        return None
    return episode


def _not_active() -> ValidationError:
    return ValidationError("This reminder is no longer active", error_code=NOT_ACTIVE)


def _do_not_contact_reason(note: Optional[str]) -> str:
    return f"Worklist outcome: do_not_contact — {note}" if note else "Worklist outcome: do_not_contact"


def _close_superseded_on_merge(primary_id: int, secondary_id: int, is_dry_run: bool, at: datetime) -> None:
    """Both accounts' orders now belong to the survivor, so of two active lapses only the one with
    the newer anchor is still current: the other closes `superseded` before the re-point, which the
    one-active-per-user index would otherwise refuse."""
    active = Episode.query.filter(
        Episode.user_id.in_((primary_id, secondary_id)),
        Episode.is_dry_run.is_(is_dry_run),
        Episode.status.in_(ACTIVE_EPISODE_STATUSES),
    ).all()
    if len(active) < 2:
        return
    keep = max(active, key=lambda episode: (ensure_utc(episode.anchor_delivered_at), episode.anchor_order_id))
    for episode in active:
        if episode is keep:
            continue
        episode.status = "closed"
        episode.closed_at = at
        episode.close_reason = "superseded"
        episode.next_action_on = None
        episode.updated_at = at
        record_event_at(
            at,
            user_id=episode.user_id,
            kind="closed",
            episode=episode,
            detail="superseded",
            payload={
                "order_id": keep.anchor_order_id,
                "delivered_at": ensure_utc(keep.anchor_delivered_at).isoformat(),
                "merged_user_id": secondary_id,
            },
        )


class ReorderReminderActions:
    @staticmethod
    def snooze(user_id: int, episode_id: int, *, now: Optional[datetime] = None) -> int:
        """*Later* (D13), allowed only while the lapse is open at step 1. `next_action_on` becomes
        the later of itself and today + SNOOZE_DAYS, so no tap moves the ladder backwards. Returns
        the snooze length the confirmation names."""
        at = _clock(now)
        cfg = current_config()
        episode = _own_live_episode(user_id, episode_id)
        if episode is None or episode.status != "open" or episode.step != 1:
            raise _not_active()
        target = max(episode.next_action_on, local_date(at) + timedelta(days=cfg.snooze_days))
        changed = Episode.query.filter(Episode.id == episode.id, Episode.status == "open", Episode.step == 1).update(
            {"next_action_on": target, "updated_at": at}, synchronize_session=False
        )
        db.session.expire(episode)
        if changed != 1:
            db.session.rollback()
            raise _not_active()
        record_event_at(
            at, user_id=user_id, kind="later", episode=episode, step=1, payload={"next_action_on": target.isoformat()}
        )
        db.session.commit()
        return cfg.snooze_days

    @staticmethod
    def stop(
        user_id: int, *, via: str, episode_id: Optional[int] = None, now: Optional[datetime] = None
    ) -> EffectiveSetting:
        """*Stop these reminders*, in any state and even with no lapse (D5). Customer-off goes
        through the one writer, so a Stop never turns admin-off into customer-off (D20). The `stop`
        event links the caller's own live lapse when the button named one. Only a customer route
        (§8 writer table) may carry a Stop; a staff route is refused like any unknown one."""
        if via not in CUSTOMER_SETTING_VIAS:
            raise ValidationError(f"{via!r} is not a customer route", error_code=INVALID)
        at = _clock(now)
        episode = _own_live_episode(user_id, episode_id) if episode_id is not None else None
        record_event_at(
            at,
            user_id=user_id,
            kind="stop",
            episode=episode,
            step=episode.step if episode is not None else None,
            detail=via,
            actor_user_id=user_id,
        )
        return ReorderReminderSettingsService.set(
            user_id,
            False,
            actor_kind=ACTOR_CUSTOMER,
            via=via,
            actor_user_id=user_id,
            episode_id=episode.id if episode is not None else None,
            now=at,
        )

    @staticmethod
    def on_customer_reply(user_id: int, *, excerpt: str, now: Optional[datetime] = None) -> None:
        """D14: any inbound support message, text or not, while the customer's lapse is open or
        escalated records `replied`; an open one also escalates now (`replied`, a direct escalation:
        no date guard). Mode `off` does nothing; the row set is the active one (`dry_run` works on
        the simulated rows, §13). Never raises: support capture must not depend on it.

        `excerpt` is the support inbox's own `build_preview`, already cut to its length, and is
        stored exactly as given: the cut is spelled once, there."""
        try:
            cfg = current_config()
            if cfg.mode == MODE_OFF:
                return
            at = _clock(now)
            member_ids = CustomerLinkService().get_cluster_user_ids(user_id)
            episode = (
                Episode.query.filter(
                    Episode.user_id.in_(member_ids),
                    Episode.is_dry_run.is_(cfg.row_set_is_dry_run),
                    Episode.status.in_(("open", "escalated")),
                )
                .order_by(Episode.id.desc())
                .first()
            )
            if episode is None:
                return
            was_open = episode.status == "open"
            record_event_at(at, user_id=user_id, kind="replied", episode=episode, payload={"excerpt": excerpt})
            if was_open:
                changed = Episode.query.filter(Episode.id == episode.id, Episode.status == "open").update(
                    {
                        "status": "escalated",
                        "escalated_at": at,
                        "escalation_flag": "replied",
                        "next_action_on": None,
                        "updated_at": at,
                    },
                    synchronize_session=False,
                )
                db.session.expire(episode)
                if changed == 1:
                    record_event_at(at, user_id=episode.user_id, kind="escalated", episode=episode, detail="replied")
            db.session.commit()
        except Exception:  # noqa: BLE001 - a reminder failure must never touch support capture
            db.session.rollback()
            logger.exception("reorder_reminders: the reply hook failed for user %s", user_id)

    @staticmethod
    def resolve(
        episode_id: int, *, outcome: str, note: Optional[str], actor_user_id: int, now: Optional[datetime] = None
    ) -> ReorderReminderEpisode:
        """Staff close a follow-up with a mandatory outcome (§6 Resolve) when `is_resolvable`:
        from `escalated`, or from `suspended` when the lapse had been escalated first. The guard is
        `resolvable_clause()`, the predicate's SQL twin (contract amendment 4). A live
        `do_not_contact` then writes admin-off through the one writer, LAST, after the transition,
        and that write commits both; an admin off always applies, so it upgrades a customer-off
        (amendment 16). Dry-run rows never write settings (§13)."""
        if outcome not in OUTCOMES:
            raise ValidationError(f"Unknown outcome {outcome!r}", error_code=OUTCOME_INVALID)
        at = _clock(now)
        episode = db.session.get(Episode, episode_id)
        if episode is None:
            raise NotFoundError(
                "Lapse not found", resource_type="reorder_reminder_episode", resource_id=str(episode_id)
            )
        note_text = (note or "").strip() or None
        changed = Episode.query.filter(Episode.id == episode_id, resolvable_clause()).update(
            {
                "status": "resolved",
                "resolved_at": at,
                "resolved_by_user_id": actor_user_id,
                "outcome": outcome,
                "outcome_note": note_text,
                "suspended_at": None,
                "suspended_reason": None,
                "next_action_on": None,
                "updated_at": at,
            },
            synchronize_session=False,
        )
        db.session.expire(episode)
        if changed != 1:
            db.session.rollback()
            raise ValidationError("Only a lapse waiting for follow-up can be resolved", error_code=NOT_RESOLVABLE)
        record_event_at(
            at,
            user_id=episode.user_id,
            kind="resolved",
            episode=episode,
            detail=outcome,
            payload={"note": note_text},
            actor_user_id=actor_user_id,
        )
        if outcome == DO_NOT_CONTACT and not episode.is_dry_run:
            ReorderReminderSettingsService.set(
                episode.user_id,
                False,
                actor_kind=ACTOR_ADMIN,
                via="worklist",
                reason=_do_not_contact_reason(note_text),
                actor_user_id=actor_user_id,
                episode_id=episode.id,
                now=at,
            )
        else:
            db.session.commit()
        return db.session.get(Episode, episode_id)

    @staticmethod
    def on_users_merged(primary_id: int, secondary_id: int) -> None:
        """§10 Merges, from `_transfer_user_references`, before the secondary user is deleted.

        Every reminder row naming the secondary moves to the survivor: the newer setting
        (`merge_rows`), every episode whatever its status (resolved history survives, so no anchor
        ever opens twice), and every event, the episode-less `setting_changed` / `stop` /
        `blocked` / `unblocked` ones included; then the stamps (`resolved_by_user_id`,
        `actor_user_id`). Per row set, of two active lapses the older anchor's closes `superseded`
        first. No commit: the merge owns its transaction. The reminder tables' user FKs are
        RESTRICT, so a reference left behind fails the merge loudly instead of erasing a Stop or
        lapse history."""
        at = get_utc_now()
        ReorderReminderSettingsService.merge_rows(primary_id, secondary_id)
        for is_dry_run in (False, True):
            _close_superseded_on_merge(primary_id, secondary_id, is_dry_run, at)
        db.session.flush()  # the closes land before the re-point the one-active index checks
        Episode.query.filter(Episode.user_id == secondary_id).update({"user_id": primary_id})
        Episode.query.filter(Episode.resolved_by_user_id == secondary_id).update({"resolved_by_user_id": primary_id})
        Event.query.filter(Event.user_id == secondary_id).update({"user_id": primary_id})
        Event.query.filter(Event.actor_user_id == secondary_id).update({"actor_user_id": primary_id})
        db.session.flush()
