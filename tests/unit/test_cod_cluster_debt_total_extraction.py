"""The cluster open-COD total is ONE named method, and the statement uses it.

``cluster_delivered_outstanding_amount`` is not a display nicety: it is the
collection ceiling ``cod_collect_ceiling.resolve_collect_scope`` hands a driver
(business_app/services/cod_collect_ceiling.py:205). This extraction must
therefore be behaviour-preserving down to the sum — it stays GROSS, on
``open_receivable_clause()``. The cap's own totals are a separate, NET pair.
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from tests.unit._scope_money_helpers import delivered_cod_order, link_users, make_user


@pytest.mark.unit
def test_cluster_total_sums_every_linked_account(db):
    u1, u2 = make_user(db), make_user(db)
    link_users(db, [u1, u2])
    delivered_cod_order(db, u1, total=Decimal("15000.00"))
    delivered_cod_order(db, u2, total=Decimal("7000.00"))

    svc = CashCollectionService()
    # One person, one credit line: both phones report the cluster's total.
    assert svc.get_cluster_open_cod_debt_total(u1.id) == Decimal("22000.00")
    assert svc.get_cluster_open_cod_debt_total(u2.id) == Decimal("22000.00")


@pytest.mark.unit
def test_statement_publishes_exactly_the_named_method(db):
    u = make_user(db)
    delivered_cod_order(db, u, total=Decimal("15000.00"))
    delivered_cod_order(db, u, total=Decimal("7000.00"))

    svc = CashCollectionService()
    statement = svc.get_customer_cod_statement(u.id)
    assert statement["cluster_delivered_outstanding_amount"] == float(
        svc.get_cluster_open_cod_debt_total(u.id)
    )
    assert statement["cluster_delivered_outstanding_amount"] == 22000.0


@pytest.mark.unit
def test_debt_free_cluster_total_is_zero(db):
    u = make_user(db)
    assert CashCollectionService().get_cluster_open_cod_debt_total(u.id) == Decimal("0.00")
    assert CashCollectionService().get_customer_cod_statement(u.id)[
        "cluster_delivered_outstanding_amount"
    ] == 0.0
