"""Every sales-agent KPI the spec names, computed once (spec § *KPIs and attribution*).

One service, twenty keys, one rule each. Every phase-3 consumer — the two admin metrics
routes, `GET /staff/sales/me/stats`, the weekly `agent_performance` email and the *Sales
agents* list-row counters — reads THIS service and re-derives nothing (CLAUDE.md "full
scope"). The bot and the admin UI render what is published here.

Windows are LOCAL calendar days. `business_app/utils/local_windows.py` turns a
`start_date`/`end_date` pair into the UTC half-open interval every query below uses, so a
visit at 20:00 Tashkent belongs to the day the agent was standing in rather than to
tomorrow (the `_driver_day_start_utc` lesson).

Every visit metric windows on `Visit.started_at`: it is the row's only NOT NULL instant,
the only indexed one (`ix_visits_started_at`, `models/sales_visits.py:86`), and the moment
the agent decided to make the visit. A visit begun at 23:50 and closed after midnight
therefore counts on the day it began — ONE window column for all six visit metrics rather
than a different one per metric, which is how two surfaces end up disagreeing about which
day a visit happened on.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import case, func
from sqlalchemy.orm import selectinload

from business_app import db
from business_app.models.order import Order, OrderItem
from business_app.models.sales import Outlet, OutletStageHistory
from business_app.models.sales_visits import SalesAgentDayPlan, Visit, VisitStockCheck
from business_app.models.user import User
from business_app.utils.local_windows import days_inclusive, local_date, local_day_bounds, window_bounds
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus

# The spec's own reading order (spec :391). Published as data so every consumer pins its
# fixture against the producer instead of restating it: the bot card, the email table and
# the CSV header all walk this tuple, and a renamed key fails a test rather than rendering
# a blank column at 08:10 on a Monday.
METRIC_KEYS = (
    "planned_visits",
    "completed_visits",
    "plan_vs_fact_pct",
    "unplanned_visits",
    "visits_per_day",
    "strike_rate_pct",
    "assigned_outlets",
    "active_outlets",
    "active_share_pct",
    "new_outlets_registered",
    "new_outlets_activated",
    "orders_placed",
    "orders_delivered_paid",
    "bottles_delivered_paid",
    "revenue_delivered_paid",
    "agent_orders_cancelled",
    "suggested_vs_accepted_pct",
    "out_of_range_checkins",
    "skipped_checkins",
    "avg_visit_minutes",
)

# The four groups the card is READ in, published as data for the same reason (R30). Concatenated in
# this order they ARE `METRIC_KEYS` — the slices below are written as slices so that relationship is
# mechanical rather than remembered, and a 21st key cannot join a group without joining the tuple.
# `suggested_vs_accepted_pct` is an ORDERS question ("did the suggestion become an order"), not a
# discipline one. The staff bot's card sections and the Analytics tab's column groups both pin to
# this dict, so the four headings exist once in the estate instead of three times.
METRIC_GROUPS = {
    "visits": METRIC_KEYS[0:6],
    "outlets": METRIC_KEYS[6:11],
    "orders": METRIC_KEYS[11:17],
    "discipline": METRIC_KEYS[17:20],
}

_ZERO = Decimal("0.00")


class AgentMetricsService:
    """Spec § *KPIs and attribution* — the twenty keys and nothing else."""

    @staticmethod
    def pct(numerator: float, denominator: float, *, cap: Optional[float] = None) -> Optional[float]:
        """A percentage to one decimal, or None when the question does not apply.

        None, never 0.0. An agent with no completed visit has no strike rate, and printing
        "0.0%" would make an empty week look like a failed one — the same distinction the
        outlet card already makes with `open_receivable is None`.

        PUBLIC, and deliberately so: `AgentDayPlanService.plan_vs_fact` (Task 4) publishes a
        per-day `strike_rate_pct` and rounds it HERE. A private copy there would be the same
        rule in two places, and the day's number and the period's number would part company
        the first time the rounding or the null rule moved.
        """
        if not denominator:
            return None
        value = round(numerator * 100.0 / float(denominator), 1)
        return min(value, cap) if cap is not None else value

    @staticmethod
    def compute(
        agent_user_id: int, start_date: date, end_date: date, *, now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """The twenty KPIs for ONE agent over the inclusive local days [start_date, end_date].

        `now` is accepted so every entry point of this service takes the caller's moment
        (the digest/nightly-job contract) and is deliberately unread here: the window is
        fixed by the two dates. The keys that are "as of now" — the two outlet counters and
        the three delivered-and-paid ones — read the CURRENT row state, which is what "as
        of now" means: an order delivered tomorrow joins this period's revenue tomorrow,
        and the UI says so.

        `parse_date_range` (Task 3) is the one guard on the pair; an inverted range never
        reaches this method, so nothing here re-checks it.

        `plan_vs_fact_pct` is measured on the days a plan existed (R47). Both halves of the
        fraction come from the SAME `sales_agent_day_plans` rows: a local day with no row is
        left out of the denominator AND its completed planned visits are left out of the
        numerator. `Visit.planned` has been stamped since phase 2a, so counting it across the
        whole window against a partial denominator would measure a seven-day numerator with a
        five-day plan and `cap=100.0` would dress the result up as a flawless 100.0%. The key
        is `None` only when no day of the window was snapshotted at all — a denominator of
        zero is not a score of zero. `planned_visits` is the same sum over the same rows.
        """
        start_utc, end_utc = window_bounds(start_date, end_date)
        visits = (
            Visit.query.filter(
                Visit.agent_user_id == agent_user_id,
                Visit.started_at >= start_utc,
                Visit.started_at < end_utc,
            )
            .order_by(Visit.id.asc())
            .all()
        )
        completed = [visit for visit in visits if visit.status == "completed"]
        completed_ids = [visit.id for visit in completed]

        # The rows themselves, not a SUM: R47 needs the DAYS as well as the total, because the
        # numerator below is narrowed to exactly the days these rows cover.
        plan_rows = (
            db.session.query(SalesAgentDayPlan.plan_date, SalesAgentDayPlan.due_count)
            .filter(
                SalesAgentDayPlan.agent_user_id == agent_user_id,
                SalesAgentDayPlan.plan_date >= start_date,
                SalesAgentDayPlan.plan_date <= end_date,
            )
            .all()
        )
        planned_days = {row.plan_date for row in plan_rows}
        planned_visits = int(sum(row.due_count or 0 for row in plan_rows))
        # Completed, planned, AND on a day that has a plan behind it. `local_date` is the same
        # bucket key `AgentDayPlanService.plan_vs_fact` files a visit under, so the period's
        # number and the per-day table cannot file one visit under two days.
        planned_completed = len(
            [visit for visit in completed if visit.planned and local_date(visit.started_at) in planned_days]
        )
        assigned_outlets, active_outlets = AgentMetricsService._outlet_counts(agent_user_id)
        registered, activated = AgentMetricsService._new_outlets(agent_user_id, start_utc, end_utc)
        orders = AgentMetricsService._order_metrics(agent_user_id, start_utc, end_utc)

        return {
            "planned_visits": planned_visits,
            "completed_visits": len(completed),
            # Capped at 100: an agent who cleared today's list and walked an overdue shop
            # from last week did 6 of 5, and "120%" reads as a data bug on a manager's screen.
            # Both halves are narrowed to the snapshotted days above (R47), so a night the
            # 01:20 job missed costs this ratio precision, never truth — and `pct` answers
            # None when the denominator is 0, which is the go-live window and nothing else.
            # The caveat rides on the VALUE, never on a 21st key: METRIC_KEYS is pinned by the
            # bot card, the three email templates and the Analytics tab.
            "plan_vs_fact_pct": AgentMetricsService.pct(planned_completed, planned_visits, cap=100.0),
            "unplanned_visits": len([visit for visit in completed if not visit.planned]),
            # Calendar days, not working days: no shift or working-day calendar exists
            # anywhere in this repo, and inventing one here would be a second one tomorrow.
            "visits_per_day": round(len(completed) / days_inclusive(start_date, end_date), 1),
            # A later cancellation does not un-strike the visit: the agent did sell, and the
            # decline is itemised in the exceptions feed rather than quietly deducted here.
            "strike_rate_pct": AgentMetricsService.pct(
                len([visit for visit in completed if visit.outcome == "order_placed"]), len(completed)
            ),
            "assigned_outlets": assigned_outlets,
            "active_outlets": active_outlets,
            "active_share_pct": AgentMetricsService.pct(active_outlets, assigned_outlets),
            "new_outlets_registered": registered,
            "new_outlets_activated": activated,
            **orders,
            "suggested_vs_accepted_pct": AgentMetricsService._suggested_vs_accepted(completed_ids),
            # `is_(False)` and not `not in_radius`: NULL means the check-in could not be
            # measured (skipped, no pin sent, or an outlet with no coordinates —
            # `visit_service.py:245-259`) and an unmeasured visit is not an offence.
            # Counted over every visit in the window, including abandoned ones: an
            # out-of-range check-in happened whether or not the visit was ever closed.
            "out_of_range_checkins": len([visit for visit in visits if visit.in_radius is False]),
            "skipped_checkins": len([visit for visit in visits if visit.checkin_skipped]),
            "avg_visit_minutes": AgentMetricsService._avg_visit_minutes(completed),
        }

    @staticmethod
    def _assigned_outlet_clauses(agent_user_ids: List[int]):
        """The clauses that define an agent's outlets, as a tuple to be splatted — spelled ONCE.

        `assigned_agent_user_id` plus `stage != 'lost'`: a written-off shop is nobody's
        territory, and counting it would drop `active_share_pct` every time an agent
        correctly closed a dead prospect. Deliberately NOT `OutletService.agent_outlet_filter`
        (assigned OR onboarded, `outlet_service.py:602-610`): that predicate answers "which
        outlets may this agent OPEN" — a visibility question, also the one
        `OutletService.due_counts` asks for the nightly plan snapshot — and a shop the agent
        registered before a territory hand-over belongs on the NEW owner's numbers. Phase 3
        ships both definitions on purpose (R7); this helper is the assignment one.

        Both counters in this service splat these clauses (`_outlet_counts` for one agent,
        `today_counters` for a whole page), so "whose numbers is this shop on" cannot be
        answered two ways from inside one file.
        """
        return (Outlet.assigned_agent_user_id.in_(agent_user_ids), Outlet.stage != "lost")

    @staticmethod
    def _assigned_active_columns():
        """`(count, active count)` over those clauses, as columns to be splatted.

        Beside the clauses because "active" is half the same rule: both counters must read
        `stage == 'active'` the same way or the page row and the agent's own card publish two
        `active_outlets`.
        """
        return (
            func.count(Outlet.id),
            func.coalesce(func.sum(case((Outlet.stage == "active", 1), else_=0)), 0),
        )

    @staticmethod
    def _outlet_counts(agent_user_id: int) -> Tuple[int, int]:
        """(assigned, active) as of now for ONE agent, off `_assigned_outlet_clauses`."""
        assigned, active = (
            db.session.query(*AgentMetricsService._assigned_active_columns())
            .filter(*AgentMetricsService._assigned_outlet_clauses([agent_user_id]))
            .one()
        )
        return int(assigned or 0), int(active or 0)

    @staticmethod
    def _new_outlets(agent_user_id: int, start_utc: datetime, end_utc: datetime) -> Tuple[int, int]:
        """(registered, activated), both keyed on `onboarded_by_user_id`.

        Registered is `created_at` in the window. An outlet created by the import backfill
        has no onboarder (`outlet_service.py:996-1011` leaves it NULL) and counts for
        nobody — which is the point: importing the existing customer book must not mint
        new-outlet credit for whoever pressed the button.

        Activated is the FIRST `outlet_stage_history` row that reaches `active`, not
        `outlets.approved_at`. `approved_at` is stamped by that same import backfill
        (`outlet_service.py:1009-1010`) on customers who have been buying for years, and it
        is NOT stamped by three of the paths that actually reach `active` (the try-out
        conversion job `:1093`, the reactivation job `:1151`, and the first order at
        `visit_service.py:722`). FIRST, so a shop that went dormant and came back does not
        pay out twice.
        """
        registered = int(
            db.session.query(func.count(Outlet.id))
            .filter(
                Outlet.onboarded_by_user_id == agent_user_id,
                Outlet.created_at >= start_utc,
                Outlet.created_at < end_utc,
            )
            .scalar()
            or 0
        )
        first_active = (
            db.session.query(
                OutletStageHistory.outlet_id.label("outlet_id"),
                func.min(OutletStageHistory.created_at).label("first_active_at"),
            )
            .filter(OutletStageHistory.to_stage == "active")
            .group_by(OutletStageHistory.outlet_id)
            .subquery()
        )
        activated = int(
            db.session.query(func.count(first_active.c.outlet_id))
            .join(Outlet, Outlet.id == first_active.c.outlet_id)
            .filter(
                Outlet.onboarded_by_user_id == agent_user_id,
                first_active.c.first_active_at >= start_utc,
                first_active.c.first_active_at < end_utc,
            )
            .scalar()
            or 0
        )
        return registered, activated

    @staticmethod
    def _order_metrics(agent_user_id: int, start_utc: datetime, end_utc: datetime) -> Dict[str, Any]:
        """The five order keys, windowed on `Order.created_at` — when the agent SOLD.

        Attribution is `created_by_staff_id` AND `order_source == "sales_agent"` together:
        the staff id alone would sweep in an order the same person placed from the admin
        panel wearing an operator hat, and the source alone carries no agent.

        Bottles come from `Product.returnable_bottles_for` — the ONE line-to-bottles
        conversion (`product.py:154-164`) — over the non-reward lines. A free reward bottle
        is not something the agent sold, and re-spelling the returnability test in SQL
        would be a second expression of a rule that has already been wrong once.
        """
        orders = (
            Order.query.options(
                # Both relationships are default-lazy, and the bottle loop below walks every
                # line of every delivered-and-paid order. Left lazy that is one query per order
                # plus one per line — and `rows_for_period` runs this per agent over a window
                # `SALES_METRICS_MAX_RANGE_DAYS` allows to be 92 days, so the all-agents metrics
                # route and the weekly email would turn a roster times a quarter of orders into
                # thousands of round trips. Two extra queries instead.
                selectinload(Order.order_items).selectinload(OrderItem.product)
            )
            .filter(
                Order.created_by_staff_id == agent_user_id,
                Order.order_source == "sales_agent",
                Order.created_at >= start_utc,
                Order.created_at < end_utc,
            )
            .order_by(Order.id.asc())
            .all()
        )
        delivered_paid = [order for order in orders if order.status == OrderStatus.DELIVERED and bool(order.is_paid)]
        revenue = sum((order.total_amount or _ZERO for order in delivered_paid), _ZERO)
        bottles = _ZERO
        for order in delivered_paid:
            for item in order.order_items:
                if item.is_reward_item or item.product is None:
                    continue
                bottles += item.product.returnable_bottles_for(item.quantity)
        return {
            "orders_placed": len(orders),
            "orders_delivered_paid": len(delivered_paid),
            "bottles_delivered_paid": int(bottles.to_integral_value(rounding=ROUND_HALF_UP)),
            # Decimal all the way through, float once at the edge — the
            # `serialize_order_brief` precedent (`sales_serializers.py:313`).
            "revenue_delivered_paid": float(revenue.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "agent_orders_cancelled": len([order for order in orders if order.status == OrderStatus.CANCELLED]),
        }

    @staticmethod
    def _avg_visit_minutes(completed: List[Visit]) -> Optional[float]:
        """Mean minutes from check-in to close, sweep-closed visits excluded.

        `checkin_at`, not `started_at`: Start is tapped on the way to the shop, sometimes
        minutes before the door, and a mean over `started_at` measures the walk. A visit the
        auto-abandon sweep closed carries a span of at least `SALES_VISIT_AUTO_ABANDON_HOURS`
        that belongs to a dead phone rather than to a conversation, so it is dropped instead
        of being allowed to drag the mean up by hours.

        Computed in Python over rows the window already bounded: SQLite (the suite's engine)
        has no `EXTRACT(EPOCH ...)`, and an agent's week is tens of rows.
        """
        cutoff_seconds = float(current_app.config["SALES_VISIT_AUTO_ABANDON_HOURS"]) * 3600.0
        spans = []
        for visit in completed:
            if visit.checkin_at is None or visit.ended_at is None:
                continue
            seconds = (ensure_utc(visit.ended_at) - ensure_utc(visit.checkin_at)).total_seconds()
            if seconds >= cutoff_seconds:
                continue
            spans.append(seconds)
        if not spans:
            return None
        return round(sum(spans) / len(spans) / 60.0, 1)

    @staticmethod
    def _suggested_vs_accepted(completed_ids: List[int]) -> Optional[float]:
        """Σ accepted ÷ Σ suggested over the window's completed-visit stock checks.

        A ratio of SUMS, not a mean of per-row ratios: an agent who took 1 of 1 on a cheap
        line and 0 of 40 on the pallet has not accepted half the suggestion.
        `accepted_qty IS NULL` is a zero, not a missing measurement — it is written only for
        products that were both stock-checked and ordered (`visit_service.py:641-644`), so
        NULL means "suggested and not taken". Rows with no suggestion (NULL or 0 — no rate
        data yet) are outside the question entirely.
        """
        if not completed_ids:
            return None
        suggested, accepted = (
            db.session.query(
                func.coalesce(func.sum(VisitStockCheck.suggested_qty), 0),
                func.coalesce(func.sum(func.coalesce(VisitStockCheck.accepted_qty, 0)), 0),
            )
            .filter(VisitStockCheck.visit_id.in_(completed_ids), VisitStockCheck.suggested_qty > 0)
            .one()
        )
        return AgentMetricsService.pct(float(accepted or 0), float(suggested or 0))

    @staticmethod
    def rows_for_period(
        start_date: date,
        end_date: date,
        *,
        now: Optional[datetime] = None,
        agent_user_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """One row per active sales agent: identity, then the twenty keys.

        The *Agent performance* tab, `GET /admin/sales/agents/metrics` and the weekly email
        all want the same table, so it is built once, here. `compute` is called per agent
        rather than fanned out into twenty grouped queries: the roster is tens of people,
        and one rule with one expression is worth more than the round trips saved.

        Sorted by name with the id as the tiebreak, so two agents called "Aziz Agent" keep
        a stable order between the table, the CSV and the email (the digest's tiebreak
        lesson: an identical sort key otherwise leaves the order to the query planner).
        """
        # Imported here, not at module scope: `agent_account_service` imports THIS module at
        # the top (its row counters read `today_counters`), and a top-level import back
        # would close the cycle.
        from business_app.services.sales.agent_account_service import SalesAgentAccountService

        if agent_user_ids is None:
            agents = SalesAgentAccountService.active_agents()
        elif agent_user_ids:
            agents = User.query.filter(User.id.in_(agent_user_ids)).all()
        else:
            agents = []
        rows = [
            {
                "agent_user_id": agent.id,
                "agent_name": agent.full_name,
                "phone": agent.phone,
                **AgentMetricsService.compute(agent.id, start_date, end_date, now=now),
            }
            for agent in agents
        ]
        rows.sort(key=lambda row: ((row["agent_name"] or "").lower(), row["agent_user_id"]))
        return rows

    @staticmethod
    def today_counters(agent_user_ids: List[int], *, now: Optional[datetime] = None) -> Dict[int, Dict[str, int]]:
        """The four numbers the *Sales agents* rows print, batched for a whole page.

        `visits_today` and `orders_today` are `compute`'s `completed_visits` and
        `orders_placed` over the local day — the SAME rule, so the list row and the agent's
        metrics card can never print two answers to one question. They are batched here
        because a 20-row page must not run twenty full metric passes; `assigned_outlets` /
        `active_outlets` ride along from the same grouped query, splatting the SAME
        `_assigned_outlet_clauses` / `_assigned_active_columns` `_outlet_counts` uses rather
        than re-typing the predicate for a whole page.
        """
        if not agent_user_ids:
            return {}
        start_utc, end_utc = local_day_bounds(local_date(now))
        counters = {
            agent_user_id: {
                "visits_today": 0,
                "orders_today": 0,
                "assigned_outlets": 0,
                "active_outlets": 0,
            }
            for agent_user_id in agent_user_ids
        }
        visit_rows = (
            db.session.query(Visit.agent_user_id, func.count(Visit.id))
            .filter(
                Visit.agent_user_id.in_(agent_user_ids),
                Visit.status == "completed",
                Visit.started_at >= start_utc,
                Visit.started_at < end_utc,
            )
            .group_by(Visit.agent_user_id)
            .all()
        )
        for agent_user_id, count in visit_rows:
            counters[agent_user_id]["visits_today"] = int(count or 0)
        order_rows = (
            db.session.query(Order.created_by_staff_id, func.count(Order.id))
            .filter(
                Order.created_by_staff_id.in_(agent_user_ids),
                Order.order_source == "sales_agent",
                Order.created_at >= start_utc,
                Order.created_at < end_utc,
            )
            .group_by(Order.created_by_staff_id)
            .all()
        )
        for agent_user_id, count in order_rows:
            counters[agent_user_id]["orders_today"] = int(count or 0)
        outlet_rows = (
            db.session.query(
                Outlet.assigned_agent_user_id,
                *AgentMetricsService._assigned_active_columns(),
            )
            .filter(*AgentMetricsService._assigned_outlet_clauses(agent_user_ids))
            .group_by(Outlet.assigned_agent_user_id)
            .all()
        )
        for agent_user_id, assigned, active in outlet_rows:
            counters[agent_user_id]["assigned_outlets"] = int(assigned or 0)
            counters[agent_user_id]["active_outlets"] = int(active or 0)
        return counters
