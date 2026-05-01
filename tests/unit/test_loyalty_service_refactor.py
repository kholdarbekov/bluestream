"""Service-level regressions for loyalty API boundary migration."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from business_app.models.user import User
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType
from shared.enums import UserRole
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name='Default Program',
        description='Default loyalty program for tests',
        is_active=True,
        is_default=True,
        uzs_per_point=250,
    )
    db.session.add(program)
    db.session.commit()
    return program


def _create_loyalty_account(db, user_id: int, program_id: int, balance: int = 0) -> LoyaltyPoints:
    account = LoyaltyPoints(
        user_id=user_id,
        program_id=program_id,
        total_earned=balance,
        total_redeemed=0,
        total_expired=0,
        current_balance=balance,
        current_tier='Bronze',
        points_to_next_tier=500,
    )
    db.session.add(account)
    db.session.commit()
    return account


def _create_reward(db, program_id: int, points_cost: int = 200, reward_type: str = 'voucher') -> LoyaltyReward:
    reward = LoyaltyReward(
        program_id=program_id,
        name='Reward Test',
        description='Reward for tests',
        reward_type=reward_type,
        points_cost=points_cost,
        discount_type='fixed',
        discount_value=Decimal('0.00'),
        is_active=True,
        is_system_reward=False,
        redemptions_used=0,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


def test_get_account_dashboard_for_user_aggregates_metrics(db, sample_user, loyalty_service, loyalty_program):
    _create_loyalty_account(db, sample_user.id, loyalty_program.id, balance=400)
    _create_reward(db, loyalty_program.id, points_cost=100)

    txn = LoyaltyTransaction(
        user_id=sample_user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=120,
        description='Monthly earn',
        created_at=datetime.now(UTC),
    )
    db.session.add(txn)
    db.session.commit()

    payload = loyalty_service.get_account_dashboard_for_user(sample_user.id)

    assert payload['current_balance'] == 400
    assert payload['points_this_month'] == 120
    assert payload['available_rewards_count'] == 1


def test_get_filtered_points_history_for_user_rejects_invalid_type(
    db,
    sample_user,
    loyalty_service,
    loyalty_program,
):
    _create_loyalty_account(db, sample_user.id, loyalty_program.id, balance=100)

    with pytest.raises(ValidationError):
        loyalty_service.get_filtered_points_history_for_user(
            user_id=sample_user.id,
            page=1,
            per_page=20,
            transaction_type='bad-type',
        )


def test_redeem_reward_for_user_returns_remaining_points(
    db,
    sample_user,
    loyalty_service,
    loyalty_program,
):
    _create_loyalty_account(db, sample_user.id, loyalty_program.id, balance=500)
    reward = _create_reward(db, loyalty_program.id, points_cost=200)

    payload = loyalty_service.redeem_reward_for_user(sample_user.id, reward.id)

    assert payload['redemption']['points_spent'] == 200
    assert payload['remaining_points'] == 300


def test_gift_points_by_phone_rejects_self(db, sample_user, loyalty_service, loyalty_program):
    _create_loyalty_account(db, sample_user.id, loyalty_program.id, balance=500)

    with pytest.raises(ValidationError):
        loyalty_service.gift_points_by_phone(
            sender_id=sample_user.id,
            recipient_phone=sample_user.phone,
            points_amount=100,
            message='self',
        )


def test_gift_points_by_phone_transfers_points(db, sample_user, loyalty_service, loyalty_program):
    recipient = User(
        email='recipient@example.com',
        phone='+998901112233',
        password_hash=hash_password('RecipientPassword123!'),
        first_name='Recipient',
        last_name='User',
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(recipient)
    db.session.commit()

    sender_account = _create_loyalty_account(db, sample_user.id, loyalty_program.id, balance=500)
    recipient_account = _create_loyalty_account(db, recipient.id, loyalty_program.id, balance=100)

    loyalty_service.gift_points_by_phone(
        sender_id=sample_user.id,
        recipient_phone=recipient.phone,
        points_amount=120,
        message='gift',
    )

    db.session.refresh(sender_account)
    db.session.refresh(recipient_account)
    assert sender_account.current_balance == 380
    assert recipient_account.current_balance == 220
