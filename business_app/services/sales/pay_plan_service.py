"""Sales-agent pay plans: named, versioned commission-and-gate configurations (spec §4.10, C10).

A plan is a name. Everything it pays is on a VERSION, effective from a month and never updated,
and a plan is never deleted or deactivated.
- A change is a new version.
- A correction for a month is a newer version for that same month. `resolve` takes the latest
  `effective_month <= month`, then the highest `version_no`.
- Commission is tier schedules (D-BANDS): one `sales_pay_plan_rates` row per tier, written in the
  version's own transaction. The rows with `product_id` NULL are the version's default schedule.

`validate` is the one statement of what a version may say (the §4.10 table). The request schema
(`VersionPayload`) coerces types and refuses a band's or the bonus's single-field bounds early; this
refuses everything else, every cross-field rule and every tier-schedule limit (`MAX_TIERS`,
`MAX_FROM_UNIT`, `MAX_PER_UNIT_RATE`, `MAX_PRODUCT_SCHEDULES`, which live only here), with
SALES_PAY_PLAN_INVALID and `details.field`/`reason` (plus `index` for a `rates` or `gate_bands`
item, and `tier` / `product_id` for a tier), so the plan editor can point at the input.
"""

import copy
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, NoReturn, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from business_app import db
from business_app.models.product import Product
from business_app.models.sales_pay import SalesPayPlan, SalesPayPlanRate, SalesPayPlanVersion
from business_app.serializers.sales_serializers import person_ref
from business_app.services.sales.pay_period_service import SalesPayPeriodService, audit_pay_write
from business_app.services.sales.pay_rules import (
    CENT,
    GateBand,
    PlanConfig,
    Rate,
    ResolvedPlan,
    Tier,
    TierSchedule,
    check_amount_bound,
    round_uzs,
)
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.utils import local_windows
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.transactions import atomic_transaction
from shared.staff_constants import SALES_PAY_BONUS_RULES, SALES_PAY_RATE_MODES

# Published by A12 so the editor pre-fills from it and never keeps a copy. Tiers, `bonus_amount` and
# `bonus_min_combined_total` have no default: an admin always types them (spec §4.10).
DEFAULT_PLAN_CONFIG = {
    "gate_bands": [
        {"min_pct": 80.0, "multiplier": 1.0},
        {"min_pct": 60.0, "multiplier": 0.8},
        {"min_pct": 0.0, "multiplier": 0.5},
    ],
    "gate_min_visits_due": 20,
    "bonus_window_days": 60,
    "bonus_min_orders_with_total": 2,
    "bonus_min_orders_any_amount": 5,
    "bonus_prior_customer_lookback_days": 180,
}

PLAN_NAME_MAX_LENGTH = 100
MAX_TIERS = 10
MAX_FROM_UNIT = 1_000_000
MAX_PRODUCT_SCHEDULES = 200
MAX_GATE_BANDS = 10
MAX_PER_UNIT_RATE = Decimal("10000000")
MAX_GATE_MIN_VISITS_DUE = 10000
MAX_BONUS_WINDOW_DAYS = 365
MAX_PRIOR_CUSTOMER_LOOKBACK_DAYS = 730
_HUNDRED = Decimal("100")
_PERCENT_STEP = Decimal("0.01")
_MIN_PCT_STEP = Decimal("0.1")
_MULTIPLIER_STEP = Decimal("0.001")


def _invalid(
    field: str,
    reason: str,
    index: Optional[int] = None,
    *,
    tier: Optional[int] = None,
    product_id: Optional[int] = None,
) -> NoReturn:
    details: Dict[str, Any] = {"field": field, "reason": reason}
    for key, value in (("index", index), ("tier", tier), ("product_id", product_id)):
        if value is not None:
            details[key] = value
    raise ValidationError("The pay plan is not valid", error_code="SALES_PAY_PLAN_INVALID", details=details)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _whole_amount(value: Any, field: str) -> Decimal:
    amount = _dec(value)
    # The bound first, with its own code (M2): past it a bonus overflows `Numeric(14,2)`.
    check_amount_bound(amount, field=field)
    if amount < 0:
        _invalid(field, "out_of_range")
    if round_uzs(amount) != amount:
        _invalid(field, "not_whole")
    return round_uzs(amount)


