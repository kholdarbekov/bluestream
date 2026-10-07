"""Sales-agent pay: the rules every pay surface reads, each written once.

Spec: docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md, §4.3 (earning and the
commission basis), §4.10 (the plan dataclasses) and §4.13 (the self-decision rule).

The ledger sync, the new-outlet bonus, the pipeline estimate and the agent KPIs all ask the same
questions: did this agent place the order, is it earned and when, how much money was received for
it, and what does it earn. Each has one answer here. A second spelling anywhere else is how the KPI
card and the pay statement come to count different orders (spec R3).

Nothing here writes, commits or reads a clock. Two functions touch the database: `agent_placed_clause`
builds a clause for the caller's query, and `basis_inputs` reads the order's lines and each product's
translated name. The rest is arithmetic on what it is handed.

Money is Decimal throughout. `round_uzs` is the one whole-UZS rounding (C8): once per earned
month, product and tier, inside `tier_allocation`, never on order money. `tier_allocation` is the
only place a rate meets a unit (S-36).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import AbstractSet, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError

from business_app.models.order import Order
from business_app.models.user import User
from business_app.utils import local_windows
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from business_app.utils.exceptions import ForbiddenError, ValidationError
from business_app.utils.payment_projection import get_payment_projection
from business_app.utils.timezone_utils import ensure_utc
from shared.business_config import MAX_PAY_AMOUNT
from shared.enums import OrderStatus, PaymentMethod
from shared.staff_constants import (
    SALES_PAY_DIFFERENCE_CAUSES,
    SALES_PAY_LEDGER_KINDS,
    SALES_PAY_RATE_MODES,
    SALES_PAY_REVERSAL_CAUSES,
)

UZS = Decimal("1")
CENT = Decimal("0.01")
_ZERO = Decimal("0.00")

_PER_UNIT, _PERCENT = SALES_PAY_RATE_MODES
_NOT_DELIVERED, _NOTHING_RECEIVED = SALES_PAY_REVERSAL_CAUSES
# The difference causes, named once from the shared vocabulary: `difference_cause` is the one
# place that picks among them (OQ-B3: units first, a fall first).
_RECEIVED_REDUCED, _RECEIVED_RESTORED, _UNITS_REDUCED, _UNITS_RESTORED = SALES_PAY_DIFFERENCE_CAUSES
_COMMISSION_DIFFERENCE = SALES_PAY_LEDGER_KINDS[2]
_COMMISSION_REVERSAL = SALES_PAY_LEDGER_KINDS[1]

# A credit line freezes each product's name in every language the drill-down and the bot render,
# so a renamed or deactivated product still reads as it did when the order was credited.
_NAME_LANGUAGES = ("en", "uz", "ru")


def product_names(product) -> Dict[str, str]:
    """A product's name in every pay language (§3.2): the one way pay writes a name, shared by a
    credit's frozen `items` (`basis_inputs`) and a statement's frozen plan (§4.8)."""
    return {language: product.get_translated("name", language) for language in _NAME_LANGUAGES}


# ------------------------------------------------------------------ rounding and allocation (C8)


def round_uzs(value: Decimal) -> Decimal:
    """Whole UZS, half-up (C8). Python's `round` is half-even and would pay 1,504 for 1,504.5."""
    return Decimal(value).quantize(UZS, rounding=ROUND_HALF_UP)


def check_amount_bound(amount: Decimal, *, field: str) -> None:
    """Final-review M2: one pay input carries at most `MAX_PAY_AMOUNT` UZS either way.

    Called BEFORE any `round_uzs`, so "1e30" (which `quantize` cannot hold) and an infinity are
    refused as typos, never a 500; and a sum past the bound never reaches a `Numeric(14,2)` column
    or a close that freezes every agent in one transaction.
    """
    if not amount.is_finite() or abs(amount) > MAX_PAY_AMOUNT:
        raise ValidationError(
            f"The {field} is past the largest amount one pay input may carry",
            error_code="SALES_PAY_AMOUNT_INVALID",
            details={"field": field, "max": MAX_PAY_AMOUNT},
        )


