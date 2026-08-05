from datetime import datetime, UTC

import pytest

from business_app.models.user import User
from business_app.models.loyalty import ReferralProgram
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
class TestReferralAtLink:
    def test_pending_intracluster_referral_is_voided(self, db):
        referrer = _user(db, "a@example.com", "+998900000001")
        referee = _user(db, "b@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        ref = ReferralProgram(referrer_id=referrer.id, referee_id=referee.id,
                              referral_code="R1", status="pending",
                              referrer_bonus_points=100, referee_bonus_points=50)
        db.session.add(ref)
        referee.referred_by_user_id = referrer.id
        db.session.commit()

        CustomerLinkService().link_accounts(referrer.id, referee.id, actor_admin_id=admin.id, reason="same person")

        db.session.refresh(ref)
        assert ref.status == "void"
