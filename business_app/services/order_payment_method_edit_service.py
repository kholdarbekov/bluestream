"""Admin order-level payment-method change with full reconciliation.

Moves an order between business_account and cash/click, reconciling the
corporate prepayment ledger and the money side. Only four transitions are
allowed; a completed online PSP is terminal. Mirrors OrderCashEditService.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from business_app.models.corporate import CorporatePrepaymentLedger
from business_app.models.order import Order
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.utils.exceptions import NotFoundError
from shared.enums import OrderStatus, PaymentStatus


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

    def preview(self, *, order_id: int, new_method: str) -> PaymentMethodEditPlan:
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
            reversed_exists = CorporatePrepaymentLedger.query.filter(
                CorporatePrepaymentLedger.order_id == order.id,
                CorporatePrepaymentLedger.idempotency_key.like("reverse:%"),
            ).first()
            if reversed_exists:
                blocking.append("corporate_settlement_previously_reversed")

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
