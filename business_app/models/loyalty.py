from datetime import datetime, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship
from business_app import db
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable
from business_app.utils.constants import LoyaltyTransactionType


class LoyaltyTransaction(db.Model, TimestampMixin):
    __tablename__ = "loyalty_transactions"
    __table_args__ = (Index("idx_loyalty_transactions_user_created", "user_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    points = Column(Integer, nullable=False)  # Can be negative for redemptions
    transaction_type = Column(
        Enum(LoyaltyTransactionType, name="loyalty_transaction_type", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    description = Column(String(255), nullable=False)

    # Related entities
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)

    # Expiration for earned points
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_expired = Column(Boolean, default=False)

    # FIFO lot accounting: the unspent remainder of a positive earn lot.
    # A positive (EARNED/BONUS/positive-ADJUSTMENT) transaction is a "lot" whose
    # ``remaining_points`` is drawn down (oldest-first) by redemptions, clawbacks,
    # and expiry. NULL means "not tracked as a lot" (negative transactions) or a
    # pre-FIFO legacy row — computations COALESCE a NULL remainder to ``points``.
    remaining_points = Column(Integer, nullable=True)

    # Additional metadata
    extra_data = Column(JSON, default={})

    user = relationship("User", back_populates="loyalty_transactions")
    order = relationship("Order")
    subscription = relationship("Subscription")

    def to_dict(self):
        return {
            "id": self.id,
            "points": self.points,
            "transaction_type": (
                self.transaction_type.value if hasattr(self.transaction_type, "value") else self.transaction_type
            ),
            "description": self.description,
            # Granular action (referral/purchase/welcome_bonus/streak_bonus/
            # reward_refund/...) lives in extra_data; expose it so clients can
            # render a stable, localized category instead of the English
            # description or the coarse transaction_type.
            "action_type": (self.extra_data or {}).get("action_type"),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_expired": self.is_expired,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "order_id": self.order_id,
            "subscription_id": self.subscription_id,
        }


@translatable("name", "description", "terms_and_conditions")
class LoyaltyProgram(db.Model, TimestampMixin):
    """Loyalty program configurations"""

    __tablename__ = "loyalty_programs"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Program settings
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)

    # Earning rules
    uzs_per_point = Column(Integer, default=250)  # UZS spent to earn 1 point
    signup_bonus = Column(Integer, default=100)  # Welcome bonus points
    referral_bonus = Column(Integer, default=50)  # Points for referring someone
    birthday_bonus = Column(Integer, default=25)  # Birthday bonus points

    # Point management
    points_expiry_days = Column(Integer, default=365)  # Points expire after X days
    min_redemption_points = Column(Integer, default=100)  # Minimum points to redeem

    # Surprise reward — random delight bonus on a delivered+paid order for
    # individual customers. All knobs are admin-configurable (DB SSOT); see
    # LoyaltyService.process_daily_surprise_rewards (nightly batch).
    surprise_enabled = Column(Boolean, default=True, nullable=False, server_default="true")
    surprise_chance_percent = Column(Integer, default=5, nullable=False, server_default="5")  # 0–100
    surprise_amounts = Column(
        String(100), default="50,100,200", nullable=False, server_default="50,100,200"
    )  # CSV of point values
    surprise_cooldown_days = Column(Integer, default=7, nullable=False, server_default="7")  # per-user cooldown
    surprise_daily_cap = Column(Integer, default=5, nullable=False, server_default="5")  # global awards per day

    # NOTE: deprecated columns points_per_uzs / tier_thresholds / tier_multipliers
    # were dropped (loyalty SSOT, Phase 2). Earning uses uzs_per_point; tiers are
    # owned by LoyaltyTierConfig. See migration a1c2e3f4d5b6.

    # Program metadata
    terms_and_conditions = Column(Text, nullable=True)
    start_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)

    # Relationship to tiers
    tiers = relationship("LoyaltyTierConfig", back_populates="program", order_by="LoyaltyTierConfig.display_order")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "is_default": self.is_default,
            "uzs_per_point": self.uzs_per_point,
            "signup_bonus": self.signup_bonus,
            "referral_bonus": self.referral_bonus,
            "birthday_bonus": self.birthday_bonus,
            "points_expiry_days": self.points_expiry_days,
            "min_redemption_points": self.min_redemption_points,
            "surprise_enabled": self.surprise_enabled,
            "surprise_chance_percent": self.surprise_chance_percent,
            "surprise_amounts": self.surprise_amounts,
            "surprise_cooldown_days": self.surprise_cooldown_days,
            "surprise_daily_cap": self.surprise_daily_cap,
            # tier_thresholds / tier_multipliers deliberately omitted — deprecated
            # JSON superseded by LoyaltyTierConfig (single source of truth).
            "terms_and_conditions": self.terms_and_conditions,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@translatable("name")
class LoyaltyTierConfig(db.Model, TimestampMixin):
    """
    Admin-managed loyalty tier configuration.

    Replaces hardcoded MEMBERSHIP_TIERS in constants.py.
    Each tier belongs to a LoyaltyProgram and defines:
    - Point thresholds for tier qualification
    - Points multiplier (earn more points at higher tiers)
    - Discount percentage
    - Benefits list
    - Visual styling (color, icon)
    """

    __tablename__ = "loyalty_tier_configs"

    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("loyalty_programs.id"), nullable=False, index=True)

    # Tier identification
    name = Column(String(50), nullable=False)  # e.g., "Bronze", "Silver", "Gold", "Platinum"
    display_order = Column(Integer, default=0)  # Order for display (0=lowest tier)

    # Point thresholds
    min_points = Column(Integer, nullable=False, default=0)  # Minimum points to qualify
    max_points = Column(Integer, nullable=True)  # Maximum points (NULL = unlimited/highest tier)

    # Earning multipliers
    points_multiplier = Column(Float, default=1.0)  # e.g., 1.5 = earn 50% more points

    # Tier benefits
    discount_percentage = Column(Float, default=0)  # e.g., 10 = 10% discount
    benefits = Column(JSON, default=[])  # List of benefit descriptions

    # Visual styling
    color = Column(String(20), default="#CD7F32")  # Hex color for UI
    icon = Column(String(50), default="fa-medal")  # Font Awesome icon class

    # Status
    is_active = Column(Boolean, default=True)

    # Relationship
    program = relationship("LoyaltyProgram", back_populates="tiers")

    def to_dict(self):
        """Serialize tier configuration for API responses"""
        # Calculate points range string for display
        if self.max_points is not None:
            points_range = f"{self.min_points:,} - {self.max_points:,}"
        else:
            points_range = f"{self.min_points:,}+"

        return {
            "id": self.id,
            "program_id": self.program_id,
            "name": self.name,
            "display_order": self.display_order,
            "min_points": self.min_points,
            "max_points": self.max_points,
            "points_range": points_range,
            "points_multiplier": self.points_multiplier,
            "discount_percentage": self.discount_percentage,
            "benefits": self.benefits or [],
            "color": self.color,
            "icon": self.icon,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def get_tier_for_points(cls, points: int, program_id: int = None) -> "LoyaltyTierConfig":
        """
        Determine the appropriate tier for a given point balance.

        Args:
            points: User's current point balance
            program_id: Optional program ID (uses default program if not specified)

        Returns:
            LoyaltyTierConfig instance for the matching tier, or None
        """
        query = cls.query.filter_by(is_active=True)

        if program_id:
            query = query.filter_by(program_id=program_id)
        else:
            # Get default program's tiers
            default_program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            if default_program:
                query = query.filter_by(program_id=default_program.id)

        # Order by min_points descending to find highest qualifying tier
        tiers = query.order_by(cls.min_points.desc()).all()

        for tier in tiers:
            if points >= tier.min_points:
                return tier

        # Return lowest tier if no match (shouldn't happen if Bronze starts at 0)
        return query.order_by(cls.min_points.asc()).first()

    @classmethod
    def get_all_tiers(cls, program_id: int = None) -> list:
        """Get all active tiers for a program, ordered by display_order"""
        query = cls.query.filter_by(is_active=True)

        if program_id:
            query = query.filter_by(program_id=program_id)
        else:
            default_program = LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            if default_program:
                query = query.filter_by(program_id=default_program.id)

        return query.order_by(cls.display_order.asc()).all()


@translatable("name")
class LoyaltyStreakRule(db.Model, TimestampMixin):
    """Admin-configurable streak earning rule.

    Awards bonus points when a user completes ``required_orders`` delivered orders
    within a trailing ``window_days`` window (each order optionally ≥
    ``min_order_amount``). Repeatable at most once per ``window_days`` (cooldown).
    """

    __tablename__ = "loyalty_streak_rules"

    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("loyalty_programs.id"), nullable=False, index=True)

    name = Column(String(100), nullable=False)  # user-facing, translatable
    required_orders = Column(Integer, nullable=False)
    window_days = Column(Integer, nullable=False)
    min_order_amount = Column(Numeric(precision=10, scale=2), nullable=True)
    bonus_points = Column(Integer, nullable=False)

    is_active = Column(Boolean, default=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    display_order = Column(Integer, default=0)

    program = relationship("LoyaltyProgram")

    def to_dict(self):
        return {
            "id": self.id,
            "program_id": self.program_id,
            "name": self.name,
            "required_orders": self.required_orders,
            "window_days": self.window_days,
            "min_order_amount": float(self.min_order_amount) if self.min_order_amount is not None else None,
            "bonus_points": self.bonus_points,
            "is_active": self.is_active,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "display_order": self.display_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def is_effective(self, now):
        """True when active and ``now`` is within the optional [starts_at, ends_at].

        Normalizes ``starts_at``/``ends_at`` to UTC-aware datetimes so the
        comparison works whether the DB backend returns tz-aware (PostgreSQL) or
        tz-naive (SQLite in tests) values.
        """
        from business_app.utils.timezone_utils import ensure_utc

        if not self.is_active:
            return False
        if self.starts_at and now < ensure_utc(self.starts_at):
            return False
        if self.ends_at and now > ensure_utc(self.ends_at):
            return False
        return True


loyalty_consec_rule_strikes = db.Table(
    "loyalty_consec_rule_strikes",
    Column(
        "consecutive_strike_rule_id",
        Integer,
        ForeignKey("loyalty_consecutive_strike_rules.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "streak_rule_id",
        Integer,
        ForeignKey("loyalty_streak_rules.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


@translatable("name")
class LoyaltyConsecutiveStrikeRule(db.Model, TimestampMixin):
    """Admin-configurable consecutive-strike bonus rule.

    Composes one or more ``LoyaltyStreakRule`` ("order strike") rows and awards
    ``bonus_points`` AquaCoins when each (``combine_mode='all'``) or any
    (``combine_mode='any'``) attached strike has been achieved
    ``required_consecutive`` times in a row, on each strike's own ``window_days``
    cadence. Repeats every N; a skipped period resets that strike's run to 0.
    Fully stateless / ledger-derived — no per-user counters.
    """

    __tablename__ = "loyalty_consecutive_strike_rules"

    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("loyalty_programs.id"), nullable=False, index=True)

    name = Column(String(100), nullable=False)  # user-facing, translatable
    required_consecutive = Column(Integer, nullable=False)
    combine_mode = Column(String(8), nullable=False, default="all")  # 'all' | 'any'
    bonus_points = Column(Integer, nullable=False)

    is_active = Column(Boolean, default=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    display_order = Column(Integer, default=0)

    program = relationship("LoyaltyProgram")
    strikes = relationship("LoyaltyStreakRule", secondary=loyalty_consec_rule_strikes)

    def to_dict(self):
        return {
            "id": self.id,
            "program_id": self.program_id,
            "name": self.name,
            "required_consecutive": self.required_consecutive,
            "combine_mode": self.combine_mode,
            "bonus_points": self.bonus_points,
            "is_active": self.is_active,
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "display_order": self.display_order,
            "strikes": [
                {
                    "id": s.id,
                    "name": s.name,
                    "required_orders": s.required_orders,
                    "window_days": s.window_days,
                }
                for s in self.strikes
            ],
            "strike_rule_ids": [s.id for s in self.strikes],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def is_effective(self, now):
        """True when active and ``now`` is within the optional [starts_at, ends_at]."""
        from business_app.utils.timezone_utils import ensure_utc

        if not self.is_active:
            return False
        if self.starts_at and now < ensure_utc(self.starts_at):
            return False
        if self.ends_at and now > ensure_utc(self.ends_at):
            return False
        return True


class LoyaltyPoints(db.Model, TimestampMixin):
    """User loyalty points balance"""

    __tablename__ = "loyalty_points"
    __table_args__ = (
        Index("idx_loyalty_points_program_tier_activity", "program_id", "current_tier", "last_activity_date"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, unique=True)
    program_id = Column(Integer, ForeignKey("loyalty_programs.id"), nullable=False)

    # Point balances
    total_earned = Column(Integer, default=0)
    total_redeemed = Column(Integer, default=0)
    total_expired = Column(Integer, default=0)
    current_balance = Column(Integer, default=0)

    # Tier information
    current_tier = Column(String(50), default="Bronze")
    points_to_next_tier = Column(Integer, default=0)
    tier_valid_until = Column(DateTime(timezone=True), nullable=True)  # Date until current tier is guaranteed

    # Metadata
    last_activity_date = Column(DateTime(timezone=True), nullable=True)
    last_expiry_check = Column(DateTime(timezone=True), nullable=True)

    program = relationship("LoyaltyProgram")

    def calculate_current_balance(self):
        """Derive the cached balance from the FIFO ledger (single source of truth).

        Balance = the sum of unspent remainders of non-expired positive lots.
        Redemptions/clawbacks/expiry have already drawn down those remainders, so
        negatives must NOT be subtracted again (that was the historical
        double-count bug). NULL remainders (legacy rows) fall back to ``points``.
        """
        from sqlalchemy import func, or_

        now = datetime.now(UTC)
        balance = (
            db.session.query(func.sum(func.coalesce(LoyaltyTransaction.remaining_points, LoyaltyTransaction.points)))
            .filter(
                LoyaltyTransaction.user_id == self.user_id,
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.is_expired == False,
                # Exclude lots already past their expiry but not yet swept, so this
                # matches LoyaltyService.get_available_points exactly (one SSOT).
                or_(LoyaltyTransaction.expires_at.is_(None), LoyaltyTransaction.expires_at > now),
            )
            .scalar()
            or 0
        )

        self.current_balance = int(balance)
        return self.current_balance

    # NOTE: the former LoyaltyPoints.calculate_tier() was removed (loyalty SSOT,
    # Unit D). It was an orphan that read the deprecated program.tier_thresholds
    # JSON first and based tier on lifetime total_earned — both contradicting the
    # single tier basis. Tier is owned by LoyaltyService._check_tier_upgrade
    # (rolling 365-day qualifying points) + LoyaltyTierConfig.

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "program_id": self.program_id,
            "total_earned": self.total_earned,
            "total_redeemed": self.total_redeemed,
            "total_expired": self.total_expired,
            "current_balance": self.current_balance,
            "current_tier": self.current_tier,
            "points_to_next_tier": self.points_to_next_tier,
            "tier_valid_until": self.tier_valid_until.isoformat() if self.tier_valid_until else None,
            "last_activity_date": self.last_activity_date.isoformat() if self.last_activity_date else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@translatable("name", "description")
class LoyaltyReward(db.Model, TimestampMixin, TranslatableMixin):
    """Available loyalty rewards for redemption"""

    __tablename__ = "loyalty_rewards"

    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("loyalty_programs.id"), nullable=False)

    # Reward details
    name = Column(String(200), nullable=False)  # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)  # Default/fallback description (Uzbek)
    reward_type = Column(String(50), nullable=False)  # discount, free_product

    # Redemption requirements
    points_cost = Column(Integer, nullable=False)
    min_order_value = Column(Numeric(precision=10, scale=2), default=Decimal("0.00"))
    max_uses_per_user = Column(Integer, default=1)
    max_redemptions = Column(Integer, nullable=True)  # Overall limit

    # Reward value
    discount_type = Column(String(20), nullable=True)
    discount_value = Column(Numeric(precision=10, scale=2), nullable=True)
    free_product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    free_product_quantity = Column(Integer, nullable=True, default=1, server_default="1")

    # Availability
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    is_system_reward = Column(
        Boolean, default=False, nullable=False
    )  # System rewards (e.g., Free Delivery) cannot be manually redeemed
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)

    # Usage tracking
    redemptions_used = Column(Integer, default=0)

    # Applicability
    applicable_products = Column(JSON, default=[])
    applicable_categories = Column(JSON, default=[])

    # Metadata
    terms_conditions = Column(Text, nullable=True)
    image_url = Column(String(255), nullable=True)
    sort_order = Column(Integer, default=0)

    program = relationship("LoyaltyProgram")
    free_product = relationship("Product")

    # NOTE: is_available_for_user was removed (loyalty SSOT, Phase 2). It was an
    # orphan with a semantically-wrong Order<->LoyaltyTransaction join and a
    # fragile description-string redemption count. The wired availability check
    # is LoyaltyService.can_redeem_reward; per-user redemption counting becomes
    # FK-based once the RewardRedemption table lands (Phase 3).

    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)

        # Add reward-specific fields
        result.update(
            {
                "program_id": self.program_id,
                "reward_type": self.reward_type,
                "points_cost": self.points_cost,
                "min_order_value": float(self.min_order_value) if self.min_order_value else None,
                "max_uses_per_user": self.max_uses_per_user,
                "max_redemptions": self.max_redemptions,
                "discount_type": self.discount_type,
                "discount_value": float(self.discount_value) if self.discount_value else None,
                "free_product_id": self.free_product_id,
                "free_product_quantity": self.free_product_quantity,
                "is_active": self.is_active,
                "is_featured": self.is_featured,
                "is_system_reward": self.is_system_reward,
                "valid_from": self.valid_from.isoformat() if self.valid_from else None,
                "valid_until": self.valid_until.isoformat() if self.valid_until else None,
                "redemptions_used": self.redemptions_used,
                "applicable_products": self.applicable_products,
                "applicable_categories": self.applicable_categories,
                "terms_conditions": self.terms_conditions,
                "image_url": self.image_url,
                "sort_order": self.sort_order,
            }
        )

        return result


class ReferralProgram(db.Model, TimestampMixin):
    """Customer referral program tracking"""

    __tablename__ = "referral_programs"

    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    referee_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Referral details. NOT unique: this records WHICH referrer code was used, and
    # one referrer code is reused across all the people they refer (a unique
    # constraint here would cap each referrer at a single referral). Per-referee
    # uniqueness is enforced via User.referred_by_user_id at referral creation.
    referral_code = Column(String(20), nullable=False, index=True)
    status = Column(String(20), default="pending")  # pending, completed, cancelled

    # Rewards
    referrer_bonus_points = Column(Integer, default=0)
    referee_bonus_points = Column(Integer, default=0)

    # Tracking
    referred_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    first_order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)

    # Relationships
    referrer = relationship("User", foreign_keys=[referrer_id])
    referee = relationship("User", foreign_keys=[referee_id])
    first_order = relationship("Order")

    def to_dict(self):
        return {
            "id": self.id,
            "referrer_id": self.referrer_id,
            "referee_id": self.referee_id,
            "referral_code": self.referral_code,
            "status": self.status,
            "referrer_bonus_points": self.referrer_bonus_points,
            "referee_bonus_points": self.referee_bonus_points,
            "referred_at": self.referred_at.isoformat() if self.referred_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "first_order_id": self.first_order_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class RewardRedemption(db.Model, TimestampMixin):
    """A single applied reward redemption (SSOT for redemption records, codes, limits)."""

    __tablename__ = "reward_redemptions"
    __table_args__ = (Index("idx_reward_redemptions_reward_user", "reward_id", "user_id"),)

    id = Column(Integer, primary_key=True)
    reward_id = Column(Integer, ForeignKey("loyalty_rewards.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)

    reward_type = Column(String(50), nullable=False)  # 'discount' | 'free_product'
    points_spent = Column(Integer, nullable=False, default=0)
    discount_amount = Column(Numeric(precision=10, scale=2), nullable=True)
    free_product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    code = Column(String(20), nullable=False, unique=True, index=True)
    status = Column(String(20), nullable=False, default="applied")  # 'applied' | 'cancelled'

    reward = relationship("LoyaltyReward")
    order = relationship("Order")

    def to_dict(self):
        return {
            "id": self.id,
            "reward_id": self.reward_id,
            "user_id": self.user_id,
            "order_id": self.order_id,
            "reward_type": self.reward_type,
            "points_spent": self.points_spent,
            "discount_amount": float(self.discount_amount) if self.discount_amount is not None else None,
            "free_product_id": self.free_product_id,
            "code": self.code,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
