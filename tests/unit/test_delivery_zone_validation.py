"""Delivery-zone (TASHKENT_POLYGON) single-source-of-truth enforcement.

Covers the SSOT helper, the UserAddress model backstop, and the service-layer
write paths. Regression coverage for the bug where a coordinate outside the
delivery polygon could be persisted (telegram bot / staff bot / admin).
"""

import pytest

from business_app.models.user import User, UserAddress
from business_app.services.auth_service import AuthService
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import ValidationError
from business_app.utils.geo_validation import ensure_within_delivery_zone, is_in_delivery_zone
from shared.enums import UserRole, UserType

IN_ZONE = (41.31, 69.28)          # central Tashkent
OUT_OF_ZONE = (39.6270, 66.9750)  # Samarkand — far outside the coverage polygon

_user_seq = 0


def _make_user(db):
    """Create a unique customer user for a test."""
    global _user_seq
    _user_seq += 1
    user = User(
        email=f"zone-user-{_user_seq}@example.com",
        phone=f"+99890000{_user_seq:04d}",
        password_hash="x",
        first_name="Zone",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


class TestGeoValidationHelper:
    def test_in_zone_passes(self, db):
        ensure_within_delivery_zone(*IN_ZONE)  # must not raise
        assert is_in_delivery_zone(*IN_ZONE) is True

    def test_out_of_zone_raises(self, db):
        with pytest.raises(ValidationError):
            ensure_within_delivery_zone(*OUT_OF_ZONE)
        assert is_in_delivery_zone(*OUT_OF_ZONE) is False

    def test_missing_coordinate_is_noop(self, db):
        # Text-only addresses (no GPS) are out of scope — must not raise.
        ensure_within_delivery_zone(None, None)
        ensure_within_delivery_zone(IN_ZONE[0], None)
        ensure_within_delivery_zone(None, IN_ZONE[1])

    def test_non_numeric_raises(self, db):
        with pytest.raises(ValidationError):
            ensure_within_delivery_zone("abc", "def")


class TestModelBackstop:
    def test_insert_out_of_zone_rejected(self, db):
        user = _make_user(db)
        db.session.add(
            UserAddress(user_id=user.id, full_address="x", latitude=OUT_OF_ZONE[0], longitude=OUT_OF_ZONE[1])
        )
        with pytest.raises(ValidationError):
            db.session.commit()
        db.session.rollback()

    def test_insert_in_zone_ok(self, db):
        user = _make_user(db)
        addr = UserAddress(user_id=user.id, full_address="x", latitude=IN_ZONE[0], longitude=IN_ZONE[1])
        db.session.add(addr)
        db.session.commit()
        assert addr.id is not None

    def test_text_only_address_ok(self, db):
        user = _make_user(db)
        addr = UserAddress(user_id=user.id, full_address="No coordinates here")
        db.session.add(addr)
        db.session.commit()
        assert addr.id is not None

    def test_update_coords_to_out_of_zone_rejected(self, db):
        user = _make_user(db)
        addr = UserAddress(user_id=user.id, full_address="x", latitude=IN_ZONE[0], longitude=IN_ZONE[1])
        db.session.add(addr)
        db.session.commit()

        addr.latitude, addr.longitude = OUT_OF_ZONE
        with pytest.raises(ValidationError):
            db.session.commit()
        db.session.rollback()

    def test_update_non_coord_field_on_legacy_out_of_zone_row_allowed(self, db):
        # Simulate a row created before enforcement by inserting via SQLAlchemy
        # core, which bypasses the ORM before_insert event.
        user = _make_user(db)
        db.session.execute(
            UserAddress.__table__.insert().values(
                user_id=user.id,
                full_address="legacy",
                latitude=OUT_OF_ZONE[0],
                longitude=OUT_OF_ZONE[1],
            )
        )
        db.session.commit()

        addr = UserAddress.query.filter_by(user_id=user.id).first()
        addr.title = "Renamed"  # no coordinate change
        db.session.commit()  # must NOT raise — coords unchanged
        assert addr.title == "Renamed"


class TestServiceWritePaths:
    def test_auth_service_add_out_of_zone_rejected(self, db):
        user = _make_user(db)
        service = AuthService()
        with pytest.raises(ValidationError):
            service.add_user_address(
                user.id, {"title": "Home", "latitude": OUT_OF_ZONE[0], "longitude": OUT_OF_ZONE[1]}
            )

    def test_auth_service_add_in_zone_ok(self, db):
        user = _make_user(db)
        service = AuthService()
        addr = service.add_user_address(
            user.id, {"title": "Home", "latitude": IN_ZONE[0], "longitude": IN_ZONE[1]}
        )
        assert addr.id is not None

    def test_auth_service_update_out_of_zone_rejected(self, db):
        user = _make_user(db)
        service = AuthService()
        addr = service.add_user_address(
            user.id, {"title": "Home", "latitude": IN_ZONE[0], "longitude": IN_ZONE[1]}
        )
        with pytest.raises(ValidationError):
            service.update_user_address(
                user.id, addr.id, {"latitude": OUT_OF_ZONE[0], "longitude": OUT_OF_ZONE[1]}
            )
        db.session.rollback()

    def test_staff_service_add_client_address_out_of_zone_rejected(self, db):
        user = _make_user(db)
        with pytest.raises(ValidationError):
            StaffService.add_client_address(
                user.id,
                {"title": "Home", "full_address": "x", "latitude": OUT_OF_ZONE[0], "longitude": OUT_OF_ZONE[1]},
            )
        db.session.rollback()

    def test_staff_service_add_client_address_in_zone_ok(self, db):
        user = _make_user(db)
        addr = StaffService.add_client_address(
            user.id,
            {"title": "Home", "full_address": "x", "latitude": IN_ZONE[0], "longitude": IN_ZONE[1]},
        )
        assert addr.id is not None
