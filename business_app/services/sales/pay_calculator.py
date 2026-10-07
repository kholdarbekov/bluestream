"""The pay calculator (§4.7): the one formula the open-month estimate and the frozen statement share.

Pure: nothing here reads the database, the clock or the config.
`SalesPayStatementService.build_input` gathers every figure a month needs into a
`CalculatorInput`, and `calculate` turns it into a `StatementResult`. The estimate the agent
watches all month and the statement the admin approves differ only in the input they are given
(a gate window to today, `is_estimate`), never in the arithmetic, so the two cannot disagree about
what a month paid (C12).

Commission is decided here at month level (v5, D-BANDS, Approach A). Ledger lines record only an
order's level, so the input carries the orders of every earned month the period touched, each
with its frozen lines and the levels its lines recorded (`OrderView`). `month_tiers` builds one
earned month's counted contributions per product at a cut and hands them to
`pay_rules.tier_allocation`. The own month is Σ_p T(P, p, ≤P); a late group is
Σ_p [T(E, p, ≤P) − T(E, p, <P)] over the products the period touched, gated at E's frozen
multiplier: one correction per (earned month, product) (Q1, I-34).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, AbstractSet, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from business_app.services.sales.pay_rules import (
    BasisInput,
    Contribution,
    Level,
    PlanConfig,
    ProductTiers,
    ResolvedPlan,
    counted_contributions,
    counted_level,
    round_uzs,
    tier_allocation,
)
from business_app.utils import local_windows
from shared.staff_constants import SALES_PAY_COMMISSION_KINDS

if TYPE_CHECKING:
    # Only a type here: importing the producer would pull the outlet services into a pure module.
    from business_app.services.sales.day_plan_service import Compliance

CALCULATOR_VERSION = 2

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BONUS = "new_outlet_bonus"
# `tally` asks only WHETHER an order counts (I-31), so any level stands in for the one it records.
_SOME_LEVEL = Level(_ZERO, {})


@dataclass(frozen=True)
class LineView:
    id: int
    kind: str
    cause: Optional[str]
    order_id: Optional[int]
    earned_month: date
    amount: Optional[Decimal]  # None on commission kinds (v5): only a bonus line holds money


@dataclass(frozen=True)
class AdjustmentView:
    id: int
    amount: Decimal
    source: str
    carried_from_month: Optional[date]


@dataclass(frozen=True)
class PenaltyView:
    id: int
    amount: Decimal


@dataclass(frozen=True)
class OriginGate:
    multiplier: Decimal
    source: str  # "statement" | "current"


@dataclass(frozen=True)
class OwedInView:
    """The outstanding owed balance a statement nets (I-28, Q15)."""

    statement_id: int  # → sales_pay_statements.owed_in_statement_id
    month: date  # that statement's month → carry_in.from_month
    amount: Decimal  # its owed_amount, > 0


@dataclass(frozen=True)
class LevelView:
    """One commission line of an order (an "event"): the level it recorded."""

    line_id: int
    kind: str
    cause: Optional[str]
    period_month: date  # the period it was posted to
    applied: Decimal  # its received_applied; 0 for a reversal
    units: Mapping[int, int]  # its units_applied; empty for a reversal (I-41)
    occurred_at: datetime


@dataclass(frozen=True)
class OrderView:
    """One credited order of one earned month, with its lines in periods up to the statement's."""

    order_id: int
    earned_month: date
    earned_instant: datetime  # the first credit's (I-19): the allocation key (I-30)
    received_ref: Decimal
    items: Tuple[BasisInput, ...]  # the first credit's frozen lines (I-19)
    events: Tuple[LevelView, ...]  # id order

    def counted_at(self, before: Optional[date]) -> Optional[Level]:
        """The level at a cut: the lines in periods before `before`, or every line given (None)
        (I-31, I-41)."""
        return counted_level(
            (event.kind, Level(event.applied, event.units))
            for event in self.events
            if before is None or event.period_month < before
        )

    def moved_in(self, month: date) -> bool:
        """It has a line posted to that period."""
        return any(event.period_month == month for event in self.events)


