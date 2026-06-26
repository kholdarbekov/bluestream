"""Unit tests for immediate settlement of a new COD order from prepaid credit.

When a customer carries a COD prepaid balance (unapplied over-collection) and a
new CASH order is created that the balance FULLY covers, the order must be
settled (marked paid) at creation time instead of merely reserving the credit
until delivery. Partial coverage stays a reservation. Cancelling/returning a
not-yet-delivered order that was settled this way must refund the consumed
credit back to the customer's prepaid balance.

Covers:
  - ``CashCollectionService.settle_new_cod_order_from_prepaid``
  - ``CashCollectionService.release_pre_delivery_prepaid_settlement_for_order``
  - the ``settled_pre_delivery`` tag added to
    ``consume_reserved_prepayment_for_payment``
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)


def _make_pending_cash_order(db, user, *, order_number: str, total: str) -> Order:
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    return order


def _seed_prepaid(db, customer, collector, amount: str) -> CashCollectionEvent:
    """Seed an unapplied COD prepayment surplus collected by ``collector``."""
    event = CashCollectionEvent(
        customer_id=customer.id,
        collector_user_id=collector.id,
        recorded_by_user_id=collector.id,
        amount=Decimal(amount),
        currency="UZS",
        source="standalone_meeting",
        occurred_at=datetime.now(UTC),
        notes="Seeded prepayment surplus",
        unapplied_amount=Decimal(amount),
    )
    db.session.add(event)
    db.session.flush()
    return event


@pytest.mark.unit
class TestSettleNewCodOrderFromPrepaid:
    def test_full_coverage_marks_order_paid_at_creation(
        self, app, db, sample_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            event = _seed_prepaid(db, sample_user, delivery_driver, "245000.00")

            order = _make_pending_cash_order(
                db, sample_user, order_number="ORD-SETTLE-FULL", total="90000.00"
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            # Reservation happens first (as the order-creation path does today).
            service.reserve_customer_prepaid_credit_for_payment(
                payment, actor_user_id=sample_user.id
            )
            db.session.flush()

            settled = service.settle_new_cod_order_from_prepaid(
                payment, actor_user_id=sample_user.id
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(order)
            db.session.refresh(event)

            assert settled == Decimal("90000.00")
            # Payment is fully settled and order marked paid.
            assert payment.amount_collected == Decimal("90000.00")
            assert payment.outstanding_amount == Decimal("0.00")
            assert payment.status == PaymentStatus.COMPLETED
            assert order.is_paid is True
            # The collector is carried from the prepayment's source event.
            assert payment.collected_by == delivery_driver.id
            # The reservation marker is cleared (it became an application).
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 0.0
            # Remaining prepaid balance is 245k - 90k = 155k.
            assert event.unapplied_amount == Decimal("155000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "155000.00"
            )

            # The ledger now carries an APPLIED prepaid_credit allocation,
            # tagged as a pre-delivery settlement so cancellation can refund it.
            alloc = CashCollectionAllocation.query.filter_by(
                payment_id=payment.id, reversed_at=None
            ).one()
            assert alloc.allocation_mode == "prepaid_credit"
            assert alloc.allocation_metadata.get("settled_pre_delivery") is True
            assert alloc.allocation_metadata.get("affects_payment_projection") is True

    def test_partial_coverage_stays_reserved_and_unpaid(
        self, app, db, sample_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            _seed_prepaid(db, sample_user, delivery_driver, "50000.00")

            order = _make_pending_cash_order(
                db, sample_user, order_number="ORD-SETTLE-PART", total="90000.00"
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()

            service.reserve_customer_prepaid_credit_for_payment(
                payment, actor_user_id=sample_user.id
            )
            db.session.flush()

            settled = service.settle_new_cod_order_from_prepaid(
                payment, actor_user_id=sample_user.id
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(order)

            # Not fully covered -> nothing consumed, stays a reservation.
            assert settled == Decimal("0.00")
            assert payment.amount_collected == Decimal("0.00")
            assert payment.outstanding_amount == Decimal("90000.00")
            assert payment.status == PaymentStatus.PENDING
            assert order.is_paid is False
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 50000.0
            alloc = CashCollectionAllocation.query.filter_by(
                payment_id=payment.id, reversed_at=None
            ).one()
            assert alloc.allocation_mode == "prepaid_reservation"


@pytest.mark.unit
class TestReleasePreDeliveryPrepaidSettlement:
    def test_cancel_refunds_pre_delivery_settlement(
        self, app, db, sample_user, admin_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            event = _seed_prepaid(db, sample_user, delivery_driver, "245000.00")

            order = _make_pending_cash_order(
                db, sample_user, order_number="ORD-SETTLE-CANCEL", total="90000.00"
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            service.reserve_customer_prepaid_credit_for_payment(
                payment, actor_user_id=sample_user.id
            )
            db.session.flush()
            service.settle_new_cod_order_from_prepaid(payment, actor_user_id=sample_user.id)
            db.session.flush()
            db.session.refresh(payment)
            assert payment.status == PaymentStatus.COMPLETED

            # Customer cancels before delivery.
            order.status = OrderStatus.CANCELLED
            db.session.flush()
            released = service.release_pre_delivery_prepaid_settlement_for_order(
                order.id,
                actor_user_id=admin_user.id,
                reason="Order cancelled before delivery",
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(order)
            db.session.refresh(event)

            assert released == Decimal("90000.00")
            # The credit is fully restored to the customer's prepaid balance.
            assert event.unapplied_amount == Decimal("245000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal(
                "245000.00"
            )
            # The payment is no longer collected/paid.
            assert payment.amount_collected == Decimal("0.00")
            assert payment.outstanding_amount == Decimal("90000.00")
            assert payment.status == PaymentStatus.PENDING
            assert order.is_paid is False
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 0.0

    def test_release_skips_delivered_order(
        self, app, db, sample_user, delivery_driver
    ):
        """A pre-delivery settlement that was actually delivered must NOT be
        refunded by the cancel path — the goods were received."""
        with app.app_context():
            service = CashCollectionService()
            event = _seed_prepaid(db, sample_user, delivery_driver, "245000.00")

            order = _make_pending_cash_order(
                db, sample_user, order_number="ORD-SETTLE-DELIV", total="90000.00"
            )
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            service.reserve_customer_prepaid_credit_for_payment(
                payment, actor_user_id=sample_user.id
            )
            db.session.flush()
            service.settle_new_cod_order_from_prepaid(payment, actor_user_id=sample_user.id)
            db.session.flush()

            # The order is delivered.
            delivery = Delivery(
                order_id=order.id,
                status=DeliveryStatus.DELIVERED,
                scheduled_date=datetime.now(UTC),
                scheduled_time_slot="09:00-12:00",
                delivered_at=datetime.now(UTC),
            )
            db.session.add(delivery)
            order.status = OrderStatus.DELIVERED
            db.session.flush()

            released = service.release_pre_delivery_prepaid_settlement_for_order(
                order.id, reason="should be a no-op"
            )
            db.session.flush()
            db.session.refresh(payment)
            db.session.refresh(event)

            assert released == Decimal("0.00")
            # Nothing refunded: payment still paid, balance still drawn down.
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.amount_collected == Decimal("90000.00")
            assert event.unapplied_amount == Decimal("155000.00")
