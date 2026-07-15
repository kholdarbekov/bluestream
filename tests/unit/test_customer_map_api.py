from unittest.mock import Mock
from flask_jwt_extended import create_access_token
from shared.enums import UserRole


def _headers(app, user_id, role):
    with app.app_context():
        token = create_access_token(identity=str(user_id),
                                    additional_claims={'role': role.value})
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def test_map_pins_returns_camelcase_pins(client, app, admin_user, monkeypatch):
    from datetime import datetime
    from decimal import Decimal
    monkeypatch.setattr(
        'business_app.services.customer_map_service.CustomerMapService.get_customer_map_pins',
        Mock(return_value=[{
            "address_id": 3, "user_id": 9, "full_name": "Jasur T", "phone": "+998900000009",
            "user_type": "individual", "entity_subtype": None, "lat": 41.3, "lng": 69.25,
            "is_default": True, "address_label": "Yunusobod 4", "address_index": 1, "address_count": 1,
            "last_order_date": datetime(2026, 7, 10, 8, 0, 0), "order_count": 3,
            "bottle_balance": Decimal("2"), "outstanding_debt": Decimal("0"),
            "active_cod_debt_count": 0, "cod_restricted": False,
        }]),
    )
    resp = client.get('/api/v1/admin/customers/map-pins',
                      headers=_headers(app, admin_user.id, UserRole.ADMIN))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    pins = body['data']['pins']
    assert len(pins) == 1
    assert pins[0]['userId'] == 9
    assert pins[0]['bottleBalance'] == 2.0
    assert pins[0]['addressLabel'] == "Yunusobod 4"
    assert isinstance(pins[0]['lastOrderDate'], str)          # ISO string, not RFC-1123
    assert pins[0]['lastOrderDate'].startswith('2026-07-10')


def test_map_pins_requires_view_users_permission(client, app, db):
    from business_app.models.user import User
    from shared.enums import UserRole, UserType
    from business_app.utils.password_security import hash_password
    driver = User(email='driver.map@ex.com', phone='+998900000010',
                  password_hash=hash_password('Passw0rd123!'), first_name='Drv', last_name='One',
                  user_type=UserType.STAFF, role=UserRole.DELIVERY_DRIVER, is_verified=True)
    db.session.add(driver); db.session.commit()
    resp = client.get('/api/v1/admin/customers/map-pins',
                      headers=_headers(app, driver.id, UserRole.DELIVERY_DRIVER))
    assert resp.status_code == 403


def test_map_pins_requires_auth(app):
    # Fresh client: the session-scoped `client` fixture can leak JWT cookies from
    # earlier tests, turning a "no auth" request into an authenticated one.
    resp = app.test_client().get('/api/v1/admin/customers/map-pins')
    assert resp.status_code == 401