@dataclass(frozen=True)
class CalculatorInput:
    period_month: date
    is_estimate: bool
    shadow_months: FrozenSet[date]
    working_dates: FrozenSet[date]
    worked_dates: FrozenSet[date]
    holidays: Mapping[date, str]
    unpaid_dates: FrozenSet[date]
    employment_start: Optional[date]
    employment_end: Optional[date]
    base_salary: Decimal
    plan: ResolvedPlan  # the version in force for period_month: its bands, minimum due and own tiers
    compliance: "Compliance"
    lines: Tuple[LineView, ...]  # the ledger lines posted to this period for this agent
    orders: Tuple[OrderView, ...]  # every order of every earned month with a commission line in this period (§4.8)
    origin_plans: Mapping[date, ResolvedPlan]  # plan_for(E) for every earlier earned month in `orders`
    origin_gates: Mapping[date, OriginGate]  # every earlier earned month in `orders`
    adjustments: Tuple[AdjustmentView, ...]  # this period, both sources
    owed_in: Optional[OwedInView]  # set only in the first unapproved month (I-28)
    penalties: Tuple[PenaltyView, ...]  # confirmed, this period


@dataclass(frozen=True)
class GateResult:
    due: int
    counted: int
    pct: Optional[float]
    min_due: int
    band_min_pct: Optional[Decimal]
    multiplier: Decimal
    rule: str  # SALES_PAY_GATE_RULES
    provisional: bool  # True on estimates (review money M3)


@dataclass(frozen=True)
class ProductChange:
    """One product of one earned-month group."""

    product_id: int
    after: Optional[ProductTiers]  # at this period's cut (None: no unit counted)
    before: Optional[ProductTiers]  # late groups only: at the cut before this period (I-34)

    @property
    def change(self) -> Decimal:
        return (self.after.total if self.after else _ZERO) - (self.before.total if self.before else _ZERO)


@dataclass(frozen=True)
class GroupResult:
    month: date
    plan_version_id: int
    counted: bool  # I-16: False for a shadow month's group in a later month
    products: Tuple[ProductChange, ...]
    gross: Decimal  # Σ products' change (own month: Σ after.total)
    multiplier: Decimal
    gated: Decimal
    source: str  # "current" for the period month, else the origin gate's source
    shares: Mapping[int, Tuple[Decimal, Optional[Decimal]]]  # order id -> (share, share_before; None in own month)


@dataclass(frozen=True)
class StatementResult:
    working_days: int
    worked_days: int
    base: Decimal
    commission: Decimal
    commission_orders: int
    late_commission: Decimal
    late_lines: int
    groups: Tuple[GroupResult, ...]
    gate: GateResult
    current_gated: Decimal
    late_gated: Decimal
    gated_commission: Decimal
    bonuses: Decimal
    bonus_count: int
    adjustments: Decimal
    penalties: Decimal
    penalty_count: int
    variable: Decimal
    carry_in: Decimal
    owed_in_statement_id: Optional[int]
    gross_total: Decimal
    total: Decimal
    carry_out: Decimal
    owed: Decimal
    shadow_excluded_line_ids: Tuple[int, ...]


def gate(c: "Compliance", plan: ResolvedPlan, *, is_estimate: bool) -> GateResult:
    """C4: the multiplier a month's own commission is paid at.

    The band is chosen by the PUBLISHED one-decimal % (`c.pct`, `AgentMetricsService.pct`), the
    same figure "My stats" prints (I-4): an agent reading 80.0 % is never paid at the 60 % band
    because the unrounded ratio was 79.96.
    """
    cfg = plan.config
    if c.pct is None or c.due < cfg.gate_min_visits_due:
        return GateResult(c.due, c.counted, c.pct, cfg.gate_min_visits_due, None, _ONE, "below_min_due", is_estimate)
    pct = Decimal(str(c.pct))
    band = next(b for b in cfg.gate_bands if pct >= b.min_pct)  # the last band is 0
    return GateResult(
        c.due, c.counted, c.pct, cfg.gate_min_visits_due, band.min_pct, band.multiplier, "band", is_estimate
    )


def employed_next_month(employment_end: Optional[date], month: date) -> bool:
    """I-26: the one test of "still employed next month", which decides carry versus owed.

    `SalesPayTermsService.affected_months` asks this same function which months an end-date change
    flips, so a leaver's month is never re-frozen by one rule and approved by another.
    """
    return employment_end is None or employment_end >= local_windows.next_month(month)


