from datetime import datetime, UTC

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from shared.enums import UserRole, UserType
from business_app.utils.password_security import hash_password


def _make_user(db, email, phone):
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="Test",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.mark.unit
class TestCustomerLinkModels:
    def test_canonical_customer_links_users_and_address_group(self, db):
        u1 = _make_user(db, "a@example.com", "+998900000001")
        u2 = _make_user(db, "b@example.com", "+998900000002")

        canonical = CanonicalCustomer(primary_user_id=u1.id, created_by_admin_id=u1.id, notes="same person")
        db.session.add(canonical)
        db.session.commit()

        u1.canonical_customer_id = canonical.id
        u2.canonical_customer_id = canonical.id
        db.session.commit()

        group = AddressGroup(canonical_customer_id=canonical.id, label="home")
        db.session.add(group)
        db.session.commit()

        addr = UserAddress(
            user_id=u1.id,
            full_address="1 Test St, Tashkent",
            city="Tashkent",
            latitude=41.3111,
            longitude=69.2797,
            address_group_id=group.id,
        )
        db.session.add(addr)
        db.session.commit()

        # Round-trip
        members = User.query.filter(User.canonical_customer_id == canonical.id).order_by(User.id).all()
        assert [m.id for m in members] == sorted([u1.id, u2.id])
        assert UserAddress.query.get(addr.id).address_group_id == group.id
        assert CanonicalCustomer.query.get(canonical.id).primary_user_id == u1.id

    def test_unlinked_user_has_null_canonical(self, db):
        u = _make_user(db, "c@example.com", "+998900000003")
        assert u.canonical_customer_id is None
