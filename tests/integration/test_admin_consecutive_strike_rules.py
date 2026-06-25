from business_app import db
from business_app.models.loyalty import LoyaltyProgram, LoyaltyStreakRule


def _seed(app):
    with app.app_context():
        p = LoyaltyProgram.query.filter_by(is_default=True).first()
        if not p:
            p = LoyaltyProgram(name="Default", is_active=True, is_default=True)
            db.session.add(p)
            db.session.commit()
        s = LoyaltyStreakRule(
            program_id=p.id, name="3 in 30", required_orders=3,
            window_days=30, bonus_points=300, is_active=True,
        )
        db.session.add(s)
        db.session.commit()
        return p.id, s.id


def test_create_list_update_delete(app, admin_auth_headers):
    program_id, strike_id = _seed(app)

    # Create
    resp = app.test_client().post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "6-in-a-row Champion",
            "required_consecutive": 6,
            "combine_mode": "all",
            "bonus_points": 1000,
            "strike_rule_ids": [strike_id],
            "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 201, resp.get_json()
    rule = resp.get_json()["data"]["consecutive_strike_rule"]
    assert rule["strike_rule_ids"] == [strike_id]
    rule_id = rule["id"]

    # List
    resp = app.test_client().get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program_id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["count"] == 1

    # Update
    resp = app.test_client().put(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        json={"required_consecutive": 3, "combine_mode": "any"},
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["consecutive_strike_rule"]["required_consecutive"] == 3

    # Delete
    resp = app.test_client().delete(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200


def test_create_rejects_empty_strikes(app, admin_auth_headers):
    program_id, _ = _seed(app)
    resp = app.test_client().post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "bad", "required_consecutive": 6, "combine_mode": "all",
            "bonus_points": 1000, "strike_rule_ids": [], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400


def test_create_rejects_invalid_combine_mode(app, admin_auth_headers):
    program_id, strike_id = _seed(app)
    resp = app.test_client().post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "bad-mode", "required_consecutive": 3, "combine_mode": "sometimes",
            "bonus_points": 100, "strike_rule_ids": [strike_id], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400, resp.get_json()


def test_create_rejects_non_numeric_required_consecutive(app, admin_auth_headers):
    program_id, strike_id = _seed(app)
    resp = app.test_client().post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "bad-type", "required_consecutive": "abc", "combine_mode": "all",
            "bonus_points": 100, "strike_rule_ids": [strike_id], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400, resp.get_json()


def test_create_rejects_strike_from_different_program(app, admin_auth_headers):
    """A strike belonging to another program must be rejected with 400."""
    from business_app.models.loyalty import LoyaltyProgram, LoyaltyStreakRule

    program_id, _ = _seed(app)  # sets up program1 + strike under it

    # Create a second program and a strike under it
    with app.app_context():
        p2 = LoyaltyProgram(name="Second Program", is_active=True, is_default=False)
        db.session.add(p2)
        db.session.commit()
        s2 = LoyaltyStreakRule(
            program_id=p2.id, name="Other strike", required_orders=2,
            window_days=14, bonus_points=50, is_active=True,
        )
        db.session.add(s2)
        db.session.commit()
        other_strike_id = s2.id

    resp = app.test_client().post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "cross-program", "required_consecutive": 2, "combine_mode": "all",
            "bonus_points": 50, "strike_rule_ids": [other_strike_id], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 400, resp.get_json()


def test_put_rejects_negative_bonus_points_no_mutation(app, admin_auth_headers):
    """PUT with bonus_points < 0 returns 400 and the rule is not mutated."""
    program_id, strike_id = _seed(app)

    # First create a rule
    client = app.test_client()
    create_resp = client.post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "mutation-guard", "required_consecutive": 3, "combine_mode": "all",
            "bonus_points": 200, "strike_rule_ids": [strike_id], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.get_json()
    rule_id = create_resp.get_json()["data"]["consecutive_strike_rule"]["id"]

    # Attempt invalid PUT
    put_resp = client.put(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        json={"bonus_points": -5},
        headers=admin_auth_headers,
    )
    assert put_resp.status_code == 400, put_resp.get_json()

    # Confirm the rule's bonus_points is unchanged
    get_resp = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program_id}",
        headers=admin_auth_headers,
    )
    rules = get_resp.get_json()["data"]["consecutive_strike_rules"]
    matched = [r for r in rules if r["id"] == rule_id]
    assert matched, "Rule should still exist"
    assert matched[0]["bonus_points"] == 200, (
        f"bonus_points was mutated to {matched[0]['bonus_points']} (expected 200)"
    )


