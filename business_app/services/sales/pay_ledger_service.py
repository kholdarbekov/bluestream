"""The sales-agent earnings ledger and its one writer (spec §4.4).

`SalesPayLedgerService.sync` compares, per candidate order, the level the order counts at NOW (its
money level and its units per credited product) with the level the ledger has already posted for
it, and appends the change: a credit, a difference, a reversal or a re-credit (the decision table,
§4.4.3). Lines record levels, never money (v5, Approach A): what a level pays moves with the other
orders of its month, so the calculator decides it at month level (§4.7). This is the only code
that writes `sales_pay_ledger_lines`. No order, payment or cash flow calls into pay, because the
negative edges (a cash correction, a downward edit, a method switch) have no hook to call from.

Three entry points reach it:
- the nightly `sales.sync_pay_ledger` (01:40 local);
- the throttled on-demand `ensure_fresh`, before an estimate is read;
- the forced sync a plan version or a terms row runs after it commits, and a close.

Two syncs of one agent serialize on the open period rows (`lock_open_periods`, the lock a
close takes too); the unique idempotency key is the backstop; Redis is only a throttle. The
posted state is derived from the lines on every read (`OrderCreditState`) and never stored
twice; the level rules are `pay_rules`' and are only called here.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from business_app import db, redis_client
from business_app.models.order import Order, OrderEditHistory, OrderItem
from business_app.models.payment import Payment
from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayPeriod
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_rules import (
    CENT,
    LEDGER_SLOT_KEYS,
    Level,
    ResolvedPlan,
    agent_placed_clause,
    basis_inputs,
    basis_inputs_from_snapshot,
    basis_snapshot,
    counted_level,
    credit_still_earned,
    difference_cause,
    earned_instant,
    lost_slot_race,
    order_is_earned,
    received_amount,
    units_by_product,
)
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.utils import local_windows, order_timing
from business_app.utils.payment_projection import get_payment_projection
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.transactions import atomic_transaction
from shared.redis_keyspace import RedisKeyspace
from shared.staff_constants import SALES_PAY_COMMISSION_KINDS

logger = logging.getLogger(__name__)

# Order-id IN lists are chunked like `order_timing.delivered_instants_by_order`'s.
_IN_CHUNK = 500


@dataclass
class SyncStats:
    """What one run did (§4.4.5). `as_dict()` is the task's result and `last_sync_stats`."""

    agents: int = 0
    credited: int = 0
    reversed: int = 0
    differences: int = 0
    bonuses: int = 0
    skipped_no_terms: List[int] = field(default_factory=list)
    skipped_future: List[int] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: int = 0
    skipped: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class OrderCreditState:
    """What the ledger already says about one order (§4.4.2), derived from its posted
    commission lines on every read.

    `lines` are the order's commission lines in id order, each with its period loaded:
    "settled" is a property of the PERIOD a line sits in (no longer open), which is why
    `_posted_lines_by_order` joins the periods. An order's lines are posted in non-decreasing
    month order (`route` only moves forward as months close), so the last line in a period
    that is no longer open is the order's settled line. Lines carry no money (v5): the state is
    the order's level and the two floors that cap the next one (I-18, I-41).
    """

    def __init__(self, lines: Sequence[SalesPayLedgerLine]):
        self.lines: List[SalesPayLedgerLine] = list(lines)

    @property
    def credited(self) -> bool:
        """An episode is open: more credits than reversals."""
        kinds = [line.kind for line in self.lines]
        return kinds.count("commission_credit") > kinds.count("commission_reversal")

    @property
    def latest(self) -> Optional[SalesPayLedgerLine]:
        return self.lines[-1] if self.lines else None

    @property
    def level(self) -> Optional[Level]:
        """The level the order counts at (I-31, I-41): `counted_level` over its lines, None before
        the first credit or after a reversal. The calculator reads the same lines the same way."""
        return counted_level((line.kind, _line_level(line)) for line in self.lines)

    @property
    def first_credit(self) -> Optional[SalesPayLedgerLine]:
        """The order's first credit ever. Its snapshot holds the frozen lines, the earned instant
        and `received_ref` that every later line is read against (I-19)."""
        return next((line for line in self.lines if line.kind == "commission_credit"), None)

    @property
    def received_ref(self) -> Optional[Decimal]:
        first = self.first_credit
        return None if first is None else Decimal(first.snapshot["received_ref"])

    @property
    def units_ref(self) -> Optional[Dict[int, int]]:
        """The units at credit per product, read from the first credit's frozen lines: stored once,
        as `items`, never as a second map (B3-5). None before the first credit."""
        first = self.first_credit
        return None if first is None else units_by_product(basis_inputs_from_snapshot(first.snapshot)[2])

    @property
    def settled_line(self) -> Optional[SalesPayLedgerLine]:
        """The last line whose period is no longer open (I-18); None while all are open."""
        return next((line for line in reversed(self.lines) if line.period.status != "open"), None)

    @property
    def settled_reversed(self) -> bool:
        """A settled reversal is final: the order is never credited again (I-18, Q14)."""
        settled = self.settled_line
        return settled is not None and settled.kind == "commission_reversal"

    @property
    def received_floor(self) -> Optional[Decimal]:
        """The highest money level a new line may apply (I-18): the settled line's
        `received_applied` (0 after a settled reversal), else `received_ref`. It never rises,
        because every line applies at most the floor in force when it is written."""
        if self.first_credit is None:
            return None
        settled = self.settled_line
        if settled is None:
            return self.received_ref
        if settled.kind == "commission_reversal":
            return Decimal("0.00")
        return Decimal(settled.snapshot["received_applied"])

    @property
    def units_floor(self) -> Optional[Dict[int, int]]:
        """The most units a new line may count per credited product (I-18, I-41): the settled
        line's `units_applied` (none after a settled reversal), else the units at credit. Like
        `received_floor` it never rises."""
        if self.first_credit is None:
            return None
        settled = self.settled_line
        if settled is None:
            return self.units_ref
        if settled.kind == "commission_reversal":
            return {product_id: 0 for product_id in self.units_ref}
        return dict(_line_level(settled).units)


