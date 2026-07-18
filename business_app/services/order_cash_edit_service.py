"""Admin order-level correction of driver-collected cash (72h window).

Order-scoped façade over CashCollectionService.adjust_event_amount: an admin
corrects the cash recorded for a delivered COD order; the correction cascades
through the existing event ledger (void + replace), payment projection,
customer prepayment credit, the driver cash session, and the denormalized
mirrors. Only the order-level event resolution, the 72h window guard, the
closed/verified-session reopen, and the preview are new here.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, DriverCashSession
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.order_timing import delivered_at_utc
from business_app.utils.transactions import atomic_transaction
from shared.enums import CashCollectionSource, DriverCashSessionStatus, OrderStatus, PaymentMethod

DEFAULT_CASH_EDIT_WINDOW_HOURS = 72

# Statuses the admin order-edit path can adjust directly (no reopen). Wider than
# CashCollectionService.ADJUSTABLE_SESSION_STATUSES because the common same-day
# case is an OPEN session, and a reopened verified/resolved session is OPEN too.
_DIRECT_ADJUSTABLE = frozenset({"open", "submitted", "partial", "mismatch", "overdue"})
# Finalized statuses we auto-reopen (-> open) before adjusting.
_REOPEN_REQUIRED = frozenset({"verified", "resolved"})
# Status set passed to adjust_event_amount (post-reopen everything is in here).
_ADMIN_ADJUSTABLE = _DIRECT_ADJUSTABLE


@dataclass
class CashEditPlan:
    order_id: int
    current_collected: Decimal
    new_amount: Decimal
    order_total: Decimal
    applied_to_order: Decimal
    projected_outstanding: Decimal
    projected_payment_status: str
    customer_credit_delta: Decimal
    session_id: Optional[int]
    session_status: Optional[str]
    session_will_reopen: bool
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_editable(self) -> bool:
        return not self.blocking_reasons

    def to_summary(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "current_collected": float(self.current_collected),
            "new_amount": float(self.new_amount),
            "order_total": float(self.order_total),
            "applied_to_order": float(self.applied_to_order),
            "projected_outstanding": float(self.projected_outstanding),
            "projected_payment_status": self.projected_payment_status,
            "customer_credit_delta": float(self.customer_credit_delta),
            "session_id": self.session_id,
            "session_status": self.session_status,
            "session_will_reopen": self.session_will_reopen,
            "is_editable": self.is_editable,
            "blocking_reasons": self.blocking_reasons,
            "warnings": self.warnings,
        }


@dataclass
class CashEditResult:
    order_id: int
    replacement_event_id: int
    summary: Dict[str, Any]
    warnings: List[str]
    post_commit_dispatch: List[Tuple[str, Tuple, Dict]] = field(default_factory=list)


class OrderCashEditService:
    """Order-level admin correction of driver-collected cash."""

    def __init__(
        self,
        *,
        cash_service: Optional[CashCollectionService] = None,
        recon_service: Optional[DriverReconciliationService] = None,
    ) -> None:
        self.cash_service = cash_service or CashCollectionService()
        self.recon_service = recon_service or DriverReconciliationService()

    # ---- window ----
    def _window_hours(self) -> int:
        return int(
            current_app.config.get("CASH_EDIT_WINDOW_HOURS", DEFAULT_CASH_EDIT_WINDOW_HOURS)
            or DEFAULT_CASH_EDIT_WINDOW_HOURS
        )

    # ---- event resolution ----
    def _event_query(self, order_id: int):
        return CashCollectionEvent.query.filter(
            CashCollectionEvent.order_id == order_id,
            CashCollectionEvent.source == CashCollectionSource.DELIVERY_COMPLETION,
            CashCollectionEvent.voided_at.is_(None),
        )

    def _resolve_event(self, order: Order, reasons: List[str]) -> Optional[CashCollectionEvent]:
        events = self._event_query(order.id).all()
        if len(events) == 0:
            reasons.append("no_cash_collection_recorded")
            return None
        if len(events) > 1:
            reasons.append("multiple_cash_events - use the cash reconciliation tool to adjust the specific event")
            return None
        return events[0]

    # ---- gating metadata for the order-detail screen ----
    def get_edit_metadata(self, order: Order) -> Dict[str, Any]:
        is_cod = order.payment_method == PaymentMethod.CASH
        delivered = order.status == OrderStatus.DELIVERED
        if not (is_cod and delivered):
            return {
                "is_collected_cash_editable": False,
                "collected_cash_edit_window_remaining_hours": None,
                "collected_cash_event_amount": None,
            }
        remaining: Optional[float] = None
        editable = True
        delivered_at = delivered_at_utc(order)
        if delivered_at is not None:
            age = (datetime.now(timezone.utc) - delivered_at).total_seconds() / 3600.0
            remaining = max(0.0, self._window_hours() - age)
            editable = age <= self._window_hours()
        events = self._event_query(order.id).all()
        if len(events) != 1:
            editable = False
        return {
            "is_collected_cash_editable": bool(editable),
            "collected_cash_edit_window_remaining_hours": (round(remaining, 1) if remaining is not None else None),
            # The edit adjusts this event; payment.amount_collected may be funded from
            # another source (card transfer, prepaid credit) and would seed a wrong figure.
            "collected_cash_event_amount": (float(events[0].amount) if len(events) == 1 else None),
        }

    # ---- preview (read-only) ----
    def preview(self, *, order_id: int, new_amount: Any) -> CashEditPlan:
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        to_dec = self.cash_service._to_decimal
        new_dec = to_dec(new_amount)
        blocking: List[str] = []
        warnings: List[str] = []

        if order.status != OrderStatus.DELIVERED:
            blocking.append(f"order_not_delivered: status is '{getattr(order.status, 'value', order.status)}'")
        if order.payment_method != PaymentMethod.CASH:
            blocking.append("order_not_cash")
        if new_dec < Decimal("0.00"):
            # 0 is valid: the admin can correct a bogus collection down to
            # "no cash collected". Only a negative amount is nonsensical.
            blocking.append("new_amount_cannot_be_negative")

        delivered_at = delivered_at_utc(order)
        if delivered_at is None:
            warnings.append("delivery_timestamp_missing - treating window as unlimited")
        else:
            age = (datetime.now(timezone.utc) - delivered_at).total_seconds() / 3600.0
            if age > self._window_hours():
                blocking.append(
                    f"cash_edit_window_expired: delivered {age:.1f}h ago, window is {self._window_hours()}h"
                )

        event = self._resolve_event(order, blocking)

        order_total = to_dec(order.total_amount)
        current_collected = to_dec(event.amount) if event else Decimal("0.00")

        session_id: Optional[int] = None
        session_status: Optional[str] = None
        session_will_reopen = False
        if event and event.driver_cash_session_id:
            session = DriverCashSession.query.get(event.driver_cash_session_id)
            if session:
                session_id = session.id
                session_status = getattr(session.status, "value", session.status)
                if session_status in _REOPEN_REQUIRED:
                    session_will_reopen = True
                    conflict = DriverCashSession.query.filter(
                        DriverCashSession.driver_user_id == session.driver_user_id,
                        DriverCashSession.id != session.id,
                        DriverCashSession.status.in_([DriverCashSessionStatus.OPEN, DriverCashSessionStatus.OVERDUE]),
                    ).first()
                    if conflict:
                        blocking.append(
                            f"cash_session_active_conflict: driver has another active session "
                            f"(id={conflict.id}); submit & verify it first"
                        )
                elif session_status not in _DIRECT_ADJUSTABLE:
                    blocking.append(f"session_not_adjustable: status '{session_status}'")

        # Project against what the allocator will actually do, not against the order total:
        # a payment already settled from another source has nothing left to apply to, so the
        # whole entry becomes customer credit.
        if event is not None:
            projection = self.cash_service.simulate_event_amount_change(
                event=event, new_amount=new_dec, order_id=order.id
            )
            applied_to_order = projection["applied_to_order"]
            projected_outstanding = projection["order_outstanding_after"]
            customer_credit_delta = projection["credit_after"] - projection["credit_before"]
            order_settled_elsewhere = projection["order_outstanding_before"] <= Decimal("0.00")
            payment_amount = projection["order_amount"] or order_total
        else:
            applied_to_order = Decimal("0.00")
            projected_outstanding = max(Decimal("0.00"), order_total - new_dec)
            customer_credit_delta = Decimal("0.00")
            order_settled_elsewhere = False
            payment_amount = order_total

        if projected_outstanding <= Decimal("0.00"):
            projected_status = "completed"
        elif payment_amount - projected_outstanding > Decimal("0.00"):
            projected_status = "partially_paid"
        else:
            projected_status = "pending"

        if event and projected_outstanding > Decimal("0.00"):
            warnings.append(
                "collected_below_order_total - order will not be fully paid; loyalty may need manual review"
            )
        if event and order_settled_elsewhere and new_dec > Decimal("0.00"):
            warnings.append(
                "order_already_settled_by_other_source - this order is already paid (card transfer "
                "or prepaid credit), so nothing applies to it and the full amount becomes customer credit"
            )
        if event and customer_credit_delta > Decimal("0.00"):
            warnings.append("surplus_credited_to_customer - auto-applies to the customer's other unpaid orders if any")

        if event is not None:
            other_active = [
                p
                for p in self.cash_service.get_active_cod_payments_for_customer(order.user_id)
                if p.order_id != order.id
            ]
            if other_active:
                warnings.append(
                    "customer_has_other_unpaid_cod_orders: corrected cash settles the customer's "
                    "oldest unpaid order first, so the per-order figures above are approximate"
                )

        return CashEditPlan(
            order_id=order.id,
            current_collected=current_collected,
            new_amount=new_dec,
            order_total=order_total,
            applied_to_order=applied_to_order,
            projected_outstanding=projected_outstanding,
            projected_payment_status=projected_status,
            customer_credit_delta=customer_credit_delta,
            session_id=session_id,
            session_status=session_status,
            session_will_reopen=session_will_reopen,
            blocking_reasons=blocking,
            warnings=warnings,
        )

    # ---- apply (transactional) ----
    def apply_edit(self, *, order_id: int, new_amount: Any, reason: str, actor_user_id: int) -> CashEditResult:
        reason = (reason or "").strip()
        if len(reason) < 5:
            raise ValidationError("reason must be at least 5 characters")

        order = Order.query.with_for_update().get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        plan = self.preview(order_id=order_id, new_amount=new_amount)
        if plan.blocking_reasons:
            raise ValidationError("; ".join(plan.blocking_reasons))

        event = self._resolve_event(order, [])
        if event is None:  # defensive; preview already guaranteed exactly one
            raise ValidationError("no_cash_collection_recorded")

        post_commit: List[Tuple[str, Tuple, Dict]] = []
        with atomic_transaction():
            if plan.session_will_reopen and plan.session_id is not None:
                session = DriverCashSession.query.get(plan.session_id)
                self.recon_service.reopen_session(
                    session_id=plan.session_id,
                    actor_user_id=actor_user_id,
                    reason=f"Collected-cash correction for order {order.order_number}: {reason}",
                    commit=False,
                )
                post_commit.append(
                    (
                        "notify_driver_session_reopened",
                        (session.driver_user_id, plan.session_id, order.id),
                        {},
                    )
                )

            replacement = self.cash_service.adjust_event_amount(
                event.id,
                new_amount=plan.new_amount,
                adjusted_by_user_id=actor_user_id,
                reason=reason,
                commit=False,
                allowed_session_statuses=_ADMIN_ADJUSTABLE,
            )

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="order_collected_cash_edited",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(order.id),
            additional_data={
                "actor_user_id": actor_user_id,
                "original_amount": float(plan.current_collected),
                "new_amount": float(plan.new_amount),
                "replacement_event_id": replacement.id,
                "session_reopened": plan.session_will_reopen,
                "reason": reason,
            },
        )

        return CashEditResult(
            order_id=order.id,
            replacement_event_id=replacement.id,
            summary=plan.to_summary(),
            warnings=plan.warnings,
            post_commit_dispatch=post_commit,
        )
