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


def test_get_loyalty_member_transactions_route_delegates_to_service(client, app, admin_user, monkeypatch):
    service = Mock()
    service.get_member_transactions.return_value = {
        "items": [{"id": 1, "points": 50, "description": "txn"}],
        "total": 1,
        "page": 2,
        "per_page": 10,
    }
    monkeypatch.setattr("business_app.api.admin.AdminLoyaltyService", service)

    response = client.get(
        "/api/v1/admin/loyalty/members/8/transactions?page=2&per_page=10",
        headers=_auth_headers(app, admin_user.id),
    )

    assert response.status_code == 200
    service.get_member_transactions.assert_called_once_with(8, page=2, per_page=10)
    payload = response.get_json()
    assert payload["data"]["items"][0]["description"] == "txn"
    assert payload["meta"]["total"] == 1


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


# ============================================================================
# STREAK RULES ENDPOINTS
# ============================================================================


def _make_default_program(db_session):
    """Helper: ensure a default LoyaltyProgram exists and return it."""
    from business_app.models.loyalty import LoyaltyProgram

    program = LoyaltyProgram(
        name="Default",
        description="Default program",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
    )
    db_session.add(program)
    db_session.commit()
    return program


def test_create_and_list_streak_rule(client, app, admin_user, db):
    _make_default_program(db.session)
    headers = _auth_headers(app, admin_user.id)

    payload = {"name": "5 in 30", "required_orders": 5, "window_days": 30, "bonus_points": 200}
    r = client.post("/api/v1/admin/loyalty/streak-rules", json=payload, headers=headers)
    assert r.status_code == 201, r.get_json()
    data = r.get_json()["data"]
    assert "streak_rule" in data
    assert data["streak_rule"]["name"] == "5 in 30"
    assert data["streak_rule"]["bonus_points"] == 200

    r2 = client.get("/api/v1/admin/loyalty/streak-rules", headers=headers)
    assert r2.status_code == 200
    names = [x["name"] for x in r2.get_json()["data"]["streak_rules"]]
    assert "5 in 30" in names


def test_create_streak_rule_rejects_bad_values(client, app, admin_user, db):
    _make_default_program(db.session)
    headers = _auth_headers(app, admin_user.id)

    # required_orders = 0 should fail
    r = client.post(
        "/api/v1/admin/loyalty/streak-rules",
        json={"name": "x", "required_orders": 0, "window_days": 30, "bonus_points": 100},
        headers=headers,
    )
    assert r.status_code == 400

    # window_days = 0 should fail
    r = client.post(
        "/api/v1/admin/loyalty/streak-rules",
        json={"name": "x", "required_orders": 5, "window_days": 0, "bonus_points": 100},
        headers=headers,
    )
    assert r.status_code == 400

    # bonus_points = 0 should fail
    r = client.post(
        "/api/v1/admin/loyalty/streak-rules",
        json={"name": "x", "required_orders": 5, "window_days": 30, "bonus_points": 0},
        headers=headers,
    )
    assert r.status_code == 400


def test_list_streak_rules_filtered_by_program_id(client, app, admin_user, db):
    from business_app.models.loyalty import LoyaltyProgram, LoyaltyStreakRule

    prog1 = LoyaltyProgram(name="Prog1", is_active=True, is_default=True, uzs_per_point=250)
    prog2 = LoyaltyProgram(name="Prog2", is_active=True, is_default=False, uzs_per_point=250)
    db.session.add_all([prog1, prog2])
    db.session.commit()

    rule1 = LoyaltyStreakRule(
        program_id=prog1.id, name="Rule Prog1", required_orders=3, window_days=14, bonus_points=50
    )
    rule2 = LoyaltyStreakRule(
        program_id=prog2.id, name="Rule Prog2", required_orders=5, window_days=30, bonus_points=100
    )
    db.session.add_all([rule1, rule2])
    db.session.commit()

    headers = _auth_headers(app, admin_user.id)
    r = client.get(f"/api/v1/admin/loyalty/streak-rules?program_id={prog2.id}", headers=headers)
    assert r.status_code == 200
    names = [x["name"] for x in r.get_json()["data"]["streak_rules"]]
    assert "Rule Prog2" in names
    assert "Rule Prog1" not in names


def test_create_streak_rule_missing_required_fields(client, app, admin_user, db):
    _make_default_program(db.session)
    headers = _auth_headers(app, admin_user.id)

    # Missing bonus_points
    r = client.post(
        "/api/v1/admin/loyalty/streak-rules",
        json={"name": "incomplete", "required_orders": 5, "window_days": 30},
        headers=headers,
    )
    assert r.status_code == 400


def test_create_streak_rule_no_default_program(client, app, admin_user, db):
    # No program seeded — should get a validation error
    headers = _auth_headers(app, admin_user.id)
    r = client.post(
        "/api/v1/admin/loyalty/streak-rules",
        json={"name": "orphan", "required_orders": 3, "window_days": 7, "bonus_points": 50},
        headers=headers,
    )
    assert r.status_code == 400


def test_update_and_delete_streak_rule(client, app, admin_user, db):
    _make_default_program(db.session)
    headers = _auth_headers(app, admin_user.id)
    rid = client.post(
        "/api/v1/admin/loyalty/streak-rules",
        json={"name": "tmp", "required_orders": 3, "window_days": 30, "bonus_points": 300},
        headers=headers,
    ).get_json()["data"]["streak_rule"]["id"]

    u = client.put(
        f"/api/v1/admin/loyalty/streak-rules/{rid}",
        json={"bonus_points": 450, "is_active": False},
        headers=headers,
    )
    assert u.status_code == 200
    assert u.get_json()["data"]["streak_rule"]["bonus_points"] == 450

    d = client.delete(f"/api/v1/admin/loyalty/streak-rules/{rid}", headers=headers)
    assert d.status_code == 200


def test_update_program_surprise_reward_config(client, app, admin_user, db):
    """Surprise reward params are admin-configurable via the program update endpoint."""
    from business_app.models.loyalty import LoyaltyProgram

    prog = LoyaltyProgram(name="SR Prog", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(prog)
    db.session.commit()

    headers = _auth_headers(app, admin_user.id)
    r = client.put(
        f"/api/v1/admin/loyalty/programs/{prog.id}",
        headers=headers,
        json={
            "surprise_enabled": False,
            "surprise_chance_percent": 10,
            "surprise_amounts": "25,75",
            "surprise_cooldown_days": 14,
            "surprise_daily_cap": 3,
        },
    )

    assert r.status_code == 200
    data = r.get_json()["data"]["program"]
    assert data["surprise_enabled"] is False
    assert data["surprise_chance_percent"] == 10
    assert data["surprise_amounts"] == "25,75"
    assert data["surprise_cooldown_days"] == 14
    assert data["surprise_daily_cap"] == 3

    db.session.refresh(prog)
    assert prog.surprise_enabled is False
    assert prog.surprise_amounts == "25,75"
    assert prog.surprise_daily_cap == 3
