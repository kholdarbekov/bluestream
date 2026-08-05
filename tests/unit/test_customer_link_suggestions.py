from datetime import datetime, UTC

import pytest

from business_app.models.user import User, UserAddress
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import UserRole, UserStatus, UserType
from business_app.utils.password_security import hash_password


def _user(db, email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.commit()
    return u


def _addr(db, user_id, lat, lng):
    a = UserAddress(user_id=user_id, full_address="x", city="Tashkent",
                    latitude=lat, longitude=lng)
    db.session.add(a); db.session.commit()
    return a


@pytest.mark.unit
class TestSuggestions:
    def test_nearby_account_is_suggested(self, db):
        target = _user(db, "t@example.com", "+998900000001")
        near = _user(db, "n@example.com", "+998900000002")
        far = _user(db, "f@example.com", "+998900000003")
        _addr(db, target.id, 41.3110, 69.2790)
        _addr(db, near.id, 41.3111, 69.2791)   # ~15 m away
        # Far outside the 50 m suggestion radius (~10 km), but still inside
        # TASHKENT_POLYGON so the UserAddress delivery-zone backstop
        # (ensure_within_delivery_zone) doesn't reject the fixture itself.
        _addr(db, far.id, 41.4000, 69.3000)

        suggestions = CustomerLinkService().get_link_suggestions(target.id)
        ids = [s["user_id"] for s in suggestions]
        assert near.id in ids
        assert far.id not in ids

    def test_dismissed_pair_is_excluded(self, db):
        target = _user(db, "t@example.com", "+998900000001")
        near = _user(db, "n@example.com", "+998900000002")
        admin = _user(db, "admin@example.com", "+998900000009")
        _addr(db, target.id, 41.3110, 69.2790)
        _addr(db, near.id, 41.3111, 69.2791)
        CustomerLinkService().dismiss_suggestion(target.id, near.id, actor_admin_id=admin.id)
        ids = [s["user_id"] for s in CustomerLinkService().get_link_suggestions(target.id)]
        assert near.id not in ids

    def test_shared_office_is_dampened(self, db):
        # 6 coworkers at one geolocation -> high shared_geo_customer_count -> low score.
        target = _user(db, "t@example.com", "+998900000001")
        _addr(db, target.id, 41.3110, 69.2790)
        coworkers = []
        for i in range(6):
            c = _user(db, f"c{i}@example.com", f"+99890010000{i}")
            _addr(db, c.id, 41.3110, 69.2790)  # exact same point
            coworkers.append(c)
        suggestions = CustomerLinkService().get_link_suggestions(target.id)
        by_id = {s["user_id"]: s for s in suggestions}
        # Each coworker records a high shared-geo count and correspondingly low score.
        for c in coworkers:
            assert by_id[c.id]["shared_geo_customer_count"] >= 6
