"""
Unit tests for AuthService aligned with current implementation.
"""

import pytest
from unittest.mock import patch

from business_app.services.auth_service import AuthService
from business_app.utils.exceptions import ConflictError, UnauthorizedError


@pytest.fixture
def auth_service(mock_redis):
    service = AuthService()
    service.redis_client = mock_redis
    return service


@pytest.mark.unit
@pytest.mark.auth
class TestAuthService:
    def test_hash_and_verify_password(self, auth_service):
        password = "StrongPass123!"

        hashed = auth_service._hash_password(password)

        assert hashed != password
        assert auth_service._verify_password(password, hashed) is True
        assert auth_service._verify_password("WrongPass123!", hashed) is False

    def test_login_user_success(self, auth_service, app, db, sample_user):
        with (
            app.test_request_context("/api/v1/auth/login", method="POST"),
            patch.object(auth_service, "_create_user_session"),
        ):
            user, tokens = auth_service.login_user(sample_user.email, "TestPassword123!")

        assert user.id == sample_user.id
        assert "access_token" in tokens
        assert "refresh_token" in tokens

    def test_login_user_invalid_credentials(self, auth_service, db):
        with pytest.raises(UnauthorizedError):
            auth_service.login_user("missing@example.com", "bad-pass")

    def test_register_user_success(self, auth_service, app, db):
        with (
            app.test_request_context("/api/v1/auth/register", method="POST"),
            patch.object(auth_service, "_send_verification_notifications"),
            patch.object(auth_service, "_create_user_session"),
        ):
            user, tokens = auth_service.register_user(
                email="new-auth-user@example.com",
                password="StrongPass123!",
                phone="+998901111111",
                first_name="New",
                last_name="User",
            )

        assert user.id is not None
        assert user.email == "new-auth-user@example.com"
        assert "access_token" in tokens
        assert "refresh_token" in tokens

    def test_register_user_duplicate_email(self, auth_service, sample_user):
        with pytest.raises(ConflictError):
            auth_service.register_user(
                email=sample_user.email,
                password="StrongPass123!",
                phone="+998902222222",
                first_name="Dup",
                last_name="User",
            )

    def test_refresh_token_invalid(self, auth_service, app):
        with app.app_context():
            with pytest.raises(UnauthorizedError):
                auth_service.refresh_token("invalid-refresh-token")

    def test_logout_without_token_returns_true(self, auth_service):
        assert auth_service.logout_user() is True
