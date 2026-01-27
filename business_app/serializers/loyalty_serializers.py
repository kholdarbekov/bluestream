"""
Loyalty Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for loyalty-related data serialization
"""
from datetime import datetime, date
from typing import Dict, Any, Optional, List
from enum import Enum
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel

# Import centralized tier configuration
from business_app.models.loyalty import LoyaltyTierConfig


class LoyaltyTier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"
    DIAMOND = "diamond"


class RewardType(str, Enum):
    DISCOUNT = "discount"
    FREE_PRODUCT = "free_product"
    FREE_DELIVERY = "free_delivery"
    CASHBACK = "cashback"
    GIFT_CARD = "gift_card"
    SPECIAL_ACCESS = "special_access"


class RewardStatus(str, Enum):
    AVAILABLE = "available"
    CLAIMED = "claimed"
    EXPIRED = "expired"
    USED = "used"


class ReferralStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ChallengeStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    PAUSED = "paused"


class LoyaltyTierInfoSchema(BaseModel):
    """Loyalty tier information schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    tier: LoyaltyTier
    name: str
    description: Optional[str] = None
    min_points: int = Field(ge=0)
    benefits: List[str] = Field(default_factory=list)
    discount_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    color: Optional[str] = None
    icon_url: Optional[str] = None


class PointsTransactionSchema(BaseModel):
    """Points transaction schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    transaction_type: str  # earned, redeemed, expired, adjusted
    points: int
    description: str
    reference_id: Optional[str] = None
    reference_type: Optional[str] = None  # order, referral, challenge, etc.
    created_at: datetime
    expires_at: Optional[datetime] = None


class LoyaltyRewardSchema(BaseModel):
    """Loyalty reward schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: str
    reward_type: RewardType
    points_required: int = Field(ge=0)
    discount_percentage: Optional[float] = Field(None, ge=0.0, le=100.0)
    discount_amount: Optional[Decimal] = None
    free_product_id: Optional[int] = None
    gift_card_amount: Optional[Decimal] = None
    is_active: bool = Field(default=True)
    is_featured: bool = Field(default=False)
    tier_requirement: Optional[LoyaltyTier] = None
    usage_limit_per_user: Optional[int] = Field(None, ge=1)
    total_usage_limit: Optional[int] = Field(None, ge=1)
    current_usage_count: int = Field(default=0, ge=0)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    terms_conditions: Optional[str] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    
    @field_validator('discount_amount', 'gift_card_amount')
    @classmethod
    def validate_amounts(cls, v):
        if v is not None:
            return float(v)
        return v


class UserRewardSchema(BaseModel):
    """User reward claim schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    reward_id: int
    user_id: int
    status: RewardStatus
    claimed_at: datetime
    used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    order_id: Optional[int] = None  # If reward was used in an order
    reward_code: Optional[str] = None
    reward: Optional[LoyaltyRewardSchema] = None


class ReferralSchema(BaseModel):
    """Referral schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    referrer_id: int
    referred_user_id: Optional[int] = None
    referral_code: str
    referred_email: Optional[str] = None
    referred_phone: Optional[str] = None
    status: ReferralStatus
    points_earned: int = Field(default=0, ge=0)
    bonus_points: int = Field(default=0, ge=0)
    created_at: datetime
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    
    # Referred user info (if completed)
    referred_user_name: Optional[str] = None


class LoyaltyProgramSchema(BaseModel):
    """Loyalty program configuration schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: str
    is_active: bool = Field(default=True)
    points_per_currency: float = Field(default=1.0, gt=0)  # Points earned per currency unit
    points_expiry_days: Optional[int] = Field(None, ge=1)
    referral_points: int = Field(default=100, ge=0)
    signup_bonus_points: int = Field(default=50, ge=0)
    birthday_bonus_points: int = Field(default=100, ge=0)
    review_points: int = Field(default=10, ge=0)
    social_share_points: int = Field(default=5, ge=0)
    min_order_for_points: Decimal = Field(default=0, ge=0)
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    @field_validator('min_order_for_points')
    @classmethod
    def validate_min_order(cls, v):
        return float(v)