class SyncContext:
    """One agent's slice of one run: the locked open periods, the clock, the shared stats,
    and the two append methods, the only places a ledger line is constructed."""

    def __init__(
        self,
        agent_id: int,
        now: datetime,
        stats: SyncStats,
        *,
        epoch_start_utc: datetime,
        open_periods: List[SalesPayPeriod],
        periods_by_month: Dict[date, SalesPayPeriod],
    ):
        self.agent_id = agent_id
        self.now = now
        self.stats = stats
        self.epoch_start_utc = epoch_start_utc
        self.open_periods = open_periods
        self.periods_by_month = periods_by_month
        self._plans: Dict[date, Optional[ResolvedPlan]] = {}

    def route(self, earned_month: date) -> Optional[SalesPayPeriod]:
        routed = SalesPayPeriodService.route(
            earned_month, now=self.now, periods_by_month=self.periods_by_month, open_periods=self.open_periods
        )
        return None if routed is None else routed[0]

    def plan_for(self, month: date) -> Optional[ResolvedPlan]:
        """`version_for(agent, month)`, once per month per run."""
        if month not in self._plans:
            self._plans[month] = SalesPayPlanService.version_for(self.agent_id, month)
        return self._plans[month]

    def append_line(
        self,
        order: Order,
        state: OrderCreditState,
        *,
        kind: str,
        cause: Optional[str],
        earned_month: date,
        occurred_at: datetime,
        plan_version_id: int,
        period: SalesPayPeriod,
        snapshot: dict,
    ) -> SalesPayLedgerLine:
        # The key names the SLOT, not the content (§4.4.4): two runs that read the same posted
        # lines compute the same next slot, and the unique index lets exactly one of them write.
        line = self._insert(
            SalesPayLedgerLine(
                agent_user_id=self.agent_id,
                period=period,
                kind=kind,
                cause=cause,
                order_id=order.id,
                plan_version_id=plan_version_id,
                amount=None,
                earned_month=earned_month,
                occurred_at=occurred_at,
                idempotency_key=f"commission:{order.id}:{len(state.lines) + 1}",
                snapshot=snapshot,
            )
        )
        state.lines.append(line)
        if kind == "commission_credit":
            self.stats.credited += 1
        elif kind == "commission_reversal":
            self.stats.reversed += 1
        else:
            self.stats.differences += 1
        return line

    def append_bonus_line(
        self,
        outlet,
        *,
        amount: Decimal,
        earned_month: date,
        occurred_at: datetime,
        plan_version_id: int,
        period: SalesPayPeriod,
        snapshot: dict,
    ) -> SalesPayLedgerLine:
        line = self._insert(
            SalesPayLedgerLine(
                agent_user_id=self.agent_id,
                period=period,
                kind="new_outlet_bonus",
                cause=None,
                outlet_id=outlet.id,
                plan_version_id=plan_version_id,
                amount=amount,
                earned_month=earned_month,
                occurred_at=occurred_at,
                idempotency_key=f"new_outlet_bonus:{outlet.id}",
                snapshot=snapshot,
            )
        )
        # Not counted here: `SalesPayBonusService.evaluate_agent` counts a bonus once its outlet's
        # SAVEPOINT has committed, so a rolled-back outlet never reaches `stats.bonuses`.
        return line

    @staticmethod
    def _insert(line: SalesPayLedgerLine) -> SalesPayLedgerLine:
        db.session.add(line)
        db.session.flush()
        return line