def month_tiers(
    orders: Sequence[OrderView],
    config: PlanConfig,
    *,
    before: Optional[date] = None,
    products: Optional[AbstractSet[int]] = None,
    extra: Sequence[Contribution] = (),
) -> Dict[int, ProductTiers]:
    """Every product of one earned month at one cut. `extra` adds pipeline contributions (§4.8). The
    one builder of contributions: `calculate` and the pipeline both call it."""
    by_product: Dict[int, List[Contribution]] = defaultdict(list)
    for order in orders:
        level = order.counted_at(before)
        if level is None:
            continue
        for contribution in counted_contributions(
            order.order_id, order.earned_instant, order.items, level, order.received_ref
        ):  # I-41
            by_product[contribution.product_id].append(contribution)
    for contribution in extra:
        by_product[contribution.product_id].append(contribution)
    return {
        product_id: tier_allocation(product_id, contributions, *config.schedule_for(product_id))
        for product_id, contributions in by_product.items()
        if products is None or product_id in products
    }


def _order_shares(
    orders: Sequence[OrderView],
    after: Mapping[int, ProductTiers],
    before: Mapping[int, ProductTiers],
    *,
    own: bool,
    period_month: date,
) -> Dict[int, Tuple[Decimal, Optional[Decimal]]]:
    """order id -> (share, share_before), in allocation order. The own month lists every order (each
    has a line this period) with no `share_before`. A late group lists an order only when it has a
    line this period or its share moved (a tier shift or a 1 UZS re-split, §4.8, R1-3): an
    unlisted order contributes 0, so Σ (share − share_before) is still the group's gross."""
    shares: Dict[int, Tuple[Decimal, Optional[Decimal]]] = {}
    for order in sorted(orders, key=lambda o: (o.earned_instant, o.order_id)):
        share = sum((tiers.share(order.order_id) for tiers in after.values()), _ZERO)
        if own:
            shares[order.order_id] = (share, None)
            continue
        share_before = sum((tiers.share(order.order_id) for tiers in before.values()), _ZERO)
        if order.moved_in(period_month) or share != share_before:
            shares[order.order_id] = (share, share_before)
    return shares


def tally(lines: Sequence[LineView], *, period_month: date, excluded_ids: AbstractSet[int]) -> Tuple[int, int, int]:
    """(orders of this month counted at the cut, late commission lines, bonus lines) among the counted
    lines, in id order.

    One counter for the estimate and for a frozen statement: a frozen A8 re-counts its own period's
    lines against the ids it froze as excluded, which is safe because a closed period never
    receives another line. Every line of an order earned this month sits in this period, so its
    lines here decide whether it counts: `counted_level`, the groups' predicate (I-31, V5-B12).
    """
    counted = [line for line in lines if line.id not in excluded_ids]
    own: Dict[int, List[str]] = {}
    for line in counted:
        if line.kind in SALES_PAY_COMMISSION_KINDS and line.earned_month == period_month:
            own.setdefault(line.order_id, []).append(line.kind)
    orders = sum(1 for kinds in own.values() if counted_level((kind, _SOME_LEVEL) for kind in kinds) is not None)
    late = sum(1 for line in counted if line.kind in SALES_PAY_COMMISSION_KINDS and line.earned_month != period_month)
    bonuses = sum(1 for line in counted if line.kind == _BONUS)
    return orders, late, bonuses


