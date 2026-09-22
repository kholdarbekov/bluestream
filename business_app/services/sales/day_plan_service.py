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
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from business_app import db
from business_app.models.sales_visits import SalesAgentDayPlan, Visit
from business_app.models.user import User
from business_app.services.sales.agent_account_service import SalesAgentAccountService
from business_app.services.sales.outlet_service import OutletService
from business_app.utils import local_windows
from business_app.utils.local_windows import local_date, window_bounds
from business_app.utils.timezone_utils import ensure_utc


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
            due_count, overdue_count = OutletService.due_counts(agent_id, now=moment)
            row = existing.get(agent_id)
            if row is None:
                row = SalesAgentDayPlan(agent_user_id=agent_id, plan_date=plan_date)
                db.session.add(row)
            row.due_count = due_count
            row.overdue_count = overdue_count
            row.snapshot_at = moment
        db.session.commit()
        return len(agent_ids)

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

        Rows come back RAW (`day` is a `date`); the route serializes them. Same division as
        `ExceptionFeedService.list` — a service that shaped its own wire payload would be the
        only one in the sales estate that did.

        `now` is accepted for call-shape parity across the phase-3 read surface; both bounds are
        absolute local dates, so nothing here reads the clock.
        """
        # Imported inside the method: `AgentMetricsService` reads this module's snapshot table
        # for `planned_visits`, so a module-level import closes the cycle (the
        # `ReplenishmentService` import inside `OutletService.list_for_agent` precedent).
        from business_app.services.sales.agent_metrics_service import AgentMetricsService

        start_utc, end_utc = window_bounds(start_date, end_date)
        plans = SalesAgentDayPlan.query.filter(
            SalesAgentDayPlan.plan_date >= start_date,
            SalesAgentDayPlan.plan_date <= end_date,
        )
        visits = Visit.query.filter(
            Visit.status == "completed",
            Visit.started_at >= start_utc,
            Visit.started_at < end_utc,
        )
        if agent_user_id is not None:
            plans = plans.filter(SalesAgentDayPlan.agent_user_id == agent_user_id)
            visits = visits.filter(Visit.agent_user_id == agent_user_id)

        due = {(plan.agent_user_id, plan.plan_date): int(plan.due_count or 0) for plan in plans.all()}
        facts = {}
        for visit in visits.all():
            # The day a visit belongs to is the LOCAL day it STARTED — the same column and the
            # same boundary `planned` was stamped against at `VisitService.start`, so a visit is
            # never counted against a plan it was not measured against.
            key = (visit.agent_user_id, local_date(ensure_utc(visit.started_at)))
            bucket = facts.setdefault(key, {"completed": 0, "unplanned": 0, "with_order": 0})
            bucket["completed"] += 1
            bucket["unplanned"] += 0 if visit.planned else 1
            bucket["with_order"] += 1 if visit.outcome == "order_placed" else 0

        keys = set(due) | set(facts)
        names = AgentDayPlanService._agent_names({key[0] for key in keys})
        empty = {"completed": 0, "unplanned": 0, "with_order": 0}
        rows = []
        for agent_id, day in sorted(keys, key=lambda key: (names.get(key[0]) or "", key[1])):
            fact = facts.get((agent_id, day), empty)
            rows.append(
                {
                    "agent_user_id": agent_id,
                    "agent_name": names.get(agent_id),
                    "day": day,
                    "due": due.get((agent_id, day)),
                    "completed": fact["completed"],
                    "unplanned": fact["unplanned"],
                    # ONE rounding rule for every percentage phase 3 publishes: 1 decimal,
                    # null when the denominator is zero.
                    "strike_rate_pct": AgentMetricsService.pct(fact["with_order"], fact["completed"]),
                    "plan_source": "snapshot" if (agent_id, day) in due else "none",
                }
            )
        return rows