class ChallengeSchema(BaseModel):
    """Loyalty challenge schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: str
    challenge_type: str  # order_count, amount_spent, referrals, reviews, etc.
    target_value: float  # Target to achieve
    reward_points: int = Field(ge=0)
    bonus_reward_id: Optional[int] = None
    is_recurring: bool = Field(default=False)
    frequency: Optional[str] = None  # daily, weekly, monthly
    start_date: datetime
    end_date: datetime
    status: ChallengeStatus
    is_featured: bool = Field(default=False)
    tier_requirement: Optional[LoyaltyTier] = None
    max_participants: Optional[int] = Field(None, ge=1)
    current_participants: int = Field(default=0, ge=0)
    image_url: Optional[str] = None
    terms_conditions: Optional[str] = None
    created_at: datetime


class UserChallengeSchema(BaseModel):
    """User challenge participation schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    challenge_id: int
    user_id: int
    current_progress: float = Field(default=0.0, ge=0)
    is_completed: bool = Field(default=False)
    completed_at: Optional[datetime] = None
    points_earned: int = Field(default=0, ge=0)
    joined_at: datetime
    challenge: Optional[ChallengeSchema] = None
    progress_percentage: float = Field(default=0.0, ge=0.0, le=100.0)


class LoyaltyStatisticsSchema(BaseModel):
    """User loyalty statistics schema"""
    total_points_earned: int = Field(default=0, ge=0)
    total_points_redeemed: int = Field(default=0, ge=0)
    total_points_expired: int = Field(default=0, ge=0)
    lifetime_value: Decimal = Field(default=0, ge=0)
    total_orders: int = Field(default=0, ge=0)
    total_referrals: int = Field(default=0, ge=0)
    successful_referrals: int = Field(default=0, ge=0)
    active_challenges: int = Field(default=0, ge=0)
    completed_challenges: int = Field(default=0, ge=0)
    rewards_claimed: int = Field(default=0, ge=0)
    rewards_used: int = Field(default=0, ge=0)
    member_since: Optional[datetime] = None
    last_activity: Optional[datetime] = None
    days_since_last_order: Optional[int] = None
    average_order_value: Decimal = Field(default=0, ge=0)
    
    @field_validator('lifetime_value', 'average_order_value')
    @classmethod
    def validate_amounts(cls, v):
        return float(v)


class UserLoyaltySchema(BaseModel):
    """Main user loyalty schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    user_id: int
    current_points: int = Field(default=0, ge=0)
    lifetime_points: int = Field(default=0, ge=0)
    tier: LoyaltyTier = Field(default=LoyaltyTier.BRONZE)
    tier_info: Optional[LoyaltyTierInfoSchema] = None
    points_to_next_tier: int = Field(default=0, ge=0)
    tier_progress_percentage: float = Field(default=0.0, ge=0.0, le=100.0)
    member_since: datetime
    last_activity: Optional[datetime] = None
    birthday: Optional[date] = None
    anniversary: Optional[date] = None
    
    # Expiring points alert
    points_expiring_soon: int = Field(default=0, ge=0)
    points_expiring_date: Optional[datetime] = None
    
    # Statistics
    statistics: Optional[LoyaltyStatisticsSchema] = None
    
    # Recent activity
    recent_transactions: List[PointsTransactionSchema] = Field(default_factory=list)
    available_rewards: List[LoyaltyRewardSchema] = Field(default_factory=list)
    active_challenges: List[UserChallengeSchema] = Field(default_factory=list)
    recent_referrals: List[ReferralSchema] = Field(default_factory=list)


class GiftPointsSchema(BaseModel):
    """Gift points schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    sender_id: int
    recipient_id: int
    points: int = Field(gt=0)
    message: Optional[str] = Field(None, max_length=500)
    is_anonymous: bool = Field(default=False)
    status: str = Field(default="pending")  # pending, sent, received
    sent_at: Optional[datetime] = None
    received_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    gift_code: Optional[str] = None
    sender_name: Optional[str] = None
    recipient_name: Optional[str] = None


class CreateReferralRequest(BaseModel):
    """Create referral request schema"""
    referred_email: Optional[str] = None
    referred_phone: Optional[str] = None
    message: Optional[str] = Field(None, max_length=500)
    
    @field_validator('referred_email', 'referred_phone')
    @classmethod
    def validate_contact_info(cls, v, info):
        # At least one contact method must be provided
        if info.field_name == 'referred_phone' and not v and not info.data.get('referred_email'):
            raise ValueError('Either email or phone must be provided')
        return v


