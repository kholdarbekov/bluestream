"""Sales-agent pay statements (spec §4.8, §4.9, §4.13): what one agent is paid for one month.

- `build_input` gathers every figure a month needs into a `CalculatorInput`, and
  `pay_calculator.calculate` does the arithmetic. The open month's estimate and the frozen
  statement differ only in that input (the gate window, `is_estimate`), never in the formula (C12).
- `_write` puts a result on a `SalesPayStatement` row: the columns plus the `inputs` JSON a frozen
  statement is read back from, never recomputed. An open month's estimate is written into an
  UNSAVED row by the same `_write`, so every read model reads one shape for open and frozen months.
  The unsaved row only ever gets foreign-key integers, never a relationship, so no cascade can pull
  it into the session.
- `freeze` and `refreeze` are the only writers of `sales_pay_statements`; the callers hold the
  period lock (the lifecycle in `pay_period_service`, and every input written into a CLOSED month).
- The admin read models (A1, A2, A8, A9) sync an open month's agents first (`ensure_fresh`) and
  publish the as-of instant.
- Commission is a share of month-level tier totals (v5, §4.7): `build_input` hands the calculator
  the orders of every earned month the period touched (`_commission_orders`), and `_write` freezes
  each group's per-product tiers and each order's share in `inputs.groups`. A8 and S1 publish the
  products, A9 and S2 list orders (their share, their units, their lines this month), and a frozen
  month is read from those keys and never recomputed (I-34).

The owed balance (I-28, Q15) is not a table: it is the agent's latest approved statement's
`owed_amount` (`outstanding_owed`), and only the first unapproved month nets it.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from flask import current_app
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from business_app import db
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_pay import (
    SalesAgentPayTerms,
    SalesPayAdjustment,
    SalesPayLedgerLine,
    SalesPayOutletCheck,
    SalesPayPenalty,
    SalesPayPeriod,
    SalesPayStatement,
)
from business_app.models.user import User
from business_app.serializers.sales_serializers import person_ref
from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService
from business_app.services.sales.day_plan_service import AgentDayPlanService
from business_app.services.sales.pay_bonus_service import SalesPayBonusService
from business_app.services.sales.pay_calculator import (
    CALCULATOR_VERSION,
    AdjustmentView,
    CalculatorInput,
    LevelView,
    LineView,
    OrderView,
    OriginGate,
    OwedInView,
    PenaltyView,
    ProductChange,
    StatementResult,
    calculate,
    gate,
    month_tiers,
    tally,
)
from business_app.services.sales.pay_ledger_service import (
    OrderCreditState,
    SalesPayLedgerService,
    _credit_level,
    _line_level,
    _posted_lines_by_order,
)
from business_app.services.sales.pay_period_service import UNAPPROVED_STATUSES, SalesPayPeriodService
from business_app.services.sales.pay_plan_service import SalesPayPlanService, tiers_json
from business_app.services.sales.pay_rules import (
    ProductTiers,
    agent_placed_clause,
    basis_inputs,
    basis_inputs_from_snapshot,
    counted_contributions,
    earned_instant,
    follows_money,
    is_self,
    order_is_earned,
    product_names,
    units_by_product,
)
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.services.sales.visit_rules import NOT_VERIFIED_REASONS
from business_app.services.sales.work_calendar import WorkCalendarService
from business_app.utils import local_windows
from business_app.utils.api_responses import pagination_meta
from business_app.utils.constants import MAX_PAGE_SIZE
from business_app.utils.exceptions import ConflictError, NotFoundError
from business_app.utils.order_timing import delivered_instants_by_order
from business_app.utils.payment_projection import order_is_dead
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus
from shared.staff_constants import (
    SALES_PAY_COMMISSION_KINDS,
    SALES_PAY_DAY_STATUSES,
    SALES_PAY_LEDGER_KINDS,
    SALES_PAY_PERIOD_ACTIONS,
    SALES_PAY_PERIOD_STATUSES,
    SALES_PAY_PIPELINE_WAITING,
)

AGENT_STATEMENT_STATUSES = ("approved", "paid")  # "last statement" (C11), S3, S4; the owed balance's source
AGENT_STATEMENTS_SIZE = 12  # S3: a year of statements, newest first
# S3's item and S4's head: parts of `_last_statement`'s line for the same row, so the list, the
# statement screen and S1's last statement never disagree about a month.
AGENT_STATEMENT_ITEM_KEYS = ("month", "total", "status", "paid_on", "is_shadow")
AGENT_STATEMENT_HEAD_KEYS = ("month", "status", "paid_on", "is_shadow", "is_latest", "owed", "owed_netted_in")
NOT_COUNTED_REASONS = ("not_visited",) + NOT_VERIFIED_REASONS
AGENT_LINE_MONTH_STATUSES = ("open", "approved", "paid")  # months whose lines an agent may list (I-14)
EARNINGS_PAGE_SIZE = 10  # S2's page; the bot sends only the page number
# S2's item (§5.4, v5): the agent-safe part of an A9 row, an order or a bonus. The money
# breakdown, the tiers, the frozen items and the lines themselves stay on the admin drill-down.
AGENT_ROW_KEYS = (
    "row",
    "id",
    "date",
    "order_number",
    "outlet_name",
    "earned_month",
    "is_late",
    "counted",
    "kind",
    "cause",
    "tier_shift",
    "units",
    "amount",
)
# S1 lists what the agent is paid on or was told about: never a proposal, never a rejection.
AGENT_PENALTY_STATUSES = ("confirmed", "cancelled")
# §4.8 and §5.4: the 20 newest waiting orders and the 20 newest penalties. The pipeline's `count`
# and `estimated_total` still cover every waiting order.
PIPELINE_SIZE = 20
AGENT_PENALTIES_SIZE = 20

_ZERO = Decimal("0")
_fm = local_windows.format_month


def _day(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


class _Gathered(NamedTuple):
    """`build_input`'s answer plus the rows `_write` stores beside the figures."""

    inp: CalculatorInput
    terms: SalesAgentPayTerms
    unpaid: Dict[date, Optional[str]]
    window_end: date


def _line_views(period: SalesPayPeriod, agent_user_id: int) -> Tuple[LineView, ...]:
    rows = (
        SalesPayLedgerLine.query.filter_by(period_id=period.id, agent_user_id=agent_user_id)
        .order_by(SalesPayLedgerLine.id)
        .all()
    )
    return tuple(LineView(r.id, r.kind, r.cause, r.order_id, r.earned_month, r.amount) for r in rows)


def _order_view(rows: Sequence[Tuple[SalesPayLedgerLine, date]]) -> OrderView:
    """One order's commission lines (each with the month it was posted to), in id order, as the
    calculator reads them. The first line is the first credit: it supplies the frozen lines, the
    earned instant and `received_ref` (I-19); every line's level comes from `_line_level`."""
    first = rows[0][0]
    events = []
    for line, posted in rows:
        level = _line_level(line)
        events.append(LevelView(line.id, line.kind, line.cause, posted, level.applied, level.units, line.occurred_at))
    return OrderView(
        order_id=first.order_id,
        earned_month=first.earned_month,
        earned_instant=datetime.fromisoformat(first.snapshot["earned_instant"]),
        received_ref=Decimal(first.snapshot["received_ref"]),
        items=basis_inputs_from_snapshot(first.snapshot)[2],
        events=tuple(events),
    )


def _order_views(agent_user_id: int, months: Set[date], *, through: Optional[date]) -> Tuple[OrderView, ...]:
    """The agent's credited orders earned in `months`, each with its commission lines posted to a
    period up to `through` (every period when None). One query: its IN list is a handful of
    months, so it needs no chunking."""
    if not months:
        return ()
    query = (
        db.session.query(SalesPayLedgerLine, SalesPayPeriod.month_start)
        .join(SalesPayPeriod, SalesPayPeriod.id == SalesPayLedgerLine.period_id)
        .filter(
            SalesPayLedgerLine.agent_user_id == agent_user_id,
            SalesPayLedgerLine.kind.in_(SALES_PAY_COMMISSION_KINDS),
            SalesPayLedgerLine.earned_month.in_(sorted(months)),
        )
    )
    if through is not None:
        query = query.filter(SalesPayPeriod.month_start <= through)
    by_order: Dict[int, List[Tuple[SalesPayLedgerLine, date]]] = {}
    for line, posted in query.order_by(SalesPayLedgerLine.id).all():
        by_order.setdefault(line.order_id, []).append((line, posted))
    return tuple(_order_view(rows) for rows in by_order.values())


def _commission_orders(period: SalesPayPeriod, agent_user_id: int) -> Tuple[OrderView, ...]:
    """§4.8 `orders`: every order of every earned month that has a commission line in this period,
    with its lines in periods up to this one. A later period's line is never read, so re-freezing a
    closed month never sees a later correction (I-34)."""
    months = {
        month
        for (month,) in db.session.query(SalesPayLedgerLine.earned_month)
        .filter(
            SalesPayLedgerLine.period_id == period.id,
            SalesPayLedgerLine.agent_user_id == agent_user_id,
            SalesPayLedgerLine.kind.in_(SALES_PAY_COMMISSION_KINDS),
        )
        .distinct()
    }
    return _order_views(agent_user_id, months, through=period.month_start)


def _shadow_months() -> FrozenSet[date]:
    return frozenset(month for (month,) in db.session.query(SalesPayPeriod.month_start).filter_by(is_shadow=True))


def _origin_gates(agent_user_id: int, months: Set[date], *, fallback: Decimal) -> Dict[date, OriginGate]:
    """Q1: a late line is paid at the gate frozen on the agent's statement for the month it was
    earned in. With no statement there (no activity that month), the current month's multiplier is
    used, and the drill-down says so with `source: "current"` (§4.7)."""
    if not months:
        return {}
    frozen = {
        month: OriginGate(multiplier, "statement")
        for month, multiplier in db.session.query(SalesPayPeriod.month_start, SalesPayStatement.gate_multiplier)
        .join(SalesPayStatement, SalesPayStatement.period_id == SalesPayPeriod.id)
        .filter(SalesPayStatement.agent_user_id == agent_user_id, SalesPayPeriod.month_start.in_(sorted(months)))
    }
    return {month: frozen.get(month, OriginGate(fallback, "current")) for month in months}


def _estimate_window_end(month: date, now: datetime) -> date:
    """The last day an OPEN month's gate is measured to: today, or the month's last day once the
    month is over (§4.8, the "month" window My stats uses). S1 publishes it as `gate_window_end`."""
    return min(local_windows.local_date(now), local_windows.month_end(month))


