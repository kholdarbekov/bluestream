"""Remediate personal-card-transfer surplus stranded as customer prepaid credit.

Background
----------
``CashCollectionService.post_collection`` used to treat PERSONAL_CARD_TRANSFER as
an exclusive branch: it allocated to the target order's payment (capped at that
order's outstanding) and then returned without ever reaching
``_allocate_oldest_first`` — the allocator every other collection source uses to
spread money across the customer's other DELIVERED COD debts.

The residual instead fell through to ``auto_reserve_against_pending_payments``,
whose filter is ``Order.status.in_(RESERVABLE_ORDER_STATUSES)`` = PENDING /
CONFIRMED / PREPARING / OUT_FOR_DELIVERY. DELIVERED is deliberately excluded
there (it is a *reservation* primitive for future orders, not a settlement one),
so an already-delivered debt was structurally unreachable and the surplus parked
itself as prepaid credit.

Reported prod case: a customer with two 90k DELIVERED COD debts paid 100k by
personal card transfer. The targeted order settled; the 10k surplus became
prepaid credit instead of paying down the second 90k debt — and because credit
does not decrement ``get_active_cod_debt_count``, the customer stayed
COD-restricted by a debt they had already funded.

``post_collection`` now spills the residual through ``_allocate_oldest_first``.
This script applies that same rule retroactively to events posted before the fix.

What this does
--------------
For each non-voided PERSONAL_CARD_TRANSFER event still carrying
``unapplied_amount > 0``, it re-runs the very allocator the fixed code path now
runs — ``_allocate_oldest_first`` — against the customer's outstanding DELIVERED
COD debts, oldest-first. Deliberately the private SSOT method rather than a
re-implementation: remediation must not drift from production behaviour.

It never collects new cash, never touches a card gateway, and never invents
money. It only re-attributes credit the customer has already paid, and only ever
onto debts they actually owe. Surplus with nowhere to go is left as credit —
that outcome is correct and is preserved.

Idempotent: an event whose surplus has already been applied has
``unapplied_amount == 0`` and is skipped. Safe to re-run.

KNOWN LIMITATION — this finds surplus still sitting as *unapplied* credit only.
If the customer had a non-delivered COD order at the time, the old code path's
``auto_reserve_against_pending_payments`` sweep will already have *reserved* the
surplus against it, which drives ``unapplied_amount`` to 0 (the reservation
decrement is unconditional) even though nothing was settled. Such surplus is
invisible to this script, so "nothing to do" means "no unapplied surplus", NOT
"no misallocated money". That money is not lost — a reservation is real,
reversible credit that is consumed when its order is delivered — but it is
earmarked for a future order rather than paying an existing delivered debt.
Reclaiming it is a separate business decision and is deliberately out of scope
here.

Run (``scripts/`` is not mounted into the container, so pipe via stdin; the args
after ``-`` still reach argparse). Dry-run prints before/after and rolls back:

    docker compose exec -T business_app python - --dry-run \
        < scripts/remediate_stranded_card_transfer_surplus.py

Apply to every stranded event:

    docker compose exec -T business_app python - --commit \
        < scripts/remediate_stranded_card_transfer_surplus.py

Apply to one customer only:

    docker compose exec -T business_app python - --customer-id 42 --commit \
        < scripts/remediate_stranded_card_transfer_surplus.py
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import List, Optional

from business_app import create_app, db
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import CashCollectionSource


def _stranded_events(customer_id: Optional[int]) -> List[CashCollectionEvent]:
    """Non-voided card-transfer events still holding unapplied surplus."""
    query = CashCollectionEvent.query.filter(
        CashCollectionEvent.source == CashCollectionSource.PERSONAL_CARD_TRANSFER,
        CashCollectionEvent.voided_at.is_(None),
        CashCollectionEvent.unapplied_amount > 0,
    )
    if customer_id is not None:
        query = query.filter(CashCollectionEvent.customer_id == customer_id)
    return query.order_by(
        CashCollectionEvent.occurred_at.asc(), CashCollectionEvent.id.asc()
    ).all()


def _describe_debts(service: CashCollectionService, customer_id: int) -> str:
    payments = service.get_active_cod_payments_for_customer(customer_id)
    if not payments:
        return "no delivered COD debt"
    return ", ".join(
        f"{payment.order.order_number if payment.order else payment.order_id}"
        f"={payment.outstanding_amount}"
        for payment in payments
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply stranded personal-card-transfer surplus to delivered COD debts.",
    )
    parser.add_argument("--customer-id", type=int, default=None)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Report and roll back.")
    group.add_argument("--commit", action="store_true", help="Persist the remediation.")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        service = CashCollectionService()
        events = _stranded_events(args.customer_id)

        if not events:
            print("No UNAPPLIED personal-card-transfer surplus found. Nothing to do.")
            print(
                "Note: surplus the old code already reserved against a non-delivered "
                "order is not visible here — see the KNOWN LIMITATION in this script's "
                "docstring before concluding a customer is unaffected."
            )
            return 0

        print(f"Found {len(events)} event(s) carrying unapplied surplus.\n")

        total_applied = Decimal("0.00")
        remediated = 0

        for event in events:
            customer = User.query.get(event.customer_id)
            order = Order.query.get(event.order_id) if event.order_id else None
            before = service._to_decimal(event.unapplied_amount)

            print(f"event #{event.id}  customer={event.customer_id} ({customer.phone if customer else '?'})")
            print(f"  posted {event.amount} against {order.order_number if order else '(no order)'}")
            print(f"  surplus stranded as credit : {before}")
            print(f"  delivered COD debts before : {_describe_debts(service, event.customer_id)}")

            # The SSOT allocator the fixed post_collection now runs for the
            # residual. Re-running it here is what makes this remediation
            # identical to the code path, rather than a parallel guess at it.
            #
            # Notifications are suppressed deliberately. `.delay()` publishes to
            # the broker immediately and does NOT roll back, so a --dry-run would
            # otherwise tell a customer a weeks-old debt was just settled and burn
            # the notification's 24h idempotency key — silently swallowing the real
            # confirmation on the subsequent --commit.
            service._allocate_oldest_first(
                event=event,
                customer_id=event.customer_id,
                order_id=event.order_id,
                allocation_mode="auto",
                trigger_completion_notification=False,
            )
            db.session.flush()

            after = service._to_decimal(event.unapplied_amount)
            applied = before - after
            total_applied += applied
            if applied > Decimal("0.00"):
                remediated += 1

            print(f"  applied to debts           : {applied}")
            print(f"  surplus remaining as credit: {after}")
            print(f"  delivered COD debts after  : {_describe_debts(service, event.customer_id)}")
            if applied <= Decimal("0.00"):
                print("  -> no delivered debt to absorb it; correctly left as credit")
            print()

        print(f"{remediated} event(s) remediated; {total_applied} total re-attributed to real debt.")

        if args.commit:
            db.session.commit()
            print("COMMITTED.")
        else:
            db.session.rollback()
            print("DRY-RUN — rolled back, nothing persisted.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
