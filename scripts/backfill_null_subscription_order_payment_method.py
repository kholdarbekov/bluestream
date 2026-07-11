"""Backfill Order.payment_method for legacy subscription-generated orders.

Context
-------
Before the subscription-order-parity fix, ``process_subscription_billing``
created each subscription order via ``create_order`` WITHOUT a payment method,
so ``orders.payment_method`` was persisted NULL while the real method lived on
a separately-created ``Payment`` row. A NULL ``payment_method`` blocks the
admin personal-card-transfer flow, the COD debt cap, and the driver cash
prompt for those orders.

The code fix stops NEW subscription orders from being NULL. This script
remediates the EXISTING ones: it copies the method from each order's own
Payment row (authoritative — that is what was actually created for the order),
falling back to the parent subscription's configured method if no Payment
exists.

Safety
------
* Dry-run by default: prints exactly what it WOULD change and exits without
  writing. Pass ``--apply`` to commit.
* Idempotent: only touches rows where ``payment_method IS NULL AND
  subscription_id IS NOT NULL``. Re-running after a successful apply is a no-op.
* Never changes a non-NULL method, never touches non-subscription orders,
  never mutates the Payment row.
* One transaction; rolls back on any error.

Run (scripts/ is NOT mounted into the business_app container):
    # dry-run
    docker compose exec -T business_app python - < scripts/backfill_null_subscription_order_payment_method.py
    # apply
    docker compose exec -T business_app python - < scripts/backfill_null_subscription_order_payment_method.py --apply
"""

import sys

from business_app import create_app, db
from business_app.models.order import Order


def _resolve_method(order):
    """The method to backfill: the order's own Payment first, then the
    subscription's configured method. Returns a ``PaymentMethod`` or None."""
    payment = getattr(order, "payment", None)
    if payment is not None and payment.payment_method is not None:
        return payment.payment_method, "payment"
    subscription = getattr(order, "subscription", None)
    if subscription is not None and subscription.payment_method is not None:
        return subscription.payment_method, "subscription"
    return None, None


def main(apply: bool) -> int:
    app = create_app()
    with app.app_context():
        candidates = (
            Order.query.filter(
                Order.payment_method.is_(None),
                Order.subscription_id.isnot(None),
            )
            .order_by(Order.created_at.asc())
            .all()
        )

        print(f"Found {len(candidates)} NULL-payment_method subscription order(s).")
        if not candidates:
            print("Nothing to backfill.")
            return 0

        planned = []
        unresolved = []
        for order in candidates:
            method, source = _resolve_method(order)
            if method is None:
                unresolved.append(order)
                print(
                    f"  UNRESOLVED order {order.id} ({order.order_number}) "
                    f"subscription_id={order.subscription_id}: no payment and no "
                    f"subscription method — SKIPPED"
                )
                continue
            planned.append((order, method))
            print(
                f"  order {order.id} ({order.order_number}) status={order.status.value} "
                f"subscription_id={order.subscription_id}: NULL -> {method.value} "
                f"(from {source})"
            )

        if unresolved:
            print(
                f"\nWARNING: {len(unresolved)} order(s) could not be resolved and "
                f"were skipped. Investigate them by hand."
            )

        if not apply:
            print(
                f"\nDRY-RUN: would update {len(planned)} order(s). "
                f"Re-run with --apply to commit."
            )
            return 0

        try:
            for order, method in planned:
                order.payment_method = method
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 — surface and roll back any failure
            db.session.rollback()
            print(f"ERROR: rolled back, no changes committed: {exc}")
            return 1

        print(f"\nAPPLIED: updated {len(planned)} order(s).")
        return 0


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv))
