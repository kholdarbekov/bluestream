from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import OrderStatus, UserRole, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


@pytest.mark.unit
class TestOrderSummaryGroupUnion:
    def test_summary_balance_is_the_place_balance(self, db):
        """Was `test_summary_balance_is_group_union`, which seeded 3 on u1's pair
        and 2 on u2's and asserted the summary unioned them to 5.

        The coworkers now share ONE pooled row of 5. The assertion — an order
        placed from phone-2 reports the empties physically at that home, not
        phone-2's slice — is unchanged, and so is the number.
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
        a1 = UserAddress(user_id=u1.id, full_address="home", city="Tashkent",
                         latitude=41.31, longitude=69.28, address_group_id=group.id)
        a2 = UserAddress(user_id=u2.id, full_address="home", city="Tashkent",
                         latitude=41.31, longitude=69.28, address_group_id=group.id)
        db.session.add_all([a1, a2]); db.session.commit()
        # ONE row per place: grouped => keyed by address_group_id, address_id NULL.
        db.session.add(BottleBalance(address_group_id=group.id, balance=Decimal("5")))
        db.session.commit()

        order = Order(user_id=u2.id, order_number="ORD-SUM", status=OrderStatus.DELIVERED,
                      subtotal=Decimal("0"), delivery_fee=Decimal("0"), discount_amount=Decimal("0"),
                      loyalty_discount=Decimal("0"), total_amount=Decimal("0"),
                      delivery_address_id=a2.id, created_at=datetime.now(UTC))
        db.session.add(order); db.session.commit()

        summary = BottleTrackingService.get_order_bottle_summary(order)
        assert summary["balance"] == Decimal("5")
