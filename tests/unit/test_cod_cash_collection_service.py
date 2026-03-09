"""Unit tests for COD receivables, cash collection, and driver reconciliation services."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import DriverCashSession, Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_cash_custody_service import DriverCashCustodyService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.staff_service import StaffService
from business_app.utils.constants import (
    DeliveryStatus,
    DriverCashSessionStatus,
    NotificationChannel,
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

    def test_get_order_payment_timeline_normalizes_completed_prepaid_projection(
        self,
        app,
        db,
        sample_order,
    ):
        with app.app_context():
            sample_order.payment_method = PaymentMethod.CARD
            payment = Payment(
                order_id=sample_order.id,
                user_id=sample_order.user_id,
                payment_method=PaymentMethod.PAYME,
                amount=sample_order.total_amount,
                currency="UZS",
                status=PaymentStatus.COMPLETED,
                amount_collected=Decimal("0.00"),
                outstanding_amount=sample_order.total_amount,
                payment_id="payme_projection_test_1",
            )
            db.session.add(payment)
            db.session.commit()

            timeline = CashCollectionService().get_order_payment_timeline(sample_order.id)

            assert timeline["amount_collected"] == float(sample_order.total_amount)
            assert timeline["outstanding_amount"] == 0.0
            assert timeline["timeline"][0]["amount_collected"] == float(sample_order.total_amount)
            assert timeline["timeline"][0]["outstanding_amount"] == 0.0

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

    def test_cod_collection_search_includes_staff_user_with_open_cod_debt(
        self,
        app,
        db,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()
            staff_cod_order = Order(
                user_id=admin_user.id,
                order_number="ORD-ADMIN-COD-001",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("10000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("10000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(staff_cod_order)
            db.session.flush()
            service.ensure_cod_payment_for_order(staff_cod_order)
            db.session.commit()

            items = StaffService.search_customers_for_cod_collection(
                admin_user.phone,
                search_type='phone',
                only_with_open_cod=True,
            )

            assert any(item['id'] == admin_user.id for item in items)

    def test_cod_collection_search_accepts_single_digit_user_id_query(
        self,
        app,
        db,
        admin_user,
    ):
        with app.app_context():
            service = CashCollectionService()
            staff_cod_order = Order(
                user_id=admin_user.id,
                order_number="ORD-ADMIN-COD-002",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("9000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("9000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(staff_cod_order)
            db.session.flush()
            service.ensure_cod_payment_for_order(staff_cod_order)
            db.session.commit()

            items = StaffService.search_customers_for_cod_collection(
                str(admin_user.id),
                search_type='phone',
                only_with_open_cod=True,
            )

            assert len(items) == 1
            assert items[0]['id'] == admin_user.id

    def test_list_users_with_open_cod_debts_includes_staff_and_customers(
        self,
        app,
        db,
        sample_user,
        admin_user,
        cod_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)

            staff_cod_order = Order(
                user_id=admin_user.id,
                order_number="ORD-ADMIN-COD-003",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("7000.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("7000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(staff_cod_order)
            db.session.flush()
            service.ensure_cod_payment_for_order(staff_cod_order)
            db.session.commit()

            items = service.list_users_with_open_cod_debts(limit=50)
            user_ids = {item['id'] for item in items}

            assert sample_user.id in user_ids
            assert admin_user.id in user_ids

    def test_prepayment_balance_is_applied_to_next_cod_payment(
        self,
        app,
        db,
        sample_user,
        admin_user,
        cod_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            existing_payment = service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("30000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin_user.id,
                order_id=cod_order.id,
                notes="Collected old COD plus extra cash kept as prepayment.",
            )
            db.session.refresh(existing_payment)

            assert existing_payment.outstanding_amount == Decimal("0.00")
            assert event.unapplied_amount > Decimal("0.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("12000.00")

            next_order = Order(
                user_id=sample_user.id,
                order_number="ORD-TEST-PREPAY-001",
                status=OrderStatus.PENDING,
                subtotal=Decimal("14000.00"),
                delivery_fee=Decimal("3000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("17000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(next_order)
            db.session.flush()
            next_payment = service.ensure_cod_payment_for_order(next_order)

            assert next_payment.amount_collected == Decimal("0.00")
            assert next_payment.outstanding_amount == Decimal("17000.00")

            service.apply_customer_prepaid_credit_to_payment(next_payment)
            db.session.commit()
            db.session.refresh(next_payment)

            assert next_payment.amount_collected == Decimal("12000.00")
            assert next_payment.outstanding_amount == Decimal("5000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")

    def test_prepayment_reservation_blocks_reuse_on_next_pending_cod_order(
        self,
        app,
        db,
        sample_user,
        admin_user,
        cod_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("33000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin_user.id,
                order_id=cod_order.id,
                notes="Over-collected to create COD prepaid credit.",
            )
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("15000.00")

            first_pending_order = Order(
                user_id=sample_user.id,
                order_number="ORD-PREPAY-RES-001",
                status=OrderStatus.PENDING,
                subtotal=Decimal("85000.00"),
                delivery_fee=Decimal("5000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("90000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(first_pending_order)
            db.session.flush()
            first_payment = service.ensure_cod_payment_for_order(first_pending_order)

            reserved = service.reserve_customer_prepaid_credit_for_payment(
                first_payment,
                actor_user_id=admin_user.id,
            )
            db.session.flush()

            assert reserved == Decimal("15000.00")
            assert first_payment.amount_collected == Decimal("0.00")
            assert first_payment.outstanding_amount == Decimal("90000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")

            second_pending_order = Order(
                user_id=sample_user.id,
                order_number="ORD-PREPAY-RES-002",
                status=OrderStatus.PENDING,
                subtotal=Decimal("45000.00"),
                delivery_fee=Decimal("5000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("50000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(second_pending_order)
            db.session.flush()
            second_payment = service.ensure_cod_payment_for_order(second_pending_order)
            second_reserved = service.reserve_customer_prepaid_credit_for_payment(second_payment)

            assert second_reserved == Decimal("0.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")

    def test_reserved_prepayment_is_released_on_non_delivered_order_cancellation(
        self,
        app,
        db,
        sample_user,
        admin_user,
        cod_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("30000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin_user.id,
                order_id=cod_order.id,
                notes="Create prepaid reserve source",
            )
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("12000.00")

            pending_order = Order(
                user_id=sample_user.id,
                order_number="ORD-PREPAY-REL-001",
                status=OrderStatus.PENDING,
                subtotal=Decimal("27000.00"),
                delivery_fee=Decimal("3000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("30000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(pending_order)
            db.session.flush()
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            service.reserve_customer_prepaid_credit_for_payment(pending_payment)
            db.session.flush()

            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("0.00")

            released = service.release_reserved_prepayment_for_order(
                pending_order.id,
                actor_user_id=admin_user.id,
                reason="Order cancelled before delivery",
            )
            db.session.flush()

            assert released == Decimal("12000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("12000.00")

    def test_reserved_prepayment_is_consumed_on_delivery_settlement(
        self,
        app,
        db,
        sample_user,
        admin_user,
        cod_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("30000.00"),
                source="admin_adjustment",
                recorded_by_user_id=admin_user.id,
                order_id=cod_order.id,
                notes="Create prepaid reserve source",
            )

            pending_order = Order(
                user_id=sample_user.id,
                order_number="ORD-PREPAY-CNS-001",
                status=OrderStatus.PENDING,
                subtotal=Decimal("14000.00"),
                delivery_fee=Decimal("3000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("17000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(pending_order)
            db.session.flush()
            pending_payment = service.ensure_cod_payment_for_order(pending_order)
            reserved = service.reserve_customer_prepaid_credit_for_payment(pending_payment)
            assert reserved == Decimal("12000.00")

            consumed = service.consume_reserved_prepayment_for_payment(pending_payment)
            db.session.flush()

            assert consumed == Decimal("12000.00")
            assert pending_payment.status == PaymentStatus.PARTIALLY_PAID
            assert pending_payment.amount_collected == Decimal("12000.00")
            assert pending_payment.outstanding_amount == Decimal("5000.00")
            assert pending_payment.provider_data.get("cod_prepayment_reserved_amount") == 0.0

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
                reason_code='manager_approved_adjustment',
                resolution_notes="Admin verified the shortage and accepted adjustment.",
                verified_cash=Decimal("9000.00"),
            )

            assert resolved.status == DriverCashSessionStatus.RESOLVED
            assert resolved.blocked_from_cod is False
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is False

    def test_submit_defaults_to_expected_on_hand_after_checkpoint_transfer(
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
            cash_service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            cash_service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collected and moved cash to checkpoint.",
            )

            recon_service = DriverReconciliationService()
            session = recon_service.get_open_session_for_driver(delivery_driver.id)

            custody_service = DriverCashCustodyService()
            transfer = custody_service.create_transfer(
                session_id=session.id,
                driver_user_id=delivery_driver.id,
                declared_transfer_cash=Decimal("6000.00"),
                notes="Handoff before end of shift",
            )
            custody_service.confirm_transfer(
                transfer_id=transfer.id,
                actor_user_id=admin_user.id,
                counted_transfer_cash=Decimal("6000.00"),
                reason_code='cash_count_matched',
            )

            submitted = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=None,
                notes="Submitting with default expected-on-hand amount",
            )

            assert submitted.expected_cash == Decimal("10000.00")
            assert submitted.transferred_cash_total == Decimal("6000.00")
            assert submitted.expected_cash_on_hand == Decimal("4000.00")
            assert submitted.declared_cash == Decimal("4000.00")
            assert submitted.declared_variance == Decimal("0.00")

    def test_verify_requires_reason_code(
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
                amount=Decimal("5000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collection before verify reason-code test.",
            )

            recon_service = DriverReconciliationService()
            submitted = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("5000.00"),
            )

            with pytest.raises(ValidationError, match="reason_code"):
                recon_service.verify_session(
                    session_id=submitted.id,
                    verified_cash=Decimal("5000.00"),
                    actor_user_id=delivery_driver.id,
                    reason_code='invalid_reason',
                )

    def test_mark_overdue_uses_submission_due_at(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            recon_service = DriverReconciliationService()
            session = recon_service.get_open_session_for_driver(delivery_driver.id)
            session.submission_due_at = datetime.now(UTC) - timedelta(minutes=5)
            db.session.commit()

            updated = recon_service.mark_overdue_sessions(reference_time=datetime.now(UTC))
            db.session.refresh(session)

            assert updated >= 1
            assert session.status == DriverCashSessionStatus.OVERDUE
            assert session.blocked_from_cod is True

    def test_reconciliation_reminder_uses_staff_bot_and_keeps_customer_telegram_channel_off(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            delivery_driver.telegram_id = "104933915"
            db.session.commit()

            recon_service = DriverReconciliationService()
            session = recon_service.get_open_session_for_driver(delivery_driver.id)
            session.expected_cash_on_hand = Decimal("15000.00")
            db.session.commit()

            with patch("business_app.services.notification_service.NotificationService") as notification_cls:
                notification_instance = notification_cls.return_value
                notification_instance.send_notification.return_value = {'in_app': {'success': True}}
                notification_instance.send_staff_telegram_message.return_value = {'success': True}

                recon_service._send_driver_reconciliation_reminder(session, stage='pre_cutoff')

            notification_instance.send_notification.assert_called_once()
            notification_kwargs = notification_instance.send_notification.call_args.kwargs
            assert notification_kwargs['channels'] == [NotificationChannel.IN_APP]

            notification_instance.send_staff_telegram_message.assert_called_once()
            _, message = notification_instance.send_staff_telegram_message.call_args.args
            assert "Reminder: Reconciliation for" in message