def calculate(inp: CalculatorInput) -> StatementResult:
    working, worked = len(inp.working_dates), len(inp.worked_dates)
    base = round_uzs(inp.base_salary * worked / working) if working else _ZERO
    month_gate = gate(inp.compliance, inp.plan, is_estimate=inp.is_estimate)

    def counted_month(month: date) -> bool:
        """I-16: a shadow month's earnings count in the shadow month's own statement and nowhere else
        (M8: the one expression, for the groups and the bonuses)."""
        return not (month in inp.shadow_months and month != inp.period_month)

    excluded = tuple(sorted(line.id for line in inp.lines if not counted_month(line.earned_month)))
    excluded_ids = frozenset(excluded)

    groups: List[GroupResult] = []
    for month in sorted({order.earned_month for order in inp.orders}):
        orders = [order for order in inp.orders if order.earned_month == month]
        own = month == inp.period_month
        plan = inp.plan if own else inp.origin_plans[month]
        if own:
            after, before = month_tiers(orders, plan.config), {}
        else:
            # I-34: only the products of the orders that moved this period can change.
            touched = {item.product_id for order in orders if order.moved_in(inp.period_month) for item in order.items}
            after = month_tiers(orders, plan.config, products=touched)
            before = month_tiers(orders, plan.config, before=inp.period_month, products=touched)
        products = tuple(
            ProductChange(product_id, after.get(product_id), None if own else before.get(product_id))
            for product_id in sorted(set(after) | set(before))
        )
        gross = sum((product.change for product in products), _ZERO)
        counted = counted_month(month)
        if own:
            multiplier, source = month_gate.multiplier, "current"
        else:
            # Q1: a late change keeps the discipline of the month it was earned in.
            origin = inp.origin_gates[month]
            multiplier, source = origin.multiplier, origin.source
        groups.append(
            GroupResult(
                month=month,
                plan_version_id=plan.version_id,
                counted=counted,
                products=products,
                gross=gross,
                multiplier=multiplier,
                gated=round_uzs(gross * multiplier) if counted else _ZERO,  # C8: one rounding per group
                source=source,
                shares=_order_shares(orders, after, before, own=own, period_month=inp.period_month),
            )
        )

    current = next((g for g in groups if g.month == inp.period_month), None)
    commission = current.gross if current else _ZERO
    current_gated = current.gated if current else _ZERO
    late_groups = [g for g in groups if g.month != inp.period_month]
    late_commission = sum((g.gross for g in late_groups if g.counted), _ZERO)
    late_gated = sum((g.gated for g in late_groups), _ZERO)
    gated = current_gated + late_gated

    bonuses = sum((line.amount for line in inp.lines if line.kind == _BONUS and line.id not in excluded_ids), _ZERO)
    adjustments = sum((a.amount for a in inp.adjustments if a.source == "admin"), _ZERO)
    carried = sum((a.amount for a in inp.adjustments if a.source == "carry_forward"), _ZERO)  # ≤ 0 (C1)
    owed_brought = -inp.owed_in.amount if inp.owed_in else _ZERO  # ≤ 0 (I-28)
    if carried and owed_brought:
        raise ValueError("carry row and owed balance in one statement")  # impossible by I-28 / §4.10
    carry_in = carried + owed_brought
    penalties = sum((p.amount for p in inp.penalties), _ZERO)

    variable = gated + bonuses + adjustments - penalties  # C1: signed, never floored
    gross_total = base + variable + carry_in  # C1: penalties and corrections reach the base
    total = max(_ZERO, gross_total)  # C1: the only floor
    shortfall = min(_ZERO, gross_total)
    moves_money = inp.period_month not in inp.shadow_months  # I-27
    employed = employed_next_month(inp.employment_end, inp.period_month)  # I-26
    carry_out = shortfall if moves_money and employed else _ZERO
    owed = -shortfall if moves_money and not employed else _ZERO

    commission_orders, late_lines, bonus_count = tally(
        inp.lines, period_month=inp.period_month, excluded_ids=excluded_ids
    )
    return StatementResult(
        working_days=working,
        worked_days=worked,
        base=base,
        commission=commission,
        commission_orders=commission_orders,
        late_commission=late_commission,
        late_lines=late_lines,
        groups=tuple(groups),
        gate=month_gate,
        current_gated=current_gated,
        late_gated=late_gated,
        gated_commission=gated,
        bonuses=bonuses,
        bonus_count=bonus_count,
        adjustments=adjustments,
        penalties=penalties,
        penalty_count=len(inp.penalties),
        variable=variable,
        carry_in=carry_in,
        owed_in_statement_id=inp.owed_in.statement_id if inp.owed_in else None,
        gross_total=gross_total,
        total=total,
        carry_out=carry_out,
        owed=owed,
        shadow_excluded_line_ids=excluded,
    )
