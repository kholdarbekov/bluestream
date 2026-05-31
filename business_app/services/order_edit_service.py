"""Admin order-edit orchestrator.

Coordinates the cascade of side-effects when an admin retroactively edits a
placed order: line items, totals, inventory, corporate ledger, customer
bottle balance, driver bottle session (with reopen if closed), cash
prepayment refund (for cash-paid post-delivery decreases), loyalty
clawback/award, and the OrderEditHistory audit row.

Notification dispatch and the AuditEventType.ORDER_EDITED log are emitted
**post-commit** by the API edge so a rollback never fires stale events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder
from business_app.models.order import Order, OrderEditHistory, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import Product
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.services.inventory_service import InventoryService
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.transactions import atomic_transaction
from shared.enums import (
    BottleLedgerEventType,
    CashCollectionSource,
    DriverBottleSessionStatus,
    OrderStatus,
    PaymentMethod,
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Input / output dataclasses
# -------------------------------------------------------------------------

EDITABLE_STATUSES = {
    OrderStatus.PENDING,
    OrderStatus.CONFIRMED,
    OrderStatus.PREPARING,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
}

POST_DELIVERY_STATUSES = {OrderStatus.DELIVERED}

# Default 72h window for editing delivered orders, overridable via env.
DEFAULT_EDIT_WINDOW_HOURS = 72


@dataclass
class OrderEditItemSpec:
    """One desired final-state line for the edit.

    ``order_item_id is None`` ⇒ this is a new line item to insert.
    ``quantity == 0`` ⇒ remove the existing line item.
    Otherwise ⇒ update the quantity of the existing line item.
    Unit price is always snapshotted from the current Product (no manual
    overrides in v1).
    """

    product_id: int
    quantity: int
    order_item_id: Optional[int] = None


@dataclass
class _ItemChange:
    """Internal representation of a single line-item delta in the plan."""

    product_id: int
    product: Product
    old_quantity: int
    new_quantity: int
    delta: int  # new - old
    unit_price: Decimal
    existing_item: Optional[OrderItem]
    direction: str  # "add" | "remove" | "increase" | "decrease" | "unchanged"


@dataclass
class OrderEditPlan:
    """Immutable plan derived from validation + diff computation.

    The plan is used by both the preview endpoint (no DB writes) and the
    apply endpoint. ``cascade_summary`` is the same shape persisted to
    ``OrderEditHistory.diff.cascade_summary``.
    """

    order_id: int
    is_post_delivery: bool
    items_before: List[Dict[str, Any]]
    items_after: List[Dict[str, Any]]
    item_changes: List[_ItemChange]
    totals_before: Dict[str, Any]
    totals_after: Dict[str, Any]
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    cascade_summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderEditResult:
    order_id: int
    history_id: int
    cascade_summary: Dict[str, Any]
    warnings: List[str]
    # Telegram notifications are queued for dispatch by the edge. We surface
    # the queue here so the route can call .delay() *after* commit succeeds.
    post_commit_dispatch: List[Tuple[str, Tuple, Dict]] = field(default_factory=list)


# -------------------------------------------------------------------------
# Service
# -------------------------------------------------------------------------


class OrderEditService:
    """Apply an admin-driven edit to an existing order and cascade side-effects."""

    def __init__(
        self,
        *,
        bottle_service: Optional[BottleTrackingService] = None,
        cash_service: Optional[CashCollectionService] = None,
        corporate_service: Optional[CorporateContractService] = None,
        inventory_service: Optional[InventoryService] = None,
        loyalty_service: Optional[LoyaltyService] = None,
    ) -> None:
        self.bottle_service = bottle_service or BottleTrackingService()
        self.cash_service = cash_service or CashCollectionService()
        self.corporate_service = corporate_service or CorporateContractService()
        self.inventory_service = inventory_service or InventoryService()
        self.loyalty_service = loyalty_service or LoyaltyService()

    # ---------------------------------------------------------------------
    # Public entry points
    # ---------------------------------------------------------------------

    def preview(
        self,
        *,
        order_id: int,
        items: List[OrderEditItemSpec],
    ) -> OrderEditPlan:
        """Compute the cascade plan + impact summary without writing anything."""
        order = self._load_order(order_id)
        return self._build_plan(order, items)

    def get_edit_history(self, order_id: int) -> Dict[str, Any]:
        """Return the newest-first list of OrderEditHistory rows for an order.

        Raises NotFoundError if the order does not exist.
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError(f"Order {order_id} not found")
        entries = OrderEditHistory.query.filter_by(order_id=order_id).order_by(OrderEditHistory.edited_at.desc()).all()
        return {
            "order_id": order_id,
            "entries": [entry.to_dict() for entry in entries],
            "total": len(entries),
        }

    def get_edit_metadata(self, order: Order) -> Dict[str, Any]:
        """Compute edit-affordance metadata for an order detail payload.

        Returns ``{is_editable, edit_window_remaining_hours, edit_history_count}``
        so the admin UI can decide whether to render the "Edit Items" CTA.
        ``edit_window_remaining_hours`` is None for non-delivered orders
        (no window) and a float for delivered orders inside the window.
        """
        edit_window_hours = int(
            current_app.config.get("ORDER_EDIT_WINDOW_HOURS", DEFAULT_EDIT_WINDOW_HOURS) or DEFAULT_EDIT_WINDOW_HOURS
        )
        is_editable = order.status in EDITABLE_STATUSES
        remaining_hours: Optional[float] = None
        if is_editable and order.status == OrderStatus.DELIVERED:
            delivered_at = self._delivered_at(order)
            if delivered_at is not None:
                age_hours = (datetime.now(timezone.utc) - delivered_at).total_seconds() / 3600.0
                remaining_hours = max(0.0, edit_window_hours - age_hours)
                if remaining_hours <= 0:
                    is_editable = False
        return {
            "is_editable": is_editable,
            "edit_window_remaining_hours": remaining_hours,
            "edit_history_count": OrderEditHistory.query.filter_by(order_id=order.id).count(),
        }

    def apply_edit(
        self,
        *,
        order_id: int,
        items: List[OrderEditItemSpec],
        reason: str,
        actor_user_id: int,
    ) -> OrderEditResult:
        """Validate, plan, and apply the edit inside a single transaction.

        Raises:
            ValidationError: blocking reasons in plan (status, window, etc.).
        """
        if not reason or len(reason.strip()) < 3:
            raise ValidationError("Reason is required and must be at least 3 characters")

        result_holder: Dict[str, Any] = {}

        with atomic_transaction():
            order = self._load_order(order_id, for_update=True)
            plan = self._build_plan(order, items)
            if plan.blocking_reasons:
                raise ValidationError(
                    "; ".join(plan.blocking_reasons),
                    error_code="ORDER_EDIT_BLOCKED",
                )

            # Snapshot items_before BEFORE we mutate (already captured by plan).
            self._apply_items(order, plan, actor_user_id)
            self._cascade_inventory(order, plan)
            self._cascade_corporate(order, plan, actor_user_id)
            self._cascade_bottle(order, plan, actor_user_id)
            self._cascade_cash(order, plan, actor_user_id)
            self._cascade_loyalty(order, plan)
            self._recompute_totals(order, plan)

            history = OrderEditHistory(
                order_id=order.id,
                edited_by_user_id=actor_user_id,
                edited_at=datetime.now(timezone.utc),
                reason=reason.strip(),
                diff={
                    "items_before": plan.items_before,
                    "items_after": plan.items_after,
                    "totals_before": plan.totals_before,
                    "totals_after": plan.totals_after,
                    "cascade_summary": plan.cascade_summary,
                    "warnings": plan.warnings,
                },
                is_post_delivery=plan.is_post_delivery,
            )
            db.session.add(history)
            db.session.flush()

            result_holder["history_id"] = history.id
            result_holder["cascade_summary"] = plan.cascade_summary
            result_holder["warnings"] = plan.warnings
            result_holder["order_id"] = order.id
            result_holder["customer_id"] = order.user_id
            result_holder["driver_session_ids"] = plan.cascade_summary.get("session_reopened", {})

        # ---- Post-commit dispatch is the caller's responsibility ----------
        # The edge (admin endpoint) should fire telegram notification +
        # audit log AFTER the transaction commits. We return the dispatch
        # payload so the route can do that.
        post_commit_dispatch: List[Tuple[str, Tuple, Dict]] = [
            (
                "send_order_notification_task",
                (result_holder["order_id"], "order_edited"),
                {},
            ),
        ]
        # Notify the affected driver if their bottle session was reopened,
        # so they know to expect a re-close cycle.
        bottle_driver_user_id = (
            result_holder["cascade_summary"].get("session_reopened", {}).get("bottle_driver_user_id")
        )
        bottle_session_id = result_holder["cascade_summary"].get("session_reopened", {}).get("bottle_session_id")
        if bottle_driver_user_id is not None and bottle_session_id is not None:
            post_commit_dispatch.append(
                (
                    "notify_driver_session_reopened",
                    (bottle_driver_user_id, bottle_session_id, result_holder["order_id"]),
                    {},
                )
            )

        return OrderEditResult(
            order_id=result_holder["order_id"],
            history_id=result_holder["history_id"],
            cascade_summary=result_holder["cascade_summary"],
            warnings=result_holder["warnings"],
            post_commit_dispatch=post_commit_dispatch,
        )

    # ---------------------------------------------------------------------
    # Plan construction (validation + diff)
    # ---------------------------------------------------------------------

    def _load_order(self, order_id: int, *, for_update: bool = False) -> Order:
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.user),
            joinedload(Order.payment),
            joinedload(Order.delivery),
        )
        if for_update:
            query = query.with_for_update(of=Order)
        order = query.get(order_id)
        if not order:
            raise NotFoundError(f"Order {order_id} not found")
        return order

    def _build_plan(self, order: Order, items: List[OrderEditItemSpec]) -> OrderEditPlan:
        plan = OrderEditPlan(
            order_id=order.id,
            is_post_delivery=order.status == OrderStatus.DELIVERED,
            items_before=[self._snapshot_item(item) for item in order.order_items],
            items_after=[],
            item_changes=[],
            totals_before=self._snapshot_totals(order),
            totals_after={},
        )

        # ---- Status gate ----
        if order.status not in EDITABLE_STATUSES:
            plan.blocking_reasons.append(
                f"order_status_not_editable: status='{order.status.value}' is not in "
                "{pending, confirmed, preparing, out_for_delivery, delivered}"
            )

        # ---- Time-window gate (delivered only) ----
        if plan.is_post_delivery:
            window_hours = int(
                current_app.config.get("ORDER_EDIT_WINDOW_HOURS", DEFAULT_EDIT_WINDOW_HOURS)
                or DEFAULT_EDIT_WINDOW_HOURS
            )
            delivered_at = self._delivered_at(order)
            if delivered_at is None:
                plan.warnings.append("delivery_timestamp_missing — treating window as unlimited")
            else:
                age = (datetime.now(timezone.utc) - delivered_at).total_seconds() / 3600.0
                if age > window_hours:
                    plan.blocking_reasons.append(
                        f"edit_window_expired: order delivered {age:.1f}h ago, " f"window is {window_hours}h"
                    )

        # ---- Build the item-change list ----
        plan.item_changes = self._compute_item_changes(order, items, plan)
        plan.items_after = self._project_items_after(order, plan.item_changes)
        plan.totals_after = self._project_totals_after(order, plan.item_changes)

        # ---- Payment-method × direction warnings (no longer blocking) ----
        # Golden rule: once we submit utilisation to Tax Committee / fiscalize
        # via Click, we NEVER revert. Card-paid edits in either direction
        # therefore use the prepayment-trick on our side:
        #   - decrease → customer credit (cash-only-usable)
        #   - increase → admin collects the delta in cash
        # Marking codes are left intact regardless of direction.
        payment_method = order.payment_method
        is_card = payment_method in (
            PaymentMethod.CARD,
            PaymentMethod.CLICK,
            PaymentMethod.PAYME,
        )
        total_delta_preview = Decimal(str(plan.totals_after["total_amount"])) - Decimal(
            str(plan.totals_before["total_amount"])
        )
        if is_card and bool(order.is_paid):
            if total_delta_preview < 0:
                plan.warnings.append(
                    "card_paid_decrease_creates_prepayment: card payment will NOT be "
                    f"refunded; {-total_delta_preview} UZS becomes customer prepayment "
                    "credit usable on future cash orders only."
                )
            elif total_delta_preview > 0:
                plan.warnings.append(
                    "card_paid_increase_requires_cash: the additional "
                    f"{total_delta_preview} UZS must be collected in CASH via the "
                    "Record Personal Card Payment flow — card will not be re-charged."
                )

        # ---- Marking codes: golden rule notice ----
        # We never revert marking codes once submitted (USED / UTILISED) or
        # even once RESERVED on a paid card order. The plan-side "release
        # RESERVED" branch was dropped in favour of a simpler universal rule.
        if is_card and bool(order.is_paid):
            for change in plan.item_changes:
                if change.direction in {"decrease", "remove"} and change.existing_item:
                    allocs = change.existing_item.marking_code_allocations or []
                    if allocs:
                        plan.warnings.append(
                            f"marking_codes_preserved: product_id={change.product_id} "
                            f"has {len(allocs)} marking code allocation(s); none will "
                            "be reverted (Tax Committee receipt stands). Value of the "
                            "removed quantity goes to customer prepayment."
                        )
        elif plan.is_post_delivery:
            for change in plan.item_changes:
                if change.direction in {"decrease", "remove"} and change.existing_item:
                    consumed_count = sum(
                        1
                        for a in (change.existing_item.marking_code_allocations or [])
                        if str(getattr(a.action, "value", a.action)).lower() in {"used", "utilised"}
                    )
                    if consumed_count > 0:
                        plan.warnings.append(
                            f"marking_codes_consumed: product_id={change.product_id} "
                            f"has {consumed_count} marking codes already declared to "
                            "Tax Committee — they will NOT be reversed; the value of "
                            "the removed quantity becomes customer prepayment."
                        )

        # ---- Build the preview cascade_summary (without writes) ----
        plan.cascade_summary = self._compute_cascade_preview(order, plan)
        return plan

    def _compute_item_changes(
        self,
        order: Order,
        items: List[OrderEditItemSpec],
        plan: OrderEditPlan,
    ) -> List[_ItemChange]:
        if not items:
            plan.blocking_reasons.append("empty_items: at least one item required")
            return []

        existing_by_id = {item.id: item for item in order.order_items}
        existing_by_product = {item.product_id: item for item in order.order_items}
        seen_existing_ids: set = set()
        seen_new_product_ids: set = set()
        changes: List[_ItemChange] = []

        for spec in items:
            if spec.quantity < 0:
                plan.blocking_reasons.append(f"negative_quantity: product_id={spec.product_id} qty={spec.quantity}")
                continue

            existing_item: Optional[OrderItem] = None
            if spec.order_item_id is not None:
                existing_item = existing_by_id.get(spec.order_item_id)
                if existing_item is None:
                    plan.blocking_reasons.append(f"order_item_not_found: id={spec.order_item_id}")
                    continue
                if existing_item.product_id != spec.product_id:
                    plan.blocking_reasons.append(
                        f"product_id_mismatch: order_item={spec.order_item_id} expected "
                        f"product_id={existing_item.product_id}, got {spec.product_id}"
                    )
                    continue
                seen_existing_ids.add(existing_item.id)
            else:
                existing_item = existing_by_product.get(spec.product_id)
                if existing_item is not None:
                    if existing_item.id in seen_existing_ids:
                        plan.blocking_reasons.append(
                            f"duplicate_product: product_id={spec.product_id} appears "
                            "in both an existing-item spec and a new-item spec"
                        )
                        continue
                    seen_existing_ids.add(existing_item.id)
                else:
                    if spec.product_id in seen_new_product_ids:
                        plan.blocking_reasons.append(
                            f"duplicate_new_product: product_id={spec.product_id} " "appears twice in the new-item list"
                        )
                        continue
                    seen_new_product_ids.add(spec.product_id)

            product = existing_item.product if existing_item is not None else Product.query.get(spec.product_id)
            if product is None:
                plan.blocking_reasons.append(f"product_not_found: id={spec.product_id}")
                continue

            old_qty = int(existing_item.quantity) if existing_item else 0
            new_qty = int(spec.quantity)
            delta = new_qty - old_qty

            if existing_item is None:
                direction = "add" if new_qty > 0 else "noop"
                if direction == "noop":
                    # qty=0 on a non-existing line is a no-op
                    continue
            elif new_qty == 0:
                direction = "remove"
            elif delta == 0:
                direction = "unchanged"
            elif delta > 0:
                direction = "increase"
            else:
                direction = "decrease"

            # Unit price snapshot: existing items keep their snapshotted price
            # (so a price change in the catalog doesn't retroactively re-price
            # an existing line). New items use current product price.
            if existing_item is not None:
                unit_price = Decimal(str(existing_item.unit_price))
            else:
                unit_price = Decimal(str(product.calculate_price()))

            changes.append(
                _ItemChange(
                    product_id=product.id,
                    product=product,
                    old_quantity=old_qty,
                    new_quantity=new_qty,
                    delta=delta,
                    unit_price=unit_price,
                    existing_item=existing_item,
                    direction=direction,
                )
            )

        # Existing items not mentioned in the spec are implicitly unchanged.
        for item in order.order_items:
            if item.id in seen_existing_ids:
                continue
            changes.append(
                _ItemChange(
                    product_id=item.product_id,
                    product=item.product,
                    old_quantity=int(item.quantity),
                    new_quantity=int(item.quantity),
                    delta=0,
                    unit_price=Decimal(str(item.unit_price)),
                    existing_item=item,
                    direction="unchanged",
                )
            )

        # Reject no-op edits (nothing actually changed)
        if all(c.direction in {"unchanged", "noop"} for c in changes):
            plan.blocking_reasons.append("no_changes: the proposed items match the current order")

        return changes

    def _project_items_after(self, order: Order, changes: List[_ItemChange]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for change in changes:
            if change.direction == "remove":
                continue
            out.append(
                {
                    "order_item_id": change.existing_item.id if change.existing_item else None,
                    "product_id": change.product_id,
                    "product_name": change.product.name if change.product else None,
                    "quantity": change.new_quantity,
                    "unit_price": float(change.unit_price),
                    "total_price": float(change.unit_price * Decimal(change.new_quantity)),
                }
            )
        return out

    def _project_totals_after(self, order: Order, changes: List[_ItemChange]) -> Dict[str, Any]:
        subtotal = Decimal("0.00")
        for change in changes:
            if change.direction == "remove":
                continue
            subtotal += change.unit_price * Decimal(change.new_quantity)
        discount = Decimal(str(order.discount_amount or 0))
        delivery_fee = Decimal(str(order.delivery_fee or 0))
        # Loyalty redemption is clamped to (subtotal - discount) so the
        # post-edit order can never go negative. Points-per-1-UZS is fixed at
        # 100 to match Order.calculate_total(). The actual redemption refund
        # to the customer's loyalty wallet happens in _cascade_loyalty.
        old_points_used = int(order.loyalty_points_used or 0)
        max_loyalty_uzs = max(Decimal("0.00"), subtotal - discount)
        new_points_used = min(old_points_used, int(max_loyalty_uzs // Decimal("100")))
        loyalty_discount = Decimal(new_points_used) * Decimal("100")
        total = subtotal - discount - loyalty_discount + delivery_fee
        return {
            "subtotal": float(subtotal),
            "discount_amount": float(discount),
            "delivery_fee": float(delivery_fee),
            "loyalty_discount": float(loyalty_discount),
            "loyalty_points_used": new_points_used,
            "loyalty_points_refunded": old_points_used - new_points_used,
            "total_amount": float(total),
        }

    def _snapshot_totals(self, order: Order) -> Dict[str, Any]:
        return {
            "subtotal": float(order.subtotal or 0),
            "discount_amount": float(order.discount_amount or 0),
            "delivery_fee": float(order.delivery_fee or 0),
            "loyalty_discount": float(order.loyalty_discount or 0),
            "total_amount": float(order.total_amount or 0),
        }

    def _snapshot_item(self, item: OrderItem) -> Dict[str, Any]:
        """JSON-safe snapshot of an existing OrderItem.

        OrderItem.to_dict() returns Decimal values for unit_price /
        discount_amount / total_price, which PostgreSQL's JSON serializer
        cannot encode. We coerce numeric fields to float here so the dict
        round-trips through the OrderEditHistory.diff JSONB column.
        """
        return {
            "id": item.id,
            "product_id": item.product_id,
            "product_name": item.product.name if item.product else None,
            "contract_id": item.contract_id,
            "quantity": int(item.quantity),
            "unit_price": float(item.unit_price or 0),
            "discount_amount": float(item.discount_amount or 0),
            "total_price": float(item.total_price or 0),
        }

    def _delivered_at(self, order: Order) -> Optional[datetime]:
        delivery = order.delivery
        if delivery is not None:
            value = getattr(delivery, "actual_delivery", None) or getattr(delivery, "actual_delivery_time", None)
            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                return value
        # Fallback to paid_at for cash orders (DELIVERED triggers is_paid).
        if order.paid_at is not None:
            value = order.paid_at
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value
        return None

    def _compute_cascade_preview(self, order: Order, plan: OrderEditPlan) -> Dict[str, Any]:
        """Project per-cascade impacts for the preview screen — read-only."""
        total_delta = Decimal(str(plan.totals_after["total_amount"])) - Decimal(str(plan.totals_before["total_amount"]))

        # Payment side
        is_paid = bool(order.is_paid)
        payment_summary: Dict[str, Any] = {
            "total_delta": float(total_delta),
            "direction": "increase" if total_delta > 0 else "decrease" if total_delta < 0 else "neutral",
            "action": None,
            "payment_method_original": (order.payment_method.value if order.payment_method else None),
        }
        if total_delta == 0:
            payment_summary["action"] = "totals_only"
        elif not is_paid:
            # Pre-payment edits just re-price the payment row; no cash event.
            payment_summary["action"] = "totals_only"
        elif total_delta < 0:
            # Any paid order with reduction → prepayment credit (cash-only-
            # usable). Card payments are NOT refunded to the original card.
            payment_summary["action"] = "create_prepayment_credit"
            payment_summary["prepayment_amount"] = float(-total_delta)
        else:
            # Any paid order with addition → admin collects delta in cash.
            payment_summary["action"] = "manual_cash_collection_required"
            payment_summary["additional_charge"] = float(total_delta)

        # Loyalty side (preview — recompute new earned)
        old_earned = int(order.loyalty_points_earned or 0)
        new_earned_estimate = self._estimate_points_for_subtotal(order.user_id, plan.totals_after["subtotal"])
        loyalty_summary = {
            "old_points_earned": old_earned,
            "new_points_earned": new_earned_estimate,
            "diff": old_earned - new_earned_estimate,
        }

        # Bottle / session side
        bottle_changes: List[Dict[str, Any]] = []
        affected_session_id: Optional[int] = None
        for change in plan.item_changes:
            bottles_per_unit = Decimal(str(getattr(change.product, "returnable_bottles_per_unit", 0) or 0))
            if bottles_per_unit == 0 or change.delta == 0:
                continue
            bottle_changes.append(
                {
                    "product_id": change.product_id,
                    "delta_bottles": float(bottles_per_unit * Decimal(change.delta)),
                }
            )
        if bottle_changes:
            session_binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
            if session_binding:
                affected_session_id = session_binding.session_id

        # Corporate side (only meaningful for users with contracts)
        corporate_summary: Dict[str, Any] = {}
        if any(
            change.existing_item is not None and change.existing_item.contract_id is not None
            for change in plan.item_changes
        ):
            corporate_summary["adjustment_required"] = True

        return {
            "payment": payment_summary,
            "loyalty": loyalty_summary,
            "bottle_balance": {
                "changes": bottle_changes,
                "affected_session_id": affected_session_id,
            },
            "corporate": corporate_summary,
            "session_reopened": {},  # populated during apply
        }

    def _estimate_points_for_subtotal(self, user_id: int, subtotal: float) -> int:
        try:
            return int(self.loyalty_service.calculate_points_for_purchase(user_id, int(subtotal)))
        except Exception:
            logger.exception("Failed to estimate loyalty points for preview")
            return 0

    # ---------------------------------------------------------------------
    # Cascade — apply
    # ---------------------------------------------------------------------

    def _apply_items(self, order: Order, plan: OrderEditPlan, actor_user_id: int) -> None:
        """Insert/update/delete OrderItem rows according to the plan.

        Deletion of OrderItems with active corporate-prepayment ledger rows
        would violate the non-cascading FK on ``corporate_prepayment_ledger
        .order_item_id``. We pre-flight check and null out those FKs (the
        audit trail is preserved via ``order_id``).
        """
        from business_app.models.corporate import CorporatePrepaymentLedger

        for change in plan.item_changes:
            if change.direction == "unchanged":
                continue
            if change.direction == "remove":
                # Null out FKs from CorporatePrepaymentLedger to allow the
                # delete; the ledger rows retain order_id for the audit trail.
                CorporatePrepaymentLedger.query.filter_by(order_item_id=change.existing_item.id).update(
                    {"order_item_id": None}, synchronize_session=False
                )
                db.session.delete(change.existing_item)
                continue
            if change.direction == "add":
                new_item = OrderItem(
                    order_id=order.id,
                    product_id=change.product_id,
                    quantity=change.new_quantity,
                    unit_price=change.unit_price,
                    discount_amount=Decimal("0.00"),
                    total_price=change.unit_price * Decimal(change.new_quantity),
                )
                db.session.add(new_item)
                db.session.flush()
                change.existing_item = new_item
                continue
            # increase / decrease
            change.existing_item.quantity = change.new_quantity
            change.existing_item.total_price = change.unit_price * Decimal(change.new_quantity)
        db.session.flush()

    def _cascade_inventory(self, order: Order, plan: OrderEditPlan) -> None:
        """Adjust Product.stock_quantity for confirmed/delivered orders.

        Pre-confirmed orders keep their Redis reservations — we leave those
        alone since they expire on TTL and won't affect DB stock. For orders
        already past CONFIRMED (non-cash) or DELIVERED (cash), DB stock was
        deducted; we apply the delta now.
        """
        is_cash = order.payment_method == PaymentMethod.CASH
        # DB-stock has been deducted if:
        #   - non-cash AND order >= CONFIRMED, OR
        #   - cash AND order DELIVERED.
        past_deduction = (not is_cash and order.status != OrderStatus.PENDING) or (
            is_cash and order.status == OrderStatus.DELIVERED
        )
        adjustments: List[Dict[str, Any]] = []
        if not past_deduction:
            plan.cascade_summary["inventory"] = {"adjustments": [], "deferred": True}
            return

        # We bypass InventoryService.adjust_inventory (decorated with its own
        # commit) to stay inside the orchestrator's atomic boundary. Stock
        # delta is applied directly with row-level lock; the audit row is the
        # OrderEditHistory we write at the end of the transaction.
        for change in plan.item_changes:
            if change.delta == 0:
                continue
            product = (
                Product.query.with_for_update().get(change.product_id)
                if change.product is None
                else Product.query.with_for_update().get(change.product.id)
            )
            if product is None:
                raise ValidationError(f"product_not_found during inventory adjust: id={change.product_id}")
            stock_delta = -change.delta  # qty up → stock down, and vice versa
            new_stock = (product.stock_quantity or 0) + stock_delta
            if new_stock < 0:
                raise ValidationError(f"insufficient_stock: product {change.product_id} would go to {new_stock}")
            product.stock_quantity = new_stock
            if hasattr(product, "is_in_stock"):
                product.is_in_stock = new_stock > 0
            adjustments.append(
                {
                    "product_id": change.product_id,
                    "qty_delta": change.delta,
                    "stock_delta": stock_delta,
                    "new_stock": new_stock,
                }
            )
        db.session.flush()
        plan.cascade_summary["inventory"] = {"adjustments": adjustments}

    def _cascade_corporate(self, order: Order, plan: OrderEditPlan, actor_user_id: int) -> None:
        """Release old corporate-prepayment reserves and re-reserve / consume.

        Strategy: release_for_order() clears reserves still in RESERVE state
        (idempotent on already-released or already-consumed entries — they
        are skipped). After items are applied we call reserve_for_order()
        again, which keys on the NEW order_item.id and so will not double-up
        on still-existing reserves (skipped via existing-idempotency-key).
        For DELIVERED orders the corporate consume already happened; v1
        does NOT attempt to reverse consume entries — instead it records a
        warning that the consume snapshot is now stale.
        """
        has_corporate_lines = any(
            (change.existing_item is not None and change.existing_item.contract_id is not None)
            or (change.direction == "add" and getattr(change.product, "contract_id", None))
            for change in plan.item_changes
        )
        summary: Dict[str, Any] = {"adjusted": False}
        if not has_corporate_lines:
            plan.cascade_summary["corporate"] = summary
            return

        if plan.is_post_delivery:
            plan.warnings.append(
                "corporate_consume_stale: delivered order had corporate CONSUME entries "
                "that will NOT be reversed automatically in v1 — finance must reconcile manually."
            )
            summary["adjusted"] = False
            summary["manual_reconciliation_required"] = True
        else:
            released = self.corporate_service.release_for_order(
                order_id=order.id,
                reason=f"Order edit by user {actor_user_id}",
                actor_user_id=actor_user_id,
            )
            reserved = self.corporate_service.reserve_for_order(
                order_id=order.id,
                actor_user_id=actor_user_id,
            )
            summary["adjusted"] = True
            summary["released_count"] = len(released or [])
            summary["reserved_count"] = len(reserved or [])
        plan.cascade_summary["corporate"] = summary

    def _cascade_bottle(self, order: Order, plan: OrderEditPlan, actor_user_id: int) -> None:
        """Apply forward-dated customer-balance adjustments + session re-tally.

        For each product that affects bottle count and has a non-zero delta:
          1. Compute bottle delta = returnable_bottles_per_unit × qty_delta.
          2. Write a BottleLedger ADMIN_ADJUSTMENT entry for the customer.
          3. If a DriverBottleSession is bound to this order:
               - If CLOSED/FORCE_CLOSED, reopen via BottleTrackingService.reopen_session.
               - Bump session.bottles_delivered by the bottle delta.
        The session is then ready for the driver to re-close and admin to verify.
        """
        if not order.delivery_address_id:
            plan.cascade_summary["bottle"] = {"adjustments": [], "skipped": "no_address"}
            return

        adjustments: List[Dict[str, Any]] = []
        affected_session_id: Optional[int] = None
        session_reopened = False

        reopened_driver_user_id: Optional[int] = None
        binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
        if binding is not None:
            affected_session_id = binding.session_id
            session = DriverBottleSession.query.get(binding.session_id)
            if session is not None and session.status in {
                DriverBottleSessionStatus.CLOSED,
                DriverBottleSessionStatus.FORCE_CLOSED,
            }:
                self.bottle_service.reopen_session(
                    session_id=session.id,
                    actor_user_id=actor_user_id,
                    reason=f"Order #{order.id} edit cascade",
                    commit=False,
                )
                session_reopened = True
                reopened_driver_user_id = session.driver_user_id

        for change in plan.item_changes:
            bottles_per_unit = Decimal(str(getattr(change.product, "returnable_bottles_per_unit", 0) or 0))
            if bottles_per_unit == 0 or change.delta == 0:
                continue
            bottle_delta = bottles_per_unit * Decimal(change.delta)
            # The customer's balance moves by +bottle_delta when we deliver
            # more bottles (they now hold more bottles), and -bottle_delta
            # when we deliver fewer. We write the ADMIN_ADJUSTMENT entry via
            # the internal _create_ledger_entry helper — the public
            # admin_adjust_balance is decorated with @transactional and would
            # commit prematurely inside our orchestrator's atomic boundary.
            try:
                ledger_entry = self.bottle_service._create_ledger_entry(
                    user_id=order.user_id,
                    address_id=order.delivery_address_id,
                    event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                    quantity=bottle_delta,
                    actor_user_id=actor_user_id,
                    order_id=order.id,
                    notes=(
                        f"Order #{order.id} edit: product {change.product_id} qty "
                        f"{change.old_quantity} → {change.new_quantity}"
                    ),
                    metadata={"source": "order_edit", "product_id": change.product_id},
                )
            except ValidationError:
                raise
            except Exception as exc:
                logger.exception("Bottle adjust failed for order edit")
                raise ValidationError(f"Bottle adjust failed for product {change.product_id}: {exc}") from exc

            # Re-tally session only when the order is already delivered.
            # For undelivered orders the delivery flow will credit the full
            # quantity when it actually runs; bumping the tally here would
            # cause the delta to be counted twice (once now, once at delivery).
            if affected_session_id is not None and order.status == OrderStatus.DELIVERED:
                session = DriverBottleSession.query.get(affected_session_id)
                if session is not None:
                    # bottle_delta > 0  → more bottles delivered to customer
                    # bottle_delta < 0  → fewer bottles delivered (some come back)
                    delivered_delta = int(bottle_delta)
                    if delivered_delta > 0:
                        session.bottles_delivered = (session.bottles_delivered or 0) + delivered_delta
                    elif delivered_delta < 0:
                        # Reduce previously-counted deliveries.
                        session.bottles_delivered = max(0, (session.bottles_delivered or 0) + delivered_delta)

            adjustments.append(
                {
                    "product_id": change.product_id,
                    "bottle_delta": float(bottle_delta),
                    "ledger_id": getattr(ledger_entry, "id", None),
                }
            )

        plan.cascade_summary["bottle"] = {
            "adjustments": adjustments,
            "affected_session_id": affected_session_id,
        }
        if session_reopened and affected_session_id is not None:
            plan.cascade_summary["session_reopened"]["bottle_session_id"] = affected_session_id
            if reopened_driver_user_id is not None:
                plan.cascade_summary["session_reopened"]["bottle_driver_user_id"] = reopened_driver_user_id

        # Also reopen the driver's CASH session if closed AND this is a
        # delivered cash order whose total changed — the cash collection
        # event for the prepayment refund (cascade_cash) writes into the
        # session. We handle the actual reopen + audit there to keep the
        # responsibility per-cascade.

    def _cascade_cash(self, order: Order, plan: OrderEditPlan, actor_user_id: int) -> None:
        """Apply cash-side changes for a paid order whose total changed.

        Policy (paid orders, any payment method):
          - decrease → post a customer prepayment credit
            ``CashCollectionEvent(amount=Δ, source=ADMIN_ADJUSTMENT,
            driver_cash_session_id=None)``. Future cash orders auto-apply.
            Card payments are NOT refunded to the original card (per the
            Tax Committee / marking-code golden rule: we do the trick on
            our side, not at the gateway).
          - increase → admin must collect the delta in cash via the
            existing Personal Card Payment flow. Card is not re-charged.

        For unpaid orders (PENDING / CONFIRMED pre-payment), there's no
        collected cash to reconcile against — the Payment.amount is
        re-priced in ``_recompute_totals`` and we exit here as a no-op.
        """
        cash_summary: Dict[str, Any] = {"action": None}
        total_delta = Decimal(str(plan.totals_after["total_amount"])) - Decimal(str(plan.totals_before["total_amount"]))
        if total_delta == 0:
            plan.cascade_summary["cash"] = cash_summary
            return

        is_paid = bool(order.is_paid)
        # We trigger the prepayment cascade for any paid order (cash post-
        # delivery, or any paid card order). Unpaid pre-delivery edits are
        # handled by simply re-pricing Payment.amount in _recompute_totals.
        if not is_paid:
            cash_summary["action"] = "no_payment_adjustment_needed"
            cash_summary["amount"] = float(total_delta)
            plan.cascade_summary["cash"] = cash_summary
            return

        if total_delta < 0:
            # Refund Δ to customer as prepayment credit. The driver's cash
            # session is NOT touched: the original collection is settled
            # history (reflected in existing CashCollectionAllocation rows
            # that we leave intact). For card-paid orders we also leave the
            # card gateway transaction alone — the customer credit lives
            # purely on our side, usable on future CASH orders.
            refund_amount = -total_delta
            try:
                event = self.cash_service.post_collection(
                    customer_id=order.user_id,
                    amount=refund_amount,
                    source=CashCollectionSource.ADMIN_ADJUSTMENT,
                    recorded_by_user_id=actor_user_id,
                    order_id=order.id,
                    driver_cash_session_id=None,
                    notes=(
                        f"Order #{order.id} edit refund: total dropped by "
                        f"{refund_amount} (payment_method={order.payment_method.value
                                                           if order.payment_method
                                                           else 'unknown'
                                                           })"
                    ),
                    idempotency_key=f"order_edit_refund:{order.id}:{refund_amount}",
                    commit=False,
                )
            except Exception as exc:
                logger.exception("post_collection failed during order edit cash cascade")
                raise ValidationError(f"Cash prepayment write failed: {exc}") from exc

            cash_summary["action"] = "prepayment_created"
            cash_summary["amount"] = float(refund_amount)
            cash_summary["event_id"] = getattr(event, "id", None)
            cash_summary["payment_method_original"] = order.payment_method.value if order.payment_method else None
        else:
            cash_summary["action"] = "additional_cash_collection_required"
            cash_summary["amount"] = float(total_delta)
            plan.warnings.append(
                f"additional_cash_collection_required: customer owes {total_delta} extra — "
                "use the Record Personal Card Payment / cash-collection flow to settle."
            )
        plan.cascade_summary["cash"] = cash_summary

    def _cascade_loyalty(self, order: Order, plan: OrderEditPlan) -> None:
        """Adjust both sides of the loyalty cascade:

        1. Redeemed points (``loyalty_points_used``): if the new subtotal can
           no longer absorb the original redemption, return the unused
           portion to the customer's wallet as an ADJUSTMENT credit.
        2. Earned points (``loyalty_points_earned``): if loyalty was already
           awarded, recompute on the new subtotal and clamp-clawback the
           difference. Skipped when no points have been awarded yet.
        """
        loyalty_summary: Dict[str, Any] = {"applied": False}

        # --- Redemption side (always — independent of award status) ---
        old_points_used = int(order.loyalty_points_used or 0)
        new_points_used = int(plan.totals_after.get("loyalty_points_used", old_points_used))
        points_to_refund = old_points_used - new_points_used
        if points_to_refund > 0:
            # Credit the user's wallet with the unused redemption. We use
            # reverse_earnings with new=0 in award direction by passing
            # old/new flipped — but simpler: create an ADJUSTMENT credit via
            # the same primitive, treating it as "old=0 → new=N" award.
            refund_result = self.loyalty_service.reverse_earnings(
                user_id=order.user_id,
                order_id=order.id,
                old_points_earned=0,
                new_points_earned=points_to_refund,
                clamp=False,
                description=f"Order #{order.id} edit: refund of unused redeemed points",
                commit=False,
            )
            loyalty_summary["redemption_refund"] = {
                "points_refunded": points_to_refund,
                "old_points_used": old_points_used,
                "new_points_used": new_points_used,
                "transaction_id": refund_result.get("transaction_id"),
            }

        # --- Earned side ---
        is_cash = order.payment_method == PaymentMethod.CASH
        awarded = (not is_cash and order.status not in {OrderStatus.PENDING}) or (
            is_cash and order.status == OrderStatus.DELIVERED
        )
        if not awarded:
            loyalty_summary["earnings"] = {"applied": False, "deferred": True}
            plan.cascade_summary["loyalty"] = loyalty_summary
            return

        old_points = int(order.loyalty_points_earned or 0)
        new_subtotal = Decimal(str(plan.totals_after["subtotal"]))
        new_points = self.loyalty_service.calculate_points_for_purchase(order.user_id, int(new_subtotal))

        result = self.loyalty_service.reverse_earnings(
            user_id=order.user_id,
            order_id=order.id,
            old_points_earned=old_points,
            new_points_earned=int(new_points),
            clamp=True,
            commit=False,
        )
        order.loyalty_points_earned = int(new_points)
        loyalty_summary["applied"] = True
        loyalty_summary["earnings"] = {
            "applied": True,
            "old_points_earned": old_points,
            "new_points_earned": int(new_points),
            "clawback": result.get("clawback", 0),
            "uncollectible": result.get("uncollectible", 0),
            "award": result.get("award", 0),
            "transaction_id": result.get("transaction_id"),
        }
        plan.cascade_summary["loyalty"] = loyalty_summary

    def _recompute_totals(self, order: Order, plan: OrderEditPlan) -> None:
        """Recompute order.subtotal and total_amount from current items.

        Also applies the clamped ``loyalty_points_used`` from the plan
        (so the projected total matches what gets persisted) and updates
        the Payment row's amount / outstanding_amount where appropriate.
        """
        # Apply the (possibly clamped-down) redemption count before calling
        # calculate_total — otherwise calculate_total uses the stale old
        # value and the total can go negative.
        if "loyalty_points_used" in plan.totals_after:
            order.loyalty_points_used = int(plan.totals_after["loyalty_points_used"])

        db.session.flush()
        order.calculate_total()

        payment: Optional[Payment] = order.payment
        if payment is None:
            db.session.flush()
            return

        new_total = Decimal(str(order.total_amount or 0))
        collected = Decimal(str(payment.amount_collected or 0))
        is_paid = bool(order.is_paid)

        # Unpaid (PENDING / CONFIRMED before payment): payment hasn't been
        # collected yet, so we can freely re-price the row to the new total.
        if not is_paid:
            payment.amount = new_total
            payment.outstanding_amount = max(Decimal("0.00"), new_total - collected)
            db.session.flush()
            return

        # Paid orders (cash post-delivery OR card-paid in any status):
        #   - new_total > collected → customer owes the delta. Surface it
        #     as outstanding so the Personal Card Payment / cash flow can
        #     settle it. We do NOT re-charge the card gateway.
        #   - new_total <= collected → leave payment row at the original
        #     collected figure (audit trail). The prepayment cushion
        #     created in _cascade_cash carries the difference.
        if new_total > collected:
            payment.amount = new_total
            payment.outstanding_amount = new_total - collected
        # else: leave payment as-is.

        db.session.flush()
