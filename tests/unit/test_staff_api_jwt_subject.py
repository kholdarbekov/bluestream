"""Regression tests for JWT subject consistency in staff auth flow."""

from flask_jwt_extended import create_refresh_token, decode_token


def test_global_identity_loader_normalizes_integer_identity(app):
    """JWT manager should normalize any integer identity to string."""
    with app.app_context():
        token = create_refresh_token(identity=123)
        decoded = decode_token(token)

    assert decoded['sub'] == '123'
    assert isinstance(decoded['sub'], str)


def test_staff_refresh_token_has_string_subject(app, client, db, sample_user):
    """Staff refresh must return an access token with string `sub` claim."""
    with app.app_context():
        from business_app.services.token_service import TokenService
        sample_user.staff_roles = ['driver']
        db.session.commit()
        tokens = TokenService().generate_tokens(sample_user, additional_claims={'staff_roles': ['driver']})
        refresh_token = tokens['refresh_token']

    response = client.post(
        '/api/v1/staff/auth/refresh',
        headers={'Authorization': f'Bearer {refresh_token}'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    access_token = payload['data']['access_token']

    with app.app_context():
        decoded = decode_token(access_token)

    assert decoded['sub'] == str(sample_user.id)
    assert isinstance(decoded['sub'], str)
    assert decoded.get('staff_roles') == ['driver']
