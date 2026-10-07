"""The pay calculator (§4.7): the one formula the open-month estimate and the frozen statement share.

Every expected figure is a literal worked out by hand in the test's docstring from §1.2 and
Appendix E of docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md. The calculator
is pure, so these tests hand it its input: the orders of each earned month with their frozen lines
and the levels their ledger lines recorded (`OrderView`, v5), never an amount: commission lines
carry none (V5-B3). `build_input`, which gathers that input from the database, is proven through
the admin routes in tests/integration/test_sales_pay_*_api.py.

The three `carry_in_view` cases at the bottom need rows (a statement, a carry adjustment) and read
them through the statement service, so they use the `db` fixture.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.sales_pay import SalesPayAdjustment, SalesPayPeriod, SalesPayStatement
from business_app.services.sales.agent_metrics_service import AgentMetricsService
from business_app.services.sales.day_plan_service import Compliance
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
    calculate,
    employed_next_month,
    gate,
    month_tiers,
)
from business_app.services.sales.pay_rules import (
    BasisInput,
    GateBand,
    Level,
    PlanConfig,
    Rate,
    ResolvedPlan,
    Tier,
    TierSchedule,
    counted_contributions,
    units_by_product,
)

SEP, OCT, NOV, DEC = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)

# October 2026 without the 1st (Illustration B's holiday); every day is a working day (C2 v5): 30.
OCT_WORKING = frozenset(date(2026, 10, d) for d in range(2, 32))
OCT_HOLIDAYS = {date(2026, 10, 1): "Teachers' Day"}
# November 2026, no holiday: all 30 days.
NOV_WORKING = frozenset(date(2026, 11, d) for d in range(1, 31))

CREDIT, REVERSAL, DIFFERENCE = "commission_credit", "commission_reversal", "commission_difference"
WATER, JUICE = 3, 9
NAMES = {
    WATER: {"en": "19L", "uz": "19L", "ru": "19L"},
    JUICE: {"en": "Juice 1 L", "uz": "Sharbat 1 L", "ru": "Сок 1 л"},
}
PRICE = {WATER: Decimal("20000.00"), JUICE: Decimal("25000.00")}
# Tier schedules as `(from_unit, mode, value)` rows.
FLAT_1500 = ((1, "per_unit", "1500"),)
FLAT_1000 = ((1, "per_unit", "1000"),)
ILLUSTRATION_B = ((1, "per_unit", "1000"), (301, "per_unit", "1500"))  # §1.2, M3
TWO_AT_500 = ((1, "per_unit", "1000"), (501, "per_unit", "1500"))  # T-TIER-10, T-TIER-11
ILLUSTRATION_C = ((1, "per_unit", "1000"), (501, "per_unit", "1500"), (1001, "per_unit", "2000"), (2001, "per_unit", "2500"))


def _schedule(rows):
    return TierSchedule(tuple(Tier(from_unit, Rate(mode, Decimal(value))) for from_unit, mode, value in rows))


def _plan(*, water=FLAT_1500, default=((1, "percent", "3"),), min_due=20, version_id=3, month=OCT):
    """Plan "Standard": 19 L on its own schedule (one tier at 1,500 unless told), every other product
    on the default schedule (3 %), the default bands (≥80 ×1.0, ≥60 ×0.8, else ×0.5)."""
    config = PlanConfig(
        default_tiers=_schedule(default),
        product_tiers={WATER: _schedule(water)},
        gate_bands=(
            GateBand(Decimal("80.0"), Decimal("1.000")),
            GateBand(Decimal("60.0"), Decimal("0.800")),
            GateBand(Decimal("0.0"), Decimal("0.500")),
        ),
        gate_min_visits_due=min_due,
        bonus_amount=Decimal("100000"),
        bonus_window_days=60,
        bonus_min_orders_with_total=2,
        bonus_min_combined_total=Decimal("300000"),
        bonus_min_orders_any_amount=5,
        bonus_prior_customer_lookback_days=180,
    )
    return ResolvedPlan(
        plan_id=1, plan_name="Standard", version_id=version_id, version_no=1, effective_month=month, config=config
    )


def _compliance(counted, due):
    """What `AgentDayPlanService.compliance` hands the gate: its % is `AgentMetricsService.pct`'s."""
    return Compliance(
        due=due, counted=counted, pct=AgentMetricsService.pct(counted, due, cap=100.0), min_seconds=60, days=()
    )


def _items(order_id, *lines):
    """An order's frozen lines in order_item_id order: `(product, quantity)` at its list price, or
    `(product, quantity, net)` for a line a discount lowered."""
    frozen = []
    for index, (product, quantity, *net) in enumerate(lines, start=1):
        gross = PRICE[product] * quantity
        line_net = Decimal(net[0]) if net else gross
        frozen.append(
            BasisInput(
                order_item_id=order_id * 10 + index,
                product_id=product,
                product_name=NAMES[product],
                quantity=quantity,
                unit_price=PRICE[product],
                total_price=gross,
                discount_share=gross - line_net,
                net=line_net,
            )
        )
    return tuple(frozen)


def _event(line_id, kind, posted, *, applied="0", units=None, cause=None):
    """One commission line as the ledger recorded it: its level, posted to the period of `posted`."""
    return LevelView(
        line_id=line_id,
        kind=kind,
        cause=cause,
        period_month=posted,
        applied=Decimal(applied),
        units=dict(units or {}),
        occurred_at=datetime(posted.year, posted.month, 15, 5, tzinfo=timezone.utc),
    )


