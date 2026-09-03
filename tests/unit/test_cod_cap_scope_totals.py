"""The cap's two scope totals, and the clause the cap counts on.

Two things are pinned here:

1. The cap's amount arm is NET of reserved prepayment. Quoting or gating
   against gross where a prepayment reservation exists is the documented cause
   of a prior production incident.
2. PRE-EXISTING BUG (a): the batch decision path counted debts with
   ``open_receivable_clause()`` while the single-user path counted them with
   ``unpaid_after_delivery_clause()``. A delivered-unpaid Click order is in the
   second set and not the first, so the two paths returned different answers
   for the same customer.
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import PaymentMethod, PaymentStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


@pytest.mark.unit
class TestClusterNetTotal:
    def test_sums_the_whole_cluster(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1, total=Decimal("6000.00"))
        delivered_cod_order(db, u2, total=Decimal("4000.00"))

        svc = CashCollectionService()
        assert svc.get_cluster_net_open_cod_debt_total(u1.id) == Decimal("10000.00")

    def test_subtracts_reserved_prepayment(self, db):
        """The whole point of NET: money already handed over is not still owed."""
        u = make_user(db)
        _order, payment = delivered_cod_order(db, u, total=Decimal("12000.00"))
        payment.provider_data = {"cod_prepayment_reserved_amount": "5000.00"}
        db.session.commit()

        svc = CashCollectionService()
        assert svc.get_cluster_net_open_cod_debt_total(u.id) == Decimal("7000.00")
        # ...while the DISPLAY/ceiling total is untouched and still gross.
        assert svc.get_cluster_open_cod_debt_total(u.id) == Decimal("12000.00")

    def test_counts_the_same_rows_the_count_arm_counts(self, db):
        """A delivered-unpaid Click order is a debt for BOTH cap arms."""
        u = make_user(db)
        delivered_cod_order(
            db,
            u,
            total=Decimal("18000.00"),
            payment_method=PaymentMethod.CLICK,
            payment_status=PaymentStatus.PENDING,
        )
        svc = CashCollectionService()
        assert svc.get_cluster_active_cod_debt_count(u.id) == 1
        assert svc.get_cluster_net_open_cod_debt_total(u.id) == Decimal("18000.00")

    def test_debt_free_cluster_is_zero(self, db):
        u = make_user(db)
        assert CashCollectionService().get_cluster_net_open_cod_debt_total(u.id) == Decimal("0.00")


@pytest.mark.unit
class TestPlaceNetTotal:
    def test_sums_every_member_address_of_the_group(self, db):
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        delivered_cod_order(db, u1, address=a1, total=Decimal("6000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("4000.00"))

        svc = CashCollectionService()
        # Keyed on an ADDRESS id, exactly like get_place_active_cod_debt_count.
        assert svc.get_place_net_open_cod_debt_total(a1.id) == Decimal("10000.00")
        assert svc.get_place_net_open_cod_debt_total(a2.id) == Decimal("10000.00")

    def test_ungrouped_address_degrades_to_that_address_alone(self, db):
        u = make_user(db)
        a = make_address(db, u)
        delivered_cod_order(db, u, address=a, total=Decimal("6000.00"))
        assert CashCollectionService().get_place_net_open_cod_debt_total(a.id) == Decimal("6000.00")


@pytest.mark.unit
class TestBatchAndSingleUserCountTheSameDebts:
    def test_delivered_unpaid_click_order_counts_on_both_paths(self, db):
        """BUG (a). Before this fix the batch path saw 1 debt and the single-user
        path saw 2 for this same customer, so the customer map and the checkout
        guard disagreed."""
        u = make_user(db)
        delivered_cod_order(db, u, total=Decimal("15000.00"))
        delivered_cod_order(
            db,
            u,
            total=Decimal("18000.00"),
            payment_method=PaymentMethod.CLICK,
            payment_status=PaymentStatus.PENDING,
        )

        svc = CashCollectionService()
        assert svc.get_cluster_active_cod_debt_count(u.id) == 2
        assert svc.get_cod_restricted_flags([u.id])[u.id] is svc.is_customer_cod_restricted(u.id)
        assert svc.get_cod_restricted_flags([u.id])[u.id] is True

    def test_batch_still_bounded_after_loading_rows(self, db, count_queries):
        """Loading the rows instead of counting them must not become an N+1."""
        users = [make_user(db) for _ in range(6)]
        for u in users:
            delivered_cod_order(db, u, total=Decimal("15000.00"))
        db.session.commit()
        user_ids = [u.id for u in users]

        svc = CashCollectionService()
        with count_queries() as counter:
            flags = svc.get_cod_restricted_flags(user_ids)

        assert set(flags) == set(user_ids)
        assert counter.count <= 4, f"expected a bounded batch, issued {counter.count} queries"
