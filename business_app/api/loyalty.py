"""
Loyalty Program API endpoints for the Water Business Platform
This file should be placed in business_app/api/loyalty.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func
from datetime import datetime, UTC, timedelta

from business_app.models.loyalty import LoyaltyProgram, LoyaltyPoints, LoyaltyReward, LoyaltyTransaction
from business_app.models.user import User
from business_app.models.order import Order
from business_app.utils.service_factory import get_loyalty_service, get_notification_service
from business_app.utils.helpers import get_current_language
from business_app.serializers.loyalty_serializers import (
    serialize_user_loyalty, serialize_loyalty_reward, serialize_referral, serialize_challenge,
    serialize_loyalty_transaction, serialize_loyalty_program,
    UserLoyaltySchema, LoyaltyRewardSchema, CreateReferralRequest, RedeemRewardRequest,
    GiftPointsRequest, JoinChallengeRequest, LoyaltyResponseSchema
)
from business_app.utils.decorators import validate_json, cache_response
from business_app.utils.constants import (
    LoyaltyTransactionType, RewardStatus, NotificationType,
    MEMBERSHIP_TIERS, MEMBERSHIP_TIER_ORDER, get_tier_for_points, get_next_tier
)
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, internal_error_response
)
from business_app import db

loyalty_bp = Blueprint('loyalty', __name__)


@loyalty_bp.route('/tiers', methods=['GET'])
@cache_response(3600)  # Cache for 1 hour
def get_membership_tiers():
    """Get all membership tier configurations - single source of truth"""
    try:
        tiers = []
        for tier_name in MEMBERSHIP_TIER_ORDER:
            tier_config = MEMBERSHIP_TIERS[tier_name]
            # Format the points range for display
            if tier_config['max_points'] is not None:
                points_range = f"{tier_config['min_points']:,} - {tier_config['max_points']:,}"
            else:
                points_range = f"{tier_config['min_points']:,}+"
            
            tiers.append({
                'name': tier_name,
                'min_points': tier_config['min_points'],
                'max_points': tier_config['max_points'],
                'points_range': points_range,
                'points_multiplier': tier_config['points_multiplier'],
                'discount_percentage': tier_config['discount_percentage'],
                'benefits': tier_config['benefits'],
                'color': tier_config['color'],
                'icon': tier_config['icon']
            })
        
        return success_response(
            data={
                'tiers': tiers,
                'tier_count': len(tiers)
            }
        )
    except Exception as e:
        current_app.logger.error(f"Get membership tiers error: {e}")
        return internal_error_response('Failed to get membership tiers')


@loyalty_bp.route('/points', methods=['GET'])
@jwt_required()
def get_loyalty_points():
    """Get user's loyalty points balance"""
    try:
        current_user_id = get_jwt_identity()

        # Get or create loyalty points record
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points:
            loyalty_points = get_loyalty_service().create_loyalty_account(current_user_id)

        return success_response(
            data={
                'points_balance': loyalty_points.current_balance,
                'lifetime_points': loyalty_points.total_earned,
                'tier': loyalty_points.current_tier,
                'next_tier_threshold': loyalty_points.points_to_next_tier or 0
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty points error: {e}")
        return internal_error_response('Failed to get loyalty points')


@loyalty_bp.route('/account', methods=['GET'])
@jwt_required()
def get_loyalty_account():
    """Get complete loyalty account data for frontend dashboard"""
    try:
        current_user_id = get_jwt_identity()

        # Get or create loyalty points record
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points:
            loyalty_points = get_loyalty_service().create_loyalty_account(current_user_id)

        # Calculate points earned this month
        from datetime import datetime
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        points_this_month = db.session.query(func.sum(LoyaltyTransaction.points)).filter(
            LoyaltyTransaction.user_id == current_user_id,
            LoyaltyTransaction.points > 0,
            LoyaltyTransaction.created_at >= month_start
        ).scalar() or 0

        # Calculate tier progress using centralized config
        current_tier = loyalty_points.current_tier or 'Bronze'
        current_balance = loyalty_points.current_balance or 0
        
        # Get current tier config
        current_tier_config = MEMBERSHIP_TIERS.get(current_tier, MEMBERSHIP_TIERS['Bronze'])
        current_tier_points = current_tier_config['min_points']
        
        # Find next tier info
        next_tier_info = get_next_tier(current_tier)
        
        if next_tier_info:
            next_tier_points = next_tier_info['min_points']
            points_needed = max(0, next_tier_points - current_balance)
        else:
            next_tier_points = current_tier_points
            points_needed = 0

        tier_progress = {
            'current': current_balance - current_tier_points if next_tier_info else current_balance,
            'next_tier_points': next_tier_points - current_tier_points if next_tier_info else 0,
            'points_needed': points_needed
        }

        # Count available rewards user can redeem
        available_rewards_count = LoyaltyReward.query.filter(
            LoyaltyReward.is_active == True,
            LoyaltyReward.points_cost <= current_balance
        ).count()

        return success_response(
            data={
                'current_balance': current_balance,
                'current_tier': current_tier,
                'points_this_month': points_this_month,
                'tier_progress': tier_progress,
                'available_rewards_count': available_rewards_count,
                'total_earned': loyalty_points.total_earned or 0,
                'total_redeemed': loyalty_points.total_redeemed or 0
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty account error: {e}")
        return internal_error_response('Failed to get loyalty account')


@loyalty_bp.route('/history', methods=['GET'])
@jwt_required()
def get_loyalty_points_history():
    """Get loyalty points transaction history"""
    try:
        current_user_id = get_jwt_identity()
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)

        # Get loyalty transactions
        pagination = LoyaltyTransaction.query.filter_by(
            user_id=current_user_id
        ).order_by(LoyaltyTransaction.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )

        return paginated_response(
            items=[
                serialize_loyalty_transaction(transaction)
                for transaction in pagination.items
            ],
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty history error: {e}")
        return internal_error_response('Failed to get loyalty history')



@loyalty_bp.route('/profile', methods=['GET'])
@jwt_required()
def get_loyalty_profile():
    """Get user's loyalty profile"""
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response('User not found')

        # Get or create loyalty points record
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points:
            loyalty_points = get_loyalty_service().create_loyalty_account(current_user_id)

        # Get active loyalty program
        active_program = LoyaltyProgram.query.filter_by(
            is_active=True,
            is_default=True
        ).first()

        # Get recent transactions
        recent_transactions = LoyaltyTransaction.query.filter_by(
            user_id=current_user_id
        ).order_by(LoyaltyTransaction.created_at.desc()).limit(10).all()

        # Calculate tier progress
        tier_progress = get_loyalty_service().calculate_tier_progress(current_user_id)

        return success_response(
            data={
                'loyalty_profile': {
                    'points_balance': loyalty_points.current_balance,
                    'total_earned': loyalty_points.total_earned,
                    'total_redeemed': loyalty_points.total_redeemed,
                    'current_tier': loyalty_points.current_tier,
                    'tier_progress': tier_progress,
                    'member_since': loyalty_points.created_at.isoformat(),
                    'expires_at': loyalty_points.expires_at.isoformat() if loyalty_points.expires_at else None
                },
                'active_program': serialize_loyalty_program(active_program) if active_program else None,
                'recent_transactions': [
                    serialize_loyalty_transaction(txn) for txn in recent_transactions
                ]
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty profile error: {e}")
        return internal_error_response('Failed to get loyalty profile')


@loyalty_bp.route('/points/history', methods=['GET'])
@jwt_required()
def get_points_history():
    """Get user's points transaction history"""
    try:
        current_user_id = get_jwt_identity()

        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 50)
        transaction_type = request.args.get('type')
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        # Build query
        query = LoyaltyTransaction.query.filter_by(user_id=current_user_id)

        # Apply filters
        if transaction_type:
            try:
                txn_type = LoyaltyTransactionType(transaction_type)
                query = query.filter_by(transaction_type=txn_type)
            except ValueError:
                return error_response('Invalid transaction type')

        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
                query = query.filter(LoyaltyTransaction.created_at >= start_dt)
            except ValueError:
                return error_response('Invalid start_date format')

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
                query = query.filter(LoyaltyTransaction.created_at <= end_dt)
            except ValueError:
                return error_response('Invalid end_date format')

        # Order by creation date (newest first)
        query = query.order_by(LoyaltyTransaction.created_at.desc())

        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )

        return paginated_response(
            items=[
                serialize_loyalty_transaction(txn) for txn in pagination.items
            ],
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get points history error: {e}")
        return internal_error_response('Failed to get points history')


@loyalty_bp.route('/rewards', methods=['GET'])
@jwt_required()
def get_available_rewards():
    """Get available loyalty rewards"""
    try:
        current_user_id = get_jwt_identity()
        category = request.args.get('category')
        min_points = request.args.get('min_points', type=int)
        max_points = request.args.get('max_points', type=int)

        # Get user's points balance
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        user_points = loyalty_points.current_balance if loyalty_points else 0

        # Build query
        query = LoyaltyReward.query.filter_by(is_active=True)

        # Apply filters
        if category:
            query = query.filter_by(category=category)

        if min_points is not None:
            query = query.filter(LoyaltyReward.points_cost >= min_points)

        if max_points is not None:
            query = query.filter(LoyaltyReward.points_cost <= max_points)

        # Order by points cost
        query = query.order_by(LoyaltyReward.points_cost.asc())

        rewards = query.all()

        return success_response(
            data={
                'rewards': [
                    {
                        **serialize_loyalty_reward(reward, None),
                        'can_redeem': user_points >= reward.points_cost,
                        'points_needed': max(0, reward.points_cost - user_points)
                    }
                    for reward in rewards
                ],
                'user_points_balance': user_points,
                'categories': get_loyalty_service().get_reward_categories()
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get available rewards error: {e}")
        return internal_error_response('Failed to get rewards')


@loyalty_bp.route('/rewards/<int:reward_id>', methods=['GET'])
@jwt_required()
def get_reward_details(reward_id):
    """Get single reward details by ID"""
    try:
        current_user_id = get_jwt_identity()

        reward = LoyaltyReward.query.filter_by(
            id=reward_id,
            is_active=True
        ).first()

        if not reward:
            return not_found_response('Reward not found')

        # Get user's points balance
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        user_points = loyalty_points.current_balance if loyalty_points else 0

        # Get language for translations
        language = get_current_language()

        return success_response(
            data={
                'id': reward.id,
                'name': reward.get_translated('name', language),
                'description': reward.get_translated('description', language),
                'reward_type': reward.reward_type,
                'points_cost': reward.points_cost,
                'points_required': reward.points_cost,  # Alias for compatibility
                'min_order_value': float(reward.min_order_value) if reward.min_order_value else None,
                'discount_type': reward.discount_type,
                'discount_value': float(reward.discount_value) if reward.discount_value else None,
                'image_url': reward.image_url,
                'terms_conditions': reward.terms_conditions,
                'valid_from': reward.valid_from.isoformat() if reward.valid_from else None,
                'valid_until': reward.valid_until.isoformat() if reward.valid_until else None,
                'can_redeem': user_points >= reward.points_cost,
                'points_needed': max(0, reward.points_cost - user_points),
                'user_points_balance': user_points
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get reward details error: {e}")
        return internal_error_response('Failed to get reward details')


@loyalty_bp.route('/rewards/<int:reward_id>/redeem', methods=['POST'])
@jwt_required()
def redeem_reward(reward_id):
    """Redeem a loyalty reward"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response('User not found')

        reward = LoyaltyReward.query.filter_by(
            id=reward_id,
            is_active=True
        ).first()

        if not reward:
            return not_found_response('Reward not found')

        # System rewards cannot be manually redeemed - they are applied automatically by the system
        if reward.is_system_reward:
            return error_response('This reward is automatically applied by the system and cannot be manually redeemed')

        # Check if reward is available (use correct model field names)
        if reward.max_redemptions and reward.redemptions_used >= reward.max_redemptions:
            return error_response('Reward is no longer available')

        # Check user's points balance
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points or loyalty_points.current_balance < reward.points_cost:
            return error_response('Insufficient points balance')

        # Check if user can redeem this reward (frequency limits)
        if not get_loyalty_service().can_redeem_reward(current_user_id, reward_id):
            return error_response('You have reached the redemption limit for this reward')

        # Process reward redemption
        redemption = get_loyalty_service().redeem_reward(
            user_id=current_user_id,
            reward_id=reward_id,
            delivery_address_id=data.get('delivery_address_id'),
            notes=data.get('notes')
        )

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            NotificationType.REWARD_REDEEMED,
            template_data={
                'reward_name': reward.get_translated('name', language),
                'points_spent': reward.points_cost,
                'remaining_points': loyalty_points.current_balance,
                'redemption_code': redemption['redemption_code'],
                'expires_at': redemption['expires_at'],
                'loyalty_url': f"{current_app.config.get('FRONTEND_URL', '')}/cabinet/loyalty"
            }
        )

        return created_response(
            data={
                'redemption': {
                    'id': redemption['id'],
                    'reward_name': reward.get_translated('name', language),
                    'points_spent': redemption['points_spent'],
                    'status': redemption['status'],
                    'redemption_code': redemption['redemption_code'],
                    'expires_at': redemption['expires_at']
                }
            },
            message='Reward redeemed successfully'
        )

    except ValueError as e:
        return error_response(str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Redeem reward error: {e}")
        return internal_error_response('Failed to redeem reward')


@loyalty_bp.route('/rewards/history', methods=['GET'])
@jwt_required()
def get_redemption_history():
    """Get user's reward redemption history"""
    try:
        current_user_id = get_jwt_identity()

        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 50)
        status = request.args.get('status')

        # Build query
        query = db.session.query(LoyaltyTransaction).filter(
            and_(
                LoyaltyTransaction.user_id == current_user_id,
                LoyaltyTransaction.transaction_type == LoyaltyTransactionType.REDEEMED
            )
        )

        # Apply status filter
        if status:
            try:
                reward_status = RewardStatus(status)
                query = query.join(LoyaltyReward).filter(LoyaltyReward.status == reward_status)
            except ValueError:
                return error_response('Invalid status value')

        # Order by creation date (newest first)
        query = query.order_by(LoyaltyTransaction.created_at.desc())

        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )

        return paginated_response(
            items=[
                serialize_loyalty_transaction(txn) for txn in pagination.items
            ],
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except Exception as e:
        current_app.logger.error(f"Get redemption history error: {e}")
        return internal_error_response('Failed to get redemption history')


@loyalty_bp.route('/programs', methods=['GET'])
@cache_response(3600)  # Cache for 1 hour
def get_loyalty_programs():
    """Get available loyalty programs"""
    try:
        programs = LoyaltyProgram.query.filter_by(is_active=True).order_by(
            LoyaltyProgram.tier_level.asc()
        ).all()

        return success_response(
            data={
                'programs': [
                    serialize_loyalty_program(program) for program in programs
                ]
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty programs error: {e}")
        return internal_error_response('Failed to get loyalty programs')


@loyalty_bp.route('/earn-points', methods=['POST'])
@jwt_required()
@validate_json(['action'])
def earn_points():
    """Manually award points for specific actions"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        action = data.get('action')
        reference_id = data.get('reference_id')
        points_amount = data.get('points_amount')

        # Validate action
        valid_actions = [
            'referral_signup',
            'social_share',
            'review_submitted',
            'birthday_bonus',
            'survey_completed',
            'app_install',
            'newsletter_signup'
        ]

        if action not in valid_actions:
            return error_response('Invalid action type')

        # Award points
        transaction = get_loyalty_service().award_points(
            user_id=current_user_id,
            points=points_amount or get_loyalty_service().get_action_points(action),
            transaction_type=LoyaltyTransactionType.EARNED,
            description=f'Points earned for {action.replace("_", " ")}',
            reference_type=action,
            reference_id=reference_id
        )

        return created_response(
            data={
                'transaction': serialize_loyalty_transaction(transaction)
            },
            message='Points awarded successfully'
        )

    except ValueError as e:
        return error_response(str(e))
    except Exception as e:
        current_app.logger.error(f"Earn points error: {e}")
        return internal_error_response('Failed to earn points')


@loyalty_bp.route('/referral', methods=['GET'])
@jwt_required()
def get_referral_info():
    """Get user's referral code and statistics"""
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response('User not found')

        # Get or create referral code
        referral_code = get_loyalty_service().get_user_referral_code(current_user_id)

        # Get referral statistics with error handling
        try:
            referral_stats = get_loyalty_service().get_referral_statistics(current_user_id)
        except Exception as stats_error:
            current_app.logger.warning(f"Failed to get referral stats: {stats_error}")
            referral_stats = {
                'total_referrals': 0,
                'pending_referrals': 0,
                'points_earned_from_referrals': 0
            }

        # Get recent referrals from ReferralProgram model (not User model)
        recent_referrals_data = []
        try:
            from business_app.models.loyalty import ReferralProgram
            recent_referrals = ReferralProgram.query.filter_by(
                referrer_id=current_user_id,
                status='completed'
            ).order_by(ReferralProgram.completed_at.desc()).limit(10).all()
            
            for ref in recent_referrals:
                referee = User.query.get(ref.referee_id) if ref.referee_id else None
                if referee:
                    recent_referrals_data.append({
                        'id': ref.id,
                        'name': f"{referee.first_name or ''} {referee.last_name or ''}".strip() or 'Anonymous',
                        'joined_at': ref.completed_at.isoformat() if ref.completed_at else ref.created_at.isoformat(),
                        'points_earned': ref.referrer_bonus_points or 0
                    })
        except Exception as ref_error:
            current_app.logger.warning(f"Failed to get recent referrals: {ref_error}")
            db.session.rollback()

        return success_response(
            data={
                'referral_code': referral_code,
                'referral_link': f"{request.host_url}register?ref={referral_code}",
                'statistics': referral_stats,
                'recent_referrals': recent_referrals_data,
                'rewards': {
                    'referrer_points': get_loyalty_service().get_referrer_bonus_points(),
                    'referee_points': get_loyalty_service().get_referee_bonus_points()
                }
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get referral info error: {e}")
        return internal_error_response('Failed to get referral info')


@loyalty_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_loyalty_statistics():
    """Get user's loyalty statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'year')  # month, quarter, year, all

        # Calculate date range
        now = datetime.now(UTC)
        if period == 'month':
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'quarter':
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'year':
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:  # all time
            start_date = None

        # Get loyalty points record
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()

        # Base query for transactions
        query = LoyaltyTransaction.query.filter_by(user_id=current_user_id)
        if start_date:
            query = query.filter(LoyaltyTransaction.created_at >= start_date)

        transactions = query.all()

        # Calculate statistics
        total_earned = sum(t.points for t in transactions if t.transaction_type == LoyaltyTransactionType.EARNED)
        total_redeemed = sum(abs(t.points) for t in transactions if t.transaction_type == LoyaltyTransactionType.REDEEMED)
        transaction_count = len(transactions)

        # Points by source
        points_by_source = {}
        for txn in transactions:
            if txn.transaction_type == LoyaltyTransactionType.EARNED:
                source = txn.reference_type or 'purchase'
                points_by_source[source] = points_by_source.get(source, 0) + txn.points

        # Monthly points trend
        monthly_points = {}
        for i in range(12):
            month_start = (now.replace(day=1) - timedelta(days=32*i)).replace(day=1)
            month_end = (month_start.replace(month=month_start.month % 12 + 1)
                        if month_start.month < 12
                        else month_start.replace(year=month_start.year + 1, month=1))

            month_transactions = [t for t in transactions
                                if month_start <= t.created_at < month_end and
                                t.transaction_type == LoyaltyTransactionType.EARNED]
            month_total = sum(t.points for t in month_transactions)

            monthly_points[month_start.strftime('%Y-%m')] = month_total

        # Tier history
        tier_history = get_loyalty_service().get_tier_history(current_user_id)

        return success_response(
            data={
                'period': period,
                'statistics': {
                    'current_balance': loyalty_points.current_balance if loyalty_points else 0,
                    'total_earned': total_earned,
                    'total_redeemed': total_redeemed,
                    'net_points': total_earned - total_redeemed,
                    'transaction_count': transaction_count,
                    'current_tier': loyalty_points.current_tier if loyalty_points else 'Bronze',
                    'points_by_source': points_by_source,
                    'monthly_points_trend': monthly_points,
                    'tier_history': tier_history
                }
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty statistics error: {e}")
        return internal_error_response('Failed to get loyalty statistics')


@loyalty_bp.route('/challenges', methods=['GET'])
@jwt_required()
def get_loyalty_challenges():
    """Get available loyalty challenges"""
    try:
        current_user_id = get_jwt_identity()

        # Get user's current challenges
        challenges = get_loyalty_service().get_user_challenges(current_user_id)

        return success_response(
            data={
                'challenges': challenges
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get loyalty challenges error: {e}")
        return internal_error_response('Failed to get loyalty challenges')


@loyalty_bp.route('/tier-benefits', methods=['GET'])
@jwt_required()
def get_tier_benefits():
    """Get benefits for user's current tier and upgrade requirements"""
    try:
        current_user_id = get_jwt_identity()

        # Get user's loyalty points record
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        current_tier = loyalty_points.current_tier if loyalty_points else 'Bronze'

        # Get tier benefits
        tier_benefits = get_loyalty_service().get_tier_benefits(current_tier)

        # Get upgrade requirements
        upgrade_info = get_loyalty_service().get_tier_upgrade_requirements(current_user_id)

        return success_response(
            data={
                'current_tier': current_tier,
                'benefits': tier_benefits,
                'upgrade_info': upgrade_info
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get tier benefits error: {e}")
        return internal_error_response('Failed to get tier benefits')


@loyalty_bp.route('/gift-points', methods=['POST'])
@jwt_required()
@validate_json(['recipient_phone', 'points_amount'])
def gift_points():
    """Gift points to another user"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        recipient_phone = data.get('recipient_phone')
        points_amount = data.get('points_amount')
        message = data.get('message', '')

        if points_amount <= 0:
            return error_response('Points amount must be positive')

        # Check sender's balance
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points or loyalty_points.current_balance < points_amount:
            return error_response('Insufficient points balance')

        # Find recipient
        recipient = User.query.filter_by(phone=recipient_phone).first()
        if not recipient:
            return not_found_response('Recipient not found')

        if recipient.id == current_user_id:
            return error_response('Cannot gift points to yourself')

        # Process gift
        gift_transaction = get_loyalty_service().gift_points(
            sender_id=current_user_id,
            recipient_id=recipient.id,
            points_amount=points_amount,
            message=message
        )

        return created_response(
            data={
                'transaction': serialize_loyalty_transaction(gift_transaction)
            },
            message='Points gifted successfully'
        )

    except ValueError as e:
        return error_response(str(e))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Gift points error: {e}")
        return internal_error_response('Failed to gift points')