def _reversal(order_id, n, posted):
    return _event(order_id * 10 + n, REVERSAL, posted, cause="nothing_received")


def _difference(order_id, n, posted, cause, applied, units):
    return _event(order_id * 10 + n, DIFFERENCE, posted, applied=applied, units=units, cause=cause)


def _order(order_id, items, *events, month=OCT, day=5):
    """An order earned at 10:00 local on `day` of `month` that owed its lines' net (no fee, I-17)."""
    return OrderView(
        order_id=order_id,
        earned_month=month,
        earned_instant=datetime(month.year, month.month, day, 5, tzinfo=timezone.utc),
        received_ref=sum((item.net for item in items), Decimal("0")),
        items=items,
        events=events,
    )


def _credited(order_id, *lines, month=OCT, day=5, posted=None, more=()):
    """An order whose first credit (line `order_id`·10 + 1) counted every unit at full money, posted to
    `posted` (its earned month unless credited late); `more` are its later lines."""
    items = _items(order_id, *lines)
    ref = sum((item.net for item in items), Decimal("0"))
    credit = _event(order_id * 10 + 1, CREDIT, posted or month, applied=str(ref), units=units_by_product(items))
    return _order(order_id, items, credit, *more, month=month, day=day)


def _bonus(line_id, amount, month=OCT):
    return LineView(id=line_id, kind="new_outlet_bonus", cause=None, order_id=None, earned_month=month, amount=Decimal(amount))


def _lines_of(period, orders, bonuses=()):
    """This period's ledger lines, as `build_input` reads them: a commission line carries no amount."""
    commission = [
        LineView(id=event.line_id, kind=event.kind, cause=event.cause, order_id=order.order_id,
                 earned_month=order.earned_month, amount=None)
        for order in orders
        for event in order.events
        if event.period_month == period
    ]
    return tuple(sorted(commission + list(bonuses), key=lambda line: line.id))


def _input(*, orders=(), bonuses=(), **fields):
    values = dict(
        period_month=OCT,
        is_estimate=False,
        shadow_months=frozenset(),
        working_dates=OCT_WORKING,
        worked_dates=OCT_WORKING,
        holidays=OCT_HOLIDAYS,
        unpaid_dates=frozenset(),
        employment_start=date(2026, 6, 1),
        employment_end=None,
        base_salary=Decimal("3000000"),
        plan=_plan(),
        compliance=_compliance(0, 0),
        orders=tuple(orders),
        origin_plans={},
        origin_gates={},
        adjustments=(),
        owed_in=None,
        penalties=(),
    )
    values.update(fields)
    values["lines"] = _lines_of(values["period_month"], values["orders"], bonuses)
    return CalculatorInput(**values)


def _carry_case(**fields):
    """§1.2's carry case: base 3,000,000 on 30 working days of which 2 are worked (the 2nd and 3rd)
    = 200,000; 20 × 19 L at 1,500 = 30,000 earned in October with nothing due (×1.0); a September
    order of 200 × 19 L, credited 300,000 and reversed in October, at September's ×1.0; a 30,000
    penalty. Variable 30,000 − 300,000 − 30,000 = −300,000; gross 200,000 − 300,000 = −100,000."""
    values = dict(
        base_salary=Decimal("3000000"),
        worked_dates=frozenset({date(2026, 10, 2), date(2026, 10, 3)}),
        unpaid_dates=OCT_WORKING - {date(2026, 10, 2), date(2026, 10, 3)},
        orders=(
            _credited(1, (WATER, 20)),
            _credited(2, (WATER, 200), month=SEP, day=20, more=(_reversal(2, 2, OCT),)),
        ),
        origin_plans={SEP: _plan(version_id=2, month=SEP)},
        origin_gates={SEP: OriginGate(Decimal("1.000"), "statement")},
        penalties=(PenaltyView(id=9, amount=Decimal("30000")),),
    )
    values.update(fields)
    return _input(**values)


def _tier_rows(tiers):
    return [(tier.from_unit, tier.to_unit, tier.units, tier.amount) for tier in tiers.tiers]


# --------------------------------------------------------------------------- #
# The worked examples of §1.2
# --------------------------------------------------------------------------- #


