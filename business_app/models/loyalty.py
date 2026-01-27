from datetime import datetime, timedelta, UTC
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import uuid
from business_app import db
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable
from business_app.utils.constants import LoyaltyTransactionType


class LoyaltyTransaction(db.Model, TimestampMixin):
    __tablename__ = 'loyalty_transactions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    points = Column(Integer, nullable=False)  # Can be negative for redemptions
    transaction_type = Column(Enum(LoyaltyTransactionType, name='loyalty_transaction_type', values_callable=lambda x: [e.value for e in x]), nullable=False)
    description = Column(String(255), nullable=False)
    
    # Related entities
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    subscription_id = Column(Integer, ForeignKey('subscriptions.id'), nullable=True)
    
    # Expiration for earned points
    expires_at = Column(DateTime, nullable=True)
    is_expired = Column(Boolean, default=False)
    
    # Additional metadata
    extra_data = Column(JSON, default={})
    
    user = relationship('User', back_populates='loyalty_transactions')
    order = relationship('Order')
    subscription = relationship('Subscription')
    
    def to_dict(self):
        return {
            'id': self.id,
            'points': self.points,
            'transaction_type': self.transaction_type.value if hasattr(self.transaction_type, 'value') else self.transaction_type,
            'description': self.description,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_expired': self.is_expired,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'order_id': self.order_id,
            'subscription_id': self.subscription_id
        }


