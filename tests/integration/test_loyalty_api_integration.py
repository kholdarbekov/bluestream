"""Integration tests for the loyalty HTTP API (real service + DB via test client).

These exercise the customer-facing ``/api/v1/loyalty/*`` endpoints and the
admin ``/api/v1/admin/loyalty/*`` reward/member/analytics endpoints end-to-end:
real rows are seeded, real requests are issued through the Flask test client,
and the resulting effects are asserted both in the JSON response envelope and
directly in the database.

Only notification side effects are stubbed (LoyaltyService._send_* no-ops);
the points ledger, rewards visibility/redeemability, gifting, and admin CRUD
all run against the real service + SQLite in-memory DB.

Loyalty fixtures are local (the shared conftest does not define loyalty rows).
The response envelope is the project standard: success payloads live under
``data`` and paginated payloads put items under ``data.items`` with pagination
metadata under ``meta``.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures (local; conftest does not seed loyalty rows)
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


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(name="Default Program", is_active=True, is_default=True)
    db.session.add(program)
    db.session.commit()
    return program


def _seed_balance(db, user_id, program_id, points):
    """Seed a loyalty account + a single EARNED FIFO lot so available == points."""
    account = LoyaltyPoints(
        user_id=user_id,
        program_id=program_id,
        total_earned=points,
        current_balance=points,
    )
    db.session.add(account)
    db.session.flush()
    lot = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=points,
        remaining_points=points,
        description="seed",
        expires_at=FAR_FUTURE,
    )
    db.session.add(lot)
    db.session.commit()
    return account


@pytest.fixture
def loyalty_account(db, sample_user, loyalty_program):
    """sample_user with a real 1000-pt balance."""
    return _seed_balance(db, sample_user.id, loyalty_program.id, 1000)


@pytest.fixture
def discount_reward(db, loyalty_program):
    """Configured, redeemable fixed-discount reward costing 100 points."""
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="500 off",
        description="500 UZS off",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("500.00"),
        points_cost=100,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def expensive_reward(db, loyalty_program):
    """A configured discount reward that costs more than the seeded balance."""
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="Premium discount",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("9000.00"),
        points_cost=5000,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def misconfigured_reward(db, loyalty_program):
    """A discount reward with discount_value=0 -> not configured -> hidden."""
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="Broken discount",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("0.00"),
        points_cost=50,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def tier_configs(db, loyalty_program):
    """Bronze/Silver/Gold tier ladder for the default program."""
    bronze = LoyaltyTierConfig(
        program_id=loyalty_program.id,
        name="Bronze",
        display_order=0,
        min_points=0,
        max_points=999,
        points_multiplier=1.0,
        discount_percentage=0,
        is_active=True,
    )
    silver = LoyaltyTierConfig(
        program_id=loyalty_program.id,
        name="Silver",
        display_order=1,
        min_points=1000,
        max_points=4999,
        points_multiplier=1.25,
        discount_percentage=5,
        is_active=True,
    )
    gold = LoyaltyTierConfig(
        program_id=loyalty_program.id,
        name="Gold",
        display_order=2,
        min_points=5000,
        max_points=None,
        points_multiplier=1.5,
        discount_percentage=10,
        is_active=True,
    )
    db.session.add_all([bronze, silver, gold])
    db.session.commit()
    return {"bronze": bronze, "silver": silver, "gold": gold}


@pytest.fixture
def recipient_user(db):
    """A second user with a valid UZS phone, used as a gift recipient."""
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
# Customer endpoints: points / account / rewards
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
def test_get_points_reflects_seeded_balance(client, auth_headers, loyalty_account):
    response = client.get("/api/v1/loyalty/points", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["points_balance"] == 1000
    assert data["current_balance"] == 1000
    assert data["lifetime_earned"] == 1000


@pytest.mark.integration
@pytest.mark.api
def test_get_account_dashboard_reflects_seeded_balance(client, auth_headers, loyalty_account):
    response = client.get("/api/v1/loyalty/account", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["current_balance"] == 1000
    assert data["total_earned"] == 1000
    assert data["total_redeemed"] == 0
    assert "tier_progress" in data


@pytest.mark.integration
@pytest.mark.api
def test_get_rewards_lists_only_configured_with_can_redeem(
    client, auth_headers, loyalty_account, discount_reward, expensive_reward, misconfigured_reward
):
    """Visible list excludes the misconfigured reward; can_redeem reflects balance."""
    response = client.get("/api/v1/loyalty/rewards", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    rewards_by_id = {r["id"]: r for r in data["rewards"]}

    # Misconfigured (discount_value=0) reward is hidden entirely.
    assert misconfigured_reward.id not in rewards_by_id
    # Affordable configured reward is redeemable; over-balance one is not.
    assert rewards_by_id[discount_reward.id]["can_redeem"] is True
    assert rewards_by_id[discount_reward.id]["points_needed"] == 0
    assert rewards_by_id[expensive_reward.id]["can_redeem"] is False
    assert rewards_by_id[expensive_reward.id]["points_needed"] == 4000
    assert data["user_points_balance"] == 1000


@pytest.mark.integration
@pytest.mark.api
def test_get_reward_details_includes_points_needed(client, auth_headers, loyalty_account, expensive_reward):
    response = client.get(f"/api/v1/loyalty/rewards/{expensive_reward.id}", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["id"] == expensive_reward.id
    assert data["points_cost"] == 5000
    assert data["points_needed"] == 4000  # 5000 cost - 1000 balance
    assert data["can_redeem"] is False
    assert data["user_points_balance"] == 1000


@pytest.mark.integration
@pytest.mark.api
def test_get_reward_details_missing_reward_returns_404(client, auth_headers, loyalty_program):
    response = client.get("/api/v1/loyalty/rewards/999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.get_json()["success"] is False


@pytest.mark.integration
@pytest.mark.api
def test_rewards_history_lists_redeemed_transactions(
    client, auth_headers, db, sample_user, loyalty_account, discount_reward
):
    """The redemption-history endpoint serializes REDEEMED ledger rows."""
    LoyaltyService().deduct_points(
        sample_user.id, 100, f"Redeemed reward: {discount_reward.name}", order_id=discount_reward.id
    )

    response = client.get("/api/v1/loyalty/rewards/history", headers=auth_headers)

    assert response.status_code == 200
    payload = response.get_json()
    items = payload["data"]["items"]
    assert payload["meta"]["total"] == 1
    assert len(items) == 1
    # deduct_points records the spend as a negative REDEEMED transaction.
    assert items[0]["points"] == -100
    assert items[0]["transaction_type"] == LoyaltyTransactionType.REDEEMED.value


@pytest.mark.integration
@pytest.mark.api
def test_statistics_computes_from_seeded_ledger(client, auth_headers, db, sample_user, loyalty_account):
    """Statistics aggregates EARNED/REDEEMED across the seeded ledger (period=all)."""
    service = LoyaltyService()
    service.deduct_points(sample_user.id, 200, "redeem")

    response = client.get("/api/v1/loyalty/statistics?period=all", headers=auth_headers)

    assert response.status_code == 200
    stats = response.get_json()["data"]["statistics"]
    assert stats["total_earned"] == 1000  # the seeded EARNED lot
    assert stats["total_redeemed"] == 200
    assert stats["net_points"] == 800
    assert stats["current_balance"] == 800


@pytest.mark.integration
@pytest.mark.api
def test_tier_benefits_reflects_account_tier(client, auth_headers, db, sample_user, loyalty_program, tier_configs):
    """tier-benefits reads the account's current_tier and returns its benefits."""
    account = _seed_balance(db, sample_user.id, loyalty_program.id, 2000)
    account.current_tier = "Silver"
    db.session.commit()

    response = client.get("/api/v1/loyalty/tier-benefits", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["current_tier"] == "Silver"
    assert data["benefits"]["discount_percentage"] == 5
    assert "upgrade_info" in data


@pytest.mark.integration
@pytest.mark.api
def test_referral_returns_code_and_link(client, auth_headers, sample_user, loyalty_account):
    response = client.get("/api/v1/loyalty/referral", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["referral_code"]  # generated/persisted on first access
    assert f"ref={data['referral_code']}" in data["referral_link"]
    assert "register?ref=" in data["referral_link"]
    assert "statistics" in data
    # The code was persisted onto the User row.
    db_user = User.query.get(sample_user.id)
    assert db_user.referral_code == data["referral_code"]


# ---------------------------------------------------------------------------
# Gift points
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
def test_gift_points_moves_balance_between_users(
    client, auth_headers, db, sample_user, loyalty_account, loyalty_program, recipient_user
):
    """A successful gift deducts from sender and credits recipient (asserted in DB)."""
    response = client.post(
        "/api/v1/loyalty/gift-points",
        headers=auth_headers,
        json={"recipient_phone": recipient_user.phone, "points_amount": 300, "message": "enjoy"},
    )

    assert response.status_code == 201
    assert response.get_json()["success"] is True

    service = LoyaltyService()
    # Sender drops 1000 -> 700; recipient (new account) gains 300.
    assert service.get_available_points(sample_user.id) == 700
    assert service.get_available_points(recipient_user.id) == 300


@pytest.mark.integration
@pytest.mark.api
def test_gift_points_insufficient_balance_returns_error(
    client, auth_headers, db, sample_user, loyalty_account, recipient_user
):
    response = client.post(
        "/api/v1/loyalty/gift-points",
        headers=auth_headers,
        json={"recipient_phone": recipient_user.phone, "points_amount": 5000},
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["success"] is False
    # No points moved.
    assert LoyaltyService().get_available_points(sample_user.id) == 1000


@pytest.mark.integration
@pytest.mark.api
def test_gift_points_to_self_returns_error(client, auth_headers, db, sample_user, loyalty_account):
    response = client.post(
        "/api/v1/loyalty/gift-points",
        headers=auth_headers,
        json={"recipient_phone": sample_user.phone, "points_amount": 100},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    # Balance untouched.
    assert LoyaltyService().get_available_points(sample_user.id) == 1000


@pytest.mark.integration
@pytest.mark.api
def test_gift_points_unknown_phone_returns_not_found(client, auth_headers, db, sample_user, loyalty_account):
    """A valid-but-unregistered UZS number resolves to NotFoundError -> 404."""
    response = client.post(
        "/api/v1/loyalty/gift-points",
        headers=auth_headers,
        json={"recipient_phone": "+998905556677", "points_amount": 100},
    )

    assert response.status_code == 404
    assert response.get_json()["success"] is False
    assert LoyaltyService().get_available_points(sample_user.id) == 1000


@pytest.mark.integration
@pytest.mark.api
def test_gift_points_invalid_phone_returns_error(client, auth_headers, db, sample_user, loyalty_account):
    """An unparseable phone fails phone validation -> 400 error envelope."""
    response = client.post(
        "/api/v1/loyalty/gift-points",
        headers=auth_headers,
        json={"recipient_phone": "12345", "points_amount": 100},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


# ---------------------------------------------------------------------------
# Earn points
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
def test_earn_points_creates_transaction_for_valid_action(
    client, auth_headers, db, sample_user, loyalty_program
):
    """A valid action awards program-derived points and persists a transaction."""
    response = client.post(
        "/api/v1/loyalty/earn-points",
        headers=auth_headers,
        json={"action": "birthday_bonus"},
    )

    assert response.status_code == 201
    txn = response.get_json()["data"]["transaction"]
    # birthday_bonus -> get_action_points reads program.birthday_bonus (default 25).
    assert txn["points"] == 25

    # A BONUS transaction lands in the ledger and the balance reflects it.
    bonus = LoyaltyTransaction.query.filter_by(user_id=sample_user.id).all()
    assert any(
        t.transaction_type == LoyaltyTransactionType.BONUS
        and (t.extra_data or {}).get("action_type") == LoyaltyActionType.BIRTHDAY_BONUS.value
        for t in bonus
    )
    assert LoyaltyService().get_available_points(sample_user.id) == 25


@pytest.mark.integration
@pytest.mark.api
def test_earn_points_honours_explicit_points_amount(client, auth_headers, db, sample_user, loyalty_program):
    response = client.post(
        "/api/v1/loyalty/earn-points",
        headers=auth_headers,
        json={"action": "review_submitted", "points_amount": 70},
    )

    assert response.status_code == 201
    assert response.get_json()["data"]["transaction"]["points"] == 70
    assert LoyaltyService().get_available_points(sample_user.id) == 70


@pytest.mark.integration
@pytest.mark.api
def test_earn_points_rejects_invalid_action(client, auth_headers, db, sample_user, loyalty_program):
    response = client.post(
        "/api/v1/loyalty/earn-points",
        headers=auth_headers,
        json={"action": "not_a_real_action"},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    # Nothing awarded.
    assert LoyaltyTransaction.query.filter_by(user_id=sample_user.id).count() == 0


@pytest.mark.integration
@pytest.mark.api
def test_earn_points_missing_action_field_rejected(client, auth_headers, db, sample_user, loyalty_program):
    """The @validate_json(["action"]) guard rejects a body without 'action'."""
    response = client.post(
        "/api/v1/loyalty/earn-points",
        headers=auth_headers,
        json={"points_amount": 50},
    )

    # @validate_json rejects with its own envelope: {"error", "missing_fields"}.
    assert response.status_code == 400
    body = response.get_json()
    assert "action" in body["missing_fields"]


# ---------------------------------------------------------------------------
# Admin reward CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
def test_admin_create_discount_reward(client, admin_auth_headers, db, loyalty_program):
    response = client.post(
        "/api/v1/admin/loyalty/rewards",
        headers=admin_auth_headers,
        json={
            "name": "1000 off",
            "reward_type": "discount",
            "points_cost": 150,
            "discount_type": "fixed",
            "discount_value": "1000.00",
        },
    )

    assert response.status_code == 201
    reward = response.get_json()["data"]["reward"]
    assert reward["reward_type"] == "discount"
    assert reward["points_cost"] == 150
    # Persisted in DB.
    assert LoyaltyReward.query.filter_by(name="1000 off").count() == 1


@pytest.mark.integration
@pytest.mark.api
def test_admin_create_free_product_reward(client, admin_auth_headers, db, loyalty_program, sample_product):
    response = client.post(
        "/api/v1/admin/loyalty/rewards",
        headers=admin_auth_headers,
        json={
            "name": "Free bottle",
            "reward_type": "free_product",
            "points_cost": 200,
            "free_product_id": sample_product.id,
            "free_product_quantity": 2,
        },
    )

    assert response.status_code == 201
    reward = response.get_json()["data"]["reward"]
    assert reward["reward_type"] == "free_product"
    assert reward["free_product_id"] == sample_product.id
    assert reward["free_product_quantity"] == 2


@pytest.mark.integration
@pytest.mark.api
@pytest.mark.parametrize("removed_type", ["voucher", "free_delivery"])
def test_admin_create_removed_reward_type_rejected(
    client, admin_auth_headers, db, loyalty_program, removed_type
):
    """voucher/free_delivery are removed types -> 400 validation error."""
    response = client.post(
        "/api/v1/admin/loyalty/rewards",
        headers=admin_auth_headers,
        json={"name": removed_type, "reward_type": removed_type, "points_cost": 100},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert LoyaltyReward.query.filter_by(reward_type=removed_type).count() == 0


@pytest.mark.integration
@pytest.mark.api
def test_admin_create_discount_missing_type_and_value_rejected(
    client, admin_auth_headers, db, loyalty_program
):
    response = client.post(
        "/api/v1/admin/loyalty/rewards",
        headers=admin_auth_headers,
        json={"name": "Bad discount", "reward_type": "discount", "points_cost": 100},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


@pytest.mark.integration
@pytest.mark.api
def test_admin_create_free_product_missing_product_id_rejected(
    client, admin_auth_headers, db, loyalty_program
):
    response = client.post(
        "/api/v1/admin/loyalty/rewards",
        headers=admin_auth_headers,
        json={"name": "Bad free product", "reward_type": "free_product", "points_cost": 100},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


@pytest.mark.integration
@pytest.mark.api
def test_admin_update_reward_fields(client, admin_auth_headers, db, discount_reward):
    response = client.put(
        f"/api/v1/admin/loyalty/rewards/{discount_reward.id}",
        headers=admin_auth_headers,
        json={"name": "Updated name", "points_cost": 250, "discount_value": "750.00"},
    )

    assert response.status_code == 200
    reward = response.get_json()["data"]["reward"]
    assert reward["points_cost"] == 250

    db.session.refresh(discount_reward)
    assert discount_reward.name == "Updated name"
    assert discount_reward.points_cost == 250
    assert discount_reward.discount_value == Decimal("750.00")


@pytest.mark.integration
@pytest.mark.api
def test_admin_delete_unused_reward_removes_row(client, admin_auth_headers, db, discount_reward):
    reward_id = discount_reward.id
    response = client.delete(
        f"/api/v1/admin/loyalty/rewards/{reward_id}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    # Unused reward (redemptions_used == 0) is hard-deleted.
    assert LoyaltyReward.query.get(reward_id) is None


@pytest.mark.integration
@pytest.mark.api
def test_admin_delete_used_reward_deactivates_instead(client, admin_auth_headers, db, discount_reward):
    """A reward with redemptions is soft-deactivated, not deleted."""
    discount_reward.redemptions_used = 3
    db.session.commit()
    reward_id = discount_reward.id

    response = client.delete(
        f"/api/v1/admin/loyalty/rewards/{reward_id}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    persisted = LoyaltyReward.query.get(reward_id)
    assert persisted is not None
    assert persisted.is_active is False


# ---------------------------------------------------------------------------
# Admin member / transactions / analytics
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.api
def test_admin_member_detail_returns_member_and_recent_redemptions(
    client, admin_auth_headers, db, sample_user, loyalty_account, discount_reward
):
    """Member detail includes the serialized member and its recent redemptions card."""
    LoyaltyService().deduct_points(sample_user.id, 100, "Redeemed reward", order_id=discount_reward.id)

    response = client.get(
        f"/api/v1/admin/loyalty/members/{sample_user.id}",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["member"]["user_id"] == sample_user.id
    assert data["member"]["current_balance"] == 900
    assert len(data["recent_redemptions"]) == 1
    assert data["recent_redemptions"][0]["points"] == -100


@pytest.mark.integration
@pytest.mark.api
def test_admin_member_detail_unknown_user_returns_404(client, admin_auth_headers, db, loyalty_program):
    response = client.get(
        "/api/v1/admin/loyalty/members/999999",
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.get_json()["success"] is False


@pytest.mark.integration
@pytest.mark.api
def test_admin_member_transactions_paginates(
    client, admin_auth_headers, db, sample_user, loyalty_program
):
    """Seed 12 transactions; assert total, page sizing, and per_page cap at 100."""
    account = LoyaltyPoints(
        user_id=sample_user.id, program_id=loyalty_program.id, total_earned=0, current_balance=0
    )
    db.session.add(account)
    db.session.flush()
    base = datetime.now(timezone.utc)
    for i in range(12):
        db.session.add(
            LoyaltyTransaction(
                user_id=sample_user.id,
                transaction_type=LoyaltyTransactionType.EARNED,
                points=10,
                remaining_points=10,
                description=f"txn {i}",
                expires_at=FAR_FUTURE,
                created_at=base + timedelta(minutes=i),
            )
        )
    db.session.commit()

    # First page of 5.
    first = client.get(
        f"/api/v1/admin/loyalty/members/{sample_user.id}/transactions?page=1&per_page=5",
        headers=admin_auth_headers,
    )
    assert first.status_code == 200
    first_payload = first.get_json()
    assert first_payload["meta"]["total"] == 12
    assert len(first_payload["data"]["items"]) == 5

    # Last (third) page holds the remaining 2.
    third = client.get(
        f"/api/v1/admin/loyalty/members/{sample_user.id}/transactions?page=3&per_page=5",
        headers=admin_auth_headers,
    )
    assert third.status_code == 200
    assert len(third.get_json()["data"]["items"]) == 2

    # per_page is capped at 100 even when a larger value is requested.
    capped = client.get(
        f"/api/v1/admin/loyalty/members/{sample_user.id}/transactions?page=1&per_page=500",
        headers=admin_auth_headers,
    )
    assert capped.status_code == 200
    assert capped.get_json()["meta"]["per_page"] == 100


@pytest.mark.integration
@pytest.mark.api
def test_admin_analytics_returns_summary(
    client, admin_auth_headers, db, sample_user, loyalty_account, discount_reward
):
    """Analytics computes a members/points summary from the real ledger."""
    LoyaltyService().deduct_points(sample_user.id, 100, "Redeemed reward", order_id=discount_reward.id)

    response = client.get(
        "/api/v1/admin/loyalty/analytics",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    data = response.get_json()["data"]
    summary = data["summary"]
    assert summary["total_members"] == 1
    assert summary["active_members"] == 1  # balance 900 > 0
    assert summary["total_points_in_circulation"] == 900
    # The recent redeem (within the default 30-day window) is counted.
    assert summary["points_redeemed"] == 100
    assert "tier_distribution" in data
    assert "redemption_metrics" in data


def test_points_summary_reports_the_qualifying_figure(db, sample_user):
    """The bot screen must show what decides the tier, not the lifetime total."""
    from decimal import Decimal

    from business_app.models.loyalty import LoyaltyPoints
    from business_app.services.loyalty_service import LoyaltyService
    from tests.integration.tier_discount_factory import seed_account, seed_program, seed_tier

    program = seed_program(db)
    seed_tier(db, program, name="Bronze", rate=Decimal("0"), min_points=0, display_order=0)
    seed_tier(db, program, name="Silver", rate=Decimal("1.5"), min_points=4000, display_order=1)
    seed_tier(db, program, name="Gold", rate=Decimal("2"), min_points=15000, display_order=2)
    seed_account(db, sample_user, program, qualifying_points=3488, balance=988)
    account = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    account.current_tier = "Silver"
    db.session.commit()

    summary = LoyaltyService().get_points_summary_for_user(sample_user.id)

    assert summary["qualifying_points"] == 3488
    assert summary["lifetime_earned"] == 3488
    assert summary["tier"] == "Silver"
    assert summary["tier_discount_percentage"] == 1.5
    assert summary["next_tier"] == "Gold"
    assert summary["points_to_next_tier"] == 15000 - 3488
    assert summary["points_needed_to_keep"] == 4000 - 3488