def test_illustration_b_pays_3_560_000():
    """Illustration B, October 2026 (§1.2, E.4).

    Base 3,000,000 × 28 / 30 = 2,800,000 (30 working days after the 1 October holiday, less the
    unpaid 14th and 15th). Commission 850,000 over 31 credited orders: 19 L, 20 orders of 25
    bottles on [(1, 1,000), (301, 1,500)]: 300 × 1,000 + 200 × 1,500 = 600,000; the juice on the
    default 2 %, ten orders of 45 and one of 50 at 25,000 = 12,500,000 net: 250,000. 160 counted of
    212 due = 75.47 %, published 75.5 → band 60 → ×0.8 = 680,000. A September juice order
    (40 × 25,000 at 2 % = 20,000) reversed in October: −20,000 at September's frozen ×1.0. Two
    bonuses of 100,000, an admin adjustment of +50,000 and one 150,000 penalty.
    Variable 680,000 − 20,000 + 200,000 + 50,000 − 150,000 = 760,000; gross 2,800,000 + 760,000
    = 3,560,000, paid in full; nothing carried and nothing owed.
    """
    illustration_b = dict(water=ILLUSTRATION_B, default=((1, "percent", "2"),))
    unpaid = frozenset({date(2026, 10, 14), date(2026, 10, 15)})
    water = [_credited(n, (WATER, 25), day=n + 1) for n in range(1, 21)]
    juice = [_credited(n, (JUICE, 45), day=n) for n in range(21, 31)] + [_credited(31, (JUICE, 50), day=31)]
    september = _credited(40, (JUICE, 40), month=SEP, day=20, more=(_reversal(40, 2, OCT),))

    result = calculate(
        _input(
            plan=_plan(**illustration_b),
            worked_dates=OCT_WORKING - unpaid,
            unpaid_dates=unpaid,
            compliance=_compliance(160, 212),
            orders=(*water, *juice, september),
            bonuses=(_bonus(901, "100000"), _bonus(902, "100000")),
            origin_plans={SEP: _plan(**illustration_b, version_id=2, month=SEP)},
            origin_gates={SEP: OriginGate(Decimal("1.000"), "statement")},
            adjustments=(AdjustmentView(id=5, amount=Decimal("50000"), source="admin", carried_from_month=None),),
            penalties=(PenaltyView(id=6, amount=Decimal("150000")),),
        )
    )

    assert (result.working_days, result.worked_days, result.base) == (30, 28, Decimal("2800000"))
    assert (result.commission, result.commission_orders) == (Decimal("850000"), 31)
    assert (result.gate.pct, result.gate.rule, result.gate.band_min_pct, result.gate.multiplier) == (
        75.5,
        "band",
        Decimal("60.0"),
        Decimal("0.800"),
    )
    assert [(g.month, g.plan_version_id, g.counted, g.gross, g.multiplier, g.gated, g.source) for g in result.groups] == [
        (SEP, 2, True, Decimal("-20000"), Decimal("1.000"), Decimal("-20000"), "statement"),
        (OCT, 3, True, Decimal("850000"), Decimal("0.800"), Decimal("680000"), "current"),
    ]
    september_group, october_group = result.groups
    assert [(p.product_id, p.after.units, p.after.total, _tier_rows(p.after), p.before) for p in october_group.products] == [
        (WATER, 500, Decimal("600000"), [(1, 300, 300, Decimal("300000")), (301, None, 200, Decimal("300000"))], None),
        (JUICE, 500, Decimal("250000"), [(1, None, 500, Decimal("250000"))], None),
    ]
    assert [(p.product_id, p.before.total, p.after, p.change) for p in september_group.products] == [
        (JUICE, Decimal("20000"), None, Decimal("-20000"))
    ]
    assert september_group.shares == {40: (Decimal("0"), Decimal("20000"))}
    assert sum(share for share, _before in october_group.shares.values()) == Decimal("850000")
    assert (result.late_commission, result.late_lines) == (Decimal("-20000"), 1)
    assert (result.current_gated, result.late_gated, result.gated_commission) == (
        Decimal("680000"),
        Decimal("-20000"),
        Decimal("660000"),
    )
    assert (result.bonuses, result.bonus_count) == (Decimal("200000"), 2)
    assert (result.adjustments, result.penalties, result.penalty_count) == (Decimal("50000"), Decimal("150000"), 1)
    assert result.variable == Decimal("760000")
    assert (result.carry_in, result.owed_in_statement_id) == (Decimal("0"), None)
    assert (result.gross_total, result.total, result.carry_out, result.owed) == (
        Decimal("3560000"),
        Decimal("3560000"),
        Decimal("0"),
        Decimal("0"),
    )
    assert result.shadow_excluded_line_ids == ()


def test_penalties_reach_the_base():
    """§1.2 "Penalties reach the base": Illustration B's base 2,800,000; 100 × 19 L at 1,000 =
    100,000 of commission at 10 counted of 40 due = 25 % → ×0.5 = 50,000; penalties 200,000.
    Variable 50,000 − 200,000 = −150,000 (never floored); gross 2,800,000 − 150,000 = 2,650,000,
    which is also the paid total. Nothing is carried."""
    unpaid = frozenset({date(2026, 10, 14), date(2026, 10, 15)})

    result = calculate(
        _input(
            plan=_plan(water=FLAT_1000),
            worked_dates=OCT_WORKING - unpaid,
            unpaid_dates=unpaid,
            compliance=_compliance(10, 40),
            orders=(_credited(1, (WATER, 100)),),
            penalties=(PenaltyView(id=1, amount=Decimal("120000")), PenaltyView(id=2, amount=Decimal("80000"))),
        )
    )

    assert (result.base, result.gated_commission, result.penalties, result.penalty_count) == (
        Decimal("2800000"),
        Decimal("50000"),
        Decimal("200000"),
        2,
    )
    assert (result.variable, result.gross_total, result.total) == (
        Decimal("-150000"),
        Decimal("2650000"),
        Decimal("2650000"),
    )
    assert (result.carry_out, result.owed) == (Decimal("0"), Decimal("0"))


def test_the_carry_case_pays_0_and_carries_minus_100_000():
    result = calculate(_carry_case())

    assert (result.base, result.commission, result.late_commission) == (
        Decimal("200000"),
        Decimal("30000"),
        Decimal("-300000"),
    )
    assert (result.gate.rule, result.gate.multiplier) == ("below_min_due", Decimal("1"))
    assert (result.variable, result.gross_total, result.total) == (Decimal("-300000"), Decimal("-100000"), Decimal("0"))
    assert (result.carry_out, result.owed) == (Decimal("-100000"), Decimal("0"))


