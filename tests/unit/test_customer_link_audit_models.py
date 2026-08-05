from datetime import datetime, UTC

import pytest

from business_app.models.user import User
from business_app.models.customer_link import (
    CanonicalCustomer, CustomerLinkEvent, CustomerDistinctPair,
)
from shared.enums import UserRole, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


@pytest.mark.unit
class TestLinkAuditModels:
    def test_link_event_roundtrip(self, db):
        admin = _user(db, "admin@example.com", "+998900000009")
        canonical = CanonicalCustomer(primary_user_id=admin.id)
        db.session.add(canonical); db.session.commit()
        ev = CustomerLinkEvent(event_type="link", canonical_customer_id=canonical.id,
                               acting_admin_id=admin.id, member_user_ids=[1, 2], reason="same person")
        db.session.add(ev); db.session.commit()
        got = CustomerLinkEvent.query.get(ev.id)
        assert got.event_type == "link"
        assert got.member_user_ids == [1, 2]
        assert got.reason == "same person"

    def test_distinct_pair_unique(self, db):
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        low, high = sorted([u1.id, u2.id])
        db.session.add(CustomerDistinctPair(user_id_low=low, user_id_high=high))
        db.session.commit()
        db.session.add(CustomerDistinctPair(user_id_low=low, user_id_high=high))
        with pytest.raises(Exception):  # IntegrityError (unique) — surfaces on flush/commit
            db.session.commit()
        db.session.rollback()
