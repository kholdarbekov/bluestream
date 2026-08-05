"""Admin order-level payment-method change with full reconciliation.

Moves an order between business_account and cash/click, reconciling the
corporate prepayment ledger and the money side. Only four transitions are
allowed; a completed online PSP is terminal. Mirrors OrderCashEditService.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from business_app import db
from business_app.models.corporate import CorporatePrepaymentLedger
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.transactions import atomic_transaction
from shared.enums import (
    CashCollectionSource,
    FiscalizationStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)

logger = logging.getLogger(__name__)


def _method_value(method) -> Optional[str]:
    if method is None:
        return None
    return method.value if hasattr(method, "value") else str(method)


# card is an alias for click (matches order_service.py:156)
_NORMALIZE = {"card": "click"}
_ONLINE = {"click", "payme", "card"}
ALLOWED_TRANSITIONS = frozenset(
    {
        ("cash", "business_account"),
        ("click", "business_account"),
        ("payme", "business_account"),
        ("business_account", "cash"),
        ("business_account", "click"),
        ("business_account", "card"),
    }
)


@dataclass
class PaymentMethodEditPlan:
    order_id: int
    current_method: Optional[str]
    new_method: str
    is_delivered: bool
    transition: Optional[Tuple[str, str]]
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def is_editable(self) -> bool:
        return not self.blocking_reasons

    def to_summary(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "current_method": self.current_method,
            "new_method": self.new_method,
            "is_delivered": self.is_delivered,
            "blocking_reasons": self.blocking_reasons,
            "warnings": self.warnings,
        }


@dataclass
class PaymentMethodEditResult:
    order_id: int
    new_method: str
    corporate_action: str
    money_action: str
    warnings: List[str] = field(default_factory=list)
    post_commit_dispatch: List[Tuple[str, Tuple, Dict]] = field(default_factory=list)
    payment_link: Optional[Dict[str, Any]] = None


class OrderPaymentMethodEditService:
    def __init__(self, corporate_service: Optional[CorporateContractService] = None):
        self.corporate_service = corporate_service or CorporateContractService()

    def _order_items_as_dicts(self, order: Order) -> List[Dict[str, Any]]:
        return [
            {
                "product_id": it.product_id,
                "contract_id": it.contract_id,
                "contract_product_price_id": it.contract_product_price_id,
                "quantity": it.quantity,
            }
            for it in (order.order_items or [])
        ]

    def _allowed_targets(self, current: Optional[str]) -> List[str]:
        return sorted({to for (frm, to) in ALLOWED_TRANSITIONS if frm == current})

    def get_edit_metadata(self, order: Order) -> Dict[str, Any]:
        # Derive metadata straight from preview so the admin dropdown can never
        # diverge from what apply_edit will actually accept: a target is offered
        # iff preview(new_method=target) has no blocking reasons (this folds in
        # the status/terminal-PSP short-circuit AND the business_account
        # eligibility + round-trip guards that live only in preview).
        current = _method_value(order.payment_method)
        editable_targets = [
            target
            for target in self._allowed_targets(current)
            if self.preview(order_id=order.id, new_method=target).is_editable
        ]
        return {
            "is_payment_method_editable": bool(editable_targets),
            "allowed_target_methods": editable_targets,
        }

    def preview(self, *, order_id: int, new_method: str, bypass_cod_check: bool = False) -> PaymentMethodEditPlan:
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        current = _method_value(order.payment_method)
        new_norm = str(new_method)
        is_delivered = order.status == OrderStatus.DELIVERED
        blocking: List[str] = []
        warnings: List[str] = []

        if order.status in {OrderStatus.CANCELLED, OrderStatus.RETURNED}:
            blocking.append(f"order_not_editable_status: {getattr(order.status, 'value', order.status)}")

        transition = (current, new_norm)
        if transition not in ALLOWED_TRANSITIONS:
            blocking.append(f"transition_not_allowed: {current} -> {new_norm}")

        if current in _ONLINE and getattr(order.payment, "status", None) == PaymentStatus.COMPLETED:
            blocking.append("completed_online_payment_terminal")

        target = _NORMALIZE.get(new_norm, new_norm)
        if target == "business_account":
            if not self.corporate_service.order_qualifies_for_business_account(
                order.user, self._order_items_as_dicts(order)
            ):
                blocking.append("not_business_account_eligible")
            # Round-trip guard: blocks re-entry INTO business_account once this
            # order's corporate settlement has been reversed. Known limitation
            # (out of scope): it does not prevent a cash->business_account flip
            # (which creates a customer prepaid credit) immediately followed by
            # business_account->cash — that leaves the earlier credit floating
            # alongside a fresh COD obligation until a downstream auto-apply
            # nets them. Repeated back-and-forth flips are not handled here.
            reversed_exists = CorporatePrepaymentLedger.query.filter(
                CorporatePrepaymentLedger.order_id == order.id,
                CorporatePrepaymentLedger.idempotency_key.like("reverse:%"),
            ).first()
            if reversed_exists:
                blocking.append("corporate_settlement_previously_reversed")

        # Switching an order TO cash mints a brand-new COD obligation, so it must
        # clear the same two-armed cap (person cluster OR destination place) that
        # gates COD at order creation. Before Phase 2b this path was a silent cap
        # bypass: an order flipped to CASH after creation never passed the cap at
        # all. Admin/staff keep an explicit, audited override.
        #
        # NOTE: this is deliberately NOT applied to
        # CashCollectionService.convert_electronic_order_to_cash — that
        # conversion's debt is settled by the same personal-card transfer inside
        # the same transaction, so no open debt is ever created.
        if target == "cash" and not bypass_cod_check:
            from business_app.services.cash_collection_service import CashCollectionService

            try:
                CashCollectionService().validate_customer_can_use_cod(
                    order.user_id, delivery_address_id=order.delivery_address_id
                )
            except ValidationError as exc:
                if getattr(exc, "error_code", None) == "COD_DEBT_LIMIT_REACHED":
                    blocking.append(
                        "cod_debt_limit_reached: converting this order to cash would "
                        "exceed the COD active-debt cap (pass bypass_cod_check to override)"
                    )
                else:
                    raise

        if target == "business_account" and is_delivered:
            warnings.append("delivered_order_will_consume_prepaid_units")
        if current == "business_account" and is_delivered:
            warnings.append("delivered_business_account_marking_codes_may_need_manual_review")

        return PaymentMethodEditPlan(
            order_id=order.id,
            current_method=current,
            new_method=new_norm,
            is_delivered=is_delivered,
            transition=transition if transition in ALLOWED_TRANSITIONS else None,
            blocking_reasons=blocking,
            warnings=warnings,
        )

    # ---- apply (transactional) ----
    def apply_edit(
        self,
        *,
        order_id: int,
        new_method: str,
        reason: str,
        actor_user_id: int,
        bypass_cod_check: bool = False,
    ) -> PaymentMethodEditResult:
        reason = (reason or "").strip()
        if len(reason) < 5:
            raise ValidationError("reason must be at least 5 characters")

        order = Order.query.with_for_update().get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        plan = self.preview(order_id=order_id, new_method=new_method, bypass_cod_check=bypass_cod_check)
        if plan.blocking_reasons:
            raise ValidationError("; ".join(plan.blocking_reasons))

        target = _NORMALIZE.get(plan.new_method, plan.new_method)
        if target == "business_account":
            return self._settle_as_business_account(order=order, plan=plan, reason=reason, actor_user_id=actor_user_id)
        if target == "cash":
            return self._unwind_to_cash(
                order=order,
                plan=plan,
                reason=reason,
                actor_user_id=actor_user_id,
                bypass_cod_check=bypass_cod_check,
            )
        if target == "click":
            return self._unwind_to_click(order=order, plan=plan, reason=reason, actor_user_id=actor_user_id)
        raise ValidationError(f"transition_not_implemented: {plan.transition}")

    def _settle_as_business_account(
        self, *, order: Order, plan: PaymentMethodEditPlan, reason: str, actor_user_id: int
    ) -> PaymentMethodEditResult:
        from business_app.services.payment_service import PaymentService

        current = plan.current_method
        payment = order.payment

        with atomic_transaction():
            # 1. Flip method FIRST (payment + order). Critical precondition: while
            #    the payment still reads CASH a freed-cash auto-allocation could
            #    re-apply to this same order, and the corporate reserve/consume
            #    gate only opens once the method is business_account.
            if payment is not None:
                payment.payment_method = PaymentMethod.BUSINESS_ACCOUNT
            order.payment_method = PaymentMethod.BUSINESS_ACCOUNT
            db.session.flush()

            # 2. Reverse / release whatever the superseded method left behind.
            if current == "cash":
                money_action = self._reverse_collected_cash(
                    order=order, payment=payment, reason=reason, actor_user_id=actor_user_id
                )
            else:  # click / payme (T2)
                self._release_online_reservation(
                    order=order, payment=payment, reason=reason, actor_user_id=actor_user_id
                )
                money_action = "online_cancelled"

            # 3. Corporate settle. Idempotent: an order whose units are already
            #    reserved+consumed no-ops here; a clean order creates the rows.
            self.corporate_service.reserve_for_order(order.id, actor_user_id=actor_user_id)
            if order.status == OrderStatus.DELIVERED:
                self.corporate_service.consume_for_order(
                    order.id,
                    delivery_id=order.delivery.id if order.delivery else None,
                    actor_user_id=actor_user_id,
                )

            # 4. Settle the payment as business_account (marks it COMPLETED,
            #    normalises the prepaid projection, consumes marking codes iff
            #    the payment carries the flag). trigger_notifications=False: this
            #    is an admin payment-method reclassification of an already
            #    delivered/paid order — nothing new was "paid", so we must not
            #    re-notify the customer (a "payment successful" SMS/email/Telegram
            #    webhook). The Celery enqueue would also run pre-commit and go
            #    uncompensated on rollback.
            #
            # Commit boundary: initialize_order_payment's BUSINESS_ACCOUNT branch
            # commits the session UNCONDITIONALLY (it ignores commit=False — see
            # payment_service.py:299). It is therefore the terminal DB write of
            # this transaction and MUST remain the last DB-mutating step; do not
            # add DB writes after it, or they would land in a separate
            # transaction and escape rollback on failure.
            PaymentService().initialize_order_payment(
                order.id,
                actor_user_id=actor_user_id,
                metadata={"consume_marking_codes": bool(getattr(payment, "consume_marking_codes", False))},
                trigger_notifications=False,
                commit=False,
            )

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="order_payment_method_changed",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(order.id),
            additional_data={
                "actor_user_id": actor_user_id,
                "from_method": current,
                "to_method": "business_account",
                "money_action": money_action,
                "reason": reason,
            },
        )

        return PaymentMethodEditResult(
            order_id=order.id,
            new_method="business_account",
            corporate_action="settled_business_account",
            money_action=money_action,
            warnings=list(plan.warnings),
        )

    def _reverse_collected_cash(self, *, order: Order, payment, reason: str, actor_user_id: int) -> str:
        """Turn this order's collected COD cash into unapplied customer credit.

        Reverses every live allocation this order's payment received from a
        non-voided DELIVERY_COMPLETION event, so the collected amount becomes the
        customer's prepaid balance without touching the driver cash session or
        other orders paid by the same event. If nothing was collected (uncollected
        COD) the pending obligation is simply superseded by the business_account
        settlement — no credit is created.
        """
        from business_app.services.cash_collection_service import CashCollectionService

        if payment is None:
            return "cod_cancelled"

        allocations = (
            CashCollectionAllocation.query.join(
                CashCollectionEvent,
                CashCollectionAllocation.cash_collection_event_id == CashCollectionEvent.id,
            )
            .filter(
                CashCollectionAllocation.payment_id == payment.id,
                CashCollectionAllocation.reversed_at.is_(None),
                CashCollectionEvent.source == CashCollectionSource.DELIVERY_COMPLETION,
                CashCollectionEvent.voided_at.is_(None),
            )
            .all()
        )
        if not allocations:
            return "cod_cancelled"

        cash_service = CashCollectionService()
        for allocation in allocations:
            cash_service.reverse_allocation_to_payment(
                allocation.id,
                reversed_by_user_id=actor_user_id,
                reason=reason,
                commit=False,
            )
        return "cash_credited"

    def _release_online_reservation(self, *, order: Order, payment, reason: str, actor_user_id: int) -> None:
        if payment is None:
            return
        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

        # release_reserved_marking_codes is a no-op when nothing is reserved, but
        # mirror convert_electronic_order_to_cash and never let a fiscal hiccup
        # abort an otherwise valid settlement.
        try:
            PaymentFiscalizationService().release_reserved_marking_codes(
                payment, reason=reason, actor_user_id=actor_user_id
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Failed to release marking codes for order %s: %s", order.id, exc)

    # ---- apply (out of business_account: T3 cash, T4 click) ----
    def _marking_codes_consumed_warnings(self, payment) -> List[str]:
        """Flag (not auto-reverse) a BA payment that already consumed marking codes.

        Consumption completed iff the payment's fiscalization row reached
        COMPLETED (set by PaymentFiscalizationService.consume_marking_codes_for_
        business_account); un-using already-USED codes is out of scope here.
        """
        fiscalization = getattr(payment, "fiscalization", None) if payment is not None else None
        if (
            payment is not None
            and getattr(payment, "consume_marking_codes", False)
            and fiscalization is not None
            and fiscalization.status == FiscalizationStatus.COMPLETED
        ):
            return ["business_account_marking_codes_consumed_manual_review"]
        return []

    def _unwind_to_cash(
        self,
        *,
        order: Order,
        plan: PaymentMethodEditPlan,
        reason: str,
        actor_user_id: int,
        bypass_cod_check: bool = False,
    ) -> PaymentMethodEditResult:
        from business_app.services.cash_collection_service import CashCollectionService
        from business_app.services.payment_fiscalization_service import PaymentFiscalizationService

        payment = order.payment
        warnings = list(plan.warnings) + self._marking_codes_consumed_warnings(payment)

        with atomic_transaction():
            # 1. Return prepaid units to availability first (idempotent; no-op if
            #    this order never reserved/consumed any).
            self.corporate_service.reverse_order_prepayment(order.id, reason=reason, actor_user_id=actor_user_id)

            # 2. Flip method and reset the payment to a fresh COD obligation.
            #    ensure_cod_payment_for_order requires order.payment_method ==
            #    CASH already, and does NOT reset a COMPLETED payment back to
            #    PENDING itself — the explicit reset here is what establishes
            #    the new-method obligation before the projection sync.
            order.payment_method = PaymentMethod.CASH
            if payment is not None:
                payment.payment_method = PaymentMethod.CASH
                payment.status = PaymentStatus.PENDING
                payment.amount_collected = Decimal("0.00")
                payment.outstanding_amount = order.total_amount
                payment.paid_at = None
            order.is_paid = False
            db.session.flush()

            cash_service = CashCollectionService()
            payment = cash_service.ensure_cod_payment_for_order(order, actor_user_id=actor_user_id)

            # 3. CASH never requires Click fiscalization; mark it NOT_REQUIRED
            #    rather than leaving a stale fiscalization row behind. Never let
            #    a fiscal hiccup abort an otherwise valid unwind.
            try:
                PaymentFiscalizationService().queue_click_fiscalization(payment.id, actor_user_id=actor_user_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Failed to mark fiscalization NOT_REQUIRED for order %s: %s", order.id, exc)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="order_payment_method_changed",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(order.id),
            additional_data={
                "actor_user_id": actor_user_id,
                "from_method": "business_account",
                "to_method": "cash",
                "money_action": "cod_obligation_created",
                "reason": reason,
                # Records that an admin/operator overrode the COD active-debt cap
                # for this conversion — the override must never be silent.
                "bypass_cod_check": bypass_cod_check,
            },
        )

        return PaymentMethodEditResult(
            order_id=order.id,
            new_method="cash",
            corporate_action="reversed_prepayment",
            money_action="cod_obligation_created",
            warnings=warnings,
        )

    def _unwind_to_click(
        self, *, order: Order, plan: PaymentMethodEditPlan, reason: str, actor_user_id: int
    ) -> PaymentMethodEditResult:
        from business_app.services.payment_service import PaymentService

        payment = order.payment
        warnings = list(plan.warnings) + self._marking_codes_consumed_warnings(payment)
        payment_link: Optional[Dict[str, Any]] = None

        with atomic_transaction():
            # 1. Return prepaid units to availability first.
            self.corporate_service.reverse_order_prepayment(order.id, reason=reason, actor_user_id=actor_user_id)

            # 2. Flip method and reset the payment to a fresh online obligation.
            order.payment_method = PaymentMethod.CLICK
            if payment is not None:
                payment.payment_method = PaymentMethod.CLICK
                payment.status = PaymentStatus.PENDING
                payment.amount_collected = Decimal("0.00")
                payment.outstanding_amount = order.total_amount
                payment.consume_marking_codes = True
                payment.paid_at = None
            order.is_paid = False
            db.session.flush()

            # 3. Capture a fresh payment link for the customer to pay online.
            if payment is not None:
                payment_link = PaymentService().create_payment_link(payment.id)

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="order_payment_method_changed",
            severity=AuditSeverity.HIGH,
            resource_type="order",
            resource_id=str(order.id),
            additional_data={
                "actor_user_id": actor_user_id,
                "from_method": "business_account",
                "to_method": "click",
                "money_action": "online_payment_link_created",
                "reason": reason,
            },
        )

        return PaymentMethodEditResult(
            order_id=order.id,
            new_method="click",
            corporate_action="reversed_prepayment",
            money_action="online_payment_link_created",
            warnings=warnings,
            payment_link=payment_link,
        )