def allocate(total: Decimal, weights: Sequence[Decimal], *, grid: Decimal) -> List[Decimal]:
    """Split `total` (a multiple of `grid`) across non-negative `weights` so the shares sum to it EXACTLY.

    Largest remainder first; a tie goes to the lower index. Callers pass the weights sorted (order
    lines by `OrderItem.id`, tier segments in allocation order, I-30), so the same input always
    splits the same way. The cent grid splits discounts and nets; the whole-UZS grid splits a tier's
    amount into order shares (C8, I-33).
    """
    weight_sum = sum(weights, Decimal("0"))
    if total == 0 or weight_sum == 0:
        return [Decimal("0").quantize(grid) for _ in weights]
    raw = [total * weight / weight_sum for weight in weights]
    shares = [value.quantize(grid, rounding=ROUND_DOWN) for value in raw]
    left = int((total - sum(shares, Decimal("0"))) / grid)
    for index in sorted(range(len(weights)), key=lambda i: (-(raw[i] - shares[i]), i))[:left]:
        shares[index] += grid
    return shares


def allocate_cents(total: Decimal, weights: Sequence[Decimal]) -> List[Decimal]:
    """`allocate` on the 0.01 grid: discount shares and the split of a line's net."""
    return allocate(total, weights, grid=CENT)


# ------------------------------------------------------------------ attribution and earning (C3)


def agent_placed_clause(agent_user_ids: Iterable[int]):
    """SQL for "the agent PLACED it" (C3): placed at a visit AND by one of these agents.

    The staff id alone would sweep in an order the same person placed from the admin panel, and the
    source alone carries no agent. `ix_orders_order_source_created_by_staff_id` serves it.
    """
    return and_(
        Order.order_source == ORDER_SOURCE_SALES_AGENT,
        Order.created_by_staff_id.in_(list(agent_user_ids)),
    )


def order_is_earned(order: Order) -> bool:
    """The first-credit rule (C3): delivered AND paid, on any channel. A business-account order is
    invoiced rather than paid at the door, so it is earned on delivery."""
    return order.status == OrderStatus.DELIVERED and (
        bool(order.is_paid) or order.payment_method == PaymentMethod.BUSINESS_ACCOUNT
    )


def earned_instant(order: Order, delivered_at: datetime) -> datetime:
    """When an earned order was earned, in UTC: the later of delivered and paid, and delivered alone
    for a business-account order (I-1, I-2).

    `delivered_at` is `order_timing.delivered_instants_by_order`'s. Paid is `Order.paid_at`, never
    the payment's own `paid_at`, which a gateway verification can set without touching the order.
    """
    delivered = ensure_utc(delivered_at)
    if order.payment_method == PaymentMethod.BUSINESS_ACCOUNT or order.paid_at is None:
        return delivered
    return max(delivered, ensure_utc(order.paid_at))


# ------------------------------------------------------------------ money received (D-Q3)


def received_amount(order: Order) -> Decimal:
    """I-17: money received FOR the order, on any rail, capped at what the order now costs.

    The payment projection gives a completed prepaid rail (the business account included)
    `amount_collected == amount`, and cash what was collected. The cap is what makes a downward edit
    count: the edit leaves `amount_collected` at the collected figure and books the difference as
    customer credit. The delivery fee is part of the total, so it counts here.
    """
    if order.payment is None:
        return _ZERO
    collected = get_payment_projection(order.payment)["amount_collected"]
    return max(_ZERO, min(collected, Decimal(order.total_amount or 0)))


def credit_still_earned(order: Order, *, received_ref: Decimal) -> Tuple[bool, Optional[str]]:
    """D-Q3: does an open credit episode stand, and if not, why (`SALES_PAY_REVERSAL_CAUSES`).

    It is reversed in full only when the order is no longer delivered, or nothing is received for it
    any more. A partial drop is a difference that lowers the order's level and an increase is
    ignored, so `is_paid` is never read: an upward edit un-pays every rail but the business account.
    `received_ref > 0` stops a zero-total order from being credited and reversed on alternate runs.
    """
    if order.status != OrderStatus.DELIVERED:
        return False, _NOT_DELIVERED  # unreachable today (DELIVERED has no way out); defensive
    if received_ref > 0 and received_amount(order) == 0:
        return False, _NOTHING_RECEIVED
    return True, None


