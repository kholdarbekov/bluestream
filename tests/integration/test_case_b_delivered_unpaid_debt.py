"""CASE B — delivered, no cash taken, order keeps the Click rail (policy 2026-08-24).

The customer took delivery and did not pay the driver. The order stays on the
Click rail with a live payable link, so the money can still arrive and the
receipt can still be issued. But until it does, the business is owed money and
must SEE it.

`open_receivable_clause()` deliberately excludes a PENDING electronic payment,
and that asymmetry is a money-safety guard (its docstring documents the bug:
widening it makes an unpaid Click order a ring-walk allocation candidate, it
absorbs an unrelated customer's cash, and the later Click payment then destroys
that allocation). So this debt becomes visible through a SEPARATE clause used
only by the display and cap surfaces — never by the allocator.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db
from business_app.models.order import Order
from business_app.models.payment import Payment
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus

from tests.integration.test_payment_matrix import _seed_click_payment


def _delivered_unpaid_click_order(db, user, *, order_number, payment_status=PaymentStatus.PENDING):
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("3000.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("18000.00"),
        payment_method=PaymentMethod.CLICK,
        is_paid=False,
    )
    db.session.add(order)
    db.session.flush()
    payment = _seed_click_payment(db, order, payment_id_str=f"pay-{order_number}")
    payment.status = payment_status
    db.session.commit()
    return order, payment


class TestCaseBCountsTowardTheDebtCap:
    @pytest.mark.parametrize("payment_status", [
        PaymentStatus.PENDING,
        PaymentStatus.CANCELLED,
        PaymentStatus.FAILED,
    ])
    def test_delivered_unpaid_click_order_counts_as_an_active_debt(
        self, app, db, sample_user, payment_status
    ):
        from business_app.services.cash_collection_service import CashCollectionService

        _delivered_unpaid_click_order(db, sample_user, order_number="CB-001",
                                      payment_status=payment_status)

        count = CashCollectionService().get_cluster_active_cod_debt_count(sample_user.id)

        assert count == 1, (
            f"a delivered unpaid Click order with a {payment_status.value} payment "
            "is money owed and must count toward the cap"
        )

    def test_two_such_orders_trip_the_cod_cap(self, app, db, sample_user):
        from business_app.services.cash_collection_service import CashCollectionService

        _delivered_unpaid_click_order(db, sample_user, order_number="CB-002")
        _delivered_unpaid_click_order(db, sample_user, order_number="CB-003")

        service = CashCollectionService()
        assert service.get_cluster_active_cod_debt_count(sample_user.id) == 2
        assert service.is_customer_cod_restricted(sample_user.id) is True

    def test_a_paid_delivered_click_order_is_not_a_debt(self, app, db, sample_user):
        from business_app.services.cash_collection_service import CashCollectionService

        order, payment = _delivered_unpaid_click_order(db, sample_user, order_number="CB-004")
        payment.status = PaymentStatus.COMPLETED
        order.is_paid = True
        db.session.commit()

        assert CashCollectionService().get_cluster_active_cod_debt_count(sample_user.id) == 0

    def test_a_live_undelivered_order_is_not_yet_a_debt(self, app, db, sample_user):
        """Nothing is owed until the goods are handed over."""
        from business_app.services.cash_collection_service import CashCollectionService

        order, _payment = _delivered_unpaid_click_order(db, sample_user, order_number="CB-005")
        order.status = OrderStatus.OUT_FOR_DELIVERY
        db.session.commit()

        assert CashCollectionService().get_cluster_active_cod_debt_count(sample_user.id) == 0


class TestCaseBIsVisibleInTheDebtorList:
    def test_admin_debtor_list_shows_the_delivered_unpaid_click_customer(
        self, client, app, db, sample_user, admin_user
    ):
        _delivered_unpaid_click_order(db, sample_user, order_number="CB-006")

        # The shared admin_auth_headers fixture is a 403 here: the decorator reads
        # the role CLAIM, and it also requires the identity to be a real ACTIVE user.
        with app.app_context():
            token = create_access_token(
                identity=str(admin_user.id), additional_claims={"role": "admin"}
            )

        resp = client.get(
            "/api/v1/admin/staff/cash-reconciliation/users/with-open-cod",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        payload = resp.get_json()
        rows = (payload.get("data") or {}).get("items", [])
        ids = {int(r["id"]) for r in rows}
        assert sample_user.id in ids, (
            "a delivered unpaid Click order must appear in the debtor list"
        )


class TestTheAllocatorIsDeliberatelyNotWidened:
    def test_case_b_debt_is_never_a_ring_walk_allocation_candidate(self, app, db, sample_user):
        """🔴 THE money-safety guard. If this ever passes a non-empty list, an
        unrelated customer's cash can be absorbed by an unpaid Click order and
        then destroyed when that Click payment completes."""
        from business_app.services.cash_collection_service import CashCollectionService

        _delivered_unpaid_click_order(db, sample_user, order_number="CB-007")

        service = CashCollectionService()
        candidates = service._active_cod_payments_query_for_users([sample_user.id]).all()

        assert candidates == [], (
            "open_receivable_clause() must stay narrow — the allocator must never "
            "see a PENDING electronic payment"
        )
