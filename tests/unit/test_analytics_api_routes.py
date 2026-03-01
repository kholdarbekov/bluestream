"""Route-level regressions for analytics API endpoints used by admin UI."""

from datetime import UTC, datetime
from unittest.mock import Mock

from flask_jwt_extended import create_access_token


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=user_id, additional_claims={'role': 'admin'})
    return {'Authorization': f'Bearer {token}'}


def test_delivery_analytics_route_uses_explicit_date_range(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_delivery_analytics.return_value = {'performance': {'success_rate': 95}}
    monkeypatch.setattr('business_app.api.analytics.get_analytics_service', lambda: service)

    response = client.get(
        '/api/v1/analytics/delivery?start_date=2026-02-01&end_date=2026-02-28',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    start_date, end_date = service.get_delivery_analytics.call_args.args
    assert start_date == datetime(2026, 2, 1, tzinfo=UTC)
    assert end_date == datetime(2026, 2, 28, 23, 59, 59, 999999, tzinfo=UTC)


def test_products_route_supports_admin_ui_timeframe_requests(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_product_analytics.return_value = [{'product_id': 1, 'product_name': 'Water'}]
    monkeypatch.setattr('business_app.api.analytics.get_analytics_service', lambda: service)

    response = client.get(
        '/api/v1/analytics/products?timeframe=30d',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    assert response.get_json()['product_analytics'][0]['product_id'] == 1


def test_customers_route_supports_admin_ui_timeframe_requests(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_customer_analytics.return_value = {'acquisition': {'total_new_customers': 1}}
    monkeypatch.setattr('business_app.api.analytics.get_analytics_service', lambda: service)

    response = client.get(
        '/api/v1/analytics/customers?timeframe=30d',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    assert response.get_json()['customer_analytics']['acquisition']['total_new_customers'] == 1


def test_predictions_route_uses_revenue_prediction_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.predict_revenue.return_value = {'next_month_revenue': 100000}
    monkeypatch.setattr('business_app.api.analytics.get_analytics_service', lambda: service)

    response = client.get(
        '/api/v1/analytics/predictions?type=revenue&horizon=45',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.predict_revenue.assert_called_once_with(90)
    assert response.get_json()['predictions']['next_month_revenue'] == 100000


def test_predictions_route_shapes_churn_payload_for_admin_ui(client, app, admin_user, monkeypatch):
    service = Mock()
    service.predict_customer_churn.return_value = {
        'high_risk_customers': 2,
        'medium_risk_customers': 3,
        'predictions': [
            {
                'user_id': 7,
                'user_name': 'Jane Doe',
                'email': 'jane@example.com',
                'churn_probability': 0.81,
                'risk_level': 'high',
            }
        ],
    }
    monkeypatch.setattr('business_app.api.analytics.get_analytics_service', lambda: service)

    response = client.get(
        '/api/v1/analytics/predictions?type=churn',
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    payload = response.get_json()['predictions']
    assert payload['at_risk_count'] == 5
    assert payload['high_risk_count'] == 2
    assert payload['customers'][0]['risk_score'] == 81.0
