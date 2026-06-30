"""End-to-end middleware regression test for the date_of_birth day-shift bug.

WHY THIS FILE EXISTS
--------------------
The existing unit tests in tests/unit/test_auth_service_profile_dob.py call
AuthService directly — they bypass the Flask before_request hook.  That means
they cannot detect the middleware bug where ``TimezoneMiddleware._convert_request_datetimes``
rewrites ``date_of_birth`` from "2003-05-22" to "2003-05-21T19:00:00+00:00"
(Asia/Tashkent +05:00 midnight → UTC) before the route/service ever sees it.

These tests use app.test_client() so the full before_request chain executes,
giving end-to-end coverage of the middleware fix.

RED state (before fix): PUT /api/v1/auth/profile { "date_of_birth": "2003-05-22" }
  → middleware converts to "2003-05-21T19:00:00+00:00"
  → _parse_validate_dob("2003-05-21T19:00:00+00:00").date() == 2003-05-21
  → GET returns "2003-05-21T00:00:00" (one day behind)

GREEN state (after fix): date_of_birth excluded from datetime_fields
  → _parse_validate_dob("2003-05-22") == datetime(2003, 5, 22, 0, 0)
  → GET returns "2003-05-22T00:00:00" (correct)
"""

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.user import User
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


# ---------------------------------------------------------------------------
# Local fixtures — function-scoped so each test gets a clean user + client
# ---------------------------------------------------------------------------

@pytest.fixture
def dob_user(app):
    """A real persisted INDIVIDUAL/CUSTOMER user for DOB round-trip tests."""
    import uuid
    uid = uuid.uuid4().hex[:8]
    with app.app_context():
        user = User(
            email=f"dob-e2e-{uid}@example.com",
            phone=f"+9989{uid[:8]}",
            password_hash=hash_password("TestPassword123!"),
            first_name="Dob",
            last_name="Test",
            user_type=UserType.INDIVIDUAL,
            role=UserRole.CUSTOMER,
            is_verified=True,
        )
        _db.session.add(user)
        _db.session.commit()
        # Yield user.id so we can re-query inside app context in each test
        user_id = user.id
    return user_id


@pytest.fixture
def dob_auth_headers(app, dob_user):
    """Bearer header for the dob_user, minted inside the app context."""
    with app.app_context():
        token = create_access_token(
            identity=str(dob_user),
            additional_claims={"type": "access", "role": UserRole.CUSTOMER.value},
        )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Middleware e2e tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_dob_put_get_round_trip_no_day_shift(app, db, dob_user, dob_auth_headers):
    """PUT date_of_birth=2003-05-22 → GET must return 2003-05-22, not 2003-05-21.

    This test exercises the TimezoneMiddleware before_request hook.  Without
    the fix, the middleware converts the bare date string to UTC (Asia/Tashkent
    +05:00 → -1 day) and the stored value is shifted one day backwards.
    """
    client = app.test_client()

    put_resp = client.put(
        "/api/v1/auth/profile",
        json={"date_of_birth": "2003-05-22"},
        headers={**dob_auth_headers, "Content-Type": "application/json"},
    )
    assert put_resp.status_code == 200, (
        f"PUT /profile failed: {put_resp.status_code} {put_resp.get_json()}"
    )

    get_resp = client.get("/api/v1/auth/profile", headers=dob_auth_headers)
    assert get_resp.status_code == 200, (
        f"GET /profile failed: {get_resp.status_code} {get_resp.get_json()}"
    )
    profile = get_resp.get_json()
    # The profile is nested under data -> ...
    dob_value = _extract_dob(profile)
    assert dob_value is not None, f"date_of_birth missing from response: {profile}"
    assert dob_value[:10] == "2003-05-22", (
        f"Day-shift bug: middleware corrupted date_of_birth. "
        f"Expected '2003-05-22', got {dob_value!r}"
    )


@pytest.mark.integration
def test_dob_put_get_second_date_no_day_shift(app, db, dob_user, dob_auth_headers):
    """Second case: 1999-01-11 must round-trip as 1999-01-11."""
    client = app.test_client()

    put_resp = client.put(
        "/api/v1/auth/profile",
        json={"date_of_birth": "1999-01-11"},
        headers={**dob_auth_headers, "Content-Type": "application/json"},
    )
    assert put_resp.status_code == 200, (
        f"PUT /profile failed: {put_resp.status_code} {put_resp.get_json()}"
    )

    get_resp = client.get("/api/v1/auth/profile", headers=dob_auth_headers)
    assert get_resp.status_code == 200
    profile = get_resp.get_json()
    dob_value = _extract_dob(profile)
    assert dob_value is not None, f"date_of_birth missing from response: {profile}"
    assert dob_value[:10] == "1999-01-11", (
        f"Day-shift bug on second case: expected '1999-01-11', got {dob_value!r}"
    )


@pytest.mark.integration
def test_middleware_unit_dob_field_not_converted(app):
    """Targeted middleware unit test: date_of_birth must pass through unconverted.

    Instantiates TimezoneMiddleware and calls _convert_request_datetimes directly
    with a known Asia/Tashkent timezone set on g.  date_of_birth must be unchanged
    while a real datetime field (created_at) IS converted, proving the exclusion
    is targeted and not a blanket disable.
    """
    import pytz
    from business_app.middleware.timezone_middleware import TimezoneMiddleware

    middleware = TimezoneMiddleware()

    with app.test_request_context("/"):
        from flask import g
        g.user_timezone = pytz.timezone("Asia/Tashkent")

        payload = {
            "date_of_birth": "2003-05-22",
            "created_at": "2003-05-22T10:00:00",
        }
        middleware._convert_request_datetimes(payload)

    # date_of_birth must be untouched — the middleware must not tz-shift a pure date
    assert payload["date_of_birth"] == "2003-05-22", (
        f"Middleware shifted date_of_birth: {payload['date_of_birth']!r} "
        f"(should be '2003-05-22')"
    )
    # created_at IS a datetime field and must have been converted
    assert payload["created_at"] != "2003-05-22T10:00:00", (
        f"created_at was NOT converted by middleware (datetime fields must still be converted)"
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _extract_dob(response_json):
    """Extract date_of_birth from various nesting shapes the profile endpoint uses."""
    if not response_json:
        return None
    # GET /profile: {"success": true, "data": {"date_of_birth": ...}}
    # PUT /profile: {"success": true, "data": {"user": {"date_of_birth": ...}}}
    data = response_json.get("data", response_json)
    if isinstance(data, dict):
        if "date_of_birth" in data:
            return data["date_of_birth"]
        user = data.get("user", {})
        if isinstance(user, dict) and "date_of_birth" in user:
            return user["date_of_birth"]
    return None
