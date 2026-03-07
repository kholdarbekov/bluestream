"""Unit tests for COD receivables, cash collection, and driver reconciliation services."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import DriverCashSession, Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.utils.constants import (
    DeliveryStatus,
    DriverCashSessionStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password


@pytest.fixture
def delivery_driver_profile(db, delivery_driver):
    profile = DeliveryPerson(
        user_id=delivery_driver.id,
        full_name="Delivery Driver",
        phone=delivery_driver.phone,
        email=delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def second_delivery_driver(db):
    user = User(
        email='driver.two@example.com',
        phone='+998901234579',
        password_hash=hash_password('DriverTwoPassword123!'),
        first_name='Delivery',
        last_name='Driver Two',
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def second_delivery_driver_profile(db, second_delivery_driver):
    profile = DeliveryPerson(
        user_id=second_delivery_driver.id,
        full_name="Delivery Driver Two",
        phone=second_delivery_driver.phone,
        email=second_delivery_driver.email,
        is_active=True,
        is_available=True,
    )
    db.session.add(profile)
    db.session.commit()
    return profile


@pytest.fixture
def cod_order(db, sample_order):
    sample_order.status = OrderStatus.DELIVERED
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.delivered_at = datetime.now(UTC)
    db.session.commit()
    return sample_order


@pytest.fixture
def cod_delivery(db, cod_order, delivery_driver):
    delivery = Delivery(
        order_id=cod_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.DELIVERED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
        actual_delivery_time=datetime.now(UTC),
        delivered_at=datetime.now(UTC),
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


@pytest.mark.unit
class TestCashCollectionService:
    def test_ensure_cod_payment_creates_canonical_payment(self, app, db, cod_order):
        with app.app_context():
            service = CashCollectionService()

            payment = service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()
            db.session.refresh(payment)

            assert payment.order_id == cod_order.id
            assert payment.payment_method == PaymentMethod.CASH
            assert payment.status == PaymentStatus.PENDING
            assert payment.amount_collected == Decimal("0.00")
            assert payment.outstanding_amount == cod_order.total_amount

    def test_active_cod_for_update_query_locks_without_outer_join(self, app, sample_user):
        with app.app_context():
            service = CashCollectionService()
            query = service._active_cod_payments_query(sample_user.id).with_for_update(of=Payment)
            sql = str(query.statement.compile(dialect=postgresql.dialect())).upper()

            assert "LEFT OUTER JOIN" not in sql
            assert "FOR UPDATE OF" in sql
            assert "PAYMENTS" in sql

    def test_post_collection_marks_cash_payment_partially_paid(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            service = CashCollectionService()
            payment = service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("5000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Customer paid part of the balance on delivery",
            )

            db.session.refresh(payment)
            fresh_order = Order.query.get(cod_order.id)
            fresh_delivery = Delivery.query.get(cod_delivery.id)

            assert event.driver_cash_session_id is not None
            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.amount_collected == Decimal("5000.00")
            assert payment.outstanding_amount == Decimal("13000.00")
            assert fresh_order.is_paid is False
            assert fresh_delivery.cash_collected == Decimal("5000.00")

            session = DriverCashSession.query.get(event.driver_cash_session_id)
            assert session is not None
            assert session.expected_cash == Decimal("5000.00")

    def test_customer_reaches_cod_debt_cap_with_two_open_delivered_cod_orders(
        self,
        app,
        db,
        sample_user,
        sample_product,
        cod_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)

            second_order = Order(
                user_id=sample_user.id,
                order_number="ORD-TEST-002",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("15000.00"),
                delivery_fee=Decimal("3000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("18000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(second_order)
            db.session.flush()
            service.ensure_cod_payment_for_order(second_order)
            db.session.commit()

            assert service.get_active_cod_debt_count(sample_user.id) == 2
            assert service.is_customer_cod_restricted(sample_user.id) is True
            with pytest.raises(ValidationError):
                service.validate_customer_can_use_cod(sample_user.id)

    def test_cross_driver_can_collect_old_cod_debt_and_session_attaches_to_collector(
        self,
        app,
        db,
        sample_user,
        cod_order,
        cod_delivery,
        second_delivery_driver,
        second_delivery_driver_profile,
    ):
        with app.app_context():
            service = CashCollectionService()
            payment = service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("7000.00"),
                source="standalone_meeting",
                collector_user_id=second_delivery_driver.id,
                recorded_by_user_id=second_delivery_driver.id,
                notes="Collected old COD debt during a later street meeting.",
            )

            db.session.refresh(payment)
            assert event.delivery_id is None
            assert event.driver_cash_session_id is not None
            assert payment.amount_collected == Decimal("7000.00")
            assert payment.outstanding_amount == Decimal("11000.00")

            session = DriverCashSession.query.get(event.driver_cash_session_id)
            assert session is not None
            assert session.driver_user_id == second_delivery_driver.id
            assert session.expected_cash == Decimal("7000.00")

    def test_blocked_driver_cannot_record_new_standalone_cod_collections(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            cash_service = CashCollectionService()
            cash_service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            cash_service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("4000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Initial partial collection on delivery.",
            )

            recon_service = DriverReconciliationService()
            session = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("3000.00"),
                notes="Mismatch created for blocking test.",
                submitted_by_user_id=delivery_driver.id,
            )
            assert session.blocked_from_cod is True

            with pytest.raises(ValidationError, match="blocked from new cash on delivery collections"):
                cash_service.post_collection(
                    customer_id=sample_user.id,
                    amount=Decimal("1000.00"),
                    source="standalone_meeting",
                    collector_user_id=delivery_driver.id,
                    recorded_by_user_id=delivery_driver.id,
                    notes="Attempted late collection while blocked.",
                )


@pytest.mark.unit
class TestDriverReconciliationService:
    def test_mismatch_submission_blocks_driver_and_appears_in_report(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        with app.app_context():
            cash_service = CashCollectionService()
            payment = cash_service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            cash_service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Driver collected most of the balance on delivery",
            )

            db.session.refresh(payment)
            recon_service = DriverReconciliationService()
            submitted = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("9000.00"),
                notes="Short by 1000 during handoff",
                submitted_by_user_id=delivery_driver.id,
            )

            assert submitted.status == DriverCashSessionStatus.MISMATCH
            assert submitted.blocked_from_cod is True
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is True

            report = recon_service.get_report(period="day", driver_user_id=delivery_driver.id)
            assert report["summary"]["blocked_session_count"] == 1
            assert report["summary"]["mismatch_session_count"] == 1
            assert report["report"][0]["blocked_session_count"] == 1

            resolved = recon_service.resolve_session(
                session_id=submitted.id,
                actor_user_id=admin_user.id,
                resolution_notes="Admin verified the shortage and accepted adjustment.",
                verified_cash=Decimal("9000.00"),
            )

            assert resolved.status == DriverCashSessionStatus.RESOLVED
            assert resolved.blocked_from_cod is False
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is False