def test_a_leaver_owes_the_shortfall_instead_of_carrying_it():
    """The leaver variant: employment ends 2026-10-31, so there is no November to carry into
    (I-26) and the 100,000 is owed by the agent."""
    result = calculate(_carry_case(employment_end=date(2026, 10, 31)))

    assert (result.gross_total, result.total, result.carry_out, result.owed) == (
        Decimal("-100000"),
        Decimal("0"),
        Decimal("0"),
        Decimal("100000"),
    )


def test_a_shadow_month_below_zero_neither_carries_nor_owes():
    """I-27: the shadow October (the epoch) with base 200,000 and a 300,000 penalty shows gross
    −100,000 and pays 0, and moves no money either way."""
    result = calculate(
        _carry_case(
            shadow_months=frozenset({OCT}),
            orders=(),
            origin_plans={},
            origin_gates={},
            penalties=(PenaltyView(id=9, amount=Decimal("300000")),),
        )
    )

    assert (result.gross_total, result.total, result.carry_out, result.owed) == (
        Decimal("-100000"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )


def test_november_deducts_the_carry_and_a_month_still_below_zero_carries_again():
    """§1.2 November: 30 working days, all worked → base 3,000,000; 300 × 19 L at 1,500 = 450,000
    at ×1.0; the carry row −100,000 is the carry-in. Gross 3,000,000 + 450,000 − 100,000 =
    3,350,000. The chain: every working day unpaid and no commission → base 0, gross −100,000,
    carried again."""
    carry = AdjustmentView(id=77, amount=Decimal("-100000"), source="carry_forward", carried_from_month=OCT)
    november = dict(
        period_month=NOV,
        working_dates=NOV_WORKING,
        holidays={},
        base_salary=Decimal("3000000"),
        adjustments=(carry,),
    )

    paid = calculate(
        _input(**november, worked_dates=NOV_WORKING, orders=(_credited(1, (WATER, 300), month=NOV, day=10),))
    )
    chained = calculate(_input(**november, worked_dates=frozenset(), unpaid_dates=NOV_WORKING))

    assert (paid.base, paid.adjustments, paid.carry_in, paid.variable) == (
        Decimal("3000000"),
        Decimal("0"),
        Decimal("-100000"),
        Decimal("450000"),
    )
    assert (paid.gross_total, paid.total, paid.carry_out) == (Decimal("3350000"), Decimal("3350000"), Decimal("0"))
    assert (chained.base, chained.gross_total, chained.total, chained.carry_out) == (
        Decimal("0"),
        Decimal("-100000"),
        Decimal("0"),
        Decimal("-100000"),
    )


def test_an_owed_balance_is_netted_as_the_carry_in_and_what_is_left_stays_owed():
    """T-OWED-3 (e): November of the leaver (employment ended 31 October, so no worked day and no
    next month). 50 × 19 L at 1,000 = 50,000 credited in November at ×1.0; October's owed 100,000 is
    netted. Gross 0 + 50,000 − 100,000 = −50,000 → paid 0, owed 50,000, carried nothing."""
    result = calculate(
        _input(
            period_month=NOV,
            working_dates=NOV_WORKING,
            worked_dates=frozenset(),
            holidays={},
            employment_end=date(2026, 10, 31),
            base_salary=Decimal("3000000"),
            plan=_plan(water=FLAT_1000, month=NOV),
            orders=(_credited(1, (WATER, 50), month=NOV, day=5),),
            owed_in=OwedInView(statement_id=41, month=OCT, amount=Decimal("100000")),
        )
    )

    assert (result.base, result.variable, result.carry_in, result.owed_in_statement_id) == (
        Decimal("0"),
        Decimal("50000"),
        Decimal("-100000"),
        41,
    )
    assert (result.gross_total, result.total, result.carry_out, result.owed) == (
        Decimal("-50000"),
        Decimal("0"),
        Decimal("0"),
        Decimal("50000"),
    )


def test_a_carry_row_and_an_owed_balance_in_one_statement_is_refused():
    """T-OWED-3 (e): impossible by I-28 and §4.10, so a guard, never a rule."""
    with pytest.raises(ValueError):
        calculate(
            _input(
                adjustments=(
                    AdjustmentView(id=1, amount=Decimal("-10000"), source="carry_forward", carried_from_month=SEP),
                ),
                owed_in=OwedInView(statement_id=2, month=SEP, amount=Decimal("5000")),
            )
        )


# --------------------------------------------------------------------------- #
# Gating: one rounding per group, late changes at their earned month's gate
# --------------------------------------------------------------------------- #


def test_each_gated_group_is_rounded_once_per_statement():
    """C8 per group (§4.7). One juice line, net 23,750 at 3 % = 712.50 → 713 (I-33), credited in
    October at ×0.8 → 570.4 → +570. November: half the money is left (11,875 of 23,750): 356.25
    → 356, so the group changes by −357, at October's ×0.8 → −285.6 → −286. December: reversed,
    356 → 0, −356 × 0.8 = −284.8 → −285. The order nets 0 (713 − 357 − 356) but the three
    statements pay +570 − 286 − 285 = −1: the documented cost of rounding each statement's group
    once."""
    juice = _items(1, (JUICE, 1, "23750.00"))
    credit = _event(11, CREDIT, OCT, applied="23750.00", units={JUICE: 1})
    halved = _difference(1, 2, NOV, "received_reduced", "11875.00", {JUICE: 1})
    late = dict(origin_plans={OCT: _plan()}, origin_gates={OCT: OriginGate(Decimal("0.800"), "statement")})

    october = calculate(_input(compliance=_compliance(14, 20), orders=(_order(1, juice, credit),)))
    november = calculate(_input(period_month=NOV, orders=(_order(1, juice, credit, halved),), **late))
    december = calculate(
        _input(period_month=DEC, orders=(_order(1, juice, credit, halved, _reversal(1, 3, DEC)),), **late)
    )

    assert [october.gated_commission, november.gated_commission, december.gated_commission] == [
        Decimal("570"),
        Decimal("-286"),
        Decimal("-285"),
    ]
    assert october.gated_commission + november.gated_commission + december.gated_commission == Decimal("-1")
    assert [november.groups[0].shares, december.groups[0].shares] == [
        {1: (Decimal("356"), Decimal("713"))},
        {1: (Decimal("0"), Decimal("356"))},
    ]
    assert november.late_gated == november.gated_commission and november.current_gated == Decimal("0")


def test_a_late_change_without_an_origin_statement_is_gated_at_the_current_month():
    """§4.7: an agent with no statement for the earned month had no activity there; the current
    multiplier applies and the group says so. 10 × 19 L at 1,000 earned in October, credited late in
    November: 10,000 × 0.5 = 5,000."""
    result = calculate(
        _input(
            period_month=NOV,
            compliance=_compliance(10, 40),
            orders=(_credited(1, (WATER, 10), posted=NOV),),
            origin_plans={OCT: _plan(water=FLAT_1000)},
            origin_gates={OCT: OriginGate(Decimal("0.500"), "current")},
        )
    )

    assert [(g.month, g.multiplier, g.gross, g.gated, g.source) for g in result.groups] == [
        (OCT, Decimal("0.500"), Decimal("10000"), Decimal("5000"), "current")
    ]


def test_a_shadow_months_tiers_count_only_in_the_shadow_months_own_statement():
    """I-16, T-TIER-16's arithmetic. October is the shadow month. R (6 bottles) was credited there;
    in November R is reversed and X (4 bottles, earned 20 October) is credited late. November's
    October group is still computed, at its own tiers: before 6 × 1,500 = 9,000, after
    4 × 1,500 = 6,000, gross −3,000, but `counted` False and gated 0, and both lines are listed as
    excluded. November's own 3 bottles (4,500) count. In October itself its lines count."""
    r = _credited(1, (WATER, 6), more=(_reversal(1, 2, NOV),))
    x = _credited(2, (WATER, 4), day=20, posted=NOV)
    n = _credited(3, (WATER, 3), month=NOV, day=10)

    november = calculate(
        _input(
            period_month=NOV,
            shadow_months=frozenset({OCT}),
            orders=(r, x, n),
            origin_plans={OCT: _plan()},
            origin_gates={OCT: OriginGate(Decimal("1.000"), "statement")},
        )
    )
    october = calculate(_input(shadow_months=frozenset({OCT}), orders=(_credited(4, (WATER, 6)),)))

    assert november.shadow_excluded_line_ids == (12, 21)
    assert [(g.month, g.counted, g.gross, g.gated) for g in november.groups] == [
        (OCT, False, Decimal("-3000"), Decimal("0")),
        (NOV, True, Decimal("4500"), Decimal("4500")),
    ]
    assert november.groups[0].shares == {1: (Decimal("0"), Decimal("9000")), 2: (Decimal("6000"), Decimal("0"))}
    assert (november.late_commission, november.late_lines, november.late_gated, november.gated_commission) == (
        Decimal("0"),
        0,
        Decimal("0"),
        Decimal("4500"),
    )
    assert (october.commission, october.gated_commission, october.shadow_excluded_line_ids) == (
        Decimal("9000"),
        Decimal("9000"),
        (),
    )


# --------------------------------------------------------------------------- #
# Month-level tiers (D-BANDS, §4.7)
# --------------------------------------------------------------------------- #


def test_a_month_fills_each_product_in_earned_order_and_a_cut_reads_only_earlier_periods():
    """T-TIER-1 through `month_tiers`. A 300 (5 Oct), B 250 (12 Oct) and C 150 (20 Oct) bottles fill
    [(1, 1,000), (501, 1,500), (1,001, 2,000), (2,001, 2,500)]: 500 × 1,000 + 200 × 1,500 =
    800,000; A 300,000, B 200 × 1,000 + 50 × 1,500 = 275,000, C 225,000; next tier from 1,001,
    301 to go. A is reversed in November: with every line given A counts nothing and B and C move
    down (B units 1-250 = 250,000, C 251-400 = 150,000: 400,000), while the cut before November
    still reads 800,000 (I-34). A's 20 juices sit on the default 3 %: 15,000 before, nothing after.
    `products` keeps only the products asked for."""
    plan = _plan(water=ILLUSTRATION_C).config
    orders = (
        _credited(1, (WATER, 300), (JUICE, 20), day=5, more=(_reversal(1, 2, NOV),)),
        _credited(2, (WATER, 250), day=12),
        _credited(3, (WATER, 150), day=20),
    )

    every_line = month_tiers(orders, plan)
    before_november = month_tiers(orders, plan, before=NOV)
    water_only = month_tiers(orders, plan, before=NOV, products={WATER})

    assert (set(every_line), set(before_november), set(water_only)) == ({WATER}, {WATER, JUICE}, {WATER})
    assert _tier_rows(before_november[WATER]) == [(1, 500, 500, Decimal("500000")), (501, 1000, 200, Decimal("300000"))]
    assert [before_november[WATER].share(order) for order in (1, 2, 3)] == [
        Decimal("300000"),
        Decimal("275000"),
        Decimal("225000"),
    ]
    assert before_november[WATER].next_tier() == (1001, 301, Rate("per_unit", Decimal("2000")))
    assert (before_november[JUICE].total, before_november[JUICE].uses_default) == (Decimal("15000"), True)
    assert (every_line[WATER].units, every_line[WATER].total, every_line[WATER].uses_default) == (
        400,
        Decimal("400000"),
        False,
    )
    assert [every_line[WATER].share(order) for order in (1, 2, 3)] == [Decimal("0"), Decimal("250000"), Decimal("150000")]


def test_waiting_orders_stack_after_the_counted_units_and_share_a_boundary():
    """T-TIER-10's arithmetic (I-36). 480 bottles counted in October at [(1, 1,000), (501, 1,500)];
    two waiting orders are stacked after them in placement order, at full money: P1 (30) holds
    units 481-510, 20 × 1,000 + 10 × 1,500 = 35,000; P2 (20) units 511-530, 20 × 1,500 = 30,000."""
    counted = _credited(1, (WATER, 480), day=5)
    now = datetime(2026, 10, 31, 15, tzinfo=timezone.utc)
    extra = []
    for order_id, bottles in ((7, 30), (8, 20)):
        items = _items(order_id, (WATER, bottles))
        ref = sum((item.net for item in items), Decimal("0"))
        extra.extend(counted_contributions(order_id, now, items, Level(ref, units_by_product(items)), ref))

    water = month_tiers((counted,), _plan(water=TWO_AT_500).config, extra=extra)[WATER]

    assert [water.share(order) for order in (1, 7, 8)] == [Decimal("480000"), Decimal("35000"), Decimal("30000")]
    assert water.total == Decimal("545000")


def test_a_late_reversal_corrects_each_product_once_at_its_earned_months_gate():
    """T-TIER-6's arithmetic (Q1, I-34). Illustration C, with A also carrying 20 juices (3 % of
    500,000 = 15,000), October frozen at ×0.8. A is reversed in November: 19 L 800,000 → 400,000
    (−400,000) and juice 15,000 → 0 (−15,000), one group: gross −415,000, × 0.8 = −332,000. A's
    share 315,000 → 0, B 275,000 → 250,000, C 225,000 → 150,000: −315,000 − 25,000 − 75,000 =
    −415,000, the group's gross."""
    orders = (
        _credited(1, (WATER, 300), (JUICE, 20), day=5, more=(_reversal(1, 2, NOV),)),
        _credited(2, (WATER, 250), day=12),
        _credited(3, (WATER, 150), day=20),
    )

    result = calculate(
        _input(
            period_month=NOV,
            orders=orders,
            origin_plans={OCT: _plan(water=ILLUSTRATION_C)},
            origin_gates={OCT: OriginGate(Decimal("0.800"), "statement")},
        )
    )

    (group,) = result.groups
    assert (group.month, group.gross, group.multiplier, group.gated, group.source) == (
        OCT,
        Decimal("-415000"),
        Decimal("0.800"),
        Decimal("-332000"),
        "statement",
    )
    assert [
        (p.product_id, p.before.units, p.after.units if p.after else 0, p.before.total, p.change) for p in group.products
    ] == [
        (WATER, 700, 400, Decimal("800000"), Decimal("-400000")),
        (JUICE, 20, 0, Decimal("15000"), Decimal("-15000")),
    ]
    assert group.shares == {
        1: (Decimal("0"), Decimal("315000")),
        2: (Decimal("250000"), Decimal("275000")),
        3: (Decimal("150000"), Decimal("225000")),
    }
    assert sum(share - before for share, before in group.shares.values()) == group.gross
    assert (result.late_commission, result.late_lines, result.gated_commission) == (
        Decimal("-415000"),
        1,
        Decimal("-332000"),
    )


def test_a_late_credit_takes_its_frozen_place_and_only_the_orders_that_moved_are_listed():
    """T-TIER-18's arithmetic (I-30, R40, §4.8). October holds A 300 (5 Oct), B 250 (12 Oct) and
    C 150 (20 Oct): 800,000. X, 100 bottles earned 10 October, is credited late in November: it
    takes units 301-400 (100,000), B moves to 401-650 (100 × 1,000 + 150 × 1,500 = 325,000,
    +50,000), C to 651-800 (225,000, unchanged). October's 19 L: 800,000 → 950,000, +150,000.
    Listed, in allocation order: X (its credit) and B (a tier shift). A and C are not."""
    orders = (
        _credited(1, (WATER, 300), day=5),
        _credited(2, (WATER, 250), day=12),
        _credited(3, (WATER, 150), day=20),
        _credited(4, (WATER, 100), day=10, posted=NOV),
    )

    result = calculate(
        _input(
            period_month=NOV,
            orders=orders,
            origin_plans={OCT: _plan(water=ILLUSTRATION_C)},
            origin_gates={OCT: OriginGate(Decimal("1.000"), "statement")},
        )
    )

    (group,) = result.groups
    assert (group.gross, [(p.before.total, p.after.total) for p in group.products]) == (
        Decimal("150000"),
        [(Decimal("800000"), Decimal("950000"))],
    )
    assert list(group.shares.items()) == [
        (4, (Decimal("100000"), Decimal("0"))),
        (2, (Decimal("325000"), Decimal("275000"))),
    ]


def test_a_one_uzs_re_split_is_listed_and_an_unchanged_order_is_not():
    """T-TIER-19 (b), R2-3. Three juices of net 16,650 each at [(1, 3 %)]: 3 × 499.5 = 1,498.5 →
    1,499, split 500 / 500 / 499 (ties to the earlier). The third is reversed in November: 999,
    split 500 / 499. Listed: the second (500 → 499, a 1 UZS re-split) and the third (499 → 0); the
    first (500 both times) is not. The rows add up to the group's gross, −500."""
    orders = (
        _credited(1, (JUICE, 1, "16650.00"), day=1),
        _credited(2, (JUICE, 1, "16650.00"), day=2),
        _credited(3, (JUICE, 1, "16650.00"), day=3, more=(_reversal(3, 2, NOV),)),
    )

    result = calculate(
        _input(
            period_month=NOV,
            orders=orders,
            origin_plans={OCT: _plan()},
            origin_gates={OCT: OriginGate(Decimal("1.000"), "statement")},
        )
    )

    (group,) = result.groups
    assert group.shares == {2: (Decimal("499"), Decimal("500")), 3: (Decimal("0"), Decimal("499"))}
    assert group.gross == Decimal("-500")


def test_late_corrections_telescope_to_the_months_final_total():
    """T-TIER-9's arithmetic (I-34). Illustration C, October at 800,000. November: C's money falls to
    half (1,500,000 of 3,000,000): tier 2 = B 75,000 + C 112,500 = 187,500, so October's 19 L is
    687,500, −112,500. December: A reversed: before 687,500; after, B units 1-250 (250,000) and C
    units 251-400 at half (75,000) = 325,000, −362,500. 800,000 − 112,500 − 362,500 = 325,000,
    the month's final total, and each statement's `before` is the previous statement's `after`."""
    c_half = _difference(3, 2, NOV, "received_reduced", "1500000.00", {WATER: 150})

    def october_orders(*, november, december):
        """The month's orders with the lines each statement's cut reads (lines in periods ≤ it)."""
        return (
            _credited(1, (WATER, 300), day=5, more=(_reversal(1, 2, DEC),) if december else ()),
            _credited(2, (WATER, 250), day=12),
            _credited(3, (WATER, 150), day=20, more=(c_half,) if november else ()),
        )

    plan = _plan(water=ILLUSTRATION_C)
    late = dict(origin_plans={OCT: plan}, origin_gates={OCT: OriginGate(Decimal("1.000"), "statement")})

    october = calculate(_input(plan=plan, orders=october_orders(november=False, december=False)))
    november = calculate(_input(period_month=NOV, orders=october_orders(november=True, december=False), **late))
    december = calculate(_input(period_month=DEC, orders=october_orders(november=True, december=True), **late))

    (in_november,) = november.groups[0].products
    (in_december,) = december.groups[0].products
    assert [october.commission, november.late_commission, december.late_commission] == [
        Decimal("800000"),
        Decimal("-112500"),
        Decimal("-362500"),
    ]
    assert (in_november.before.total, in_november.after.total) == (Decimal("800000"), Decimal("687500"))
    assert (in_december.before.total, in_december.after.total) == (Decimal("687500"), Decimal("325000"))
    assert october.commission + november.late_commission + december.late_commission == in_december.after.total


def test_an_order_reversed_inside_its_own_month_is_not_a_sale_of_the_month():
    """V5-B12 (§4.7): `commission_orders` counts the orders counted at the cut. Two October orders;
    the second (4 bottles) is reversed in October: one order, 6 × 1,500 = 9,000. Both are listed in
    the own group (each has a line this period), the reversed one at 0. Calculator version 2 is
    what a frozen `inputs` names."""
    result = calculate(
        _input(orders=(_credited(1, (WATER, 6)), _credited(2, (WATER, 4), day=6, more=(_reversal(2, 2, OCT),))))
    )

    assert (result.commission, result.commission_orders) == (Decimal("9000"), 1)
    assert result.groups[0].shares == {1: (Decimal("9000"), None), 2: (Decimal("0"), None)}
    assert CALCULATOR_VERSION == 2


# --------------------------------------------------------------------------- #
# The gate's edges (T-VIS-4, T-VIS-5: the multiplier halves)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "counted, due, published, band_min_pct, multiplier",
    [
        (1999, 2500, 80.0, Decimal("80.0"), Decimal("1.000")),  # 79.96 % publishes 80.0 (I-4)
        (120, 200, 60.0, Decimal("60.0"), Decimal("0.800")),  # exactly 60.0
        (599, 1000, 59.9, Decimal("0.0"), Decimal("0.500")),
    ],
)
def test_the_band_follows_the_published_percentage(counted, due, published, band_min_pct, multiplier):
    result = gate(_compliance(counted, due), _plan(), is_estimate=False)

    assert (result.pct, result.band_min_pct, result.multiplier, result.rule) == (
        published,
        band_min_pct,
        multiplier,
        "band",
    )
    assert (result.due, result.counted, result.min_due, result.provisional) == (due, counted, 20, False)


