"""Pins the rail-agnostic open-receivable SSOT.

The Python half (`open_receivable_amount`) and the SQL half
(`open_receivable_clause`) are two expressions of ONE decision. The equivalence
test at the bottom is what stops them drifting.

Plan: docs/superpowers/plans/2026-08-08-open-receivable-ssot.md
"""

from decimal import Decimal

import pytest

from business_app import db
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.utils.payment_projection import (
    has_open_receivable,
    is_ledger_receivable,
    open_receivable_amount,
    open_receivable_clause,
)
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _payment(method, status, amount, collected, outstanding=None):
    """Detached Payment for the pure-Python half (no DB round trip)."""
    return Payment(
        order_id=1,
        user_id=1,
        payment_method=method,
        amount=Decimal(str(amount)),
        amount_collected=Decimal(str(collected)),
        outstanding_amount=(None if outstanding is None else Decimal(str(outstanding))),
        status=status,
        currency="UZS",
    )


def _make_order_payment(user, method, status, amount, collected, suffix):
    order = Order(
        user_id=user.id,
        order_number=f"ORD-ORP-{suffix}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal(str(amount)),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(str(amount)),
        payment_method=method,
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=method,
        amount=Decimal(str(amount)),
        amount_collected=Decimal(str(collected)),
        outstanding_amount=Decimal(str(amount - collected)),
        status=status,
        currency="UZS",
        payment_id=f"pay-orp-{suffix}",
        # ck_payments_cash_completed_requires_collector: a CASH payment may only
        # reach COMPLETED with a recorded collector. Non-cash rows are exempt by
        # the constraint's first disjunct — which is exactly why settling an
        # electronic receivable in place needs no collector back-fill.
        collected_by=(
            user.id
            if method == PaymentMethod.CASH and status == PaymentStatus.COMPLETED
            else None
        ),
    )
    db.session.add(payment)
    return order, payment


@pytest.mark.unit
class TestOpenReceivableAmount:
    def test_click_partially_paid_reports_the_delta(self):
        """Prod order 961: 2 bottles paid by Click, 3rd added at the door."""
        p = _payment(PaymentMethod.CLICK, PaymentStatus.PARTIALLY_PAID, 90000, 60000)
        assert open_receivable_amount(p) == Decimal("30000.00")
        assert has_open_receivable(p) is True

    def test_click_completed_reports_zero_even_with_stale_outstanding(self):
        """A settled prepayment owes nothing however stale the column is."""
        p = _payment(PaymentMethod.CLICK, PaymentStatus.COMPLETED, 90000, 90000, outstanding=90000)
        assert open_receivable_amount(p) == Decimal("0.00")
        assert has_open_receivable(p) is False

    def test_click_cancelled_with_zeroed_column_still_owes_the_full_amount(self):
        """The gateway zeroes the column on cancel; the customer still owes."""
        p = _payment(PaymentMethod.CLICK, PaymentStatus.CANCELLED, 45000, 0, outstanding=0)
        assert open_receivable_amount(p) == Decimal("45000.00")
        assert has_open_receivable(p) is True

    def test_click_pending_owes_the_full_amount(self):
        p = _payment(PaymentMethod.CLICK, PaymentStatus.PENDING, 36000, 0)
        assert open_receivable_amount(p) == Decimal("36000.00")

    def test_cash_pending_owes_the_full_amount(self):
        p = _payment(PaymentMethod.CASH, PaymentStatus.PENDING, 36000, 0)
        assert open_receivable_amount(p) == Decimal("36000.00")

    def test_cash_fully_collected_owes_nothing(self):
        p = _payment(PaymentMethod.CASH, PaymentStatus.COMPLETED, 36000, 36000)
        assert open_receivable_amount(p) == Decimal("0.00")
        assert has_open_receivable(p) is False

    def test_over_collection_never_reports_negative(self):
        p = _payment(PaymentMethod.CASH, PaymentStatus.COMPLETED, 36000, 40000)
        assert open_receivable_amount(p) == Decimal("0.00")

    def test_none_payment_owes_nothing(self):
        assert open_receivable_amount(None) == Decimal("0.00")
        assert has_open_receivable(None) is False


