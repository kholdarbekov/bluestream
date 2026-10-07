"""The per-agent per-local-day plan snapshot (R1) — plan-vs-fact's denominator.

`Visit.planned` records only visits that were STARTED, so it can answer "was this
visit on the plan" but never "how much of the plan went unvisited" — and the
misses are the whole reason plan-vs-fact exists. The due count itself lives in a
column (`outlets.next_visit_due_at`) that the 01:00 recompute rewrites wholesale
every night, so the only faithful record is one taken at the time.

`snapshot_all` is the job's whole transaction: one commit at the end, the
`ReplenishmentService.recompute_all` precedent. It is an upsert on
`(agent_user_id, plan_date)` so a RE-RUN AFTER THE FIRST HAS FINISHED — a retry,
a manual `celery call` — corrects the day's row instead of doubling it. The
read-then-write is not a lock: two runs OVERLAPPING would both find no row, both
insert, and the second commit would die on `uq_sales_agent_day_plans_agent_date`.
Beat does not produce that overlap (one entry, 01:20), and the portable spelling is
deliberate — a Postgres `ON CONFLICT` would be invisible to the SQLite suite.

The row also freezes WHICH outlets were due (`due_outlet_ids`, spec §4.1.2), from the
same query that counts them, and `compliance` is the one producer of plan compliance
built on it (C4): a due outlet counts once per day when a verified visit
(`visit_rules`) reached it, on the days that have a row AND are worked days. Rows
written before the set existed carry NULL and are read through a legacy fallback.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from business_app import db
from business_app.models.sales_visits import SalesAgentDayPlan, Visit
from business_app.models.user import User
from business_app.services.sales.agent_account_service import SalesAgentAccountService
from business_app.services.sales.agent_metrics_service import AgentMetricsService
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.visit_rules import is_verified_visit, not_verified_reason, short_visit_threshold
from business_app.services.sales.work_calendar import WorkCalendarService
from business_app.utils import local_windows
from business_app.utils.local_windows import local_date, window_bounds
from business_app.utils.timezone_utils import ensure_utc


@dataclass(frozen=True)
class DayCompliance:
    """One local day of `AgentDayPlanService.compliance`.

    `due` is the snapshot's count, None when no row exists. `counted` is 0 and `not_counted`
    is {} on any day that is not measured (not a worked day, or no row). `not_counted` maps a
    reason (`"not_visited"` or a `visit_rules.NOT_VERIFIED_REASONS` member) to the number of due
    outlets it explains. A legacy row has no per-outlet answer and publishes {}.
    """

    day: date
    day_status: str
    plan_source: str
    due: Optional[int]
    counted: int
    not_counted: Dict[str, int]
    legacy: bool


@dataclass(frozen=True)
class Compliance:
    """Plan compliance over an inclusive local range: `due` and `counted` sum the measured days."""

    due: int
    counted: int
    pct: Optional[float]
    min_seconds: int
    days: Tuple[DayCompliance, ...]


class AgentDayPlanService:
    """Spec § *KPIs and attribution* → `sales.snapshot_agent_day_plans`."""

    @staticmethod
    def snapshot_all(now: Optional[datetime] = None) -> int:
        """Write today's plan for every active agent; return how many rows it covered.

        ONE `now` for the whole run, so two agents cannot straddle a local day
        boundary and be filed against different days by the same job. Every active
        agent gets a row even when nothing is due: a missing row means "we were not
        snapshotting on that day" and plan-vs-fact publishes it as
        `plan_source: "none"`, which a zero must not be confused with.
        """
        # Through `local_windows.local_now` — the module attribute the whole phase freezes —
        # rather than a bare `datetime.now(timezone.utc)`, so a job driven with a frozen clock
        # in a test or a probe files its rows against the same day every other reader sees.
        moment = ensure_utc(now) if now is not None else ensure_utc(local_windows.local_now())
        plan_date = local_date(moment)
        # ONE expression of "an active sales agent" (R1): the same roster Task 2's
        # `AgentMetricsService.rows_for_period` reports on, so a snapshot can never
        # exist for an agent the report omits.
        agent_ids = [agent.id for agent in SalesAgentAccountService.active_agents()]
        if not agent_ids:
            return 0

        existing = {
            row.agent_user_id: row
            for row in SalesAgentDayPlan.query.filter(
                SalesAgentDayPlan.plan_date == plan_date,
                SalesAgentDayPlan.agent_user_id.in_(agent_ids),
            ).all()
        }
        for agent_id in agent_ids:
            # WHICH outlets, not only how many (spec §4.1.2): the numerator counts a verified
            # visit to one of THESE, so a shop that turns due after 01:20 (an admin's class
            # edit, a reassignment) cannot enter a day it was not planned on. The ids and the
            # count come from one query, `OutletService.due_scope`.
            due_ids = OutletService.due_outlet_ids(agent_id, now=moment)
            _due_total, overdue_count = OutletService.due_counts(agent_id, now=moment)
            row = existing.get(agent_id)
            if row is None:
                row = SalesAgentDayPlan(agent_user_id=agent_id, plan_date=plan_date)
                db.session.add(row)
            row.due_outlet_ids = due_ids
            row.due_count = len(due_ids)
            row.overdue_count = overdue_count
            row.snapshot_at = moment
        db.session.commit()
        return len(agent_ids)

    @staticmethod
    def compliance(agent_user_id: int, start_date: date, end_date: date) -> Compliance:
        """The ONE producer of plan compliance (C4, spec §4.1.3).

        The numerator is due outlets reached by a verified visit (`visit_rules`), once per
        outlet per day; the denominator is the frozen due set. A day is MEASURED only when it
        has a snapshot row (R47) AND is a worked day (`WorkCalendarService.day_statuses`); both
        rules narrow both halves here, inside the producer, so no caller can forget either. The
        metrics card, the Analytics tab and CSV, the weekly email, the plan-vs-fact table and
        the pay gate all read this answer.

        `pct` is `AgentMetricsService.pct(counted, due, cap=100.0)`: one decimal, None when no
        day was measured. A denominator of zero is not a score of zero.
        """
        start_utc, end_utc = window_bounds(start_date, end_date)
        plans = {
            row.plan_date: row
            for row in SalesAgentDayPlan.query.filter(
                SalesAgentDayPlan.agent_user_id == agent_user_id,
                SalesAgentDayPlan.plan_date >= start_date,
                SalesAgentDayPlan.plan_date <= end_date,
            ).all()
        }
        # Every status: an abandoned visit to a due outlet is a reason (`not_completed`), not an
        # absence.
        visits = Visit.query.filter(
            Visit.agent_user_id == agent_user_id,
            Visit.started_at >= start_utc,
            Visit.started_at < end_utc,
        ).all()
        min_seconds = short_visit_threshold()
        days = AgentDayPlanService._compliance_days(
            start_date,
            end_date,
            plans=plans,
            statuses=WorkCalendarService.day_statuses(agent_user_id, start_date, end_date),
            visits=visits,
            min_seconds=min_seconds,
        )
        measured = [day for day in days if day.day_status == "worked" and day.plan_source == "snapshot"]
        due = sum(day.due for day in measured)
        counted = sum(day.counted for day in measured)
        return Compliance(
            due=due,
            counted=counted,
            pct=AgentMetricsService.pct(counted, due, cap=100.0),
            min_seconds=min_seconds,
            days=days,
        )

    @staticmethod
    def _compliance_days(
        start_date: date,
        end_date: date,
        *,
        plans: Dict[date, SalesAgentDayPlan],
        statuses: Dict[date, str],
        visits: Sequence[Visit],
        min_seconds: int,
    ) -> Tuple[DayCompliance, ...]:
        """`compliance`'s per-day half for ONE agent, over rows the caller already fetched.

        Split out so `plan_vs_fact`, which fetches a whole page of agents in one pass, publishes
        each row's `counted` and `day_status` from THIS loop and not from a copy of it.
        """
        by_key: Dict[Tuple[date, int], List[Visit]] = {}
        for visit in visits:
            # The LOCAL day the visit STARTED: the bucket every KPI and `plan_vs_fact` use (R2).
            by_key.setdefault((local_date(ensure_utc(visit.started_at)), visit.outlet_id), []).append(visit)

        days = []
        day = start_date
        while day <= end_date:
            row = plans.get(day)
            status = statuses[day]
            due = int(row.due_count or 0) if row is not None else None
            if status != "worked" or row is None:
                days.append(
                    DayCompliance(
                        day=day,
                        day_status=status,
                        plan_source="snapshot" if row is not None else "none",
                        due=due,
                        counted=0,
                        not_counted={},
                        legacy=False,
                    )
                )
            elif row.due_outlet_ids is not None:
                counted = 0
                not_counted: Counter = Counter()
                for outlet_id in row.due_outlet_ids:
                    outlet_visits = by_key.get((day, outlet_id), [])
                    if any(is_verified_visit(visit, min_seconds=min_seconds) for visit in outlet_visits):
                        # Once per outlet per day: a second visit to a shop already counted is
                        # activity, not more of the plan.
                        counted += 1
                    elif not outlet_visits:
                        not_counted["not_visited"] += 1
                    else:
                        latest = max(outlet_visits, key=lambda visit: (ensure_utc(visit.started_at), visit.id))
                        not_counted[not_verified_reason(latest, min_seconds=min_seconds)] += 1
                days.append(
                    DayCompliance(
                        day=day,
                        day_status=status,
                        plan_source="snapshot",
                        due=due,
                        counted=counted,
                        not_counted=dict(not_counted),
                        legacy=False,
                    )
                )
            else:
                # A row the 01:20 job wrote before it froze WHICH outlets were due. The only
                # numerator it supports is the verified PLANNED visits that day, capped at its
                # count.
                verified = {
                    visit.outlet_id
                    for (visit_day, _outlet_id), outlet_visits in by_key.items()
                    if visit_day == day
                    for visit in outlet_visits
                    if visit.planned and is_verified_visit(visit, min_seconds=min_seconds)
                }
                days.append(
                    DayCompliance(
                        day=day,
                        day_status=status,
                        plan_source="snapshot",
                        due=due,
                        counted=min(len(verified), due),
                        not_counted={},
                        legacy=True,
                    )
                )
            day += timedelta(days=1)
        return tuple(days)

    @staticmethod
    def _days_by_agent(
        agent_ids: Sequence[int],
        start_date: date,
        end_date: date,
        plan_rows: Sequence[SalesAgentDayPlan],
        visit_rows: Sequence[Visit],
    ) -> Dict[int, Dict[date, DayCompliance]]:
        """`_compliance_days` for every agent on a plan-vs-fact page, from rows fetched once."""
        if not agent_ids:
            return {}
        plans: Dict[int, Dict[date, SalesAgentDayPlan]] = {}
        for plan in plan_rows:
            plans.setdefault(plan.agent_user_id, {})[plan.plan_date] = plan
        visits: Dict[int, List[Visit]] = {}
        for visit in visit_rows:
            visits.setdefault(visit.agent_user_id, []).append(visit)
        statuses = WorkCalendarService.day_statuses_many(agent_ids, start_date, end_date)
        min_seconds = short_visit_threshold()
        return {
            agent_id: {
                entry.day: entry
                for entry in AgentDayPlanService._compliance_days(
                    start_date,
                    end_date,
                    plans=plans.get(agent_id, {}),
                    statuses=statuses[agent_id],
                    visits=visits.get(agent_id, []),
                    min_seconds=min_seconds,
                )
            }
            for agent_id in agent_ids
        }

    @staticmethod
    def _agent_names(user_ids) -> Dict[int, str]:
        """`{user_id: full_name}` for the rows about to be published.

        Read from `users`, not off a Visit: a day that has a plan and no visits has no visit to
        take a name from, and that row is exactly the one a supervisor came here to find.
        """
        ids = list(user_ids)
        if not ids:
            return {}
        return {user.id: user.full_name for user in User.query.filter(User.id.in_(ids)).all()}

    @staticmethod
    def plan_vs_fact(
        start_date: date,
        end_date: date,
        *,
        agent_user_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """One row per agent per LOCAL day: what was due, and what actually happened (R1/R2).

        The denominator is the nightly SNAPSHOT, never a replay of today's `next_visit_due_at`.
        That column is rewritten for every outlet every night and has no history, and four of
        the five inputs behind it (agent assignment, class, cadence override, the catalogue) are
        mutable with no history either — a replay would report today's cadence against a past
        calendar and silently change its answer for a finished day every time anyone edits an
        outlet. A day the job never ran for publishes `due: null` / `plan_source: "none"`
        instead of a plausible number nobody can check.

        A row exists for every (agent, day) that has EITHER a plan or a visit. Post-deploy the
        snapshot writes a row for every active agent every day, including zero-due days, which
        is what makes an empty row mean "nothing was due" rather than "the job was not live".

        Each row also carries `counted` and `day_status`: `compliance`'s own per-day answer (C4),
        built by `_compliance_days` from the rows fetched here, so Σ `counted` over a window IS
        the numerator the metrics card, the email and the pay gate publish.

        Rows come back RAW (`day` is a `date`); the route serializes them. Same division as
        `ExceptionFeedService.list` — a service that shaped its own wire payload would be the
        only one in the sales estate that did.

        `now` is accepted for call-shape parity across the phase-3 read surface; both bounds are
        absolute local dates, so nothing here reads the clock.
        """
        start_utc, end_utc = window_bounds(start_date, end_date)
        plans = SalesAgentDayPlan.query.filter(
            SalesAgentDayPlan.plan_date >= start_date,
            SalesAgentDayPlan.plan_date <= end_date,
        )
        # Every status, not only `completed`: `_compliance_days` names an abandoned visit to a
        # due outlet as a reason. The `completed` tally below filters for itself.
        visits = Visit.query.filter(
            Visit.started_at >= start_utc,
            Visit.started_at < end_utc,
        )
        if agent_user_id is not None:
            plans = plans.filter(SalesAgentDayPlan.agent_user_id == agent_user_id)
            visits = visits.filter(Visit.agent_user_id == agent_user_id)
        plan_rows = plans.all()
        visit_rows = visits.all()

        due = {(plan.agent_user_id, plan.plan_date): int(plan.due_count or 0) for plan in plan_rows}
        facts = {}
        for visit in visit_rows:
            if visit.status != "completed":
                continue
            # The day a visit belongs to is the LOCAL day it STARTED — the same column and the
            # same boundary `planned` was stamped against at `VisitService.start`, so a visit is
            # never counted against a plan it was not measured against.
            key = (visit.agent_user_id, local_date(ensure_utc(visit.started_at)))
            bucket = facts.setdefault(key, {"completed": 0, "unplanned": 0, "with_order": 0})
            bucket["completed"] += 1
            bucket["unplanned"] += 0 if visit.planned else 1
            bucket["with_order"] += 1 if visit.outcome == "order_placed" else 0

        keys = set(due) | set(facts)
        agent_ids = sorted({key[0] for key in keys})
        names = AgentDayPlanService._agent_names(agent_ids)
        per_day = AgentDayPlanService._days_by_agent(agent_ids, start_date, end_date, plan_rows, visit_rows)
        empty = {"completed": 0, "unplanned": 0, "with_order": 0}
        rows = []
        for agent_id, day in sorted(keys, key=lambda key: (names.get(key[0]) or "", key[1])):
            fact = facts.get((agent_id, day), empty)
            measured = per_day[agent_id][day]
            rows.append(
                {
                    "agent_user_id": agent_id,
                    "agent_name": names.get(agent_id),
                    "day": day,
                    "due": due.get((agent_id, day)),
                    "completed": fact["completed"],
                    "counted": measured.counted,
                    "unplanned": fact["unplanned"],
                    # ONE rounding rule for every percentage phase 3 publishes: 1 decimal,
                    # null when the denominator is zero.
                    "strike_rate_pct": AgentMetricsService.pct(fact["with_order"], fact["completed"]),
                    "plan_source": "snapshot" if (agent_id, day) in due else "none",
                    "day_status": measured.day_status,
                }
            )
        return rows