def test_fewer_visits_due_than_the_plan_minimum_earn_x1():
    """T-VIS-5: Σ due 19 at 10.5 % → ×1.0 `below_min_due`; nothing due → no % and ×1.0."""
    short = gate(_compliance(2, 19), _plan(), is_estimate=False)
    empty = gate(_compliance(0, 0), _plan(), is_estimate=True)

    assert (short.pct, short.rule, short.band_min_pct, short.multiplier) == (10.5, "below_min_due", None, Decimal("1"))
    assert (empty.pct, empty.rule, empty.multiplier, empty.provisional) == (None, "below_min_due", Decimal("1"), True)


def test_an_estimates_gate_is_provisional():
    result = calculate(_input(is_estimate=True, compliance=_compliance(18, 20)))

    assert (result.gate.multiplier, result.gate.provisional) == (Decimal("1.000"), True)


# --------------------------------------------------------------------------- #
# employed_next_month (I-26)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "employment_end, month, employed",
    [
        (None, OCT, True),
        (date(2026, 11, 1), OCT, True),
        (date(2026, 10, 31), OCT, False),
        (date(2026, 11, 30), NOV, False),
        (date(2026, 12, 31), DEC, False),
        (date(2027, 1, 1), DEC, True),
    ],
)
def test_employed_next_month(employment_end, month, employed):
    assert employed_next_month(employment_end, month) is employed


