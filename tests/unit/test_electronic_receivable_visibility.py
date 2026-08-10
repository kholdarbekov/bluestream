"""A delivered electronic order that still owes money must be visible as debt.

Prod order 961 (2026-08-07): 2 bottles paid by Click, a 3rd added by an admin
edit 46 seconds before the driver submitted the delivered status. The payment
row was re-priced correctly (amount=3b, collected=2b, PARTIALLY_PAID) but every
reader filtered `payment_method == CASH`, so the receivable was invisible on
every surface at once.

Plan: docs/superpowers/plans/2026-08-08-open-receivable-ssot.md (Task 3 / Task 4)
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import PaymentMethod, PaymentStatus

from ._scope_money_helpers import delivered_cod_order


@pytest.fixture
def click_receivable(app, db, sample_user):
    """90,000 order, 60,000 paid by Click, 30,000 still owed."""
    return delivered_cod_order(
        db,
        sample_user,
        total=Decimal("90000.00"),
        outstanding=Decimal("30000.00"),
        payment_method=PaymentMethod.CLICK,
        payment_status=PaymentStatus.PARTIALLY_PAID,
    )


@pytest.fixture
def click_settled(app, db, sample_user):
    """Fully-paid Click order carrying a STALE positive outstanding column."""
    order, payment = delivered_cod_order(
        db,
        sample_user,
        total=Decimal("50000.00"),
        outstanding=Decimal("0.00"),
        payment_method=PaymentMethod.CLICK,
        payment_status=PaymentStatus.COMPLETED,
    )
    payment.outstanding_amount = Decimal("50000.00")  # stale artefact
    db.session.commit()
    return order, payment


@pytest.mark.unit
class TestElectronicReceivableIsVisible:
    def test_appears_in_the_debtor_list(self, click_receivable, sample_user):
        rows = CashCollectionService().list_users_with_open_cod_debts(limit=50)
        assert sample_user.id in {row["id"] for row in rows}

    def test_debtor_row_reports_the_delta_not_the_total(self, click_receivable, sample_user):
        rows = CashCollectionService().list_users_with_open_cod_debts(limit=50)
        row = next(r for r in rows if r["id"] == sample_user.id)
        assert row["total_outstanding_amount"] == 30000.0

    def test_counts_toward_the_cod_debt_limit(self, click_receivable, sample_user):
        """Owner decision 2026-08-08: an electronic receivable DOES cap."""
        assert CashCollectionService().get_active_cod_debt_count(sample_user.id) == 1

    def test_appears_in_the_cluster_delivered_outstanding_figure(
        self, click_receivable, sample_user
    ):
        statement = CashCollectionService().get_customer_cod_statement(sample_user.id)
        assert statement["cluster_delivered_outstanding_amount"] == 30000.0

    def test_is_an_allocation_candidate_for_its_own_customer(self, click_receivable, sample_user):
        """Ring-2 read: the debt must be reachable by the collection engine."""
        payments = CashCollectionService().get_active_cod_payments_for_customer(sample_user.id)
        assert [p.payment_method for p in payments] == [PaymentMethod.CLICK]


@pytest.mark.unit
class TestSettledElectronicIsNotDebt:
    def test_stale_outstanding_does_not_create_phantom_debt(self, click_settled, sample_user):
        rows = CashCollectionService().list_users_with_open_cod_debts(limit=50)
        assert sample_user.id not in {row["id"] for row in rows}

    def test_stale_outstanding_does_not_count_toward_the_cap(self, click_settled, sample_user):
        assert CashCollectionService().get_active_cod_debt_count(sample_user.id) == 0

    def test_stale_outstanding_is_not_an_allocation_candidate(self, click_settled, sample_user):
        payments = CashCollectionService().get_active_cod_payments_for_customer(sample_user.id)
        assert payments == []


@pytest.mark.unit
class TestCustomerStatementShape:
    def test_statement_lists_electronic_payments(self, click_receivable, sample_user):
        """Owner decision 2026-08-08: full history for BOTH rails."""
        statement = CashCollectionService().get_customer_cod_statement(sample_user.id)
        methods = {item.get("payment_method") for item in statement["items"]}
        assert PaymentMethod.CLICK.value in methods

    def test_statement_lists_a_settled_electronic_payment_too(self, click_settled, sample_user):
        statement = CashCollectionService().get_customer_cod_statement(sample_user.id)
        assert len(statement["items"]) == 1
        assert statement["items"][0]["payment_method"] == PaymentMethod.CLICK.value
