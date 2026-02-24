"""Business-rule unit tests for LoyaltyService earn/redeem/expiry logic."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyReward, LoyaltyTransaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType
from business_app.utils.exceptions import NotFoundError, ValidationError


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for tests",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def loyalty_account(db, sample_user, loyalty_program):
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
    db.session.commit()
    return account


@pytest.fixture
def active_reward(db, loyalty_program):
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="Free Delivery Voucher",
        description="Free delivery for one order",
        reward_type="voucher",
        points_cost=300,
        discount_type="fixed",
        discount_value=Decimal("0.00"),
        is_active=True,
        is_system_reward=False,
        valid_until=datetime.now(timezone.utc) + timedelta(days=7),
        redemptions_used=0,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.mark.unit
class TestLoyaltyServiceBusinessRules:
    def test_calculate_points_for_purchase_returns_zero_for_non_positive_amount(self, loyalty_service):
        assert loyalty_service.calculate_points_for_purchase(user_id=1, amount=0) == 0
        assert loyalty_service.calculate_points_for_purchase(user_id=1, amount=-50) == 0

    def test_calculate_points_for_purchase_uses_program_ratio_and_tier_multiplier(self, loyalty_service, monkeypatch):
        fake_account = SimpleNamespace(
            program=SimpleNamespace(uzs_per_point=200),
            current_tier="Gold",
            program_id=42,
        )
        monkeypatch.setattr(loyalty_service, "get_or_create_loyalty_account", lambda _uid: fake_account)
        monkeypatch.setattr(loyalty_service, "_get_tier_multiplier", lambda _tier, _pid: 1.5)

        points = loyalty_service.calculate_points_for_purchase(user_id=10, amount=1000)

        assert points == 7  # floor(1000/200)=5, multiplier=1.5 => 7.5 -> int(7)

    def test_get_or_create_loyalty_account_raises_not_found_for_missing_user(self, loyalty_service, db):
        with pytest.raises(NotFoundError, match="User not found"):
            loyalty_service.get_or_create_loyalty_account(user_id=999999)

    def test_award_points_rejects_non_positive_values(self, loyalty_service):
        with pytest.raises(ValidationError, match="Points must be positive"):
            loyalty_service.award_points(user_id=1, points=0, description="invalid")

    def test_award_points_updates_account_and_creates_bonus_transaction(
        self,
        loyalty_service,
        loyalty_account,
        db,
        monkeypatch,
    ):
        monkeypatch.setattr(loyalty_service, "_check_tier_upgrade", lambda _account: None)
        notify = Mock()
        monkeypatch.setattr(loyalty_service, "_send_points_notification", notify)

        tx = loyalty_service.award_points(
            user_id=loyalty_account.user_id,
            points=250,
            description="Referral bonus",
            action_type=LoyaltyActionType.REFERRAL,
        )

        db.session.refresh(loyalty_account)
        assert tx.points == 250
        assert tx.transaction_type == LoyaltyTransactionType.BONUS
        assert loyalty_account.current_balance == 1250
        assert loyalty_account.total_earned == 1250
        notify.assert_called_once()

    def test_deduct_points_rejects_when_balance_insufficient(self, loyalty_service, loyalty_account, monkeypatch):
        monkeypatch.setattr(loyalty_service, "get_available_points", lambda _uid: 100)

        with pytest.raises(ValidationError, match="Insufficient points"):
            loyalty_service.deduct_points(
                user_id=loyalty_account.user_id,
                points=200,
                description="Insufficient test",
            )

    def test_deduct_points_success_updates_balance(self, loyalty_service, loyalty_account, db, monkeypatch):
        monkeypatch.setattr(loyalty_service, "get_available_points", lambda _uid: 1000)

        tx = loyalty_service.deduct_points(
            user_id=loyalty_account.user_id,
            points=300,
            description="Redeem points",
            skip_notification=True,
        )

        db.session.refresh(loyalty_account)
        assert tx.points == -300
        assert tx.transaction_type == LoyaltyTransactionType.REDEEMED
        assert loyalty_account.current_balance == 700
        assert loyalty_account.total_redeemed == 300

    def test_deduct_points_can_trigger_notification(self, loyalty_service, loyalty_account, monkeypatch):
        monkeypatch.setattr(loyalty_service, "get_available_points", lambda _uid: 1000)
        notify = Mock()
        monkeypatch.setattr(loyalty_service, "_send_points_notification", notify)

        loyalty_service.deduct_points(
            user_id=loyalty_account.user_id,
            points=100,
            description="Redeem with notification",
            skip_notification=False,
            notification_type_str="reward_redeemed",
        )

        notify.assert_called_once_with(loyalty_account.user_id, 100, "redeemed", "reward_redeemed")

    def test_redeem_reward_rejects_system_rewards(self, loyalty_service, active_reward, db):
        active_reward.is_system_reward = True
        db.session.commit()

        with pytest.raises(ValidationError, match="automatically applied"):
            loyalty_service.redeem_reward(user_id=1, reward_id=active_reward.id)

    def test_redeem_reward_rejects_when_user_points_are_insufficient(self, loyalty_service, active_reward, monkeypatch):
        monkeypatch.setattr(loyalty_service, "get_available_points", lambda _uid: 100)

        with pytest.raises(ValidationError, match="Insufficient points"):
            loyalty_service.redeem_reward(user_id=1, reward_id=active_reward.id)

    def test_redeem_reward_success_increments_usage_and_returns_payload(
        self,
        loyalty_service,
        active_reward,
        db,
        monkeypatch,
    ):
        monkeypatch.setattr(loyalty_service, "get_available_points", lambda _uid: 5000)
        monkeypatch.setattr(
            loyalty_service,
            "deduct_points",
            lambda *args, **kwargs: SimpleNamespace(id=777),
        )
        monkeypatch.setattr(loyalty_service, "_generate_reward_code", lambda *_args, **_kwargs: "RWDTEST01")

        result = loyalty_service.redeem_reward(user_id=1, reward_id=active_reward.id)

        db.session.refresh(active_reward)
        assert result["transaction_id"] == 777
        assert result["points_spent"] == active_reward.points_cost
        assert result["redemption_code"] == "RWDTEST01"
        assert active_reward.redemptions_used == 1

    def test_remove_expired_points_marks_transactions_and_reduces_balance(
        self,
        loyalty_service,
        loyalty_account,
        db,
    ):
        expired_tx = LoyaltyTransaction(
            user_id=loyalty_account.user_id,
            transaction_type=LoyaltyTransactionType.EARNED,
            points=200,
            description="Old points",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            is_expired=False,
        )
        active_tx = LoyaltyTransaction(
            user_id=loyalty_account.user_id,
            transaction_type=LoyaltyTransactionType.EARNED,
            points=150,
            description="Active points",
            expires_at=datetime.now(timezone.utc) + timedelta(days=10),
            is_expired=False,
        )
        db.session.add_all([expired_tx, active_tx])
        db.session.commit()

        loyalty_service._remove_expired_points(loyalty_account.user_id)
        db.session.refresh(loyalty_account)
        db.session.refresh(expired_tx)
        db.session.refresh(active_tx)

        assert expired_tx.is_expired is True
        assert active_tx.is_expired is False
        assert loyalty_account.current_balance == 800

    def test_validate_discount_code_currently_returns_zero(self, loyalty_service, sample_user):
        assert loyalty_service.validate_discount_code("ANY-CODE", sample_user.id) == 0