# --------------------------------------------------------------------------- #
# carry_in_view: the one labeller of a carry-in (§4.8)
# --------------------------------------------------------------------------- #

APPROVED_AT = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
AGENT_ID = 41


def _period(db, month, status):
    stamped = status != "open"
    period = SalesPayPeriod(
        month_start=month,
        status=status,
        is_shadow=False,
        holidays=[],
        closed_at=APPROVED_AT if stamped else None,
        closed_by_user_id=1 if stamped else None,
        approved_at=APPROVED_AT if status in ("approved", "paid") else None,
        approved_by_user_id=1 if status in ("approved", "paid") else None,
    )
    db.session.add(period)
    db.session.flush()
    return period


def _owing_statement(db, period):
    """October's leaver statement: gross −100,000 → paid 0, owed 100,000."""
    statement = SalesPayStatement(
        period_id=period.id, agent_user_id=AGENT_ID, terms_id=1, plan_version_id=1, revision=1,
        computed_at=APPROVED_AT, computed_by_user_id=1, base_salary=Decimal("3000000"), working_days=30,
        worked_days=2, base_amount=Decimal("200000"), commission_amount=Decimal("0"),
        late_commission_amount=Decimal("0"), gate_visits_due=0, gate_visits_counted=0, gate_compliance_pct=None,
        gate_multiplier=Decimal("1.000"), gated_commission_amount=Decimal("0"), bonus_amount=Decimal("0"),
        penalty_amount=Decimal("300000"), adjustment_amount=Decimal("0"), variable_amount=Decimal("-300000"),
        carry_in_amount=Decimal("0"), gross_amount=Decimal("-100000"), total_amount=Decimal("0"),
        carry_out_amount=Decimal("0"), owed_amount=Decimal("100000"), inputs={},
    )
    db.session.add(statement)
    db.session.flush()
    return statement


