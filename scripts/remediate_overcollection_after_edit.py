"""Remediate paid orders left with a phantom outstanding after an upward edit.

Background
----------
See ``scripts/audit_paid_orders_with_phantom_outstanding.py`` for the full
incident write-up (prod order TG_000190_26). In short: an admin edited a paid
order's total upward, and the old cascade surfaced the increase as outstanding
without netting it against cash the customer had already over-paid at delivery,
leaving the order both "paid" and "owing".

What this does
--------------
For each targeted order it runs the SAME settlement the fixed order-edit cascade
now performs, inside one transaction:

  1. Re-align ``Payment.amount`` to the order's current ``total_amount``.
  2. ``sync_payment_projection`` — re-derive outstanding/status/is_paid from the
     true ``amount_collected`` (this alone corrects the impossible
     paid-with-outstanding state).
  3. ``settle_payment_from_customer_credit`` — cover the remaining outstanding
     from the customer's own cash credit: prepayment reserved against this
     order, available unapplied prepayment, then this order's own over-
     collection reclaimed from the customer's other pending orders.

Idempotent: an order already fully settled (no outstanding) is skipped. It never
collects new cash and never touches a card gateway — it only re-attributes money
the customer has already paid.

Run (``scripts/`` is not mounted into the container, so pipe via stdin; the
args after ``-`` still reach argparse). Dry-run prints before/after and rolls
back:

    docker compose exec -T business_app python - --order-number TG_000190_26 --dry-run \
        < scripts/remediate_overcollection_after_edit.py

Apply (single order):

    docker compose exec -T business_app python - --order-number TG_000190_26 --commit \
        < scripts/remediate_overcollection_after_edit.py

Apply to every order the audit flags as fully auto-fixable:

    docker compose exec -T business_app python - --all-auto-fixable --commit \
        < scripts/remediate_overcollection_after_edit.py

``--actor-user-id`` is an optional fallback collector id stamped on the
settlement; the authoritative collector is still derived from the original
cash-collection event (whoever physically collected the cash).
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import List, Optional

from business_app import create_app, db
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import PaymentMethod, PaymentStatus


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _snapshot(payment: Payment) -> str:
    order = payment.order
    return (
        f"order={order.order_number if order else payment.order_id} "
        f"is_paid={bool(order.is_paid) if order else '?'} "
        f"status={_status_value(payment.status)} "
        f"amount={Decimal(str(payment.amount or 0))} "
        f"collected={Decimal(str(payment.amount_collected or 0))} "
        f"outstanding={Decimal(str(payment.outstanding_amount or 0))}"
    )


def _select_payments(order_number: Optional[str], order_id: Optional[int], all_auto: bool) -> List[Payment]:
    if order_number or order_id:
        query = Payment.query.join(Order, Order.id == Payment.order_id)
        if order_number:
            query = query.filter(Order.order_number == order_number)
        else:
            query = query.filter(Payment.order_id == order_id)
        return query.all()

    # --all-auto-fixable: the bug fingerprint, restricted to CASH orders whose
    # outstanding can be fully covered from the customer's own credit.
    candidates = (
        Payment.query.join(Order, Order.id == Payment.order_id)
        .filter(
            Payment.outstanding_amount > Decimal("0.00"),
            Payment.payment_method == PaymentMethod.CASH,
            db.or_(Order.is_paid.is_(True), Payment.status == PaymentStatus.COMPLETED),
        )
        .all()
    )
    cash_service = CashCollectionService()
    fixable: List[Payment] = []
    for payment in candidates:
        creditable = cash_service.estimate_settleable_credit_for_order(payment.order)
        if creditable >= Decimal(str(payment.outstanding_amount or 0)):
            fixable.append(payment)
    return fixable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--order-number", help="Order number to remediate, e.g. TG_000190_26")
    target.add_argument("--order-id", type=int, help="Order id to remediate")
    target.add_argument(
        "--all-auto-fixable",
        action="store_true",
        help="Remediate every order the audit flags as fully auto-fixable from credit.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Print before/after and roll back.")
    mode.add_argument("--commit", action="store_true", help="Apply inside one transaction and commit.")
    parser.add_argument("--actor-user-id", type=int, default=None, help="Fallback collector id.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        cash_service = CashCollectionService()
        payments = _select_payments(args.order_number, args.order_id, args.all_auto_fixable)

        if not payments:
            print("No matching orders found. Nothing to do.")
            return

        changed = skipped = 0
        for payment in payments:
            order = payment.order
            if order is None:
                print(f"payment_id={payment.id}: no order attached — skipped")
                skipped += 1
                continue

            outstanding = Decimal(str(payment.outstanding_amount or 0))
            if outstanding <= Decimal("0.00"):
                print(f"{order.order_number}: outstanding=0 — already settled, skipped")
                skipped += 1
                continue

            print("-" * 100)
            print(f"BEFORE  {_snapshot(payment)}")

            # Mirror the fixed order-edit cascade exactly.
            payment.amount = Decimal(str(order.total_amount or 0))
            db.session.flush()
            cash_service.sync_payment_projection(payment)
            cash_service.settle_payment_from_customer_credit(
                payment, actor_user_id=args.actor_user_id
            )
            db.session.flush()

            print(f"AFTER   {_snapshot(payment)}")
            changed += 1

        print("=" * 100)
        if args.commit:
            db.session.commit()
            print(f"COMMITTED. Remediated {changed} order(s), skipped {skipped}.")
        else:
            db.session.rollback()
            print(f"DRY-RUN — rolled back. Would remediate {changed} order(s), skip {skipped}.")


if __name__ == "__main__":
    main()
