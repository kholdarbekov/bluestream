"""Unit tests for COD receivables, cash collection, and driver reconciliation services."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, DriverCashSession, Payment
from business_app.models.user import User
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.staff_service import StaffService
from business_app.utils.constants import (
    NotificationChannel,
)
from shared.enums import (
    DeliveryStatus,
    DriverCashSessionStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)
from business_app.utils.exceptions import ConflictError, ValidationError
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


def _make_cod_debtor(db, service, *, email, phone, name, amount):
    """Create a customer with one open delivered COD debt of `amount`."""
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password('DebtorPassword123!'),
        first_name=name,
        last_name='Debtor',
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    order = Order(
        user_id=user.id,
        order_number=f"ORD-DEBT-{name.upper()}-{uuid4().hex[:8]}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal(amount),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(amount),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    service.ensure_cod_payment_for_order(order)
    return user


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

    def test_cod_statement_exposes_customer_identity(self, app, db):
        """The staff statement payload must name the debtor (name + phone) so
        the bot can confirm who the cash is being collected from before it
        moves — guards against collecting from the wrong customer."""
        with app.app_context():
            service = CashCollectionService()
            debtor = _make_cod_debtor(
                db,
                service,
                email='cod.identity@example.com',
                phone='+998900000777',
                name='Identity',
                amount='45000.00',
            )
            db.session.commit()

            statement = service.get_customer_cod_statement(debtor.id)

            assert statement['first_name'] == 'Identity'
            assert statement['last_name'] == 'Debtor'
            assert statement['phone'] == '+998900000777'

    def test_get_session_detail_enriches_events_with_customer_order_and_settlement(
        self, app, db, delivery_driver, delivery_driver_profile
    ):
        """Session-detail events must name the customer/order and, for each
        settled order, whether it is now fully or partially paid — the admin
        modal renders a per-event settlement breakdown from this."""
        with app.app_context():
            cash = CashCollectionService()
            recon = DriverReconciliationService()

            # Debtor A: fully settled (collect the whole outstanding).
            debtor_full = _make_cod_debtor(
                db, cash, email='full@example.com', phone='+998900000111',
                name='Fulla', amount='45000.00',
            )
            # Debtor B: partially settled (collect less than outstanding).
            debtor_part = _make_cod_debtor(
                db, cash, email='part@example.com', phone='+998900000222',
                name='Parta', amount='90000.00',
            )
            db.session.commit()

            ev_full = cash.post_collection(
                customer_id=debtor_full.id, amount=Decimal("45000.00"),
                source="standalone_meeting", collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                notes="Full standalone settlement",
            )
            session_id = ev_full.driver_cash_session_id
            cash.post_collection(
                customer_id=debtor_part.id, amount=Decimal("30000.00"),
                source="standalone_meeting", collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                driver_cash_session_id=session_id,
                notes="Partial standalone settlement",
            )
            db.session.commit()

            payload = recon.get_session_detail(session_id)
            events = {e["customer_phone"]: e for e in payload["events"]}

            full = events["+998900000111"]
            assert full["customer_name"] == "Fulla Debtor"
            assert len(full["allocations"]) == 1
            alloc_full = full["allocations"][0]
            assert alloc_full["order_number"] is not None
            assert alloc_full["allocated_amount"] == 45000.0
            assert alloc_full["settlement"] == "fully"
            assert alloc_full["reversed"] is False

            part = events["+998900000222"]
            assert part["customer_name"] == "Parta Debtor"
            alloc_part = part["allocations"][0]
            assert alloc_part["allocated_amount"] == 30000.0
            assert alloc_part["settlement"] == "partial"

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

    def test_get_order_payment_timeline_includes_customer_identity(
        self, app, db
    ):
        """The payment-timeline payload must name the customer so the admin
        modal header can show who the order belongs to."""
        with app.app_context():
            cash = CashCollectionService()
            debtor = _make_cod_debtor(
                db, cash, email='tl.identity@example.com', phone='+998900000333',
                name='Timeline', amount='60000.00',
            )
            db.session.commit()
            order = Order.query.filter_by(user_id=debtor.id).first()

            timeline = cash.get_order_payment_timeline(order.id)

            assert timeline['customer_id'] == debtor.id
            assert timeline['customer_name'] == 'Timeline Debtor'
            assert timeline['customer_phone'] == '+998900000333'

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

    def test_cod_exempt_customer_bypasses_debt_limit(
        self,
        app,
        db,
        sample_user,
        cod_order,
    ):
        """Trusted users flagged ``cod_debt_check_exempt`` must always pass COD
        validation, even with the active-COD-debt count at/above the cap."""
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)

            # Two more delivered COD orders -> 3 active debts, well past the cap of 2.
            for idx in range(2):
                extra_order = Order(
                    user_id=sample_user.id,
                    order_number=f"ORD-EXEMPT-{idx:03d}",
                    status=OrderStatus.DELIVERED,
                    subtotal=Decimal("12000.00"),
                    delivery_fee=Decimal("2000.00"),
                    discount_amount=Decimal("0.00"),
                    loyalty_discount=Decimal("0.00"),
                    total_amount=Decimal("14000.00"),
                    payment_method=PaymentMethod.CASH,
                    created_at=datetime.now(UTC),
                )
                db.session.add(extra_order)
                db.session.flush()
                service.ensure_cod_payment_for_order(extra_order)
            db.session.commit()
            user_id = sample_user.id

            # Sanity: without the flag the user is restricted.
            assert service.get_active_cod_debt_count(user_id) == 3
            assert service.is_customer_cod_restricted(user_id) is True

            # Flip the admin-granted exemption on. Use a freshly-attached
            # instance: the sanity checks above issued their own ``User.query``
            # calls, leaving the original ``sample_user`` reference potentially
            # detached from the active session.
            fresh_user = User.query.get(user_id)
            fresh_user.cod_debt_check_exempt = True
            db.session.commit()

            assert service.is_customer_cod_restricted(user_id) is False
            # Should NOT raise.
            service.validate_customer_can_use_cod(user_id)

    def test_cod_restriction_context_reports_exemption(
        self,
        app,
        db,
        sample_user,
        cod_order,
    ):
        """get_cod_restriction_context surfaces the admin exemption as
        cod_restricted=False with a distinct reason and cod_exempt=True,
        while still reporting the accurate debt count."""
        with app.app_context():
            service = CashCollectionService()
            service.ensure_cod_payment_for_order(cod_order)
            second_order = Order(
                user_id=sample_user.id,
                order_number="ORD-EXEMPT-CTX-001",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("8000.00"),
                delivery_fee=Decimal("2000.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("10000.00"),
                payment_method=PaymentMethod.CASH,
                created_at=datetime.now(UTC),
            )
            db.session.add(second_order)
            db.session.flush()
            service.ensure_cod_payment_for_order(second_order)
            db.session.commit()
            user_id = sample_user.id

            # Fetch a session-attached instance to flip the flag. See
            # rationale in test_cod_exempt_customer_bypasses_debt_limit.
            fresh_user = User.query.get(user_id)
            fresh_user.cod_debt_check_exempt = True
            db.session.commit()

            context = service.get_cod_restriction_context(user_id)

            assert context["active_cod_debt_count"] == 2
            assert context["cod_restricted"] is False
            assert context["cod_exempt"] is True
            assert context["cod_restriction_reason"] == "customer_is_cod_exempt"

    def test_cod_exempt_flag_independent_of_grocery_store(
        self,
        app,
        db,
        sample_user,
    ):
        """The admin exemption works even when the user is not a grocery store
        and short-circuits before the debt cap is evaluated."""
        with app.app_context():
            service = CashCollectionService()
            # No debts at all + flag on -> not restricted, reason reflects exemption.
            sample_user.cod_debt_check_exempt = True
            db.session.commit()

            assert sample_user.is_grocery_store is False
            assert service.is_customer_cod_restricted(sample_user.id) is False
            context = service.get_cod_restriction_context(sample_user.id)
            assert context["cod_restricted"] is False
            assert context["cod_exempt"] is True
            assert context["cod_restriction_reason"] == "customer_is_cod_exempt"

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

    def test_paginate_users_with_open_cod_debts_sorts_and_pages(self, app, db):
        with app.app_context():
            service = CashCollectionService()
            u_low = _make_cod_debtor(db, service, email='debtor.low@example.com',
                                     phone='+998900000111', name='Low', amount='10000.00')
            u_mid = _make_cod_debtor(db, service, email='debtor.mid@example.com',
                                     phone='+998900000112', name='Mid', amount='20000.00')
            u_high = _make_cod_debtor(db, service, email='debtor.high@example.com',
                                      phone='+998900000113', name='High', amount='30000.00')
            db.session.commit()

            page1 = service.paginate_users_with_open_cod_debts(page=1, per_page=2)
            assert [item['id'] for item in page1['items']] == [u_high.id, u_mid.id]
            assert page1['items'][0]['total_outstanding_amount'] == 30000.0
            assert page1['items'][1]['total_outstanding_amount'] == 20000.0
            assert page1['pagination'] == {'page': 1, 'per_page': 2, 'total': 3, 'pages': 2}
            # Row shape parity with the admin list serialization.
            assert set(page1['items'][0]) == {
                'id', 'first_name', 'last_name', 'phone', 'role', 'user_type',
                'active_cod_debt_count', 'total_outstanding_amount', 'cod_restricted',
            }

            page2 = service.paginate_users_with_open_cod_debts(page=2, per_page=2)
            assert [item['id'] for item in page2['items']] == [u_low.id]

    def test_paginate_users_with_open_cod_debts_out_of_range_page_returns_empty(self, app, db):
        with app.app_context():
            service = CashCollectionService()
            _make_cod_debtor(db, service, email='debtor.solo@example.com',
                             phone='+998900000114', name='Solo', amount='15000.00')
            db.session.commit()

            result = service.paginate_users_with_open_cod_debts(page=9, per_page=10)
            assert result['items'] == []
            assert result['pagination'] == {'page': 9, 'per_page': 10, 'total': 1, 'pages': 1}

    def test_paginate_users_with_open_cod_debts_clamps_inputs(self, app, db):
        with app.app_context():
            service = CashCollectionService()
            result = service.paginate_users_with_open_cod_debts(page=0, per_page=500)
            assert result['items'] == []
            assert result['pagination'] == {'page': 1, 'per_page': 100, 'total': 0, 'pages': 0}

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
            # Driver hands over the full expected (4000) so the session closes;
            # admin then verifies a short count, which produces the blocked-from-cod
            # MISMATCH state we want to exercise. (Partial submission no longer
            # auto-blocks — that was the bug fixed alongside the partial-handoff
            # rollout, so we use the verify path to reach the blocked state.)
            submitted = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("4000.00"),
                notes="Driver declared all collected cash before counting.",
                submitted_by_user_id=delivery_driver.id,
            )
            assert submitted.status == DriverCashSessionStatus.SUBMITTED
            verified = recon_service.verify_session(
                session_id=submitted.id,
                verified_cash=Decimal("3000.00"),
                actor_user_id=delivery_driver.id,
                reason_code='cash_count_short',
                notes="Admin counted 1000 less than declared.",
            )
            assert verified.blocked_from_cod is True

            with pytest.raises(ValidationError, match="blocked from new cash on delivery collections"):
                cash_service.post_collection(
                    customer_id=sample_user.id,
                    amount=Decimal("1000.00"),
                    source="standalone_meeting",
                    collector_user_id=delivery_driver.id,
                    recorded_by_user_id=delivery_driver.id,
                    notes="Attempted late collection while blocked.",
                )

    def test_multi_day_cash_collections_remain_in_one_active_driver_session(
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
            service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            first_event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("5000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collected on delivery two days ago.",
                occurred_at=datetime.now(UTC) - timedelta(days=2),
            )
            second_event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("4000.00"),
                source="standalone_meeting",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                notes="Collected remaining cash later.",
                occurred_at=datetime.now(UTC),
            )

            assert first_event.driver_cash_session_id == second_event.driver_cash_session_id
            session = DriverCashSession.query.get(first_event.driver_cash_session_id)
            assert session.status == DriverCashSessionStatus.OPEN
            assert session.expected_cash == Decimal("9000.00")

    def test_admin_backfill_targets_explicit_historical_cash_session(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
        admin_user,
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
                notes="Original collection for old session.",
            )
            recon_service = DriverReconciliationService()
            old_session = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("5000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            next_session = getattr(old_session, "_next_active_session", None)
            assert next_session is not None
            assert next_session.id != old_session.id

            backfill_event = cash_service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("1000.00"),
                source="backfill",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=admin_user.id,
                driver_cash_session_id=old_session.id,
                notes="Historical backfill after cashier audit.",
            )

            assert backfill_event.driver_cash_session_id == old_session.id
            db.session.refresh(old_session)
            db.session.refresh(next_session)
            assert old_session.expected_cash == Decimal("6000.00")
            assert next_session.expected_cash == Decimal("0.00")

    def test_personal_card_transfer_can_settle_pending_cod_order_before_delivery(
        self,
        app,
        db,
        sample_user,
        admin_user,
        sample_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            sample_order.status = OrderStatus.CONFIRMED
            sample_order.payment_method = PaymentMethod.CASH
            db.session.commit()

            payment = service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("5000.00"),
                source="personal_card_transfer",
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes="Customer transferred to owner personal card.",
            )
            db.session.refresh(payment)
            refreshed_order = Order.query.get(sample_order.id)

            assert event.driver_cash_session_id is None
            assert event.unapplied_amount == Decimal("0.00")
            assert payment.status == PaymentStatus.PARTIALLY_PAID
            assert payment.amount_collected == Decimal("5000.00")
            assert payment.outstanding_amount == Decimal("13000.00")
            assert refreshed_order.is_paid is False
            assert DriverCashSession.query.count() == 0

    def test_personal_card_transfer_overflow_becomes_customer_prepayment_balance(
        self,
        app,
        db,
        sample_user,
        admin_user,
        sample_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            sample_order.status = OrderStatus.PREPARING
            sample_order.payment_method = PaymentMethod.CASH
            db.session.commit()

            payment = service.ensure_cod_payment_for_order(sample_order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("25000.00"),
                source="personal_card_transfer",
                recorded_by_user_id=admin_user.id,
                order_id=sample_order.id,
                notes="Customer transferred full amount plus extra to personal card.",
            )
            db.session.refresh(payment)
            refreshed_order = Order.query.get(sample_order.id)

            assert payment.status == PaymentStatus.COMPLETED
            assert payment.amount_collected == refreshed_order.total_amount
            assert payment.outstanding_amount == Decimal("0.00")
            assert refreshed_order.is_paid is True
            assert event.unapplied_amount == Decimal("7000.00")
            assert service.get_customer_prepaid_balance(sample_user.id) == Decimal("7000.00")

    def test_personal_card_transfer_requires_target_order_and_disallows_driver_context(
        self,
        app,
        db,
        sample_user,
        admin_user,
        delivery_driver,
    ):
        with app.app_context():
            service = CashCollectionService()

            with pytest.raises(ValidationError, match="order_id is required"):
                service.post_collection(
                    customer_id=sample_user.id,
                    amount=Decimal("1000.00"),
                    source="personal_card_transfer",
                    recorded_by_user_id=admin_user.id,
                    notes="Missing target order",
                )

            with pytest.raises(ValidationError, match="collector_user_id is not allowed"):
                service.post_collection(
                    customer_id=sample_user.id,
                    amount=Decimal("1000.00"),
                    source="personal_card_transfer",
                    recorded_by_user_id=admin_user.id,
                    collector_user_id=delivery_driver.id,
                    order_id=999999,
                    notes="Driver attribution is not allowed for personal transfers.",
                )

    def test_personal_card_transfer_rejects_cancelled_cod_order(
        self,
        app,
        db,
        sample_user,
        admin_user,
        sample_order,
    ):
        with app.app_context():
            service = CashCollectionService()
            sample_order.status = OrderStatus.CANCELLED
            sample_order.payment_method = PaymentMethod.CASH
            db.session.commit()

            with pytest.raises(ValidationError, match="Cancelled or returned COD orders cannot be targeted"):
                service.post_collection(
                    customer_id=sample_user.id,
                    amount=Decimal("1000.00"),
                    source="personal_card_transfer",
                    recorded_by_user_id=admin_user.id,
                    order_id=sample_order.id,
                    notes="Customer claimed personal transfer after cancellation.",
                )

    def test_delivery_without_cash_then_later_personal_card_transfer_settles_order_without_driver_cash_impact(
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

            no_cash_event = cash_service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("0.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Customer will transfer to owner card later.",
            )
            db.session.refresh(payment)
            assert no_cash_event.driver_cash_session_id is not None
            assert payment.status == PaymentStatus.PENDING
            assert payment.outstanding_amount == cod_order.total_amount

            driver_session = DriverCashSession.query.get(no_cash_event.driver_cash_session_id)
            assert driver_session is not None
            assert driver_session.expected_cash == Decimal("0.00")

            personal_event = cash_service.post_collection(
                customer_id=sample_user.id,
                amount=cod_order.total_amount,
                source="personal_card_transfer",
                recorded_by_user_id=admin_user.id,
                order_id=cod_order.id,
                notes="Customer transferred to owner personal card after delivery.",
            )
            db.session.refresh(payment)
            refreshed_order = Order.query.get(cod_order.id)

            DriverReconciliationService().refresh_expected_cash(driver_session)
            db.session.flush()

            assert personal_event.driver_cash_session_id is None
            assert payment.status == PaymentStatus.COMPLETED
            assert payment.outstanding_amount == Decimal("0.00")
            assert refreshed_order.is_paid is True
            assert driver_session.expected_cash == Decimal("0.00")

    def test_personal_card_transfer_settles_cancelled_click_payment(
        self,
        app,
        db,
        sample_user,
        admin_user,
        sample_order,
    ):
        """Admin can settle a CANCELLED Click payment via personal card transfer.

        Covers the canonical prod scenario (order TG_000178_26 / payment 652):
        the customer's Click payment timed out and was auto-cancelled, the order
        was already delivered, and the customer later transferred the amount
        directly to the owner's card.  The resulting payment must flip to CASH /
        COMPLETED and the order must be marked paid.
        """
        from shared.enums import CashCollectionSource

        with app.app_context():
            # Arrange: a delivered Click order whose payment was timeout-cancelled.
            # sample_order has no payment by default so we create one here.
            sample_order.status = OrderStatus.DELIVERED
            sample_order.payment_method = PaymentMethod.CLICK
            payment = Payment(
                order_id=sample_order.id,
                user_id=sample_order.user_id,
                payment_method=PaymentMethod.CLICK,
                status=PaymentStatus.CANCELLED,
                amount=Decimal("36000.00"),
                amount_collected=Decimal("0.00"),
                outstanding_amount=Decimal("36000.00"),
                currency="UZS",
                payment_id="click_cancelled_test_001",
            )
            db.session.add(payment)
            db.session.commit()

            order_id = sample_order.id
            payment_id = payment.id
            customer_id = sample_order.user_id

            CashCollectionService().post_collection(
                customer_id=customer_id,
                amount=Decimal("36000.00"),
                source=CashCollectionSource.PERSONAL_CARD_TRANSFER,
                recorded_by_user_id=admin_user.id,
                order_id=order_id,
                notes="Customer transferred to owner card after Click timeout",
            )

            fresh_payment = Payment.query.get(payment_id)
            fresh_order = Order.query.get(order_id)
            assert fresh_payment.status == PaymentStatus.COMPLETED
            assert fresh_payment.payment_method == PaymentMethod.CASH
            assert fresh_order.is_paid is True
            # Fully settled (re-derivation of a stale outstanding holds at zero).
            assert fresh_payment.outstanding_amount == Decimal("0.00")
            # Now a CASH sale → fiscalization must be flipped off the stuck
            # 'pending' state to NOT_REQUIRED (no tax receipt for cash).
            from business_app.models.payment import PaymentFiscalization
            from shared.enums import FiscalizationStatus

            fiscalization = PaymentFiscalization.query.filter_by(payment_id=payment_id).first()
            assert fiscalization is not None
            assert fiscalization.status == FiscalizationStatus.NOT_REQUIRED


@pytest.mark.unit
class TestDriverReconciliationService:
    def test_verify_short_blocks_driver_and_appears_in_report(
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
        """Driver-declared shorts no longer auto-block; admin verification still does.

        Under the partial-handoff rollout, a driver submitting less than the
        expected on-hand amount yields a PARTIAL session (still open). Only
        admin verification with a non-zero variance produces the blocked-from-COD
        MISMATCH state, and only that path needs to be guarded against
        bypass-by-future-COD-collection.
        """
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
                declared_cash=Decimal("10000.00"),
                notes="Driver declared full amount; admin will recount.",
                submitted_by_user_id=delivery_driver.id,
            )
            assert submitted.status == DriverCashSessionStatus.SUBMITTED
            assert submitted.blocked_from_cod is False

            verified = recon_service.verify_session(
                session_id=submitted.id,
                verified_cash=Decimal("9000.00"),
                actor_user_id=admin_user.id,
                reason_code='cash_count_short',
                notes="Admin recount came up 1000 short.",
            )

            assert verified.status == DriverCashSessionStatus.MISMATCH
            assert verified.blocked_from_cod is True
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is True

            report = recon_service.get_report(period="day", driver_user_id=delivery_driver.id)
            assert report["summary"]["blocked_session_count"] == 1
            assert report["summary"]["mismatch_session_count"] == 1
            assert report["report"][0]["blocked_session_count"] == 1

            resolved = recon_service.resolve_session(
                session_id=verified.id,
                actor_user_id=admin_user.id,
                reason_code='manager_approved_adjustment',
                resolution_notes="Admin verified the shortage and accepted adjustment.",
                verified_cash=Decimal("9000.00"),
            )

            assert resolved.status == DriverCashSessionStatus.RESOLVED
            assert resolved.blocked_from_cod is False
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is False

    def test_submit_defaults_to_expected_cash_on_hand(
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
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collected at delivery.",
            )

            recon_service = DriverReconciliationService()
            submitted = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=None,
                notes="Submitting with default expected-on-hand amount",
            )

            assert submitted.expected_cash == Decimal("10000.00")
            assert submitted.expected_cash_on_hand == Decimal("10000.00")
            assert submitted.declared_cash == Decimal("10000.00")
            assert submitted.declared_variance == Decimal("0.00")
            next_session = getattr(submitted, "_next_active_session", None)
            assert next_session is not None
            assert next_session.id != submitted.id
            assert next_session.driver_user_id == delivery_driver.id
            assert next_session.status == DriverCashSessionStatus.OPEN
            assert next_session.expected_cash_on_hand == Decimal("0.00")

    def test_partial_submission_keeps_session_open(
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
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collected on delivery.",
            )

            recon_service = DriverReconciliationService()
            partial = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("3000.00"),
                submitted_by_user_id=delivery_driver.id,
            )

            assert partial.status == DriverCashSessionStatus.PARTIAL
            assert partial.submitted_at is None
            assert partial.session_ended_at is None
            assert partial.blocked_from_cod is False
            assert partial.declared_cash == Decimal("3000.00")
            assert getattr(partial, "_next_active_session", None) is None
            # Re-fetching the open session for the same driver yields the same
            # partial session, not a fresh one.
            assert recon_service.get_open_session_for_driver(delivery_driver.id).id == partial.id

    def test_two_partial_handoffs_close_session_when_total_meets_expected(
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
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collected on delivery.",
            )

            recon_service = DriverReconciliationService()
            first = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("4000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            assert first.status == DriverCashSessionStatus.PARTIAL
            assert first.declared_cash == Decimal("4000.00")

            second = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("6000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            assert second.id == first.id
            assert second.status == DriverCashSessionStatus.SUBMITTED
            assert second.declared_cash == Decimal("10000.00")
            assert second.declared_variance == Decimal("0.00")
            assert second.submitted_at is not None
            assert second.session_ended_at is not None
            next_session = getattr(second, "_next_active_session", None)
            assert next_session is not None
            assert next_session.id != second.id

    def test_submit_all_after_partial_settles_only_the_remainder(
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
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
            )

            recon_service = DriverReconciliationService()
            recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("3000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            # The "Submit all" button passes declared_cash=None; the service
            # must interpret that as "the remainder", not "the full expected".
            closed = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=None,
                submitted_by_user_id=delivery_driver.id,
            )
            assert closed.status == DriverCashSessionStatus.SUBMITTED
            assert closed.declared_cash == Decimal("10000.00")
            # The remainder handoff should have been exactly 7000, not 10000.
            unvoided = [h for h in closed.handoffs if h.voided_at is None]
            amounts = sorted(h.amount for h in unvoided)
            assert amounts == [Decimal("3000.00"), Decimal("7000.00")]

    def test_over_submission_closes_session_without_blocking(
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
                amount=Decimal("10000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
            )

            recon_service = DriverReconciliationService()
            over = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("15000.00"),
                submitted_by_user_id=delivery_driver.id,
            )

            assert over.status == DriverCashSessionStatus.SUBMITTED
            assert over.declared_cash == Decimal("15000.00")
            assert over.declared_variance == Decimal("5000.00")
            assert over.blocked_from_cod is False
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is False
            assert getattr(over, "_next_active_session", None) is not None

    def test_zero_or_negative_handoff_amount_is_rejected(
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
            )

            recon_service = DriverReconciliationService()
            with pytest.raises(ValidationError, match="positive"):
                recon_service.submit_session(
                    driver_user_id=delivery_driver.id,
                    declared_cash=Decimal("0.00"),
                    submitted_by_user_id=delivery_driver.id,
                )

    def test_reopen_voids_existing_handoffs(
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
                amount=Decimal("5000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
            )
            recon_service = DriverReconciliationService()
            closed = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("5000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            assert closed.status == DriverCashSessionStatus.SUBMITTED
            assert len([h for h in closed.handoffs if h.voided_at is None]) == 1

            # Closing the session opened a fresh next-active session for the
            # driver. The reopen guard rejects reopen while another active
            # session exists, so we verify the empty successor first.
            next_active = getattr(closed, "_next_active_session", None)
            assert next_active is not None
            recon_service.verify_session(
                session_id=next_active.id,
                verified_cash=Decimal("0.00"),
                actor_user_id=admin_user.id,
                reason_code='cash_count_matched',
                notes="Empty successor closed before reopening original.",
            )

            reopened = recon_service.reopen_session(
                session_id=closed.id,
                actor_user_id=admin_user.id,
                reason="Order edit forced re-tally.",
            )
            assert reopened.status == DriverCashSessionStatus.OPEN
            assert reopened.submitted_at is None
            assert reopened.declared_cash is None
            assert all(h.voided_at is not None for h in reopened.handoffs)
            assert all(h.void_reason == "session_reopened" for h in reopened.handoffs)

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

    def test_warning_due_session_is_visible_without_blocking_cod(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            recon_service = DriverReconciliationService()
            session = recon_service.get_open_session_for_driver(delivery_driver.id)
            session.session_started_at = datetime.now(UTC) - timedelta(days=8)
            session.warning_due_at = datetime.now(UTC) - timedelta(minutes=5)
            session.submission_due_at = session.warning_due_at
            session.last_cash_activity_at = datetime.now(UTC) - timedelta(days=8)
            db.session.add(CashCollectionEvent(
                customer_id=sample_user.id,
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                driver_cash_session_id=session.id,
                amount=Decimal("15000.00"),
                source="standalone_meeting",
                occurred_at=datetime.now(UTC) - timedelta(days=8),
                notes="Old cash activity for warning-only test.",
            ))
            db.session.commit()

            updated = recon_service.mark_overdue_sessions(reference_time=datetime.now(UTC))
            db.session.refresh(session)

            assert updated >= 1
            assert session.status == DriverCashSessionStatus.OVERDUE
            assert session.blocked_from_cod is False
            assert recon_service.is_driver_blocked_from_cod(delivery_driver.id) is False
            assert "reconciliation_warning_due" in session.risk_flags

    def test_period_window_excludes_closed_session_with_null_session_ended_at(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        """A session verified directly (so session_ended_at stays NULL) must not
        leak into the current-day window once it was closed weeks ago.

        Regression: _apply_session_window_filters used to treat a NULL
        session_ended_at as "still ongoing", which surfaced every old
        verified/resolved session in the "today" cash-reconciliation filter.
        """
        with app.app_context():
            recon_service = DriverReconciliationService()
            session = recon_service.get_open_session_for_driver(delivery_driver.id)
            old_day = datetime.now(UTC) - timedelta(days=21)
            session.session_started_at = old_day
            session.status = DriverCashSessionStatus.VERIFIED
            session.verified_at = old_day
            session.verified_cash = Decimal("0.00")
            session.session_ended_at = None
            db.session.commit()

            today = datetime.now(UTC).date()
            listed = recon_service.list_sessions(
                start_date=today,
                end_date=today,
                driver_user_id=delivery_driver.id,
            )
            assert session.id not in {item["id"] for item in listed["items"]}

            report = recon_service.get_report(
                period="day", driver_user_id=delivery_driver.id
            )
            assert session.id not in {item["id"] for item in report["sessions"]}
            assert report["summary"]["session_count"] == 0

    def test_period_window_keeps_open_session_started_in_the_past(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        """A still-open session stays visible in the current-day window even
        when it was started — and last touched — days ago.

        Raw SQL pushes every timestamp into the past so the ORM onupdate hook
        does not bump updated_at back to now: this proves visibility comes from
        the session's active status, not from a close/activity timestamp
        landing inside the window.
        """
        with app.app_context():
            from sqlalchemy import text

            recon_service = DriverReconciliationService()
            session = recon_service.get_open_session_for_driver(delivery_driver.id)
            assert session.status == DriverCashSessionStatus.OPEN
            session_id = session.id

            old_day = (datetime.now(UTC) - timedelta(days=15)).isoformat()
            db.session.execute(
                text(
                    "UPDATE driver_cash_sessions "
                    "SET session_started_at = :d, updated_at = :d, "
                    "last_cash_activity_at = NULL, session_ended_at = NULL, "
                    "submitted_at = NULL, verified_at = NULL "
                    "WHERE id = :id"
                ),
                {"d": old_day, "id": session_id},
            )
            db.session.commit()

            today = datetime.now(UTC).date()
            listed = recon_service.list_sessions(
                start_date=today,
                end_date=today,
                driver_user_id=delivery_driver.id,
            )
            assert session_id in {item["id"] for item in listed["items"]}

    def test_reconciliation_reminder_uses_staff_bot_and_keeps_customer_telegram_channel_off(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            # B-1: the reminder body is now resolved from a DB-backed
            # translation keyed off driver.preferred_language. Seed the
            # `en` variant so the body assertion below has something
            # concrete to match — without this, business_app.utils.translations
            # falls back to returning the bare key (`staff.notification.…`)
            # because the unit-test sqlite has no staff_bot translations.
            from business_app.models.translation import Translation
            db.session.add(Translation(
                key='staff.notification.reconciliation_reminder_due',
                language='en',
                value=(
                    'Reminder: cash reconciliation for {date} is pending. '
                    'Expected on-hand cash: {expected_cash} UZS.'
                ),
                category='staff_bot',
                is_active=True,
            ))
            db.session.add(Translation(
                key='staff.notification.subject.driver_cash_reconciliation',
                language='en',
                value='Driver cash reconciliation',
                category='staff_bot',
                is_active=True,
            ))
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
            # Resolved body must come from the seeded translation, not from
            # the bare-key fallback path (which would indicate the lookup
            # failed to find anything in the staff_bot category).
            assert message != 'staff.notification.reconciliation_reminder_due'
            assert 'Reminder: cash reconciliation' in message
            assert '15,000' in message  # placeholder substitution worked
            # And the staff Telegram path is invoked with the driver's own
            # language so downstream NotificationService doesn't fall back
            # to the customer-bot's language settings.
            staff_kwargs = notification_instance.send_staff_telegram_message.call_args.kwargs
            assert staff_kwargs.get('language') == 'en'

    def test_submit_closes_settled_session_when_expected_dropped_to_zero(
        self,
        app,
        db,
        sample_user,
        delivery_driver,
        delivery_driver_profile,
        cod_order,
        cod_delivery,
    ):
        """A PARTIAL session whose backing COD collection was later voided
        (expected -> 0, declared stays positive) must be closeable via the
        'hand off all expected cash' button instead of raising 'must be
        positive'. Reproduces the prod screenshot (expected 0, declared 1000,
        remaining 0, stuck on PARTIAL)."""
        with app.app_context():
            cash_service = CashCollectionService()
            cash_service.ensure_cod_payment_for_order(cod_order)
            db.session.commit()

            event = cash_service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("2000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=cod_order.id,
                delivery_id=cod_delivery.id,
                notes="Collected on delivery.",
            )
            db.session.commit()

            recon_service = DriverReconciliationService()
            # Partial handoff of 1000 against the 2000 expected -> stays PARTIAL
            # (no next session created, session stays active).
            partial = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("1000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            assert partial.status == DriverCashSessionStatus.PARTIAL

            # The backing COD collection is later voided (order cancelled / cash
            # edit reversed), dropping expected_cash_on_hand to 0 while the 1000
            # handoff stands. This is the trapped state.
            voided = CashCollectionEvent.query.get(event.id)
            voided.voided_at = datetime.now(UTC)
            db.session.commit()

            session = recon_service.get_open_session_for_driver(delivery_driver.id)
            recon_service.refresh_expected_cash(session)
            db.session.commit()
            assert session.status == DriverCashSessionStatus.PARTIAL
            assert session.expected_cash_on_hand == Decimal("0.00")
            assert session.declared_cash == Decimal("1000.00")
            unvoided_before = [h for h in session.handoffs if h.voided_at is None]
            assert len(unvoided_before) == 1

            closed = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=None,
                submitted_by_user_id=delivery_driver.id,
            )

            assert closed.status == DriverCashSessionStatus.SUBMITTED
            assert closed.declared_cash == Decimal("1000.00")
            assert closed.declared_variance == Decimal("1000.00")
            assert closed.blocked_from_cod is False
            # No zero/negative handoff row inserted.
            unvoided_after = [h for h in closed.handoffs if h.voided_at is None]
            assert len(unvoided_after) == 1
            assert getattr(closed, "_next_active_session", None) is not None

    def test_submit_empty_session_raises_clear_error(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        """Submitting a fresh session with nothing collected and nothing handed
        off is a no-op error with a clear message (not 'must be positive')."""
        with app.app_context():
            recon_service = DriverReconciliationService()
            recon_service.get_or_create_session(driver_user_id=delivery_driver.id)

            with pytest.raises(ValidationError) as exc:
                recon_service.submit_session(
                    driver_user_id=delivery_driver.id,
                    declared_cash=None,
                    submitted_by_user_id=delivery_driver.id,
                )
            assert "No cash to reconcile" in str(exc.value)

    def test_force_closed_status_and_reason_column_roundtrip(
        self,
        app,
        db,
        delivery_driver,
        delivery_driver_profile,
    ):
        """The new FORCE_CLOSED enum value + force_close_reason column persist
        and serialize."""
        with app.app_context():
            recon_service = DriverReconciliationService()
            session = recon_service.get_or_create_session(driver_user_id=delivery_driver.id)
            session.status = DriverCashSessionStatus.FORCE_CLOSED
            session.force_close_reason = "driver left the company"
            db.session.commit()

            fetched = DriverCashSession.query.get(session.id)
            assert fetched.status == DriverCashSessionStatus.FORCE_CLOSED
            assert fetched.to_dict()["force_close_reason"] == "driver left the company"

    def test_force_close_from_partial_closes_and_unblocks(
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
                notes="Collected on delivery.",
            )
            recon_service = DriverReconciliationService()
            partial = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=Decimal("3000.00"),
                submitted_by_user_id=delivery_driver.id,
            )
            assert partial.status == DriverCashSessionStatus.PARTIAL

            closed = recon_service.force_close_session(
                session_id=partial.id,
                actor_user_id=admin_user.id,
                reason="Driver stopped responding; closing session.",
            )

            assert closed.status == DriverCashSessionStatus.FORCE_CLOSED
            assert closed.force_close_reason == "Driver stopped responding; closing session."
            assert closed.session_ended_at is not None
            assert closed.submitted_by_user_id == admin_user.id
            assert closed.blocked_from_cod is False
            assert closed.verified_cash is None

    def test_force_close_records_verified_cash_variance(
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
                notes="Collected on delivery.",
            )
            recon_service = DriverReconciliationService()
            recon_service.get_or_create_session(driver_user_id=delivery_driver.id)
            session = recon_service.get_open_session_for_driver(delivery_driver.id)

            closed = recon_service.force_close_session(
                session_id=session.id,
                actor_user_id=admin_user.id,
                reason="Counted cash at office and closing.",
                verified_cash=Decimal("9000.00"),
            )

            assert closed.status == DriverCashSessionStatus.FORCE_CLOSED
            assert closed.verified_cash == Decimal("9000.00")
            assert closed.verified_variance == Decimal("-1000.00")
            assert closed.verified_by_user_id == admin_user.id
            assert closed.verified_at is not None

    def test_force_close_rejects_blank_reason(
        self,
        app,
        db,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            recon_service = DriverReconciliationService()
            session = recon_service.get_or_create_session(driver_user_id=delivery_driver.id)
            with pytest.raises(ValidationError):
                recon_service.force_close_session(
                    session_id=session.id,
                    actor_user_id=admin_user.id,
                    reason="   ",
                )

    def test_force_close_rejects_already_closed_session(
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
                notes="Collected on delivery.",
            )
            recon_service = DriverReconciliationService()
            submitted = recon_service.submit_session(
                driver_user_id=delivery_driver.id,
                declared_cash=None,
                submitted_by_user_id=delivery_driver.id,
            )
            assert submitted.status == DriverCashSessionStatus.SUBMITTED
            with pytest.raises(ConflictError):
                recon_service.force_close_session(
                    session_id=submitted.id,
                    actor_user_id=admin_user.id,
                    reason="Trying to force close an already-submitted session.",
                )

    def test_reopen_from_force_closed_clears_reason(
        self,
        app,
        db,
        admin_user,
        delivery_driver,
        delivery_driver_profile,
    ):
        with app.app_context():
            recon_service = DriverReconciliationService()
            session = recon_service.get_or_create_session(driver_user_id=delivery_driver.id)
            closed = recon_service.force_close_session(
                session_id=session.id,
                actor_user_id=admin_user.id,
                reason="close for reopen test",
                verified_cash=Decimal("0.00"),
            )
            assert closed.status == DriverCashSessionStatus.FORCE_CLOSED

            reopened = recon_service.reopen_session(
                session_id=closed.id,
                actor_user_id=admin_user.id,
                reason="reopen after force close",
            )
            assert reopened.status == DriverCashSessionStatus.OPEN
            assert reopened.force_close_reason is None


@pytest.mark.unit
class TestCashCollectionGroceryUnitsMirror:
    """Regression suite for grocery stores on legacy UNITS-mode contracts.

    Cash collected at delivery for these contracts must auto-post TOPUP ledger
    entries matching the CONSUME entries already written. AMOUNT-mode grocery
    contracts continue to produce a single COLLECT entry (and no TOPUPs).
    """

    @staticmethod
    def _seed_units_grocery_state(db, sample_user, sample_product, contract_currency="UZS"):
        from uuid import uuid4

        from business_app.models.corporate import (
            CorporateContract,
            CorporateContractProductPrice,
            CorporateContractStatus,
            CorporatePrepaymentAccount,
        )
        from business_app.models.order import OrderItem
        from shared.enums import CorporateContractTrackingMode, EntitySubtype

        sample_user.user_type = UserType.ENTITY
        sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
        db.session.commit()

        # Direct-write a UNITS-mode contract for the grocery store, bypassing the
        # service-level forced-AMOUNT enforcement to reproduce the legacy state.
        contract = CorporateContract(
            user_id=sample_user.id,
            contract_number=f"GS-UNITS-{uuid4().hex[:10]}",
            name="Legacy Units Grocery Contract",
            status=CorporateContractStatus.ACTIVE,
            start_date=datetime.now(UTC) - timedelta(days=1),
            currency=contract_currency,
            is_active=True,
            tracking_mode=CorporateContractTrackingMode.UNITS,
        )
        db.session.add(contract)
        db.session.flush()
        account = CorporatePrepaymentAccount(contract_id=contract.id, is_active=True)
        db.session.add(account)
        price_row = CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=sample_product.id,
            unit_price=Decimal("12000.00"),
            is_prepayment_eligible=True,
            is_active=True,
        )
        db.session.add(price_row)
        db.session.commit()
        return contract, account, price_row

    @staticmethod
    def _build_units_order(db, sample_user, sample_product, price_row, *, quantity=2):
        from uuid import uuid4

        from business_app.models.order import OrderItem

        unit_price = Decimal(str(price_row.unit_price))
        total = unit_price * Decimal(str(quantity))
        order = Order(
            order_number=f"AD-UNITS-{uuid4().hex[:8]}",
            user_id=sample_user.id,
            status=OrderStatus.PENDING,
            subtotal=total,
            delivery_fee=Decimal("0.00"),
            total_amount=total,
            payment_method=PaymentMethod.CASH,
            order_source="admin",
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=sample_product.id,
                contract_id=price_row.contract_id,
                contract_product_price_id=price_row.id,
                quantity=quantity,
                unit_price=unit_price,
                total_price=total,
            )
        )
        db.session.commit()
        return order

    @staticmethod
    def _attach_delivery(db, order, delivery_driver, *, status=DeliveryStatus.DELIVERED):
        delivery = Delivery(
            order_id=order.id,
            delivery_person_id=delivery_driver.id,
            status=status,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
            actual_delivery_time=datetime.now(UTC),
            delivered_at=datetime.now(UTC) if status == DeliveryStatus.DELIVERED else None,
        )
        db.session.add(delivery)
        db.session.commit()
        return delivery

    def test_post_collection_creates_auto_topup_for_units_mode_grocery_contract(
        self,
        app,
        db,
        sample_user,
        sample_product,
        delivery_driver,
        delivery_driver_profile,
    ):
        from business_app.models.corporate import (
            CorporatePrepaymentBalance,
            CorporatePrepaymentEventType,
            CorporatePrepaymentLedger,
        )
        from business_app.services.corporate_contract_service import CorporateContractService

        with app.app_context():
            contract, account, price_row = self._seed_units_grocery_state(
                db, sample_user, sample_product
            )
            order = self._build_units_order(
                db, sample_user, sample_product, price_row, quantity=3
            )

            corporate_service = CorporateContractService()
            corporate_service.reserve_for_order(order.id)
            corporate_service.consume_for_order(order.id)
            db.session.commit()

            delivery = self._attach_delivery(db, order, delivery_driver)
            order.status = OrderStatus.DELIVERED
            order.delivered_at = datetime.now(UTC)
            db.session.commit()

            service = CashCollectionService()
            service.ensure_cod_payment_for_order(order)
            db.session.commit()

            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal(str(order.total_amount)),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                delivery_id=delivery.id,
                notes="Cash collected at delivery for legacy UNITS grocery",
            )

            topups = (
                CorporatePrepaymentLedger.query.filter_by(
                    order_id=order.id,
                    event_type=CorporatePrepaymentEventType.TOPUP,
                )
                .all()
            )
            assert len(topups) == 1
            topup = topups[0]
            assert topup.contract_id == contract.id
            assert topup.product_id == sample_product.id
            assert Decimal(str(topup.units)) == Decimal("3.00")
            assert topup.idempotency_key == f"topup:cash_event:{event.id}:consume:{topup.entry_metadata['source_consume_entry_id']}"
            assert topup.entry_metadata.get("auto_topup") is True
            assert topup.entry_metadata.get("source") == "delivery_completion"

            balance = CorporatePrepaymentBalance.query.filter_by(
                account_id=account.id, product_id=sample_product.id
            ).first()
            db.session.refresh(balance)
            assert Decimal(str(balance.prepaid_units)) == Decimal("3.00")
            assert Decimal(str(balance.consumed_units)) == Decimal("3.00")
            # Topup matched consumption; available balance is non-negative again.
            assert balance.available_units == Decimal("0.00")

            # No COLLECT entries should be posted for a UNITS-mode contract.
            assert (
                CorporatePrepaymentLedger.query.filter_by(
                    order_id=order.id,
                    event_type=CorporatePrepaymentEventType.COLLECT,
                ).count()
                == 0
            )

    def test_post_collection_skips_units_topup_when_no_order_id(
        self,
        app,
        db,
        sample_user,
        sample_product,
        delivery_driver,
        delivery_driver_profile,
    ):
        from business_app.models.corporate import (
            CorporatePrepaymentEventType,
            CorporatePrepaymentLedger,
        )

        with app.app_context():
            self._seed_units_grocery_state(db, sample_user, sample_product)

            service = CashCollectionService()
            # Standalone collection: no order_id, no delivery_id.
            event = service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("5000.00"),
                source="standalone_meeting",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=None,
                delivery_id=None,
                notes="Walked-in payment",
            )

            assert event is not None
            assert (
                CorporatePrepaymentLedger.query.filter_by(
                    event_type=CorporatePrepaymentEventType.TOPUP
                ).count()
                == 0
            )

    def test_post_collection_skips_units_topup_when_consume_not_yet_posted(
        self,
        app,
        db,
        sample_user,
        sample_product,
        delivery_driver,
        delivery_driver_profile,
    ):
        from business_app.models.corporate import (
            CorporatePrepaymentEventType,
            CorporatePrepaymentLedger,
        )
        from business_app.services.corporate_contract_service import CorporateContractService

        with app.app_context():
            _contract, _account, price_row = self._seed_units_grocery_state(
                db, sample_user, sample_product
            )
            order = self._build_units_order(
                db, sample_user, sample_product, price_row, quantity=1
            )

            # Reserve but do NOT consume yet — defensive path: cash collected
            # while CONSUME ledger entries are still missing (skew / replay /
            # manual data fix). Order is DELIVERED so the collection validator
            # accepts it; the missing CONSUME entries are the focus here.
            CorporateContractService().reserve_for_order(order.id)
            db.session.commit()

            delivery = self._attach_delivery(db, order, delivery_driver)
            order.status = OrderStatus.DELIVERED
            order.delivered_at = datetime.now(UTC)
            db.session.commit()

            service = CashCollectionService()
            service.ensure_cod_payment_for_order(order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal(str(order.total_amount)),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                delivery_id=delivery.id,
            )

            assert (
                CorporatePrepaymentLedger.query.filter_by(
                    order_id=order.id,
                    event_type=CorporatePrepaymentEventType.TOPUP,
                ).count()
                == 0
            )

    def test_post_collection_amount_mode_path_unchanged(
        self,
        app,
        db,
        sample_user,
        sample_product,
        delivery_driver,
        delivery_driver_profile,
    ):
        from uuid import uuid4

        from business_app.models.corporate import (
            CorporateContract,
            CorporateContractStatus,
            CorporatePrepaymentAccount,
            CorporatePrepaymentEventType,
            CorporatePrepaymentLedger,
        )
        from business_app.models.order import OrderItem
        from shared.enums import CorporateContractTrackingMode, EntitySubtype

        with app.app_context():
            sample_user.user_type = UserType.ENTITY
            sample_user.entity_subtype = EntitySubtype.GROCERY_STORE
            db.session.commit()

            contract = CorporateContract(
                user_id=sample_user.id,
                contract_number=f"GS-AMT-{uuid4().hex[:10]}",
                name="Grocery Store AMOUNT Contract",
                status=CorporateContractStatus.ACTIVE,
                start_date=datetime.now(UTC) - timedelta(days=1),
                currency="UZS",
                is_active=True,
                tracking_mode=CorporateContractTrackingMode.AMOUNT,
            )
            db.session.add(contract)
            db.session.flush()
            db.session.add(CorporatePrepaymentAccount(contract_id=contract.id, is_active=True))
            db.session.commit()

            order = Order(
                order_number=f"AD-AMT-{uuid4().hex[:8]}",
                user_id=sample_user.id,
                status=OrderStatus.PENDING,
                subtotal=Decimal("36000.00"),
                delivery_fee=Decimal("0.00"),
                total_amount=Decimal("36000.00"),
                payment_method=PaymentMethod.CASH,
                order_source="admin",
            )
            db.session.add(order)
            db.session.flush()
            db.session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=sample_product.id,
                    quantity=3,
                    unit_price=Decimal("12000.00"),
                    total_price=Decimal("36000.00"),
                )
            )
            db.session.commit()

            delivery = self._attach_delivery(db, order, delivery_driver)
            order.status = OrderStatus.DELIVERED
            order.delivered_at = datetime.now(UTC)
            db.session.commit()

            service = CashCollectionService()
            service.ensure_cod_payment_for_order(order)
            db.session.commit()

            service.post_collection(
                customer_id=sample_user.id,
                amount=Decimal("36000.00"),
                source="delivery_completion",
                collector_user_id=delivery_driver.id,
                recorded_by_user_id=delivery_driver.id,
                order_id=order.id,
                delivery_id=delivery.id,
            )

            # Existing AMOUNT-mode path: exactly one COLLECT, zero TOPUP entries.
            assert (
                CorporatePrepaymentLedger.query.filter_by(
                    contract_id=contract.id,
                    event_type=CorporatePrepaymentEventType.COLLECT,
                ).count()
                == 1
            )
            assert (
                CorporatePrepaymentLedger.query.filter_by(
                    contract_id=contract.id,
                    event_type=CorporatePrepaymentEventType.TOPUP,
                ).count()
                == 0
            )


def _add_cash_order(db, user, *, status, amount, pay_status, collected="0.00"):
    """Attach an extra CASH order+payment (any status) to `user`.

    Used to model non-collectible debt (cancelled / not-yet-delivered orders)
    whose Payment row may still carry a stale outstanding_amount.
    """
    order = Order(
        user_id=user.id,
        order_number=f"ORD-{status.value.upper()}-{uuid4().hex[:8]}",
        status=status,
        subtotal=Decimal(amount),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(amount),
        payment_method=PaymentMethod.CASH,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_id=f"pay-{uuid4().hex[:12]}",
        payment_method=PaymentMethod.CASH,
        amount=Decimal(amount),
        amount_collected=Decimal(collected),
        outstanding_amount=Decimal(amount) - Decimal(collected),
        currency="UZS",
        status=pay_status,
        created_at=datetime.now(UTC),
    )
    db.session.add(payment)
    db.session.flush()
    return order, payment


@pytest.mark.unit
class TestCodStatementExcludesCancelledReturnedDebt:
    """get_customer_cod_statement totals drop cancelled/returned orders (not
    collectible) but keep pending orders; `items` still lists every order."""

    @pytest.mark.parametrize("order_status", [OrderStatus.CANCELLED, OrderStatus.RETURNED])
    def test_terminal_order_outstanding_excluded_from_totals(self, app, db, order_status):
        with app.app_context():
            service = CashCollectionService()
            debtor = _make_cod_debtor(
                db, service,
                email=f'rc2.{order_status.value}@example.com',
                phone='+998900000155',
                name='RcTwoExcl',
                amount='63000.00',
            )
            # A terminal order whose payment still shows outstanding.
            _add_cash_order(
                db, debtor, status=order_status,
                amount='90000.00', pay_status=PaymentStatus.CANCELLED,
            )
            db.session.commit()

            statement = service.get_customer_cod_statement(debtor.id)

            # Only the delivered debt counts; the terminal order is dropped.
            assert statement['total_outstanding_amount'] == 63000.0
            assert statement['gross_outstanding_amount'] == 63000.0
            assert statement['net_outstanding_amount'] == 63000.0
            # Display still lists every payment (including the excluded one).
            assert len(statement['items']) == 2

    def test_pending_pipeline_order_still_counted_in_totals(self, app, db):
        # Pending orders stay in the totals for the admin reserved/net modal;
        # only the collect-all consumer filters to delivered.
        with app.app_context():
            service = CashCollectionService()
            debtor = _make_cod_debtor(
                db, service,
                email='rc2.pending@example.com',
                phone='+998900000156',
                name='RcTwoPending',
                amount='63000.00',
            )
            _add_cash_order(
                db, debtor, status=OrderStatus.CONFIRMED,
                amount='144000.00', pay_status=PaymentStatus.PENDING,
            )
            db.session.commit()

            statement = service.get_customer_cod_statement(debtor.id)

            assert statement['total_outstanding_amount'] == 207000.0