def test_carry_in_view_names_the_statement_whose_balance_it_nets(db):
    from business_app.services.sales.pay_statement_service import SalesPayStatementService

    october = _period(db, OCT, "approved")
    november = _period(db, NOV, "open")
    source = _owing_statement(db, october)

    view = SalesPayStatementService.carry_in_view(
        november, AGENT_ID, carry_in_amount=Decimal("-100000"), owed_in_statement_id=source.id
    )

    assert view == {"amount": Decimal("-100000"), "from_month": "2026-10", "source": "owed"}


def test_carry_in_view_names_the_month_a_carry_row_came_from(db):
    from business_app.services.sales.pay_statement_service import SalesPayStatementService

    october = _period(db, OCT, "approved")
    november = _period(db, NOV, "open")
    db.session.add(
        SalesPayAdjustment(
            agent_user_id=AGENT_ID, period_id=november.id, amount=Decimal("-100000"), reason="carry_forward 2026-10",
            source="carry_forward", carried_from_period_id=october.id, created_by_user_id=1,
        )
    )
    db.session.flush()

    view = SalesPayStatementService.carry_in_view(
        november, AGENT_ID, carry_in_amount=Decimal("-100000"), owed_in_statement_id=None
    )

    assert view == {"amount": Decimal("-100000"), "from_month": "2026-10", "source": "carry_forward"}


def test_carry_in_view_of_nothing_brought_in(db):
    from business_app.services.sales.pay_statement_service import SalesPayStatementService

    november = _period(db, NOV, "open")

    view = SalesPayStatementService.carry_in_view(
        november, AGENT_ID, carry_in_amount=Decimal("0"), owed_in_statement_id=None
    )

    assert view == {"amount": Decimal("0"), "from_month": None, "source": None}
