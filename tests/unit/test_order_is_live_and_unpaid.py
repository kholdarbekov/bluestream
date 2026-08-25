"""`order_is_live_and_unpaid` — the ONE order-side expression of "still fulfillable".

Prod incident TG_000413_26 (2026-08-21): the Click late-COMPLETE re-fulfil gate
and `reconcile_pending_payments`' PAY-007 guard each derived this question from
`order.status` independently, and reached opposite conclusions about the same
order. reconcile read past-PENDING as "still live, protect the payment"; the
re-fulfil gate read past-PENDING as "dead, don't fulfil". A genuine 54 000 debit
on a CONFIRMED, unpaid, out-for-delivery order was diverted to floating customer
credit, and the order was delivered unpaid and un-fiscalized.
"""

import pytest

from business_app.services.payment_service import _RAIL_LOCKED_ORDER_STATUSES
from business_app.utils.payment_projection import (
    LIVE_ORDER_STATUSES,
    order_is_live,
    order_is_live_and_unpaid,
    order_is_payable_online,
    order_is_resolved,
)
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


class _Order:
    def __init__(self, status, is_paid=False):
        self.status = status
        self.is_paid = is_paid


@pytest.mark.parametrize("status", sorted(LIVE_ORDER_STATUSES, key=lambda s: s.value))
def test_live_unpaid_statuses_are_fulfillable(status):
    assert order_is_live_and_unpaid(_Order(status)) is True


@pytest.mark.parametrize("status", [OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED])
def test_terminal_statuses_are_not_fulfillable(status):
    assert order_is_live_and_unpaid(_Order(status)) is False


@pytest.mark.parametrize("status", sorted(LIVE_ORDER_STATUSES, key=lambda s: s.value))
def test_a_paid_order_is_never_fulfillable_again(status):
    assert order_is_live_and_unpaid(_Order(status, is_paid=True)) is False


def test_missing_order_is_not_fulfillable():
    assert order_is_live_and_unpaid(None) is False


def test_raw_string_status_is_tolerated():
    """Serialized/legacy rows can carry a plain string."""
    assert order_is_live_and_unpaid(_Order("confirmed")) is True
    assert order_is_live_and_unpaid(_Order("delivered")) is False


def test_live_and_rail_locked_are_one_partition_not_two_lists():
    """The complement must be DERIVED. Two hand-maintained halves drift the
    moment a status is added to the enum."""
    assert LIVE_ORDER_STATUSES.isdisjoint(_RAIL_LOCKED_ORDER_STATUSES)
    assert LIVE_ORDER_STATUSES | _RAIL_LOCKED_ORDER_STATUSES == set(OrderStatus)


def test_pending_is_not_the_only_live_status():
    """The exact regression: the old gate was `status == PENDING`."""
    assert order_is_live_and_unpaid(_Order(OrderStatus.CONFIRMED)) is True
    assert order_is_live_and_unpaid(_Order(OrderStatus.PREPARING)) is True
    assert order_is_live_and_unpaid(_Order(OrderStatus.OUT_FOR_DELIVERY)) is True


class TestOrderIsLiveTheNoMoneySibling:
    """`order_is_live` — the pure lifecycle half, "has it left the door yet".

    🔴 NOT B1's guard predicate. An earlier draft of this docstring said it was;
    that was wrong, and it contradicted both the function's own docstring and
    `TestOrderIsResolvedGovernsTheEndOfAPayment` below. `order_is_live` stops at
    OUT_FOR_DELIVERY, so a guard built on it strips the payment and the codes of
    a DELIVERED-but-unpaid order — policy case B. The guard uses
    `order_is_resolved`.

    What this predicate IS: the no-money conjunct `order_is_live_and_unpaid` is
    derived from, so the live-status set is written down exactly once.
    """

    @pytest.mark.parametrize("status", sorted(LIVE_ORDER_STATUSES, key=lambda s: s.value))
    def test_live_statuses_are_live(self, status):
        assert order_is_live(_Order(status)) is True

    @pytest.mark.parametrize(
        "status", [OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.RETURNED]
    )
    def test_resolved_statuses_are_not_live(self, status):
        assert order_is_live(_Order(status)) is False

    def test_missing_order_is_not_live(self):
        assert order_is_live(None) is False

    def test_raw_string_status_is_tolerated(self):
        assert order_is_live(_Order("confirmed")) is True
        assert order_is_live(_Order("delivered")) is False

    @pytest.mark.parametrize("status", sorted(LIVE_ORDER_STATUSES, key=lambda s: s.value))
    def test_a_PAID_live_order_is_still_live(self, status):
        """The single cell where the two predicates disagree.

        `order_is_live` is the pure lifecycle half, so payment state cannot move
        it. This is what makes `order_is_live_and_unpaid` derivable from it
        rather than the other way round.
        """
        paid_live = _Order(status, is_paid=True)
        assert order_is_live(paid_live) is True
        assert order_is_live_and_unpaid(paid_live) is False

    def test_unpaid_is_the_only_difference_between_the_two(self):
        """The lifecycle status set must be written down ONCE.

        `order_is_live_and_unpaid` is DERIVED from `order_is_live`; two
        hand-maintained copies of the status set drift the moment a status is
        added to the enum.
        """
        for status in OrderStatus:
            for is_paid in (True, False):
                order = _Order(status, is_paid=is_paid)
                assert order_is_live_and_unpaid(order) is (order_is_live(order) and not is_paid)