def scaled_by_money(raw: Decimal, *, applied: Decimal, received_ref: Decimal) -> Decimal:
    """D-Q3: what `raw` earns at money level `applied` against the reference `received_ref`.

    Unrounded, because a tier rounds once (C8); never above `raw` (I-32). A zero reference (a
    zero-total order) keeps `raw`.
    """
    if received_ref <= 0:
        return raw
    return raw * min(applied, received_ref) / received_ref


@dataclass(frozen=True)
class Level:
    """What an order counts at a cut (I-31, I-41): its money level and its units per credited product."""

    applied: Decimal  # received_applied (I-18)
    units: Mapping[int, int]  # units_applied: credited product id -> units counted (OQ-B3)


def units_by_product(lines) -> Dict[int, int]:
    """Σ quantity per product over non-reward lines.

    Over the first credit's frozen lines: the units at credit (`basis_inputs` froze no reward line).
    Over the live order's `order_items`: the units now, the only thing besides the money that the
    sync reads from an order after its first credit (OQ-B3, I-41).
    """
    units: Dict[int, int] = {}
    for line in lines:
        if getattr(line, "is_reward_item", False):
            continue
        units[line.product_id] = units.get(line.product_id, 0) + int(line.quantity)
    return units


def counted_level(lines: Iterable[Tuple[str, Level]]) -> Optional[Level]:
    """`(kind, level)` of an order's commission lines in id order -> the level it counts at: the last
    line's, or None when there is no line or the last is a reversal (I-31). The sync's
    `OrderCreditState.level` and the calculator's `OrderView.counted_at` both call it."""
    lines = list(lines)
    if not lines or lines[-1][0] == _COMMISSION_REVERSAL:
        return None
    return lines[-1][1]


def difference_cause(old: Level, new: Level) -> Optional[str]:
    """The cause of the difference line that moves an order from level `old` to `new`; None when
    they are equal (OQ-B3, B3-4). One line records both the money and the units, so one cause names
    it: the units first, and a fall first. `units_reduced` if any product's units fell, else
    `units_restored` if any rose, else `received_reduced` / `received_restored` by the money level.
    No threshold: a level that moves by 0.01 is a line (§4.4.3)."""
    products = set(old.units) | set(new.units)
    if any(new.units.get(product, 0) < old.units.get(product, 0) for product in products):
        return _UNITS_REDUCED
    if any(new.units.get(product, 0) > old.units.get(product, 0) for product in products):
        return _UNITS_RESTORED
    if new.applied < old.applied:
        return _RECEIVED_REDUCED
    if new.applied > old.applied:
        return _RECEIVED_RESTORED
    return None


def follows_money(kind: str, cause: Optional[str], *, is_recredit: bool) -> bool:
    """D-Q3 display rule (§5.2, §6.2 item 2): a re-credit, or any difference.

    Each moves the order's level, money or units, and settles with its month: once that month
    closes the level is final, and money or units arriving later need an adjustment (I-18). v5
    retired `plan_changed`, the one difference that did not, so the cause no longer decides; it is
    kept so A9's call reads the line as stored. A9 publishes the answer per line (ruling T14-R1),
    so the drill-down never decides it.
    """
    return is_recredit or kind == _COMMISSION_DIFFERENCE


# ------------------------------------------------------------------ the sync's slots (§4.4.4)

# The unique keys a concurrent sync can win: by Postgres constraint name, with the `table.column`
# SQLite names in its message instead (SQLite reports no constraint name). Any other
# IntegrityError is a fault in that order or outlet, not a race (final-review M3).
LEDGER_SLOT_KEYS = {"uq_sales_pay_ledger_lines_idempotency_key": "sales_pay_ledger_lines.idempotency_key"}
BONUS_SLOT_KEYS = {
    **LEDGER_SLOT_KEYS,
    "uq_sales_pay_outlet_checks_outlet_id": "sales_pay_outlet_checks.outlet_id",
}


