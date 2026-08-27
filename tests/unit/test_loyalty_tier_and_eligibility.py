"""Unit tests for LoyaltyService tier progression and reward eligibility.

Characterization tests of the EXISTING behavior in LoyaltyService:
- _check_tier_upgrade (via award_points / check_tier_expiration): upgrade,
  requalify (lock refresh), and downgrade-only-after-lock-expiry semantics.
- calculate_points_for_purchase floor + tier-multiplier int() truncation.
- calculate_tier_progress / get_tier_upgrade_requirements / get_requalification_info shapes.
- can_redeem_reward eligibility matrix.
- is_reward_configured structural matrix.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
    RewardRedemption,
)
from business_app.models.product import Product, ProductSizeEnum
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyActionType, LoyaltyTransactionType


@pytest.fixture(autouse=True)
def _silence_notifications(loyalty_notification_spy):
    """Tier/award flows fire notifications; spy on them (signature-enforcing)."""
    return loyalty_notification_spy


@pytest.fixture
def loyalty_service(app):
    with app.app_context():
        return LoyaltyService()


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program for tier tests",
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
def tiers(db, loyalty_program):
    """Bronze (0+), Silver (500+), Gold (1500+) with ascending display_order."""
    bronze = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Bronze", display_order=0,
        min_points=0, max_points=499, points_multiplier=1.0, is_active=True,
    )
    silver = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Silver", display_order=1,
        min_points=500, max_points=1499, points_multiplier=1.0, is_active=True,
    )
    gold = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Gold", display_order=2,
        min_points=1500, max_points=None, points_multiplier=1.0, is_active=True,
    )
    db.session.add_all([bronze, silver, gold])
    db.session.commit()
    return {"Bronze": bronze, "Silver": silver, "Gold": gold}


@pytest.fixture
def account(db, sample_user, loyalty_program):
    """Bronze account at 0 points, no tier lock."""
    acc = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=loyalty_program.id,
        total_earned=0,
        total_redeemed=0,
        total_expired=0,
        current_balance=0,
        current_tier="Bronze",
        points_to_next_tier=500,
    )
    db.session.add(acc)
    db.session.commit()
    return acc


def _earn_lot(user_id, points, *, txn_type=LoyaltyTransactionType.EARNED):
    """Add a live EARNED/BONUS lot that counts toward qualifying points."""
    lot = LoyaltyTransaction(
        user_id=user_id,
        transaction_type=txn_type,
        points=points,
        remaining_points=points,
        description="seed lot",
        expires_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
    )
    _db.session.add(lot)
    _db.session.commit()
    return lot


# --------------------------------------------------------------------------
# Tier progression
# --------------------------------------------------------------------------
@pytest.mark.unit
class TestTierProgression:
    def test_award_points_upgrades_bronze_to_silver_with_365d_lock(
        self, loyalty_service, account, tiers, db
    ):
        before = datetime.now(timezone.utc)
        loyalty_service.award_points(
            user_id=account.user_id, points=600, description="purchase",
            action_type=LoyaltyActionType.PURCHASE,
        )
        db.session.refresh(account)

        assert account.current_tier == "Silver"
        assert account.tier_valid_until is not None
        valid_until = account.tier_valid_until
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        # Lock is ~ now + 365 days.
        expected = before + timedelta(days=365)
        assert abs((valid_until - expected).total_seconds()) < 120

    def test_crossing_gold_threshold_upgrades_to_gold(self, loyalty_service, account, tiers, db):
        loyalty_service.award_points(
            user_id=account.user_id, points=1600, description="big purchase",
            action_type=LoyaltyActionType.PURCHASE,
        )
        db.session.refresh(account)
        assert account.current_tier == "Gold"

    def test_tier_upgrade_notification_carries_what_its_template_renders(
        self, loyalty_service, account, tiers, db, _silence_notifications
    ):
        """The payload the old no-op stub swallowed.

        The message says "you reached <tier>, balance <n>", so the sender must
        carry the tier's config id (its translated name lives there) and the
        post-award balance. Missing either is what leaked {points}/{balance}
        to a customer in production.
        """
        loyalty_service.award_points(
            user_id=account.user_id, points=1600, description="big purchase",
            action_type=LoyaltyActionType.PURCHASE,
        )

        calls = _silence_notifications.tier_upgrade.calls
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args == (account.user_id,)
        assert kwargs == {
            "tier": "Gold",
            "tier_config_id": tiers["Gold"].id,
            "balance": 1600,
        }

    def test_requalify_same_tier_refreshes_lock(self, loyalty_service, account, tiers, db):
        # First award lands at Silver and sets the lock.
        loyalty_service.award_points(
            user_id=account.user_id, points=600, description="first",
            action_type=LoyaltyActionType.PURCHASE,
        )
        db.session.refresh(account)
        # Manually age the lock so we can detect a refresh.
        account.tier_valid_until = datetime.now(timezone.utc) - timedelta(days=10)
        db.session.commit()

        # Another small award keeps the user in Silver (still 500-1499) and must
        # refresh the lock back into the future.
        loyalty_service.award_points(
            user_id=account.user_id, points=100, description="second",
            action_type=LoyaltyActionType.PURCHASE,
        )
        db.session.refresh(account)
        assert account.current_tier == "Silver"
        valid_until = account.tier_valid_until
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        assert valid_until > datetime.now(timezone.utc)

    def test_no_downgrade_while_lock_in_future_even_if_points_drop(
        self, loyalty_service, account, tiers, db
    ):
        # Put the user at Silver with a future lock, but with qualifying points
        # that have dropped below the Silver threshold (e.g. only a tiny lot).
        account.current_tier = "Silver"
        account.tier_valid_until = datetime.now(timezone.utc) + timedelta(days=100)
        db.session.commit()
        _earn_lot(account.user_id, 50)  # qualifying points = 50 (< 500)

        loyalty_service.check_tier_expiration(account.user_id)
        db.session.refresh(account)
        # Lock still in the future -> tier protected.
        assert account.current_tier == "Silver"

    def test_downgrade_happens_after_lock_expires_and_points_insufficient(
        self, loyalty_service, account, tiers, db
    ):
        account.current_tier = "Silver"
        account.tier_valid_until = datetime.now(timezone.utc) - timedelta(days=1)  # expired lock
        db.session.commit()
        _earn_lot(account.user_id, 50)  # qualifying points = 50 (< 500 Silver min)

        loyalty_service.check_tier_expiration(account.user_id)
        db.session.refresh(account)
        assert account.current_tier == "Bronze"
        assert account.tier_valid_until is None

    def test_bonus_points_count_toward_qualifying_for_upgrade(
        self, loyalty_service, account, tiers, db
    ):
        # BONUS-type awards (referral/welcome/birthday) DO count toward tier.
        loyalty_service.award_points(
            user_id=account.user_id, points=600, description="referral bonus",
            action_type=LoyaltyActionType.REFERRAL,
        )
        db.session.refresh(account)
        assert account.current_tier == "Silver"


# --------------------------------------------------------------------------
# calculate_points_for_purchase
# --------------------------------------------------------------------------
@pytest.mark.unit
class TestCalculatePointsForPurchase:
    def test_zero_and_negative_amount_yield_zero(self, loyalty_service, account, tiers):
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 0) == 0
        assert loyalty_service.calculate_points_for_purchase(account.user_id, -100) == 0

    def test_floor_division_by_uzs_per_point(self, loyalty_service, account, tiers):
        # 1000 / 250 = 4, multiplier 1.0 -> 4
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 1000) == 4
        # 1100 / 250 = floor(4.4) = 4
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 1100) == 4

    def test_tier_multiplier_is_int_truncated(self, loyalty_service, account, tiers, db):
        # Give Bronze a 1.5x multiplier so the truncation is observable.
        tiers["Bronze"].points_multiplier = 1.5
        db.session.commit()
        # 1250 / 250 = 5 base points; 5 * 1.5 = 7.5 -> floor to 7.
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 1250) == 7
        # 1000 / 250 = 4 base; 4 * 1.5 = 6.0 -> 6 (exact, no truncation).
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 1000) == 6

    def test_tier_multiplier_no_float_truncation_at_integer_boundary(
        self, loyalty_service, account, tiers, db
    ):
        # Regression: 1.15x is not exactly representable as a binary float, so
        # 360 * 1.15 evaluates to 413.99999999999994 and int() used to floor
        # it to 413. The EXACT product is 414, which is what must be awarded.
        tiers["Bronze"].points_multiplier = 1.15
        db.session.commit()
        # 90000 / 250 = 360 base points; 360 * 1.15 = 414 (exact integer).
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 90000) == 414
        # 3000 / 250 = 12 base; 12 * 1.15 = 13.8 -> floor to 13 (genuine fraction).
        assert loyalty_service.calculate_points_for_purchase(account.user_id, 3000) == 13


# --------------------------------------------------------------------------
# Tier progress / upgrade requirements / requalification info shapes
# --------------------------------------------------------------------------
@pytest.mark.unit
class TestTierInfoShapes:
    def test_calculate_tier_progress_against_qualifying_points(
        self, loyalty_service, account, tiers
    ):
        _earn_lot(account.user_id, 200)  # qualifying = 200; next tier Silver @ 500
        progress = loyalty_service.calculate_tier_progress(account.user_id)
        assert progress["current_tier"] == "Bronze"
        assert progress["current_points"] == 200
        assert progress["next_tier"] == "Silver"
        assert progress["points_to_next_tier"] == 300  # 500 - 200
        # 200/500 * 100 = 40%
        assert progress["progress_percentage"] == pytest.approx(40.0)

    def test_get_tier_upgrade_requirements_points_needed_math(
        self, loyalty_service, account, tiers
    ):
        _earn_lot(account.user_id, 350)  # qualifying = 350
        req = loyalty_service.get_tier_upgrade_requirements(account.user_id)
        assert req["current_tier"] == "Bronze"
        assert req["next_tier"] == "Silver"
        assert req["current_points"] == 350
        assert req["target_points"] == 500
        assert req["points_needed"] == 150  # 500 - 350

    def test_get_tier_upgrade_requirements_at_highest_tier(
        self, loyalty_service, account, tiers, db
    ):
        account.current_tier = "Gold"
        db.session.commit()
        _earn_lot(account.user_id, 2000)
        req = loyalty_service.get_tier_upgrade_requirements(account.user_id)
        assert req["next_tier"] is None
        assert req["points_needed"] == 0
        assert req["target_points"] is None
        assert "message" in req

    def test_get_requalification_info_shape_and_points_needed(
        self, loyalty_service, account, tiers, db
    ):
        account.current_tier = "Silver"
        lock = datetime.now(timezone.utc) + timedelta(days=200)
        account.tier_valid_until = lock
        db.session.commit()
        _earn_lot(account.user_id, 300)  # below Silver min (500)

        info = loyalty_service.get_requalification_info(account.user_id)
        assert info["tier"] == "Silver"
        valid_until = info["valid_until"]
        if valid_until is not None and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        assert valid_until == lock
        assert info["qualifying_points"] == 300
        # Need 500 - 300 = 200 to keep Silver.
        assert info["points_needed_to_keep"] == 200


# --------------------------------------------------------------------------
# can_redeem_reward eligibility matrix
# --------------------------------------------------------------------------
@pytest.mark.unit
class TestCanRedeemReward:
    def _make_reward(self, program_id, **kw):
        base = dict(
            program_id=program_id, name="r", reward_type="discount", points_cost=100,
            discount_type="fixed", discount_value=Decimal("500.00"), is_active=True,
            is_system_reward=False, redemptions_used=0,
        )
        base.update(kw)
        r = LoyaltyReward(**base)
        _db.session.add(r)
        _db.session.commit()
        return r

    def _fund(self, user_id, program_id, points):
        acc = LoyaltyPoints(
            user_id=user_id, program_id=program_id, total_earned=points, current_balance=points
        )
        _db.session.add(acc)
        _db.session.flush()
        _earn_lot(user_id, points)
        return acc

    def test_true_for_affordable_configured_discount(
        self, loyalty_service, sample_user, loyalty_program
    ):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(loyalty_program.id)
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is True

    def test_true_for_affordable_configured_free_product(
        self, loyalty_service, sample_user, loyalty_program, sample_category, db
    ):
        product = Product(
            name="Free Bottle", base_price=Decimal("8000.00"), category_id=sample_category.id,
            size=ProductSizeEnum.SIZE_19L, is_active=True,
        )
        db.session.add(product); db.session.commit()
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(
            loyalty_program.id, reward_type="free_product", points_cost=200,
            discount_type=None, discount_value=None,
            free_product_id=product.id, free_product_quantity=1,
        )
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is True

    def test_false_for_missing_reward(self, loyalty_service, sample_user, loyalty_program):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        assert loyalty_service.can_redeem_reward(sample_user.id, 999999) is False

    def test_false_for_system_reward(self, loyalty_service, sample_user, loyalty_program):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(loyalty_program.id, is_system_reward=True)
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_for_inactive_reward(self, loyalty_service, sample_user, loyalty_program):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(loyalty_program.id, is_active=False)
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_for_unconfigured_discount_zero_value(
        self, loyalty_service, sample_user, loyalty_program
    ):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(loyalty_program.id, discount_value=Decimal("0.00"))
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_for_insufficient_points(self, loyalty_service, sample_user, loyalty_program):
        self._fund(sample_user.id, loyalty_program.id, 50)  # reward costs 100
        reward = self._make_reward(loyalty_program.id, points_cost=100)
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_before_valid_from(self, loyalty_service, sample_user, loyalty_program):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(
            loyalty_program.id,
            valid_from=datetime.now(timezone.utc) + timedelta(days=3),
        )
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_after_valid_until(self, loyalty_service, sample_user, loyalty_program):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(
            loyalty_program.id,
            valid_until=datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_when_max_redemptions_reached(
        self, loyalty_service, sample_user, loyalty_program
    ):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(
            loyalty_program.id, max_redemptions=2, redemptions_used=2,
        )
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_when_user_max_uses_per_user_reached(
        self, loyalty_service, sample_user, loyalty_program, db
    ):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        reward = self._make_reward(loyalty_program.id, max_uses_per_user=1)
        # Seed an existing applied redemption for this user/reward.
        db.session.add(RewardRedemption(
            reward_id=reward.id, user_id=sample_user.id, order_id=None,
            reward_type="discount", points_spent=100, code="RWDUSED01", status="applied",
        ))
        db.session.commit()
        assert loyalty_service.can_redeem_reward(sample_user.id, reward.id) is False

    def test_false_for_removed_types_voucher_and_free_delivery(
        self, loyalty_service, sample_user, loyalty_program
    ):
        self._fund(sample_user.id, loyalty_program.id, 1000)
        voucher = self._make_reward(
            loyalty_program.id, reward_type="voucher", discount_type=None, discount_value=None,
        )
        free_delivery = self._make_reward(
            loyalty_program.id, reward_type="free_delivery", discount_type=None, discount_value=None,
        )
        assert loyalty_service.can_redeem_reward(sample_user.id, voucher.id) is False
        assert loyalty_service.can_redeem_reward(sample_user.id, free_delivery.id) is False


# --------------------------------------------------------------------------
# is_reward_configured structural matrix
# --------------------------------------------------------------------------
@pytest.mark.unit
class TestIsRewardConfigured:
    def _reward(self, program_id, **kw):
        base = dict(program_id=program_id, name="r", points_cost=10, is_active=True)
        base.update(kw)
        return LoyaltyReward(**base)

    def test_discount_requires_type_and_positive_value(self, loyalty_service, loyalty_program):
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="discount",
                         discount_type="fixed", discount_value=Decimal("500"))
        ) is True
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="discount",
                         discount_type="percentage", discount_value=Decimal("10"))
        ) is True
        # Zero value -> not configured.
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="discount",
                         discount_type="fixed", discount_value=Decimal("0"))
        ) is False
        # Missing/invalid discount_type -> not configured.
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="discount",
                         discount_value=Decimal("500"))
        ) is False

    def test_free_product_requires_active_product_and_quantity(
        self, loyalty_service, loyalty_program, sample_category, db
    ):
        active = Product(name="A", base_price=Decimal("1000"), category_id=sample_category.id,
                         size=ProductSizeEnum.SIZE_19L, is_active=True)
        inactive = Product(name="B", base_price=Decimal("1000"), category_id=sample_category.id,
                           size=ProductSizeEnum.SIZE_19L, is_active=False)
        db.session.add_all([active, inactive]); db.session.commit()

        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="free_product",
                         free_product_id=active.id, free_product_quantity=2)
        ) is True
        # No product id.
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="free_product", free_product_id=None)
        ) is False
        # Inactive product.
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="free_product",
                         free_product_id=inactive.id, free_product_quantity=1)
        ) is False
        # Quantity 0.
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="free_product",
                         free_product_id=active.id, free_product_quantity=0)
        ) is False

    def test_removed_types_are_never_configured(self, loyalty_service, loyalty_program):
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="voucher")
        ) is False
        assert loyalty_service.is_reward_configured(
            self._reward(loyalty_program.id, reward_type="free_delivery")
        ) is False