class TestOrderIsResolvedGovernsTheEndOfAPayment:
    """`order_is_resolved` — B1's guard predicate, and the ORDER-SIDE half of
    `order_is_payable_online`.

    "Has this order reached its end state?" An order resolves by being PAID (on
    any rail, cash at the door included) or by being DEAD (CANCELLED/RETURNED).
    Deliberately NOT a lifecycle test: DELIVERED-but-unpaid is UNRESOLVED, which
    is policy case B.
    """

    @pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.RETURNED])
    def test_a_dead_order_is_resolved(self, status):
        assert order_is_resolved(_Order(status)) is True

    @pytest.mark.parametrize("status", list(OrderStatus))
    def test_a_paid_order_is_resolved_whatever_its_status(self, status):
        assert order_is_resolved(_Order(status, is_paid=True)) is True

    def test_a_missing_order_is_resolved(self):
        """Nothing left to protect."""
        assert order_is_resolved(None) is True

    @pytest.mark.parametrize(
        "status",
        sorted(LIVE_ORDER_STATUSES, key=lambda s: s.value) + [OrderStatus.DELIVERED],
    )
    def test_an_unpaid_order_through_delivery_is_UNRESOLVED(self, status):
        """🔴 THE CASE-B CELL, and the whole reason this is not `order_is_live`.

        DELIVERED is absent from LIVE_ORDER_STATUSES, so a guard written against
        `order_is_live` would declare a delivered-unpaid order finished and both
        cancel its payment and free its codes — permanently locking the customer
        out of settling a debt the policy explicitly says they may still settle
        by link, and destroying the codes its receipt still needs.
        """
        assert order_is_resolved(_Order(status)) is False

    def test_delivered_is_exactly_where_the_two_predicates_must_differ(self):
        delivered_unpaid = _Order(OrderStatus.DELIVERED)
        assert order_is_live(delivered_unpaid) is False
        assert order_is_resolved(delivered_unpaid) is False

    def test_raw_string_status_is_tolerated(self):
        assert order_is_resolved(_Order("cancelled")) is True
        assert order_is_resolved(_Order("delivered")) is False

    def test_it_is_RAIL_AGNOSTIC_and_takes_no_payment(self):
        """The chosen shape (option b) over reusing `order_is_payable_online`.

        The guard's question is purely order-side. Keeping it rail-free means the
        rule stays correct if a non-Click cancel branch is ever added, and avoids
        asking "is this payment still payable" in order to decide whether to make
        it unpayable — which would be circular, since payability reads the very
        `payment.status` the guard is deciding whether to overwrite.
        """
        import inspect

        assert list(inspect.signature(order_is_resolved).parameters) == ["order"]

    @pytest.mark.parametrize("status", list(OrderStatus))
    @pytest.mark.parametrize("is_paid", [True, False])
    def test_payable_online_is_DERIVED_not_a_second_copy_of_the_rule(self, status, is_paid):
        """One question, one expression. `order_is_payable_online` must never
        answer "is this order finished with" differently from the guard."""

        class _Payment:
            payment_method = PaymentMethod.CLICK
            status = PaymentStatus.PENDING

        order = _Order(status, is_paid=is_paid)
        if order_is_resolved(order):
            assert order_is_payable_online(order, _Payment()) is False
        else:
            assert order_is_payable_online(order, _Payment()) is True


def test_click_service_has_no_local_payability_proxy():
    """The re-fulfil gate must not re-derive fulfillability from order.status.

    Scoped to the Click service on purpose. `payment_tasks.py` also compares
    against OrderStatus.PENDING, but those are different questions and must NOT
    be collapsed into this predicate:
      * :82/:87  — "is the order still PENDING, so confirm it now" is a state
        TRANSITION, not a payability test.
      * :211     — the PAY-007 auto-cancel guard asks "has this order started
        moving", which is narrower than "is it live and unpaid" (it deliberately
        still permits cancelling a PENDING order). Widening it to this predicate
        would change the cancel policy, which is DECISION 2's substance and is
        gated on the COD-fiscalization ruling.
      * :692     — the payment-reminder gate.
    """
    from pathlib import Path

    src = Path("business_app/services/click_payment_provider_service.py").read_text()
    assert "order.status == OrderStatus.PENDING" not in src, (
        "click_payment_provider_service still derives fulfillability from a local "
        "PENDING check; use order_is_live_and_unpaid instead"
    )
    assert "order_is_live_and_unpaid" in src