def lost_slot_race(exc: IntegrityError, keys: Mapping[str, str]) -> bool:
    """True when `exc` is a concurrent run winning one of `keys`, False for any other constraint."""
    name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if name:
        return name in keys
    message = str(exc.orig)
    return any(column in message for column in keys.values())


# ------------------------------------------------------------------ self-decision (D-Q11)


def is_self(agent_user_id: int, actor_user_id: Optional[int]) -> bool:
    """The one spelling of "a decision about the actor's own pay". The write guard, its check-mode
    twin and the statement tag (§4.13) all call it. A system write has no actor and is never self."""
    return actor_user_id is not None and int(agent_user_id) == int(actor_user_id)


def self_decision_refused(agent_user_id: int, actor: User) -> bool:
    """The guard in check mode: True when `actor` may NOT decide about `agent_user_id`, i.e. a
    non-admin deciding about themselves. `check_self_decision` raises exactly when this is True, and
    OA1 publishes `can_decide` from it, so the button and the refusal cannot disagree.

    The role is read from the users row (`User.is_admin`), never from `g.current_user_role`, whose
    type differs between decorators.
    """
    return is_self(agent_user_id, actor.id) and not actor.is_admin


def check_self_decision(agent_user_id: int, actor: User) -> bool:
    """D-Q11 (owner, 2026-09-28): refuse a non-admin deciding about their own pay.

    `actor` is the users row the caller already loads for its `*_by_user_id` column. An admin may
    decide about themselves: True comes back, and the caller audits it with `self_decided`. In
    practice the refusal meets a manager with an agent profile proposing about themselves (M2), or
    deciding a held order they placed or whose outlet they onboarded (OA2/OA3, called once per
    decision subject, I-24).
    """
    if self_decision_refused(agent_user_id, actor):
        raise ForbiddenError(
            "You cannot propose or decide anything about your own pay",
            error_code="SALES_PAY_SELF_DECISION",
            details={"agent_user_id": int(agent_user_id)},
        )
    return is_self(agent_user_id, actor.id)


# ------------------------------------------------------------------ new-outlet bonus days (C5, C14)


def bonus_counted_orders(
    earned: Sequence[Tuple[Order, datetime, datetime]], approved_ids: AbstractSet[int]
) -> List[Tuple[Order, datetime, datetime]]:
    """Which of an outlet's earned orders its new-outlet bonus counts (C5 + C14, I-23).

    `earned` is `[(order, delivered_instant, earned_instant)]`, sorted by `(earned_instant, order.id)`.
    Per LOCAL delivery day an outlet contributes every staff-approved order plus the earliest other
    one. Approved orders take no slot, so the count does not depend on which was delivered first,
    and the slot goes to the earliest EARNED order, so a day whose first delivery is paid late still
    counts once.
    """
    counted: List[Tuple[Order, datetime, datetime]] = []
    days_taken = set()
    for order, delivered, instant in earned:
        if order.id in approved_ids:
            counted.append((order, delivered, instant))
            continue
        day = local_windows.local_date(delivered)
        if day not in days_taken:
            days_taken.add(day)
            counted.append((order, delivered, instant))
    return counted


# ------------------------------------------------------------------ plans (§4.10)


@dataclass(frozen=True)
class Rate:
    mode: str  # SALES_PAY_RATE_MODES
    value: Decimal  # whole UZS per unit, or a percent 0-100


@dataclass(frozen=True)
class Tier:
    from_unit: int  # the tier's first unit, >= 1 (I-29)
    rate: Rate  # its own mode (I-35)


