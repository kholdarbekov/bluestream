"""The reorder-reminder event log's one writer (spec 2026-10-09 §10 `reorder_reminder_events`).

Append-only rows that are also the metrics source. `record_event` only ADDS to the session: the
caller owns the transaction, so an event commits or rolls back with the state change it describes.
"""

from datetime import datetime
from typing import Optional

from business_app import db
from business_app.models.reorder_reminder import (
    CHANNELS,
    EVENT_KINDS,
    ReorderReminderEpisode,
    ReorderReminderEvent,
)


def record_event(
    *,
    user_id: int,
    kind: str,
    episode: Optional[ReorderReminderEpisode] = None,
    is_dry_run: Optional[bool] = None,
    step: Optional[int] = None,
    channel: Optional[str] = None,
    detail: Optional[str] = None,
    payload: Optional[dict] = None,
    actor_user_id: Optional[int] = None,
) -> ReorderReminderEvent:
    """Add one event to the session; never commits.

    `is_dry_run` defaults to the episode's flag, else False. An event takes its episode's mode, so
    passing the opposite one is refused. An event with no episode records a real customer action (a
    setting change, a block, a Stop with no episode) and is never dry-run (§10), so asking for one
    is a programming error. Unknown vocabulary is refused here, at the call site, rather than at a
    later flush.
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"Unknown reorder-reminder event kind: {kind!r}")
    if channel is not None and channel not in CHANNELS:
        raise ValueError(f"Unknown reorder-reminder channel: {channel!r}")
    if episode is not None and is_dry_run is not None and bool(is_dry_run) != bool(episode.is_dry_run):
        raise ValueError("An event takes its episode's mode: is_dry_run contradicts the episode")
    if is_dry_run is None:
        is_dry_run = bool(episode.is_dry_run) if episode is not None else False
    if episode is None and is_dry_run:
        raise ValueError("An event with no episode records a real customer action and is never dry-run")
    event = ReorderReminderEvent(
        user_id=user_id,
        episode=episode,
        is_dry_run=is_dry_run,
        kind=kind,
        step=step,
        channel=channel,
        detail=detail,
        payload=payload,
        actor_user_id=actor_user_id,
    )
    db.session.add(event)
    return event


def record_event_at(at: Optional[datetime], **fields) -> ReorderReminderEvent:
    """`record_event`, stamped with the caller's clock.

    Every writer (the run, the send task, the customer actions) reads `now` once and passes it, so
    an event's `created_at` is the moment the decision was made: the 30-minute re-publish check and
    the "blocked within 3 days of a reminder" metric compare these stamps. None keeps the column
    default (the wall clock).
    """
    event = record_event(**fields)
    if at is not None:
        event.created_at = at
    return event
