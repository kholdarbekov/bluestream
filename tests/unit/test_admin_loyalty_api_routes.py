"""Route-level regressions for admin loyalty management endpoints."""

from unittest.mock import Mock

from flask_jwt_extended import create_access_token


def _auth_headers(app, user_id: int) -> dict:
    with app.app_context():
        token = create_access_token(identity=str(user_id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_get_loyalty_members_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.list_members.return_value = {
        "items": [{"id": 1, "user_id": 7, "customer_name": "Test User", "current_balance": 120}],
        "page": 2,
        "per_page": 10,
        "total": 14,
        "summary": {"total_members": 14, "total_points_in_circulation": 920},
    }
    monkeypatch.setattr("business_app.api.admin.AdminLoyaltyService", service)

    response = client.get(
        "/api/v1/admin/loyalty/members?page=2&per_page=10&search=test&program_id=3&tier=Gold",
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.list_members.assert_called_once_with(
        page=2,
        per_page=10,
        search="test",
        program_id=3,
        tier="Gold",
    )
    payload = response.get_json()
    assert payload["data"]["items"][0]["customer_name"] == "Test User"
    assert payload["meta"]["summary"]["total_points_in_circulation"] == 920


def test_loyalty_customers_alias_route_uses_member_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.list_members.return_value = {
        "items": [{"id": 3, "user_id": 9, "customer_name": "Alias User"}],
        "page": 1,
        "per_page": 20,
        "total": 1,
        "summary": {"total_members": 1},
    }
    monkeypatch.setattr("business_app.api.admin.AdminLoyaltyService", service)

    response = client.get(
        "/api/v1/admin/loyalty-customers",
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.list_members.assert_called_once_with(
        page=1,
        per_page=20,
        search="",
        program_id=None,
        tier=None,
    )
    payload = response.get_json()
    assert payload["data"]["items"][0]["customer_name"] == "Alias User"


def test_get_loyalty_member_detail_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_member_detail.return_value = {
        "member": {"user_id": 8, "customer_name": "Detail User"},
        "recent_transactions": [],
        "recent_redemptions": [],
        "referral_statistics": {},
        "tier_progress": {},
        "streak": {},
    }
    monkeypatch.setattr("business_app.api.admin.AdminLoyaltyService", service)

    response = client.get(
        "/api/v1/admin/loyalty/members/8",
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.get_member_detail.assert_called_once_with(8)
    payload = response.get_json()
    assert payload["data"]["member"]["customer_name"] == "Detail User"


def test_get_loyalty_analytics_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_analytics.return_value = {
        "summary": {"total_members": 2},
        "tier_distribution": [{"tier": "Gold", "count": 1}],
        "top_rewards": [],
        "points_trend": [],
        "redemption_metrics": {"points_earned": 500},
        "program_breakdown": [],
    }
    monkeypatch.setattr("business_app.api.admin.AdminLoyaltyService", service)

    response = client.get(
        "/api/v1/admin/loyalty/analytics?start_date=2026-02-01&end_date=2026-02-29&program_id=4",
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.get_analytics.assert_called_once_with(
        start_date="2026-02-01",
        end_date="2026-02-29",
        program_id=4,
    )
    payload = response.get_json()
    assert payload["data"]["summary"]["total_members"] == 2
