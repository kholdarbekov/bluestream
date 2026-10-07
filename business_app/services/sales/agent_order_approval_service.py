"""The same-day staff approval hold (compensation spec C14, §4.18).

An agent's second or later order at one outlet on one LOCAL day is decided at placement: it
waits PENDING for a manager or an admin instead of going to the store or straight to the pool
(I-21). This module owns the one rule (`same_day_agent_order_ids`), the hold row
(`agent_order_approvals`), the one spelling of "a hold is pending" and every decision on it.

Who owns a PENDING order (§4.18.3): every automatic confirmer asks `is_awaiting` (or reads
`awaiting_clause` through `AgentOrderConfirmationService.pending_request_filter`) and skips a held
order; every explicit confirmer goes through `OrderService.update_order_status`, whose guard
(`assert_not_awaiting`) refuses it. OA2 (`approve`) is the only way to CONFIRMED.

Nothing else re-derives "second order today": the queue shows the frozen `earlier_order_ids`
(I-22), never a re-run of the rule.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import selectinload

from business_app import db
from business_app.models.order import Order, OrderItem
from business_app.models.sales_visits import AgentOrderApproval, Visit
from business_app.models.user import User
from business_app.services.order_service import OrderService
from business_app.services.sales import notifications
from business_app.services.sales.pay_rules import check_self_decision, self_decision_refused
from business_app.utils import local_windows
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.constants import ORDER_SOURCE_SALES_AGENT
from business_app.utils.exceptions import ConflictError, NotFoundError
from business_app.utils.timezone_utils import get_utc_now
from shared.enums import OrderStatus
from shared.staff_constants import SALES_EVENT_AGENT_ORDER_APPROVED, SALES_EVENT_AGENT_ORDER_REJECTED

logger = logging.getLogger(__name__)

# Where the queue lives in the admin UI. Published in the 409's details and in the managers'
# alert, so neither the copy nor a client types the path a second time.
ORDER_APPROVALS_PATH = "/sales/order-approvals"

# The two decisions. A `cancelled` row is closed by whoever cancelled the ORDER, which is not a
# pay decision (I-24), so it is never "self-decided".
_DECIDED_STATUSES = ("approved", "rejected")


@dataclass(frozen=True)
class StaffApprovalHold:
    """What `VisitService.place_order` decided under the outlet lock, handed to `create_order`."""

    outlet_id: int
    earlier_order_ids: Tuple[int, ...]


@dataclass(frozen=True)
class ApprovalPage:
    """One page of the queue, with everything the serializer reads already loaded."""

    rows: List[AgentOrderApproval]
    earlier: Dict[int, Order]
    viewer: User
    total: int
    pending_count: int


class AgentOrderApprovalService:
    """Compensation spec §4.18: the rule, the row and the decisions."""

    @staticmethod
    def same_day_agent_order_ids(outlet_id: int, *, now: datetime) -> List[int]:
        """THE rule for "this agent order must wait" (I-21, I-22). Non-empty means hold.

        Every non-cancelled agent order placed from a visit at this outlet during the LOCAL day
        `now` falls in, whoever placed it and whatever became of it since (delivered, held).
        Another channel's order (the store's own Telegram order, an operator's phone order) is
        not an agent order, and a branch of the same account is a different outlet.
        """
        start, end = local_windows.local_day_bounds(local_windows.local_date(now))
        return [
            row.id
            for row in (
                db.session.query(Order.id)
                .join(Visit, Visit.id == Order.visit_id)
                .filter(
                    Visit.outlet_id == outlet_id,
                    Order.order_source == ORDER_SOURCE_SALES_AGENT,
                    Order.status != OrderStatus.CANCELLED,
                    Order.created_at >= start,
                    Order.created_at < end,
                )
                .order_by(Order.id)
                .all()
            )
        ]

    @staticmethod
    def open_hold(order: Order, hold: Optional[StaffApprovalHold]) -> Optional[AgentOrderApproval]:
        """Write the `pending` row inside `create_order`'s transaction; flush only.

        Called after the items flush and BEFORE the payment row, so a business-account payment
        completing inside `initialize_order_payment` already sees the hold and does not confirm
        the order. The row commits, or rolls back, with the order. None when there is no hold.
        """
        if hold is None:
            return None
        row = AgentOrderApproval(
            order_id=order.id,
            outlet_id=hold.outlet_id,
            agent_user_id=order.created_by_staff_id,
            earlier_order_ids=list(hold.earlier_order_ids),
            status="pending",
            requested_at=get_utc_now(),
        )
        db.session.add(row)
        db.session.flush()
        return row

    # One private filter underlies the four predicates below, so "a hold is <status>" has one
    # spelling whether it is asked of one order, a batch or a correlated subquery.
    @staticmethod
    def _with_status(status: str, order_filter):
        return and_(AgentOrderApproval.status == status, order_filter)

    @staticmethod
    def awaiting_clause():
        """Correlated EXISTS on `Order.id`: this order has a pending hold. For
        `AgentOrderConfirmationService.pending_request_filter`, which negates it."""
        return (
            db.session.query(AgentOrderApproval.id)
            .filter(AgentOrderApprovalService._with_status("pending", AgentOrderApproval.order_id == Order.id))
            .exists()
        )

    @staticmethod
    def is_awaiting(order_id: int) -> bool:
        """One indexed query (`uq_agent_order_approvals_order_id`)."""
        return bool(
            db.session.query(
                db.session.query(AgentOrderApproval.id)
                .filter(AgentOrderApprovalService._with_status("pending", AgentOrderApproval.order_id == order_id))
                .exists()
            ).scalar()
        )

    @staticmethod
    def _order_ids_with_status(status: str, order_ids: Iterable[int]) -> Set[int]:
        ids = {int(order_id) for order_id in order_ids}
        if not ids:
            return set()
        return {
            order_id
            for (order_id,) in db.session.query(AgentOrderApproval.order_id).filter(
                AgentOrderApprovalService._with_status(status, AgentOrderApproval.order_id.in_(ids))
            )
        }

    @staticmethod
    def awaiting_ids(order_ids: Iterable[int]) -> Set[int]:
        """Batched `is_awaiting`: the admin order list, the user detail, the S1 pipeline."""
        return AgentOrderApprovalService._order_ids_with_status("pending", order_ids)

    @staticmethod
    def approved_ids(order_ids: Iterable[int]) -> Set[int]:
        """Orders a manager or an admin approved: the new-outlet bonus's per-day filter (§4.6 step 5)."""
        return AgentOrderApprovalService._order_ids_with_status("approved", order_ids)

    @staticmethod
    def approved_rows(order_ids: Iterable[int]) -> Dict[int, AgentOrderApproval]:
        """The approved rows themselves, keyed by `order_id`: the statement's self-decision list
        and the bonus snapshot read who approved and when."""
        ids = {int(order_id) for order_id in order_ids}
        if not ids:
            return {}
        rows = AgentOrderApproval.query.filter(
            AgentOrderApprovalService._with_status("approved", AgentOrderApproval.order_id.in_(ids))
        ).all()
        return {row.order_id: row for row in rows}

    @staticmethod
    def assert_not_awaiting(order: Order) -> None:
        """The `update_order_status(→ CONFIRMED)` guard: only OA2 confirms a held order (R27)."""
        if AgentOrderApprovalService.is_awaiting(order.id):
            raise ConflictError(
                "This order waits for a manager's approval",
                error_code="ORDER_AWAITING_STAFF_APPROVAL",
                details={"order_id": order.id, "path": ORDER_APPROVALS_PATH},
            )

    @staticmethod
    def decision_subjects(row: AgentOrderApproval) -> Tuple[int, ...]:
        """I-24: whose pay a decision on this hold changes. The placer's commission (C3) and,
        because an approved order counts on its own, the outlet onboarder's bonus (C5). Two people
        after a reassignment; the onboarder is read live from the outlet."""
        return tuple(sorted({row.agent_user_id, row.outlet.onboarded_by_user_id} - {None}))

    @staticmethod
    def can_decide(row: AgentOrderApproval, viewer: User) -> bool:
        """The check-mode twin of `approve` / `reject`'s guard, published per row so the page draws
        its buttons from it: a pending row, and the viewer is refused for no subject."""
        return row.status == "pending" and not any(
            self_decision_refused(subject, viewer) for subject in AgentOrderApprovalService.decision_subjects(row)
        )

    @staticmethod
    def self_decided(row: AgentOrderApproval) -> bool:
        """An admin decided a hold whose decision changes their own pay (D-Q11, I-24). A cancel is
        not a decision, so a cancelled row never is."""
        return (
            row.status in _DECIDED_STATUSES
            and row.decided_by_user_id in AgentOrderApprovalService.decision_subjects(row)
        )

    @staticmethod
    def earlier_orders(rows: Sequence[AgentOrderApproval]) -> Dict[int, Order]:
        """ONE query for every frozen earlier id on the page, with the staff member who placed each,
        so the queue renders their LIVE status without a query per row."""
        ids = {order_id for row in rows for order_id in (row.earlier_order_ids or [])}
        if not ids:
            return {}
        orders = Order.query.options(selectinload(Order.created_by_staff)).filter(Order.id.in_(ids)).all()
        return {order.id: order for order in orders}

    # ------------------------------------------------------------------ decisions (OA2, OA3)

    @staticmethod
    def _lock(order_id: int) -> AgentOrderApproval:
        """Order row FIRST, then the hold row, both FOR UPDATE + populate_existing: the order every
        cancel path takes (it holds the order before `close_on_cancel` touches the hold), so a
        decision racing a cancel waits instead of deadlocking. `populate_existing` because
        `with_for_update` alone hands back the identity map's stale instance (the
        `OrderScheduleService.ensure_delivery_if_due` note). 404 when either row is missing."""
        order = db.session.get(Order, order_id, with_for_update=True, populate_existing=True)
        row = None
        if order is not None:
            row = (
                AgentOrderApproval.query.filter(AgentOrderApproval.order_id == order_id)
                .with_for_update()
                .populate_existing()
                .one_or_none()
            )
        if row is None:
            raise NotFoundError(
                "This order has no staff approval",
                error_code="SALES_ORDER_APPROVAL_NOT_FOUND",
                details={"order_id": order_id},
            )
        return row

    @staticmethod
    def _assert_pending(row: AgentOrderApproval) -> None:
        """The row still pending AND the locked order still PENDING. Otherwise a cancel (or an
        earlier decision) won, and the page is answered with the one code it handles."""
        order_status = row.order.status
        if row.status != "pending" or order_status != OrderStatus.PENDING:
            raise ConflictError(
                "This order no longer waits for approval",
                error_code="SALES_ORDER_APPROVAL_NOT_PENDING",
                details={
                    "order_id": row.order_id,
                    "status": row.status,
                    "order_status": getattr(order_status, "value", order_status),
                },
            )

    @staticmethod
    def _check_subjects(row: AgentOrderApproval, actor: User) -> bool:
        """`check_self_decision` once per decision subject (I-24, D-Q11): a manager subject is
        refused (403 SALES_PAY_SELF_DECISION, `details.agent_user_id` = that subject); an admin
        subject is allowed and True comes back. A list, not a generator, so every subject is
        checked before `any` answers."""
        return any(
            [check_self_decision(subject, actor) for subject in AgentOrderApprovalService.decision_subjects(row)]
        )

    @staticmethod
    def _committed_status(approval_id: int) -> Optional[str]:
        """The row's status as committed, read after a rollback."""
        return db.session.query(AgentOrderApproval.status).filter(AgentOrderApproval.id == approval_id).scalar()

    @staticmethod
    def _audit(row: AgentOrderApproval, event_type, action: str, self_decided: bool, **extra: Any) -> None:
        """§4.17: an order audit row after commit. `reason` rides along on a reject."""
        audit_logger.log_event(
            event_type=event_type,
            action=action,
            severity=AuditSeverity.MEDIUM,
            resource_type="order",
            resource_id=str(row.order_id),
            description=f"Same-day agent order {row.order.order_number} {row.status} by user {row.decided_by_user_id}",
            additional_data={
                "order_id": row.order_id,
                "order_number": row.order.order_number,
                "outlet_id": row.outlet_id,
                "agent_user_id": row.agent_user_id,
                "approval_id": row.id,
                "self_decided": self_decided,
                **extra,
            },
        )

    @staticmethod
    def _after_approve(approval_id: int, self_decided: bool) -> None:
        """Post-commit, each step in its own try: a failure is logged, never raised."""
        row = db.session.get(AgentOrderApproval, approval_id)
        try:
            notifications.notify_agent_order_event(
                row.order, row.outlet, SALES_EVENT_AGENT_ORDER_APPROVED, event_id=f"order-approval:{row.id}:approved"
            )
        except Exception:  # noqa: BLE001 — the decision is committed; the push is best effort
            logger.exception("Approval push for held order %s was not queued", row.order_id)
        try:
            AgentOrderApprovalService._audit(
                row, AuditEventType.ORDER_UPDATED, "sales_agent_order_approved", self_decided
            )
        except Exception:  # noqa: BLE001
            logger.exception("Approval audit for held order %s was not written", row.order_id)

    @staticmethod
    def _after_reject(approval_id: int, self_decided: bool, reason: str) -> None:
        """Post-commit, each step in its own try: a failure is logged, never raised."""
        row = db.session.get(AgentOrderApproval, approval_id)
        try:
            notifications.notify_agent_order_event(
                row.order,
                row.outlet,
                SALES_EVENT_AGENT_ORDER_REJECTED,
                reason=reason,
                event_id=f"order-approval:{row.id}:rejected",
            )
        except Exception:  # noqa: BLE001 — the decision is committed; the push is best effort
            logger.exception("Rejection push for held order %s was not queued", row.order_id)
        try:
            AgentOrderApprovalService._audit(
                row, AuditEventType.ORDER_CANCELLED, "sales_agent_order_rejected", self_decided, reason=reason
            )
        except Exception:  # noqa: BLE001
            logger.exception("Rejection audit for held order %s was not written", row.order_id)

    @staticmethod
    def approve(order_id: int, *, actor_id: int) -> AgentOrderApproval:
        """OA2: the only way a held order reaches CONFIRMED, and exactly the normal confirmed path:
        inventory confirmation for a non-cash order, `ensure_delivery_if_due` (the pool row, the
        broadcast, auto-assignment) and the store's ordinary `status_changed_confirmed`. The
        placement-time date rule is not re-applied: a date that passed while the order waited
        releases it at once (R31). Approval never refuses for stock (R29)."""
        row = AgentOrderApprovalService._lock(order_id)
        actor = db.session.get(User, actor_id)
        self_decided = AgentOrderApprovalService._check_subjects(row, actor)
        AgentOrderApprovalService._assert_pending(row)
        row.status, row.decided_at, row.decided_by_user_id = "approved", get_utc_now(), actor_id
        db.session.flush()  # the update_order_status guard now sees no pending hold
        approval_id = row.id
        try:
            # The note the no-Telegram agent path already writes: the customer's Track screen
            # prints `notes`.
            OrderService().update_order_status(
                row.order_id, OrderStatus.CONFIRMED, updated_by=actor_id, notes="Confirmed at sales visit"
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            # `create_delivery` commits inside the confirm path, so a raise after it leaves the
            # decision committed. A committed decision never goes silent.
            if AgentOrderApprovalService._committed_status(approval_id) == "approved":
                AgentOrderApprovalService._after_approve(approval_id, self_decided)
            raise
        AgentOrderApprovalService._after_approve(approval_id, self_decided)
        return db.session.get(AgentOrderApproval, approval_id)

    @staticmethod
    def reject(order_id: int, *, actor_id: int, reason: Any) -> AgentOrderApproval:
        """OA3: the order is cancelled through `cancel_order` (the reservation is released, the
        store gets its ordinary `status_changed_cancelled`). The reason is the hold row's and the
        cancel history row's staff-only `reason`; the customer-visible `notes` stays empty."""
        reason = OrderService.require_admin_reason(reason)  # F6: ADMIN_REASON_REQUIRED / _TOO_LONG
        row = AgentOrderApprovalService._lock(order_id)
        actor = db.session.get(User, actor_id)
        self_decided = AgentOrderApprovalService._check_subjects(row, actor)
        AgentOrderApprovalService._assert_pending(row)
        row.status, row.decided_at, row.decided_by_user_id, row.reason = "rejected", get_utc_now(), actor_id, reason
        db.session.flush()
        approval_id = row.id
        try:
            OrderService().cancel_order(row.order_id, reason=None, actor_user_id=actor_id, history_reason=reason)
        except Exception:
            db.session.rollback()
            # `cancel_order` commits the CANCELLED transition, and this row with it, before the
            # corporate-unit release and its own final commit, so a raise after that commit leaves
            # the decision committed. A committed decision never goes silent (approve's rule).
            if AgentOrderApprovalService._committed_status(approval_id) == "rejected":
                AgentOrderApprovalService._after_reject(approval_id, self_decided, reason)
            raise
        AgentOrderApprovalService._after_reject(approval_id, self_decided, reason)
        return db.session.get(AgentOrderApproval, approval_id)

    @staticmethod
    def close_on_cancel(order: Order, *, actor_id: Optional[int]) -> bool:
        """Any cancellation ends a pending hold (S-28): pending -> cancelled; flush only.

        Called from the CANCELLED branch of `OrderService._handle_status_change_actions`, which
        every cancel path reaches holding the order, so the hold row is locked second. Not a pay
        decision (I-24): no self-decision check; `decided_by_user_id` records the canceller (None
        for a system sweep). A no-op for OA3, whose row is already `rejected`."""
        row = (
            AgentOrderApproval.query.filter(
                AgentOrderApprovalService._with_status("pending", AgentOrderApproval.order_id == order.id)
            )
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
        if row is None:
            return False
        # Every value read before the first write: the CHECKs pair `status` with its stamps.
        decided_at, decided_by = get_utc_now(), (int(actor_id) if actor_id is not None else None)
        row.status, row.decided_at, row.decided_by_user_id = "cancelled", decided_at, decided_by
        db.session.flush()
        return True

    # ------------------------------------------------------------------ the queue (OA1)

    @staticmethod
    def _pending_count() -> int:
        """Every pending row, whatever the filters: the nav badge."""
        return AgentOrderApproval.query.filter(AgentOrderApproval.status == "pending").count()

    @staticmethod
    def list_rows(
        *,
        viewer_id: int,
        status: str = "pending",
        agent_user_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> ApprovalPage:
        """OA1: one page of the queue in a fixed number of queries. Pending rows oldest first (the
        next to decide on top); decided rows newest decision first."""
        filters = [AgentOrderApproval.status == status]
        if agent_user_id is not None:
            filters.append(AgentOrderApproval.agent_user_id == agent_user_id)
        base = AgentOrderApproval.query.filter(*filters)
        total = base.count()
        ordering = (
            (AgentOrderApproval.requested_at.asc(), AgentOrderApproval.id.asc())
            if status == "pending"
            else (AgentOrderApproval.decided_at.desc(), AgentOrderApproval.id.desc())
        )
        rows = (
            base.options(
                selectinload(AgentOrderApproval.order).selectinload(Order.order_items).selectinload(OrderItem.product),
                selectinload(AgentOrderApproval.outlet),
                selectinload(AgentOrderApproval.agent),
                selectinload(AgentOrderApproval.decided_by),
            )
            .order_by(*ordering)
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return ApprovalPage(
            rows=rows,
            earlier=AgentOrderApprovalService.earlier_orders(rows),
            viewer=db.session.get(User, viewer_id),
            total=total,
            pending_count=AgentOrderApprovalService._pending_count(),
        )

    @staticmethod
    def decision_page(row: AgentOrderApproval, *, viewer_id: int) -> ApprovalPage:
        """The one row OA2 / OA3 answer with, loaded the way a queue page is, for the same
        serializer."""
        return ApprovalPage(
            rows=[row],
            earlier=AgentOrderApprovalService.earlier_orders([row]),
            viewer=db.session.get(User, viewer_id),
            total=1,
            pending_count=AgentOrderApprovalService._pending_count(),
        )
