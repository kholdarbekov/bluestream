"""Service-layer tests for staff API boundary refactors."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.payment import Payment
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.staff_service import StaffService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import UserStatus


def _create_user(db, phone: str, role: UserRole) -> User:
    user = User(
        phone=phone,
        first_name="Test",
        last_name="User",
        password_hash=hash_password("TestPassword123!"),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_get_recent_operator_orders_returns_latest_first(db, sample_user):
    operator = _create_user(db, "+998901111111", UserRole.OPERATOR)
    other_operator = _create_user(db, "+998902222222", UserRole.OPERATOR)

    older = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        order_source="phone",
        created_by_staff_id=operator.id,
        created_at=datetime.now(UTC) - timedelta(hours=2),
    )
    latest = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("20000"),
        total_amount=Decimal("20000"),
        order_source="phone",
        created_by_staff_id=operator.id,
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    foreign = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("30000"),
        total_amount=Decimal("30000"),
        order_source="phone",
        created_by_staff_id=other_operator.id,
        created_at=datetime.now(UTC),
    )
    db.session.add_all([older, latest, foreign])
    db.session.commit()

    orders = StaffService.get_recent_operator_orders(operator.id, limit=10)

    assert [o.id for o in orders] == [latest.id, older.id]


def test_add_and_get_client_addresses_delegate_to_service_layer(db, sample_user):
    created = StaffService.add_client_address(
        sample_user.id,
        {
            "label": "Home",
            "full_address": "Street 1, House 5",
            "district": "Yunusabad",
        },
    )

    addresses = StaffService.get_client_addresses(sample_user.id)

    assert created.id is not None
    assert len(addresses) == 1
    assert addresses[0].id == created.id
    assert addresses[0].title == "Home"
    assert addresses[0].full_address == "Street 1, House 5"


def test_staff_service_address_methods_raise_for_missing_user(db):
    with pytest.raises(NotFoundError):
        StaffService.add_client_address(999999, {"full_address": "X"})

    with pytest.raises(NotFoundError):
        StaffService.get_client_addresses(999999)


def test_get_cod_collection_projection_subtracts_reserved_prepayment(db, sample_order):
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("90000")

    payment = sample_order.payment or Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        amount=Decimal("90000"),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.PENDING,
        amount_collected=Decimal("0"),
        outstanding_amount=Decimal("90000"),
        provider_data={},
    )
    payment.provider_data = {"cod_prepayment_reserved_amount": 15000}
    payment.outstanding_amount = Decimal("90000")
    if sample_order.payment is None:
        db.session.add(payment)
    db.session.commit()

    projection = StaffService.get_cod_collection_projection(sample_order)

    assert projection["cod_reserved_prepayment_amount"] == 15000.0
    assert projection["expected_cash_to_collect"] == 75000.0


def test_get_cod_collection_projection_clamps_reserved_prepayment_to_outstanding(db, sample_order):
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("90000")

    payment = sample_order.payment or Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        amount=Decimal("90000"),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.PENDING,
        amount_collected=Decimal("0"),
        outstanding_amount=Decimal("5000"),
        provider_data={},
    )
    payment.provider_data = {"cod_prepayment_reserved_amount": 15000}
    payment.outstanding_amount = Decimal("5000")
    if sample_order.payment is None:
        db.session.add(payment)
    db.session.commit()

    projection = StaffService.get_cod_collection_projection(sample_order)

    assert projection["cod_reserved_prepayment_amount"] == 5000.0
    assert projection["expected_cash_to_collect"] == 0.0


def test_get_cod_collection_projection_keeps_zero_outstanding_without_total_fallback(db, sample_order):
    sample_order.payment_method = PaymentMethod.CASH
    sample_order.total_amount = Decimal("90000")

    payment = sample_order.payment or Payment(
        order_id=sample_order.id,
        user_id=sample_order.user_id,
        amount=Decimal("90000"),
        payment_method=PaymentMethod.CASH,
        status=PaymentStatus.COMPLETED,
        amount_collected=Decimal("90000"),
        outstanding_amount=Decimal("0"),
        provider_data={},
    )
    payment.provider_data = {"cod_prepayment_reserved_amount": 0}
    payment.outstanding_amount = Decimal("0")
    if sample_order.payment is None:
        db.session.add(payment)
    db.session.commit()

    projection = StaffService.get_cod_collection_projection(sample_order)

    assert projection["cod_reserved_prepayment_amount"] == 0.0
    assert projection["expected_cash_to_collect"] == 0.0


def test_create_client_user_rejects_unparseable_phone_instead_of_null_match(db):
    """Phone-validation SSOT None-guard.

    `format_phone_number("not-a-phone")` returns None. Without the guard the
    uniqueness check would run `filter_by(phone=None)` -> `WHERE phone IS NULL`
    and collide with an arbitrary NULL-phone user. The guard must raise
    ValidationError before any query, so the NULL-phone bait user is never
    matched and no duplicate-customer row is created.
    """
    # NULL-phone bait user: a telegram-only customer with no phone on file.
    bait = User(
        first_name="Bait",
        password_hash=hash_password("TestPassword123!"),
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE.value,
        telegram_id="999000111",
        registration_source="telegram",
    )
    db.session.add(bait)
    db.session.commit()
    assert bait.phone is None

    operator = _create_user(db, "+998903334444", UserRole.OPERATOR)

    with pytest.raises(ValidationError) as exc_info:
        StaffService.create_client_user(
            operator_id=operator.id,
            user_data={"phone": "not-a-phone", "first_name": "Customer"},
        )

    # It must be the invalid-format guard, not a downstream conflict from the
    # NULL-row match (which would surface as STAFF_PHONE_EXISTS).
    assert getattr(exc_info.value, "error_code", None) == "STAFF_PHONE_INVALID"

    # No customer was created for the unparseable phone.
    assert User.query.filter_by(first_name="Customer").first() is None
