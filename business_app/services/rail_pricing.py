"""THE one expression of "the price follows the payment method" for an EXISTING order.

Spec: docs/superpowers/specs/2026-10-09-corporate-contract-dual-pricing-design.md §5.

`quote_order_for_rail` is read-only: what this order would cost on `payment_method`.
`reprice_order_for_rail` applies that quote (line prices, subtotal, tier discount, total), keeps a
delivered grocery order's AMOUNT-mode charge equal to the new total, and writes one audit event.
Every place an order's payment method changes calls `reprice_order_for_rail`. Every screen that
previews such a change, or must recognise money an earlier method asked for, calls
`quote_order_for_rail`. Import them; never re-derive a line price.

Which lines move:
  * covered now                -> THE contract rule for the method (`contract_price_for_method`);
  * covered when placed, not now -> the live catalogue price (the contract no longer prices it);
  * never covered              -> unchanged (a regular customer's price never depended on the method);
  * loyalty reward line        -> unchanged (a gift at 0 must never be billed).
The subtotal moves by the changed lines only; `delivery_fee` and `discount_amount` stay as stored.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Tuple, Union

from business_app import db
from business_app.models.order import Order
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.order_totals import compute_order_total
from shared.enums import PaymentMethod
from shared.payment_methods import canonical_rail

PaymentMethodLike = Union[str, PaymentMethod, None]


@dataclass(frozen=True)
class RailLine:
    order_item_id: int
    product_id: int
    quantity: int
    old_unit_price: Decimal
    new_unit_price: Decimal
    old_total_price: Decimal
    new_total_price: Decimal
    contract_id: Optional[int]
    contract_product_price_id: Optional[int]
    pricing_source: str

    @property
    def changed(self) -> bool:
        return self.new_unit_price != self.old_unit_price or self.new_total_price != self.old_total_price


@dataclass(frozen=True)
class RailQuote:
    payment_method: Any
    lines: Tuple[RailLine, ...]
    previous_subtotal: Decimal
    previous_tier_discount: Decimal
    previous_total: Decimal
    subtotal: Decimal
    tier_discount: Decimal
    total: Decimal

    @property
    def changed(self) -> bool:
        return (
            self.total != self.previous_total
            or self.tier_discount != self.previous_tier_discount
            or any(line.changed for line in self.lines)
        )


def _method_value(method: Any) -> Optional[str]:
    if method is None:
        return None
    return method.value if hasattr(method, "value") else str(method)


def quote_order_for_rail(order: Order, payment_method: PaymentMethodLike) -> RailQuote:
    """What `order` would cost on `payment_method`. Writes nothing."""
    from business_app.services.corporate_contract_service import (
        PRICING_SOURCE_FALLBACK,
        CorporateContractService,
    )
    from business_app.services.loyalty_service import tier_discount_for_rail

    service = CorporateContractService()
    lines = []
    for item in sorted(order.order_items, key=lambda line: line.id or 0):
        quantity = int(item.quantity)
        old_unit = Decimal(str(item.unit_price))
        old_total = Decimal(str(item.total_price))
        unchanged = RailLine(
            order_item_id=item.id,
            product_id=item.product_id,
            quantity=quantity,
            old_unit_price=old_unit,
            new_unit_price=old_unit,
            old_total_price=old_total,
            new_total_price=old_total,
            contract_id=item.contract_id,
            contract_product_price_id=item.contract_product_price_id,
            pricing_source="reward" if item.is_reward_item else PRICING_SOURCE_FALLBACK,
        )
        if item.is_reward_item:
            lines.append(unchanged)
            continue

        resolution = service.resolve_contract_pricing_for_user_product(
            user_id=order.user_id,
            product_id=item.product_id,
            fallback_price=Decimal(str(item.product.calculate_price(quantity=quantity))),
            payment_method=payment_method,
            effective_at=order.created_at,
        )
        row = resolution["contract_price_row"]
        if row is None and item.contract_product_price_id is None:
            lines.append(unchanged)
            continue

        new_unit = resolution["unit_price"]
        lines.append(
            RailLine(
                order_item_id=item.id,
                product_id=item.product_id,
                quantity=quantity,
                old_unit_price=old_unit,
                new_unit_price=new_unit,
                old_total_price=old_total,
                new_total_price=new_unit * Decimal(quantity),
                contract_id=row.contract_id if row is not None else None,
                contract_product_price_id=row.id if row is not None else None,
                pricing_source=resolution["pricing_source"],
            )
        )

    previous_subtotal = Decimal(str(order.subtotal or 0))
    previous_tier = Decimal(str(order.tier_discount or 0))
    previous_total = Decimal(str(order.total_amount or 0))
    subtotal = previous_subtotal + sum((line.new_total_price - line.old_total_price for line in lines), Decimal("0.00"))
    discount_amount = Decimal(str(order.discount_amount or 0))
    loyalty_discount = Decimal(str(order.loyalty_discount or 0))
    tier_discount = tier_discount_for_rail(
        order.user,
        subtotal=subtotal,
        discount_amount=discount_amount,
        loyalty_discount=loyalty_discount,
        payment_method=payment_method,
    )
    if subtotal == previous_subtotal and tier_discount == previous_tier:
        total = previous_total
    else:
        total = compute_order_total(
            subtotal=subtotal,
            discount_amount=discount_amount,
            delivery_fee=Decimal(str(order.delivery_fee or 0)),
            loyalty_discount=loyalty_discount,
            tier_discount=tier_discount,
        )

    return RailQuote(
        payment_method=canonical_rail(payment_method),
        lines=tuple(lines),
        previous_subtotal=previous_subtotal,
        previous_tier_discount=previous_tier,
        previous_total=previous_total,
        subtotal=subtotal,
        tier_discount=tier_discount,
        total=total,
    )


def reprice_order_for_rail(
    order: Order,
    payment_method: PaymentMethodLike,
    *,
    actor_user_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> RailQuote:
    """Apply `quote_order_for_rail` to `order`. Flushes; the caller owns the transaction.

    Writes nothing, and logs nothing, when the quote moves nothing. Callers re-price
    `payment.amount` / `outstanding_amount` themselves, off `order.total_amount`, because each
    method change rebuilds the payment differently.
    """
    quote = quote_order_for_rail(order, payment_method)
    if not quote.changed:
        return quote

    items_by_id = {item.id: item for item in order.order_items}
    for line in quote.lines:
        if not line.changed:
            continue
        item = items_by_id[line.order_item_id]
        item.unit_price = line.new_unit_price
        item.total_price = line.new_total_price
        item.contract_id = line.contract_id
        item.contract_product_price_id = line.contract_product_price_id
    order.subtotal = quote.subtotal
    order.tier_discount = quote.tier_discount
    order.total_amount = quote.total
    db.session.flush()

    from business_app.services.corporate_contract_service import CorporateContractService

    CorporateContractService().true_up_order_charge(order, actor_user_id=actor_user_id, reason=reason)

    audit_logger.log_event(
        event_type=AuditEventType.ORDER_UPDATED,
        action="order_repriced_for_payment_method",
        severity=AuditSeverity.MEDIUM,
        resource_type="order",
        resource_id=str(order.id),
        additional_data={
            "payment_method": _method_value(quote.payment_method),
            "actor_user_id": actor_user_id,
            "reason": reason,
            "previous_subtotal": str(quote.previous_subtotal),
            "subtotal": str(quote.subtotal),
            "previous_total": str(quote.previous_total),
            "total": str(quote.total),
            "lines": [
                {
                    "order_item_id": line.order_item_id,
                    "product_id": line.product_id,
                    "old_unit_price": str(line.old_unit_price),
                    "new_unit_price": str(line.new_unit_price),
                    "pricing_source": line.pricing_source,
                }
                for line in quote.lines
                if line.changed
            ],
        },
    )
    return quote