def _money(value) -> str:
    """Order money in a snapshot: a decimal string on the 0.01 grid (§3.1)."""
    return str(Decimal(value).quantize(CENT))


def _iso(instant: datetime) -> str:
    return ensure_utc(instant).isoformat()


def _enum_value(value):
    return getattr(value, "value", value)


def _units(units) -> Dict[str, int]:
    """Units in a snapshot: product id as a JSON string -> units (§3.2)."""
    return {str(product_id): int(count) for product_id, count in sorted(units.items())}


def _line_level(line: SalesPayLedgerLine) -> Level:
    """The level a commission line recorded (§3.2): a credit's or a difference's `received_applied`
    and `units_applied`. A reversal counts nothing; `counted_level` never returns its level."""
    if line.kind == "commission_reversal":
        return Level(Decimal("0.00"), {})
    return Level(
        Decimal(line.snapshot["received_applied"]),
        {int(product_id): int(count) for product_id, count in line.snapshot["units_applied"].items()},
    )


def _level_snapshot(
    order: Order, *, earned_instant: str, received: Decimal, received_ref: Decimal, level: Level
) -> dict:
    """A credit, re-credit or difference line's record (§3.2): the money and the units observed on
    the live order (evidence, like `received`), and the level the line applies. No pay amount: the
    calculator derives it (§4.7)."""
    return {
        "order_number": order.order_number,
        "earned_instant": earned_instant,
        "payment_method": _enum_value(order.payment_method),
        "received": _money(received),
        "received_ref": _money(received_ref),
        "received_applied": _money(level.applied),
        "units": _units(units_by_product(order.order_items)),
        "units_applied": _units(level.units),
    }


def _observed(order: Order, received: Decimal) -> dict:
    """What a reversal saw: why the order no longer earns (§3.2)."""
    collected = get_payment_projection(order.payment)["amount_collected"] if order.payment is not None else 0
    return {
        "status": _enum_value(order.status),
        "is_paid": bool(order.is_paid),
        "payment_method": _enum_value(order.payment_method),
        "total_amount": _money(order.total_amount or 0),
        "amount_collected": _money(collected),
        "received": _money(received),
    }