def _int_between(value: Any, low: int, high: int, field: str) -> int:
    number = int(value)
    if not low <= number <= high:
        _invalid(field, "out_of_range")
    return number


def _rate(item: Mapping[str, Any], field: str, *, tier: int, product_id: Optional[int]) -> Rate:
    """One tier's rate (I-35): whole UZS per unit up to MAX_PER_UNIT_RATE, or a percent to 2 decimals."""
    mode = item.get("mode")
    if mode not in SALES_PAY_RATE_MODES:
        _invalid(f"{field}.mode", "mode_invalid", tier=tier, product_id=product_id)
    value = _dec(item.get("value"))
    if mode == "per_unit":
        if not 0 <= value <= MAX_PER_UNIT_RATE:
            _invalid(f"{field}.value", "out_of_range", tier=tier, product_id=product_id)
        if round_uzs(value) != value:
            _invalid(f"{field}.value", "not_whole", tier=tier, product_id=product_id)
        return Rate(mode=mode, value=round_uzs(value))
    if not 0 <= value <= _HUNDRED:
        _invalid(f"{field}.value", "out_of_range", tier=tier, product_id=product_id)
    if value != value.quantize(_PERCENT_STEP):
        _invalid(f"{field}.value", "too_many_decimals", tier=tier, product_id=product_id)
    return Rate(mode=mode, value=value.quantize(_PERCENT_STEP))


def _schedule(tiers_in: List[Mapping[str, Any]], field: str, product_id: Optional[int] = None) -> TierSchedule:
    """One schedule (§4.10): 1 to MAX_TIERS tiers whose first units start at 1 and strictly ascend, so
    no unit goes unpaid or is paid twice (I-29). Modes may mix and rates may fall with volume (I-35,
    OQ-B1's default)."""
    if not tiers_in:
        _invalid(field, "required", product_id=product_id)
    if len(tiers_in) > MAX_TIERS:
        _invalid(field, "too_many", product_id=product_id)
    tiers: List[Tier] = []
    for position, item in enumerate(tiers_in, start=1):
        where = {"tier": position, "product_id": product_id}
        if item.get("from_unit") is None:
            _invalid(f"{field}.from_unit", "required", **where)
        from_unit = int(item["from_unit"])
        if not 1 <= from_unit <= MAX_FROM_UNIT:
            _invalid(f"{field}.from_unit", "out_of_range", **where)
        if position == 1 and from_unit != 1:
            _invalid(f"{field}.from_unit", "first_not_one", **where)
        if tiers and from_unit <= tiers[-1].from_unit:
            _invalid(f"{field}.from_unit", "not_increasing", **where)
        tiers.append(Tier(from_unit=from_unit, rate=_rate(item, field, **where)))
    return TierSchedule(tiers=tuple(tiers))


def _gate_bands(bands_in: List[Mapping[str, Any]]) -> tuple:
    if not 1 <= len(bands_in) <= MAX_GATE_BANDS:
        _invalid("gate_bands", "count")
    bands: List[GateBand] = []
    for index, band in enumerate(bands_in):
        min_pct, multiplier = _dec(band["min_pct"]), _dec(band["multiplier"])
        if not 0 <= min_pct <= _HUNDRED:
            _invalid("gate_bands.min_pct", "out_of_range", index)
        if min_pct != min_pct.quantize(_MIN_PCT_STEP):
            _invalid("gate_bands.min_pct", "too_many_decimals", index)
        if not 0 <= multiplier <= 1:
            _invalid("gate_bands.multiplier", "out_of_range", index)
        if multiplier != multiplier.quantize(_MULTIPLIER_STEP):
            _invalid("gate_bands.multiplier", "too_many_decimals", index)
        if bands and min_pct >= bands[-1].min_pct:
            _invalid("gate_bands.min_pct", "not_descending", index)
        if bands and multiplier > bands[-1].multiplier:
            # A lower compliance band may never pay a higher multiplier.
            _invalid("gate_bands.multiplier", "multiplier_increases", index)
        bands.append(
            GateBand(min_pct=min_pct.quantize(_MIN_PCT_STEP), multiplier=multiplier.quantize(_MULTIPLIER_STEP))
        )
    if bands[-1].min_pct != 0:
        # The last band catches every compliance down to 0%, so no agent falls outside the table.
        _invalid("gate_bands.min_pct", "last_not_zero", len(bands) - 1)
    return tuple(bands)


