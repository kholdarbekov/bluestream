"""Route regressions for public payment API surfaces."""

from flask_jwt_extended import create_access_token


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'customer'})
    return {'Authorization': f'Bearer {token}'}


def test_get_payment_methods_returns_only_supported_public_methods(client, app, sample_user):
    response = client.get('/api/v1/payments/methods', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    payload = response.get_json()
    methods = [item['method'] for item in payload['data']['available_methods']]

    assert methods == ['click', 'payme', 'cash']
    assert 'uzcard' not in methods
    assert 'humo' not in methods