def _candidate_orders(agent_id: int, now: datetime, full: bool) -> List[Order]:
    """The agent's PLACED orders one run looks at (§4.4.1).

    Windowed by default. An order is a candidate when it was created within the lookback, or
    touched within the touch window: the order row, its payment (a collection or a
    correction, which `Order.updated_at` misses), or an admin edit (an equal-subtotal edit
    may not move `updated_at` either). It is also a candidate while it carries a commission
    line in a still-open month: a cheap catch for a restoration that touches no row the windows
    read (v5: a plan version re-rates nothing; the calculator applies it).
    `full=True` drops the windows; it is for recovery only
    (`celery call sales.sync_pay_ledger --kwargs '{"full": true}'`).
    """
    query = Order.query.options(
        selectinload(Order.order_items).selectinload(OrderItem.product),
        selectinload(Order.payment),
    ).filter(agent_placed_clause([agent_id]))
    if not full:
        created_since = now - timedelta(days=int(current_app.config["SALES_PAY_SYNC_LOOKBACK_DAYS"]))
        touched_since = now - timedelta(days=int(current_app.config["SALES_PAY_SYNC_TOUCH_DAYS"]))
        query = query.filter(
            or_(
                Order.created_at >= created_since,
                Order.updated_at >= touched_since,
                Order.id.in_(select(Payment.order_id).where(Payment.updated_at >= touched_since)),
                Order.id.in_(select(OrderEditHistory.order_id).where(OrderEditHistory.edited_at >= touched_since)),
                Order.id.in_(
                    select(SalesPayLedgerLine.order_id)
                    .join(SalesPayPeriod, SalesPayLedgerLine.period_id == SalesPayPeriod.id)
                    .where(
                        SalesPayLedgerLine.kind.in_(SALES_PAY_COMMISSION_KINDS),
                        SalesPayLedgerLine.agent_user_id == agent_id,
                        SalesPayPeriod.status == "open",
                    )
                ),
            )
        )
    return query.order_by(Order.id).all()


def _posted_lines_by_order(agent_user_id: int, order_ids: Sequence[int]) -> Dict[int, List[SalesPayLedgerLine]]:
    """Every commission line of these orders, in id order, each with its period joined:
    one query per 500 orders."""
    ids = sorted({int(order_id) for order_id in order_ids})
    posted: Dict[int, List[SalesPayLedgerLine]] = {}
    for start in range(0, len(ids), _IN_CHUNK):
        rows = (
            SalesPayLedgerLine.query.options(joinedload(SalesPayLedgerLine.period))
            .filter(
                SalesPayLedgerLine.agent_user_id == agent_user_id,
                SalesPayLedgerLine.kind.in_(SALES_PAY_COMMISSION_KINDS),
                SalesPayLedgerLine.order_id.in_(ids[start : start + _IN_CHUNK]),
            )
            .order_by(SalesPayLedgerLine.id)
            .all()
        )
        for row in rows:
            posted.setdefault(row.order_id, []).append(row)
    return posted


def _credit_level(order: Order, state: OrderCreditState, received: Decimal) -> Tuple[Decimal, Level]:
    """The ONE level rule, shared by the sync (credit, re-credit, difference) and the pipeline
    estimate (§4.8): `(received_ref, Level(received_applied, units_applied))`.

    A first credit takes the money received now and every unit on the order. Every later line is
    capped by the settled floors (I-18): the money by `received_floor`, and per credited product
    the units on the live order now by `units_floor` (OQ-B3, I-41). A product the first credit did
    not hold never counts (I-19). No money is computed here (Approach A).
    """
    now = units_by_product(order.order_items)
    if state.first_credit is None:
        return received, Level(received, now)
    floor = state.units_floor
    return state.received_ref, Level(
        min(received, state.received_floor),
        {product_id: min(now.get(product_id, 0), floor[product_id]) for product_id in state.units_ref},
    )


def _claim_fresh_window(agent_id: int) -> bool:
    """SET NX EX on the agent's throttle key: True when this caller should sync. A Redis
    failure answers True, because the throttle only saves work: the period lock and the
    unique key keep the ledger right without it."""
    try:
        return bool(
            redis_client.set(
                RedisKeyspace.sales_pay_fresh(agent_id),
                "1",
                nx=True,
                ex=int(current_app.config["SALES_PAY_ONDEMAND_SYNC_SECONDS"]),
            )
        )
    except Exception:  # noqa: BLE001 - a throttle is never a reason to skip the sync
        logger.warning("Sales pay throttle unavailable for agent %s; syncing anyway", agent_id, exc_info=True)
        return True