class RedeemRewardRequest(BaseModel):
    """Redeem reward request schema"""
    reward_id: int
    order_id: Optional[int] = None  # If redeeming for a specific order


class GiftPointsRequest(BaseModel):
    """Gift points request schema"""
    recipient_id: int
    points: int = Field(..., gt=0, le=10000)  # Max 10000 points per gift
    message: Optional[str] = Field(None, max_length=500)
    is_anonymous: bool = Field(default=False)


class JoinChallengeRequest(BaseModel):
    """Join challenge request schema"""
    challenge_id: int


class LoyaltyResponseSchema(BaseModel):
    """Standard loyalty response schema"""
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None


# Export all schemas for easy importing
__all__ = [
    'UserLoyaltySchema',
    'LoyaltyRewardSchema',
    'UserRewardSchema',
    'ReferralSchema',
    'ChallengeSchema',
    'UserChallengeSchema',
    'LoyaltyProgramSchema',
    'GiftPointsSchema',
    'CreateReferralRequest',
    'RedeemRewardRequest',
    'GiftPointsRequest',
    'JoinChallengeRequest',
    'LoyaltyResponseSchema',
    'LoyaltyTier',
    'RewardType',
    'RewardStatus',
    'ReferralStatus',
    'ChallengeStatus'
]


def serialize_user_loyalty(user_loyalty, include_detailed_stats: bool = False) -> Dict[str, Any]:
    """
    Serialize user loyalty data to dictionary using Pydantic
    
    Args:
        user_loyalty: UserLoyalty model instance
        include_detailed_stats: Whether to include detailed statistics
        
    Returns:
        Serialized user loyalty data
    """
    try:
        data = {
            'id': user_loyalty.id,
            'user_id': user_loyalty.user_id,
            'current_points': user_loyalty.current_points or 0,
            'lifetime_points': user_loyalty.lifetime_points or 0,
            'tier': user_loyalty.tier.value if user_loyalty.tier else LoyaltyTier.BRONZE.value,
            'member_since': user_loyalty.member_since.isoformat() if user_loyalty.member_since else None,
            'last_activity': user_loyalty.last_activity.isoformat() if user_loyalty.last_activity else None,
            'birthday': user_loyalty.birthday.isoformat() if user_loyalty.birthday else None,
            'anniversary': user_loyalty.anniversary.isoformat() if user_loyalty.anniversary else None
        }
        
        # Calculate tier information
        tier_info = get_tier_info(user_loyalty.tier.value if user_loyalty.tier else 'bronze')
        if tier_info:
            data['tier_info'] = tier_info
        
        # Calculate points to next tier and progress
        next_tier_info = get_next_tier_info(user_loyalty.tier.value if user_loyalty.tier else 'bronze')
        if next_tier_info:
            points_needed = max(0, next_tier_info['min_points'] - (user_loyalty.current_points or 0))
            data['points_to_next_tier'] = points_needed
            
            if next_tier_info['min_points'] > 0:
                progress = min(100, ((user_loyalty.current_points or 0) / next_tier_info['min_points']) * 100)
                data['tier_progress_percentage'] = round(progress, 1)
            else:
                data['tier_progress_percentage'] = 100.0
        else:
            data['points_to_next_tier'] = 0
            data['tier_progress_percentage'] = 100.0
        
        # Add expiring points information
        expiring_info = get_expiring_points(user_loyalty)
        data['points_expiring_soon'] = expiring_info.get('points', 0)
        data['points_expiring_date'] = expiring_info.get('date')
        
        # Add statistics if requested
        if include_detailed_stats:
            data['statistics'] = get_loyalty_statistics(user_loyalty)
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        return {
            'id': user_loyalty.id if hasattr(user_loyalty, 'id') else 0,
            'user_id': user_loyalty.user_id if hasattr(user_loyalty, 'user_id') else 0,
            'current_points': getattr(user_loyalty, 'current_points', 0),
            'lifetime_points': getattr(user_loyalty, 'lifetime_points', 0),
            'tier': getattr(user_loyalty, 'tier', 'bronze'),
            'member_since': getattr(user_loyalty, 'member_since', None)
        }


