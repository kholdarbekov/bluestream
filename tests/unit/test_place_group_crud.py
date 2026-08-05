from datetime import datetime, UTC

import pytest

from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.user import User, UserAddress
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, UserRole, UserStatus, UserType


def _user(db, email, phone, *, user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
          entity_subtype=None, company_name=None):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=user_type, role=role,
             entity_subtype=entity_subtype, company_name=company_name,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    db.session.add(a); db.session.commit()
    return a


@pytest.mark.unit
class TestCreatePlaceGroup:
    def test_groups_two_unlinked_customers(self, db):
        """UC3: distinct, UNLINKED people share a place — no cluster required."""
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="same office", label="office"
        )

        db.session.refresh(a1); db.session.refresh(a2)
        assert isinstance(group, AddressGroup)
        assert group.canonical_customer_id is None          # ownerless by construction
        assert a1.address_group_id == a2.address_group_id == group.id
        event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
        assert event.canonical_customer_id is None
        assert event.member_user_ids == sorted([u1.id, u2.id])
        assert event.reason.startswith(f"[group {group.id}]")
        assert "same office" in event.reason

    def test_distinct_pair_does_not_block_grouping(self, db):
        """DistinctPair gates LINK only — coworkers dismissed as different
        people must still be groupable (spec §3)."""
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        svc.dismiss_suggestion(u1.id, u2.id, actor_admin_id=admin.id)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)

        group = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="coworkers")

        db.session.refresh(a1)
        assert a1.address_group_id == group.id

    def test_rejects_grocery_member(self, db):
        grocery = _user(db, "g@example.com", "+998900000003", user_type=UserType.ENTITY,
                        entity_subtype=EntitySubtype.GROCERY_STORE, company_name="Shop")
        u = _user(db, "a@example.com", "+998900000001")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1, a2 = _addr(db, grocery.id), _addr(db, u.id)
        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "PLACE_GROUP_GROCERY_MEMBER"
        db.session.rollback()
        assert UserAddress.query.get(a2.id).address_group_id is None  # no partial mutation

    def test_rejects_entity_member(self, db):
        entity = _user(db, "e@example.com", "+998900000003", user_type=UserType.ENTITY,
                       entity_subtype=EntitySubtype.WORKPLACE, company_name="Acme")
        u = _user(db, "a@example.com", "+998900000001")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1, a2 = _addr(db, entity.id), _addr(db, u.id)
        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "PLACE_GROUP_ENTITY_MEMBER"

    def test_rejects_already_grouped_address(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        a1, a2, a3 = _addr(db, u1.id), _addr(db, u2.id), _addr(db, u3.id)
        svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="first")
        with pytest.raises(ValidationError) as exc:
            svc.create_place_group([a1.id, a3.id], acting_admin_id=admin.id, reason="second")
        assert exc.value.error_code == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"

    def test_rejects_missing_address_and_single_address(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1 = _addr(db, u.id)
        svc = CustomerLinkService()
        with pytest.raises(ValidationError) as exc:
            svc.create_place_group([a1.id, 999999], acting_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "CUSTOMER_LINK_ADDRESS_NOT_FOUND"
        with pytest.raises(ValidationError) as exc:
            svc.create_place_group([a1.id], acting_admin_id=admin.id, reason="r")
        assert exc.value.error_code == "PLACE_GROUP_MIN_ADDRESSES"

    def test_long_reason_is_truncated_to_column_width(self, db):
        """CustomerLinkEvent.reason is String(500) and the 2c admin UI allows a
        full 500-char reason — the "[group <id>] " prefix would otherwise
        overflow the column (StringDataRightTruncation on Postgres).
        """
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="R" * 500
        )

        event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
        assert len(event.reason) == 500
        assert event.reason.startswith(f"[group {group.id}] RRR")


@pytest.mark.unit
class TestAddAndReadPlaceGroup:
    def test_add_addresses_to_group_appends_and_audits(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        a1, a2, a3 = _addr(db, u1.id), _addr(db, u2.id), _addr(db, u3.id)
        group = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="office")

        result = svc.add_addresses_to_group(group.id, [a3.id], acting_admin_id=admin.id, reason="new hire")

        db.session.refresh(a3)
        assert result.id == group.id
        assert a3.address_group_id == group.id
        event = CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").one()
        assert event.member_user_ids == [u3.id]
        assert event.reason.startswith(f"[group {group.id}]")

    def test_add_rejects_already_grouped_address(self, db):
        """The add path must run the same fences as create — proves
        _assert_place_group_eligible is actually wired into add_addresses_to_group.
        """
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        u4 = _user(db, "d@example.com", "+998900000004")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        a1, a2, a3, a4 = _addr(db, u1.id), _addr(db, u2.id), _addr(db, u3.id), _addr(db, u4.id)
        first = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="first")
        second = svc.create_place_group([a3.id, a4.id], acting_admin_id=admin.id, reason="second")

        with pytest.raises(ValidationError) as exc:
            svc.add_addresses_to_group(second.id, [a1.id], acting_admin_id=admin.id, reason="move")

        assert exc.value.error_code == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"
        db.session.rollback()
        assert UserAddress.query.get(a1.id).address_group_id == first.id  # unmoved

    def test_add_to_missing_group_raises(self, db):
        u = _user(db, "a@example.com", "+998900000001")
        admin = _user(db, "admin@example.com", "+998900000009")
        a1 = _addr(db, u.id)
        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().add_addresses_to_group(
                999999, [a1.id], acting_admin_id=admin.id, reason="r"
            )
        assert exc.value.error_code == "PLACE_GROUP_NOT_FOUND"

    def test_get_place_group_user_ids_sorted_distinct_owners(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        a1b = _addr(db, u1.id)  # second address of u1 at the same place
        group = svc.create_place_group([a1.id, a2.id, a1b.id], acting_admin_id=admin.id, reason="r")

        assert svc.get_place_group_user_ids(group.id) == sorted([u1.id, u2.id])
        assert svc.get_place_group_user_ids(999999) == []

    def test_get_place_group_user_ids_returns_empty_for_missing_group(self, db):
        """group_id=None must not compile to `address_group_id IS NULL` and
        collapse every ungrouped address's owner into the result."""
        u1 = _user(db, "a@example.com", "+998900000001")
        _addr(db, u1.id)  # ungrouped address owned by u1

        assert CustomerLinkService().get_place_group_user_ids(None) == []

    def test_add_empty_address_list_is_noop_without_audit_event(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        group = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="office")
        event_count_before = CustomerLinkEvent.query.count()

        result = svc.add_addresses_to_group(group.id, [], acting_admin_id=admin.id, reason="no-op")

        assert result.id == group.id
        assert CustomerLinkEvent.query.count() == event_count_before
