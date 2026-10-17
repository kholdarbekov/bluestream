"""Reorder-reminder request bodies and query strings (spec 2026-10-09 §6, §8, §9.1).

Request payload models only (master amendment 15). The published shape of the setting is
`EffectiveSetting.to_dict` (business_app/services/reorder_reminders/settings.py), which every
surface publishes through, so no second dict is written here.
"""

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, StrictInt, constr, field_validator

from business_app.models.reorder_reminder import ESCALATION_FLAGS, OUTCOMES
from business_app.serializers.sales_serializers import PageQuery, _iso


class StopReorderRemindersPayload(BaseModel):
    """`POST /reorder-reminders/stop`. The episode only links the event; Stop works without one.

    Strict on the id: a client that sends `"77"` or `7.5` has a bug the 400 should show, not a Stop
    that quietly drops its link. Unknown keys are ignored, so a body can never name its own route.
    """

    model_config = ConfigDict(extra="ignore")

    episode_id: Optional[StrictInt] = None


class WorklistQuery(PageQuery):
    """`GET /admin/reorder-reminders/episodes`'s query string, typed once.

    `page`/`per_page` clamp through `PageQuery` (the one home of the clamp). An unknown tab or
    flag is a pydantic error, which `handle_api_exception` answers with 400.
    """

    tab: str = "follow_up"
    flag: Optional[Literal[ESCALATION_FLAGS]] = None
    search: Optional[str] = None

    @field_validator("tab")
    @classmethod
    def _known_tab(cls, value: str) -> str:
        # Imported here so the customer Later/Stop API (Task 16a), which shares this module, does
        # not load the worklist's service graph at import time.
        from business_app.services.reorder_reminders.worklist import TABS

        if value not in TABS:
            raise ValueError(f"unknown worklist tab {value!r}")
        return value


# The outcome note is capped at 2000 characters AFTER its whitespace is stripped (`constr` strips
# before it measures, the house form of "trim, then cap" as in `_PenaltyTypeName`): a longer note is
# REFUSED with a 400, never cut (the Lapsed customers page relies on that refusal), and padding never
# turns a note of exactly 2000 characters into a 400.
_OutcomeNote = constr(strip_whitespace=True, max_length=2000)


class ResolveEpisodePayload(BaseModel):
    """`POST /admin/reorder-reminders/episodes/<id>/resolve`'s body (§9.1): the mandatory outcome
    from the published vocabulary and an optional note. `extra="forbid"`: a stray key is a 400."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal[OUTCOMES]
    note: Optional[_OutcomeNote] = None


def serialize_resolved_episode(episode) -> Dict[str, Any]:
    """The resolve route's answer: what the outcome modal confirms. The page refetches its rows."""
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "outcome": episode.outcome,
        "outcome_note": episode.outcome_note,
        "resolved_at": _iso(episode.resolved_at),
        "resolved_by_user_id": episode.resolved_by_user_id,
    }