def _product_schedules(rates_in: List[Mapping[str, Any]]) -> Dict[int, TierSchedule]:
    if len(rates_in) > MAX_PRODUCT_SCHEDULES:
        _invalid("rates", "too_many")
    schedules: Dict[int, TierSchedule] = {}
    for index, item in enumerate(rates_in):
        product_id = item.get("product_id")
        if product_id is None:
            _invalid("rates.product_id", "required", index)
        if product_id in schedules:
            _invalid("rates.product_id", "duplicate_product", index)
        schedules[product_id] = _schedule(list(item.get("tiers") or []), "rates.tiers", product_id)
    if schedules:
        # An inactive product may have a schedule: it can still be on an order the plan pays for (spec §3.2).
        known = {product_id for (product_id,) in db.session.query(Product.id).filter(Product.id.in_(list(schedules)))}
        for index, item in enumerate(rates_in):
            if item["product_id"] not in known:
                _invalid("rates.product_id", "unknown_product", index)
    return schedules


def _row_in_force(plan_id: int, month: date) -> Optional[SalesPayPlanVersion]:
    """The ONE resolution rule (spec §3.2): the latest effective month on or before `month`, then the
    highest `version_no`, so a same-month correction wins. `resolve` asks it for one month;
    `_coverage` asks it once per distinct effective month to publish the months each version covers."""
    return (
        SalesPayPlanVersion.query.filter(
            SalesPayPlanVersion.plan_id == plan_id, SalesPayPlanVersion.effective_month <= month
        )
        .order_by(SalesPayPlanVersion.effective_month.desc(), SalesPayPlanVersion.version_no.desc())
        .first()
    )


def _coverage(
    plan_id: int, versions: List[SalesPayPlanVersion]
) -> Tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    """The months each version is paid on. The winners are `_row_in_force`'s own answers, asked
    once per distinct effective month: at a month a version starts in, the rule's latest effective
    month is that month itself, so it answers with the month's highest `version_no`, which stays in
    force until the next effective month. The timeline therefore cannot disagree with `resolve`.

    Returns the `versions[]` fields by version id and the plan's `timeline` (one entry per distinct
    effective month, ascending), so the Plans tab renders both and never re-derives the rule. A
    version a higher `version_no` replaces within its own month applies to no month."""
    months = sorted({version.effective_month for version in versions})
    winners = {month: _row_in_force(plan_id, month) for month in months}
    last_months = {month: local_windows.previous_month(later) for month, later in zip(months, months[1:])}
    coverage: Dict[int, Dict[str, Any]] = {}
    for version in versions:
        winner = winners[version.effective_month]
        replaced = winner.id != version.id
        until = None if replaced else last_months.get(version.effective_month)
        coverage[version.id] = {
            "applies_from": None if replaced else local_windows.format_month(version.effective_month),
            "applies_until": None if until is None else local_windows.format_month(until),
            "replaced_by_version_no": winner.version_no if replaced else None,
        }
    timeline = [
        {
            "from_month": local_windows.format_month(month),
            "version_id": winners[month].id,
            "version_no": winners[month].version_no,
        }
        for month in months
    ]
    return coverage, timeline


def _bands_descending(bands: List[Mapping[str, str]]) -> List[Mapping[str, str]]:
    return sorted(bands, key=lambda band: Decimal(band["min_pct"]), reverse=True)


