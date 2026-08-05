from datetime import datetime, UTC, timedelta

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.customer_link import (
    CanonicalCustomer, CustomerLinkEvent, CustomerDistinctPair, AddressGroup,
)
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import UserRole, UserType, UserStatus
from business_app.utils.password_security import hash_password
from business_app.utils.exceptions import ValidationError


def _user(db, email, phone, *, created=None, user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=user_type, role=role,
             status=UserStatus.ACTIVE, is_verified=True,
             created_at=created or datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


@pytest.mark.unit
class TestLinkAccounts:
    def test_link_two_unlinked_creates_canonical_with_oldest_primary(self, db):
        older = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=10))
        newer = _user(db, "b@example.com", "+998900000002", created=datetime.now(UTC))
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)

        svc = CustomerLinkService()
        result = svc.link_accounts(newer.id, older.id, actor_admin_id=admin.id, reason="same person")

        db.session.refresh(older); db.session.refresh(newer)
        assert older.canonical_customer_id == newer.canonical_customer_id is not None
        assert result["primary_user_id"] == older.id  # oldest ACTIVE member
        assert sorted(result["member_user_ids"]) == sorted([older.id, newer.id])
        assert result["already_linked"] is False
        ev = CustomerLinkEvent.query.filter_by(event_type="link").one()
        assert ev.acting_admin_id == admin.id and ev.reason == "same person"

    def test_third_phone_coalesces_into_existing_cluster(self, db):
        u1 = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=10))
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        svc = CustomerLinkService()
        r1 = svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        r2 = svc.link_accounts(u2.id, u3.id, actor_admin_id=admin.id, reason="r")
        # Same canonical, never a new one / chain.
        assert r2["canonical_customer_id"] == r1["canonical_customer_id"]
        assert sorted(r2["member_user_ids"]) == sorted([u1.id, u2.id, u3.id])

    def test_distinct_pair_hard_blocks_link(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        low, high = sorted([u1.id, u2.id])
        db.session.add(CustomerDistinctPair(user_id_low=low, user_id_high=high, dismissed_by_admin_id=admin.id))
        db.session.commit()
        svc = CustomerLinkService()
        with pytest.raises(ValidationError) as exc:
            svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "CUSTOMER_LINK_DISTINCT_CONFLICT"

    def test_entity_cannot_be_linked(self, db):
        indiv = _user(db, "a@example.com", "+998900000001")
        entity = _user(db, "e@example.com", "+998900000002", user_type=UserType.ENTITY)
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        svc = CustomerLinkService()
        with pytest.raises(ValidationError) as exc:
            svc.link_accounts(indiv.id, entity.id, actor_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "CUSTOMER_LINK_NOT_INDIVIDUAL"

    def test_same_cluster_relink_is_idempotent(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        svc = CustomerLinkService()
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        again = svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        assert again["already_linked"] is True

    def test_coalesce_two_clusters_larger_wins(self, db):
        u1 = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=10))
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        u4 = _user(db, "d@example.com", "+998900000004")
        u5 = _user(db, "e@example.com", "+998900000005")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        svc = CustomerLinkService()

        # Cluster A = {u1, u2, u3} (size 3)
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        a_result = svc.link_accounts(u2.id, u3.id, actor_admin_id=admin.id, reason="r")
        a_canonical_id = a_result["canonical_customer_id"]

        # Cluster B = {u4, u5} (size 2)
        b_result = svc.link_accounts(u4.id, u5.id, actor_admin_id=admin.id, reason="r")
        b_canonical_id = b_result["canonical_customer_id"]

        result = svc.link_accounts(u3.id, u4.id, actor_admin_id=admin.id, reason="merge clusters")

        for u in (u1, u2, u3, u4, u5):
            db.session.refresh(u)

        canonical_ids = {u.canonical_customer_id for u in (u1, u2, u3, u4, u5)}
        assert canonical_ids == {a_canonical_id}
        assert result["canonical_customer_id"] == a_canonical_id
        assert sorted(result["member_user_ids"]) == sorted([u1.id, u2.id, u3.id, u4.id, u5.id])
        assert result["primary_user_id"] == u1.id
        assert User.query.filter_by(canonical_customer_id=b_canonical_id).count() == 0

    def test_coalesce_leaves_place_groups_untouched(self, db):
        """Phase 2: place groups are ownerless — cluster coalescing must not
        write to AddressGroup rows at all."""
        u1 = _user(db, "a@example.com", "+998900000001", created=datetime.now(UTC) - timedelta(days=10))
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        u4 = _user(db, "d@example.com", "+998900000004")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        svc = CustomerLinkService()

        a_result = svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        svc.link_accounts(u3.id, u4.id, actor_admin_id=admin.id, reason="r")

        a3 = UserAddress(user_id=u3.id, full_address="x", city="Tashkent",
                         latitude=41.31, longitude=69.28)
        a4 = UserAddress(user_id=u4.id, full_address="y", city="Tashkent",
                         latitude=41.31, longitude=69.28)
        db.session.add_all([a3, a4]); db.session.commit()
        group = svc.create_place_group([a3.id, a4.id], acting_admin_id=admin.id, reason="same home")

        result = svc.link_accounts(u2.id, u3.id, actor_admin_id=admin.id, reason="merge clusters")

        db.session.refresh(group); db.session.refresh(a3); db.session.refresh(a4)
        assert result["canonical_customer_id"] == a_result["canonical_customer_id"]
        assert group.canonical_customer_id is None            # never repointed
        assert a3.address_group_id == group.id                # membership intact
        assert a4.address_group_id == group.id

    def test_coalesce_blocked_by_transitive_distinct_pair(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        u4 = _user(db, "d@example.com", "+998900000004")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        svc = CustomerLinkService()

        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        svc.link_accounts(u3.id, u4.id, actor_admin_id=admin.id, reason="r")

        low, high = sorted([u1.id, u4.id])
        db.session.add(CustomerDistinctPair(user_id_low=low, user_id_high=high, dismissed_by_admin_id=admin.id))
        db.session.commit()

        with pytest.raises(ValidationError) as exc:
            svc.link_accounts(u2.id, u3.id, actor_admin_id=admin.id, reason="merge clusters")
        assert exc.value.error_code == "CUSTOMER_LINK_DISTINCT_CONFLICT"

    def test_link_rejects_grocery_account(self, db):
        from shared.enums import EntitySubtype
        grocery = _user(db, "g@example.com", "+998900000005",
                        user_type=UserType.ENTITY, role=UserRole.CUSTOMER)
        grocery.entity_subtype = EntitySubtype.GROCERY_STORE
        grocery.company_name = "Shop"
        db.session.commit()
        u = _user(db, "a@example.com", "+998900000001")
        admin = _user(db, "admin@example.com", "+998900000009", role=UserRole.ADMIN, user_type=UserType.STAFF)
        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().link_accounts(u.id, grocery.id, actor_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "CUSTOMER_LINK_GROCERY_ACCOUNT"