def serialize_loyalty_reward(reward, user=None) -> Dict[str, Any]:
    """
    Serialize loyalty reward to dictionary
    
    Args:
        reward: LoyaltyReward model instance
        user: Current user for personalization
        
    Returns:
        Serialized reward data
    """
    try:
        # Get points_cost from the model (the actual field name)
        points_cost = getattr(reward, 'points_cost', None) or getattr(reward, 'points_required', 0) or 0
        
        data = {
            'id': reward.id,
            'name': reward.name,
            'description': reward.description,
            'reward_type': reward.reward_type if isinstance(reward.reward_type, str) else (reward.reward_type.value if reward.reward_type else None),
            'points_cost': points_cost,  # Primary field used by model
            'points_required': points_cost,  # Alias for backwards compatibility
            'is_active': reward.is_active,
            'is_featured': getattr(reward, 'is_featured', False),
            'is_system_reward': getattr(reward, 'is_system_reward', False),
            'tier_requirement': reward.tier_requirement.value if hasattr(reward, 'tier_requirement') and reward.tier_requirement else None,
            'usage_limit_per_user': getattr(reward, 'usage_limit_per_user', None) or getattr(reward, 'max_uses_per_user', None),
            'total_usage_limit': getattr(reward, 'total_usage_limit', None) or getattr(reward, 'max_redemptions', None),
            'current_usage_count': getattr(reward, 'current_usage_count', 0) or getattr(reward, 'redemptions_used', 0) or 0,
            'valid_from': reward.valid_from.isoformat() if reward.valid_from else None,
            'valid_until': reward.valid_until.isoformat() if reward.valid_until else None,
            'terms_conditions': reward.terms_conditions,
            'image_url': reward.image_url,
            'category': getattr(reward, 'category', reward.reward_type)
        }
        
        # Add type-specific information
        if reward.reward_type:
            if reward.reward_type.value == 'discount':
                data['discount_percentage'] = reward.discount_percentage
                data['discount_amount'] = float(reward.discount_amount) if reward.discount_amount else None
            elif reward.reward_type.value == 'free_product':
                data['free_product_id'] = reward.free_product_id
                # Could add product details here
            elif reward.reward_type.value == 'gift_card':
                data['gift_card_amount'] = float(reward.gift_card_amount) if reward.gift_card_amount else None
        
        # Add user-specific information
        if user:
            user_loyalty = getattr(user, 'loyalty', None)
            if user_loyalty:
                current_points = getattr(user_loyalty, 'current_points', 0)
                user_tier = getattr(user_loyalty, 'tier', 'bronze')
                
                data['can_redeem'] = (
                    reward.is_active and
                    current_points >= (reward.points_required or 0) and
                    (not reward.tier_requirement or user_tier == reward.tier_requirement.value) and
                    (not reward.total_usage_limit or reward.current_usage_count < reward.total_usage_limit)
                )
                
                data['user_usage_count'] = get_user_reward_usage_count(user.id, reward.id)
                data['remaining_user_uses'] = None
                if reward.usage_limit_per_user:
                    data['remaining_user_uses'] = max(0, reward.usage_limit_per_user - data['user_usage_count'])
        
        # Add availability information
        remaining_uses = None
        if reward.total_usage_limit:
            remaining_uses = max(0, reward.total_usage_limit - (reward.current_usage_count or 0))
        data['remaining_total_uses'] = remaining_uses
        
        return data
        
    except Exception:
        # Fallback to basic serialization
        points_cost = getattr(reward, 'points_cost', None) or getattr(reward, 'points_required', 0) or 0
        return {
            'id': reward.id,
            'name': reward.name,
            'description': reward.description,
            'points_cost': points_cost,
            'points_required': points_cost,
            'is_active': getattr(reward, 'is_active', True),
            'is_system_reward': getattr(reward, 'is_system_reward', False),
            'reward_type': getattr(reward, 'reward_type', 'discount')
        }


def serialize_referral(referral) -> Dict[str, Any]:
    """Serialize referral data"""
    try:
        data = {
            'id': referral.id,
            'referral_code': referral.referral_code,
            'referred_email': referral.referred_email,
            'referred_phone': referral.referred_phone,
            'status': referral.status.value if referral.status else None,
            'points_earned': referral.points_earned or 0,
            'bonus_points': referral.bonus_points or 0,
            'created_at': referral.created_at.isoformat() if referral.created_at else None,
            'completed_at': referral.completed_at.isoformat() if referral.completed_at else None,
            'expires_at': referral.expires_at.isoformat() if referral.expires_at else None
        }
        
        # Add referred user info if available
        if referral.referred_user_id:
            data['referred_user_id'] = referral.referred_user_id
            data['referred_user_name'] = referral.referred_user_name
        
        return data
        
    except Exception:
        return {
            'id': referral.id,
            'referral_code': referral.referral_code,
            'status': getattr(referral, 'status', 'pending'),
            'points_earned': getattr(referral, 'points_earned', 0)
        }


