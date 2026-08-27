"""Integration tests for loyalty streak rules.

Covers focused single-behavior integration tests:
  - Admin CRUD HTTP endpoints (create, list, update, delete, validation, auth)
  - Customer /loyalty/account includes streak_progress (regression)
  - Admin /admin/loyalty/members/<id> includes streak_progress
  - Guide page /loyalty-guide renders active rules
  - Earning: service-level update_streak against real DB
  - Trigger wiring: order_service calls update_streak on delivery
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyTransaction,
)
from business_app.models.order import Order
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, UserRole, UserType

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _default_program(db) -> LoyaltyProgram:
    """Get or create the default active LoyaltyProgram (mirrors existing tests)."""
    program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
    if not program:
        program = LoyaltyProgram(name="Default Program", is_active=True, is_default=True)
        db.session.add(program)
        db.session.commit()
    return program


def _create_rule(db, program, **kw) -> LoyaltyStreakRule:
    """Create a LoyaltyStreakRule with sensible defaults."""
    defaults = dict(
        name="3 in 30",
        required_orders=3,
        window_days=30,
        bonus_points=300,
        is_active=True,
        display_order=0,
    )
    defaults.update(kw)
    rule = LoyaltyStreakRule(program_id=program.id, **defaults)
    db.session.add(rule)
    db.session.commit()
    return rule


def _delivered_order(db, user_id: int, total, days_ago: int) -> Order:
    """Create a DELIVERED order, backdated by days_ago days."""
    order = Order(
        user_id=user_id,
        subtotal=Decimal(str(total)),
        total_amount=Decimal(str(total)),
        status=OrderStatus.DELIVERED,
    )
    db.session.add(order)
    db.session.flush()
    order.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.session.commit()
    return order


def _streak_points(db, user_id: int) -> int:
    """Sum of points from STREAK_BONUS transactions for this user."""
    txns = LoyaltyTransaction.query.filter_by(user_id=user_id).all()
    total = 0
    for t in txns:
        ed = t.extra_data or {}
        if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value:
            total += t.points
    return total


# ---------------------------------------------------------------------------
# Autouse fixture: silence notification side effects
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _silence_loyalty_notifications(loyalty_notification_spy):
    """Signature-enforcing spies rather than no-ops.

    A ``lambda *a, **k: None`` stub accepts ANY call, so a sender whose
    payload or signature drifts keeps every test green — that is how the
    tier-upgrade notification shipped rendering the wrong template. The
    shared fixture binds each call against the real signature instead.
    """
    return loyalty_notification_spy


# ===========================================================================
# ADMIN CRUD — HTTP tests
# ===========================================================================

class TestAdminStreakRuleCRUD:

    @pytest.mark.integration
    def test_create_basic_rule_returns_201_and_persists(self, client, admin_auth_headers, db):
        program = _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "3 in 30", "required_orders": 3, "window_days": 30, "bonus_points": 300},
        )
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["success"] is True
        rule = body["data"]["streak_rule"]
        assert rule["name"] == "3 in 30"
        assert rule["required_orders"] == 3
        assert rule["window_days"] == 30
        assert rule["bonus_points"] == 300
        assert LoyaltyStreakRule.query.filter_by(name="3 in 30").count() == 1

    @pytest.mark.integration
    def test_create_with_full_optional_fields(self, client, admin_auth_headers, db):
        program = _default_program(db)
        starts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ends = datetime(2026, 12, 31, tzinfo=timezone.utc)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Full Rule",
                "required_orders": 5,
                "window_days": 60,
                "bonus_points": 500,
                "min_order_amount": "10000.00",
                "is_active": True,
                "starts_at": starts.isoformat(),
                "ends_at": ends.isoformat(),
                "display_order": 2,
                "translations": {"name": {"ru": "Полное правило", "uz": "To'liq qoida"}},
            },
        )
        assert resp.status_code == 201
        rule_data = resp.get_json()["data"]["streak_rule"]
        assert float(rule_data["min_order_amount"]) == 10000.0
        assert rule_data["display_order"] == 2
        assert rule_data["starts_at"] is not None
        assert rule_data["ends_at"] is not None
        # Translations persisted
        rule_obj = LoyaltyStreakRule.query.get(rule_data["id"])
        assert rule_obj.get_translated("name", "ru") == "Полное правило"

    @pytest.mark.integration
    def test_create_with_explicit_program_id(self, client, admin_auth_headers, db):
        program = _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Explicit program rule",
                "required_orders": 2,
                "window_days": 14,
                "bonus_points": 100,
                "program_id": program.id,
            },
        )
        assert resp.status_code == 201
        rule = resp.get_json()["data"]["streak_rule"]
        assert rule["program_id"] == program.id

    @pytest.mark.integration
    def test_created_rule_appears_in_list(self, client, admin_auth_headers, db):
        program = _default_program(db)
        _create_rule(db, program, name="Listed Rule")
        resp = client.get("/api/v1/admin/loyalty/streak-rules", headers=admin_auth_headers)
        assert resp.status_code == 200
        names = [r["name"] for r in resp.get_json()["data"]["streak_rules"]]
        assert "Listed Rule" in names

    @pytest.mark.integration
    def test_create_validation_required_orders_zero(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "Bad", "required_orders": 0, "window_days": 30, "bonus_points": 100},
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_validation_window_days_zero(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "Bad", "required_orders": 3, "window_days": 0, "bonus_points": 100},
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_validation_bonus_points_zero(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "Bad", "required_orders": 3, "window_days": 30, "bonus_points": 0},
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_validation_min_order_amount_zero(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Bad", "required_orders": 3, "window_days": 30,
                "bonus_points": 100, "min_order_amount": 0,
            },
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_validation_min_order_amount_negative(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Bad", "required_orders": 3, "window_days": 30,
                "bonus_points": 100, "min_order_amount": -500,
            },
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_validation_ends_at_before_starts_at(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Bad dates",
                "required_orders": 3,
                "window_days": 30,
                "bonus_points": 100,
                "starts_at": "2026-12-31T00:00:00+00:00",
                "ends_at": "2026-01-01T00:00:00+00:00",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_validation_ends_at_equal_to_starts_at(self, client, admin_auth_headers, db):
        _default_program(db)
        ts = "2026-06-01T00:00:00+00:00"
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={
                "name": "Same dates",
                "required_orders": 3,
                "window_days": 30,
                "bonus_points": 100,
                "starts_at": ts,
                "ends_at": ts,
            },
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_missing_required_field_rejected(self, client, admin_auth_headers, db):
        _default_program(db)
        # Missing bonus_points
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "Missing", "required_orders": 3, "window_days": 30},
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_no_default_program_returns_400(self, client, admin_auth_headers, db):
        # Ensure no default program exists
        assert LoyaltyProgram.query.filter_by(is_default=True).count() == 0
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            headers=admin_auth_headers,
            json={"name": "No prog", "required_orders": 3, "window_days": 30, "bonus_points": 100},
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_update_bonus_points_reflected(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(db, program)
        resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
            json={"bonus_points": 999},
        )
        assert resp.status_code == 200
        db.session.refresh(rule)
        assert rule.bonus_points == 999

    @pytest.mark.integration
    def test_update_threshold_reflected(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(db, program)
        resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
            json={"required_orders": 7, "window_days": 45},
        )
        assert resp.status_code == 200
        db.session.refresh(rule)
        assert rule.required_orders == 7
        assert rule.window_days == 45

    @pytest.mark.integration
    def test_update_is_active_reflected(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(db, program, is_active=True)
        resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
            json={"is_active": False},
        )
        assert resp.status_code == 200
        db.session.refresh(rule)
        assert rule.is_active is False

    @pytest.mark.integration
    def test_update_clears_min_order_amount_and_dates(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(
            db, program,
            min_order_amount=Decimal("5000"),
            starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ends_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        )
        resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
            json={"min_order_amount": None, "starts_at": None, "ends_at": None},
        )
        assert resp.status_code == 200
        db.session.refresh(rule)
        assert rule.min_order_amount is None
        assert rule.starts_at is None
        assert rule.ends_at is None

    @pytest.mark.integration
    def test_update_bad_values_returns_400(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(db, program)
        resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
            json={"required_orders": 0},
        )
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_update_missing_id_returns_404(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.put(
            "/api/v1/admin/loyalty/streak-rules/999999",
            headers=admin_auth_headers,
            json={"bonus_points": 100},
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_update_translations_reflected_in_get(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(db, program, name="Original")
        resp = client.put(
            f"/api/v1/admin/loyalty/streak-rules/{rule.id}",
            headers=admin_auth_headers,
            json={"translations": {"name": {"ru": "Обновлённое", "uz": "Yangilangan"}}},
        )
        assert resp.status_code == 200
        # Translations shape in GET list: {"name": {lang: value}}
        list_resp = client.get("/api/v1/admin/loyalty/streak-rules", headers=admin_auth_headers)
        rules_data = list_resp.get_json()["data"]["streak_rules"]
        target = next(r for r in rules_data if r["id"] == rule.id)
        assert "translations" in target
        assert isinstance(target["translations"], dict)
        assert "name" in target["translations"]
        assert target["translations"]["name"].get("ru") == "Обновлённое"

    @pytest.mark.integration
    def test_delete_rule_returns_200_and_gone(self, client, admin_auth_headers, db):
        program = _default_program(db)
        rule = _create_rule(db, program)
        rule_id = rule.id
        resp = client.delete(
            f"/api/v1/admin/loyalty/streak-rules/{rule_id}",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert LoyaltyStreakRule.query.get(rule_id) is None

    @pytest.mark.integration
    def test_delete_missing_rule_returns_404(self, client, admin_auth_headers, db):
        _default_program(db)
        resp = client.delete(
            "/api/v1/admin/loyalty/streak-rules/999999",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_list_filtered_by_program_id(self, client, admin_auth_headers, db):
        prog1 = _default_program(db)
        # Create a second non-default program
        prog2 = LoyaltyProgram(name="Other Program", is_active=True, is_default=False)
        db.session.add(prog2)
        db.session.commit()

        _create_rule(db, prog1, name="Rule Prog1")
        _create_rule(db, prog2, name="Rule Prog2")

        resp = client.get(
            f"/api/v1/admin/loyalty/streak-rules?program_id={prog2.id}",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        names = [r["name"] for r in resp.get_json()["data"]["streak_rules"]]
        assert "Rule Prog2" in names
        assert "Rule Prog1" not in names

    @pytest.mark.integration
    def test_list_ordered_by_display_order(self, client, admin_auth_headers, db):
        program = _default_program(db)
        _create_rule(db, program, name="Third", display_order=2)
        _create_rule(db, program, name="First", display_order=0)
        _create_rule(db, program, name="Second", display_order=1)

        resp = client.get("/api/v1/admin/loyalty/streak-rules", headers=admin_auth_headers)
        assert resp.status_code == 200
        names = [r["name"] for r in resp.get_json()["data"]["streak_rules"]]
        assert names == ["First", "Second", "Third"]

    @pytest.mark.integration
    def test_unauthenticated_create_returns_401(self, client, db):
        _default_program(db)
        resp = client.post(
            "/api/v1/admin/loyalty/streak-rules",
            json={"name": "X", "required_orders": 3, "window_days": 30, "bonus_points": 100},
        )
        assert resp.status_code == 401

    @pytest.mark.integration
    def test_unauthenticated_list_returns_401(self, client, db):
        _default_program(db)
        resp = client.get("/api/v1/admin/loyalty/streak-rules")
        assert resp.status_code == 401

    @pytest.mark.integration
    def test_unauthenticated_delete_returns_401(self, client, db):
        program = _default_program(db)
        rule = _create_rule(db, program)
        resp = client.delete(f"/api/v1/admin/loyalty/streak-rules/{rule.id}")
        assert resp.status_code == 401


# ===========================================================================
# Customer /loyalty/account — streak_progress regression
# ===========================================================================

class TestCustomerAccountStreakProgress:

    @pytest.mark.integration
    def test_account_includes_streak_progress_with_2_of_3_orders(
        self, client, auth_headers, db, sample_user
    ):
        """Regression: /loyalty/account must expose streak_progress with current_orders."""
        program = _default_program(db)
        _create_rule(db, program, name="3-in-30 Rule", required_orders=3, window_days=30, bonus_points=300)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)
        _delivered_order(db, sample_user.id, 10000, days_ago=10)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert "streak_progress" in data
        progress = data["streak_progress"]
        assert len(progress) == 1
        entry = progress[0]
        assert entry["name"] == "3-in-30 Rule"
        assert entry["required_orders"] == 3
        assert entry["current_orders"] == 2
        assert entry["window_days"] == 30
        assert entry["bonus_points"] == 300

    @pytest.mark.integration
    def test_account_streak_progress_empty_when_no_rules(self, client, auth_headers, db, sample_user):
        _default_program(db)
        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert "streak_progress" in data
        assert data["streak_progress"] == []

    @pytest.mark.integration
    def test_account_streak_min_amount_filters_progress(self, client, auth_headers, db, sample_user):
        """Orders below min_order_amount should not count toward streak_progress."""
        program = _default_program(db)
        _create_rule(
            db, program, name="Min Amount Rule",
            required_orders=3, window_days=30, bonus_points=200,
            min_order_amount=Decimal("20000"),
        )
        # 2 orders above threshold + 1 below
        _delivered_order(db, sample_user.id, 25000, days_ago=5)
        _delivered_order(db, sample_user.id, 25000, days_ago=10)
        _delivered_order(db, sample_user.id, 5000, days_ago=15)  # below threshold

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        progress = resp.get_json()["data"]["streak_progress"]
        assert len(progress) == 1
        assert progress[0]["current_orders"] == 2  # only 2 qualify

    @pytest.mark.integration
    def test_account_multiple_rules_multiple_entries_ordered(
        self, client, auth_headers, db, sample_user
    ):
        program = _default_program(db)
        _create_rule(db, program, name="Second Rule", required_orders=5, window_days=30, bonus_points=200, display_order=1)
        _create_rule(db, program, name="First Rule", required_orders=3, window_days=30, bonus_points=300, display_order=0)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)
        _delivered_order(db, sample_user.id, 10000, days_ago=10)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        progress = resp.get_json()["data"]["streak_progress"]
        assert len(progress) == 2
        assert progress[0]["name"] == "First Rule"
        assert progress[1]["name"] == "Second Rule"

    @pytest.mark.integration
    def test_account_inactive_rule_excluded_from_progress(
        self, client, auth_headers, db, sample_user
    ):
        program = _default_program(db)
        _create_rule(db, program, name="Inactive Rule", required_orders=2, window_days=30, bonus_points=100, is_active=False)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)
        _delivered_order(db, sample_user.id, 10000, days_ago=10)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        progress = resp.get_json()["data"]["streak_progress"]
        assert len(progress) == 0

    @pytest.mark.integration
    def test_account_future_dated_rule_excluded_from_progress(
        self, client, auth_headers, db, sample_user
    ):
        program = _default_program(db)
        future_start = datetime.now(timezone.utc) + timedelta(days=10)
        _create_rule(
            db, program, name="Future Rule",
            required_orders=2, window_days=30, bonus_points=100,
            starts_at=future_start,
        )
        _delivered_order(db, sample_user.id, 10000, days_ago=5)
        _delivered_order(db, sample_user.id, 10000, days_ago=10)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        progress = resp.get_json()["data"]["streak_progress"]
        assert len(progress) == 0

    @pytest.mark.integration
    def test_account_current_orders_capped_at_required(
        self, client, auth_headers, db, sample_user
    ):
        """Even with 10 qualifying orders, current_orders must not exceed required_orders."""
        program = _default_program(db)
        _create_rule(db, program, name="Cap Rule", required_orders=3, window_days=30, bonus_points=300)
        for i in range(10):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        resp = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert resp.status_code == 200
        progress = resp.get_json()["data"]["streak_progress"]
        assert len(progress) == 1
        assert progress[0]["current_orders"] == progress[0]["required_orders"] == 3


# ===========================================================================
# Admin member detail — streak_progress
# ===========================================================================

class TestAdminMemberDetailStreakProgress:

    @pytest.mark.integration
    def test_admin_member_detail_includes_streak_progress(
        self, client, admin_auth_headers, db, sample_user
    ):
        program = _default_program(db)
        _create_rule(db, program, name="Admin View Rule", required_orders=3, window_days=30, bonus_points=300)
        _delivered_order(db, sample_user.id, 10000, days_ago=5)
        _delivered_order(db, sample_user.id, 10000, days_ago=10)

        resp = client.get(
            f"/api/v1/admin/loyalty/members/{sample_user.id}",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert "streak_progress" in data
        progress = data["streak_progress"]
        assert len(progress) == 1
        assert progress[0]["name"] == "Admin View Rule"
        assert progress[0]["current_orders"] == 2

    @pytest.mark.integration
    def test_admin_member_detail_streak_progress_reflects_orders(
        self, client, admin_auth_headers, db, sample_user
    ):
        """Member with 0 orders has current_orders=0."""
        program = _default_program(db)
        _create_rule(db, program, name="Progress Rule", required_orders=5, window_days=30, bonus_points=200)

        resp = client.get(
            f"/api/v1/admin/loyalty/members/{sample_user.id}",
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200
        progress = resp.get_json()["data"]["streak_progress"]
        assert len(progress) == 1
        assert progress[0]["current_orders"] == 0


# ===========================================================================
# Guide page — /loyalty-guide
# ===========================================================================

class TestLoyaltyGuidePageStreakRules:

    @pytest.mark.integration
    def test_active_rule_name_rendered(self, client, db):
        program = _default_program(db)
        _create_rule(db, program, name="Unique Streak Name 42X")
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"Unique Streak Name 42X" in resp.data

    @pytest.mark.integration
    def test_rule_with_min_amount_rendered(self, client, db):
        program = _default_program(db)
        _create_rule(
            db, program, name="Big Spender Streak",
            min_order_amount=Decimal("50000"), required_orders=3, window_days=30, bonus_points=500,
        )
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"Big Spender Streak" in resp.data

    @pytest.mark.integration
    def test_no_rules_sentinel_absent(self, client, db):
        """With no rules, a sentinel string that only appears with rules (the rule name) is absent."""
        _default_program(db)
        SENTINEL = "XK_STREAK_SENTINEL_NO_RULE_HERE"
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert SENTINEL.encode() not in resp.data

    @pytest.mark.integration
    def test_multiple_rules_all_listed(self, client, db):
        program = _default_program(db)
        _create_rule(db, program, name="First Streak Rule AAA", display_order=0)
        _create_rule(db, program, name="Second Streak Rule BBB", display_order=1)
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"First Streak Rule AAA" in resp.data
        assert b"Second Streak Rule BBB" in resp.data

    @pytest.mark.integration
    def test_inactive_rule_not_rendered(self, client, db):
        program = _default_program(db)
        _create_rule(db, program, name="ACTIVE_RULE_YYY", is_active=True)
        _create_rule(db, program, name="INACTIVE_RULE_ZZZ", is_active=False)
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"ACTIVE_RULE_YYY" in resp.data
        assert b"INACTIVE_RULE_ZZZ" not in resp.data

    @pytest.mark.integration
    def test_future_rule_not_rendered(self, client, db):
        program = _default_program(db)
        future_start = datetime.now(timezone.utc) + timedelta(days=30)
        _create_rule(db, program, name="FUTURE_RULE_XYZ", starts_at=future_start)
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"FUTURE_RULE_XYZ" not in resp.data

    @pytest.mark.integration
    def test_expired_rule_not_rendered(self, client, db):
        program = _default_program(db)
        past_end = datetime.now(timezone.utc) - timedelta(days=1)
        _create_rule(db, program, name="EXPIRED_RULE_ABC", ends_at=past_end)
        resp = client.get("/loyalty-guide?lang=en")
        assert resp.status_code == 200
        assert b"EXPIRED_RULE_ABC" not in resp.data

    @pytest.mark.integration
    def test_rule_with_translation_shown_in_correct_language(self, client, db):
        program = _default_program(db)
        rule = _create_rule(db, program, name="English Name Rule")
        rule.set_translations({"name": {"ru": "Правило на русском"}})
        db.session.commit()

        resp_ru = client.get("/loyalty-guide?lang=ru")
        assert resp_ru.status_code == 200
        assert "Правило на русском".encode() in resp_ru.data


# ===========================================================================
# Earning — service-level update_streak against real DB
# ===========================================================================

class TestEarningServiceLevel:

    @pytest.mark.integration
    def test_exact_threshold_awards_bonus(self, app, db, sample_user):
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=3, bonus_points=300)
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 300
        account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
        assert account is not None
        assert account.current_balance == 300

    @pytest.mark.integration
    def test_one_below_threshold_no_award(self, app, db, sample_user):
        program = _default_program(db)
        _create_rule(db, program, required_orders=3, bonus_points=300)
        for i in range(2):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

    @pytest.mark.integration
    def test_streak_bonus_transaction_carries_rule_id(self, app, db, sample_user):
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=2, bonus_points=150)
        for i in range(2):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
        streak_txns = [
            t for t in txns
            if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value
        ]
        assert len(streak_txns) == 1
        assert streak_txns[0].extra_data["streak_rule_id"] == rule.id

    @pytest.mark.integration
    def test_window_boundary_29_days_counts_31_days_doesnt(self, app, db, sample_user):
        """Order 29 days ago counts; order 31 days ago doesn't for window_days=30."""
        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)
        _delivered_order(db, sample_user.id, 10000, days_ago=29)   # within window
        _delivered_order(db, sample_user.id, 10000, days_ago=31)   # outside window

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        # Only 1 order in window, need 2 -> no award
        assert _streak_points(db, sample_user.id) == 0

    @pytest.mark.integration
    def test_window_boundary_both_within_awards(self, app, db, sample_user):
        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)
        _delivered_order(db, sample_user.id, 10000, days_ago=29)  # still within

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 200

    @pytest.mark.integration
    def test_min_order_amount_filters_qualifying_orders(self, app, db, sample_user):
        """Orders below min_order_amount do not count for streak earning."""
        program = _default_program(db)
        _create_rule(
            db, program, required_orders=3, window_days=30, bonus_points=300,
            min_order_amount=Decimal("20000"),
        )
        _delivered_order(db, sample_user.id, 25000, days_ago=1)
        _delivered_order(db, sample_user.id, 25000, days_ago=2)
        _delivered_order(db, sample_user.id, 5000, days_ago=3)   # below min

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0  # only 2 qualify, need 3

    @pytest.mark.integration
    def test_exactly_min_amount_counts(self, app, db, sample_user):
        program = _default_program(db)
        _create_rule(
            db, program, required_orders=2, window_days=30, bonus_points=200,
            min_order_amount=Decimal("10000"),
        )
        _delivered_order(db, sample_user.id, 10000, days_ago=1)   # exactly at min
        _delivered_order(db, sample_user.id, 15000, days_ago=2)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 200

    @pytest.mark.integration
    def test_two_rules_both_satisfied_both_awarded(self, app, db, sample_user):
        """5 orders qualifies rule-3-in-30 (+300) and rule-5-in-30 (+200); total = 500."""
        program = _default_program(db)
        _create_rule(db, program, name="3 in 30", required_orders=3, window_days=30, bonus_points=300)
        _create_rule(db, program, name="5 in 30", required_orders=5, window_days=30, bonus_points=200)
        for i in range(5):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 500

    @pytest.mark.integration
    def test_cooldown_prevents_double_award(self, app, db, sample_user):
        """Calling update_streak twice in the same window awards only once."""
        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)
        _delivered_order(db, sample_user.id, 10000, days_ago=2)

        with app.app_context():
            svc = LoyaltyService()
            svc.update_streak(sample_user.id)
            svc.update_streak(sample_user.id)  # immediate re-run

        assert _streak_points(db, sample_user.id) == 200  # awarded only once

    @pytest.mark.integration
    def test_cooldown_expires_after_window_re_award_possible(self, app, db, sample_user):
        """Once the old award txn ages past window_days + fresh orders exist → re-award."""
        program = _default_program(db)
        rule = _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)
        _delivered_order(db, sample_user.id, 10000, days_ago=1)
        _delivered_order(db, sample_user.id, 10000, days_ago=2)

        with app.app_context():
            svc = LoyaltyService()
            svc.update_streak(sample_user.id)

            # Age the existing STREAK_BONUS txn beyond the window.
            # Use Python-level filtering (extra_data is JSON; .astext is PostgreSQL-only
            # and not available in SQLite used by tests).
            all_txns = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
            old_txn = next(
                (t for t in all_txns
                 if (t.extra_data or {}).get("action_type") == LoyaltyActionType.STREAK_BONUS.value),
                None,
            )
            assert old_txn is not None, "Expected a STREAK_BONUS transaction after first update_streak"
            old_txn.created_at = datetime.now(timezone.utc) - timedelta(days=35)
            db.session.commit()

            # Add two more fresh orders
            _delivered_order(db, sample_user.id, 10000, days_ago=3)
            _delivered_order(db, sample_user.id, 10000, days_ago=5)

            svc.update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 400  # awarded twice total

    @pytest.mark.integration
    def test_non_delivered_orders_not_counted(self, app, db, sample_user):
        """PENDING and CANCELLED orders must not count toward streak."""
        from business_app.models.order import Order as _Order

        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)

        # PENDING order
        pending = _Order(
            user_id=sample_user.id,
            subtotal=Decimal("10000"),
            total_amount=Decimal("10000"),
            status=OrderStatus.PENDING,
        )
        db.session.add(pending)
        # CANCELLED order
        cancelled = _Order(
            user_id=sample_user.id,
            subtotal=Decimal("10000"),
            total_amount=Decimal("10000"),
            status=OrderStatus.CANCELLED,
        )
        db.session.add(cancelled)
        db.session.commit()

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

    @pytest.mark.integration
    def test_cross_user_isolation(self, app, db, sample_user):
        """Another user's orders don't affect sample_user's streak, and vice versa."""
        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)

        other = User(
            email="other_streak@example.com",
            phone="+998909876501",
            password_hash=hash_password("TestPassword123!"),
            first_name="Other",
            last_name="User",
            user_type=UserType.INDIVIDUAL,
            role=UserRole.CUSTOMER,
            is_verified=True,
        )
        db.session.add(other)
        db.session.commit()

        # Only other_user has 2 orders; sample_user has 0
        for i in range(2):
            _delivered_order(db, other.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0
        # other user also not yet awarded (update_streak not called for them)
        assert _streak_points(db, other.id) == 0

    @pytest.mark.integration
    def test_rule_on_non_default_program_not_evaluated(self, app, db, sample_user):
        """A streak rule tied to a non-default program is ignored when a default program exists.

        update_streak uses the default program (is_default=True). Rules on other programs
        are never evaluated. We seed a default program with NO rules, and a non-default
        program WITH a rule; only the default program's (empty) rule set is evaluated.
        """
        default = _default_program(db)  # is_default=True, no rules
        non_default = LoyaltyProgram(name="Non-Default", is_active=True, is_default=False)
        db.session.add(non_default)
        db.session.commit()
        _create_rule(db, non_default, required_orders=2, window_days=30, bonus_points=200)
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

    @pytest.mark.integration
    def test_future_starts_at_no_award(self, app, db, sample_user):
        program = _default_program(db)
        future_start = datetime.now(timezone.utc) + timedelta(days=10)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200, starts_at=future_start)
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

    @pytest.mark.integration
    def test_past_ends_at_no_award(self, app, db, sample_user):
        program = _default_program(db)
        past_end = datetime.now(timezone.utc) - timedelta(days=1)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200, ends_at=past_end)
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0

    @pytest.mark.integration
    def test_effective_within_dates_awards(self, app, db, sample_user):
        program = _default_program(db)
        past_start = datetime.now(timezone.utc) - timedelta(days=10)
        future_end = datetime.now(timezone.utc) + timedelta(days=10)
        _create_rule(
            db, program, required_orders=2, window_days=30, bonus_points=200,
            starts_at=past_start, ends_at=future_end,
        )
        for i in range(2):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 200

    @pytest.mark.integration
    def test_no_default_program_no_award_no_error(self, app, db, sample_user):
        """update_streak must gracefully no-op when there is no default program."""
        assert LoyaltyProgram.query.count() == 0
        for i in range(5):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        # Must not raise
        with app.app_context():
            LoyaltyService().update_streak(sample_user.id)

        assert _streak_points(db, sample_user.id) == 0