def _gather(period: SalesPayPeriod, agent_user_id: int, *, now: datetime, estimate: bool) -> _Gathered:
    """Every input of one agent's month (§4.8 `build_input` windows).

    The estimate's gate window ends today, the freeze's at the month end; the base always uses the
    whole month. "Terms in force" is `version_for` resolving (terms AND a plan version), the same
    test the sync's `plan_for` makes, so a statement is never built for a month the ledger could not
    rate.
    """
    month = period.month_start
    last = local_windows.month_end(month)
    terms = SalesPayTermsService.terms_for(agent_user_id, month)
    plan = SalesPayPlanService.version_for(agent_user_id, month)
    if terms is None or plan is None:
        raise ConflictError(
            "The agent has no pay terms for this month",
            error_code="SALES_PAY_TERMS_MISSING",
            details={"agents": [int(agent_user_id)]},
        )
    window_end = _estimate_window_end(month, now) if estimate else last
    compliance = AgentDayPlanService.compliance(agent_user_id, month, window_end)
    unpaid = WorkCalendarService.unpaid_days(agent_user_id, month, last)
    employment_start, employment_end = WorkCalendarService.employment(agent_user_id)
    lines = _line_views(period, agent_user_id)
    orders = _commission_orders(period, agent_user_id)
    earlier = {order.earned_month for order in orders if order.earned_month < month}
    fallback = gate(compliance, plan, is_estimate=estimate).multiplier
    adjustments = tuple(
        AdjustmentView(
            row.id,
            row.amount,
            row.source,
            row.carried_from_period.month_start if row.carried_from_period_id is not None else None,
        )
        for row in SalesPayAdjustment.query.filter_by(period_id=period.id, agent_user_id=agent_user_id).order_by(
            SalesPayAdjustment.id
        )
    )
    penalties = tuple(
        PenaltyView(row.id, row.amount)
        for row in SalesPayPenalty.query.filter_by(
            period_id=period.id, agent_user_id=agent_user_id, status="confirmed"
        ).order_by(SalesPayPenalty.id)
    )
    owed_in = None
    if month == SalesPayPeriodService.first_unapproved_month():
        owed = SalesPayStatementService.outstanding_owed(agent_user_id)
        if owed is not None:
            owed_in = OwedInView(owed.id, owed.period.month_start, owed.owed_amount)
    inp = CalculatorInput(
        period_month=month,
        is_estimate=estimate,
        shadow_months=_shadow_months(),
        working_dates=WorkCalendarService.working_dates(month, last),
        worked_dates=WorkCalendarService.worked_dates(agent_user_id, month, last),
        holidays=WorkCalendarService.holidays(month, last),
        unpaid_dates=frozenset(unpaid),
        employment_start=employment_start,
        employment_end=employment_end,
        base_salary=terms.base_salary,
        plan=plan,
        compliance=compliance,
        lines=lines,
        orders=orders,
        # plan_for(E): fixed once E is closed (§4.10), so a re-freeze rates E as its statement did.
        origin_plans={
            earlier_month: SalesPayPlanService.version_for(agent_user_id, earlier_month)
            for earlier_month in sorted(earlier)
        },
        origin_gates=_origin_gates(agent_user_id, earlier, fallback=fallback),
        adjustments=adjustments,
        owed_in=owed_in,
        penalties=penalties,
    )
    return _Gathered(inp, terms, unpaid, window_end)


def _plan_json(inp: CalculatorInput) -> Dict[str, Any]:
    plan, config = inp.plan, inp.plan.config
    names = {
        product.id: product_names(product)
        for product in db.session.query(Product).filter(Product.id.in_(list(config.product_tiers)))
    }
    return {
        "plan_id": plan.plan_id,
        "plan_name": plan.plan_name,
        "version_id": plan.version_id,
        "version_no": plan.version_no,
        "effective_month": plan.effective_month.isoformat(),
        "default_tiers": tiers_json(config.default_tiers),
        "rates": [
            {"product_id": product_id, "product_name": names.get(product_id), "tiers": tiers_json(schedule)}
            for product_id, schedule in sorted(config.product_tiers.items())
        ],
        "gate": {
            "min_visits_due": config.gate_min_visits_due,
            "bands": [{"min_pct": str(b.min_pct), "multiplier": str(b.multiplier)} for b in config.gate_bands],
        },
        "bonus": {
            "amount": str(config.bonus_amount),
            "window_days": config.bonus_window_days,
            "min_orders_with_total": config.bonus_min_orders_with_total,
            "min_combined_total": str(config.bonus_min_combined_total),
            "min_orders_any_amount": config.bonus_min_orders_any_amount,
            "prior_customer_lookback_days": config.bonus_prior_customer_lookback_days,
        },
    }


def _tiers_json(tiers: Optional[ProductTiers]) -> List[Dict[str, Any]]:
    """A product's tiers holding units, ascending, as frozen (§4.8): pay amounts as whole-UZS ints,
    the rate and the net as stored decimals, `to_unit` as `TierSchedule.bounds()` gave it."""
    if tiers is None:
        return []
    return [
        {
            "from_unit": tier.from_unit,
            "to_unit": tier.to_unit,
            "mode": tier.rate.mode,
            "value": str(tier.rate.value),
            "units": tier.units,
            "net": str(tier.net),
            "amount_full": int(tier.amount_full),
            "amount": int(tier.amount),
        }
        for tier in tiers.tiers
    ]


def _segments_json(products: Sequence[Optional[ProductTiers]], order_id: int) -> List[Dict[str, Any]]:
    """One order's segments over a group's products, in product then allocation order."""
    return [
        {
            "product_id": segment.contribution.product_id,
            "from_unit": segment.from_unit,
            "units": segment.units,
            "net": str(segment.net),
            "share": int(segment.share),
        }
        for tiers in products
        if tiers is not None
        for segment in tiers.segments
        if segment.contribution.order_id == order_id
    ]


def _next_tier_json(tiers: ProductTiers) -> Optional[Dict[str, Any]]:
    """V5-B9: the first tier above the units counted (`ProductTiers.next_tier`), None at the top."""
    upcoming = tiers.next_tier()
    if upcoming is None:
        return None
    from_unit, units_to_go, rate = upcoming
    return {"from_unit": from_unit, "units_to_go": units_to_go, "mode": rate.mode, "value": str(rate.value)}


def _groups_json(inp: CalculatorInput, result: StatementResult) -> List[Dict[str, Any]]:
    """`inputs.groups` (§4.8, calculator v2): each group's per-product tiers before and after, and
    each listed order's share and segments, frozen so a closed A8 and A9 read them and never
    recompute them (I-34, V5-B5). `next_tier` is kept on an open month's estimate only (V5-B9)."""
    names = {
        (order.order_id, item.order_item_id): dict(item.product_name) for order in inp.orders for item in order.items
    }

    def name_of(product: ProductChange) -> Dict[str, str]:
        # The name frozen on the product's first contribution in allocation order (§4.8).
        first = (product.after or product.before).segments[0].contribution
        return names[(first.order_id, first.order_item_id)]

    groups = []
    for group in result.groups:
        own = group.month == inp.period_month
        groups.append(
            {
                "earned_month": _fm(group.month),
                "plan_version_id": group.plan_version_id,
                "counted": group.counted,
                "gross": str(group.gross),
                "multiplier": str(group.multiplier),
                "source": group.source,
                "gated": int(group.gated),
                "products": [
                    {
                        "product_id": product.product_id,
                        "product_name": name_of(product),
                        "uses_default_tiers": (product.after or product.before).uses_default,
                        "units": product.after.units if product.after else 0,
                        "total": int(product.after.total) if product.after else 0,
                        "units_before": None if own else (product.before.units if product.before else 0),
                        "total_before": None if own else (int(product.before.total) if product.before else 0),
                        "change": int(product.change),
                        "tiers": _tiers_json(product.after),
                        "tiers_before": None if own else _tiers_json(product.before),
                        "next_tier": _next_tier_json(product.after) if own and inp.is_estimate else None,
                    }
                    for product in group.products
                ],
                "orders": [
                    {
                        "order_id": order_id,
                        "share": int(share),
                        "share_before": None if share_before is None else int(share_before),
                        "segments": _segments_json([product.after for product in group.products], order_id),
                        "segments_before": (
                            None if own else _segments_json([product.before for product in group.products], order_id)
                        ),
                    }
                    for order_id, (share, share_before) in group.shares.items()
                ],
            }
        )
    return groups


def _write(
    statement: SalesPayStatement,
    period: SalesPayPeriod,
    gathered: _Gathered,
    result: StatementResult,
    *,
    computed_at: datetime,
    actor_id: Optional[int],
) -> None:
    """Put one calculation on a statement row: the columns, and the `inputs` JSON (§4.8) a frozen
    statement is read back from. Beyond the §4.8 keys, `inputs` also keeps what a frozen A8 prints
    and must never recompute: the plan's name, the unpaid days' notes, and the gate's figures."""
    inp, terms, g = gathered.inp, gathered.terms, result.gate
    decisions = SalesPayStatementService.self_decisions(period, statement.agent_user_id)
    statement.terms_id = terms.id
    statement.plan_version_id = inp.plan.version_id
    statement.computed_at = ensure_utc(computed_at)
    statement.computed_by_user_id = actor_id
    statement.base_salary = terms.base_salary
    statement.working_days = result.working_days
    statement.worked_days = result.worked_days
    statement.base_amount = result.base
    statement.commission_amount = result.commission
    statement.late_commission_amount = result.late_commission
    statement.gate_visits_due = g.due
    statement.gate_visits_counted = g.counted
    statement.gate_compliance_pct = Decimal(str(g.pct)) if g.pct is not None else None
    statement.gate_multiplier = g.multiplier
    statement.gated_commission_amount = result.gated_commission
    statement.bonus_amount = result.bonuses
    statement.penalty_amount = result.penalties
    statement.adjustment_amount = result.adjustments
    statement.variable_amount = result.variable
    statement.carry_in_amount = result.carry_in
    statement.owed_in_statement_id = result.owed_in_statement_id
    statement.gross_amount = result.gross_total
    statement.total_amount = result.total
    statement.carry_out_amount = result.carry_out
    statement.owed_amount = result.owed
    statement.inputs = {
        "calculator_version": CALCULATOR_VERSION,
        "terms": {
            "id": terms.id,
            "effective_month": terms.effective_month.isoformat(),
            "base_salary": str(terms.base_salary),
            "plan_id": terms.plan_id,
        },
        "plan": _plan_json(inp),
        "calendar": {
            "month_start": inp.period_month.isoformat(),
            "holidays": [{"date": day.isoformat(), "note": note} for day, note in sorted(inp.holidays.items())],
            "working_dates": sorted(day.isoformat() for day in inp.working_dates),
            "employment_start": _day(inp.employment_start),
            "employment_end": _day(inp.employment_end),
            "unpaid_dates": sorted(day.isoformat() for day in inp.unpaid_dates),
            "unpaid_days": [{"date": day.isoformat(), "note": note} for day, note in sorted(gathered.unpaid.items())],
            "worked_dates": sorted(day.isoformat() for day in inp.worked_dates),
        },
        "gate": {
            "short_visit_seconds": inp.compliance.min_seconds,
            "geofence_radius_m": int(current_app.config["SALES_GEOFENCE_RADIUS_M"]),
            "window_start": inp.period_month.isoformat(),
            "window_end": gathered.window_end.isoformat(),
            "rule": g.rule,
            "provisional": g.provisional,
            "due": g.due,
            "counted": g.counted,
            "pct": g.pct,
            "min_due": g.min_due,
            "band_min_pct": str(g.band_min_pct) if g.band_min_pct is not None else None,
            "multiplier": str(g.multiplier),
            "days": [
                {
                    "day": d.day.isoformat(),
                    "day_status": d.day_status,
                    "plan_source": d.plan_source,
                    "due": d.due,
                    "counted": d.counted,
                    "not_counted": dict(d.not_counted),
                    "legacy": d.legacy,
                }
                for d in inp.compliance.days
            ],
        },
        "groups": _groups_json(inp, result),
        "self_decisions": [
            {
                "input": d["input"],
                "id": d["id"],
                "action": d["action"],
                "at": d["at"].isoformat(),
                "date": _day(d["date"]),
            }
            for d in decisions
        ],
        "shadow_excluded_line_ids": list(result.shadow_excluded_line_ids),
        "lines": {
            "ledger_line_ids": [line.id for line in inp.lines],
            "penalty_ids": [penalty.id for penalty in inp.penalties],
            "adjustment_ids": [adjustment.id for adjustment in inp.adjustments],
        },
    }


