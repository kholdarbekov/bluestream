"""READ-ONLY audit: paid orders that show a phantom outstanding balance.

Background (prod incident, order TG_000190_26)
----------------------------------------------
When an admin edited a *paid* order's total upward, the old
``OrderEditService._recompute_totals`` re-priced the Payment and hand-set
``outstanding_amount = new_total - amount_collected`` **without** routing the
change through ``sync_payment_projection`` and **without** netting the increase
against cash the customer had already over-paid at delivery.

That produced two anomalies on the affected Payment row:

  1. ``order.is_paid is True`` / ``payment.status == COMPLETED`` *while*
     ``payment.outstanding_amount > 0`` — an impossible combination that
     ``sync_payment_projection`` never produces. It also makes the order match
     the active-COD-debt query (outstanding>0 AND delivered), so the customer
     can be wrongly flagged as a debtor / hit the COD limit.
  2. A phantom outstanding equal to cash the driver already collected, which is
     sitting as the customer's unapplied prepayment credit.

This script finds every order carrying that fingerprint and reports, per order,
how much customer cash credit is available to settle it (available unapplied
prepayment + this order's own over-collection currently reserved against the
customer's other pending orders). It modifies NOTHING.

Run (``scripts/`` is not mounted into the container, so pipe via stdin):

    docker compose exec -T business_app python - < scripts/audit_paid_orders_with_phantom_outstanding.py

To remediate the orders this finds, use
``scripts/remediate_overcollection_after_edit.py``.
"""

from __future__ import annotations

from decimal import Decimal

from business_app import create_app, db
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import PaymentMethod, PaymentStatus


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def main() -> None:
    app = create_app()
    with app.app_context():
        cash_service = CashCollectionService()

        # Fingerprint: outstanding>0 while the order/payment still reads as
        # fully paid. A legitimately-unpaid COD debt has is_paid=False and a
        # PENDING/PARTIALLY_PAID status, so it is excluded here.
        rows = (
            db.session.query(Payment, Order)
            .join(Order, Order.id == Payment.order_id)
            .filter(
                Payment.outstanding_amount > Decimal("0.00"),
                db.or_(Order.is_paid.is_(True), Payment.status == PaymentStatus.COMPLETED),
            )
            .order_by(Payment.id)
            .all()
        )

        if not rows:
            print("No paid orders with a phantom outstanding balance found. Nothing to do.")
            return

        users = {
            u.id: u
            for u in User.query.filter(User.id.in_({p.user_id for p, _ in rows})).all()
        }

        print(f"Affected orders: {len(rows)}")
        print("=" * 110)
        header = (
            f"{'order':<16}{'method':<8}{'total':>12}{'collected':>12}"
            f"{'outstanding':>13}{'creditable':>12}  fixability"
        )
        print(header)
        print("-" * 110)

        auto_full = auto_partial = no_credit = 0
        for payment, order in rows:
            total = Decimal(str(payment.amount or 0))
            collected = Decimal(str(payment.amount_collected or 0))
            outstanding = Decimal(str(payment.outstanding_amount or 0))

            # Over-collection credit is cash-only-usable; card orders get 0.
            creditable = cash_service.estimate_settleable_credit_for_order(order)

            if payment.payment_method != PaymentMethod.CASH:
                fixability = "card — no credit; remediation only corrects is_paid/status"
                no_credit += 1
            elif creditable >= outstanding:
                fixability = "AUTO-FIXABLE (full settle from credit)"
                auto_full += 1
            elif creditable > Decimal("0.00"):
                fixability = (
                    f"AUTO-FIXABLE (partial: {creditable} credit, "
                    f"{outstanding - creditable} stays owed)"
                )
                auto_partial += 1
            else:
                fixability = "no credit — remediation only corrects is_paid/status; residual owed"
                no_credit += 1

            u = users.get(payment.user_id)
            label = ""
            if u:
                name = " ".join(filter(None, [u.first_name, u.last_name])).strip()
                label = name or getattr(u, "phone", None) or getattr(u, "email", None) or ""
            print(
                f"{(order.order_number or str(order.id)):<16}"
                f"{_status_value(payment.payment_method):<8}"
                f"{float(total):>12.2f}{float(collected):>12.2f}"
                f"{float(outstanding):>13.2f}{float(creditable):>12.2f}  {fixability}"
            )
            if label:
                print(f"{'':<16}customer: {label} (user_id={payment.user_id}), "
                      f"order_status={_status_value(order.status)}")

        print("=" * 110)
        print(
            f"Summary: {auto_full} fully auto-fixable, {auto_partial} partially "
            f"auto-fixable, {no_credit} need is_paid/status correction only."
        )
        print("No data was modified (read-only audit).")


if __name__ == "__main__":
    main()
