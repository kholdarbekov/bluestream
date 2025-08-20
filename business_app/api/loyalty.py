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
from business_app.serializers.loyalty_serializers import (
    serialize_user_loyalty, serialize_loyalty_reward, serialize_referral, serialize_challenge,
    serialize_loyalty_transaction, serialize_loyalty_program,
    UserLoyaltySchema, LoyaltyRewardSchema, CreateReferralRequest, RedeemRewardRequest,
    GiftPointsRequest, JoinChallengeRequest, LoyaltyResponseSchema
)
from business_app.utils.decorators import validate_json, cache_response
from business_app.utils.constants import LoyaltyTransactionType, RewardStatus
from business_app import db

loyalty_bp = Blueprint('loyalty', __name__)


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
        
        return jsonify({
            'success': True,
            'data': {
                'points_balance': loyalty_points.current_balance,
                'lifetime_points': loyalty_points.total_earned,
                'tier': loyalty_points.current_tier,
                'next_tier_threshold': loyalty_points.points_to_next_tier or 0
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get loyalty points error: {e}")
        return jsonify({'error': 'Failed to get loyalty points'}), 500


@loyalty_bp.route('/history', methods=['GET'])
@jwt_required()
def get_loyalty_points_history():
    """Get loyalty points transaction history"""
    try:
        current_user_id = get_jwt_identity()
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 100)
        
        # Get loyalty transactions
        transactions = LoyaltyTransaction.query.filter_by(
            user_id=current_user_id
        ).order_by(LoyaltyTransaction.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'success': True,
            'data': {
                'transactions': [
                    serialize_loyalty_transaction(transaction) 
                    for transaction in transactions.items
                ],
                'pagination': {
                    'page': page,
                    'pages': transactions.pages,
                    'per_page': per_page,
                    'total': transactions.total,
                    'has_next': transactions.has_next,
                    'has_prev': transactions.has_prev
                }
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get loyalty history error: {e}")
        return jsonify({'error': 'Failed to get loyalty history'}), 500



@loyalty_bp.route('/profile', methods=['GET'])
@jwt_required()
def get_loyalty_profile():
    """Get user's loyalty profile"""
    try:
        current_user_id = get_jwt_identity()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
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
        
        return jsonify({
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
        })
        
    except Exception as e:
        current_app.logger.error(f"Get loyalty profile error: {e}")
        return jsonify({'error': 'Failed to get loyalty profile'}), 500


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
                return jsonify({'error': 'Invalid transaction type'}), 400
        
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
                query = query.filter(LoyaltyTransaction.created_at >= start_dt)
            except ValueError:
                return jsonify({'error': 'Invalid start_date format'}), 400
        
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
                query = query.filter(LoyaltyTransaction.created_at <= end_dt)
            except ValueError:
                return jsonify({'error': 'Invalid end_date format'}), 400
        
        # Order by creation date (newest first)
        query = query.order_by(LoyaltyTransaction.created_at.desc())
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'transactions': [
                serialize_loyalty_transaction(txn) for txn in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get points history error: {e}")
        return jsonify({'error': 'Failed to get points history'}), 500


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
        
        return jsonify({
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
        })
        
    except Exception as e:
        current_app.logger.error(f"Get available rewards error: {e}")
        return jsonify({'error': 'Failed to get rewards'}), 500


@loyalty_bp.route('/rewards/<int:reward_id>/redeem', methods=['POST'])
@jwt_required()
def redeem_reward(reward_id):
    """Redeem a loyalty reward"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json() or {}
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        reward = LoyaltyReward.query.filter_by(
            id=reward_id,
            is_active=True
        ).first()
        
        if not reward:
            return jsonify({'error': 'Reward not found'}), 404
        
        # Check if reward is available
        if reward.quantity_limit and reward.redeemed_count >= reward.quantity_limit:
            return jsonify({'error': 'Reward is no longer available'}), 400
        
        # Check user's points balance
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points or loyalty_points.current_balance < reward.points_cost:
            return jsonify({'error': 'Insufficient points balance'}), 400
        
        # Check if user can redeem this reward (frequency limits)
        if not get_loyalty_service().can_redeem_reward(current_user_id, reward_id):
            return jsonify({'error': 'You have reached the redemption limit for this reward'}), 400
        
        # Process reward redemption
        redemption = get_loyalty_service().redeem_reward(
            user_id=current_user_id,
            reward_id=reward_id,
            delivery_address_id=data.get('delivery_address_id'),
            notes=data.get('notes')
        )
        
        # Send notification
        get_notification_service().send_notification(
            current_user_id,
            'reward_redeemed',
            template_data={
                'reward_name': reward.name,
                'points_spent': reward.points_cost,
                'remaining_points': loyalty_points.current_balance - reward.points_cost
            }
        )
        
        return jsonify({
            'message': 'Reward redeemed successfully',
            'redemption': {
                'id': redemption.id,
                'reward_name': reward.name,
                'points_spent': reward.points_cost,
                'status': redemption.status.value,
                'redemption_code': redemption.redemption_code,
                'expires_at': redemption.expires_at.isoformat() if redemption.expires_at else None
            }
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Redeem reward error: {e}")
        return jsonify({'error': 'Failed to redeem reward'}), 500


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
                return jsonify({'error': 'Invalid status value'}), 400
        
        # Order by creation date (newest first)
        query = query.order_by(LoyaltyTransaction.created_at.desc())
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'redemptions': [
                serialize_loyalty_transaction(txn) for txn in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get redemption history error: {e}")
        return jsonify({'error': 'Failed to get redemption history'}), 500


@loyalty_bp.route('/programs', methods=['GET'])
@cache_response(3600)  # Cache for 1 hour
def get_loyalty_programs():
    """Get available loyalty programs"""
    try:
        programs = LoyaltyProgram.query.filter_by(is_active=True).order_by(
            LoyaltyProgram.tier_level.asc()
        ).all()
        
        return jsonify({
            'programs': [
                serialize_loyalty_program(program) for program in programs
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get loyalty programs error: {e}")
        return jsonify({'error': 'Failed to get loyalty programs'}), 500


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
            return jsonify({'error': 'Invalid action type'}), 400
        
        # Award points
        transaction = get_loyalty_service().award_points(
            user_id=current_user_id,
            points=points_amount or get_loyalty_service().get_action_points(action),
            transaction_type=LoyaltyTransactionType.EARNED,
            description=f'Points earned for {action.replace("_", " ")}',
            reference_type=action,
            reference_id=reference_id
        )
        
        return jsonify({
            'message': 'Points awarded successfully',
            'transaction': serialize_loyalty_transaction(transaction)
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Earn points error: {e}")
        return jsonify({'error': 'Failed to earn points'}), 500


@loyalty_bp.route('/referral', methods=['GET'])
@jwt_required()
def get_referral_info():
    """Get user's referral code and statistics"""
    try:
        current_user_id = get_jwt_identity()
        
        user = User.query.get(current_user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        # Get or create referral code
        referral_code = get_loyalty_service().get_user_referral_code(current_user_id)
        
        # Get referral statistics
        referral_stats = get_loyalty_service().get_referral_statistics(current_user_id)
        
        # Get recent referrals
        recent_referrals = db.session.query(User).filter(
            User.referred_by_user_id == current_user_id
        ).order_by(User.created_at.desc()).limit(10).all()
        
        return jsonify({
            'referral_code': referral_code,
            'referral_link': f"{request.host_url}register?ref={referral_code}",
            'statistics': referral_stats,
            'recent_referrals': [
                {
                    'id': ref.id,
                    'name': f"{ref.first_name} {ref.last_name}",
                    'joined_at': ref.created_at.isoformat(),
                    'points_earned': get_loyalty_service().get_referral_points_earned(current_user_id, ref.id)
                }
                for ref in recent_referrals
            ],
            'rewards': {
                'referrer_points': get_loyalty_service().get_referrer_bonus_points(),
                'referee_points': get_loyalty_service().get_referee_bonus_points()
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get referral info error: {e}")
        return jsonify({'error': 'Failed to get referral info'}), 500


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
        
        return jsonify({
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
        })
        
    except Exception as e:
        current_app.logger.error(f"Get loyalty statistics error: {e}")
        return jsonify({'error': 'Failed to get loyalty statistics'}), 500


@loyalty_bp.route('/challenges', methods=['GET'])
@jwt_required()
def get_loyalty_challenges():
    """Get available loyalty challenges"""
    try:
        current_user_id = get_jwt_identity()
        
        # Get user's current challenges
        challenges = get_loyalty_service().get_user_challenges(current_user_id)
        
        return jsonify({
            'challenges': challenges
        })
        
    except Exception as e:
        current_app.logger.error(f"Get loyalty challenges error: {e}")
        return jsonify({'error': 'Failed to get loyalty challenges'}), 500


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
        
        return jsonify({
            'current_tier': current_tier,
            'benefits': tier_benefits,
            'upgrade_info': upgrade_info
        })
        
    except Exception as e:
        current_app.logger.error(f"Get tier benefits error: {e}")
        return jsonify({'error': 'Failed to get tier benefits'}), 500


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
            return jsonify({'error': 'Points amount must be positive'}), 400
        
        # Check sender's balance
        loyalty_points = LoyaltyPoints.query.filter_by(user_id=current_user_id).first()
        if not loyalty_points or loyalty_points.current_balance < points_amount:
            return jsonify({'error': 'Insufficient points balance'}), 400
        
        # Find recipient
        recipient = User.query.filter_by(phone=recipient_phone).first()
        if not recipient:
            return jsonify({'error': 'Recipient not found'}), 404
        
        if recipient.id == current_user_id:
            return jsonify({'error': 'Cannot gift points to yourself'}), 400
        
        # Process gift
        gift_transaction = get_loyalty_service().gift_points(
            sender_id=current_user_id,
            recipient_id=recipient.id,
            points_amount=points_amount,
            message=message
        )
        
        return jsonify({
            'message': 'Points gifted successfully',
            'transaction': serialize_loyalty_transaction(gift_transaction)
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Gift points error: {e}")
        return jsonify({'error': 'Failed to gift points'}), 500