@dataclass(frozen=True)
class TierSchedule:
    """The volume tiers one product's units fill in a month (D-BANDS). First units strictly ascend
    from 1 (`SalesPayPlanService.validate`), so tiers can neither gap nor overlap. Only the first
    units are stored."""

    tiers: Tuple[Tier, ...]

    def bounds(self) -> Tuple[Tuple[int, Optional[int], Rate], ...]:
        """(from_unit, to_unit, rate) per tier: `to_unit` is the next tier's `from_unit` - 1, and None
        for the last. The one expression of a tier's upper bound (A8, A14, S1 and the push publish
        it, I-29)."""
        next_starts = [tier.from_unit for tier in self.tiers[1:]] + [None]
        return tuple(
            (tier.from_unit, None if next_start is None else next_start - 1, tier.rate)
            for tier, next_start in zip(self.tiers, next_starts)
        )


@dataclass(frozen=True)
class GateBand:
    min_pct: Decimal
    multiplier: Decimal


@dataclass(frozen=True)
class PlanConfig:
    """One plan version's numbers, as `SalesPayPlanService.validate` accepts them (spec §4.10).
    `default_tiers` pays every product without a schedule of its own (C3). `gate_bands` run strictly
    descending by `min_pct`, and the last one is 0."""

    default_tiers: TierSchedule
    product_tiers: Mapping[int, TierSchedule]
    gate_bands: Tuple[GateBand, ...]
    gate_min_visits_due: int
    bonus_amount: Decimal
    bonus_window_days: int
    bonus_min_orders_with_total: int
    bonus_min_combined_total: Decimal
    bonus_min_orders_any_amount: int
    bonus_prior_customer_lookback_days: int

    def schedule_for(self, product_id: int) -> Tuple[TierSchedule, bool]:
        """The schedule one product's units fill under this version, and whether it is the default.
        A product on the default schedule is still counted on its own (C3)."""
        own = self.product_tiers.get(product_id)
        return (self.default_tiers, True) if own is None else (own, False)


@dataclass(frozen=True)
class ResolvedPlan:
    plan_id: int
    plan_name: str
    version_id: int
    version_no: int
    effective_month: date
    config: PlanConfig


# ------------------------------------------------------------------ commission basis (§4.3.4)


@dataclass(frozen=True)
class BasisInput:
    """One order line, plan-independent. Frozen on the order's first credit (I-19)."""

    order_item_id: int
    product_id: int
    product_name: Dict[str, str]
    quantity: int
    unit_price: Decimal
    total_price: Decimal
    discount_share: Decimal
    net: Decimal


def _decimal_text(value: Decimal) -> str:
    """Money and rates inside a snapshot: a decimal string on the 0.01 grid (spec §3.1)."""
    return str(Decimal(value).quantize(CENT))


def basis_inputs(order: Order) -> Tuple[Decimal, Decimal, Tuple[BasisInput, ...]]:
    """The LIVE order's lines, with the order's discounts split across them to the cent.

    Read only before the order's first credit: for that credit, and for the pipeline estimate of a
    never-credited order. Afterwards the calculator reads the frozen lines from the credit's
    snapshot (`basis_inputs_from_snapshot`, I-19), because after an edit the discount columns are
    stale absolute amounts, and the sync reads only the live units (`units_by_product`, OQ-B3).
    Reward lines leave before `gross` is taken, and the delivery fee is never read.
    """
    items = sorted((item for item in order.order_items if not item.is_reward_item), key=lambda item: item.id)
    gross = sum((Decimal(item.total_price or 0) for item in items), _ZERO)
    discounts = sum(
        (Decimal(value or 0) for value in (order.discount_amount, order.loyalty_discount, order.tier_discount)),
        _ZERO,
    )
    order_discount = min(gross, discounts)
    shares = allocate_cents(order_discount, [Decimal(item.total_price or 0) for item in items])
    return (
        gross,
        order_discount,
        tuple(
            BasisInput(
                order_item_id=item.id,
                product_id=item.product_id,
                product_name=product_names(item.product),
                quantity=int(item.quantity),
                unit_price=Decimal(item.unit_price or 0),
                total_price=Decimal(item.total_price or 0),
                discount_share=share,
                net=max(_ZERO, Decimal(item.total_price or 0) - share),
            )
            for item, share in zip(items, shares)
        ),
    )


