"""Route regressions for public payment API surfaces."""

from decimal import Decimal

from flask_jwt_extended import create_access_token

from business_app.models.payment import Payment
from shared.enums import PaymentMethod, PaymentStatus


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


def test_get_payment_statistics_delegates_to_service_and_preserves_shape(client, app, db, sample_user):
    """Fix A6: the handler must return the same shape as before the
    extraction of PaymentService.get_user_payment_statistics."""
    payment = Payment(
        user_id=sample_user.id,
        payment_method=PaymentMethod.CARD,
        amount=Decimal('12000.00'),
        currency='UZS',
        status=PaymentStatus.COMPLETED,
        payment_id='stats_test_payment_1',
    )
    db.session.add(payment)
    db.session.commit()

    response = client.get(
        '/api/v1/payments/statistics?period=all', headers=_auth_headers(app, sample_user.id)
    )

    assert response.status_code == 200
    payload = response.get_json()
    data = payload['data']

    assert data['period'] == 'all'
    stats = data['statistics']
    assert stats['total_payments'] == 1
    assert stats['successful_payments'] == 1
    # Flask's DefaultJSONProvider renders Decimal as str(); this is
    # pre-existing behaviour, unaffected by the service extraction.
    assert stats['total_amount'] == '12000.00'
    assert set(stats.keys()) == {
        'total_payments',
        'successful_payments',
        'failed_payments',
        'success_rate',
        'total_amount',
        'average_payment',
        'payment_methods',
        'monthly_spending_trend',
    }
    assert set(stats['payment_methods'].keys()) == {
        'instant',
        'card_payment',
        'digital_wallet',
        'points',
        'account_balance',
    }
