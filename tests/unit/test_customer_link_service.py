from datetime import datetime, UTC

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import UserRole, UserType
from business_app.utils.password_security import hash_password


def _make_user(db, email, phone):
    user = User(
        email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
        first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _addr(db, user_id, group_id=None):
    a = UserAddress(user_id=user_id, full_address="x, Tashkent", city="Tashkent",
                    latitude=41.31, longitude=69.28, address_group_id=group_id)
    db.session.add(a)
    db.session.commit()
    return a


@pytest.mark.unit
class TestCustomerLinkResolvers:
    def test_unlinked_user_resolves_to_self(self, db):
        u = _make_user(db, "a@example.com", "+998900000001")
        svc = CustomerLinkService()
        assert svc.get_cluster_user_ids(u.id) == [u.id]
        assert svc.resolve_canonical(u.id) is None

    def test_missing_user_resolves_to_self_list(self, db):
        svc = CustomerLinkService()
        assert svc.get_cluster_user_ids(999999) == [999999]
        assert svc.resolve_canonical(999999) is None

    def test_linked_users_resolve_to_full_cluster(self, db):
        u1 = _make_user(db, "a@example.com", "+998900000001")
        u2 = _make_user(db, "b@example.com", "+998900000002")
        u3 = _make_user(db, "c@example.com", "+998900000003")
        canonical = CanonicalCustomer(primary_user_id=u1.id)
        db.session.add(canonical)
        db.session.commit()
        for u in (u1, u2, u3):
            u.canonical_customer_id = canonical.id
        db.session.commit()

        svc = CustomerLinkService()
        expected = sorted([u1.id, u2.id, u3.id])
        assert svc.get_cluster_user_ids(u2.id) == expected
        assert svc.resolve_canonical(u3.id) == canonical.id

    def test_address_group_union(self, db):
        u1 = _make_user(db, "a@example.com", "+998900000001")
        canonical = CanonicalCustomer(primary_user_id=u1.id)
        db.session.add(canonical)
        db.session.commit()
        u1.canonical_customer_id = canonical.id
        db.session.commit()
        group = AddressGroup(canonical_customer_id=canonical.id, label="home")
        db.session.add(group)
        db.session.commit()

        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u1.id, group_id=group.id)
        a3 = _addr(db, u1.id, group_id=None)  # ungrouped

        svc = CustomerLinkService()
        assert svc.get_address_group_member_ids(a1.id) == sorted([a1.id, a2.id])
        assert svc.get_address_group_member_ids(a3.id) == [a3.id]
