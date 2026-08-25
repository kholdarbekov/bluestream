"""Helpers for deriving consistent payment amount projections."""

from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import and_, or_

from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

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


# Order statuses in which the order still exists and has not left the door.
# The complement ({DELIVERED, CANCELLED, RETURNED}) is derived from this set in
# PaymentService rather than written out a second time — two hand-maintained
# halves of one partition drift.
LIVE_ORDER_STATUSES = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
    }
)


# Order statuses in which the order is GONE — it will never be delivered and can
# never be paid for. The other terminal status, DELIVERED, is deliberately NOT
# here: a delivered order may still owe money and may still be paid by link
# (policy case B), which is exactly what `order_is_resolved` turns on.
#
# Four services still spell this set out by hand. This is where they migrate to:
# B1's whole thesis is that a question gets written down once.
DEAD_ORDER_STATUSES = frozenset({OrderStatus.CANCELLED, OrderStatus.RETURNED})


def order_is_live(order: Any) -> bool:
    """Does this order still exist, and has it NOT yet left the door?

    The ORDER-LIFECYCLE half on its own, with NO money conjunct — the sibling
    :func:`order_is_live_and_unpaid` is defined in terms of this one, so the
    status set is written down exactly once.

    Used where the question is purely "has this order left the door yet".

    🔴 NOT the predicate for the marking-code release / payment-cancel guard in
    ``PaymentService.update_payment_status`` — that is :func:`order_is_resolved`.
    This set stops at OUT_FOR_DELIVERY, but policy case B keeps a
    DELIVERED-but-unpaid order's link payable AND its codes reserved, so a guard
    written against this predicate would strip both from the exact population
    Phase 4 exists to serve. Reach for :func:`order_is_resolved` for any
    "may we end this payment" question; this one is a lifecycle test only.

    Kept public although :func:`order_is_live_and_unpaid` is now its only
    production caller: it is the named half that makes that derivation readable,
    and "has it left the door" is a question that recurs. It is a lifecycle
    predicate, never a payment one — that is the whole of its contract.
    """
    if order is None:
        return False
    status = getattr(order, "status", None)
    if status is not None and not hasattr(status, "value"):
        # Tolerate a raw string status from a serialized/legacy row.
        return str(status) in {s.value for s in LIVE_ORDER_STATUSES}
    return status in LIVE_ORDER_STATUSES


def order_is_live_and_unpaid(order: Any) -> bool:
    """The order still exists, has not left the door, and still owes money.

    THE single order-side expression of "is this order still fulfillable".
    Shared by the Click late-COMPLETE re-fulfil gate and (as its complement) the
    reconcile PAY-007 guard.

    Prod incident TG_000413_26: those two places each rolled their own version of
    this question from `order.status` and reached OPPOSITE conclusions about the
    same order. reconcile read past-PENDING as "still live, don't touch it";
    the re-fulfil gate read past-PENDING as "dead, don't fulfil it". A genuine
    54 000 debit on a CONFIRMED, unpaid, out-for-delivery order was therefore
    diverted to floating customer credit while the order was delivered unpaid.

    Deliberately carries NO payment-status conjunct. Each call site composes it
    with its own payment test, because they ask genuinely different questions
    about the payment (may we still take money / did a dead payment just get
    paid / may we auto-cancel).

    DERIVED from :func:`order_is_live` — the lifecycle half is expressed once,
    here composed with the money conjunct.
    """
    if getattr(order, "is_paid", False):
        return False
    return order_is_live(order)


def order_is_resolved(order: Any) -> bool:
    """Has this order reached its END STATE — nothing further owed, nothing
    further to deliver?

    THE single expression of "this order is finished with", and the ORDER-SIDE
    half of :func:`order_is_payable_online`, which is DERIVED from it below so
    the question is written down exactly once.

    An order resolves in exactly two ways:

    * it is **paid** — settled on whatever rail, including cash at the door; or
    * it is **dead** — CANCELLED / RETURNED, so there is nothing left to pay for.

    Everything else is unresolved, INCLUDING ``DELIVERED`` while unpaid. That
    carve-out is the whole point and is deliberately NOT
    :func:`order_is_live`, whose set stops at OUT_FOR_DELIVERY. Policy 2026-08-24
    case B: a customer who took delivery without paying the driver keeps the
    Click rail, a live payable link AND its reserved marking codes, because the
    money can still arrive and the receipt still has to be issued. A predicate
    that stopped at the door would declare exactly that population finished and
    strip both.

    USE THIS for any "may one abandoned gateway transaction end this payment or
    free its codes" question — see ``PaymentService.update_payment_status``. A
    missing order counts as resolved: there is nothing left to protect.
    """
    if order is None:
        return True
    if getattr(order, "is_paid", False):
        return True
    return _enum_value(getattr(order, "status", None)) in {s.value for s in DEAD_ORDER_STATUSES}