def basis_snapshot(gross: Decimal, order_discount: Decimal, inputs: Sequence[BasisInput]) -> dict:
    """The frozen lines as the first credit stores them (§3.2): order money as decimal strings and
    each product's name in three languages. Plan-independent: no rate and no amount (I-19)."""
    return {
        "gross": _decimal_text(gross),
        "discount_total": _decimal_text(order_discount),
        "items": [
            {
                "order_item_id": line.order_item_id,
                "product_id": line.product_id,
                "product_name": dict(line.product_name),
                "quantity": line.quantity,
                "unit_price": _decimal_text(line.unit_price),
                "total_price": _decimal_text(line.total_price),
                "discount_share": _decimal_text(line.discount_share),
                "net": _decimal_text(line.net),
            }
            for line in inputs
        ],
    }


def basis_inputs_from_snapshot(snapshot: dict) -> Tuple[Decimal, Decimal, Tuple[BasisInput, ...]]:
    """The FROZEN lines of an order's first credit (I-19): `gross`, `discount_total` and `items[]`.
    `counted_contributions` cuts them to the units the order counts (I-41)."""
    return (
        Decimal(snapshot["gross"]),
        Decimal(snapshot["discount_total"]),
        tuple(
            BasisInput(
                order_item_id=int(item["order_item_id"]),
                product_id=int(item["product_id"]),
                product_name=dict(item["product_name"]),
                quantity=int(item["quantity"]),
                unit_price=Decimal(item["unit_price"]),
                total_price=Decimal(item["total_price"]),
                discount_share=Decimal(item["discount_share"]),
                net=Decimal(item["net"]),
            )
            for item in snapshot["items"]
        ),
    )


# ------------------------------------------------------------------ the tier formula (D-BANDS, §4.3.4)


@dataclass(frozen=True)
class Contribution:
    """The counted part of one frozen order line of one product, at a cut (I-41)."""

    order_id: int
    order_item_id: int
    product_id: int
    earned_instant: datetime  # the first credit's (I-19): the allocation key (I-30)
    quantity: int  # the line's counted units
    net: Decimal  # their exact share of the line's frozen net
    applied: Decimal  # the order's money level (I-18, I-31)
    ref: Decimal  # what the order owed at credit for its counted units (I-41)


def counted_contributions(
    order_id: int, earned_instant: datetime, items: Sequence[BasisInput], level: Level, received_ref: Decimal
) -> Tuple[Contribution, ...]:
    """OQ-B3 (I-41): an order's frozen lines cut to the units it counts.

    Per product the first `level.units[p]` units count, in `order_item_id` order (B3-3); a partly
    counted line keeps its exact share of the net. `ref` is what the order owed at credit for the
    counted units, `received_ref` less the frozen net of the removed ones, so a removed unit is not
    charged twice (B3-1) and the delivery fee stays on both sides (I-17). With every unit counted it
    is `received_ref`, and a cash-only change scales as D-Q3 says.
    """
    left = dict(level.units)
    kept = []
    for item in items:  # frozen, in order_item_id order (I-30)
        counted = min(item.quantity, left.get(item.product_id, 0))
        left[item.product_id] = left.get(item.product_id, 0) - counted
        if counted == item.quantity:
            net = item.net
        else:
            net = allocate_cents(item.net, [Decimal(counted), Decimal(item.quantity - counted)])[0]
        kept.append((item, counted, net))
    ref = max(_ZERO, received_ref - sum((item.net - net for item, _counted, net in kept), _ZERO))
    return tuple(
        Contribution(
            order_id=order_id,
            order_item_id=item.order_item_id,
            product_id=item.product_id,
            earned_instant=earned_instant,
            quantity=counted,
            net=net,
            applied=level.applied,
            ref=ref,
        )
        for item, counted, net in kept
        if counted > 0
    )


@dataclass(frozen=True)
class Segment:
    """The part of one contribution inside one tier."""

    contribution: Contribution
    from_unit: int
    units: int
    net: Decimal  # allocate_cents share of the contribution's net (I-30)
    raw: Decimal  # the tier's rate at full money, unrounded
    weight: Decimal  # scaled_by_money(raw), unrounded
    share: Decimal  # whole UZS, from the tier's rounded amount (I-33)