class LoyaltyProgram(db.Model, TimestampMixin):
    """Loyalty program configurations"""
    __tablename__ = 'loyalty_programs'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    
    # Program settings
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    
    # Earning rules
    points_per_uzs = Column(Float, default=1.0)  # DEPRECATED: Points earned per UZS spent
    uzs_per_point = Column(Integer, default=250)  # UZS spent to earn 1 point
    signup_bonus = Column(Integer, default=100)  # Welcome bonus points
    referral_bonus = Column(Integer, default=50)  # Points for referring someone
    birthday_bonus = Column(Integer, default=25)  # Birthday bonus points
    
    # Point management
    points_expiry_days = Column(Integer, default=365)  # Points expire after X days
    min_redemption_points = Column(Integer, default=100)  # Minimum points to redeem
    
    # Tier system (DEPRECATED - use LoyaltyTierConfig instead)
    tier_thresholds = Column(JSON, default={})  # Points needed for each tier
    tier_multipliers = Column(JSON, default={})  # Point multipliers per tier
    
    # Program metadata
    terms_and_conditions = Column(Text, nullable=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    
    # Relationship to tiers
    tiers = relationship('LoyaltyTierConfig', back_populates='program', order_by='LoyaltyTierConfig.display_order')
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'is_active': self.is_active,
            'is_default': self.is_default,
            'points_per_uzs': self.points_per_uzs,
            'signup_bonus': self.signup_bonus,
            'referral_bonus': self.referral_bonus,
            'birthday_bonus': self.birthday_bonus,
            'points_expiry_days': self.points_expiry_days,
            'min_redemption_points': self.min_redemption_points,
            'tier_thresholds': self.tier_thresholds,
            'tier_multipliers': self.tier_multipliers,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


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
    __tablename__ = 'loyalty_tier_configs'
    
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey('loyalty_programs.id'), nullable=False, index=True)
    
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
    color = Column(String(20), default='#CD7F32')  # Hex color for UI
    icon = Column(String(50), default='fa-medal')  # Font Awesome icon class
    
    # Status
    is_active = Column(Boolean, default=True)
    
    # Relationship
    program = relationship('LoyaltyProgram', back_populates='tiers')
    
    def to_dict(self):
        """Serialize tier configuration for API responses"""
        # Calculate points range string for display
        if self.max_points is not None:
            points_range = f"{self.min_points:,} - {self.max_points:,}"
        else:
            points_range = f"{self.min_points:,}+"
        
        return {
            'id': self.id,
            'program_id': self.program_id,
            'name': self.name,
            'display_order': self.display_order,
            'min_points': self.min_points,
            'max_points': self.max_points,
            'points_range': points_range,
            'points_multiplier': self.points_multiplier,
            'discount_percentage': self.discount_percentage,
            'benefits': self.benefits or [],
            'color': self.color,
            'icon': self.icon,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    @classmethod
    def get_tier_for_points(cls, points: int, program_id: int = None) -> 'LoyaltyTierConfig':
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


class LoyaltyPoints(db.Model, TimestampMixin):
    """User loyalty points balance"""
    __tablename__ = 'loyalty_points'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True, unique=True)
    program_id = Column(Integer, ForeignKey('loyalty_programs.id'), nullable=False)
    
    # Point balances
    total_earned = Column(Integer, default=0)
    total_redeemed = Column(Integer, default=0)
    total_expired = Column(Integer, default=0)
    current_balance = Column(Integer, default=0)
    
    # Tier information
    current_tier = Column(String(50), default='Bronze')
    points_to_next_tier = Column(Integer, default=0)
    tier_valid_until = Column(DateTime, nullable=True)  # Date until current tier is guaranteed
    
    # Streak Tracking
    current_streak = Column(Integer, default=0)
    last_streak_update = Column(DateTime, nullable=True)
    streak_orders_this_month = Column(Integer, default=0)
    
    # Metadata
    last_activity_date = Column(DateTime, nullable=True)
    last_expiry_check = Column(DateTime, nullable=True)
    
    program = relationship('LoyaltyProgram')
    
    def calculate_current_balance(self):
        """Calculate current balance from transactions"""
        from sqlalchemy import func
        
        # Sum all non-expired transactions
        earned = db.session.query(func.sum(LoyaltyTransaction.points)).filter(
            LoyaltyTransaction.user_id == self.user_id,
            LoyaltyTransaction.points > 0,
            LoyaltyTransaction.is_expired == False
        ).scalar() or 0
        
        redeemed = abs(db.session.query(func.sum(LoyaltyTransaction.points)).filter(
            LoyaltyTransaction.user_id == self.user_id,
            LoyaltyTransaction.points < 0
        ).scalar() or 0)
        
        self.current_balance = earned - redeemed
        return self.current_balance
    
    def calculate_tier(self):
        """Calculate user's current tier based on points"""
        total_points = self.total_earned
        
        # Use program-specific thresholds if available, otherwise use centralized config
        if self.program and self.program.tier_thresholds:
            thresholds = self.program.tier_thresholds
            current_tier = 'Bronze'
            points_to_next = 0
            
            for tier, threshold in sorted(thresholds.items(), key=lambda x: x[1]):
                if total_points >= threshold:
                    current_tier = tier
                else:
                    points_to_next = threshold - total_points
                    break
        else:
            # Use centralized tier config
            # Use LoyaltyTierConfig model directly
            from business_app.models.loyalty import LoyaltyTierConfig
            
            # Find current tier
            tier = LoyaltyTierConfig.get_tier_for_points(total_points, self.program_id)
            current_tier = tier.name if tier else 'Bronze'
            
            # Find next tier
            next_tier = None
            if tier:
                next_tier = LoyaltyTierConfig.query.filter(
                    LoyaltyTierConfig.program_id == self.program_id,
                    LoyaltyTierConfig.is_active == True,
                    LoyaltyTierConfig.display_order > tier.display_order
                ).order_by(LoyaltyTierConfig.display_order.asc()).first()
            else:
                 # If fell back to bronze or none, find lowest tier that is higher than 0?
                 # Actually if no tier found, assuming below lowest. Find lowest.
                 next_tier = LoyaltyTierConfig.query.filter(
                    LoyaltyTierConfig.program_id == self.program_id,
                    LoyaltyTierConfig.is_active == True
                 ).order_by(LoyaltyTierConfig.display_order.asc()).first()

            points_to_next = max(0, next_tier.min_points - total_points) if next_tier else 0
        
        self.current_tier = current_tier
        self.points_to_next_tier = points_to_next
        return current_tier
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'program_id': self.program_id,
            'total_earned': self.total_earned,
            'total_redeemed': self.total_redeemed,
            'total_expired': self.total_expired,
            'current_balance': self.current_balance,
            'current_tier': self.current_tier,
            'points_to_next_tier': self.points_to_next_tier,
            'tier_valid_until': self.tier_valid_until.isoformat() if self.tier_valid_until else None,
            'current_streak': self.current_streak,
            'streak_orders_this_month': self.streak_orders_this_month,
            'last_activity_date': self.last_activity_date.isoformat() if self.last_activity_date else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


