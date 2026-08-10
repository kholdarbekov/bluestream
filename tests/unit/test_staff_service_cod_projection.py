"""The at-door cash figure must be truthful on every payment rail.

Before this change `get_cod_collection_projection` early-returned the FULL order
total for any non-CASH order, so widening the bot's money block would have made
the driver collect the whole order instead of the unpaid delta.

Plan: docs/superpowers/plans/2026-08-08-open-receivable-ssot.md (Task 2 / risk R1)
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.staff_service import StaffService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _order_with_payment(db, user, method, status, total, collected):
    suffix = f"{method.value}-{status.value}"
    order = Order(
        user_id=user.id,
        order_number=f"ORD-PROJ-{suffix}",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal(str(total)),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(str(total)),
        payment_method=method,
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=method,
        amount=Decimal(str(total)),
        amount_collected=Decimal(str(collected)),
        outstanding_amount=Decimal(str(total - collected)),
        status=status,
        currency="UZS",
        payment_id=f"pay-proj-{suffix}",
        collected_by=(
            user.id if method == PaymentMethod.CASH and status == PaymentStatus.COMPLETED else None
        ),
    )
    db.session.add(payment)
    db.session.commit()
    return order


@pytest.mark.unit
class TestCodCollectionProjectionAcrossRails:
    def test_click_partially_paid_expects_only_the_delta(self, app, db, sample_user):
        """Prod order 961. The driver must be asked for 30,000 — not 90,000."""
        order = _order_with_payment(
            db, sample_user, PaymentMethod.CLICK, PaymentStatus.PARTIALLY_PAID, 90000, 60000
        )
        projection = StaffService.get_cod_collection_projection(order)
        assert projection["expected_cash_to_collect"] == 30000.0

    def test_click_completed_expects_nothing(self, app, db, sample_user):
        order = _order_with_payment(
            db, sample_user, PaymentMethod.CLICK, PaymentStatus.COMPLETED, 90000, 90000
        )
        projection = StaffService.get_cod_collection_projection(order)
        assert projection["expected_cash_to_collect"] == 0.0

    def test_click_pending_expects_the_full_amount(self, app, db, sample_user):
        """Unchanged behaviour: an unpaid online order is fully due at the door."""
        order = _order_with_payment(
            db, sample_user, PaymentMethod.CLICK, PaymentStatus.PENDING, 36000, 0
        )
        projection = StaffService.get_cod_collection_projection(order)
        assert projection["expected_cash_to_collect"] == 36000.0

    def test_click_cancelled_with_zeroed_column_still_expects_the_full_amount(
        self, app, db, sample_user
    ):
        """The gateway zeroes outstanding_amount on cancel; the money is still due."""
        order = _order_with_payment(
            db, sample_user, PaymentMethod.CLICK, PaymentStatus.CANCELLED, 45000, 0
        )
        order.payment.outstanding_amount = Decimal("0.00")
        db.session.commit()
        projection = StaffService.get_cod_collection_projection(order)
        assert projection["expected_cash_to_collect"] == 45000.0

    def test_cash_behaviour_is_unchanged(self, app, db, sample_user):
        order = _order_with_payment(
            db, sample_user, PaymentMethod.CASH, PaymentStatus.PENDING, 36000, 0
        )
        projection = StaffService.get_cod_collection_projection(order)
        assert projection["expected_cash_to_collect"] == 36000.0

    def test_order_without_payment_row_falls_back_to_total(self, app, db, sample_user):
        order = Order(
            user_id=sample_user.id,
            order_number="ORD-PROJ-NOPAY",
            status=OrderStatus.OUT_FOR_DELIVERY,
            subtotal=Decimal("21000.00"),
            delivery_fee=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            loyalty_discount=Decimal("0.00"),
            total_amount=Decimal("21000.00"),
            payment_method=PaymentMethod.CASH,
        )
        db.session.add(order)
        db.session.commit()
        projection = StaffService.get_cod_collection_projection(order)
        assert projection["expected_cash_to_collect"] == 21000.0

    def test_none_order_falls_back_to_zero(self, app):
        projection = StaffService.get_cod_collection_projection(None)
        assert projection["expected_cash_to_collect"] == 0.0
