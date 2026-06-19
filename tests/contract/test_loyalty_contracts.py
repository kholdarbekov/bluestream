"""Response-contract tests for loyalty (customer + admin) API endpoints.

These are characterization/contract tests: for each endpoint they assert the
standard success envelope ({success, data, message/meta as applicable}) and the
presence + types of the documented keys, WITHOUT over-asserting concrete values.

The success envelope is produced by ``business_app/utils/api_responses.py``:
- ``success_response``  -> {"success": true, "data": {...}} (message omitted when None)
- ``paginated_response`` -> {"success": true, "data": {"items": [...]},
                             "meta": {page, per_page, total, pages, has_next, has_prev, ...}}
- ``created_response``  -> 201 with {"success": true, "data": {...}, "message": ...}
- ``error_response``    -> {"success": false, "message": ...}

Loyalty fixtures are LOCAL (the conftest only provides user/admin/auth fixtures),
so a default program + account + EARNED lot + a configured reward + tiers are
seeded here so every payload is non-empty.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType


# ---------------------------------------------------------------------------
# Envelope assertion helpers
# ---------------------------------------------------------------------------
def _assert_success_envelope(body):
    """Common success-envelope invariants."""
    assert isinstance(body, dict)
    assert "success" in body
    assert isinstance(body["success"], bool)
    assert body["success"] is True
    assert "data" in body
    assert isinstance(body["data"], dict)


def _assert_error_envelope(body):
    assert isinstance(body, dict)
    assert body.get("success") is False
    assert "message" in body or "errors" in body


def _assert_pagination_meta(body):
    """paginated_response puts pagination keys under ``meta`` (not ``data``)."""
    assert "meta" in body
    meta = body["meta"]
    for key in ("page", "per_page", "total", "pages", "has_next", "has_prev"):
        assert key in meta, f"missing pagination meta key: {key}"
    assert isinstance(meta["page"], int)
    assert isinstance(meta["per_page"], int)
    assert isinstance(meta["total"], int)
    assert "items" in body["data"]
    assert isinstance(body["data"]["items"], list)


# ---------------------------------------------------------------------------
# Local loyalty fixtures (mirrors test_loyalty_service_business_rules /
# test_order_reward_redemption fixture style)
# ---------------------------------------------------------------------------
@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for contract tests",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        points_expiry_days=365,
        signup_bonus=100,
        referral_bonus=50,
        birthday_bonus=25,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def loyalty_tiers(db, loyalty_program):
    """Bronze (0+) and Silver (500+) so tier payloads are non-empty."""
    bronze = LoyaltyTierConfig(
        program_id=loyalty_program.id,
        name="Bronze",
        display_order=0,
        min_points=0,
        max_points=499,
        points_multiplier=1.0,
        is_active=True,
    )
    silver = LoyaltyTierConfig(
        program_id=loyalty_program.id,
        name="Silver",
        display_order=1,
        min_points=500,
        max_points=None,
        points_multiplier=1.5,
        is_active=True,
    )
    db.session.add_all([bronze, silver])
    db.session.commit()
    return [bronze, silver]


@pytest.fixture
def loyalty_account(db, sample_user, loyalty_program):
    """Account backed by a real 1000-pt EARNED lot (so balance is spendable)."""
    account = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=loyalty_program.id,
        total_earned=1000,
        total_redeemed=0,
        total_expired=0,
        current_balance=1000,
        current_tier="Bronze",
        points_to_next_tier=500,
    )
    db.session.add(account)
    db.session.flush()

    lot = LoyaltyTransaction(
        user_id=sample_user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=1000,
        remaining_points=1000,
        description="seed",
    )
    lot.expires_at = datetime(2999, 1, 1, tzinfo=timezone.utc)
    db.session.add(lot)
    db.session.commit()
    return account


@pytest.fixture
def configured_reward(db, loyalty_program):
    """A configured (redeemable) fixed-discount reward — cost < seeded balance."""
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="500 off",
        description="500 UZS off your order",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("500.00"),
        points_cost=100,
        is_active=True,
        is_system_reward=False,
        max_uses_per_user=1,
        redemptions_used=0,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture(autouse=True)
def _mute_loyalty_notifications(monkeypatch):
    """Keep notification side effects out of every contract test."""
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    monkeypatch.setattr(LoyaltyService, "_send_tier_upgrade_notification", lambda *a, **k: None)
    monkeypatch.setattr(LoyaltyService, "_send_points_expiry_notification", lambda *a, **k: None)


@pytest.fixture
def recipient_user(db):
    """A second user used as the gift-points recipient."""
    from business_app.models.user import User
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserType

    user = User(
        email="recipient@example.com",
        phone="+998901112233",
        password_hash=hash_password("TestPassword123!"),
        first_name="Recipient",
        last_name="User",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(user)
    db.session.commit()
    return user


# ---------------------------------------------------------------------------
# Customer endpoints
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.api
class TestLoyaltyCustomerContracts:
    def test_get_tiers_contract(self, client, db, loyalty_program, loyalty_tiers):
        # /tiers is public (cached, no auth required).
        response = client.get("/api/v1/loyalty/tiers")
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        assert "tiers" in body["data"]
        assert isinstance(body["data"]["tiers"], list)
        assert len(body["data"]["tiers"]) >= 1
        assert "tier_count" in body["data"]
        assert isinstance(body["data"]["tier_count"], int)
        tier = body["data"]["tiers"][0]
        for key in ("id", "name", "min_points", "points_multiplier"):
            assert key in tier

    def test_get_points_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/points", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in (
            "points_balance",
            "lifetime_points",
            "current_balance",
            "lifetime_earned",
            "tier",
            "next_tier_threshold",
        ):
            assert key in data
        assert isinstance(data["current_balance"], int)

    def test_get_account_contract(self, client, auth_headers, loyalty_account, loyalty_tiers):
        response = client.get("/api/v1/loyalty/account", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in (
            "current_balance",
            "current_tier",
            "points_this_month",
            "tier_progress",
            "available_rewards_count",
            "total_earned",
            "total_redeemed",
        ):
            assert key in data
        assert isinstance(data["tier_progress"], dict)
        for key in ("current", "next_tier_points", "points_needed"):
            assert key in data["tier_progress"]

    def test_get_history_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/history", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)

    def test_get_profile_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/profile", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        assert "loyalty_profile" in data
        assert isinstance(data["loyalty_profile"], dict)
        for key in (
            "points_balance",
            "total_earned",
            "total_redeemed",
            "current_tier",
            "tier_progress",
            "member_since",
        ):
            assert key in data["loyalty_profile"]
        assert "active_program" in data
        assert "recent_transactions" in data
        assert isinstance(data["recent_transactions"], list)

    def test_get_points_history_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/points/history", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)

    def test_get_rewards_contract(self, client, auth_headers, loyalty_account, configured_reward):
        response = client.get("/api/v1/loyalty/rewards", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        assert "rewards" in data
        assert isinstance(data["rewards"], list)
        assert len(data["rewards"]) >= 1
        assert "user_points_balance" in data
        assert isinstance(data["user_points_balance"], int)
        assert "categories" in data
        assert isinstance(data["categories"], list)
        reward = data["rewards"][0]
        # Each reward row carries the per-user redeemability + points_needed map.
        for key in ("id", "reward_type", "points_cost", "can_redeem", "points_needed"):
            assert key in reward
        assert isinstance(reward["can_redeem"], bool)
        assert isinstance(reward["points_needed"], int)

    def test_get_reward_details_contract(self, client, auth_headers, loyalty_account, configured_reward):
        response = client.get(
            f"/api/v1/loyalty/rewards/{configured_reward.id}", headers=auth_headers
        )
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in (
            "id",
            "name",
            "reward_type",
            "points_cost",
            "points_required",
            "discount_type",
            "discount_value",
            "can_redeem",
            "points_needed",
            "user_points_balance",
        ):
            assert key in data
        assert data["id"] == configured_reward.id
        assert isinstance(data["can_redeem"], bool)

    def test_get_rewards_history_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/rewards/history", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)

    def test_get_statistics_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/statistics", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        assert "period" in data
        assert "statistics" in data
        stats = data["statistics"]
        for key in (
            "current_balance",
            "total_earned",
            "total_redeemed",
            "net_points",
            "transaction_count",
            "current_tier",
            "points_by_source",
            "monthly_points_trend",
        ):
            assert key in stats

    def test_get_tier_benefits_contract(self, client, auth_headers, loyalty_account, loyalty_tiers):
        response = client.get("/api/v1/loyalty/tier-benefits", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in ("current_tier", "benefits", "upgrade_info"):
            assert key in data

    def test_get_referral_contract(self, client, auth_headers, loyalty_account):
        response = client.get("/api/v1/loyalty/referral", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in (
            "referral_code",
            "referral_link",
            "statistics",
            "recent_referrals",
            "rewards",
        ):
            assert key in data
        assert isinstance(data["recent_referrals"], list)
        assert isinstance(data["rewards"], dict)
        for key in ("referrer_points", "referee_points"):
            assert key in data["rewards"]

    def test_get_programs_contract(self, client, db, loyalty_program):
        # /programs is public (cached, no auth required).
        response = client.get("/api/v1/loyalty/programs")
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        assert "programs" in body["data"]
        assert isinstance(body["data"]["programs"], list)
        assert len(body["data"]["programs"]) >= 1

    def test_post_earn_points_success_contract(self, client, auth_headers, loyalty_account):
        response = client.post(
            "/api/v1/loyalty/earn-points",
            headers=auth_headers,
            json={"action": "review_submitted"},
        )
        assert response.status_code == 201
        body = response.get_json()
        _assert_success_envelope(body)
        assert "message" in body
        assert "transaction" in body["data"]
        assert isinstance(body["data"]["transaction"], dict)

    def test_post_earn_points_invalid_action_error_contract(
        self, client, auth_headers, loyalty_account
    ):
        response = client.post(
            "/api/v1/loyalty/earn-points",
            headers=auth_headers,
            json={"action": "not_a_real_action"},
        )
        assert response.status_code == 400
        body = response.get_json()
        _assert_error_envelope(body)

    def test_post_gift_points_success_contract(
        self, client, auth_headers, loyalty_account, recipient_user
    ):
        response = client.post(
            "/api/v1/loyalty/gift-points",
            headers=auth_headers,
            json={
                "recipient_phone": recipient_user.phone,
                "points_amount": 100,
                "message": "Enjoy!",
            },
        )
        assert response.status_code == 201
        body = response.get_json()
        _assert_success_envelope(body)
        assert "message" in body
        assert "transaction" in body["data"]
        assert isinstance(body["data"]["transaction"], dict)

    def test_post_gift_points_validation_error_contract(
        self, client, auth_headers, loyalty_account, recipient_user
    ):
        # Non-positive amount -> ValidationError -> 400 error envelope.
        response = client.post(
            "/api/v1/loyalty/gift-points",
            headers=auth_headers,
            json={"recipient_phone": recipient_user.phone, "points_amount": 0},
        )
        assert response.status_code == 400
        body = response.get_json()
        _assert_error_envelope(body)


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.api
class TestLoyaltyAdminContracts:
    def test_admin_members_contract(
        self, client, admin_auth_headers, loyalty_program, loyalty_account
    ):
        response = client.get("/api/v1/admin/loyalty/members", headers=admin_auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)
        # members route adds a summary block under meta.
        assert "summary" in body["meta"]
        assert isinstance(body["meta"]["summary"], dict)
        for key in (
            "total_members",
            "active_members",
            "total_points_in_circulation",
            "total_points_earned",
        ):
            assert key in body["meta"]["summary"]

    def test_admin_member_detail_contract(
        self, client, admin_auth_headers, sample_user, loyalty_program, loyalty_account
    ):
        response = client.get(
            f"/api/v1/admin/loyalty/members/{sample_user.id}", headers=admin_auth_headers
        )
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in (
            "member",
            "recent_redemptions",
            "referral_statistics",
            "tier_progress",
            "streak_progress",
        ):
            assert key in data
        assert isinstance(data["member"], dict)
        for key in ("user_id", "customer_name", "current_balance", "current_tier"):
            assert key in data["member"]

    def test_admin_member_transactions_pagination_contract(
        self, client, admin_auth_headers, sample_user, loyalty_program, loyalty_account
    ):
        response = client.get(
            f"/api/v1/admin/loyalty/members/{sample_user.id}/transactions",
            headers=admin_auth_headers,
        )
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)
        # The seeded EARNED lot should appear in the member's ledger.
        assert body["meta"]["total"] >= 1

    def test_admin_rewards_contract(
        self, client, admin_auth_headers, loyalty_program, configured_reward
    ):
        response = client.get("/api/v1/admin/loyalty/rewards", headers=admin_auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)
        assert body["meta"]["total"] >= 1
        reward = body["data"]["items"][0]
        for key in ("id", "reward_type", "points_cost"):
            assert key in reward

    def test_admin_reward_detail_contract(
        self, client, admin_auth_headers, loyalty_program, configured_reward
    ):
        response = client.get(
            f"/api/v1/admin/loyalty/rewards/{configured_reward.id}", headers=admin_auth_headers
        )
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        assert "reward" in body["data"]
        assert isinstance(body["data"]["reward"], dict)
        assert body["data"]["reward"]["id"] == configured_reward.id

    def test_admin_analytics_contract(
        self, client, admin_auth_headers, loyalty_program, loyalty_account
    ):
        response = client.get("/api/v1/admin/loyalty/analytics", headers=admin_auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        data = body["data"]
        for key in (
            "summary",
            "tier_distribution",
            "top_rewards",
            "points_trend",
            "redemption_metrics",
            "program_breakdown",
        ):
            assert key in data
        assert isinstance(data["summary"], dict)
        for key in ("total_members", "total_points_in_circulation"):
            assert key in data["summary"]

    def test_admin_programs_contract(self, client, admin_auth_headers, loyalty_program):
        response = client.get("/api/v1/admin/loyalty/programs", headers=admin_auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        _assert_success_envelope(body)
        _assert_pagination_meta(body)
        assert body["meta"]["total"] >= 1
        program = body["data"]["items"][0]
        assert "id" in program
        assert "name" in program