def _schedules(row: SalesPayPlanVersion) -> Tuple[TierSchedule, Dict[int, TierSchedule]]:
    """A stored version's tier rows as (default schedule, product schedules), each in ascending
    `from_unit`.

    "A default schedule exists", "it starts at unit 1" and "no first unit repeats" span rows, so no
    CHECK holds them (§3.2) and `validate` is their guard. A version written around it is refused
    here, as an uncoded 500, rather than rated silently."""
    by_product: Dict[Optional[int], List[Tier]] = {}
    for rate in sorted(row.rates, key=lambda rate: rate.from_unit):
        by_product.setdefault(rate.product_id, []).append(
            Tier(from_unit=rate.from_unit, rate=Rate(mode=rate.rate_mode, value=rate.rate_value))
        )
    if None not in by_product:
        raise RuntimeError(f"plan version {row.id} has no default schedule")
    for product_id, tiers in by_product.items():
        starts = [tier.from_unit for tier in tiers]
        if starts[0] != 1 or len(set(starts)) != len(starts):
            whose = "default" if product_id is None else f"product {product_id}"
            raise RuntimeError(f"plan version {row.id}: the {whose} schedule is corrupt (first units {starts})")
    default = TierSchedule(tiers=tuple(by_product.pop(None)))
    return default, {product_id: TierSchedule(tiers=tuple(tiers)) for product_id, tiers in by_product.items()}


def _tier_rows(schedule: TierSchedule) -> List[Dict[str, Any]]:
    """A schedule as A14 publishes it (raw): ascending, each tier's `to_unit` from `bounds()` (I-29)."""
    return [
        {"from_unit": from_unit, "to_unit": to_unit, "mode": rate.mode, "value": rate.value}
        for from_unit, to_unit, rate in schedule.bounds()
    ]


def tiers_json(schedule: TierSchedule) -> List[Dict[str, Any]]:
    """One schedule as stored, in JSON types: `[{from_unit, mode, value}]`, ascending, with the value at
    `rate_value`'s two decimals ("1500.00"). The plan audit (§4.17) and a statement's frozen
    `inputs.plan` (§4.8) both write it, so neither keeps its own copy of the shape."""
    return [
        {"from_unit": tier.from_unit, "mode": tier.rate.mode, "value": str(tier.rate.value.quantize(CENT))}
        for tier in schedule.tiers
    ]


def _tiers_audit(config: PlanConfig) -> Dict[str, Any]:
    """§4.17: a version's audit row holds its tiers as stored, the default schedule first."""
    return {
        "default_tiers": tiers_json(config.default_tiers),
        "rates": [
            {"product_id": product_id, "tiers": tiers_json(schedule)}
            for product_id, schedule in sorted(config.product_tiers.items())
        ],
    }


def _resolved(row: SalesPayPlanVersion) -> ResolvedPlan:
    default_tiers, product_tiers = _schedules(row)
    config = PlanConfig(
        default_tiers=default_tiers,
        product_tiers=product_tiers,
        gate_bands=tuple(
            GateBand(min_pct=Decimal(band["min_pct"]), multiplier=Decimal(band["multiplier"]))
            for band in _bands_descending(row.gate_bands)
        ),
        gate_min_visits_due=row.gate_min_visits_due,
        bonus_amount=row.bonus_amount,
        bonus_window_days=row.bonus_window_days,
        bonus_min_orders_with_total=row.bonus_min_orders_with_total,
        bonus_min_combined_total=row.bonus_min_combined_total,
        bonus_min_orders_any_amount=row.bonus_min_orders_any_amount,
        bonus_prior_customer_lookback_days=row.bonus_prior_customer_lookback_days,
    )
    return ResolvedPlan(
        plan_id=row.plan_id,
        plan_name=row.plan.name,
        version_id=row.id,
        version_no=row.version_no,
        effective_month=row.effective_month,
        config=config,
    )


