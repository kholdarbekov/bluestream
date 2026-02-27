"""Route-level regressions for loyalty API delegation."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token

from business_app.utils.exceptions import NotFoundError, ValidationError


class _Reward:
    def __init__(self):
        self.id = 1
        self.points_cost = 200
        self.min_order_value = None
        self.discount_type = 'fixed'
        self.discount_value = None
        self.image_url = None
        self.terms_conditions = None
        self.valid_from = None
        self.valid_until = None
        self.reward_type = 'voucher'

    def get_translated(self, field: str, _language: str):
        values = {
            'name': 'Reward Test',
            'description': 'Reward description',
        }
        return values[field]


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def test_get_loyalty_points_route_delegates_to_service(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_points_summary_for_user.return_value = {
        'points_balance': 120,
        'lifetime_points': 500,
        'tier': 'Bronze',
        'next_tier_threshold': 200,
    }
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)

    response = client.get('/api/v1/loyalty/points', headers=_auth_headers(app, sample_user.id))

    assert response.status_code == 200
    service.get_points_summary_for_user.assert_called_once()
    assert int(service.get_points_summary_for_user.call_args.args[0]) == sample_user.id


def test_get_points_history_route_maps_validation_error(client, app, sample_user, monkeypatch):
    service = Mock()
    service.get_filtered_points_history_for_user.side_effect = ValidationError('Invalid transaction type')
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)

    response = client.get(
        '/api/v1/loyalty/points/history?type=bad-type',
        headers=_auth_headers(app, sample_user.id),
    )

    assert response.status_code == 400


def test_redeem_reward_route_sends_notification(client, app, sample_user, monkeypatch):
    service = Mock()
    service.redeem_reward_for_user.return_value = {
        'reward': _Reward(),
        'redemption': {
            'id': 10,
            'points_spent': 200,
            'status': 'pending',
            'redemption_code': 'RWD123',
            'expires_at': None,
        },
        'remaining_points': 300,
    }
    notification_service = Mock()

    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)
    monkeypatch.setattr('business_app.api.loyalty.get_notification_service', lambda: notification_service)

    response = client.post(
        '/api/v1/loyalty/rewards/1/redeem',
        headers=_auth_headers(app, sample_user.id),
        json={},
    )

    assert response.status_code == 201
    service.redeem_reward_for_user.assert_called_once()
    notification_service.send_notification.assert_called_once()


def test_gift_points_route_maps_not_found(client, app, sample_user, monkeypatch):
    service = Mock()
    service.gift_points_by_phone.side_effect = NotFoundError('Recipient not found')
    monkeypatch.setattr('business_app.api.loyalty.get_loyalty_service', lambda: service)

    response = client.post(
        '/api/v1/loyalty/gift-points',
        headers=_auth_headers(app, sample_user.id),
        json={'recipient_phone': '+998900000000', 'points_amount': 10},
    )

    assert response.status_code == 404