def order_is_dead(order: Any) -> bool:
    """Is this order GONE — cancelled or returned, never to be delivered or paid?

    THE single expression of :data:`DEAD_ORDER_STATUSES`, so the four services
    that used to spell the set out by hand have one place to ask. Distinct from
    :func:`order_is_resolved`, which also counts a *paid* order as finished:
    a cancelled order is dead whether or not money was taken for it, and a paid
    live order is resolved without being dead.

    A missing order counts as dead: there is nothing left to fiscalize, notify
    about, or collect for.
    """
    if order is None:
        return True
    return _enum_value(getattr(order, "status", None)) in {s.value for s in DEAD_ORDER_STATUSES}


# The rails whose money arrives through the Click merchant gateway and whose
# COMPLETED payments therefore carry a filed (or owed) fiscal receipt. PAYME is
# excluded BY CONSTRUCTION, and that exclusion is the payme carve-out for the
# owner's 2026-08-24 no-reversal rule: every Payme payment is created and looked
# up as ``PaymentMethod.PAYME`` (payme_provider.py:236, :483, :500), so a gate
# written against this set can never be reached by Payme's protocol-mandated
# CancelTransaction — no hand-written "unless payme" exclusion that a later edit
# could drop.
#
# 🔴 NOT the same set as ``prometheus_metrics._pending_payment_rows``, which
# deliberately includes PAYME: that query asks "which PENDING payments are
# stuck at a gateway", a question Payme shares. This one asks "whose receipt
# cannot be un-filed", which Payme does not.
FISCALIZED_RAILS = frozenset({PaymentMethod.CLICK, PaymentMethod.CARD})

# Value-string projection of the same set, for the string-keyed call sites.
# Derived, never re-typed — two hand-maintained halves of one set drift.
ONLINE_PAYABLE_METHOD_VALUES = {method.value for method in FISCALIZED_RAILS}


