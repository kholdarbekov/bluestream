"""READ-ONLY audit: orders that are DELIVERED but whose online payment never completed.

These are orders where:
  - order.status == DELIVERED
  - order.payment_method in {CLICK, PAYME, CARD}  (electronic method)
  - payment.status in {PENDING, CANCELLED, FAILED}  (payment never settled)

Such orders were delivered to the customer but were never marked as paid. They
are candidates for offline remediation — either via the admin "Record Personal
Card Payment" flow (Task 2/3 of the offline-payment plan) or via a cash
collection recorded at delivery (Task 4).

For each order the script also reports whether an EARNED loyalty transaction
already exists so the operator knows the loyalty balance was (or was not)
credited and does NOT need to be re-awarded.

It does NOT modify any data. Read-only, exits 0 on success.

Run:

    docker compose exec -T business_app python - < scripts/audit_offline_unpaid_delivered_orders.py

Reference: docs/superpowers/plans/2026-06-22-offline-payment-on-failed-electronic-orders.md
"""

from __future__ import annotations

from business_app import create_app, db
from business_app.models.loyalty import LoyaltyTransaction
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User
from business_app.utils.constants import LoyaltyTransactionType
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


_ELECTRONIC_METHODS = {PaymentMethod.CLICK, PaymentMethod.PAYME, PaymentMethod.CARD}
_UNSETTLED_STATUSES = {PaymentStatus.PENDING, PaymentStatus.CANCELLED, PaymentStatus.FAILED}


def _val(enum_or_str) -> str:
    return enum_or_str.value if hasattr(enum_or_str, "value") else str(enum_or_str)


def main() -> None:
    app = create_app()
    with app.app_context():
        # Load all DELIVERED orders with an electronic payment method that has
        # not completed (PENDING / CANCELLED / FAILED).
        rows = (
            db.session.query(Order, Payment)
            .join(Payment, Payment.order_id == Order.id)
            .filter(
                Order.status == OrderStatus.DELIVERED,
                Order.payment_method.in_([m.value for m in _ELECTRONIC_METHODS]),
                Payment.status.in_([s.value for s in _UNSETTLED_STATUSES]),
            )
            .order_by(Order.created_at)
            .all()
        )

        # Collect order IDs to batch-check for EARNED loyalty transactions.
        order_ids = [order.id for order, _ in rows]

        earned_order_ids: set[int] = set()
        if order_ids:
            earned_txns = (
                db.session.query(LoyaltyTransaction.order_id)
                .filter(
                    LoyaltyTransaction.transaction_type == LoyaltyTransactionType.EARNED,
                    LoyaltyTransaction.order_id.in_(order_ids),
                )
                .distinct()
                .all()
            )
            earned_order_ids = {row[0] for row in earned_txns}

        # Collect user IDs for display names.
        user_ids = list({order.user_id for order, _ in rows})
        users = {}
        if user_ids:
            users = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}

        # ── Header ────────────────────────────────────────────────────────────
        print("=" * 90)
        print("DELIVERED-BUT-UNPAID ELECTRONIC-ORDER AUDIT (read-only)")
        print("=" * 90)
        print(
            f"{'order_number':<22}"
            f"{'ord_id':>6}  "
            f"{'pay_id':>6}  "
            f"{'method':<8}"
            f"{'pay_status':<12}"
            f"{'amount':>12}  "
            f"{'loyalty':>10}  "
            f"{'created_at':<22}"
            f"{'delivered_at':<22}"
            f"customer"
        )
        print("-" * 90)

        for order, payment in rows:
            has_loyalty = order.id in earned_order_ids
            u = users.get(order.user_id)
            customer = "(unknown)"
            if u:
                name = " ".join(filter(None, [u.first_name, u.last_name])).strip()
                customer = (
                    name
                    or getattr(u, "phone", None)
                    or getattr(u, "email", None)
                    or "(no name)"
                )

            created = order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "—"
            delivered = order.updated_at.strftime("%Y-%m-%d %H:%M") if order.updated_at else "—"
            amount_str = f"{payment.amount:,.2f}" if payment.amount is not None else "—"
            loyalty_str = "EARNED" if has_loyalty else "none"

            print(
                f"{order.order_number:<22}"
                f"{order.id:>6}  "
                f"{payment.id:>6}  "
                f"{_val(payment.payment_method):<8}"
                f"{_val(payment.status):<12}"
                f"{amount_str:>12}  "
                f"{loyalty_str:>10}  "
                f"{created:<22}"
                f"{delivered:<22}"
                f"{customer}"
            )

        # ── Summary ───────────────────────────────────────────────────────────
        print("=" * 90)
        total = len(rows)
        with_loyalty = sum(1 for order, _ in rows if order.id in earned_order_ids)
        without_loyalty = total - with_loyalty

        by_method: dict[str, int] = {}
        by_pay_status: dict[str, int] = {}
        for _, payment in rows:
            m = _val(payment.payment_method)
            s = _val(payment.status)
            by_method[m] = by_method.get(m, 0) + 1
            by_pay_status[s] = by_pay_status.get(s, 0) + 1

        print(f"Total delivered-but-unpaid electronic orders: {total}")
        if total:
            print(f"  With loyalty already earned:  {with_loyalty}")
            print(f"  Without loyalty earned:       {without_loyalty}")
            print("  By payment method:")
            for method, count in sorted(by_method.items()):
                print(f"    {method:<10} {count}")
            print("  By payment status:")
            for status, count in sorted(by_pay_status.items()):
                print(f"    {status:<12} {count}")

        if total == 0:
            print("No affected orders found.")

        print("=" * 90)
        print("No data was modified. This is a read-only audit script.")


if __name__ == "__main__":
    main()