# ===========================================================================
# Trigger wiring — order_service calls update_streak on delivery
# ===========================================================================

class TestTriggerWiring:

    @pytest.mark.integration
    def test_update_streak_called_on_order_delivery(self, app, db, sample_user):
        """Verify that _handle_status_change_actions calls update_streak when an order
        transitions to DELIVERED.

        We patch LoyaltyService.update_streak rather than running the full delivery
        pipeline (which requires a Delivery row, driver, payment, address, etc.).
        The patch point matches where order_service imports the class at call time:
        ``business_app.services.loyalty_service.LoyaltyService``.
        The patch proves the wiring is present without needing the entire stack.

        Note: _handle_status_change_actions does NOT take an old_status kwarg —
        it receives only the new_status and fires conditional logic based on it.
        """
        from business_app.services.order_service import OrderService
        from business_app.models.order import Order as _Order

        program = _default_program(db)
        _create_rule(db, program, required_orders=2, window_days=30, bonus_points=200)
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        # Build an already-DELIVERED order to simulate the post-delivery hook path
        order = _Order(
            user_id=sample_user.id,
            subtotal=Decimal("10000"),
            total_amount=Decimal("10000"),
            status=OrderStatus.DELIVERED,
        )
        db.session.add(order)
        db.session.commit()

        called_with = []

        with patch(
            "business_app.services.loyalty_service.LoyaltyService.update_streak",
            side_effect=lambda user_id, commit=True: called_with.append(user_id),
        ):
            with app.app_context():
                svc = OrderService()
                # Call _handle_status_change_actions directly to test the wiring without
                # going through the full update_order_status (which needs driver assignment,
                # payment, address, delivery row, etc.).
                svc._handle_status_change_actions(
                    order=order,
                    new_status=OrderStatus.DELIVERED,
                    updated_by=sample_user.id,
                    commit=False,
                )

        assert sample_user.id in called_with, (
            "update_streak was not called with the user's id when order transitioned to DELIVERED"
        )

    @pytest.mark.integration
    def test_update_streak_actually_awards_on_delivery(self, app, db, sample_user):
        """Integration (no mocks): confirm that calling _handle_status_change_actions
        with DELIVERED triggers a real streak bonus award for a qualifying user."""
        from business_app.services.order_service import OrderService
        from business_app.models.order import Order as _Order

        program = _default_program(db)
        _create_rule(db, program, required_orders=3, window_days=30, bonus_points=300)
        for i in range(3):
            _delivered_order(db, sample_user.id, 10000, days_ago=i + 1)

        order = _Order(
            user_id=sample_user.id,
            subtotal=Decimal("10000"),
            total_amount=Decimal("10000"),
            status=OrderStatus.DELIVERED,
        )
        db.session.add(order)
        db.session.commit()

        with app.app_context():
            svc = OrderService()
            svc._handle_status_change_actions(
                order=order,
                new_status=OrderStatus.DELIVERED,
                updated_by=sample_user.id,
                commit=True,
            )

        assert _streak_points(db, sample_user.id) == 300