def serialize_challenge(challenge, user=None) -> Dict[str, Any]:
    """Serialize challenge data"""
    try:
        data = {
            'id': challenge.id,
            'name': challenge.name,
            'description': challenge.description,
            'challenge_type': challenge.challenge_type,
            'target_value': challenge.target_value,
            'reward_points': challenge.reward_points or 0,
            'bonus_reward_id': challenge.bonus_reward_id,
            'is_recurring': challenge.is_recurring,
            'frequency': challenge.frequency,
            'start_date': challenge.start_date.isoformat() if challenge.start_date else None,
            'end_date': challenge.end_date.isoformat() if challenge.end_date else None,
            'status': challenge.status.value if challenge.status else None,
            'is_featured': challenge.is_featured,
            'tier_requirement': challenge.tier_requirement.value if challenge.tier_requirement else None,
            'max_participants': challenge.max_participants,
            'current_participants': challenge.current_participants or 0,
            'image_url': challenge.image_url,
            'terms_conditions': challenge.terms_conditions,
            'created_at': challenge.created_at.isoformat() if challenge.created_at else None
        }
        
        # Add user-specific information
        if user:
            user_challenge = get_user_challenge_participation(user.id, challenge.id)
            if user_challenge:
                data['user_participation'] = {
                    'is_participating': True,
                    'current_progress': user_challenge.get('current_progress', 0),
                    'progress_percentage': user_challenge.get('progress_percentage', 0),
                    'is_completed': user_challenge.get('is_completed', False),
                    'completed_at': user_challenge.get('completed_at'),
                    'points_earned': user_challenge.get('points_earned', 0)
                }
            else:
                data['user_participation'] = {
                    'is_participating': False,
                    'can_join': can_user_join_challenge(user, challenge)
                }
        
        # Add availability information
        spots_remaining = None
        if challenge.max_participants:
            spots_remaining = max(0, challenge.max_participants - (challenge.current_participants or 0))
        data['spots_remaining'] = spots_remaining
        
        return data
        
    except Exception:
        return {
            'id': challenge.id,
            'name': challenge.name,
            'description': challenge.description,
            'reward_points': getattr(challenge, 'reward_points', 0),
            'status': getattr(challenge, 'status', 'active')
        }


# Helper functions
def get_tier_info(tier_name: str) -> Optional[Dict[str, Any]]:
    """Get tier information from database config"""
    try:
        tier_config = LoyaltyTierConfig.query.filter_by(name=tier_name, is_active=True).first()
        if tier_config:
            return {
                'tier': tier_name.lower(),
                'name': tier_config.name,
                'description': f"Earn {tier_config.points_multiplier}x points per 250 UZS",
                'min_points': tier_config.min_points,
                'benefits': tier_config.benefits,
                'discount_percentage': tier_config.discount_percentage,
                'color': tier_config.color,
                'icon_url': f"/static/images/tiers/{tier_name.lower()}.png"
            }
    except Exception:
        pass
    return None


def get_next_tier_info(current_tier: str) -> Optional[Dict[str, Any]]:
    """Get next tier information from database config"""
    try:
        current = LoyaltyTierConfig.query.filter_by(name=current_tier, is_active=True).first()
        if current:
            next_tier = LoyaltyTierConfig.query.filter(
                LoyaltyTierConfig.program_id == current.program_id,
                LoyaltyTierConfig.is_active == True,
                LoyaltyTierConfig.display_order > current.display_order
            ).order_by(LoyaltyTierConfig.display_order.asc()).first()
            
            if next_tier:
                return get_tier_info(next_tier.name)
        else:
             # Try to find lowest tier if current not found? Or just return None
             pass
    except Exception:
        pass
    
    return None


def get_expiring_points(user_loyalty) -> Dict[str, Any]:
    """Get information about expiring points"""
    # This would typically query the database for points with expiry dates
    # For now, return placeholder data
    return {
        'points': 0,
        'date': None
    }


