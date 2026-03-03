"""Regression tests for JWT subject consistency in staff auth flow."""

from flask_jwt_extended import create_refresh_token, decode_token
from business_app.utils.constants import UserStatus


def test_global_identity_loader_normalizes_integer_identity(app):
    """JWT manager should normalize any integer identity to string."""
    with app.app_context():
        token = create_refresh_token(identity=123)
        decoded = decode_token(token)

    assert decoded['sub'] == '123'
    assert isinstance(decoded['sub'], str)


def test_staff_refresh_token_has_string_subject(app, db, sample_user):
    """Staff refresh token generation keeps `sub` as string for staff contexts."""
    with app.test_request_context('/api/v1/staff/auth/refresh'):
        from business_app.services.token_service import TokenService
        sample_user.status = UserStatus.ACTIVE.value
        sample_user.staff_roles = ['driver']
        db.session.commit()
        tokens = TokenService().generate_tokens(sample_user, additional_claims={'staff_roles': ['driver']})
        decoded = decode_token(tokens['refresh_token'])

    assert decoded['sub'] == str(sample_user.id)
    assert isinstance(decoded['sub'], str)
    assert decoded.get('user_id') == sample_user.id


def test_refresh_access_token_accepts_active_enum_status(app, db, sample_user):
    """Refreshing should work for normal enum-backed active users."""
    with app.test_request_context('/api/v1/staff/auth/refresh'):
        from business_app.services.token_service import TokenService

        sample_user.status = UserStatus.ACTIVE
        sample_user.staff_roles = ['driver']
        db.session.commit()

        token_service = TokenService()
        tokens = token_service.generate_tokens(sample_user, additional_claims={'staff_roles': ['driver']})
        refreshed = token_service.refresh_access_token(tokens['refresh_token'])
        decoded = decode_token(refreshed['access_token'])

    assert refreshed['access_token']
    assert decoded['sub'] == str(sample_user.id)
    assert decoded['status'] == UserStatus.ACTIVE.value