def _activity_agent_ids(period: SalesPayPeriod) -> Set[int]:
    """Agents with something in the period: lines, adjustments, confirmed penalties, or already a
    statement. An owed balance is not activity (I-28)."""
    sources = (
        db.session.query(SalesPayLedgerLine.agent_user_id).filter_by(period_id=period.id),
        db.session.query(SalesPayAdjustment.agent_user_id).filter_by(period_id=period.id),
        db.session.query(SalesPayPenalty.agent_user_id).filter_by(period_id=period.id, status="confirmed"),
        db.session.query(SalesPayStatement.agent_user_id).filter_by(period_id=period.id),
    )
    return {agent_id for query in sources for (agent_id,) in query.distinct()}


def _employment_overlaps(start: Optional[date], end: Optional[date], month: date) -> bool:
    return (start is None or start <= local_windows.month_end(month)) and (end is None or end >= month)


class SalesPayStatementService:
    @staticmethod
    def build_input(period: SalesPayPeriod, agent_user_id: int, *, now: datetime, estimate: bool) -> CalculatorInput:
        return _gather(period, agent_user_id, now=now, estimate=estimate).inp

    @staticmethod
    def estimate(period: SalesPayPeriod, agent_user_id: int, *, now: datetime) -> StatementResult:
        """An OPEN month's live figures (C12): the calculator on the estimate window. Never stored."""
        return calculate(SalesPayStatementService.build_input(period, agent_user_id, now=now, estimate=True))

    @staticmethod
    def _estimate_row(period: SalesPayPeriod, agent_user_id: int, *, now: datetime) -> SalesPayStatement:
        """The estimate written into an UNSAVED statement by `_write`, so a read model reads one
        shape for open and frozen months. It is never added to the session."""
        gathered = _gather(period, agent_user_id, now=now, estimate=True)
        row = SalesPayStatement(period_id=period.id, agent_user_id=agent_user_id)
        _write(row, period, gathered, calculate(gathered.inp), computed_at=now, actor_id=None)
        return row

    @staticmethod
    def freeze(period: SalesPayPeriod, agent_user_id: int, *, actor_id: int, now: datetime) -> SalesPayStatement:
        """Compute the agent's month on the full window and store it: a new statement at revision 1,
        or the existing one at revision + 1 (one statement per period and agent). The caller holds
        the period lock and has already set the status (§4.9: status before freeze)."""
        gathered = _gather(period, agent_user_id, now=now, estimate=False)
        result = calculate(gathered.inp)
        statement = SalesPayStatement.query.filter_by(period_id=period.id, agent_user_id=agent_user_id).one_or_none()
        if statement is None:
            # Added only once written: `_write` queries (the self-decisions), and an autoflush of
            # a half-built row would trip its NOT NULL columns.
            statement = SalesPayStatement(period_id=period.id, agent_user_id=agent_user_id, revision=1)
            _write(statement, period, gathered, result, computed_at=now, actor_id=actor_id)
            db.session.add(statement)
        else:
            statement.revision += 1
            _write(statement, period, gathered, result, computed_at=now, actor_id=actor_id)
        db.session.flush()
        return statement

    @staticmethod
    def refreeze(
        period: SalesPayPeriod,
        agent_user_ids: Sequence[int],
        *,
        actor_id: int,
        now: datetime,
        cascade: bool = True,
    ) -> List[SalesPayStatement]:
        """Re-freeze the listed agents in `period` (§4.9 "Re-freeze on input change"), then, with
        `cascade`, in every later CLOSED period, in month order: those read this period's gate for
        their late lines. A listed agent who is eligible but has no statement yet gets one at
        revision 1; one who is not eligible is left alone. The later rows are locked here, after
        the caller's lock on `period`, so the locks stay in month order (§4.15)."""
        targets = [period]
        if cascade:
            later = SalesPayPeriodService.lock_periods_from(local_windows.next_month(period.month_start))
            targets += [row for row in later if row.status == "closed"]
        statements = []
        for target in targets:
            eligible = set(SalesPayStatementService.eligible_agent_ids(target))
            for agent_user_id in agent_user_ids:
                if int(agent_user_id) in eligible:
                    statements.append(
                        SalesPayStatementService.freeze(target, int(agent_user_id), actor_id=actor_id, now=now)
                    )
        return statements

    @staticmethod
    def eligible_agent_ids(period: SalesPayPeriod) -> List[int]:
        """Agents with terms in force for the month (`version_for` resolves) and either employment
        overlapping it or activity in the period (§4.8). A statement already in the period counts
        as activity, so a re-freeze never leaves a frozen statement stale."""
        month = period.month_start
        active = _activity_agent_ids(period)
        eligible = []
        for agent_user_id in SalesPayTermsService.agent_ids_with_terms():
            if SalesPayPlanService.version_for(agent_user_id, month) is None:
                continue
            start, end = WorkCalendarService.employment(agent_user_id)
            employed = start is not None and _employment_overlaps(start, end, month)
            if employed or agent_user_id in active:
                eligible.append(agent_user_id)
        return eligible

    @staticmethod
    def agents_missing_terms(period: SalesPayPeriod, *, skipped_order_ids: Sequence[int]) -> List[int]:
        """Agents the month should pay but cannot, because no terms are in force for it (§4.8):
        an active profile whose employment overlaps the month or is unset; activity in the period;
        or an order the sync skipped for want of terms that was earned in the month."""
        month = period.month_start
        candidates = set(_activity_agent_ids(period))
        for profile in SalesAgentProfile.query.filter_by(is_active=True):
            if _employment_overlaps(profile.employment_start_date, profile.employment_end_date, month):
                candidates.add(profile.user_id)
        if skipped_order_ids:
            delivered = delivered_instants_by_order(skipped_order_ids)
            for order in Order.query.filter(Order.id.in_(list(skipped_order_ids))):
                if (
                    order.id in delivered
                    and local_windows.local_month(earned_instant(order, delivered[order.id])) == month
                ):
                    candidates.add(order.created_by_staff_id)
        return sorted(
            agent_user_id
            for agent_user_id in candidates
            if agent_user_id is not None and SalesPayPlanService.version_for(agent_user_id, month) is None
        )

    @staticmethod
    def _approved_statements(agent_user_id: int):
        """The agent's statements in approved-or-paid months, newest month first, each with its
        period: the one selection S1's last statement, S3 and S4 read. A closed month is under
        review and never in it (I-14)."""
        return (
            SalesPayStatement.query.join(SalesPayPeriod, SalesPayStatement.period_id == SalesPayPeriod.id)
            .options(contains_eager(SalesPayStatement.period))
            .filter(
                SalesPayStatement.agent_user_id == agent_user_id,
                SalesPayPeriod.status.in_(AGENT_STATEMENT_STATUSES),
            )
            .order_by(SalesPayPeriod.month_start.desc())
        )

    @staticmethod
    def latest_approved_statement(agent_user_id: int) -> Optional[SalesPayStatement]:
        """The agent's statement in the latest approved-or-paid month, whatever it owes (§4.8)."""
        return SalesPayStatementService._approved_statements(agent_user_id).first()

    @staticmethod
    def outstanding_owed(agent_user_id: int) -> Optional[SalesPayStatement]:
        """The latest approved statement when it owes something, else None. Never "the latest
        statement that owes": once a later approved statement owes 0, the older balance was netted
        (T-OWED-2 (b))."""
        latest = SalesPayStatementService.latest_approved_statement(agent_user_id)
        return latest if latest is not None and latest.owed_amount > 0 else None

    @staticmethod
    def owed_to_date(agent_user_id: int) -> Dict[str, Any]:
        """A16 `owed_to_date`: the one outstanding balance, the month holding it, and the month that
        nets it (where a recorded repayment goes). Never a sum."""
        owed = SalesPayStatementService.outstanding_owed(agent_user_id)
        if owed is None:
            return {"amount": _ZERO, "month": None, "nets_in": None}
        nets_in = SalesPayPeriodService.first_unapproved_month()
        return {
            "amount": owed.owed_amount,
            "month": _fm(owed.period.month_start),
            "nets_in": _fm(nets_in) if nets_in is not None else None,
        }

    @staticmethod
    def carry_in_view(
        period: SalesPayPeriod,
        agent_user_id: int,
        *,
        carry_in_amount: Decimal,
        owed_in_statement_id: Optional[int],
    ) -> Dict[str, Any]:
        """The one labeller of a carry-in (§4.8), for an estimate and a frozen statement alike."""
        if owed_in_statement_id is not None:
            source = db.session.get(SalesPayStatement, owed_in_statement_id)
            return {"amount": carry_in_amount, "from_month": _fm(source.period.month_start), "source": "owed"}
        if carry_in_amount < 0:
            carry = SalesPayAdjustment.query.filter_by(
                period_id=period.id, agent_user_id=agent_user_id, source="carry_forward"
            ).one()
            return {
                "amount": carry_in_amount,
                "from_month": _fm(carry.carried_from_period.month_start),
                "source": "carry_forward",
            }
        return {"amount": carry_in_amount, "from_month": None, "source": None}

    @staticmethod
    def self_decisions(period: SalesPayPeriod, agent_user_id: int) -> List[Dict[str, Any]]:
        """Every input of the agent's month that the agent decided themselves (§4.13, I-20), sorted
        by `at`. An admin may decide about their own pay; the statement says so.

        One entry per self action: a direct penalty is one act (`created`), a proposal that the
        agent later confirmed is two (`proposed`, `confirmed`).
        """
        month = period.month_start
        entries: List[Dict[str, Any]] = []

        def add(kind: str, row_id: int, action: str, actor_id: Optional[int], at, day: Optional[date] = None) -> None:
            if at is not None and is_self(agent_user_id, actor_id):
                entries.append({"input": kind, "id": row_id, "action": action, "at": ensure_utc(at), "date": day})

        terms = SalesPayTermsService.terms_for(agent_user_id, month)
        if terms is not None:
            add("terms", terms.id, "added", terms.created_by_user_id, terms.created_at)
        profile = SalesAgentProfile.query.filter_by(user_id=agent_user_id).one_or_none()
        if profile is not None:
            add("employment", profile.id, "set", profile.employment_set_by_user_id, profile.employment_set_at)
        # Task 3's one row reader (PR10): nothing else queries sales_agent_unpaid_days.
        for row in WorkCalendarService.unpaid_day_rows(agent_user_id, month, local_windows.month_end(month)):
            add("unpaid_day", row.id, "set", row.created_by_user_id, row.created_at, row.unpaid_date)
        for row in SalesPayAdjustment.query.filter_by(
            period_id=period.id, agent_user_id=agent_user_id, source="admin"
        ).order_by(SalesPayAdjustment.id):
            add("adjustment", row.id, "created", row.created_by_user_id, row.created_at)
        for row in SalesPayPenalty.query.filter(
            SalesPayPenalty.period_id == period.id,
            SalesPayPenalty.agent_user_id == agent_user_id,
            SalesPayPenalty.status.in_(("confirmed", "cancelled")),
        ).order_by(SalesPayPenalty.id):
            if row.origin == "direct":
                add("penalty", row.id, "created", row.proposed_by_user_id, row.proposed_at)
            else:
                add("penalty", row.id, "proposed", row.proposed_by_user_id, row.proposed_at)
                add("penalty", row.id, "confirmed", row.decided_by_user_id, row.decided_at)
            add("penalty", row.id, "cancelled", row.cancelled_by_user_id, row.cancelled_at)
        order_ids: Set[int] = set()
        for line in SalesPayLedgerLine.query.filter_by(period_id=period.id, agent_user_id=agent_user_id):
            if line.kind in SALES_PAY_COMMISSION_KINDS:
                order_ids.add(line.order_id)
            else:
                order_ids.update(o["order_id"] for o in (line.snapshot or {}).get("qualifying_orders", []))
        for approval in (AgentOrderApprovalService.approved_rows(order_ids) if order_ids else {}).values():
            add("order_approval", approval.id, "approved", approval.decided_by_user_id, approval.decided_at)
        return sorted(entries, key=lambda entry: entry["at"])

    # ------------------------------------------------------------ admin read models (§5.2)

    @staticmethod
    def _read_period(
        month: date, agent_user_ids: Sequence[int], *, now: datetime
    ) -> Tuple[SalesPayPeriod, Optional[datetime]]:
        """The month's period, with an OPEN month's agents synced first (§5.2): returns the as-of
        instant, or None for a frozen month, whose figures never move. `ensure_periods` runs first
        so a month that began since the last sync is already listed."""
        SalesPayPeriodService.ensure_periods(now)
        period = SalesPayPeriodService.get(month)
        if period.status != "open":
            return period, None
        return period, SalesPayLedgerService.ensure_fresh(agent_user_ids, now=now)

    @staticmethod
    def _statements(period: SalesPayPeriod, *, now: datetime) -> List[SalesPayStatement]:
        """The month's statements: stored ones once it is closed, unsaved estimates while open."""
        if period.status == "open":
            return [
                SalesPayStatementService._estimate_row(period, agent_user_id, now=now)
                for agent_user_id in SalesPayStatementService.eligible_agent_ids(period)
            ]
        return SalesPayStatement.query.filter_by(period_id=period.id).order_by(SalesPayStatement.agent_user_id).all()

    @staticmethod
    def _find_statement(period: SalesPayPeriod, agent_user_id: int, *, now: datetime) -> Optional[SalesPayStatement]:
        """The agent's statement for the month: an unsaved estimate while it is open and the close
        will freeze one (`eligible_agent_ids`), the frozen row otherwise; None when there is none."""
        if period.status == "open":
            if agent_user_id in SalesPayStatementService.eligible_agent_ids(period):
                return SalesPayStatementService._estimate_row(period, agent_user_id, now=now)
            return None
        return SalesPayStatement.query.filter_by(period_id=period.id, agent_user_id=agent_user_id).one_or_none()

    @staticmethod
    def _statement_of(period: SalesPayPeriod, agent_user_id: int, *, now: datetime) -> SalesPayStatement:
        statement = SalesPayStatementService._find_statement(period, agent_user_id, now=now)
        if statement is None:
            raise NotFoundError(
                "This agent has no statement for this month",
                error_code="SALES_PAY_NOT_FOUND",
                details={"resource": "statement", "id": int(agent_user_id)},
            )
        return statement

    @staticmethod
    def period_list(*, now: Optional[datetime] = None) -> Dict[str, Any]:
        """A1: every month, newest first, unpaginated (a year is twelve rows)."""
        now = ensure_utc(now or local_windows.local_now())
        SalesPayPeriodService.ensure_periods(now)
        epoch = SalesPayPeriodService.epoch()
        periods = SalesPayPeriod.query.order_by(SalesPayPeriod.month_start.desc()).all()
        as_of = None
        if any(period.status == "open" for period in periods):
            as_of = SalesPayLedgerService.ensure_fresh(SalesPayTermsService.agent_ids_with_terms(), now=now)
        from business_app.services.sales.pay_penalty_service import SalesPayPenaltyService

        return {
            "started": epoch is not None,
            "startable_month": None if epoch is not None else _fm(local_windows.local_month(now)),
            "items": [
                _period_row(period, SalesPayStatementService._statements(period, now=now), now=now)
                for period in periods
            ],
            "statuses": list(SALES_PAY_PERIOD_STATUSES),
            "pending_penalty_count": SalesPayPenaltyService.pending_count(),
            "actions": list(SALES_PAY_PERIOD_ACTIONS),
            "as_of": as_of,
        }

    @staticmethod
    def period_detail(month: date, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """A2 (`PeriodDetail`), also the answer of A0 and A3-A7."""
        now = ensure_utc(now or local_windows.local_now())
        period, as_of = SalesPayStatementService._read_period(
            month, SalesPayTermsService.agent_ids_with_terms(), now=now
        )
        statements = SalesPayStatementService._statements(period, now=now)
        last = local_windows.month_end(month)
        holidays = WorkCalendarService.holidays(month, last)
        working = WorkCalendarService.working_dates(month, last)
        stats = period.last_sync_stats or {}
        missing = SalesPayStatementService.agents_missing_terms(
            period, skipped_order_ids=stats.get("skipped_no_terms", [])
        )
        names = _names([statement.agent_user_id for statement in statements] + missing)
        agents = [_agent_row(period, statement, names[statement.agent_user_id]) for statement in statements]
        return {
            **_period_row(period, statements, now=now),
            "holidays": list(period.holidays or []),
            "calendar": {
                "working_days": len(working),
                "days": [
                    {
                        "date": day,
                        "weekday": day.weekday(),
                        "is_working_day": day in working,
                        "holiday_note": holidays.get(day),
                    }
                    for day in _month_dates(month)
                ],
            },
            "agents": sorted(agents, key=lambda row: (row["agent_name"], row["agent_user_id"])),
            "unconfigured_agents": [{"agent_user_id": agent_id, "agent_name": names[agent_id]} for agent_id in missing],
            "sync": {"last_synced_at": period.last_synced_at, "stats": stats},
            "can_edit_inputs": period.status in UNAPPROVED_STATUSES,
            "closed_by": person_ref(period.closed_by),
            "approved_by": person_ref(period.approved_by),
            "paid_by": person_ref(period.paid_by),
            "as_of": as_of,
        }

    @staticmethod
    def _penalty_rows(month: date, agent_user_id: int, *, now: datetime) -> List[dict]:
        """A8 `penalties` (§5.2): the agent's penalties posted to `month`, confirmed or cancelled.

        They come in the admin row shape the Penalties tab uses, so the drawer and the tab
        cannot disagree about a row or its actions. A month holds a handful of penalties, far
        below the page cap.
        """
        from business_app.serializers.sales_pay_serializers import serialize_admin_penalty_row
        from business_app.services.sales.pay_penalty_service import SalesPayPenaltyService

        posted = SalesPayPenaltyService.list(agent_user_id=agent_user_id, month=month, page=1, per_page=MAX_PAGE_SIZE)
        return [serialize_admin_penalty_row(penalty, now=now) for penalty in posted["items"]]

    @staticmethod
    def statement_detail(month: date, agent_user_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """A8 (`StatementDetail`): live for an open month, frozen otherwise, one shape either way.

        `inputs`, `plan`, `summary` and `days` come from the statement's columns and `inputs` JSON,
        never re-derived; the lines are re-counted against the ids the statement froze as excluded
        (`tally`), which is safe because a closed period never receives another line.
        """
        now = ensure_utc(now or local_windows.local_now())
        period, as_of = SalesPayStatementService._read_period(month, [agent_user_id], now=now)
        statement = SalesPayStatementService._statement_of(period, agent_user_id, now=now)
        inputs = statement.inputs
        calendar, gate_json, plan_json = inputs["calendar"], inputs["gate"], inputs["plan"]
        lines = _line_views(period, agent_user_id)
        orders, _late, bonus_count = tally(
            lines, period_month=month, excluded_ids=frozenset(inputs["shadow_excluded_line_ids"])
        )
        user = db.session.get(User, agent_user_id)
        return {
            "month": _fm(month),
            "status": period.status,
            "is_estimate": period.status == "open",
            "is_shadow": period.is_shadow,
            "revision": statement.revision,
            "can_edit_inputs": period.status in UNAPPROVED_STATUSES,
            "self_decided": bool(inputs["self_decisions"]),
            "self_decisions": inputs["self_decisions"],
            "agent": {"user_id": user.id, "name": user.full_name, "phone": user.phone},
            "plan": {
                "plan_id": plan_json["plan_id"],
                "plan_name": plan_json["plan_name"],
                "version_id": plan_json["version_id"],
                "version_no": plan_json["version_no"],
                "effective_month": _fm(date.fromisoformat(plan_json["effective_month"])),
                "gate_bands": [
                    {"min_pct": Decimal(band["min_pct"]), "multiplier": Decimal(band["multiplier"])}
                    for band in plan_json["gate"]["bands"]
                ],
                "gate_min_due": plan_json["gate"]["min_visits_due"],
            },
            "inputs": {
                "base_salary": statement.base_salary,
                "employment_start": calendar["employment_start"],
                "employment_end": calendar["employment_end"],
                "working_days": statement.working_days,
                "worked_days": statement.worked_days,
                "holidays": calendar["holidays"],
                "unpaid_days": calendar["unpaid_days"],
                "short_visit_seconds": gate_json["short_visit_seconds"],
                "geofence_radius_m": gate_json["geofence_radius_m"],
            },
            "summary": _summary(period, statement, orders=orders, bonus_count=bonus_count),
            "days": [
                {
                    "date": row["day"],
                    "weekday": date.fromisoformat(row["day"]).weekday(),
                    "day_status": row["day_status"],
                    "plan_source": row["plan_source"],
                    "legacy": row["legacy"],
                    "due": row["due"],
                    "counted": row["counted"],
                    "not_counted": row["not_counted"],
                }
                for row in gate_json["days"]
            ],
            "day_statuses": list(SALES_PAY_DAY_STATUSES),
            "not_counted_reasons": list(NOT_COUNTED_REASONS),
            "new_outlets": _outlet_check_rows(period, agent_user_id),
            "adjustments": [
                SalesPayStatementService.adjustment_row(row)
                for row in SalesPayAdjustment.query.filter_by(
                    period_id=period.id, agent_user_id=agent_user_id
                ).order_by(SalesPayAdjustment.id)
            ],
            "penalties": SalesPayStatementService._penalty_rows(month, agent_user_id, now=now),
            "line_kinds": list(SALES_PAY_LEDGER_KINDS),
            "line_counts": {kind: sum(1 for line in lines if line.kind == kind) for kind in SALES_PAY_LEDGER_KINDS},
            "as_of": as_of,
        }

    @staticmethod
    def statement_lines(
        month: date, agent_user_id: int, *, kind: Optional[str], page: int, per_page: int
    ) -> Dict[str, Any]:
        """A9 (§5.2, v5): the orders behind the month's commission groups, then its bonus lines.
        Commission is a share of month-level tiers, so a row is an order of a group with its lines of
        this month as `events`, read from the statement (frozen, or the open month's estimate). A
        `kind` keeps the rows holding a line of that kind this month; one outside `line_kinds`
        matches nothing."""
        now = ensure_utc(local_windows.local_now())
        period, as_of = SalesPayStatementService._read_period(month, [agent_user_id], now=now)
        statement = SalesPayStatementService._statement_of(period, agent_user_id, now=now)
        refs, events = _drill_down_refs(period, statement)
        if kind is not None:
            refs = [ref for ref in refs if kind in ref.kinds]
        shown = refs[(page - 1) * per_page : page * per_page]
        return {
            "items": _drill_down_rows(period, statement, shown, events),
            "meta": pagination_meta(page, per_page, len(refs)),
            "kind": kind,
            "line_kinds": list(SALES_PAY_LEDGER_KINDS),
            "as_of": as_of,
        }

    @staticmethod
    def adjustment_row(adjustment: SalesPayAdjustment) -> Dict[str, Any]:
        """`AdjustmentRow` (§5.2), for A8's list and A11's answer. A carry row is labelled by its
        source and `carried_from_month`, never by its stored system reason."""
        return {
            "id": adjustment.id,
            "amount": adjustment.amount,
            "reason": adjustment.reason,
            "source": adjustment.source,
            "carried_from_month": (
                _fm(adjustment.carried_from_period.month_start)
                if adjustment.carried_from_period_id is not None
                else None
            ),
            "posting_month": _fm(adjustment.period.month_start),
            "created_at": adjustment.created_at,
            "created_by": person_ref(adjustment.created_by),
        }

    # ------------------------------------------------------------ agent read models (§5.4)

    @staticmethod
    def _open_estimates(
        agent_user_id: int, periods: Sequence[SalesPayPeriod], *, now: datetime
    ) -> List[Tuple[SalesPayPeriod, Optional[SalesPayStatement]]]:
        """S1's open months among `periods`, in their order, each with the agent's unsaved estimate
        or None. S4 reads the same pairs for the one row they can net (`outstanding_owed`), so
        `owed_netted_in` has one source.

        "Configured" is `version_for` resolving: the test `_gather` and `eligible_agent_ids` make.
        No terms: the month is listed with `configured: false`, a state. Terms, but no statement
        to come (final-review M10, owner decision 2026-10-05): a leaver's month with no activity
        is one the close freezes nothing for, so it is omitted rather than estimated, and no
        `owed_netted_in` promises a deduction there. The predicate is the close's own.
        """
        estimates = []
        for period in periods:
            if period.status != "open":
                continue
            if SalesPayPlanService.version_for(agent_user_id, period.month_start) is None:
                estimates.append((period, None))
            elif agent_user_id in SalesPayStatementService.eligible_agent_ids(period):
                estimates.append((period, SalesPayStatementService._estimate_row(period, agent_user_id, now=now)))
        return estimates

    @staticmethod
    def agent_earnings(agent_user_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """S1: the agent's own pay screen. Every figure, month and state the bot prints is here.

        Open months are synced first (`ensure_fresh`, throttled per agent) and estimated by the
        calculator (C12) when the close would freeze a statement for the agent
        (`eligible_agent_ids`); an agent with no terms in force for a month sees
        `configured: false`, a state, and a month with terms but no statement to come (a leaver
        with no activity) is not listed (M10). CLOSED months are named with no numbers (I-14).
        The last statement is the row `outstanding_owed` reads, so its `owed` IS the outstanding
        balance (I-28), and `owed_netted_in` names the open month whose estimate nets it.
        """
        now = ensure_utc(now or local_windows.local_now())
        SalesPayPeriodService.ensure_periods(now)
        if SalesPayPeriodService.epoch() is None:
            return {
                "state": "not_started",
                "as_of": now,
                "open_months": [],
                "in_review": [],
                "last_statement": None,
                "pipeline": None,
                "penalties": [],
            }
        as_of = SalesPayLedgerService.ensure_fresh([agent_user_id], now=now)
        periods = (
            SalesPayPeriod.query.filter(SalesPayPeriod.status.in_(("open", "closed")))
            .order_by(SalesPayPeriod.month_start.desc())
            .all()
        )
        estimates = SalesPayStatementService._open_estimates(agent_user_id, periods, now=now)
        last = SalesPayStatementService.latest_approved_statement(agent_user_id)
        penalty_period_ids = [period.id for period, _row in estimates] + ([last.period_id] if last is not None else [])
        return {
            "state": "ok",
            "as_of": as_of,
            "open_months": [_open_month(period, row, now=now) for period, row in estimates],
            "in_review": [
                {"month": _fm(period.month_start), "status": period.status}
                for period in periods
                if period.status == "closed"
            ],
            "last_statement": _last_statement(last, estimates, latest=last),
            "pipeline": SalesPayStatementService.pipeline(agent_user_id, now=now),
            "penalties": _agent_penalties(agent_user_id, penalty_period_ids),
        }

    @staticmethod
    def agent_lines(agent_user_id: int, month: date, *, page: int) -> Dict[str, Any]:
        """S2: A9's rows for the agent's own month, EARNINGS_PAGE_SIZE to a page, in the agent's
        shape (`AGENT_ROW_KEYS`).

        Only months in AGENT_LINE_MONTH_STATUSES are shown (I-14). A CLOSED month is under review
        and a month with no period has nothing; both answer `available: false` with no items, so a
        button drawn before a close and tapped after it gets an empty page, never an error. An open
        month is synced first, as S1 and A9 are; a month the agent has no statement in lists nothing.
        """
        period = SalesPayPeriod.query.filter_by(month_start=month).one_or_none()
        answer = {
            "month": _fm(month),
            "status": period.status if period is not None else None,
            "available": False,
            "page": page,
            "has_more": False,
            "items": [],
        }
        if period is None or period.status not in AGENT_LINE_MONTH_STATUSES:
            return answer
        now = ensure_utc(local_windows.local_now())
        if period.status == "open":
            SalesPayLedgerService.ensure_fresh([agent_user_id], now=now)
        statement = SalesPayStatementService._find_statement(period, agent_user_id, now=now)
        if statement is None:
            return {**answer, "available": True}
        refs, events = _drill_down_refs(period, statement)
        start = (page - 1) * EARNINGS_PAGE_SIZE
        rows = _drill_down_rows(period, statement, refs[start : start + EARNINGS_PAGE_SIZE], events)
        return {
            **answer,
            "available": True,
            "has_more": len(refs) > start + EARNINGS_PAGE_SIZE,
            "items": [_agent_item(row) for row in rows],
        }

    @staticmethod
    def agent_statements(agent_user_id: int, *, limit: int = AGENT_STATEMENTS_SIZE) -> Dict[str, Any]:
        """S3: the agent's own approved-or-paid statements, newest month first, at most `limit`.

        The rows S1's last statement is chosen from (`_approved_statements`), so a month under
        review is never listed (I-14). Each item is part of `_last_statement`'s line for its row
        (`AGENT_STATEMENT_ITEM_KEYS`); S3 publishes no `owed_netted_in`, so no open month is
        estimated for it.
        """
        statements = SalesPayStatementService._approved_statements(agent_user_id).limit(limit).all()
        latest = statements[0] if statements else None  # the selection's first row: `latest_approved_statement`
        lines = [_last_statement(statement, (), latest=latest) for statement in statements]
        return {"items": [{key: line[key] for key in AGENT_STATEMENT_ITEM_KEYS} for line in lines]}

    @staticmethod
    def agent_statement(agent_user_id: int, month: date, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """S4: one of the agent's own approved-or-paid statements in full.

        `statement` is `_agent_estimate` of the frozen row: S1's estimate shape, A8's figures. The
        head is `_last_statement`'s line for the same row (`AGENT_STATEMENT_HEAD_KEYS`), so its
        `is_latest`, `owed` and `owed_netted_in` are the ones S1 publishes when this is the last
        statement; an older row is `is_latest: false`, its `owed` and `carry_out` history.

        Only the agent's outstanding balance (`outstanding_owed`: the latest statement, when it
        owes) can be netted by an open estimate, so only that row is read against S1's own
        open-month estimates after S1's own sync. Any other row's `owed_netted_in` is None whatever
        an estimate says, so S4 syncs and estimates nothing for it: a frozen read does not sync
        (`_read_period`). A month that is open, closed (under review, I-14) or has no period, or
        that holds no statement of the agent's, is SALES_PAY_STATEMENT_NOT_AVAILABLE.
        """
        now = ensure_utc(now or local_windows.local_now())
        statement = (
            SalesPayStatementService._approved_statements(agent_user_id)
            .filter(SalesPayPeriod.month_start == month)
            .one_or_none()
        )
        if statement is None:
            raise NotFoundError(
                "There is no approved statement for this month",
                error_code="SALES_PAY_STATEMENT_NOT_AVAILABLE",
                details={"month": _fm(month)},
            )
        latest = SalesPayStatementService.latest_approved_statement(agent_user_id)
        owed = SalesPayStatementService.outstanding_owed(agent_user_id)
        estimates: Sequence[Tuple[SalesPayPeriod, Optional[SalesPayStatement]]] = ()
        if owed is not None and owed.id == statement.id:
            SalesPayPeriodService.ensure_periods(now)
            SalesPayLedgerService.ensure_fresh([agent_user_id], now=now)
            open_periods = (
                SalesPayPeriod.query.filter_by(status="open").order_by(SalesPayPeriod.month_start.desc()).all()
            )
            estimates = SalesPayStatementService._open_estimates(agent_user_id, open_periods, now=now)
        line = _last_statement(statement, estimates, latest=latest)
        return {
            **{key: line[key] for key in AGENT_STATEMENT_HEAD_KEYS},
            "statement": _agent_estimate(statement.period, statement),
        }

    @staticmethod
    def pipeline(agent_user_id: int, *, now: datetime) -> Dict[str, Any]:
        """The agent's placed orders still waiting to be counted (§4.8, C9). Never part of a total.

        An order waits when it was placed within SALES_PAY_SYNC_LOOKBACK_DAYS, is not dead
        (`order_is_dead`), has no open credit episode, and its credit was not reversed in a month
        that is now settled (I-18: a settled reversal is final). A never-credited order that is
        already earned (`order_is_earned`, the sync's first-credit gate) waits for nothing: the
        sync skipped it for good (earned before pay started, no terms, no delivered row), so it
        is not "not counted yet" (S-6). A reversed order is left to the money rule: its re-credit
        waits on money received, not on `is_paid`. Its estimate is its share when stacked on what
        its earned month already counts (I-36, `_pipeline_estimates`).
        """
        since = now - timedelta(days=int(current_app.config["SALES_PAY_SYNC_LOOKBACK_DAYS"]))
        placed = (
            Order.query.options(selectinload(Order.order_items).selectinload(OrderItem.product))
            .filter(agent_placed_clause([agent_user_id]), Order.created_at >= since)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .all()
        )
        live = [order for order in placed if not order_is_dead(order)]
        posted = _posted_lines_by_order(agent_user_id, [order.id for order in live])
        waiting = []
        for order in live:
            state = OrderCreditState(posted.get(order.id, []))
            if state.credited or state.settled_reversed:
                continue
            if state.first_credit is None and order_is_earned(order):
                continue
            waiting.append((order, state))
        estimates = _pipeline_estimates(agent_user_id, waiting, now=now)
        shown = waiting[:PIPELINE_SIZE]
        awaiting = AgentOrderApprovalService.awaiting_ids([order.id for order, _state in shown])
        outlet_names = _order_outlet_names([order for order, _state in shown])
        return {
            "count": len(waiting),
            "estimated_total": sum((amount for amount in estimates.values() if amount is not None), _ZERO),
            "items": [
                {
                    "order_number": order.order_number,
                    "outlet_name": outlet_names.get(order.id),
                    "placed_on": local_windows.local_date(order.created_at),
                    "waiting_for": _waiting_for(order, awaiting),
                    "total": order.total_amount,
                    "estimated_commission": estimates[order.id],
                }
                for order, _state in shown
            ],
        }


# ----------------------------------------------------------------- read-model rows


def _month_dates(month: date) -> List[date]:
    last = local_windows.month_end(month)
    return [month + timedelta(days=offset) for offset in range((last - month).days + 1)]


def _names(user_ids: Sequence[int]) -> Dict[int, str]:
    ids = set(user_ids)
    return {user.id: user.full_name for user in User.query.filter(User.id.in_(ids))} if ids else {}


def _period_row(period: SalesPayPeriod, statements: Sequence[SalesPayStatement], *, now: datetime) -> Dict[str, Any]:
    """`PeriodRow` (§5.2). `totals.total` is the sum of the PAID totals (C1); it equals base +
    variable only when nobody has a carry-in or a shortfall."""
    return {
        "month": _fm(period.month_start),
        "status": period.status,
        "is_shadow": period.is_shadow,
        "start_date": period.month_start,
        "end_date": local_windows.month_end(period.month_start),
        "is_estimate": period.status == "open",
        "agent_count": len(statements),
        "totals": {
            "base": sum((statement.base_amount for statement in statements), _ZERO),
            "variable": sum((statement.variable_amount for statement in statements), _ZERO),
            "total": sum((statement.total_amount for statement in statements), _ZERO),
        },
        "next_actions": SalesPayPeriodService.allowed_actions(period, now=now),
        "closable_from": SalesPayPeriodService.closable_from(period.month_start),
        "closed_at": period.closed_at,
        "approved_at": period.approved_at,
        "paid_on": period.paid_on,
        "last_synced_at": period.last_synced_at,
    }


def _agent_row(period: SalesPayPeriod, statement: SalesPayStatement, name: str) -> Dict[str, Any]:
    """A2 `agents[]`: one agent's month in the month view."""
    inputs = statement.inputs
    month = _fm(period.month_start)
    lines = _line_views(period, statement.agent_user_id)
    _orders, late_lines, _bonuses = tally(
        lines, period_month=period.month_start, excluded_ids=frozenset(inputs["shadow_excluded_line_ids"])
    )
    checks = _outlet_check_rows(period, statement.agent_user_id)
    return {
        "agent_user_id": statement.agent_user_id,
        "agent_name": name,
        "self_decided": bool(inputs["self_decisions"]),
        "statement_id": statement.id,
        "revision": statement.revision,
        "worked_days": statement.worked_days,
        "working_days": statement.working_days,
        "base_salary": statement.base_salary,
        "base_amount": statement.base_amount,
        "commission": statement.commission_amount,
        "late_commission_after_gate": sum(
            (Decimal(group["gated"]) for group in inputs["groups"] if group["earned_month"] != month), _ZERO
        ),
        "compliance_pct": inputs["gate"]["pct"],
        "visits_due": statement.gate_visits_due,
        "visits_counted": statement.gate_visits_counted,
        "gate_multiplier": statement.gate_multiplier,
        "gated_commission": statement.gated_commission_amount,
        "new_outlets": statement.bonus_amount,
        "adjustments": statement.adjustment_amount,
        "penalties": statement.penalty_amount,
        "variable": statement.variable_amount,
        "carry_in": statement.carry_in_amount,
        "gross_total": statement.gross_amount,
        "total": statement.total_amount,
        "carry_out": statement.carry_out_amount,
        "owed": statement.owed_amount,
        "late_lines": late_lines,
        "review_flags": sum(1 for row in checks if SalesPayBonusService.has_review_flag(row["review_flags"])),
    }


def _tier_view(tier: Mapping[str, Any]) -> Dict[str, Any]:
    """A frozen tier as A8 publishes it (§5.2): the rate and the net as numbers, pay in whole UZS."""
    return {
        "from_unit": tier["from_unit"],
        "to_unit": tier["to_unit"],
        "units": tier["units"],
        "mode": tier["mode"],
        "value": Decimal(tier["value"]),
        "net": Decimal(tier["net"]),
        "amount_full": Decimal(tier["amount_full"]),
        "amount": Decimal(tier["amount"]),
    }


def _product_view(product: Mapping[str, Any]) -> Dict[str, Any]:
    """A8 `summary.commission.products[]` (§5.2): one product of the own group."""
    upcoming = product["next_tier"]
    return {
        "product_id": product["product_id"],
        "product_name": product["product_name"],
        "uses_default_tiers": product["uses_default_tiers"],
        "units": product["units"],
        "total": Decimal(product["total"]),
        "tiers": [_tier_view(tier) for tier in product["tiers"]],
        "next_tier": (
            None
            if upcoming is None
            else {
                "from_unit": upcoming["from_unit"],
                "units_to_go": upcoming["units_to_go"],
                "mode": upcoming["mode"],
                "value": Decimal(upcoming["value"]),
            }
        ),
    }


def _late_product_view(product: Mapping[str, Any]) -> Dict[str, Any]:
    """A8 `summary.late[].products[]` (§5.2): what a late group did to one product (I-34)."""
    return {
        "product_id": product["product_id"],
        "product_name": product["product_name"],
        "units_before": product["units_before"],
        "units": product["units"],
        "total_before": Decimal(product["total_before"]),
        "total": Decimal(product["total"]),
        "change": Decimal(product["change"]),
        "tiers_before": [_tier_view(tier) for tier in product["tiers_before"]],
        "tiers": [_tier_view(tier) for tier in product["tiers"]],
    }


def _summary(period: SalesPayPeriod, statement: SalesPayStatement, *, orders: int, bonus_count: int) -> Dict[str, Any]:
    """A8 `summary`: the stored figures and the stored groups, gate and carry-in label."""
    inputs = statement.inputs
    month = _fm(period.month_start)
    gate_json, groups = inputs["gate"], inputs["groups"]
    current = next((group for group in groups if group["earned_month"] == month), None)
    band = (
        None
        if gate_json["band_min_pct"] is None
        else {"min_pct": Decimal(gate_json["band_min_pct"]), "multiplier": Decimal(gate_json["multiplier"])}
    )
    return {
        "base_amount": statement.base_amount,
        "commission": {
            "gross": statement.commission_amount,
            "orders": orders,
            "after_gate": Decimal(current["gated"]) if current is not None else _ZERO,
            "products": [_product_view(product) for product in current["products"]] if current is not None else [],
        },
        "late": [
            {
                "earned_month": group["earned_month"],
                "plan_version_id": group["plan_version_id"],
                # I-16: False for a shadow month's group, which is shown at its own tiers but adds 0.
                "counted": group["counted"],
                "gross": Decimal(group["gross"]),
                "multiplier": Decimal(group["multiplier"]),
                "source": group["source"],
                "after_gate": Decimal(group["gated"]),
                "products": [_late_product_view(product) for product in group["products"]],
            }
            for group in groups
            if group["earned_month"] != month
        ],
        "gate": {
            "compliance_pct": gate_json["pct"],
            "visits_due": gate_json["due"],
            "visits_counted": gate_json["counted"],
            "multiplier": Decimal(gate_json["multiplier"]),
            "rule": gate_json["rule"],
            "band": band,
            "provisional": gate_json["provisional"],
        },
        "gated_commission": statement.gated_commission_amount,
        "new_outlets": {"amount": statement.bonus_amount, "count": bonus_count},
        "adjustments": statement.adjustment_amount,
        "penalties": statement.penalty_amount,
        "variable": statement.variable_amount,
        "carry_in": SalesPayStatementService.carry_in_view(
            period,
            statement.agent_user_id,
            carry_in_amount=statement.carry_in_amount,
            owed_in_statement_id=statement.owed_in_statement_id,
        ),
        "gross_total": statement.gross_amount,
        "total": statement.total_amount,
        "carry_out": statement.carry_out_amount,
        "owed": statement.owed_amount,
    }


def _check_concerns(check: SalesPayOutletCheck, period: SalesPayPeriod, start: datetime, end: datetime) -> bool:
    """Whether an outlet check belongs in this month's drawer: a qualified check where its bonus
    line is posted; any other check whose window overlaps the month; a window-less check
    activated in the month, or still tracking since before it."""
    if check.ledger_line is not None:
        return check.ledger_line.period_id == period.id
    if check.window_start is not None:
        return ensure_utc(check.window_start) < end and ensure_utc(check.window_end) > start
    if check.activated_at is None:
        return False
    activated = ensure_utc(check.activated_at)
    return activated < end and (activated >= start or check.status == "tracking")


def _check_row(check: SalesPayOutletCheck) -> Dict[str, Any]:
    """A8 `new_outlets[]`. A qualified row shows its bonus line's qualifying orders; a
    prior-customer row the orders that made the store a prior customer; others none."""
    line = check.ledger_line
    evidence = check.evidence or {}
    snapshot = line.snapshot if line is not None else {}
    if line is not None:
        orders = [
            {
                "order_id": order["order_id"],
                "order_number": order["order_number"],
                "order_source": order["order_source"],
                "earned_instant": order["earned_instant"],
                "total": Decimal(str(order["total_amount"])),
                "staff_approved": order["staff_approved"],
            }
            for order in snapshot.get("qualifying_orders", [])
        ]
    elif check.status == "prior_customer":
        orders = list((evidence.get("prior_customer") or {}).get("orders", []))
    else:
        orders = []
    return {
        "outlet_id": check.outlet_id,
        "outlet_name": check.outlet.name,
        "status": check.status,
        "activation_reason": check.activation_reason,
        "onboarded_at": check.onboarded_at,
        "window_start": check.window_start,
        "window_end": check.window_end,
        # The frozen evaluation version's window (I-9, PR9); a version-less check has none.
        "window_days": check.plan_version.bonus_window_days if check.plan_version is not None else None,
        "window_opened_by": evidence.get("window_opened_by"),
        "rule": snapshot.get("rule_met"),
        "amount": line.amount if line is not None else None,
        "is_late": SalesPayPeriodService.is_late(line.earned_month, line.period) if line is not None else None,
        "review_flags": SalesPayBonusService.review_flags(check, line),
        "orders": orders,
    }


def _outlet_check_rows(period: SalesPayPeriod, agent_user_id: int) -> List[Dict[str, Any]]:
    start, end = local_windows.month_bounds(period.month_start)
    checks = SalesPayOutletCheck.query.filter_by(agent_user_id=agent_user_id).order_by(SalesPayOutletCheck.id)
    return [_check_row(check) for check in checks if _check_concerns(check, period, start, end)]


def _order_outlet_names(orders: Sequence[Order]) -> Dict[int, str]:
    """order id -> the name of the outlet it was delivered to: the account's outlet at the order's
    delivery address, else the account's first outlet (a branch account has several)."""
    user_ids = {order.user_id for order in orders}
    outlets = Outlet.query.filter(Outlet.user_id.in_(user_ids)).order_by(Outlet.id).all() if user_ids else []
    names = {}
    for order in orders:
        mine = [outlet for outlet in outlets if outlet.user_id == order.user_id]
        pinned = next((outlet for outlet in mine if outlet.address_id == order.delivery_address_id), None)
        chosen = pinned or (mine[0] if mine else None)
        if chosen is not None:
            names[order.id] = chosen.name
    return names


def _dec(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


# A9 `items[]` of an order row: its first credit's frozen lines (I-19), never re-rated.
_ITEM_KEYS = ("product_id", "product_name", "quantity", "unit_price", "total_price", "discount_share", "net")
_ITEM_MONEY = frozenset({"unit_price", "total_price", "discount_share", "net"})
_REVERSAL = SALES_PAY_LEDGER_KINDS[1]
_BONUS_KIND = SALES_PAY_LEDGER_KINDS[3]


class _RowRef(NamedTuple):
    """One drill-down row before it is built: an order of a group, or a bonus line."""

    group: Optional[Mapping[str, Any]]
    entry: Optional[Mapping[str, Any]]  # the group's frozen `orders[]` entry
    bonus: Optional[SalesPayLedgerLine]
    kinds: FrozenSet[str]  # the kinds of its lines posted to this month (A9's `kind` filter)


def _drill_down_refs(
    period: SalesPayPeriod, statement: SalesPayStatement
) -> Tuple[List[_RowRef], Dict[int, List[SalesPayLedgerLine]]]:
    """A9's rows in their order (§5.2): the own group's orders, then each late group's by earned
    month, each in allocation order as frozen; then the bonus lines by date. Also returns each
    order's commission lines of this month (its `events`)."""
    lines = (
        SalesPayLedgerLine.query.options(joinedload(SalesPayLedgerLine.outlet))
        .filter_by(period_id=period.id, agent_user_id=statement.agent_user_id)
        .order_by(SalesPayLedgerLine.id)
        .all()
    )
    events: Dict[int, List[SalesPayLedgerLine]] = {}
    for line in lines:
        if line.kind in SALES_PAY_COMMISSION_KINDS:
            events.setdefault(line.order_id, []).append(line)
    month = _fm(period.month_start)
    groups = statement.inputs["groups"]
    ordered = [group for group in groups if group["earned_month"] == month] + [
        group for group in groups if group["earned_month"] != month
    ]
    refs = [
        _RowRef(group, entry, None, frozenset(line.kind for line in events.get(entry["order_id"], [])))
        for group in ordered
        for entry in group["orders"]
    ]
    bonuses = sorted(
        (line for line in lines if line.kind == _BONUS_KIND),
        key=lambda line: (local_windows.local_date(line.occurred_at), line.id),
    )
    refs += [_RowRef(None, None, line, frozenset({line.kind})) for line in bonuses]
    return refs, events


def _drill_down_rows(
    period: SalesPayPeriod,
    statement: SalesPayStatement,
    refs: Sequence[_RowRef],
    events: Mapping[int, List[SalesPayLedgerLine]],
) -> List[Dict[str, Any]]:
    """Build one page of rows: three queries whatever its length (every line of its orders, the
    orders, their outlets)."""
    order_ids = [ref.entry["order_id"] for ref in refs if ref.entry is not None]
    posted = _posted_lines_by_order(statement.agent_user_id, order_ids)
    orders = Order.query.filter(Order.id.in_(order_ids)).all() if order_ids else []
    outlet_names = _order_outlet_names(orders)
    excluded = frozenset(statement.inputs["shadow_excluded_line_ids"])
    return [
        (
            _bonus_row(ref.bonus, period, excluded_ids=excluded)
            if ref.entry is None
            else _order_row(
                period,
                ref.group,
                ref.entry,
                lines=posted[ref.entry["order_id"]],
                events=events.get(ref.entry["order_id"], []),
                outlet_name=outlet_names.get(ref.entry["order_id"]),
            )
        )
        for ref in refs
    ]


def _segment_view(segment: Mapping[str, Any], tiers: Mapping[Tuple[int, int], Mapping[str, Any]]) -> Dict[str, Any]:
    """An order's part of one tier (A9 `products[].tiers[]`), with the tier's published bounds and rate."""
    tier = tiers[(segment["product_id"], segment["from_unit"])]
    return {
        "from_unit": segment["from_unit"],
        "to_unit": tier["to_unit"],
        "units": segment["units"],
        "mode": tier["mode"],
        "value": Decimal(tier["value"]),
        "share": Decimal(segment["share"]),
    }


def _received(line: SalesPayLedgerLine) -> Dict[str, Optional[Decimal]]:
    """The money a commission line recorded (§3.2): what had been received when it was written (a
    reversal's `observed.received`) and the level it applied (none on a reversal, I-18). One reader
    for an event and for an order row's `money`."""
    snapshot = line.snapshot or {}
    if line.kind == _REVERSAL:
        return {"received": _dec((snapshot.get("observed") or {}).get("received")), "received_applied": None}
    return {"received": _dec(snapshot.get("received")), "received_applied": _dec(snapshot.get("received_applied"))}


def _event_row(line: SalesPayLedgerLine) -> Dict[str, Any]:
    """A line of this month in an order row's `events` (§5.2). `is_recredit` is the sync's snapshot
    marker (PR8); `follows_money` is the D-Q3 display rule (`pay_rules.follows_money`, T14-R1), both
    published so the UI never derives them."""
    snapshot = line.snapshot or {}
    is_recredit = line.kind == "commission_credit" and bool(snapshot.get("recredit"))
    return {
        "id": line.id,
        "kind": line.kind,
        "cause": line.cause,
        "is_recredit": is_recredit,
        "follows_money": follows_money(line.kind, line.cause, is_recredit=is_recredit),
        "occurred_at": line.occurred_at,
        "date": local_windows.local_date(line.occurred_at),
        **_received(line),
    }


def _order_row(
    period: SalesPayPeriod,
    group: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    lines: Sequence[SalesPayLedgerLine],
    events: Sequence[SalesPayLedgerLine],
    outlet_name: Optional[str],
) -> Dict[str, Any]:
    """`AdminOrderRow` (§5.2): one order's place in one group of this statement.

    The figures are the group's frozen entry (`inputs.groups[].orders`), never re-derived; the
    names, frozen items, money and units come from the immutable ledger rows, read at this period's
    cut (a later month's line is never shown). `amount` is the row's part of the group's gross:
    the share in the own month, `share − share_before` in a late group. A tier-shift row has no
    line this month and a moved share: another order of its month left or joined the count (I-31,
    I-33).
    """
    first = lines[0]
    latest = [line for line in lines if line.period.month_start <= period.month_start][-1]
    reversed_ = latest.kind == _REVERSAL
    own = group["earned_month"] == _fm(period.month_start)
    share = Decimal(entry["share"])
    share_before = None if entry["share_before"] is None else Decimal(entry["share_before"])
    items = first.snapshot["items"]
    names = {item["product_id"]: item["product_name"] for item in items}
    tiers = {(p["product_id"], t["from_unit"]): t for p in group["products"] for t in p["tiers"]}
    tiers_before = {(p["product_id"], t["from_unit"]): t for p in group["products"] for t in p["tiers_before"] or []}
    after, before = entry["segments"], entry["segments_before"] or []
    product_ids = sorted({segment["product_id"] for segment in after} | {segment["product_id"] for segment in before})
    at_credit = units_by_product(basis_inputs_from_snapshot(first.snapshot)[2])
    counted = None if reversed_ else _line_level(latest).units
    on_order = None if reversed_ else {int(key): int(value) for key, value in latest.snapshot["units"].items()}
    return {
        "row": "order",
        "order_id": entry["order_id"],
        "order_number": first.snapshot.get("order_number"),
        "outlet_name": outlet_name,
        "earned_month": group["earned_month"],
        "earned_date": local_windows.local_date(datetime.fromisoformat(first.snapshot["earned_instant"])),
        "date": local_windows.local_date(events[-1].occurred_at) if events else None,
        "is_late": SalesPayPeriodService.is_late(first.earned_month, period),
        "counted": group["counted"],
        "tier_shift": not events and share_before is not None and share != share_before,
        "amount": share if own else share - share_before,
        "share": share,
        "share_before": share_before,
        "products": [
            {
                "product_id": product_id,
                "product_name": names[product_id],
                "units": sum(segment["units"] for segment in after if segment["product_id"] == product_id),
                "net": sum((Decimal(s["net"]) for s in after if s["product_id"] == product_id), _ZERO),
                "tiers": [_segment_view(s, tiers) for s in after if s["product_id"] == product_id],
                "tiers_before": (
                    None if own else [_segment_view(s, tiers_before) for s in before if s["product_id"] == product_id]
                ),
            }
            for product_id in product_ids
        ],
        "units_level": [
            {
                "product_id": product_id,
                "product_name": names[product_id],
                "at_credit": units,
                "on_order": None if on_order is None else on_order.get(product_id, 0),
                "counted": None if counted is None else counted.get(product_id, 0),
            }
            for product_id, units in sorted(at_credit.items())
        ],
        "items": [{key: _dec(item[key]) if key in _ITEM_MONEY else item[key] for key in _ITEM_KEYS} for item in items],
        "money": {"received_ref": _dec(latest.snapshot.get("received_ref")), **_received(latest)},
        "events": [_event_row(line) for line in events],
    }


def _bonus_row(line: SalesPayLedgerLine, period: SalesPayPeriod, *, excluded_ids: FrozenSet[int]) -> Dict[str, Any]:
    """`AdminBonusRow` (§5.2): v4's bonus `AdminLine` with `row: "bonus"`. `counted` is the
    statement's own I-16 answer: the bonus lines it froze as excluded."""
    return {
        "row": "bonus",
        "id": line.id,
        "kind": line.kind,
        "cause": line.cause,
        "is_recredit": False,
        "follows_money": follows_money(line.kind, line.cause, is_recredit=False),
        "earned_month": _fm(line.earned_month),
        "posted_month": _fm(period.month_start),
        "is_late": SalesPayPeriodService.is_late(line.earned_month, period),
        "counted": line.id not in excluded_ids,
        "order_id": line.order_id,
        "order_number": (line.snapshot or {}).get("order_number"),
        "outlet_name": line.outlet.name,
        "occurred_at": line.occurred_at,
        "date": local_windows.local_date(line.occurred_at),
        "amount": line.amount,
        "plan_version_id": line.plan_version_id,
        "money": None,
        "items": [],
    }


def _agent_item(row: Mapping[str, Any]) -> Dict[str, Any]:
    """S2's item (§5.4, `AGENT_ROW_KEYS`): the agent-safe part of an A9 row. An order row names its
    latest line of the month (none on a tier-shift row) and, per product, the units it counts, or
    the units it had at credit once it no longer counts (a reversal row prints what it removed,
    I-41)."""
    if row["row"] == "bonus":
        item = {**row, "tier_shift": False, "units": []}
    else:
        latest = row["events"][-1] if row["events"] else None
        item = {
            **row,
            "id": row["order_id"],
            "kind": latest["kind"] if latest else None,
            "cause": latest["cause"] if latest else None,
            "units": [
                {
                    "product_name": entry["product_name"],
                    "units": entry["at_credit"] if entry["counted"] is None else entry["counted"],
                }
                for entry in row["units_level"]
            ],
        }
    return {key: item[key] for key in AGENT_ROW_KEYS}


# ----------------------------------------------------------------- the agent's screen (S1)


def _open_month(period: SalesPayPeriod, statement: Optional[SalesPayStatement], *, now: datetime) -> Dict[str, Any]:
    """One `open_months` entry. `statement` is the unsaved estimate, or None when no terms are in
    force for the month (`configured: false`, a state, not an error)."""
    return {
        "month": _fm(period.month_start),
        "status": period.status,
        "is_shadow": period.is_shadow,
        "configured": statement is not None,
        "start_date": period.month_start,
        "end_date": local_windows.month_end(period.month_start),
        "gate_window_end": _estimate_window_end(period.month_start, now),
        "estimate": _agent_estimate(period, statement) if statement is not None else None,
    }


def _agent_estimate(period: SalesPayPeriod, statement: SalesPayStatement) -> Dict[str, Any]:
    """S1 `estimate`: A8's `summary` of the same unsaved row, in the agent's shape. One source for
    the figures the drawer, this screen and the approval push print.

    `base` pairs the whole month's days with the amount (the gate is month-to-date, the base is
    not, §4.8); `late` sums the gated late groups; `adjustments` lists the admin's own entries as
    typed (a carry or a netted balance is `carry_in`, labelled by `carry_in_view`, never by a
    stored reason); the penalty count is the confirmed penalties the calculator used.
    """
    inputs = statement.inputs
    orders, late_lines, bonus_count = tally(
        _line_views(period, statement.agent_user_id),
        period_month=period.month_start,
        excluded_ids=frozenset(inputs["shadow_excluded_line_ids"]),
    )
    summary = _summary(period, statement, orders=orders, bonus_count=bonus_count)
    gate_view = summary["gate"]
    return {
        "base": {
            "monthly": statement.base_salary,
            "amount": summary["base_amount"],
            "worked_days": statement.worked_days,
            "working_days": statement.working_days,
        },
        "commission": summary["commission"],
        "late": {
            "after_gate": sum((group["after_gate"] for group in summary["late"]), _ZERO),
            "lines": late_lines,
            "groups": summary["late"],
        },
        "gate": {
            "compliance_pct": gate_view["compliance_pct"],
            "visits_due": gate_view["visits_due"],
            "visits_counted": gate_view["visits_counted"],
            "multiplier": gate_view["multiplier"],
            "min_due": inputs["gate"]["min_due"],
            "rule": gate_view["rule"],
            "provisional": gate_view["provisional"],
        },
        "new_outlets": summary["new_outlets"],
        "adjustments": {
            "amount": summary["adjustments"],
            "items": [
                {"amount": row.amount, "reason": row.reason}
                for row in SalesPayAdjustment.query.filter_by(
                    period_id=period.id, agent_user_id=statement.agent_user_id, source="admin"
                ).order_by(SalesPayAdjustment.id)
            ],
        },
        "penalties": {"amount": summary["penalties"], "count": len(inputs["lines"]["penalty_ids"])},
        "variable": summary["variable"],
        "carry_in": summary["carry_in"],
        "gross_total": summary["gross_total"],
        "total": summary["total"],
        "carry_out": summary["carry_out"],
        "owed": summary["owed"],
    }


def _last_statement(
    statement: Optional[SalesPayStatement], estimates, *, latest: Optional[SalesPayStatement]
) -> Optional[Dict[str, Any]]:
    """S1 `last_statement`: the latest approved-or-paid statement (C11) with its own carry-out and
    owed. `owed_netted_in` is the open month whose estimate nets exactly this statement (the
    `build_input` decision, read off the estimate's `owed_in_statement_id`), else None: the bot
    then prints `owed_netted_note` or `owed_note`, never both (§7.2). S3's items and S4's head are
    parts of this same line for any approved-or-paid statement.

    `is_latest` says whether the row is `latest`, the agent's own `latest_approved_statement`
    (agent-scoped: the row S1 shows and `outstanding_owed` reads, which for a leaver can be older
    than the newest approved month). Only that row's `owed` is the outstanding balance (I-28) and
    only it can be netted, so `owed_netted_in` is never set unless `is_latest` is. An older row's
    `owed` and `carry_out` are what it said when approved, since netted or carried by a later
    statement."""
    if statement is None:
        return None
    period = statement.period
    netted_in = next(
        (
            _fm(open_period.month_start)
            for open_period, row in estimates
            if row is not None and row.owed_in_statement_id == statement.id
        ),
        None,
    )
    return {
        "month": _fm(period.month_start),
        "status": period.status,
        "is_shadow": period.is_shadow,
        "paid_on": period.paid_on,
        "is_latest": latest is not None and latest.id == statement.id,
        "base": statement.base_amount,
        "gated_commission": statement.gated_commission_amount,
        "new_outlets": statement.bonus_amount,
        "adjustments": statement.adjustment_amount,
        "penalties": statement.penalty_amount,
        "carry_in": statement.carry_in_amount,
        "total": statement.total_amount,
        "carry_out": statement.carry_out_amount,
        "owed": statement.owed_amount,
        "owed_netted_in": netted_in,
    }


def _agent_penalties(agent_user_id: int, period_ids: Sequence[int]) -> List[Dict[str, Any]]:
    """S1 `penalties` (§5.4): the agent's confirmed and cancelled penalties posted to the open
    months and the last statement's month, newest incident first. The reason is shown, the
    evidence never. `is_late` is `SalesPayPeriodService.is_late`, the rule the admin row and the
    push read."""
    # Imported here: pay_penalty_service imports this module (the re-freeze).
    from business_app.services.sales.pay_penalty_service import penalty_type_names, prefetch_type_names

    if not period_ids:
        return []
    rows = (
        SalesPayPenalty.query.options(joinedload(SalesPayPenalty.period), joinedload(SalesPayPenalty.penalty_type))
        .filter(
            SalesPayPenalty.agent_user_id == agent_user_id,
            SalesPayPenalty.period_id.in_(list(period_ids)),
            SalesPayPenalty.status.in_(AGENT_PENALTY_STATUSES),
        )
        .order_by(SalesPayPenalty.incident_date.desc(), SalesPayPenalty.id.desc())
        .limit(AGENT_PENALTIES_SIZE)
        .all()
    )
    prefetch_type_names(row.penalty_type for row in rows)
    return [
        {
            "id": row.id,
            "incident_date": row.incident_date,
            "type_names": penalty_type_names(row.penalty_type),
            "reason": row.reason,
            "amount": row.amount,
            "status": row.status,
            "posting_month": _fm(row.period.month_start),
            "is_late": SalesPayPeriodService.is_late(local_windows.month_start(row.incident_date), row.period),
        }
        for row in rows
    ]


def _waiting_for(order: Order, awaiting: Set[int]) -> str:
    """`SALES_PAY_PIPELINE_WAITING`: a manager's approval (C14), else the delivery, else the money."""
    approval, delivery, payment = SALES_PAY_PIPELINE_WAITING
    if order.id in awaiting:
        return approval
    return payment if order.status == OrderStatus.DELIVERED else delivery


def _pipeline_estimates(
    agent_user_id: int, waiting: Sequence[Tuple[Order, OrderCreditState]], *, now: datetime
) -> Dict[int, Optional[Decimal]]:
    """I-36: each waiting order's share when stacked on the units its earned month already counts,
    through the calculator's own `month_tiers(extra=…)`.

    A never-credited order joins this month after every counted unit, in placement order (its
    earned instant is `now`, ties by order id), on its live lines at full money. An order whose
    reversal is still unsettled returns to its frozen place in its earned month, at the level the
    sync's `_credit_level` would give it were it paid in full now (I-18, I-19, I-41). Each month
    is one allocation with all of its waiting orders, so a boundary several of them cross is split
    between them. None when no plan resolves (no terms).
    """
    this_month = local_windows.local_month(now)
    joining: Dict[date, List[Tuple[Order, OrderCreditState]]] = {}
    for order, state in waiting:
        month = this_month if state.first_credit is None else state.first_credit.earned_month
        joining.setdefault(month, []).append((order, state))
    estimates: Dict[int, Optional[Decimal]] = {}
    for month, members in joining.items():
        plan = SalesPayPlanService.version_for(agent_user_id, month)
        if plan is None:
            estimates.update((order.id, None) for order, _state in members)
            continue
        extra = []
        for order, state in members:
            received_ref, level = _credit_level(order, state, Decimal(order.total_amount or 0))
            if state.first_credit is None:
                instant, items = now, basis_inputs(order)[2]
            else:
                snapshot = state.first_credit.snapshot
                instant = datetime.fromisoformat(snapshot["earned_instant"])
                items = basis_inputs_from_snapshot(snapshot)[2]
            extra.extend(counted_contributions(order.id, instant, items, level, received_ref))
        tiers = month_tiers(_order_views(agent_user_id, {month}, through=None), plan.config, extra=extra)
        for order, _state in members:
            estimates[order.id] = sum((product.share(order.id) for product in tiers.values()), _ZERO)
    return estimates
