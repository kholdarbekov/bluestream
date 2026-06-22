"""Integration tests for the require_loyalty_eligible API guard.

Verifies that:
- Ineligible ENTITY users (no corporate contract) get 403 with code "loyalty_not_available"
  on all guarded loyalty endpoints.
- INDIVIDUAL users are not blocked (status != 403).
- The three open config endpoints (/tiers, /programs, /tier-benefits) are not guarded.
"""

from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.user import User
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType

GUARDED_GET = [
    "/api/v1/loyalty/points",
    "/api/v1/loyalty/account",
    "/api/v1/loyalty/history",
    "/api/v1/loyalty/profile",
    "/api/v1/loyalty/points/history",
    "/api/v1/loyalty/rewards",
    "/api/v1/loyalty/rewards/history",
    "/api/v1/loyalty/referral",
    "/api/v1/loyalty/statistics",
]

OPEN_CONFIG = [
    "/api/v1/loyalty/tiers",
    "/api/v1/loyalty/programs",
    "/api/v1/loyalty/tier-benefits",
]

GUARDED_POST = [
    ("/api/v1/loyalty/earn-points", {"action": "test"}),
    ("/api/v1/loyalty/gift-points", {"recipient_phone": "+998901234567", "points_amount": 1}),
]


# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def make_user(app, db):
    """Factory: create a persisted User with the given user_type."""
    created = []

    def _factory(user_type: UserType) -> User:
        uid = uuid4().hex[:8]
        user = User(
            email=f"guard-test-{uid}@example.com",
            phone=f"+9989{uid[:8]}",
            password_hash=hash_password("TestPassword123!"),
            first_name="Guard",
            last_name="Test",
            user_type=user_type,
            role=UserRole.CUSTOMER,
            is_verified=True,
        )
        db.session.add(user)
        db.session.commit()
        created.append(user)
        return user

    yield _factory

    # Cleanup: best-effort; SQLite in-memory resets per session anyway
    for u in created:
        try:
            db.session.delete(u)
        except Exception:
            pass
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


@pytest.fixture
def auth_header_for(app):
    """Return a function that mints a JWT header dict for the given user."""

    def _make(user: User) -> dict:
        with app.app_context():
            token = create_access_token(identity=str(user.id))
        return {"Authorization": f"Bearer {token}"}

    return _make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", GUARDED_GET)
def test_ineligible_entity_user_gets_403(client, path, auth_header_for, make_user):
    entity = make_user(user_type=UserType.ENTITY)  # no contract -> ineligible
    resp = client.get(path, headers=auth_header_for(entity))
    assert resp.status_code == 403
    body = resp.get_json() or {}
    assert body.get("data", {}).get("code") == "loyalty_not_available"


@pytest.mark.parametrize("path", GUARDED_GET)
def test_individual_user_not_blocked(client, path, auth_header_for, make_user):
    user = make_user(user_type=UserType.INDIVIDUAL)
    resp = client.get(path, headers=auth_header_for(user))
    assert resp.status_code != 403


def test_open_config_endpoints_not_guarded(client, auth_header_for, make_user):
    entity = make_user(user_type=UserType.ENTITY)
    for path in OPEN_CONFIG:
        resp = client.get(path, headers=auth_header_for(entity))
        assert resp.status_code != 403, f"Expected open endpoint {path} to allow entity user, got {resp.status_code}"


@pytest.mark.parametrize("path,body", GUARDED_POST)
def test_ineligible_entity_user_gets_403_on_post(client, path, body, auth_header_for, make_user):
    entity = make_user(user_type=UserType.ENTITY)  # no contract -> ineligible
    resp = client.post(path, json=body, headers=auth_header_for(entity))
    assert resp.status_code == 403
    data = resp.get_json() or {}
    assert data.get("data", {}).get("code") == "loyalty_not_available"