class SalesPayLedgerService:
    @staticmethod
    def sync(
        agent_user_ids: Optional[Sequence[int]] = None, *, now: Optional[datetime] = None, full: bool = False
    ) -> SyncStats:
        """Reconcile the agents' commission lines with their orders, one transaction per agent.

        `None` means every agent who ever had terms, leavers included. An empty list syncs
        nobody: `ensure_fresh` hands its throttled subset here, and an empty subset must not
        turn into a sync of the whole team.
        """
        # Lazy, as the plan and terms services import this module lazily: the bonus module
        # imports SalesPayPlanService, whose post-commit `ensure_fresh` imports this module.
        from business_app.services.sales.pay_bonus_service import SalesPayBonusService

        now = ensure_utc(now or local_windows.local_now())
        SalesPayPeriodService.ensure_periods(now)
        epoch = SalesPayPeriodService.epoch()
        if epoch is None:
            return SyncStats(skipped="not_started")
        stats = SyncStats()
        epoch_start_utc = local_windows.month_bounds(epoch)[0]
        agent_ids = SalesPayTermsService.agent_ids_with_terms() if agent_user_ids is None else list(agent_user_ids)
        for agent_id in agent_ids:
            with atomic_transaction():
                # The lock a close takes too: while this agent's lines are being posted no open
                # month can be frozen, and a second sync of the agent waits here, then reads the
                # lines this one committed and finds nothing to do (§4.15).
                open_periods = SalesPayPeriodService.lock_open_periods()
                ctx = SyncContext(
                    int(agent_id),
                    now,
                    stats,
                    epoch_start_utc=epoch_start_utc,
                    open_periods=open_periods,
                    # populate_existing: a month closed while this run waited on its lock must read
                    # 'closed' here, not the status loaded before the wait (§10.5).
                    periods_by_month={
                        period.month_start: period for period in SalesPayPeriod.query.populate_existing().all()
                    },
                )
                SalesPayLedgerService._sync_agent(ctx, full=full)
                # Same transaction, same period locks: a bonus is routed while no close can move a month.
                SalesPayBonusService.evaluate_agent(ctx)
            stats.agents += 1
        SalesPayPeriodService.record_sync(now, stats)
        return stats

    @staticmethod
    def ensure_fresh(agent_user_ids: Sequence[int], *, now: Optional[datetime] = None, force: bool = False) -> datetime:
        """Sync these agents before an estimate is read (C12), and return the as-of instant.

        Throttled per agent to one sync per SALES_PAY_ONDEMAND_SYNC_SECONDS; `force=True`
        skips the throttle and leaves its key alone. Never raises into the read: a failure is
        logged, the session rolled back, and the read uses the ledger as it stands.
        """
        as_of = ensure_utc(now or local_windows.local_now())
        agent_ids = [int(agent_id) for agent_id in agent_user_ids]
        due = agent_ids if force else [agent_id for agent_id in agent_ids if _claim_fresh_window(agent_id)]
        if not due:
            return as_of
        try:
            SalesPayLedgerService.sync(due, now=as_of)
        except Exception:  # noqa: BLE001 - an estimate must still answer
            db.session.rollback()
            logger.exception("On-demand sales pay sync failed for agents %s; reading the ledger as it stands", due)
        return as_of

    @staticmethod
    def _sync_agent(ctx: SyncContext, *, full: bool) -> None:
        orders = _candidate_orders(ctx.agent_id, ctx.now, full)
        order_ids = [order.id for order in orders]
        delivered = order_timing.delivered_instants_by_order(order_ids)
        posted = _posted_lines_by_order(ctx.agent_id, order_ids)
        for order in orders:
            try:
                # One SAVEPOINT per order: a fault or a lost key race costs that order alone.
                with db.session.begin_nested():
                    SalesPayLedgerService._sync_order(
                        ctx, order, OrderCreditState(posted.get(order.id, [])), delivered.get(order.id)
                    )
            except IntegrityError as exc:
                if lost_slot_race(exc, LEDGER_SLOT_KEYS):
                    ctx.stats.conflicts += 1  # a concurrent run won this slot
                    logger.warning(
                        "Sales pay ledger slot for order %s (agent %s) was taken by a concurrent run",
                        order.id,
                        ctx.agent_id,
                    )
                else:  # any other constraint is a fault in this order: name it, never a silent "conflict"
                    logger.exception("Sales pay sync failed for order %s", order.id)
                    ctx.stats.failed.append({"agent_id": ctx.agent_id, "order_id": order.id, "error": "IntegrityError"})
            except Exception as exc:  # noqa: BLE001 - one order must not stop the agent's others
                logger.exception("Sales pay sync failed for order %s", order.id)
                ctx.stats.failed.append({"agent_id": ctx.agent_id, "order_id": order.id, "error": type(exc).__name__})

    @staticmethod
    def _sync_order(ctx: SyncContext, order: Order, state: OrderCreditState, delivered_at: Optional[datetime]) -> None:
        """One row of the decision table (§4.4.3) for one order. Every line records a level; its
        money, and any tier shift of the month's other orders, is the calculator's (§4.7)."""
        received = received_amount(order)  # I-17
        if state.first_credit is None:
            # DELIVERED with no history row cannot be dated; it is left alone.
            if not (order_is_earned(order) and delivered_at is not None):
                return
            instant = earned_instant(order, delivered_at)
            if instant < ctx.epoch_start_utc:
                return  # earned before pay started: never credited
            month = local_windows.local_month(instant)
            target = ctx.route(month)
            if target is None:
                ctx.stats.skipped_future.append(order.id)
                return
            plan = ctx.plan_for(month)  # must resolve; recorded as evidence only
            if plan is None:
                ctx.stats.skipped_no_terms.append(order.id)
                return
            received_ref, level = _credit_level(order, state, received)
            ctx.append_line(
                order,
                state,
                kind="commission_credit",
                cause=None,
                earned_month=month,
                occurred_at=instant,
                plan_version_id=plan.version_id,
                period=target,
                # The order's contribution, frozen once (I-19): its lines, units and nets.
                snapshot={
                    **_level_snapshot(
                        order, earned_instant=_iso(instant), received=received, received_ref=received_ref, level=level
                    ),
                    **basis_snapshot(*basis_inputs(order)),
                },
            )
            return

        # Every later line carries the first credit's month and instant (I-19, I-30).
        credit_month = state.first_credit.earned_month
        credit_instant = state.first_credit.snapshot["earned_instant"]
        still, cause = credit_still_earned(order, received_ref=state.received_ref)
        if not state.credited:
            if state.settled_reversed or not still:
                return  # a settled reversal is final (I-18); an unsettled one waits for money
            received_ref, level = _credit_level(order, state, received)
            ctx.append_line(
                order,
                state,
                kind="commission_credit",
                cause=None,
                earned_month=credit_month,
                occurred_at=ctx.now,
                plan_version_id=ctx.plan_for(credit_month).version_id,  # evidence only (M7)
                period=ctx.route(credit_month),  # the reversal's own, still open, period
                # The re-credit marker (plan ruling PR8): A9's `is_recredit` reads this key; a
                # first credit never carries it.
                snapshot={
                    **_level_snapshot(
                        order, earned_instant=credit_instant, received=received, received_ref=received_ref, level=level
                    ),
                    "recredit": True,
                },
            )
            return

        if not still:
            ctx.append_line(
                order,
                state,
                kind="commission_reversal",
                cause=cause,
                earned_month=credit_month,
                occurred_at=ctx.now,
                plan_version_id=state.latest.plan_version_id,
                period=ctx.route(credit_month),
                # The order's units leave its month's count (I-31).
                snapshot={
                    "order_number": order.order_number,
                    "observed": _observed(order, received),
                    "received_ref": _money(state.received_ref),
                },
            )
            return

        _received_ref, level = _credit_level(order, state, received)  # both floored (I-18, I-41)
        change = difference_cause(state.level, level)  # OQ-B3: units first, a fall first
        if change is None:
            return  # every increase of the order itself lands here (D-Q3)
        ctx.append_line(
            order,
            state,
            kind="commission_difference",
            cause=change,
            earned_month=credit_month,
            occurred_at=ctx.now,
            plan_version_id=ctx.plan_for(credit_month).version_id,  # evidence only (M7)
            period=ctx.route(credit_month),
            snapshot=_level_snapshot(
                order, earned_instant=credit_instant, received=received, received_ref=state.received_ref, level=level
            ),
        )