def test_put_invalid_combine_mode_does_not_mutate_name(app, admin_auth_headers):
    """PUT with valid name + invalid combine_mode must return 400 and leave name unchanged."""
    program_id, strike_id = _seed(app)

    client = app.test_client()
    create_resp = client.post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "original-name", "required_consecutive": 4, "combine_mode": "all",
            "bonus_points": 150, "strike_rule_ids": [strike_id], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.get_json()
    rule_id = create_resp.get_json()["data"]["consecutive_strike_rule"]["id"]

    # PUT with valid name but invalid combine_mode — must be rejected entirely
    put_resp = client.put(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        json={"name": "new-name", "combine_mode": "sometimes"},
        headers=admin_auth_headers,
    )
    assert put_resp.status_code == 400, put_resp.get_json()

    # Name must remain unchanged in the DB
    get_resp = client.get(
        f"/api/v1/admin/loyalty/consecutive-strike-rules?program_id={program_id}",
        headers=admin_auth_headers,
    )
    rules = get_resp.get_json()["data"]["consecutive_strike_rules"]
    matched = [r for r in rules if r["id"] == rule_id]
    assert matched, "Rule should still exist"
    assert matched[0]["name"] == "original-name", (
        f"name was mutated to '{matched[0]['name']}' (expected 'original-name')"
    )


def test_put_non_numeric_display_order_returns_400(app, admin_auth_headers):
    """PUT with display_order='abc' must return 400, not 500."""
    program_id, strike_id = _seed(app)

    client = app.test_client()
    create_resp = client.post(
        "/api/v1/admin/loyalty/consecutive-strike-rules",
        json={
            "name": "display-order-test", "required_consecutive": 2, "combine_mode": "any",
            "bonus_points": 50, "strike_rule_ids": [strike_id], "program_id": program_id,
        },
        headers=admin_auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.get_json()
    rule_id = create_resp.get_json()["data"]["consecutive_strike_rule"]["id"]

    put_resp = client.put(
        f"/api/v1/admin/loyalty/consecutive-strike-rules/{rule_id}",
        json={"display_order": "abc"},
        headers=admin_auth_headers,
    )
    assert put_resp.status_code == 400, f"Expected 400, got {put_resp.status_code}: {put_resp.get_json()}"


def test_public_facts_include_consecutive_rules(app, db):
    program_id, strike_id = _seed(app)
    with app.app_context():
        from business_app.models.loyalty import LoyaltyConsecutiveStrikeRule
        rule = LoyaltyConsecutiveStrikeRule(
            program_id=program_id, name="6-in-a-row", required_consecutive=6,
            combine_mode="all", bonus_points=1000, is_active=True,
        )
        from business_app.models.loyalty import LoyaltyStreakRule
        rule.strikes = [LoyaltyStreakRule.query.get(strike_id)]
        db.session.add(rule)
        db.session.commit()

        from business_app.frontend.routes import get_public_loyalty_facts
        facts = get_public_loyalty_facts()
        assert "consecutive_strike_rules" in facts
        assert facts["consecutive_strike_rules"][0]["required_consecutive"] == 6
        assert facts["consecutive_strike_rules"][0]["bonus_points"] == 1000
        assert facts["consecutive_strike_rules"][0]["strike_names"]  # non-empty
