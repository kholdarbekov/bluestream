"""DOB validation hardening for AuthService.update_user_profile_data (Deliverable C1)."""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.services.auth_service import AuthService
from business_app.utils.exceptions import ValidationError


@pytest.fixture
def auth_service(mock_redis):
    service = AuthService()
    service.redis_client = mock_redis
    return service


def test_valid_dob_is_stored_as_naive_utc_midnight(auth_service):
    """_parse_validate_dob must return a naive datetime at midnight with no tzinfo.

    Previously it returned a tz-aware local-midnight (+05:00) value which, when
    written to a timestamptz column in a UTC session, was stored as the *previous*
    day at 19:00 UTC.  The fix: return a naive datetime so Postgres treats it as
    UTC midnight and round-trips losslessly.
    """
    result = auth_service._parse_validate_dob("2003-05-22")
    assert result.tzinfo is None, "must be naive (no tzinfo)"
    assert (result.year, result.month, result.day, result.hour, result.minute) == (
        2003, 5, 22, 0, 0,
    )


def test_valid_dob_round_trip_no_day_shift(auth_service, db, sample_user):
    """Storing then re-reading date_of_birth via the DB must return the same date.

    This is the regression test for the day-shift bug: the old tz-aware storage
    caused a -1 day shift on every read, and a second re-save shifted another day.
    After the fix, read-back must show exactly '2003-05-22'.
    """
    auth_service.update_user_profile_data(
        sample_user.id, {"date_of_birth": "2003-05-22"}
    )
    db.session.flush()
    db.session.expire(sample_user, ["date_of_birth"])
    # Force a real DB re-read in the UTC session.
    db.session.refresh(sample_user)
    dob = sample_user.date_of_birth
    assert dob is not None
    assert dob.isoformat()[:10] == "2003-05-22", (
        f"Day-shift bug: stored date came back as {dob.isoformat()[:10]!r} "
        f"instead of '2003-05-22'"
    )


def test_valid_dob_round_trip_idempotent_on_resave(auth_service, db, sample_user):
    """Re-feeding the read-back value through _parse_validate_dob must not shift the date.

    The old tz-aware code shifted -1 day per round-trip; admin re-submits caused
    -2 total (user entered 22, saw 20 after two saves).  After the fix, the
    tz-aware read-back string '2003-05-22T00:00:00+00:00' must parse back to
    date(2003, 5, 22) and produce a naive midnight with no further drift.
    """
    auth_service.update_user_profile_data(
        sample_user.id, {"date_of_birth": "2003-05-22"}
    )
    db.session.refresh(sample_user)
    dob = sample_user.date_of_birth
    assert dob is not None
    # Simulate what the admin DatePicker submits on re-save: isoformat of the DB value.
    readback_string = dob.isoformat()
    result = auth_service._parse_validate_dob(readback_string)
    assert (result.year, result.month, result.day) == (2003, 5, 22), (
        f"Idempotency broken: re-parsing '{readback_string}' gave "
        f"{result.year}-{result.month:02d}-{result.day:02d}"
    )


def test_clearing_dob_with_empty_string_sets_none(auth_service, db, sample_user):
    auth_service.update_user_profile_data(sample_user.id, {"date_of_birth": "1990-05-17"})
    auth_service.update_user_profile_data(sample_user.id, {"date_of_birth": ""})
    db.session.refresh(sample_user)
    assert sample_user.date_of_birth is None


def test_future_dob_is_rejected(auth_service, db, sample_user):
    future = (datetime.now(timezone.utc) + timedelta(days=2)).date().isoformat()
    with pytest.raises(ValidationError):
        auth_service.update_user_profile_data(sample_user.id, {"date_of_birth": future})


def test_too_young_dob_is_rejected(auth_service, db, sample_user):
    too_young = (datetime.now(timezone.utc).date().replace(
        year=datetime.now(timezone.utc).year - 5
    )).isoformat()
    with pytest.raises(ValidationError):
        auth_service.update_user_profile_data(sample_user.id, {"date_of_birth": too_young})


def test_too_old_dob_is_rejected(auth_service, db, sample_user):
    too_old = (datetime.now(timezone.utc).date().replace(
        year=datetime.now(timezone.utc).year - 130
    )).isoformat()
    with pytest.raises(ValidationError):
        auth_service.update_user_profile_data(sample_user.id, {"date_of_birth": too_old})


def test_malformed_dob_is_rejected(auth_service, db, sample_user):
    with pytest.raises(ValidationError):
        auth_service.update_user_profile_data(sample_user.id, {"date_of_birth": "17-05-1990"})
