"""The agent's morning digest (D7): one message, no new rules.

Every list here is an ANSWER another surface already asks for, read back rather than
re-derived (CLAUDE.md "full scope", spec D15):

  * *due today* and *overdue* are the due scope itself — `OutletService.list_for_agent(...,
    "due")`, the exact query behind the hub's *Due today & overdue* row — split by the
    `overdue_days` number the card and the list buttons already print, so the digest can
    never call an outlet overdue that the list one tap later shows without a "+Nd";
  * *unvisited* is the admin Outlets filter's own predicate (`OutletService.unvisited_filter`);
  * *open visit* is `VisitService.current`, the lookup the hub and the card already share.

Nothing here commits, sends or formats: `build` returns a payload and the staff bot renders
it (`staff.sales.notify.morning_digest*`). `None` means "this agent has nothing to be told
this morning" and the caller sends no message at all (spec, *Notifications*) — a digest
that arrives empty every Sunday is how an agent learns to ignore the one that matters.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import current_app

from business_app.models.sales import Outlet
from business_app.serializers.sales_serializers import overdue_days
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.visit_service import VisitService
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import ensure_utc

# The payload's shape, published as data so the staff bot's renderer can pin its fixture
# against the producer instead of restating it (Task 8). The bot renders and never
# re-decides, which means the ONE thing it must agree with is the set of keys it reads —
# and a fixture written by hand is exactly where that agreement rots: it keeps passing
# while the service renames a field, and the first evidence is a KeyError at 08:30.
DIGEST_SECTION_KEYS = ("due_today", "overdue", "unvisited")
# Two row shapes, deliberately. A due row answers "how late is this shop" (`overdue_days`)
# and an unvisited row answers "how long since anyone came" (`days`); flattening them into
# one tuple would let either section lose its own number without a test noticing. The shop
# is `outlet_name` in all three, the spelling every other sales-event payload uses.
DIGEST_DUE_ROW_KEYS = ("outlet_id", "outlet_name", "overdue_days")
DIGEST_UNVISITED_ROW_KEYS = ("outlet_id", "outlet_name", "days")
DIGEST_ROW_KEYS = {
    "due_today": DIGEST_DUE_ROW_KEYS,
    "overdue": DIGEST_DUE_ROW_KEYS,
    "unvisited": DIGEST_UNVISITED_ROW_KEYS,
}


class AgentDigestService:
    """Spec § *Stages, due dates and alerts* → `send_agent_morning_digest`."""

    # The spec's own cut ("unvisited > SALES_UNVISITED_ALERT_DAYS, top five by days").
    UNVISITED_LIMIT = 5
    # How many rows each DUE section may carry. R6 caps only `unvisited`, but a digest is a
    # Telegram message and the bot draws one line AND one inline button per row: an agent
    # with ~60 due outlets produces a message past the 4096-character limit and a keyboard
    # past its own, `send_message` raises, `push_sales_event` retries three times, and the
    # agent gets NOTHING — the failure mode a cap exists to avoid. The overflow is counted
    # and published as `more_count` rather than silently dropped, and it is counted HERE
    # because membership is the backend's answer (R6/SSOT): a bot that truncated would be
    # deciding which shops the agent hears about.
    DUE_SECTION_LIMIT = 10
    # How many due outlets `build` READS — the due scope's own ceiling (`list_for_agent` caps
    # `per_page` at 100). It is one page, not the estate, so the page size must not leak into
    # the published number: `more_count` adds the rows this page never reached
    # (`_total - len(page)`) to the rows the section cut dropped. An agent with 140 due shops
    # is told about 130 unnamed ones, not 90.
    DUE_PAGE_SIZE = 100

    @staticmethod
    def _local_date(now: datetime) -> str:
        """The calendar day the agent is standing in, ISO, for the bot to format.

        `local_windows.local_date` is the repo's ONE local-calendar-day expression and this
        is a thin ISO wrapper on it — the hand-rolled `now.astimezone(local_now().tzinfo)`
        that used to live here was a second copy of the same three lines. The INSTANT is
        still the job's own `now` (`build` hands it in already `ensure_utc`-normalised),
        because a digest is a message about one moment and must not re-read the wall clock
        halfway through building itself.
        """
        return local_date(now).isoformat()

    @staticmethod
    def _due_row(outlet: Outlet, *, now: datetime) -> Dict[str, Any]:
        return {
            "outlet_id": outlet.id,
            "outlet_name": outlet.name,
            "overdue_days": overdue_days(outlet.next_visit_due_at, now),
        }

    @staticmethod
    def _unvisited_rows(agent_user_id: int, *, now: datetime) -> List[Dict[str, Any]]:
        """Active outlets nobody has been to lately, stalest first, top five.

        `days` is None for an outlet that has never been visited at all — the honest
        answer, and the reason the sort key puts those first: "never" is not a number the
        backend may invent from `created_at` and then print under a 21-day heading.
        """
        threshold = int(current_app.config["SALES_UNVISITED_ALERT_DAYS"])
        outlets = Outlet.query.filter(
            OutletService.agent_outlet_filter(agent_user_id),
            OutletService.unvisited_filter(threshold, now=now),
        ).all()
        # Sorted here rather than in SQL: NULLS FIRST is dialect-specific and the suite runs
        # on SQLite, while an agent's estate is tens of rows.
        # `outlet.id` breaks the tie: "never visited" is the SAME key for every such outlet, so
        # without it which five the digest names is whatever order the query happened to return.
        outlets.sort(
            key=lambda outlet: (
                outlet.last_visit_at is not None,
                ensure_utc(outlet.last_visit_at or now),
                outlet.id,
            )
        )
        rows = []
        for outlet in outlets[: AgentDigestService.UNVISITED_LIMIT]:
            last_visit_at = ensure_utc(outlet.last_visit_at) if outlet.last_visit_at else None
            rows.append(
                {
                    "outlet_id": outlet.id,
                    "outlet_name": outlet.name,
                    "days": (now - last_visit_at).days if last_visit_at is not None else None,
                }
            )
        return rows

    @staticmethod
    def build(agent_user_id: int, *, now: datetime) -> Optional[Dict[str, Any]]:
        """The whole digest for one agent, or None when there is nothing to say."""
        moment = ensure_utc(now)

        due_today: List[Dict[str, Any]] = []
        overdue: List[Dict[str, Any]] = []
        # The page the message NAMES, and — separately — the true total it counts.
        # Both come from the due scope, but the total comes through `due_counts`,
        # the one helper the 01:20 plan snapshot also reads: the agent's "+N more"
        # and the manager's plan-vs-fact denominator are the same number and must
        # not be computed twice.
        due_outlets, _page_total = OutletService.list_for_agent(
            agent_user_id, "due", page=1, per_page=AgentDigestService.DUE_PAGE_SIZE, now=moment
        )
        due_total, _overdue_total = OutletService.due_counts(agent_user_id, now=moment)
        for outlet in due_outlets:
            # The due scope already excludes a NULL due date, so `overdue_days` here is an
            # int: 0 means "due today", anything higher is the number the list prints.
            row = AgentDigestService._due_row(outlet, now=moment)
            (overdue if row["overdue_days"] else due_today).append(row)

        # Capped HERE, and the overflow counted rather than dropped: the bot draws a line
        # and a button per row, and an uncapped list is a `send_message` that raises and
        # three retries that deliver nothing at all. The oldest debt is what survives the
        # cut, because `list_for_agent("due")` already orders due-date ascending.
        #
        # `more_count` is every due outlet this message does NOT name, and there are two ways
        # to go unnamed: the section cut dropped you (the first two terms), or the single page
        # `build` reads never reached you at all (the third). Counting only the cut would make
        # the number silently wrong past `DUE_PAGE_SIZE` — for precisely the agent whose
        # morning is worst, which is the agent "+N more" exists to warn.
        limit = AgentDigestService.DUE_SECTION_LIMIT
        more_count = (
            max(0, len(overdue) - limit) + max(0, len(due_today) - limit) + max(0, due_total - len(due_outlets))
        )
        overdue = overdue[:limit]
        due_today = due_today[:limit]

        unvisited = AgentDigestService._unvisited_rows(agent_user_id, now=moment)

        visit = VisitService.current(agent_user_id)
        open_visit = (
            {
                "visit_id": visit.id,
                "outlet_id": visit.outlet_id,
                # `outlet_name`, as every other sales-event payload spells it
                # (`services/sales/notifications.py`). No `current_step`: the digest says
                # "you left one open" and offers the card, which is where the step lives.
                "outlet_name": visit.outlet.name if visit.outlet is not None else None,
            }
            if visit is not None
            else None
        )

        if not (due_today or overdue or unvisited or open_visit):
            return None
        return {
            "date": AgentDigestService._local_date(moment),
            "due_today": due_today,
            "overdue": overdue,
            "more_count": more_count,
            "unvisited": unvisited,
            "open_visit": open_visit,
        }