def _insert_version(
    plan_id: int, version_no: int, month: date, config: PlanConfig, note: Optional[str], actor_id: int
) -> SalesPayPlanVersion:
    row = SalesPayPlanVersion(
        plan_id=plan_id,
        version_no=version_no,
        effective_month=month,
        gate_bands=[{"min_pct": str(band.min_pct), "multiplier": str(band.multiplier)} for band in config.gate_bands],
        gate_min_visits_due=config.gate_min_visits_due,
        bonus_amount=config.bonus_amount,
        bonus_window_days=config.bonus_window_days,
        bonus_min_orders_with_total=config.bonus_min_orders_with_total,
        bonus_min_combined_total=config.bonus_min_combined_total,
        bonus_min_orders_any_amount=config.bonus_min_orders_any_amount,
        bonus_prior_customer_lookback_days=config.bonus_prior_customer_lookback_days,
        note=(note or "").strip() or None,
        created_by_user_id=actor_id,
    )
    db.session.add(row)
    db.session.flush()
    schedules = [(None, config.default_tiers), *sorted(config.product_tiers.items())]
    db.session.add_all(
        [
            SalesPayPlanRate(
                plan_version_id=row.id,
                product_id=product_id,
                from_unit=tier.from_unit,
                rate_mode=tier.rate.mode,
                rate_value=tier.rate.value,
            )
            for product_id, schedule in schedules
            for tier in schedule.tiers
        ]
    )
    return row


def _assert_name_free(name: str) -> None:
    if SalesPayPlan.query.filter_by(name=name).first() is not None:
        _invalid("name", "duplicate")


