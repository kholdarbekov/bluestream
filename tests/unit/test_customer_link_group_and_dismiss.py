"""Dismiss-suggestion contract + the retirement pin for mark_address_group.

Phase 2: mark_address_group (single-cluster groups, CUSTOMER_LINK_ADDRESS_NOT_IN_CLUSTER
fence) is REPLACED by the ownerless place-group CRUD — see
tests/unit/test_place_group_crud.py for those contracts.
"""
from datetime import datetime, UTC

import pytest

from business_app.models.user import User
from business_app.models.customer_link import CustomerDistinctPair
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import UserRole, UserStatus, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


@pytest.mark.unit
class TestGroupAndDismiss:
    def test_mark_address_group_is_retired(self, db):
        # Phase 2 removed the single-cluster grouping API entirely.
        assert not hasattr(CustomerLinkService(), "mark_address_group")

    def test_dismiss_suggestion_normalizes_pair(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        svc = CustomerLinkService()
        # Dismiss with args in either order -> same normalized row.
        r1 = svc.dismiss_suggestion(u2.id, u1.id, actor_admin_id=admin.id)
        assert r1["user_id_low"] == min(u1.id, u2.id)
        assert r1["user_id_high"] == max(u1.id, u2.id)
        assert CustomerDistinctPair.query.count() == 1
        # Idempotent re-dismiss doesn't error or duplicate.
        svc.dismiss_suggestion(u1.id, u2.id, actor_admin_id=admin.id)
        assert CustomerDistinctPair.query.count() == 1