@pytest.mark.unit
class TestOpenReceivableClauseEquivalence:
    def test_ledger_clause_is_a_strict_subset_of_the_door_predicate(self, app, db, sample_user):
        """THE anti-drift test.

        The two halves answer different questions and the SQL half is
        deliberately NARROWER (see `open_receivable_clause`'s docstring): the
        ledger never allocates arbitrary cash against a live gateway payment,
        while the driver at the door still collects it.

        The contract pinned here is therefore: SQL ⊂ Python, and the difference
        is EXACTLY the unpaid-electronic rows. Any other divergence is drift.
        """
        specs = [
            (PaymentMethod.CASH, PaymentStatus.PENDING, 30000, 0),
            (PaymentMethod.CASH, PaymentStatus.COMPLETED, 30000, 30000),
            (PaymentMethod.CASH, PaymentStatus.PARTIALLY_PAID, 30000, 10000),
            (PaymentMethod.CLICK, PaymentStatus.COMPLETED, 60000, 60000),
            (PaymentMethod.CLICK, PaymentStatus.PARTIALLY_PAID, 90000, 60000),
            (PaymentMethod.CLICK, PaymentStatus.PENDING, 45000, 0),
            (PaymentMethod.PAYME, PaymentStatus.PARTIALLY_PAID, 50000, 20000),
            (PaymentMethod.BUSINESS_ACCOUNT, PaymentStatus.COMPLETED, 25000, 25000),
        ]
        created = []
        for i, (method, status, amount, collected) in enumerate(specs):
            _order, payment = _make_order_payment(
                sample_user, method, status, amount, collected, suffix=str(i)
            )
            created.append(payment)
        db.session.commit()

        python_ids = {p.id for p in created if has_open_receivable(p)}
        sql_ids = {
            row.id
            for row in Payment.query.filter(
                Payment.id.in_([p.id for p in created]),
                open_receivable_clause(),
            ).all()
        }
        ledger_ids = {p.id for p in created if is_ledger_receivable(p)}

        # The row-level Python mirror must agree with the SQL clause EXACTLY —
        # the allocator's current-order appends rely on it.
        assert ledger_ids == sql_ids

        # The ledger is a strict subset of what is owed at a door.
        assert sql_ids < python_ids

        # …and the difference is exactly the unpaid-electronic rows.
        by_id = {p.id: p for p in created}
        for pid in python_ids - sql_ids:
            p = by_id[pid]
            assert p.payment_method != PaymentMethod.CASH
            assert p.status != PaymentStatus.PARTIALLY_PAID

        # Sanity: the fixture set is not trivially all-or-nothing.
        assert 0 < len(sql_ids) < len(created)

    def test_sql_half_excludes_a_completed_prepayment_with_a_stale_column(
        self, app, db, sample_user
    ):
        """A COMPLETED Click row carrying a stale positive outstanding is NOT debt.

        Pinned independently by tests/unit/test_cod_cash_collection_service.py
        and tests/unit/test_order_detail_cod_surface_boundaries.py — a naive
        `outstanding_amount > 0` rewrite would resurrect every such row as
        phantom debt.
        """
        _order, payment = _make_order_payment(
            sample_user,
            PaymentMethod.CLICK,
            PaymentStatus.COMPLETED,
            90000,
            90000,
            suffix="stale",
        )
        payment.outstanding_amount = Decimal("90000.00")  # stale artefact
        db.session.commit()

        assert has_open_receivable(payment) is False
        found = Payment.query.filter(Payment.id == payment.id, open_receivable_clause()).all()
        assert found == []


@pytest.mark.unit
class TestLiveGatewayPaymentIsNotALedgerReceivable:
    """🔴 Adversarial-review finding, 2026-08-08.

    An unpaid electronic payment (PENDING/CANCELLED/FAILED) must NOT enter the
    debtor ledger or the allocation rings, even though real money is owed.

    Why: `Payment.__init__` seeds `outstanding_amount = amount - amount_collected`,
    so EVERY unpaid Click row carries a positive outstanding. A naive
    "outstanding > 0" clause therefore made a still-live Click order an
    allocation candidate, and an unrelated customer's cash could be absorbed by
    it — leaving the payer's own COD debt open. Worse, when the customer later
    paid the Click link, `PaymentService._sync_completed_prepayment_projection`
    forces `amount_collected = amount`, DESTROYING the cash allocation.

    Such orders are settled by `convert_electronic_order_to_cash` through an
    EXPLICIT target (the door flow or Record Personal Card Payment), never by a
    ring walk. Only the repriced-after-settlement shape (PARTIALLY_PAID) is a
    ledger receivable.
    """

    @pytest.mark.parametrize(
        "status",
        [PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.CANCELLED, PaymentStatus.FAILED],
    )
    def test_unpaid_electronic_is_not_in_the_ledger_clause(self, app, db, sample_user, status):
        _order, payment = _make_order_payment(
            sample_user, PaymentMethod.CLICK, status, 90000, 0, suffix=f"live-{status.value}"
        )
        db.session.commit()
        found = Payment.query.filter(Payment.id == payment.id, open_receivable_clause()).all()
        assert found == [], f"{status.value} Click must not be a ledger receivable"

    def test_partially_paid_electronic_IS_in_the_ledger_clause(self, app, db, sample_user):
        """The prod-961 shape — repriced after the card already settled."""
        _order, payment = _make_order_payment(
            sample_user, PaymentMethod.CLICK, PaymentStatus.PARTIALLY_PAID, 90000, 60000, suffix="repriced"
        )
        db.session.commit()
        found = Payment.query.filter(Payment.id == payment.id, open_receivable_clause()).all()
        assert [p.id for p in found] == [payment.id]

    @pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.PARTIALLY_PAID])
    def test_cash_is_unaffected_in_every_status(self, app, db, sample_user, status):
        _order, payment = _make_order_payment(
            sample_user, PaymentMethod.CASH, status, 30000, 0, suffix=f"cash-{status.value}"
        )
        db.session.commit()
        found = Payment.query.filter(Payment.id == payment.id, open_receivable_clause()).all()
        assert [p.id for p in found] == [payment.id]

    def test_python_half_still_reports_the_door_amount_for_an_unpaid_click(self):
        """The at-door prompt MUST still fire for an unsettled online order.

        The two halves answer different questions: `open_receivable_amount` is
        "how much is due at this door" (the driver collects and the order is
        converted); `open_receivable_clause` is "is this an open debt row in the
        ledger". This asymmetry is deliberate — see the docstrings.
        """
        p = _payment(PaymentMethod.CLICK, PaymentStatus.PENDING, 36000, 0)
        assert open_receivable_amount(p) == Decimal("36000.00")