def get_loyalty_statistics(user_loyalty) -> Dict[str, Any]:
    """Get detailed loyalty statistics for user"""
    # This would typically aggregate data from various tables
    # For now, return placeholder statistics
    return {
        'total_points_earned': getattr(user_loyalty, 'lifetime_points', 0),
        'total_points_redeemed': 0,
        'total_points_expired': 0,
        'lifetime_value': 0.0,
        'total_orders': 0,
        'total_referrals': 0,
        'successful_referrals': 0,
        'active_challenges': 0,
        'completed_challenges': 0,
        'rewards_claimed': 0,
        'rewards_used': 0,
        'member_since': user_loyalty.member_since.isoformat() if user_loyalty.member_since else None,
        'last_activity': user_loyalty.last_activity.isoformat() if user_loyalty.last_activity else None,
        'days_since_last_order': None,
        'average_order_value': 0.0
    }


def get_user_reward_usage_count(user_id: int, reward_id: int) -> int:
    """Get count of how many times user has used this reward"""
    # This would query the database for user reward usage
    return 0


def get_user_challenge_participation(user_id: int, challenge_id: int) -> Optional[Dict[str, Any]]:
    """Get user's participation in a challenge"""
    # This would query the database for user challenge participation
    return None


def can_user_join_challenge(user, challenge) -> bool:
    """Check if user can join a challenge"""
    # Check various conditions like tier requirements, max participants, etc.
    if challenge.tier_requirement:
        user_tier = getattr(user.loyalty, 'tier', 'bronze') if hasattr(user, 'loyalty') else 'bronze'
        tier_hierarchy = ['bronze', 'silver', 'gold', 'platinum', 'diamond']
        
        required_tier_index = tier_hierarchy.index(challenge.tier_requirement.value.lower())
        user_tier_index = tier_hierarchy.index(user_tier.lower())
        
        if user_tier_index < required_tier_index:
            return False
    
    if challenge.max_participants:
        if (challenge.current_participants or 0) >= challenge.max_participants:
            return False
    
    return True


def serialize_loyalty_transaction(transaction) -> Dict[str, Any]:
    """Serialize loyalty transaction data"""
    try:
        # Safely get transaction_type as string
        transaction_type = None
        if hasattr(transaction, 'transaction_type') and transaction.transaction_type is not None:
            if hasattr(transaction.transaction_type, 'value'):
                transaction_type = transaction.transaction_type.value
            else:
                transaction_type = str(transaction.transaction_type)
        
        return {
            'id': transaction.id,
            'user_id': transaction.user_id,
            'points': transaction.points,
            'transaction_type': transaction_type,
            'description': transaction.description,
            'order_id': getattr(transaction, 'order_id', None),
            'subscription_id': getattr(transaction, 'subscription_id', None),
            'is_expired': getattr(transaction, 'is_expired', False),
            'created_at': transaction.created_at.isoformat() if transaction.created_at else None,
            'expires_at': transaction.expires_at.isoformat() if hasattr(transaction, 'expires_at') and transaction.expires_at else None
        }
    except Exception as e:
        # Fallback to basic serialization - ensure enum is converted
        transaction_type = 'earned'
        if hasattr(transaction, 'transaction_type'):
            tt = transaction.transaction_type
            if hasattr(tt, 'value'):
                transaction_type = tt.value
            elif tt is not None:
                transaction_type = str(tt)
        
        return {
            'id': getattr(transaction, 'id', 0),
            'points': getattr(transaction, 'points', 0),
            'transaction_type': transaction_type,
            'description': getattr(transaction, 'description', ''),
            'created_at': transaction.created_at.isoformat() if hasattr(transaction, 'created_at') and transaction.created_at else None
        }


def serialize_loyalty_program(program) -> Dict[str, Any]:
    """Serialize loyalty program data"""
    try:
        return {
            'id': program.id,
            'name': program.name,
            'description': program.description,
            'is_active': program.is_active,
            'points_per_currency': getattr(program, 'points_per_currency', 1.0),
            'points_expiry_days': getattr(program, 'points_expiry_days', None),
            'referral_points': getattr(program, 'referral_points', 100),
            'signup_bonus_points': getattr(program, 'signup_bonus_points', 50),
            'created_at': program.created_at.isoformat() if program.created_at else None
        }
    except Exception:
        return {
            'id': program.id,
            'name': program.name,
            'description': program.description,
            'is_active': getattr(program, 'is_active', True)
        }