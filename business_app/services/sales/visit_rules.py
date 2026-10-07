"""The verified visit (C4, spec §4.1.1): the ONE rule for "was the agent really in the shop".

A visit is verified when it was closed as `completed`, carried a real check-in, was not
skipped, was measured inside the geofence, and lasted at least the short-visit threshold from
check-in to close. Plan compliance (`AgentDayPlanService.compliance`), the supervisor's
`short_visit` exception, the average visit length and the pay gate all ask these functions;
none of them measures a visit on its own (tests/unit/test_visit_rules.py pins that).

"Verified", not "countable": `replenishment_service.visit_is_countable` already means something
else (a visit whose stock check may feed the consumption rate).

Imports no service, so every service may import it.
"""

from typing import Optional

from flask import current_app

from business_app.utils.timezone_utils import ensure_utc

# In the order `not_verified_reason` checks them, so the drill-down names the FIRST failure.
NOT_VERIFIED_REASONS = ("not_completed", "no_checkin", "skipped", "out_of_range", "no_location", "short")


def short_visit_threshold() -> int:
    """`SALES_SHORT_VISIT_SECONDS` (shared/business_config.py, default 60), read here and nowhere else."""
    return int(current_app.config["SALES_SHORT_VISIT_SECONDS"])


def presence_seconds(visit) -> Optional[int]:
    """Check-in to end, in whole seconds; None without either instant.

    Computed in Python: SQLite, the suite's engine, has no epoch extraction, and it hands a
    timestamp back without its zone, which `ensure_utc` reads as UTC.
    """
    if visit.checkin_at is None or visit.ended_at is None:
        return None
    return int((ensure_utc(visit.ended_at) - ensure_utc(visit.checkin_at)).total_seconds())


def not_verified_reason(visit, *, min_seconds: int) -> Optional[str]:
    """THE rule. None means verified; otherwise the first failing `NOT_VERIFIED_REASONS` member."""
    if visit.status != "completed":
        return "not_completed"
    if visit.checkin_at is None:
        # Also the visit `VisitService.close` accepted without the check-in step.
        return "no_checkin"
    if visit.checkin_skipped:
        return "skipped"
    if visit.in_radius is False:
        return "out_of_range"
    if visit.in_radius is None:
        # Q9 (owner-confirmed): no pin was sent, or the outlet has no coordinates, so presence was
        # never measured.
        return "no_location"
    seconds = presence_seconds(visit)
    if seconds is None or seconds < min_seconds:
        return "short"
    return None


def is_verified_visit(visit, *, min_seconds: int) -> bool:
    """Defined as "no reason", so the reasons a drill-down shows and this answer cannot drift."""
    return not_verified_reason(visit, min_seconds=min_seconds) is None


def is_short_visit(visit, *, min_seconds: int) -> bool:
    """The exception feed's `short_visit`: the same duration and threshold, inverted.

    `completed` only, as the feed always had it: an abandoned visit's `ended_at` is when it was
    given up on (the agent's tap or the sweep), not the end of a conversation.
    """
    seconds = presence_seconds(visit)
    return visit.status == "completed" and seconds is not None and seconds < min_seconds
