"""Helpers for deriving consistent payment amount projections."""

from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_

from shared.enums import PaymentMethod, PaymentStatus

PREPAID_METHOD_VALUES = {
    PaymentMethod.CARD.value,
    PaymentMethod.PAYME.value,
    PaymentMethod.CLICK.value,
    # Retained for HISTORICAL orders only — points are no longer accepted as a
    # payment method, but pre-existing points-paid orders must still project as
    # settled prepayments.
    PaymentMethod.LOYALTY_POINTS.value,
    PaymentMethod.BUSINESS_ACCOUNT.value,
}


def _enum_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(Decimal("0.01"))


def is_settled_prepayment(payment: Any) -> bool:
    """Return True when payment is a non-COD prepaid payment marked completed."""
    method_value = _enum_value(getattr(payment, "payment_method", None))
    status_value = _enum_value(getattr(payment, "status", None))
    return method_value in PREPAID_METHOD_VALUES and status_value == PaymentStatus.COMPLETED.value


def get_payment_projection(payment: Any) -> Dict[str, Any]:
    """
    Return normalized payment projection fields for read/write surfaces.

    For completed prepaid payments, projection is always fully settled:
    amount_collected == amount and outstanding_amount == 0.
    """
    amount = max(Decimal("0.00"), _to_decimal(getattr(payment, "amount", 0)))
    amount_collected = max(
        Decimal("0.00"),
        min(amount, _to_decimal(getattr(payment, "amount_collected", 0))),
    )
    outstanding_amount = max(
        Decimal("0.00"),
        _to_decimal(getattr(payment, "outstanding_amount", 0)),
    )

    if is_settled_prepayment(payment):
        amount_collected = amount
        outstanding_amount = Decimal("0.00")
    else:
        method_value = _enum_value(getattr(payment, "payment_method", None))
        if method_value == PaymentMethod.CASH.value:
            outstanding_amount = max(Decimal("0.00"), amount - amount_collected)
        else:
            outstanding_amount = min(amount, outstanding_amount)

    return {
        "amount": amount,
        "amount_collected": amount_collected,
        "outstanding_amount": outstanding_amount,
        "payment_method": _enum_value(getattr(payment, "payment_method", None)),
        "payment_status": _enum_value(getattr(payment, "status", None)),
        "is_settled_prepayment": is_settled_prepayment(payment),
    }


def open_receivable_amount(payment: Any) -> Decimal:
    """Money still owed on this payment — RAIL-AGNOSTIC.

    This is the SSOT for "does this order still owe money", replacing the
    ``payment_method == CASH`` proxy that used to stand in for it across 14+
    hand-rolled queries. A card-paid order whose total was edited upward at the
    door owes the delta exactly as a COD order owes its balance; the rail the
    first payment travelled on says nothing about whether money is outstanding.

    Deliberately computed as ``amount - amount_collected`` rather than read from
    the stored ``outstanding_amount`` column. The column is stale on a
    gateway-cancelled electronic payment — the provider callback zeroes it while
    nothing was ever collected. ``CashCollectionService.convert_electronic_order_to_cash``
    re-derives it with this same expression for precisely that shape. Reading
    the column here would tell a driver that a cancelled-Click order at the door
    owes nothing.

    A settled prepayment owes nothing whatever its column says: that carve-out is
    :func:`is_settled_prepayment`, the same one :func:`get_payment_projection`
    applies, so this figure and the figure every read surface renders agree by
    construction.
    """
    if payment is None:
        return Decimal("0.00")
    if is_settled_prepayment(payment):
        return Decimal("0.00")
    amount = _to_decimal(getattr(payment, "amount", 0))
    amount_collected = _to_decimal(getattr(payment, "amount_collected", 0))
    return max(Decimal("0.00"), amount - amount_collected)


def has_open_receivable(payment: Any) -> bool:
    """True when this payment still owes money, on any rail."""
    return open_receivable_amount(payment) > Decimal("0.00")


def open_receivable_clause():
    """Is this an OPEN DEBT ROW IN THE LEDGER? — the query-level predicate.

    🔴 DELIBERATELY NARROWER THAN :func:`has_open_receivable`, AND THE
    ASYMMETRY IS A MONEY-SAFETY GUARD. Do not "unify" them.

    The two answer different questions:

    * :func:`open_receivable_amount` — "how much is due AT THIS DOOR, right
      now". Used by the driver's prompt and by the explicit settle paths. It
      reports the full amount for an unpaid Click order, because the driver
      really does collect it (and the order is then converted to CASH).
    * this clause — "is this a standing debt the ledger may allocate arbitrary
      cash against". An unpaid electronic order is NOT: it is settled only
      through an EXPLICIT target (``convert_electronic_order_to_cash`` via the
      door flow or Record Personal Card Payment), never by a ring walk.

    WHY THAT MATTERS — the money bug this guard exists to prevent.
    ``Payment.__init__`` seeds ``outstanding_amount = amount - amount_collected``,
    so EVERY unpaid Click row carries a positive outstanding. A plain
    "outstanding > 0" clause therefore made a still-live Click order an
    allocation candidate, and:

      1. an unrelated customer's cash (or a coworker's, at a shared place) got
         absorbed by it while the payer's own COD debt stayed open; and
      2. when the customer then paid the Click link,
         ``PaymentService._sync_completed_prepayment_projection`` forces
         ``amount_collected = amount`` — DESTROYING the cash allocation, so the
         business lost the banknotes the driver had already banked.

    So an electronic payment is a ledger receivable in exactly ONE state:
    ``PARTIALLY_PAID`` — the repriced-after-settlement shape this whole design
    exists for (prod order 961; the card already settled, the order then grew).
    PENDING/PROCESSING have a live gateway link; CANCELLED/FAILED go through the
    conversion path; COMPLETED owes nothing.

    CASH is unconstrained by status, exactly as before this change.

    Callers add their own ``Order.status == DELIVERED`` conjunct — this clause is
    only the "still owes money" half. Do not fold the order-status test in here:
    the ceiling queries and the debt-cap counts scope it differently.
    """
    from business_app.models.payment import Payment

    prepaid_methods = [PaymentMethod(value) for value in PREPAID_METHOD_VALUES]
    return and_(
        Payment.outstanding_amount > 0,
        or_(
            Payment.payment_method.notin_(prepaid_methods),
            Payment.status == PaymentStatus.PARTIALLY_PAID,
        ),
    )


def is_ledger_receivable(payment: Any) -> bool:
    """Row-level Python mirror of :func:`open_receivable_clause`.

    For call sites that hold a loaded ``Payment`` rather than a query — chiefly
    the allocator's "current order" appends, which must apply the same rule the
    ring queries applied or a live gateway payment sneaks back in through the
    side door.
    """
    if payment is None:
        return False
    if _to_decimal(getattr(payment, "outstanding_amount", 0)) <= Decimal("0.00"):
        return False
    method_value = _enum_value(getattr(payment, "payment_method", None))
    if method_value not in PREPAID_METHOD_VALUES:
        return True
    return _enum_value(getattr(payment, "status", None)) == PaymentStatus.PARTIALLY_PAID.value