@dataclass(frozen=True)
class TierResult:
    from_unit: int
    to_unit: Optional[int]
    rate: Rate
    units: int
    net: Decimal
    amount_full: Decimal  # round_uzs(Σ raw): display only (V5-B10)
    amount: Decimal  # round_uzs(Σ weight): pay (C8)


@dataclass(frozen=True)
class ProductTiers:
    """One product's month at one cut: the tiers holding units, ascending, and their segments."""

    product_id: int
    uses_default: bool
    units: int
    tiers: Tuple[TierResult, ...]
    segments: Tuple[Segment, ...]
    total: Decimal  # Σ tiers[].amount
    schedule: TierSchedule

    def share(self, order_id: int) -> Decimal:
        """The order's share of this product's month: Σ of its segments' shares. Σ over orders == total."""
        return sum(
            (segment.share for segment in self.segments if segment.contribution.order_id == order_id), Decimal("0")
        )

    def next_tier(self) -> Optional[Tuple[int, int, Rate]]:
        """`(from_unit, units_to_go, rate)` of the first tier above the units counted; None at the top tier."""
        for low, _high, rate in self.schedule.bounds():
            if low > self.units:
                return low, low - self.units, rate
        return None


def tier_allocation(
    product_id: int, contributions: Sequence[Contribution], schedule: TierSchedule, uses_default: bool
) -> ProductTiers:
    """THE commission formula (C3, D-BANDS): one product, one earned month, one cut. The only place a
    rate meets a unit (S-36).

    Units fill the schedule from unit 1 in (earned instant, order id, order item id) order (I-30). A
    line crossing a boundary is split by unit number, and its net by `allocate_cents`. A segment
    weighs its tier's rate at full money, scaled by its order's money level (D-Q3, I-32). Each tier
    rounds once (C8) and splits its amount into whole-UZS shares by largest remainder, ties to the
    earlier segment, so the shares add up to the tier exactly (I-33).
    """
    bounds = schedule.bounds()
    pieces = []  # (contribution, from_unit, units, net, raw, weight), in allocation order
    cursor = 0  # units counted before the contribution in hand
    for contribution in sorted(contributions, key=lambda c: (c.earned_instant, c.order_id, c.order_item_id)):
        first, last = cursor + 1, cursor + contribution.quantity
        cut = [
            (low, min(last, high or last) - max(first, low) + 1, rate)
            for low, high, rate in bounds
            if max(first, low) <= min(last, high or last)
        ]
        nets = allocate_cents(contribution.net, [Decimal(units) for _low, units, _rate in cut])
        for (low, units, rate), net in zip(cut, nets):
            raw = rate.value * units if rate.mode == _PER_UNIT else net * rate.value / 100
            weight = scaled_by_money(raw, applied=contribution.applied, received_ref=contribution.ref)
            pieces.append((contribution, low, units, net, raw, weight))
        cursor = last
    tiers: List[TierResult] = []
    segments: List[Segment] = []
    for low, high, rate in bounds:
        mine = [piece for piece in pieces if piece[1] == low]
        if not mine:
            continue
        amount = round_uzs(sum((piece[5] for piece in mine), Decimal("0")))  # C8: once per (month, product, tier)
        shares = allocate(amount, [piece[5] for piece in mine], grid=UZS)  # Σ == amount; ties -> earlier (I-33)
        segments.extend(Segment(*piece, share) for piece, share in zip(mine, shares))
        tiers.append(
            TierResult(
                from_unit=low,
                to_unit=high,
                rate=rate,
                units=sum(piece[2] for piece in mine),
                net=sum((piece[3] for piece in mine), _ZERO),
                amount_full=round_uzs(sum((piece[4] for piece in mine), Decimal("0"))),
                amount=amount,
            )
        )
    return ProductTiers(
        product_id=product_id,
        uses_default=uses_default,
        units=cursor,
        tiers=tuple(tiers),
        segments=tuple(segments),
        total=sum((tier.amount for tier in tiers), Decimal("0")),
        schedule=schedule,
    )
