"""Unit tests for admin loyalty query/service contracts."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import event

from business_app import db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.services.admin_loyalty_service import AdminLoyaltyService
from business_app.utils.constants import LoyaltyTransactionType


def _create_program() -> LoyaltyProgram:
    program = LoyaltyProgram(
        name="Gold Club",
        description="Primary loyalty program",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
    )
    db.session.add(program)
    db.session.commit()
    return program


def test_list_members_returns_summary_and_canonical_fields(db, sample_user):
    program = _create_program()
    tier = LoyaltyTierConfig(
        program_id=program.id,
        name="Gold",
        display_order=1,
        min_points=500,
        max_points=None,
        points_multiplier=1.2,
        discount_percentage=5,
        is_active=True,
    )
    account = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=program.id,
        total_earned=700,
        total_redeemed=100,
        current_balance=600,
        current_tier="Gold",
        points_to_next_tier=0,
        last_activity_date=datetime(2026, 2, 25, tzinfo=UTC),
    )
    db.session.add_all([tier, account])
    db.session.commit()

    payload = AdminLoyaltyService.list_members(search="Test")

    assert payload["total"] == 1
    assert payload["summary"]["total_members"] == 1
    assert payload["summary"]["total_points_in_circulation"] == 600
    item = payload["items"][0]
    assert item["user_id"] == sample_user.id
    assert item["customer_name"] == "Test User"
    assert item["program_name"] == "Gold Club"
    assert item["current_tier"] == "Gold"
    assert item["current_balance"] == 600


def test_list_members_uses_bounded_query_count(db, sample_user):
    program = _create_program()
    account = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=program.id,
        total_earned=700,
        total_redeemed=100,
        current_balance=600,
        current_tier="Gold",
        points_to_next_tier=0,
        last_activity_date=datetime(2026, 2, 25, tzinfo=UTC),
    )
    db.session.add(account)
    db.session.commit()

    statements = []

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", _before_cursor_execute)
    try:
        payload = AdminLoyaltyService.list_members(search="Test")
    finally:
        event.remove(db.engine, "before_cursor_execute", _before_cursor_execute)

    assert payload["total"] == 1
    assert len(statements) <= 3


def test_list_programs_returns_batched_member_and_tier_counts(db, sample_user):
    program = _create_program()
    tier = LoyaltyTierConfig(
        program_id=program.id,
        name="Gold",
        display_order=1,
        min_points=500,
        max_points=None,
        points_multiplier=1.2,
        discount_percentage=5,
        is_active=True,
    )
    account = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=program.id,
        total_earned=700,
        total_redeemed=100,
        current_balance=600,
        current_tier="Gold",
        points_to_next_tier=0,
    )
    db.session.add_all([tier, account])
    db.session.commit()

    payload = AdminLoyaltyService.list_programs()

    assert payload["total"] == 1
    assert payload["items"][0]["member_count"] == 1
    assert payload["items"][0]["tier_count"] == 1


def test_get_analytics_returns_summary_breakdowns_and_trends(db, sample_user):
    program = _create_program()
    account = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=program.id,
        total_earned=900,
        total_redeemed=200,
        current_balance=700,
        current_tier="Gold",
        points_to_next_tier=0,
    )
    reward = LoyaltyReward(
        program_id=program.id,
        name="Discount Voucher",
        description="Discount reward",
        reward_type="voucher",
        points_cost=200,
        discount_type="fixed",
        discount_value=Decimal("10000.00"),
        is_active=True,
        redemptions_used=3,
    )
    earned_txn = LoyaltyTransaction(
        user_id=sample_user.id,
        points=500,
        transaction_type=LoyaltyTransactionType.EARNED,
        description="Purchase points",
        created_at=datetime(2026, 2, 20, tzinfo=UTC),
    )
    redeemed_txn = LoyaltyTransaction(
        user_id=sample_user.id,
        points=-200,
        transaction_type=LoyaltyTransactionType.REDEEMED,
        description="Reward redemption",
        created_at=datetime(2026, 2, 21, tzinfo=UTC),
    )
    db.session.add_all([account, reward, earned_txn, redeemed_txn])
    db.session.commit()

    payload = AdminLoyaltyService.get_analytics(
        start_date="2026-02-01",
        end_date="2026-02-28",
    )

    assert payload["summary"]["total_members"] == 1
    assert payload["summary"]["total_points_in_circulation"] == 700
    assert payload["summary"]["points_earned"] == 500
    assert payload["summary"]["points_redeemed"] == 200
    assert payload["tier_distribution"][0]["tier"] == "Gold"
    assert payload["top_rewards"][0]["name"] == "Discount Voucher"
    assert payload["program_breakdown"][0]["program_name"] == "Gold Club"
    assert payload["points_trend"][0]["date"] == "2026-02-20"
