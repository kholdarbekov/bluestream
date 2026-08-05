from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance
from business_app.api.staff import _customer_bottle_balance
from shared.enums import OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


@pytest.mark.unit
class TestReturnAnchorGroupUnion:
    def test_anchor_reads_the_place_when_ordering_from_second_phone(self, db):
        """Was `test_anchor_sums_group_when_ordering_from_second_phone`, which
        seeded 3 on u1's pair + 2 on u2's and asserted the driver saw the union 5
        rather than phone-2's slice of 2.

        There is no slice any more — the coworkers share ONE pooled row of 5.
        The driver-facing number (5.0) and the cross-phone intent are unchanged.
        """
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        canonical = CanonicalCustomer(primary_user_id=u1.id)
        db.session.add(canonical); db.session.commit()
        u1.canonical_customer_id = canonical.id
        u2.canonical_customer_id = canonical.id
        db.session.commit()
        group = AddressGroup(canonical_customer_id=canonical.id, label="home")
        db.session.add(group); db.session.commit()

        a1 = UserAddress(user_id=u1.id, full_address="home, Tashkent", city="Tashkent",
                         latitude=41.31, longitude=69.28, address_group_id=group.id)
        a2 = UserAddress(user_id=u2.id, full_address="home, Tashkent", city="Tashkent",
                         latitude=41.31, longitude=69.28, address_group_id=group.id)
        db.session.add_all([a1, a2]); db.session.commit()
        # ONE row per place: grouped => keyed by address_group_id, address_id NULL.
        db.session.add(BottleBalance(address_group_id=group.id, balance=Decimal("5")))
        db.session.commit()

        # Order placed from phone-2 to the same home.
        order = Order(user_id=u2.id, order_number="ORD-A2", status=OrderStatus.DELIVERED,
                      subtotal=Decimal("0"), delivery_fee=Decimal("0"), discount_amount=Decimal("0"),
                      loyalty_discount=Decimal("0"), total_amount=Decimal("0"),
                      delivery_address_id=a2.id, created_at=datetime.now(UTC))
        db.session.add(order); db.session.commit()

        # Driver sees the true empties at that home (5), not phone-2's slice (2).
        assert _customer_bottle_balance(order) == 5.0

    def test_anchor_zero_for_no_address(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        order = Order(user_id=u.id, order_number="ORD-NA", status=OrderStatus.DELIVERED,
                      subtotal=Decimal("0"), delivery_fee=Decimal("0"), discount_amount=Decimal("0"),
                      loyalty_discount=Decimal("0"), total_amount=Decimal("0"),
                      delivery_address_id=None, created_at=datetime.now(UTC))
        db.session.add(order); db.session.commit()
        assert _customer_bottle_balance(order) == 0.0
