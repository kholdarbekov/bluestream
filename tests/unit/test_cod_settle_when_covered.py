"""A COD order reads paid as soon as reserved balance + payments cover it.

Prod case: TG_000620_26 — a 35,460 order carrying 540 of reserved balance was
paid by a 36,000 card transfer, yet read ``partially_paid`` until delivery
because the reservation was only consumed by the driver marking it delivered.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus


def _order(db, user, *, number, total):
    order = Order(
        user_id=user.id,
        order_number=number,
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


def _credit(db, customer, amount, *, collector=None, source="standalone_meeting"):
    """Unapplied customer balance, optionally collected by ``collector``."""
    event = CashCollectionEvent(
        customer_id=customer.id,
        collector_user_id=collector.id if collector else None,
        recorded_by_user_id=collector.id if collector else None,
        amount=Decimal(amount),
        currency="UZS",
        source=source,
        occurred_at=datetime.now(UTC),
        notes="seeded credit",
        unapplied_amount=Decimal(amount),
    )
    db.session.add(event)
    db.session.flush()
    return event


def _reserved_order(db, service, user, *, number, total, credit, collector):
    """A pending CASH order holding a reservation of the customer's balance."""
    _credit(db, user, credit, collector=collector)
    order = _order(db, user, number=number, total=total)
    payment = service.ensure_cod_payment_for_order(order)
    db.session.flush()
    service.reserve_customer_prepaid_credit_for_payment(payment, actor_user_id=user.id)
    db.session.flush()
    # Creation and the later transfer are separate requests in prod.
    db.session.expire_all()
    return order, payment


def _card_transfer(db, service, user, admin, order, amount):
    service.post_collection(
        customer_id=user.id,
        amount=Decimal(amount),
        source="personal_card_transfer",
        recorded_by_user_id=admin.id,
        order_id=order.id,
        notes="karta",
        commit=False,
    )
    db.session.flush()


@pytest.mark.unit
@pytest.mark.payment
class TestCardTransferCoveringTheNetSettlesNow:
    def test_prod_case_reads_paid_right_after_the_transfer(
        self, app, db, sample_user, admin_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order, payment = _reserved_order(
                db, service, sample_user,
                number="SWC-PROD", total="35460.00", credit="540.00", collector=delivery_driver,
            )

            _card_transfer(db, service, sample_user, admin_user, order, "36000.00")
            db.session.refresh(payment)
            db.session.refresh(order)

            assert payment.status == PaymentStatus.COMPLETED
            assert payment.amount_collected == Decimal("35460.00")
            assert payment.outstanding_amount == Decimal("0.00")
            assert order.is_paid is True
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 0.0
            # 540 balance + 36,000 card − 35,460 order.
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("1080.00")
            credit_row = CashCollectionAllocation.query.filter_by(
                payment_id=payment.id, allocation_mode="prepaid_credit", reversed_at=None
            ).one()
            assert credit_row.allocated_amount == Decimal("540.00")
            assert credit_row.allocation_metadata.get("settled_pre_delivery") is True

    def test_partial_transfer_keeps_the_reservation(
        self, app, db, sample_user, admin_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order, payment = _reserved_order(
                db, service, sample_user,
                number="SWC-PART", total="35460.00", credit="540.00", collector=delivery_driver,
            )

            _card_transfer(db, service, sample_user, admin_user, order, "20000.00")
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.amount_collected == Decimal("20000.00")
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 540.0

    def test_cancel_after_settlement_returns_the_balance_slice(
        self, app, db, sample_user, admin_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order, payment = _reserved_order(
                db, service, sample_user,
                number="SWC-CANCEL", total="35460.00", credit="540.00", collector=delivery_driver,
            )
            _card_transfer(db, service, sample_user, admin_user, order, "36000.00")

            order = Order.query.get(order.id)
            order.status = OrderStatus.CANCELLED
            db.session.flush()
            refunded = service.release_pre_delivery_prepaid_settlement_for_order(
                order.id, actor_user_id=admin_user.id
            )
            db.session.flush()
            db.session.refresh(payment)

            assert refunded == Decimal("540.00")
            assert payment.amount_collected == Decimal("34920.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("1620.00")


@pytest.mark.unit
@pytest.mark.payment
class TestSweepAndGuards:
    def test_sweep_that_fully_covers_a_pending_order_settles_it(
        self, app, db, sample_user, admin_user
    ):
        with app.app_context():
            service = CashCollectionService()
            order = _order(db, sample_user, number="SWC-SWEEP", total="30000.00")
            payment = service.ensure_cod_payment_for_order(order)
            event = _credit(db, sample_user, "50000.00", source="admin_adjustment")
            event.recorded_by_user_id = admin_user.id
            db.session.flush()

            service.auto_reserve_against_pending_payments(sample_user.id, actor_user_id=admin_user.id)
            db.session.flush()
            db.session.refresh(payment)

            assert payment.status == PaymentStatus.COMPLETED
            assert payment.amount_collected == Decimal("30000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("20000.00")

    def test_no_collector_anywhere_leaves_the_reservation(self, app, db, sample_user):
        with app.app_context():
            service = CashCollectionService()
            _credit(db, sample_user, "50000.00", collector=None, source="admin_adjustment")
            order = _order(db, sample_user, number="SWC-NOCOLL", total="30000.00")
            payment = service.ensure_cod_payment_for_order(order)
            db.session.flush()
            service.reserve_customer_prepaid_credit_for_payment(payment)
            db.session.flush()

            settled = service.settle_reserved_prepayment_if_covered(payment, actor_user_id=None)

            assert settled == Decimal("0.00")
            assert payment.status == PaymentStatus.PENDING
            assert payment.provider_data.get("cod_prepayment_reserved_amount") == 30000.0

    def test_delivered_order_is_left_to_the_delivery_path(
        self, app, db, sample_user, delivery_driver
    ):
        with app.app_context():
            service = CashCollectionService()
            order, payment = _reserved_order(
                db, service, sample_user,
                number="SWC-DELIV", total="30000.00", credit="50000.00", collector=delivery_driver,
            )
            order = Order.query.get(order.id)
            order.status = OrderStatus.DELIVERED
            db.session.flush()

            assert service.settle_reserved_prepayment_if_covered(payment) == Decimal("0.00")
