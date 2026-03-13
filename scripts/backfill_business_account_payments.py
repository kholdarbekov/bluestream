"""Backfill missing payment records for historical business-account orders."""

from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.corporate import CorporateContractProductPrice
from business_app.models.order import Order
from business_app.services.payment_service import PaymentService
from business_app.utils.constants import OrderStatus, PaymentMethod


def _is_fully_contract_backed(order: Order) -> bool:
    if not order.order_items:
        return False

    for item in order.order_items:
        if not item.contract_id or not item.contract_product_price_id:
            return False

        price_row = CorporateContractProductPrice.query.filter_by(
            id=item.contract_product_price_id,
            contract_id=item.contract_id,
            product_id=item.product_id,
            is_active=True,
            is_prepayment_eligible=True,
        ).first()
        if not price_row:
            return False

    return True


def backfill_business_account_payments(*, apply_changes: bool = False) -> Dict[str, Any]:
    """Backfill completed payments for business-account orders missing a payment record."""
    candidate_orders = (
        Order.query.options(
            joinedload(Order.order_items),
            joinedload(Order.payment),
        )
        .filter(
            Order.payment_method == PaymentMethod.BUSINESS_ACCOUNT,
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.RETURNED]),
            ~Order.payment.has(),
        )
        .order_by(Order.id.asc())
        .all()
    )

    summary: Dict[str, Any] = {
        'candidate_order_ids': [order.id for order in candidate_orders],
        'applied_count': 0,
        'applied_order_ids': [],
        'skipped': [],
    }

    if not apply_changes:
        for order in candidate_orders:
            if not _is_fully_contract_backed(order):
                summary['skipped'].append({
                    'order_id': order.id,
                    'reason': 'Order is not fully contract-backed',
                })
        return summary

    payment_service = PaymentService()
    for order in candidate_orders:
        if not _is_fully_contract_backed(order):
            summary['skipped'].append({
                'order_id': order.id,
                'reason': 'Order is not fully contract-backed',
            })
            continue

        payment_service.initialize_order_payment(
            order.id,
            metadata={
                'backfill_applied': True,
                'backfill_source': 'business_account_payment_backfill',
            },
            trigger_notifications=False,
            allow_order_confirmation=False,
        )
        summary['applied_count'] += 1
        summary['applied_order_ids'].append(order.id)

    db.session.commit()
    return summary


if __name__ == '__main__':
    from business_app import create_app

    app = create_app()
    with app.app_context():
        result = backfill_business_account_payments(apply_changes=False)
        print(result)