@translatable('name', 'description')
class LoyaltyReward(db.Model, TimestampMixin, TranslatableMixin):
    """Available loyalty rewards for redemption"""
    __tablename__ = 'loyalty_rewards'
    
    id = Column(Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey('loyalty_programs.id'), nullable=False)
    
    # Reward details
    name = Column(String(200), nullable=False)      # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)       # Default/fallback description (Uzbek)
    reward_type = Column(String(50), nullable=False)  # discount, free_product, free_delivery, voucher
    
    # Redemption requirements
    points_cost = Column(Integer, nullable=False)
    min_order_value = Column(Numeric(precision=10, scale=2), default=Decimal('0.00'))
    max_uses_per_user = Column(Integer, default=1)
    max_redemptions = Column(Integer, nullable=True)  # Overall limit
    
    # Reward value  
    discount_type = Column(String(20), nullable=True)
    discount_value = Column(Numeric(precision=10, scale=2), nullable=True)
    free_product_id = Column(Integer, ForeignKey('products.id'), nullable=True)
    voucher_code = Column(String(50), nullable=True)
    
    # Availability
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    is_system_reward = Column(Boolean, default=False, nullable=False)  # System rewards (e.g., Free Delivery) cannot be manually redeemed
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    
    # Usage tracking
    redemptions_used = Column(Integer, default=0)
    
    # Applicability
    applicable_products = Column(JSON, default=[])
    applicable_categories = Column(JSON, default=[])
    
    # Metadata
    terms_conditions = Column(Text, nullable=True)
    image_url = Column(String(255), nullable=True)
    sort_order = Column(Integer, default=0)
    
    program = relationship('LoyaltyProgram')
    free_product = relationship('Product')
    
    def is_available_for_user(self, user_id):
        """Check if reward is available for specific user"""
        if not self.is_active:
            return False, "Reward is not active"
        
        # Check date validity
        now = datetime.now(UTC)
        if self.valid_from and now < self.valid_from:
            return False, "Reward not yet available"
        
        if self.valid_until and now > self.valid_until:
            return False, "Reward has expired"
        
        # Check total redemption limit
        if self.max_redemptions and self.redemptions_used >= self.max_redemptions:
            return False, "Reward redemption limit reached"
        
        # Check user-specific usage limit
        if self.max_uses_per_user:
            from business_app.models.order import Order
            user_redemptions = Order.query.join(LoyaltyTransaction).filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.description.contains(f"Redeemed reward: {self.name}")
            ).count()
            
            if user_redemptions >= self.max_uses_per_user:
                return False, "User redemption limit reached"
        
        return True, "Available"
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add reward-specific fields
        result.update({
            'program_id': self.program_id,
            'reward_type': self.reward_type,
            'points_cost': self.points_cost,
            'min_order_value': float(self.min_order_value) if self.min_order_value else None,
            'max_uses_per_user': self.max_uses_per_user,
            'max_redemptions': self.max_redemptions,
            'discount_type': self.discount_type,
            'discount_value': float(self.discount_value) if self.discount_value else None,
            'free_product_id': self.free_product_id,
            'voucher_code': self.voucher_code,
            'is_active': self.is_active,
            'is_featured': self.is_featured,
            'is_system_reward': self.is_system_reward,
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_until': self.valid_until.isoformat() if self.valid_until else None,
            'redemptions_used': self.redemptions_used,
            'applicable_products': self.applicable_products,
            'applicable_categories': self.applicable_categories,
            'terms_conditions': self.terms_conditions,
            'image_url': self.image_url,
            'sort_order': self.sort_order
        })
        
        return result


class ReferralProgram(db.Model, TimestampMixin):
    """Customer referral program tracking"""
    __tablename__ = 'referral_programs'
    
    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    referee_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    
    # Referral details
    referral_code = Column(String(20), nullable=False, unique=True)
    status = Column(String(20), default='pending')  # pending, completed, cancelled
    
    # Rewards
    referrer_bonus_points = Column(Integer, default=0)
    referee_bonus_points = Column(Integer, default=0)
    
    # Tracking
    referred_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))
    completed_at = Column(DateTime, nullable=True)
    first_order_id = Column(Integer, ForeignKey('orders.id'), nullable=True)
    
    # Relationships
    referrer = relationship('User', foreign_keys=[referrer_id])
    referee = relationship('User', foreign_keys=[referee_id])
    first_order = relationship('Order')
    
    def to_dict(self):
        return {
            'id': self.id,
            'referrer_id': self.referrer_id,
            'referee_id': self.referee_id,
            'referral_code': self.referral_code,
            'status': self.status,
            'referrer_bonus_points': self.referrer_bonus_points,
            'referee_bonus_points': self.referee_bonus_points,
            'referred_at': self.referred_at.isoformat() if self.referred_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'first_order_id': self.first_order_id,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