def order_is_payable_online(order: Any, payment: Any) -> bool:
    """May the customer still pay this order's gateway link RIGHT NOW?

    THE single authority on payability, consumed by the Click PREPARE guard.
    Under the 2026-08-24 policy the payable window runs from order creation
    until the order is SETTLED or dead — deliberately THROUGH delivery, because
    a customer who took delivery without paying cash keeps the Click rail and
    may pay the link afterwards (case B). That is why this is NOT
    :func:`order_is_live_and_unpaid`, which excludes DELIVERED.

    Refused when:
    * the order is gone (CANCELLED / RETURNED) — nothing left to pay for;
    * the order is already paid — including one settled as cash at the door,
      which is what stops most double-payments at the card rather than having
      to reverse them afterwards;
    * the payment is not awaiting money (already COMPLETED, or cancelled by the
      gateway itself);
    * the rail is not one we can actually fiscalize.

    A PREPARE that passes this and a COMPLETE that lands after the order was
    settled at the door is still possible — the customer's bank step sits
    between the two — so ``handle_complete`` keeps its own late-debit handling.
    """
    if order is None or payment is None:
        return False

    # DERIVED — the order-side half is order_is_resolved, never a second copy of
    # "paid or dead". reconcile's guard and this payability test must agree by
    # construction: the guard exists precisely so we never write a status that
    # would make THIS function refuse a link it would otherwise accept.
    if order_is_resolved(order):
        return False

    if _enum_value(getattr(payment, "payment_method", None)) not in ONLINE_PAYABLE_METHOD_VALUES:
        return False

    return _enum_value(getattr(payment, "status", None)) in {
        PaymentStatus.PENDING.value,
        PaymentStatus.PROCESSING.value,
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

    reserved = reserved_prepayment_amount(payment)
    return {
        "amount": amount,
        "amount_collected": amount_collected,
        "outstanding_amount": outstanding_amount,
        "reserved_prepayment_amount": reserved,
        "net_outstanding_amount": max(Decimal("0.00"), outstanding_amount - reserved),
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


def reserved_prepayment_amount(payment: Any, *, ceiling: Optional[Decimal] = None) -> Decimal:
    """Customer prepayment currently parked on this payment, clamped to what is
    still owed.

    Mirror of the ``prepaid_reservation`` allocation total that
    ``CashCollectionService._sync_reserved_prepayment_projection`` stamps into
    ``provider_data``. Reading the stamp rather than re-summing the allocations
    keeps this usable from read surfaces that hold no session.

    The clamp is what makes the figure safe to subtract anywhere: a reservation
    can outlive the balance it was parked against (the payment gets settled from
    another source, or the order is edited down), and an unclamped stamp would
    then drive the net receivable negative.

    ``ceiling`` overrides what it is clamped against, for the one caller that
    models a balance other than the live one (``simulate_event_amount_change``
    projects the receivable a reversal would restore). Same rule, different
    baseline — do not re-implement the clamp at the call site.
    """
    if payment is None:
        return Decimal("0.00")
    provider_data = getattr(payment, "provider_data", None) or {}
    if not isinstance(provider_data, dict):
        return Decimal("0.00")
    reserved = max(Decimal("0.00"), _to_decimal(provider_data.get("cod_prepayment_reserved_amount") or 0))
    limit = open_receivable_amount(payment) if ceiling is None else max(Decimal("0.00"), _to_decimal(ceiling))
    return min(reserved, limit)


def net_open_receivable_amount(payment: Any) -> Decimal:
    """Money that still has to be COLLECTED on this payment — the SSOT figure.

    ``open_receivable_amount`` minus prepayment the customer has already handed
    over and that is reserved against this payment. Every surface that quotes
    "how much is left to pay" or that decides how much incoming cash a payment
    may absorb must use THIS, not the gross receivable:

    * quoting gross tells an admin/driver to collect money the customer already
      paid, and
    * allocating against gross lets an unrelated settlement fill the space the
      reservation was holding, orphaning it (prod order AD_000630_26).

    The reservation itself is turned into collected money by
    ``CashCollectionService.consume_reserved_prepayment_for_payment`` at
    delivery, which is what closes the remaining gap.
    """
    return max(Decimal("0.00"), open_receivable_amount(payment) - reserved_prepayment_amount(payment))


def unpaid_after_delivery_clause():
    """ "Owes money after taking delivery" — DISPLAY AND DEBT-CAP ONLY.

    🔴 NEVER pass this to an allocator. :func:`open_receivable_clause` is
    deliberately narrower and that asymmetry is a money-safety guard; read its
    docstring before touching either.

    Policy 2026-08-24, case B: a customer who takes delivery without paying the
    driver keeps the Click rail and a live payable link, so the money can still
    arrive and the receipt can still be issued. Until it does, the business is
    owed money — but ``open_receivable_clause`` excludes a PENDING electronic
    payment on purpose, so that debt was invisible to the debtor lists, the COD
    statements and the debt cap. That invisibility IS prod incident
    TG_000413_26's end state.

    The fix is a SECOND, wider clause used only where we DISPLAY or COUNT debt,
    never where we ALLOCATE cash against it. Widening the allocator instead
    would let an unrelated customer's banknotes be absorbed by an unpaid Click
    order and then destroyed when that Click payment completes.

    Callers add their own ``Order.status == DELIVERED`` conjunct, exactly as
    they do for ``open_receivable_clause``.
    """
    from business_app.models.order import Order
    from business_app.models.payment import Payment

    return or_(
        open_receivable_clause(),
        and_(
            Payment.payment_method.in_(list(ONLINE_PAYABLE_METHOD_VALUES)),
            Payment.status.in_(
                [
                    PaymentStatus.PENDING.value,
                    PaymentStatus.CANCELLED.value,
                    PaymentStatus.FAILED.value,
                ]
            ),
            Payment.outstanding_amount > Decimal("0.00"),
            Order.is_paid.is_(False),
        ),
    )


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
