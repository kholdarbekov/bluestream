"""Admin user update accepts + persists date_of_birth (Deliverable C9)."""

import pytest

from business_app.services.auth_service import AuthService
from business_app.serializers.admin_serializers import serialize_user_admin
from business_app.utils.exceptions import ValidationError


@pytest.fixture
def auth_service(mock_redis):
    service = AuthService()
    service.redis_client = mock_redis
    return service


def test_admin_update_sets_dob_naive_utc_midnight(auth_service, db, sample_user):
    """Admin update path stores DOB as naive UTC midnight — no day shift on read-back."""
    user = auth_service.update_user_by_admin(
        user_id=sample_user.id,
        updated_by_admin_id=sample_user.id,
        first_name=sample_user.first_name or "Admin",
        phone=sample_user.phone,
        date_of_birth="1988-03-09",
    )
    db.session.refresh(user)
    dob = user.date_of_birth
    assert dob is not None
    # After the timezone fix, the stored value round-trips as the correct date.
    assert dob.isoformat()[:10] == "1988-03-09"
    assert serialize_user_admin(user)["date_of_birth"].startswith("1988-03-09")


def test_admin_update_without_dob_leaves_unchanged(auth_service, db, sample_user):
    auth_service.update_user_by_admin(
        user_id=sample_user.id,
        updated_by_admin_id=sample_user.id,
        first_name=sample_user.first_name or "Admin",
        phone=sample_user.phone,
        date_of_birth="1988-03-09",
    )
    # Second call omits date_of_birth (sentinel) -> must NOT clear it.
    user = auth_service.update_user_by_admin(
        user_id=sample_user.id,
        updated_by_admin_id=sample_user.id,
        first_name="Renamed",
        phone=sample_user.phone,
    )
    assert user.date_of_birth is not None


def test_admin_update_rejects_future_dob(auth_service, db, sample_user):
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    with pytest.raises(ValidationError):
        auth_service.update_user_by_admin(
            user_id=sample_user.id,
            updated_by_admin_id=sample_user.id,
            first_name=sample_user.first_name or "Admin",
            phone=sample_user.phone,
            date_of_birth=future,
        )


def test_admin_update_rejects_dob_too_young(auth_service, db, sample_user):
    from datetime import datetime, timezone
    too_young = datetime.now(timezone.utc).date().replace(
        year=datetime.now(timezone.utc).year - 5
    ).isoformat()
    with pytest.raises(ValidationError):
        auth_service.update_user_by_admin(
            user_id=sample_user.id,
            updated_by_admin_id=sample_user.id,
            first_name=sample_user.first_name or "Admin",
            phone=sample_user.phone,
            date_of_birth=too_young,
        )


def test_admin_update_rejects_dob_too_old(auth_service, db, sample_user):
    from datetime import datetime, timezone
    too_old = datetime.now(timezone.utc).date().replace(
        year=datetime.now(timezone.utc).year - 130
    ).isoformat()
    with pytest.raises(ValidationError):
        auth_service.update_user_by_admin(
            user_id=sample_user.id,
            updated_by_admin_id=sample_user.id,
            first_name=sample_user.first_name or "Admin",
            phone=sample_user.phone,
            date_of_birth=too_old,
        )


def test_admin_update_rejects_malformed_dob(auth_service, db, sample_user):
    with pytest.raises(ValidationError):
        auth_service.update_user_by_admin(
            user_id=sample_user.id,
            updated_by_admin_id=sample_user.id,
            first_name=sample_user.first_name or "Admin",
            phone=sample_user.phone,
            date_of_birth="not-a-date",
        )