class SalesPayPlanService:
    @staticmethod
    def validate(version: Mapping[str, Any]) -> PlanConfig:
        """`VersionPayload.model_dump(exclude_unset=True)` -> the `PlanConfig` it describes, or
        SALES_PAY_PLAN_INVALID naming the first input to fix (the §4.10 table)."""
        default_tiers = _schedule(list(version.get("default_tiers") or []), "default_tiers")
        product_tiers = _product_schedules(list(version.get("rates") or []))
        bands = _gate_bands(list(version.get("gate_bands") or []))
        min_visits_due = _int_between(version["gate_min_visits_due"], 0, MAX_GATE_MIN_VISITS_DUE, "gate_min_visits_due")
        bonus = version["new_outlet_bonus"]
        amount = _whole_amount(bonus["amount"], "new_outlet_bonus.amount")
        combined_total = _whole_amount(bonus["min_combined_total"], "new_outlet_bonus.min_combined_total")
        window_days = _int_between(bonus["window_days"], 1, MAX_BONUS_WINDOW_DAYS, "new_outlet_bonus.window_days")
        lookback_days = _int_between(
            bonus["prior_customer_lookback_days"],
            0,
            MAX_PRIOR_CUSTOMER_LOOKBACK_DAYS,
            "new_outlet_bonus.prior_customer_lookback_days",
        )
        with_total = int(bonus["min_orders_with_total"])
        if with_total < 1:
            _invalid("new_outlet_bonus.min_orders_with_total", "out_of_range")
        any_amount = int(bonus["min_orders_any_amount"])
        if any_amount < with_total:
            # "N orders of any amount" must ask for at least as many orders as "N orders with a total".
            _invalid("new_outlet_bonus.min_orders_any_amount", "below_min_orders_with_total")
        return PlanConfig(
            default_tiers=default_tiers,
            product_tiers=product_tiers,
            gate_bands=bands,
            gate_min_visits_due=min_visits_due,
            bonus_amount=amount,
            bonus_window_days=window_days,
            bonus_min_orders_with_total=with_total,
            bonus_min_combined_total=combined_total,
            bonus_min_orders_any_amount=any_amount,
            bonus_prior_customer_lookback_days=lookback_days,
        )

    @staticmethod
    def create_plan(
        name: str, version: Mapping[str, Any], *, actor_id: int, now: Optional[datetime] = None
    ) -> SalesPayPlan:
        """A new plan with its version 1, in one transaction (spec §4.10). Company-wide, so there is no
        self-decision check (§4.13)."""
        now = now or local_windows.local_now()
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > PLAN_NAME_MAX_LENGTH:
            _invalid("name", "length")
        month = local_windows.parse_month(version["effective_month"])
        config = SalesPayPlanService.validate(version)
        try:
            with atomic_transaction():
                _assert_name_free(clean_name)
                SalesPayPeriodService.assert_month_editable(month, now=now)
                plan = SalesPayPlan(name=clean_name, created_by_user_id=actor_id)
                db.session.add(plan)
                db.session.flush()
                row = _insert_version(plan.id, 1, month, config, version.get("note"), actor_id)
        except IntegrityError:
            # A concurrent create took the name between the check and the insert; the unique
            # constraint refused this one. Anything else is re-raised as it is.
            _assert_name_free(clean_name)
            raise
        audit_pay_write(
            "plan_created",
            resource_type="sales_pay_plan",
            resource_id=plan.id,
            new_values={
                "name": plan.name,
                "version_id": row.id,
                "version_no": 1,
                "effective_month": local_windows.format_month(month),
                **_tiers_audit(config),
            },
        )
        return plan

    @staticmethod
    def create_version(
        plan_id: int, version: Mapping[str, Any], *, actor_id: int, now: Optional[datetime] = None
    ) -> SalesPayPlanVersion:
        """The plan's next version (max + 1 under the plan row's lock; the unique pair is the backstop).

        Locks the plan row, then the effective month's period row (spec §4.15), so a version cannot
        commit into a month a concurrent close has just frozen.
        """
        now = now or local_windows.local_now()
        with atomic_transaction():
            plan = SalesPayPlan.query.filter_by(id=plan_id).with_for_update().populate_existing().one_or_none()
            if plan is None:
                raise NotFoundError(
                    "No such pay plan", error_code="SALES_PAY_NOT_FOUND", details={"resource": "plan", "id": plan_id}
                )
            month = local_windows.parse_month(version["effective_month"])
            config = SalesPayPlanService.validate(version)
            SalesPayPeriodService.assert_month_editable(month, now=now)
            last_no = (
                db.session.query(func.max(SalesPayPlanVersion.version_no))
                .filter(SalesPayPlanVersion.plan_id == plan.id)
                .scalar()
            )
            row = _insert_version(plan.id, (last_no or 0) + 1, month, config, version.get("note"), actor_id)
        audit_pay_write(
            "plan_version_created",
            resource_type="sales_pay_plan",
            resource_id=plan_id,
            new_values={
                "version_id": row.id,
                "version_no": row.version_no,
                "effective_month": local_windows.format_month(month),
                **_tiers_audit(config),
            },
        )
        # §4.10 (v5): a version writes no ledger line; the calculator rates every open month
        # with the version in force whenever it computes. The forced sync credits at once any
        # order skipped because no plan resolved (`skipped_no_terms`), rather than at 01:40.
        # Every agent with terms, not a
        # computed subset: which agents a version reaches is `terms_for` at each month from
        # its effective month on, and working that out here would be a second copy of the
        # effective-dating rule. ensure_fresh never raises; a failure leaves it to the
        # nightly run. Imported here: the ledger service imports this module.
        from business_app.services.sales.pay_ledger_service import SalesPayLedgerService

        SalesPayLedgerService.ensure_fresh(SalesPayTermsService.agent_ids_with_terms(), now=now, force=True)
        return row

    @staticmethod
    def resolve(plan_id: int, month: date) -> Optional[ResolvedPlan]:
        """The plan's version in force for `month`, or None when its first version starts later."""
        row = _row_in_force(plan_id, month)
        return None if row is None else _resolved(row)

    @staticmethod
    def resolve_version(version_id: int) -> ResolvedPlan:
        """One stored version, e.g. the evaluation version frozen on a new-outlet check (I-9)."""
        row = db.session.get(SalesPayPlanVersion, version_id)
        if row is None:
            raise NotFoundError(
                "No such pay plan version",
                error_code="SALES_PAY_NOT_FOUND",
                details={"resource": "version", "id": version_id},
            )
        return _resolved(row)

    @staticmethod
    def version_for(agent_user_id: int, month: date) -> Optional[ResolvedPlan]:
        """The plan version an agent is paid on in `month`: their terms' plan, resolved for the month
        (spec §4.10). None without terms, or before that plan's first version."""
        terms = SalesPayTermsService.terms_for(agent_user_id, month)
        return None if terms is None else SalesPayPlanService.resolve(terms.plan_id, month)

    @staticmethod
    def list_plans(*, now: Optional[datetime] = None) -> Dict[str, Any]:
        """A12, raw: every plan with its history, and what the editor needs to draw its inputs."""
        now = now or local_windows.local_now()
        plans = (
            SalesPayPlan.query.options(selectinload(SalesPayPlan.versions).joinedload(SalesPayPlanVersion.created_by))
            .order_by(SalesPayPlan.name)
            .all()
        )
        return {
            "items": [SalesPayPlanService.plan_row(plan, now=now) for plan in plans],
            "rate_modes": list(SALES_PAY_RATE_MODES),
            "bonus_rules": list(SALES_PAY_BONUS_RULES),
            "editable_from_month": local_windows.format_month(SalesPayPeriodService.editable_from_month(now)),
            "current_month": local_windows.format_month(local_windows.local_month(now)),
            "default_config": copy.deepcopy(DEFAULT_PLAN_CONFIG),
        }

    @staticmethod
    def plan_row(plan: SalesPayPlan, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """`PlanRow`, raw: the version in force this month (through `resolve`'s rule), the history
        newest first with the months each version applies to, and the `timeline` (`_coverage`)."""
        in_force = _row_in_force(plan.id, local_windows.local_month(now or local_windows.local_now()))
        coverage, timeline = _coverage(plan.id, plan.versions)
        return {
            "id": plan.id,
            "name": plan.name,
            "version_in_force": (
                None
                if in_force is None
                else {
                    "id": in_force.id,
                    "version_no": in_force.version_no,
                    "effective_month": local_windows.format_month(in_force.effective_month),
                }
            ),
            "versions": [
                {
                    "id": version.id,
                    "version_no": version.version_no,
                    "effective_month": local_windows.format_month(version.effective_month),
                    "created_at": version.created_at,
                    "created_by": person_ref(version.created_by),
                    "note": version.note,
                    **coverage[version.id],
                }
                for version in sorted(plan.versions, key=lambda version: version.version_no, reverse=True)
            ],
            "timeline": timeline,
        }

    @staticmethod
    def get_version(plan_id: int, version_id: int) -> Dict[str, Any]:
        """`VersionDetail`, raw: the stored version in the shape `VersionPayload` posts, so "New
        version" pre-fills from it. Each tier carries its derived `to_unit`, and each scheduled product
        its name and whether it is still active."""
        row = (
            SalesPayPlanVersion.query.filter_by(id=version_id, plan_id=plan_id)
            .options(
                joinedload(SalesPayPlanVersion.plan),
                joinedload(SalesPayPlanVersion.created_by),
                selectinload(SalesPayPlanVersion.rates).joinedload(SalesPayPlanRate.product),
            )
            .one_or_none()
        )
        if row is None:
            raise NotFoundError(
                "No such pay plan version",
                error_code="SALES_PAY_NOT_FOUND",
                details={"resource": "version", "id": version_id},
            )
        default_tiers, product_tiers = _schedules(row)
        products = {rate.product_id: rate.product for rate in row.rates if rate.product_id is not None}
        return {
            "id": row.id,
            "plan_id": row.plan_id,
            "plan_name": row.plan.name,
            "version_no": row.version_no,
            "effective_month": local_windows.format_month(row.effective_month),
            "default_tiers": _tier_rows(default_tiers),
            "rates": [
                {
                    "product_id": product_id,
                    "product_name": products[product_id].name,
                    "product_is_active": bool(products[product_id].is_active),
                    "tiers": _tier_rows(schedule),
                }
                for product_id, schedule in sorted(product_tiers.items())
            ],
            "gate_bands": [
                {"min_pct": Decimal(band["min_pct"]), "multiplier": Decimal(band["multiplier"])}
                for band in _bands_descending(row.gate_bands)
            ],
            "gate_min_visits_due": row.gate_min_visits_due,
            "new_outlet_bonus": {
                "amount": row.bonus_amount,
                "window_days": row.bonus_window_days,
                "prior_customer_lookback_days": row.bonus_prior_customer_lookback_days,
                "min_orders_with_total": row.bonus_min_orders_with_total,
                "min_combined_total": row.bonus_min_combined_total,
                "min_orders_any_amount": row.bonus_min_orders_any_amount,
            },
            "note": row.note,
            "created_at": row.created_at,
            "created_by": person_ref(row.created_by),
        }
