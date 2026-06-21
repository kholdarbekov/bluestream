"""Postgres end-to-end enforcement of the COD cash collector invariant.

The default SQLite suite cannot prove that the *real* migration-declared CHECK
constraint ``ck_payments_cash_completed_requires_collector`` fires on commit —
which is precisely how the production incident slipped through. These tests run
against a fresh, fully-migrated Postgres database (the ``pg_app``/``pg_db``
fixtures) so the genuine constraint and enum types are present and a violation
raises ``sqlalchemy.exc.IntegrityError`` on commit, exactly like prod.

Two scenarios:
  1. Positive: a delivered COD order fully covered by reserved prepayment is
     settled via the real service path, committed, and the persisted
     ``collected_by`` column is set — no exception.
  2. Negative: committing a CASH + COMPLETED payment with ``collected_by=NULL``
     raises IntegrityError (the exact prod failure the fix prevents).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from business_app.models.delivery import DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import CashCollectionEvent, Payment
from business_app.models.user import User, UserAddress
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.password_security import hash_password
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)


def _make_customer(pg_db):
    user = User(
        email="cod.customer.pg@example.com",
        phone="+998900000201",
        password_hash=hash_password("CustPassword123!"),
        first_name="COD",
        last_name="Customer",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add(user)
    pg_db.session.flush()
    return user


def _make_driver(pg_db):
    user = User(
        email="cod.driver.pg@example.com",
        phone="+998900000202",
        password_hash=hash_password("DriverPassword123!"),
        first_name="COD",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add(user)
    pg_db.session.flush()
    profile = DeliveryPerson(
        user_id=user.id,
        full_name="COD Driver",
        phone=user.phone,
        email=user.email,
        is_active=True,
        is_available=True,
    )
    pg_db.session.add(profile)
    pg_db.session.flush()
    return user


def _make_address(pg_db, user):
    address = UserAddress(
        user_id=user.id,
        full_address="123 Test Street, Tashkent",
        street_address="123 Test Street",
        city="Tashkent",
        latitude=41.2995,
        longitude=69.2401,
        is_default=True,
    )
    pg_db.session.add(address)
    pg_db.session.flush()
    return address


def _make_cash_order(pg_db, user, *, order_number, total, status):
    # Delivery-bearing states (ARCH-006: ck_orders_address_required_after_pending)
    # require a delivery_address_id on Postgres.
    address = _make_address(pg_db, user)
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=status,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    pg_db.session.add(order)
    pg_db.session.flush()
    return order


@pytest.mark.integration
class TestCodCollectorConstraintPostgres:
    def test_settled_cod_payment_commits_with_collector_set(self, pg_app, pg_db):
        """A delivered COD order, fully covered by reserved prepayment whose
        source event records the driver as collector, settles and COMMITS
        cleanly with the persisted collected_by populated."""
        service = CashCollectionService()

        customer = _make_customer(pg_db)
        driver = _make_driver(pg_db)

        # Driver collected standalone cash earlier -> unapplied credit.
        event = CashCollectionEvent(
            customer_id=customer.id,
            collector_user_id=driver.id,
            recorded_by_user_id=driver.id,
            amount=Decimal("30000.00"),
            currency="UZS",
            source="standalone_meeting",
            occurred_at=datetime.now(UTC),
            notes="Driver collected standalone cash",
            unapplied_amount=Decimal("30000.00"),
        )
        pg_db.session.add(event)
        pg_db.session.flush()

        order = _make_cash_order(
            pg_db, customer, order_number="ORD-PG-SETTLE",
            total="30000.00", status=OrderStatus.PENDING,
        )
        payment = service.ensure_cod_payment_for_order(order)
        pg_db.session.flush()
        service.reserve_customer_prepaid_credit_for_payment(payment)
        pg_db.session.commit()

        # Deliver + settle the reservation.
        order.status = OrderStatus.DELIVERED
        pg_db.session.flush()
        consumed = service.consume_reserved_prepayment_for_payment(
            payment, collected_at=datetime.now(UTC), collected_by=driver.id
        )

        # Commit on Postgres: the real CHECK constraint validates the row.
        pg_db.session.commit()

        pg_db.session.refresh(payment)
        assert consumed == Decimal("30000.00")
        assert payment.status == PaymentStatus.COMPLETED
        assert payment.outstanding_amount == Decimal("0.00")
        assert payment.collected_by == driver.id

        # And the value is genuinely persisted at the SQL level.
        persisted = pg_db.session.execute(
            text(
                "SELECT collected_by, status, payment_method FROM payments WHERE id = :pid"
            ),
            {"pid": payment.id},
        ).one()
        assert persisted.collected_by == driver.id
        assert persisted.status == PaymentStatus.COMPLETED.value
        assert persisted.payment_method == PaymentMethod.CASH.value

    def test_commit_cash_completed_without_collector_raises_integrity_error(
        self, pg_app, pg_db
    ):
        """The exact prod failure: a CASH payment committed as COMPLETED with
        collected_by=NULL must raise IntegrityError from
        ck_payments_cash_completed_requires_collector."""
        customer = _make_customer(pg_db)
        order = _make_cash_order(
            pg_db, customer, order_number="ORD-PG-VIOLATE",
            total="30000.00", status=OrderStatus.DELIVERED,
        )

        # Construct the offending row directly, bypassing the service guard, so
        # the DB CHECK is the only defence — mirroring how a buggy code path
        # would have tried to persist this.
        bad_payment = Payment(
            order_id=order.id,
            user_id=customer.id,
            amount=Decimal("30000.00"),
            currency="UZS",
            payment_method=PaymentMethod.CASH,
            status=PaymentStatus.COMPLETED,
            amount_collected=Decimal("30000.00"),
            outstanding_amount=Decimal("0.00"),
            collected_by=None,
            paid_at=datetime.now(UTC),
        )
        pg_db.session.add(bad_payment)

        with pytest.raises(IntegrityError) as exc_info:
            pg_db.session.commit()

        # The named CHECK constraint is what fired.
        assert "ck_payments_cash_completed_requires_collector" in str(exc_info.value)
        pg_db.session.rollback()

    def test_setting_collector_satisfies_constraint_after_violation(
        self, pg_app, pg_db
    ):
        """After the constraint rejects a NULL-collector cash completion,
        stamping a collector and re-committing succeeds — proving the fix's
        remedy is exactly what the constraint requires."""
        customer = _make_customer(pg_db)
        driver = _make_driver(pg_db)
        order = _make_cash_order(
            pg_db, customer, order_number="ORD-PG-FIX",
            total="30000.00", status=OrderStatus.DELIVERED,
        )
        # Commit the setup so the rollback after the constraint violation does
        # not also discard the order/users we rely on for the retry.
        pg_db.session.commit()

        bad_payment = Payment(
            order_id=order.id,
            user_id=customer.id,
            amount=Decimal("30000.00"),
            currency="UZS",
            payment_method=PaymentMethod.CASH,
            status=PaymentStatus.COMPLETED,
            amount_collected=Decimal("30000.00"),
            outstanding_amount=Decimal("0.00"),
            collected_by=None,
            paid_at=datetime.now(UTC),
        )
        pg_db.session.add(bad_payment)
        with pytest.raises(IntegrityError):
            pg_db.session.commit()
        pg_db.session.rollback()

        # Re-create with a collector set — the constraint is satisfied.
        good_payment = Payment(
            order_id=order.id,
            user_id=customer.id,
            amount=Decimal("30000.00"),
            currency="UZS",
            payment_method=PaymentMethod.CASH,
            status=PaymentStatus.COMPLETED,
            amount_collected=Decimal("30000.00"),
            outstanding_amount=Decimal("0.00"),
            collected_by=driver.id,
            paid_at=datetime.now(UTC),
        )
        pg_db.session.add(good_payment)
        pg_db.session.commit()

        pg_db.session.refresh(good_payment)
        assert good_payment.collected_by == driver.id
        assert good_payment.status == PaymentStatus.COMPLETED
