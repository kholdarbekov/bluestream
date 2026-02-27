"""Service-layer tests for staff API boundary refactors."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.staff_service import StaffService
from business_app.utils.constants import OrderStatus, UserRole
from business_app.utils.exceptions import NotFoundError
from business_app.utils.password_security import hash_password


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
