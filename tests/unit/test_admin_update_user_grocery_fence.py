"""Grocery flag-flip fence in `update_user_by_admin` (Phase 2 spec §5.8 layer 2).

`User.is_grocery_store` is a DERIVED property (`user_type` + `entity_subtype`),
not a column, so the layer-1 link/group fences (which check the flag at
link/group time) can be walked around: an admin can link/group an individual
first and only THEN flip them into a grocery entity. Grocery money is mirrored
onto a corporate contract, so a collection posted for a clustered/grouped
grocery account would settle the contract AND another person's COD debt — the
same money twice. This fence blocks the transition INTO entity/grocery status
while the account is clustered or owns a place-grouped address; the admin must
unlink / ungroup first.
"""

from datetime import datetime, UTC

import pytest

from business_app.models.user import User, UserAddress
from business_app.services.auth_service import AuthService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    db.session.add(a); db.session.commit()
    return a


def _admin(db):
    a = User(email="fence-admin@test.local", phone="+998900000099",
             password_hash=hash_password("TestPassword123!"),
             first_name="A", last_name="D", user_type=UserType.INDIVIDUAL, role=UserRole.ADMIN,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(a); db.session.commit()
    return a


def _flip_to_grocery(db, user):
    # A real admin row, not a magic id — SQLite runs with FKs OFF, so a dangling
    # updated_by_admin_id would pass here and violate the FK on dev Postgres.
    return AuthService().update_user_by_admin(
        user.id,
        first_name="T",
        updated_by_admin_id=_admin(db).id,
        user_type="entity",
        company_name="Shop",
        entity_subtype="grocery_store",
    )


@pytest.mark.unit
class TestGroceryFlagFlipFence:
    def test_blocked_while_linked(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        CustomerLinkService().link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")

        with pytest.raises(ValidationError) as exc:
            _flip_to_grocery(db, u1)
        assert exc.value.error_code == "GROCERY_FLAG_BLOCKED_WHILE_LINKED"
        db.session.rollback()
        assert User.query.get(u1.id).normalized_user_type == UserType.INDIVIDUAL.value

    def test_blocked_while_address_grouped(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        CustomerLinkService().create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="office")

        with pytest.raises(ValidationError) as exc:
            _flip_to_grocery(db, u1)   # u1 is UNLINKED but owns a grouped address
        assert exc.value.error_code == "GROCERY_FLAG_BLOCKED_WHILE_LINKED"

    def test_allowed_when_unlinked_and_ungrouped(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        _addr(db, u.id)  # plain, ungrouped address

        updated = _flip_to_grocery(db, u)
        assert updated.is_grocery_store is True

    def test_allowed_after_unlink_and_ungroup(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="office")

        svc.unlink_account(u1.id, actor_admin_id=admin.id, reason="fix")
        svc.remove_address_from_group(a1.id, acting_admin_id=admin.id, reason="fix")

        updated = _flip_to_grocery(db, u1)
        assert updated.is_grocery_store is True

    def test_non_type_update_of_linked_user_is_unaffected(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        CustomerLinkService().link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="r")

        updated = AuthService().update_user_by_admin(
            u1.id, first_name="Renamed", updated_by_admin_id=admin.id,
        )
        assert updated.first_name == "Renamed"
