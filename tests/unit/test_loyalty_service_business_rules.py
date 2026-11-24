"""Business-rule unit tests for LoyaltyService earn/redeem/expiry logic."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.models.product import Product, ProductSizeEnum
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

    def test_calculate_points_for_purchase_uses_program_ratio_and_tier_multiplier(
        self, loyalty_service, monkeypatch, db, sample_user
    ):
        # A REAL user is required: calculate_points_for_purchase checks loyalty
        # eligibility before touching the account, so an id with no user row
        # resolves to ineligible and earns 0. effective_tier() also queries
        # LoyaltyTierConfig/LoyaltyTransaction directly. No tier is seeded for
        # the program, so effective_tier() returns None and the badge "Gold" falls
        # through unchanged to _get_tier_multiplier, which this test still stubs.
        program = LoyaltyProgram(name="Ratio program", is_active=True, is_default=True, uzs_per_point=200)
        db.session.add(program)
        db.session.flush()
        db.session.add(
            LoyaltyPoints(user_id=sample_user.id, program_id=program.id, current_balance=0, current_tier="Gold")
        )
        db.session.commit()
        asked = []

        def multiplier_for(tier_name, program_id=None):  # _get_tier_multiplier's own signature
            asked.append((tier_name, program_id))
            return 1.5

        monkeypatch.setattr(loyalty_service, "_get_tier_multiplier", multiplier_for)

        points = loyalty_service.calculate_points_for_purchase(user_id=sample_user.id, amount=1000)

        assert points == 7  # floor(1000/200)=5, multiplier=1.5 => 7.5 -> int(7)
        assert asked == [("Gold", program.id)]

    def test_a_first_timers_points_are_worked_out_without_creating_his_account(self, loyalty_service, db, sample_user):
        """A customer with no loyalty account earns what his first account would give him, and the calculation writes
        nothing: no account row (even unflushed or flushed), and the caller's transaction is not ended. A flushed first
        account held his unique user id until the caller's commit, so any other first-account write for him waited on
        it and then failed. The figures are those of the account `get_or_create_loyalty_account` then creates."""
        program = LoyaltyProgram(name="First-timer program", is_active=True, is_default=True, uzs_per_point=200)
        db.session.add(program)
        db.session.flush()
        db.session.add(
            LoyaltyTierConfig(
                program_id=program.id,
                name="Starter",
                display_order=0,
                min_points=0,
                points_multiplier=1.5,
                is_active=True,
            )
        )
        db.session.commit()
        user_id = sample_user.id
        assert LoyaltyPoints.query.filter_by(user_id=user_id).count() == 0
        transaction = db.session().get_transaction()  # the scoped session's own Session
        assert transaction is not None

        points = loyalty_service.calculate_points_for_purchase(user_id=user_id, amount=1000)

        assert points == 7  # floor(1000/200)=5, the 0-point tier's x1.5 => 7.5 -> int(7)
        assert db.session().get_transaction() is transaction
        assert [obj for obj in db.session.new if isinstance(obj, LoyaltyPoints)] == []
        assert LoyaltyPoints.query.filter_by(user_id=user_id).count() == 0

        account = loyalty_service.get_or_create_loyalty_account(user_id)
        assert (account.program_id, account.current_tier) == (program.id, "Starter")
        assert loyalty_service.calculate_points_for_purchase(user_id=user_id, amount=1000) == 7

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
        # D1: award_points parks notifications on session.info for the
        # after_commit listener (loyalty_award_dispatch.py) rather than calling
        # _send_points_notification directly. Notification dispatch is tested
        # separately; this test focuses on the account and transaction outcomes.

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


@pytest.mark.unit
def test_is_reward_configured_truth_table(db, loyalty_program, sample_category):
    svc = LoyaltyService()
    active_product = Product(name="P", base_price=Decimal("1000"), category_id=sample_category.id,
                             size=ProductSizeEnum.SIZE_19L, is_active=True)
    inactive_product = Product(name="PX", base_price=Decimal("1000"), category_id=sample_category.id,
                               size=ProductSizeEnum.SIZE_19L, is_active=False)
    _db.session.add_all([active_product, inactive_product]); _db.session.commit()

    def reward(**kw):
        base = dict(program_id=loyalty_program.id, name="r", points_cost=10, is_active=True)
        base.update(kw)
        return LoyaltyReward(**base)

    # discount
    assert svc.is_reward_configured(reward(reward_type="discount", discount_type="fixed",
                                           discount_value=Decimal("500"))) is True
    assert svc.is_reward_configured(reward(reward_type="discount", discount_type="fixed",
                                           discount_value=Decimal("0"))) is False
    assert svc.is_reward_configured(reward(reward_type="discount", discount_value=Decimal("500"))) is False  # no type
    # free_product
    assert svc.is_reward_configured(reward(reward_type="free_product",
                                           free_product_id=active_product.id, free_product_quantity=2)) is True
    assert svc.is_reward_configured(reward(reward_type="free_product", free_product_id=None)) is False
    assert svc.is_reward_configured(reward(reward_type="free_product",
                                           free_product_id=inactive_product.id)) is False
    assert svc.is_reward_configured(reward(reward_type="free_product",
                                           free_product_id=active_product.id,
                                           free_product_quantity=0)) is False
    # unsupported / removed types are never configured
    assert svc.is_reward_configured(reward(reward_type="free_delivery")) is False
    assert svc.is_reward_configured(reward(reward_type="voucher")) is False


@pytest.mark.unit
def test_get_rewards_for_user_excludes_misconfigured(db, sample_user, loyalty_program):
    svc = LoyaltyService()
    good = LoyaltyReward(program_id=loyalty_program.id, name="ok", reward_type="discount", points_cost=10,
                         discount_type="fixed", discount_value=Decimal("500"), is_active=True)
    bad = LoyaltyReward(program_id=loyalty_program.id, name="bad", reward_type="free_product", points_cost=10,
                        free_product_id=None, is_active=True)
    _db.session.add_all([good, bad]); _db.session.commit()

    payload = svc.get_rewards_for_user(sample_user.id)
    names = {r.name for r in payload["rewards"]}
    assert "ok" in names and "bad" not in names
    # user has no points -> good reward is listed but not redeemable
    assert payload["can_redeem_by_id"][good.id] is False
