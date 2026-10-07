"""The new-outlet bonus (C5, spec §4.6): the one evaluator, run inside each agent's ledger sync.

An outlet an agent onboarded earns the plan's fixed bonus once, when it becomes a real new
customer:
- it was first activated by someone else through an approval path (Q5);
- it was not already a customer before onboarding (C5, Q6);
- inside the window that opens on its first delivery (D-Q12), it reaches 2 orders with a combined
  total, or 5 orders. They are counted once per local delivery day, plus every extra same-day
  order a manager approved (Q7, C14, I-23).

State lives on `sales_pay_outlet_checks`, one row per outlet, ever:
- `_open_check` decides the verdicts that cannot be recomputed later and freezes them: whether the
  activation qualifies, whether the outlet was already a customer, the evaluation plan version
  (I-9) and the review evidence. `order_scope` reads the live branch count, so a sibling added
  next month would otherwise flip "already a customer" (review money I6).
- `_advance` walks a `tracking` row forward each night. It opens the window once, counts, and
  then either posts the bonus line, expires the row, or leaves it for tomorrow.

Every state except `tracking` is terminal, and a bonus is never reversed (I-10).

The evaluator writes only inside the caller's per-agent transaction: `SalesPayLedgerService.sync`
holds the open period rows FOR UPDATE, so `route` cannot move under it. It uses one SAVEPOINT per
outlet, as the commission pass uses one per order, so a failing outlet is reported in
`stats.failed` and rolled back alone.
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.order import Order
from business_app.models.sales import Outlet, OutletStageHistory
from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayOutletCheck
from business_app.models.user import UserAddress
from business_app.serializers.sales_serializers import _iso
from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService
from business_app.services.sales.outlet_service import OutletService
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_rules import (
    BONUS_SLOT_KEYS,
    bonus_counted_orders,
    earned_instant,
    lost_slot_race,
    order_is_earned,
)
from business_app.utils import local_windows, order_timing
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus
from shared.staff_constants import SALES_PAY_BONUS_RULES, SALES_PAY_OUTLET_CHECK_STATUSES

if TYPE_CHECKING:
    from business_app.services.sales.pay_ledger_service import SyncContext
    from business_app.services.sales.pay_rules import PlanConfig

logger = logging.getLogger(__name__)

# Q5 (owner, 2026-09-28). The FIRST `active` history row must carry one of these, written by
# someone other than the onboarder. Activation at creation (`activated`), a link to an existing
# customer (`linked`), `visit_order` and `imported` never qualify. Historical self-approvals fail
# the actor half, so they need no data fix.
QUALIFYING_FIRST_ACTIVATION_REASONS = ("approved", "attached", "job_tryout_converted")

# The one list of review flags. A8 publishes `review_flags` with exactly these keys, A2 counts the
# checks with any of them set, and the admin `pay.flag.*` keys are pinned to it. The first two are
# frozen on the check when it opens. The other three exist only on a bonus snapshot, so they read
# False/0 until a bonus is posted.
SALES_PAY_REVIEW_FLAGS = (
    "dedupe_forced",
    "nearby_other_accounts",
    "all_orders_placed_by_onboarder",
    "delivered_by_onboarder",
    "self_approved_orders",
)

TRACKING, QUALIFIED, PRIOR_CUSTOMER, NOT_ELIGIBLE, EXPIRED = SALES_PAY_OUTLET_CHECK_STATUSES
ORDERS_WITH_TOTAL, ORDERS_ANY = SALES_PAY_BONUS_RULES

# (order, delivered instant, earned instant), both instants UTC: `bonus_counted_orders`' shape.
Earned = Tuple[Order, datetime, datetime]


class SalesPayBonusService:
    """The new-outlet evaluator, and the one reading of its review flags."""

    @staticmethod
    def evaluate_agent(ctx: "SyncContext") -> None:
        """Evaluate every outlet `ctx.agent_id` onboarded that still has a decision to make.

        Imported outlets have no onboarder and are never asked. A prospect has no account yet
        and waits for its activation. Terminal checks are skipped before any query about them.
        """
        outlets = (
            Outlet.query.filter(Outlet.onboarded_by_user_id == ctx.agent_id, Outlet.user_id.isnot(None))
            .order_by(Outlet.id.asc())
            .all()
        )
        if not outlets:
            return
        checks = {
            check.outlet_id: check
            for check in SalesPayOutletCheck.query.filter(
                SalesPayOutletCheck.outlet_id.in_([outlet.id for outlet in outlets])
            )
        }
        pending = [outlet for outlet in outlets if outlet.id not in checks or checks[outlet.id].status == TRACKING]
        first_active = SalesPayBonusService._first_activation_rows(
            [outlet.id for outlet in pending if outlet.id not in checks]
        )
        for outlet in pending:
            try:
                with db.session.begin_nested():
                    posted = SalesPayBonusService._evaluate_outlet(
                        ctx, outlet, checks.get(outlet.id), first_active.get(outlet.id)
                    )
            except IntegrityError as exc:
                if not lost_slot_race(exc, BONUS_SLOT_KEYS):
                    # Any other constraint is a fault in this outlet, named (final-review M3).
                    logger.exception("sales pay bonus evaluation failed for outlet %s", outlet.id)
                    ctx.stats.failed.append(
                        {"agent_id": ctx.agent_id, "outlet_id": outlet.id, "error": "IntegrityError"}
                    )
                    continue
                # A concurrent run posted `new_outlet_bonus:{outlet_id}` (or opened the check)
                # first; the next run finds the row terminal.
                ctx.stats.conflicts += 1
                logger.warning(
                    "Sales pay bonus slot for outlet %s (agent %s) was taken by a concurrent run",
                    outlet.id,
                    ctx.agent_id,
                )
                continue
            except Exception as exc:  # noqa: BLE001 -- one outlet must not cost the agent the rest
                logger.exception("sales pay bonus evaluation failed for outlet %s", outlet.id)
                ctx.stats.failed.append({"agent_id": ctx.agent_id, "outlet_id": outlet.id, "error": type(exc).__name__})
                continue
            if posted:
                ctx.stats.bonuses += 1

    @staticmethod
    def review_flags(
        check: SalesPayOutletCheck, bonus_line: Optional[SalesPayLedgerLine] = None
    ) -> Dict[str, Union[bool, int]]:
        """Exactly `SALES_PAY_REVIEW_FLAGS`, read from the frozen rows and never recomputed: the
        check's evidence, and the bonus line's snapshot when there is one."""
        evidence = check.evidence or {}
        snapshot = bonus_line.snapshot if bonus_line is not None else {}
        return {
            "dedupe_forced": bool(evidence.get("dedupe_forced", False)),
            "nearby_other_accounts": int(evidence.get("nearby_other_accounts", 0)),
            "all_orders_placed_by_onboarder": bool(snapshot.get("all_orders_placed_by_onboarder", False)),
            "delivered_by_onboarder": bool(snapshot.get("delivered_by_onboarder", False)),
            "self_approved_orders": int(snapshot.get("self_approved_orders", 0)),
        }

    @staticmethod
    def has_review_flag(flags: Mapping[str, Union[bool, int]]) -> bool:
        """At least one flag set (true or non-zero): what A2's per-agent `review_flags` counts."""
        return any(bool(value) for value in flags.values())

    # ------------------------------------------------------------------ one outlet

    @staticmethod
    def _evaluate_outlet(
        ctx: "SyncContext",
        outlet: Outlet,
        check: Optional[SalesPayOutletCheck],
        first: Optional[OutletStageHistory],
    ) -> bool:
        """Open the outlet's check if it has none, then advance it while it is tracking. True
        when this run posted the bonus."""
        if check is None:
            check = SalesPayBonusService._open_check(ctx, outlet, first)
            if check is None or check.status != TRACKING:
                return False
        return SalesPayBonusService._advance(ctx, outlet, check)

    @staticmethod
    def _first_activation_rows(outlet_ids: Sequence[int]) -> Dict[int, OutletStageHistory]:
        """Each outlet's FIRST `to_stage='active'` history row (I-7), in one query."""
        if not outlet_ids:
            return {}
        rows = (
            OutletStageHistory.query.filter(
                OutletStageHistory.outlet_id.in_(list(outlet_ids)), OutletStageHistory.to_stage == "active"
            )
            .order_by(OutletStageHistory.created_at.asc(), OutletStageHistory.id.asc())
            .all()
        )
        first: Dict[int, OutletStageHistory] = {}
        for row in rows:
            first.setdefault(row.outlet_id, row)
        return first

    @staticmethod
    def _open_check(
        ctx: "SyncContext", outlet: Outlet, first: Optional[OutletStageHistory]
    ) -> Optional[SalesPayOutletCheck]:
        """Create the outlet's one check and freeze its verdicts (§4.6 `_open_check`).

        None means "not yet": the outlet has no first activation, or the agent has no plan for
        this month, and tomorrow's run asks again. Everything written here is final.
        """
        if first is None:
            return None
        frozen = {
            "outlet_id": outlet.id,
            "agent_user_id": ctx.agent_id,
            "activation_reason": first.reason_code,
            "activation_actor_user_id": first.actor_user_id,
            "activated_at": first.created_at,
            "onboarded_at": outlet.created_at,
        }
        if first.reason_code not in QUALIFYING_FIRST_ACTIVATION_REASONS:
            ineligible: Optional[str] = "activation_reason"
        elif first.actor_user_id == outlet.onboarded_by_user_id:
            ineligible = "self_approved"
        else:
            ineligible = None
        if ineligible is not None:
            check = SalesPayOutletCheck(
                status=NOT_ELIGIBLE,
                plan_version_id=None,
                evidence={"reason": ineligible, "approved_by_user_id": first.actor_user_id},
                **frozen,
            )
        else:
            plan = ctx.plan_for(local_windows.local_month(ctx.now))
            if plan is None:
                return None
            onboarded_at = ensure_utc(outlet.created_at)
            lookback_from = onboarded_at - timedelta(days=plan.config.bonus_prior_customer_lookback_days)
            evidence: Dict[str, Any] = {
                "scope": SalesPayBonusService._scope(outlet),
                "approved_by_user_id": first.actor_user_id,
                "dedupe_forced": bool(outlet.dedupe_candidates),
                "nearby_other_accounts": SalesPayBonusService._nearby_other_accounts(
                    outlet, lookback_from, onboarded_at
                ),
            }
            prior = SalesPayBonusService._prior_customer_orders(outlet, lookback_from, onboarded_at)
            if prior:
                evidence["prior_customer"] = {"orders": prior}
            check = SalesPayOutletCheck(
                status=PRIOR_CUSTOMER if prior else TRACKING,
                plan_version_id=plan.version_id,
                evidence=evidence,
                **frozen,
            )
        db.session.add(check)
        db.session.flush()
        return check

    @staticmethod
    def _advance(ctx: "SyncContext", outlet: Outlet, check: SalesPayOutletCheck) -> bool:
        """Walk one `tracking` check forward (§4.6 `_advance`, steps 1-9). True when it posted the bonus."""
        config = SalesPayPlanService.resolve_version(check.plan_version_id).config  # I-9: the frozen version
        delivered = SalesPayBonusService._delivered(
            Order.query.filter(OutletService.order_scope(outlet), Order.status == OrderStatus.DELIVERED).all()
        )
        onboarded_at = ensure_utc(check.onboarded_at)
        delivered = [(order, delivered_at) for order, delivered_at in delivered if delivered_at >= onboarded_at]
        if not delivered:
            return False
        if check.window_start is None:
            # D-Q12: the window opens on the first delivery on or after onboarding, from any
            # channel and paid or not, and is written once. A later scope change cannot move it.
            # Everything is read before the first write, and the pair is written together, so no
            # autoflush can land a half-open window (ck_sales_pay_outlet_checks_window_pair).
            opener, opened_at = delivered[0]
            evidence = {
                **check.evidence,
                "window_opened_by": {
                    "order_id": opener.id,
                    "order_number": opener.order_number,
                    "order_source": opener.order_source,
                    "delivered_instant": _iso(opened_at),
                    "is_paid": bool(opener.is_paid),
                },
            }
            check.window_start, check.window_end = opened_at, opened_at + timedelta(days=config.bonus_window_days)
            check.evidence = evidence
        window_start, window_end = ensure_utc(check.window_start), ensure_utc(check.window_end)

        # Step 4. The lower bound keeps an older order that joins a widened scope (a sibling
        # marked lost) from counting; an order paid after the window ends does not count.
        earned: List[Earned] = []
        for order, delivered_at in delivered:
            if not order_is_earned(order) or delivered_at < window_start:
                continue
            earned_at = ensure_utc(earned_instant(order, delivered_at))
            if earned_at <= window_end:
                earned.append((order, delivered_at, earned_at))
        earned.sort(key=lambda row: (row[2], row[0].id))
        counted = bonus_counted_orders(earned, AgentOrderApprovalService.approved_ids([row[0].id for row in earned]))

        met = SalesPayBonusService._first_met(counted, config)
        if met is None:
            if ctx.now > window_end:
                check.status = EXPIRED
            return False
        met_at, rule, qualifying = met
        if met_at < ctx.epoch_start_utc:
            evidence = {**check.evidence, "reason": "met_before_pay_start"}
            check.status, check.evidence = NOT_ELIGIBLE, evidence
            return False
        earned_month = local_windows.local_month(met_at)
        period = ctx.route(earned_month)
        if period is None:
            return False  # met in a month that has not started yet: a later run posts it
        line = ctx.append_bonus_line(
            outlet,
            amount=config.bonus_amount,
            earned_month=earned_month,
            occurred_at=met_at,
            plan_version_id=check.plan_version_id,
            period=period,
            snapshot=SalesPayBonusService._bonus_snapshot(outlet, check, config, rule, qualifying),
        )
        # The line id is read first: `qualified` and its line are one CHECK
        # (ck_sales_pay_outlet_checks_qualified), so they are written together.
        line_id = line.id
        check.status, check.ledger_line_id = QUALIFIED, line_id
        db.session.flush()
        return True

    # ------------------------------------------------------------------ evidence

    @staticmethod
    def _scope(outlet: Outlet) -> Dict[str, Any]:
        """What `order_scope` means for this outlet right now, in terms an admin can check later."""
        return {
            "user_id": outlet.user_id,
            "address_id": outlet.address_id,
            "branch_mode": OutletService.is_branch(outlet),
            "pin": None if outlet.latitude is None else {"latitude": outlet.latitude, "longitude": outlet.longitude},
        }

    @staticmethod
    def _delivered(orders: Sequence[Order]) -> List[Tuple[Order, datetime]]:
        """Each order with its delivered instant (I-1), ordered by (instant, id). One grouped
        query; an order with no DELIVERED history row has no instant and is left out."""
        instants = order_timing.delivered_instants_by_order([order.id for order in orders])
        rows = [(order, ensure_utc(instants[order.id])) for order in orders if order.id in instants]
        return sorted(rows, key=lambda row: (row[1], row[0].id))

    @staticmethod
    def _prior_customer_orders(outlet: Outlet, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """The delivered orders that make this outlet an existing customer (§4.6 step 4).

        A counting order was delivered in `[start, end)` and meets one of two tests:
        - (a) it is in the outlet's order scope as it stands at this first evaluation;
        - (b) Q6: it is from the outlet's own account, delivered to an address within
          SALES_DEDUPE_RADIUS_M of the pin.

        (b) closes the attach path. There the branch's brand-new address leaves (a) empty, while
        the account has been buying at the same door (review gaming F3).
        """
        candidates = {
            order.id: order
            for order in Order.query.filter(OutletService.order_scope(outlet), Order.status == OrderStatus.DELIVERED)
        }
        if outlet.latitude is not None:
            same_account = (
                db.session.query(Order, UserAddress.latitude, UserAddress.longitude)
                .join(UserAddress, UserAddress.id == Order.delivery_address_id)
                .filter(Order.user_id == outlet.user_id, Order.status == OrderStatus.DELIVERED)
            )
            for order, latitude, longitude in same_account:
                if (
                    OutletService._km_within_dedupe_radius(outlet.latitude, outlet.longitude, latitude, longitude)
                    is not None
                ):
                    candidates[order.id] = order
        return [
            {"order_id": order.id, "order_number": order.order_number, "delivered_instant": _iso(delivered_at)}
            for order, delivered_at in SalesPayBonusService._delivered(list(candidates.values()))
            if start <= delivered_at < end
        ]

    @staticmethod
    def _nearby_other_accounts(outlet: Outlet, start: datetime, end: datetime) -> int:
        """How many orders OTHER accounts took delivery of at this place in `[start, end)` (§4.6
        step 5). They are flagged, never refused: SALES_DEDUPE_RADIUS_M also catches the
        neighbours in a bazaar or an apartment block. "Other" is every account but the outlet's
        own, accounts linked to it (CustomerLinkService clusters) included, because Q6 treats
        only `Order.user_id == outlet.user_id` as the same customer."""
        if outlet.latitude is None:
            return 0
        lat_lo, lat_hi, lng_lo, lng_hi = OutletService._dedupe_box(outlet.latitude, outlet.longitude)
        rows = (
            db.session.query(Order, UserAddress.latitude, UserAddress.longitude)
            .join(UserAddress, UserAddress.id == Order.delivery_address_id)
            .filter(
                Order.user_id != outlet.user_id,
                Order.status == OrderStatus.DELIVERED,
                UserAddress.latitude.between(lat_lo, lat_hi),
                UserAddress.longitude.between(lng_lo, lng_hi),
            )
        )
        nearby = [
            order
            for order, latitude, longitude in rows
            if OutletService._km_within_dedupe_radius(outlet.latitude, outlet.longitude, latitude, longitude)
            is not None
        ]
        return sum(1 for _order, delivered_at in SalesPayBonusService._delivered(nearby) if start <= delivered_at < end)

    @staticmethod
    def _first_met(counted: Sequence[Earned], config: "PlanConfig") -> Optional[Tuple[datetime, str, List[Earned]]]:
        """The first prefix of the counted orders that meets either rule (§4.6 step 6): its last
        earned instant, the rule, and the orders that met it. Q8: the combined total is
        `total_amount`, delivery fee included."""
        combined = Decimal("0.00")
        for k, (order, _delivered_at, earned_at) in enumerate(counted, start=1):
            combined += Decimal(order.total_amount or 0)
            if k >= config.bonus_min_orders_with_total and combined >= config.bonus_min_combined_total:
                return earned_at, ORDERS_WITH_TOTAL, list(counted[:k])
            if k >= config.bonus_min_orders_any_amount:
                return earned_at, ORDERS_ANY, list(counted[:k])
        return None

    @staticmethod
    def _bonus_snapshot(
        outlet: Outlet, check: SalesPayOutletCheck, config: "PlanConfig", rule: str, qualifying: Sequence[Earned]
    ) -> Dict[str, Any]:
        """The bonus line's snapshot (C18): the check's evidence, plus the window, the qualifying
        orders, the rule, the thresholds, the live scope, and the three snapshot-only review
        flags. Copied once, so later renames and scope changes never touch it (T-NEW-8)."""
        approvals = AgentOrderApprovalService.approved_rows([order.id for order, _, _ in qualifying])
        onboarder = outlet.onboarded_by_user_id
        orders = []
        for order, _delivered_at, earned_at in qualifying:
            approval = approvals.get(order.id)
            orders.append(
                {
                    "order_id": order.id,
                    "order_number": order.order_number,
                    "order_source": order.order_source,
                    "earned_instant": _iso(earned_at),
                    "total_amount": f"{Decimal(order.total_amount or 0):.2f}",
                    "placed_by_user_id": order.created_by_staff_id,
                    # `Delivery.delivery_person_id` is a users.id, so it compares with the onboarder.
                    "delivered_by_user_id": order.delivery.delivery_person_id if order.delivery is not None else None,
                    "staff_approved": approval is not None,
                    "approved_by_user_id": approval.decided_by_user_id if approval is not None else None,
                }
            )
        return {
            **check.evidence,
            "outlet_name": outlet.name,
            "scope": SalesPayBonusService._scope(outlet),
            "window": {
                "start": _iso(check.window_start),
                "end": _iso(check.window_end),
                "opened_by": check.evidence["window_opened_by"],
            },
            "qualifying_orders": orders,
            "rule_met": rule,
            "thresholds": {
                "amount": int(config.bonus_amount),
                "window_days": config.bonus_window_days,
                "min_orders_with_total": config.bonus_min_orders_with_total,
                "min_combined_total": int(config.bonus_min_combined_total),
                "min_orders_any_amount": config.bonus_min_orders_any_amount,
                "prior_customer_lookback_days": config.bonus_prior_customer_lookback_days,
            },
            "all_orders_placed_by_onboarder": all(row["placed_by_user_id"] == onboarder for row in orders),
            "delivered_by_onboarder": any(row["delivered_by_user_id"] == onboarder for row in orders),
            # Only an ADMIN onboarder can produce one: OA2 refuses a manager who is a decision
            # subject, and the onboarder always is (I-24).
            "self_approved_orders": sum(1 for row in orders if row["approved_by_user_id"] == onboarder),
        }
