"""
Loyalty service for the Water Business Platform
Handles loyalty points, rewards, referrals, and customer retention programs
"""

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from flask import current_app
from sqlalchemy import func, and_

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyTransaction,
    LoyaltyReward,
    LoyaltyProgram,
    ReferralProgram,
    LoyaltyTierConfig,
)
from business_app.models.user import User
from business_app.models.order import Order
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError
from business_app.utils.constants import (
    LoyaltyActionType,
    LoyaltyTransactionType,
    RewardStatus,
)
from shared.enums import (
    OrderStatus,
)
from business_app import db


class LoyaltyService:
    """Service for managing loyalty programs"""

    # TODO: Note for mylself: correct loyalty points for user_id in (8,9,1, 12,7,13,10,31,22);

    def __init__(self):
        self.points_ratio = current_app.config.get("LOYALTY_POINTS_RATIO", 100)  # 1 point per 100 UZS
        self.redemption_ratio = current_app.config.get("LOYALTY_REDEMPTION_RATIO", 1)  # 1 point = 1 UZS
        self.referral_bonus = current_app.config.get("REFERRAL_BONUS_POINTS", 500)
        self.points_expiry_days = current_app.config.get("LOYALTY_POINTS_EXPIRY_DAYS", 365)

    def calculate_points_for_purchase(self, user_id: int, amount: int) -> int:
        """
        Calculate loyalty points earned for a purchase amount.

        Uses LoyaltyProgram.points_per_uzs from database as primary source,
        then applies tier-based multiplier from LoyaltyTierConfig (database).

        Args:
            user_id: User ID
            amount: Purchase amount in UZS

        Returns:
            Number of points to award
        """
        if amount <= 0:
            return 0

        # Get user's loyalty account and program
        account = self.get_or_create_loyalty_account(user_id)

        # Get uzs_per_point from LoyaltyProgram (primary source)
        # Default: 250 UZS = 1 AquaCoin
        uzs_per_point = 250
        if account.program:
            uzs_per_point = account.program.uzs_per_point or 250

        # Calculate base points (Floor division)
        base_points = amount // uzs_per_point

        # Get tier-based multiplier from database (preferred) or constants (fallback)
        current_tier = account.current_tier or "Bronze"
        multiplier = self._get_tier_multiplier(current_tier, account.program_id)

        # Final points calculation
        final_points = int(base_points * multiplier)

        return max(0, final_points)

    def _get_tier_multiplier(self, tier_name: str, program_id: int = None) -> float:
        """
        Get points multiplier for a tier.

        Queries LoyaltyTierConfig from database.
        """
        # Try database first
        try:
            tier = LoyaltyTierConfig.query.filter_by(name=tier_name, is_active=True)
            if program_id:
                tier = tier.filter_by(program_id=program_id)
            tier = tier.first()

            if tier:
                return tier.points_multiplier or 1.0
        except Exception:
            pass

        # Default behavior if tier not found
        return 1.0

    def get_tiers(self, program_id: int = None) -> List[Dict[str, Any]]:
        """
        Get all tier configurations from database.

        Returns an empty list if no tiers are configured.
        """
        try:
            tiers = LoyaltyTierConfig.get_all_tiers(program_id)
            if tiers:
                return [tier.to_dict() for tier in tiers]
        except Exception:
            pass

        # Return empty list if no tiers configured in database
        return []

    def get_or_create_loyalty_account(self, user_id: int) -> LoyaltyPoints:
        """Get or create loyalty account for user"""
        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()

        if not account:
            user = User.query.get(user_id)
            if not user:
                raise NotFoundError("User not found")

            # Get default loyalty program
            program = LoyaltyProgram.query.filter_by(is_default=True).first()
            if not program:
                program = LoyaltyProgram.query.filter_by(is_active=True).first()

            program_id = program.id if program else 1

            # Determine starting tier for 0 points using database config
            # This mirrors the logic in LoyaltyPoints.calculate_tier()
            starting_tier = LoyaltyTierConfig.get_tier_for_points(0, program_id)
            current_tier_name = starting_tier.name if starting_tier else "Bronze"
            starting_order = starting_tier.display_order if starting_tier else -1

            # Find next tier above starting tier
            next_tier = (
                LoyaltyTierConfig.query.filter(
                    LoyaltyTierConfig.program_id == program_id,
                    LoyaltyTierConfig.is_active == True,
                    LoyaltyTierConfig.display_order > starting_order,
                )
                .order_by(LoyaltyTierConfig.display_order.asc())
                .first()
            )

            # Points needed is the next tier's min_points (since user has 0 points)
            points_to_next_tier = next_tier.min_points if next_tier else 0

            account = LoyaltyPoints(
                user_id=user_id,
                program_id=program_id,
                total_earned=0,
                total_redeemed=0,
                total_expired=0,
                current_balance=0,
                current_tier=current_tier_name,
                points_to_next_tier=points_to_next_tier,
            )

            db.session.add(account)
            db.session.commit()

        return account

    def get_points_summary_for_user(self, user_id: int) -> Dict[str, Any]:
        """Get points summary payload for API."""
        account = self.get_or_create_loyalty_account(user_id)
        return {
            "points_balance": account.current_balance or 0,
            "lifetime_points": account.total_earned or 0,
            "current_balance": account.current_balance or 0,
            "lifetime_earned": account.total_earned or 0,
            "tier": account.current_tier,
            "next_tier_threshold": account.points_to_next_tier or 0,
        }

    def get_account_dashboard_for_user(self, user_id: int) -> Dict[str, Any]:
        """Get account dashboard metrics for API."""
        account = self.get_or_create_loyalty_account(user_id)
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        points_this_month = (
            db.session.query(func.sum(LoyaltyTransaction.points))
            .filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.created_at >= month_start,
            )
            .scalar()
            or 0
        )

        current_tier = account.current_tier or "Bronze"
        current_balance = account.current_balance or 0
        program_id = account.program_id

        current_tier_config = LoyaltyTierConfig.query.filter_by(
            name=current_tier,
            program_id=program_id,
            is_active=True,
        ).first()
        current_tier_points = current_tier_config.min_points if current_tier_config else 0
        current_tier_order = current_tier_config.display_order if current_tier_config else -1

        next_tier_config = (
            LoyaltyTierConfig.query.filter(
                LoyaltyTierConfig.program_id == program_id,
                LoyaltyTierConfig.is_active == True,
                LoyaltyTierConfig.display_order > current_tier_order,
            )
            .order_by(LoyaltyTierConfig.display_order.asc())
            .first()
        )

        if next_tier_config:
            next_tier_points = next_tier_config.min_points
            points_needed = max(0, next_tier_points - current_balance)
            current_progress = current_balance - current_tier_points
            next_tier_progress_target = next_tier_points - current_tier_points
        else:
            next_tier_points = current_tier_points
            points_needed = 0
            current_progress = current_balance
            next_tier_progress_target = 0

        available_rewards_count = LoyaltyReward.query.filter(
            LoyaltyReward.is_active == True,
            LoyaltyReward.points_cost <= current_balance,
        ).count()

        return {
            "current_balance": current_balance,
            "current_tier": current_tier,
            "points_this_month": points_this_month,
            "tier_progress": {
                "current": current_progress,
                "next_tier_points": next_tier_progress_target,
                "points_needed": points_needed,
            },
            "available_rewards_count": available_rewards_count,
            "total_earned": account.total_earned or 0,
            "total_redeemed": account.total_redeemed or 0,
        }

    def get_loyalty_history_for_user(self, user_id: int, page: int, per_page: int) -> Dict[str, Any]:
        """Get paginated loyalty history for API."""
        pagination = (
            LoyaltyTransaction.query.filter_by(
                user_id=user_id,
            )
            .order_by(LoyaltyTransaction.created_at.desc())
            .paginate(
                page=page,
                per_page=per_page,
                error_out=False,
            )
        )
        return {
            "items": pagination.items,
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
        }

    def get_profile_for_user(self, user_id: int) -> Dict[str, Any]:
        """Get full loyalty profile payload for API."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        account = self.get_or_create_loyalty_account(user_id)
        active_program = LoyaltyProgram.query.filter_by(
            is_active=True,
            is_default=True,
        ).first()
        recent_transactions = (
            LoyaltyTransaction.query.filter_by(
                user_id=user_id,
            )
            .order_by(LoyaltyTransaction.created_at.desc())
            .limit(10)
            .all()
        )
        tier_progress = self.calculate_tier_progress(user_id)

        return {
            "loyalty_profile": {
                "points_balance": account.current_balance,
                "total_earned": account.total_earned,
                "total_redeemed": account.total_redeemed,
                "current_tier": account.current_tier,
                "tier_progress": tier_progress,
                "member_since": account.created_at.isoformat(),
                "expires_at": (account.expires_at.isoformat() if getattr(account, "expires_at", None) else None),
            },
            "active_program": active_program,
            "recent_transactions": recent_transactions,
        }

    def get_filtered_points_history_for_user(
        self,
        user_id: int,
        page: int,
        per_page: int,
        transaction_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get filtered points history for API."""
        query = LoyaltyTransaction.query.filter_by(user_id=user_id)

        if transaction_type:
            try:
                txn_type = LoyaltyTransactionType(transaction_type)
            except ValueError as exc:
                raise ValidationError("Invalid transaction type") from exc
            query = query.filter_by(transaction_type=txn_type)

        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date)
            except ValueError as exc:
                raise ValidationError("Invalid start date format") from exc
            query = query.filter(LoyaltyTransaction.created_at >= start_dt)

        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date)
            except ValueError as exc:
                raise ValidationError("Invalid end date format") from exc
            query = query.filter(LoyaltyTransaction.created_at <= end_dt)

        pagination = query.order_by(LoyaltyTransaction.created_at.desc()).paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )
        return {
            "items": pagination.items,
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
        }

    def get_rewards_for_user(
        self,
        user_id: int,
        category: Optional[str] = None,
        min_points: Optional[int] = None,
        max_points: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get rewards list payload for API."""
        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        user_points = account.current_balance if account else 0

        query = LoyaltyReward.query.filter_by(is_active=True)
        if category:
            query = query.filter_by(reward_type=category)
        if min_points is not None:
            query = query.filter(LoyaltyReward.points_cost >= min_points)
        if max_points is not None:
            query = query.filter(LoyaltyReward.points_cost <= max_points)

        rewards = query.order_by(LoyaltyReward.points_cost.asc()).all()
        return {
            "rewards": rewards,
            "user_points_balance": user_points,
            "categories": self.get_reward_categories(),
        }

    def get_reward_details_for_user(self, user_id: int, reward_id: int) -> Dict[str, Any]:
        """Get single reward details payload for API."""
        reward = LoyaltyReward.query.filter_by(
            id=reward_id,
            is_active=True,
        ).first()
        if not reward:
            raise NotFoundError("Reward not found")

        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        user_points = account.current_balance if account else 0
        return {
            "reward": reward,
            "user_points_balance": user_points,
            "can_redeem": user_points >= reward.points_cost,
            "points_needed": max(0, reward.points_cost - user_points),
        }

    def redeem_reward_for_user(
        self,
        user_id: int,
        reward_id: int,
        delivery_address_id: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate and redeem reward for API."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        reward = LoyaltyReward.query.filter_by(
            id=reward_id,
            is_active=True,
        ).first()
        if not reward:
            raise NotFoundError("Reward not found")

        if reward.is_system_reward:
            raise ValidationError("Reward is auto-applied")

        if reward.max_redemptions and reward.redemptions_used >= reward.max_redemptions:
            raise ValidationError("Reward is no longer available")

        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        if not account or account.current_balance < reward.points_cost:
            raise ValidationError("Insufficient points")

        if not self.can_redeem_reward(user_id, reward_id):
            raise ValidationError("Redemption limit reached")

        redemption = self.redeem_reward(
            user_id=user_id,
            reward_id=reward_id,
            delivery_address_id=delivery_address_id,
            notes=notes,
        )
        refreshed_account = LoyaltyPoints.query.filter_by(user_id=user_id).first()

        return {
            "reward": reward,
            "redemption": redemption,
            "remaining_points": refreshed_account.current_balance if refreshed_account else 0,
        }

    def get_redemption_history_for_user(
        self,
        user_id: int,
        page: int,
        per_page: int,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get user's reward redemption history for API."""
        base_query = LoyaltyTransaction.query.filter(
            and_(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.transaction_type == LoyaltyTransactionType.REDEEMED,
            )
        ).order_by(LoyaltyTransaction.created_at.desc())

        if not status:
            pagination = base_query.paginate(page=page, per_page=per_page, error_out=False)
            return {
                "items": pagination.items,
                "total": pagination.total,
                "page": page,
                "per_page": per_page,
            }

        try:
            reward_status = RewardStatus(status)
        except ValueError as exc:
            raise ValidationError("Invalid status value") from exc

        # Reward status is not persisted as a first-class reward field in current schema;
        # filter using transaction metadata when present.
        filtered_transactions = [
            txn for txn in base_query.all() if ((txn.extra_data or {}).get("reward_status") == reward_status.value)
        ]
        total = len(filtered_transactions)
        offset = max(0, (page - 1) * per_page)
        page_items = filtered_transactions[offset : offset + per_page]
        return {
            "items": page_items,
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    def get_active_programs(self) -> List[LoyaltyProgram]:
        """Get active loyalty programs for API."""
        return (
            LoyaltyProgram.query.filter_by(is_active=True)
            .order_by(
                LoyaltyProgram.created_at.desc(),
            )
            .all()
        )

    def get_referral_info_for_user(self, user_id: int, host_url: str) -> Dict[str, Any]:
        """Get referral payload for API."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        referral_code = self.get_user_referral_code(user_id)
        try:
            referral_stats = self.get_referral_statistics(user_id)
        except Exception:
            referral_stats = {
                "total_referrals": 0,
                "pending_referrals": 0,
                "points_earned_from_referrals": 0,
            }

        recent_referrals = (
            ReferralProgram.query.filter_by(
                referrer_id=user_id,
                status="completed",
            )
            .order_by(ReferralProgram.completed_at.desc())
            .limit(10)
            .all()
        )

        recent_referrals_data = []
        for referral in recent_referrals:
            referee = User.query.get(referral.referee_id) if referral.referee_id else None
            if not referee:
                continue
            recent_referrals_data.append(
                {
                    "id": referral.id,
                    "name": f"{referee.first_name or ''} {referee.last_name or ''}".strip() or "Anonymous",
                    "joined_at": (
                        referral.completed_at.isoformat() if referral.completed_at else referral.created_at.isoformat()
                    ),
                    "points_earned": referral.referrer_bonus_points or 0,
                }
            )

        return {
            "referral_code": referral_code,
            "referral_link": f"{host_url}register?ref={referral_code}",
            "statistics": referral_stats,
            "recent_referrals": recent_referrals_data,
            "rewards": {
                "referrer_points": self.get_referrer_bonus_points(),
                "referee_points": self.get_referee_bonus_points(),
            },
        }

    def get_statistics_for_user(self, user_id: int, period: str) -> Dict[str, Any]:
        """Get loyalty statistics payload for API."""
        now = datetime.now(timezone.utc)
        if period == "month":
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "quarter":
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(
                month=quarter_start_month,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        elif period == "year":
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start_date = None

        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        query = LoyaltyTransaction.query.filter_by(user_id=user_id)
        if start_date:
            query = query.filter(LoyaltyTransaction.created_at >= start_date)
        transactions = query.all()

        total_earned = sum(t.points for t in transactions if t.transaction_type == LoyaltyTransactionType.EARNED)
        total_redeemed = sum(
            abs(t.points) for t in transactions if t.transaction_type == LoyaltyTransactionType.REDEEMED
        )
        points_by_source: Dict[str, int] = {}
        for txn in transactions:
            if txn.transaction_type == LoyaltyTransactionType.EARNED:
                source = (txn.extra_data or {}).get("action_type") or "purchase"
                points_by_source[source] = points_by_source.get(source, 0) + txn.points

        monthly_points: Dict[str, int] = {}
        current_month_anchor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(12):
            month_start = current_month_anchor
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
            month_key = month_start.strftime("%Y-%m")
            monthly_points[month_key] = sum(
                txn.points
                for txn in transactions
                if (txn.transaction_type == LoyaltyTransactionType.EARNED and month_start <= txn.created_at < month_end)
            )
            previous_month_anchor = month_start - timedelta(days=1)
            current_month_anchor = previous_month_anchor.replace(day=1)

        return {
            "period": period,
            "statistics": {
                "current_balance": account.current_balance if account else 0,
                "total_earned": total_earned,
                "total_redeemed": total_redeemed,
                "net_points": total_earned - total_redeemed,
                "transaction_count": len(transactions),
                "current_tier": account.current_tier if account else "Bronze",
                "points_by_source": points_by_source,
                "monthly_points_trend": monthly_points,
                "tier_history": self.get_tier_history(user_id),
            },
        }

    def get_tier_benefits_for_user(self, user_id: int) -> Dict[str, Any]:
        """Get tier benefits payload for API."""
        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        current_tier = account.current_tier if account else "Bronze"
        return {
            "current_tier": current_tier,
            "benefits": self.get_tier_benefits(current_tier),
            "upgrade_info": self.get_tier_upgrade_requirements(user_id),
        }

    def gift_points_by_phone(
        self,
        sender_id: int,
        recipient_phone: str,
        points_amount: int,
        message: str = "",
    ) -> LoyaltyTransaction:
        """Gift points to a recipient resolved by phone number."""
        if points_amount <= 0:
            raise ValidationError("Points amount must be positive")

        sender_account = LoyaltyPoints.query.filter_by(user_id=sender_id).first()
        if not sender_account or sender_account.current_balance < points_amount:
            raise ValidationError("Insufficient points")

        recipient = User.query.filter_by(phone=recipient_phone).first()
        if not recipient:
            raise NotFoundError("Recipient not found")
        if recipient.id == sender_id:
            raise ValidationError("Cannot gift to self")

        return self.gift_points(
            sender_id=sender_id,
            recipient_id=recipient.id,
            points_amount=points_amount,
            message=message,
        )

    def award_points(
        self,
        user_id: int,
        points: int,
        description: str,
        action_type: LoyaltyActionType = LoyaltyActionType.PURCHASE,
        reference_id: int = None,
        expires_at: datetime = None,
        commit: bool = True,
    ) -> LoyaltyTransaction:
        """
        Award loyalty points to user

        Args:
            user_id: User ID
            points: Number of points to award
            description: Description of the transaction
            action_type: Type of loyalty action
            reference_id: Reference to related entity (order, referral, etc.)
            expires_at: When points expire

        Returns:
            LoyaltyTransaction object
        """
        if points <= 0:
            raise ValidationError("Points must be positive")

        account = self.get_or_create_loyalty_account(user_id)

        # Set expiry date if not provided
        if expires_at is None:
            expires_at = datetime.now(timezone.utc) + timedelta(days=self.points_expiry_days)

        # Create transaction
        # Map transaction_type to enum value expected by model
        transaction_type_enum = LoyaltyTransactionType.EARNED
        if action_type == LoyaltyActionType.REFERRAL:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.BIRTHDAY_BONUS:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.WELCOME_BONUS:
            transaction_type_enum = LoyaltyTransactionType.BONUS

        transaction = LoyaltyTransaction(
            user_id=user_id,
            transaction_type=transaction_type_enum,
            points=points,
            description=description,
            order_id=reference_id if action_type == LoyaltyActionType.PURCHASE else None,
            expires_at=expires_at,
            extra_data={"action_type": action_type.value if hasattr(action_type, "value") else action_type},
        )

        db.session.add(transaction)

        # Update account balance
        account.current_balance += points
        account.total_earned += points

        # Check for tier upgrade
        self._check_tier_upgrade(account)

        if commit:
            db.session.commit()
            # Send notification only after a successful commit so a rolled-back
            # award does not push a stale push to the customer.
            self._send_points_notification(user_id, points, "earned")
        else:
            db.session.flush()

        return transaction

    def deduct_points(
        self,
        user_id: int,
        points: int,
        description: str,
        reference_id: int = None,
        skip_notification: bool = True,
        notification_type_str: str = None,
    ) -> LoyaltyTransaction:
        """Deduct loyalty points from user

        Args:
            user_id: User to deduct points from
            points: Number of points to deduct (positive number)
            description: Description of the deduction
            reference_id: Optional reference ID (e.g., order_id)
            skip_notification: If True, don't send points notification (default: True since callers usually handle their own)  # noqa: E501
            notification_type_str: String value of NotificationType enum to use for notification
        """
        if points <= 0:
            raise ValidationError("Points must be positive")

        account = self.get_or_create_loyalty_account(user_id)

        # Check if user has enough points
        available_points = self.get_available_points(user_id)
        if available_points < points:
            raise ValidationError(f"Insufficient points. Available: {available_points}, Required: {points}")

        # Create transaction
        transaction = LoyaltyTransaction(
            user_id=user_id,
            transaction_type=LoyaltyTransactionType.REDEEMED,
            points=-points,  # Negative for deductions
            description=description,
            order_id=reference_id,
        )

        db.session.add(transaction)

        # Update account balance
        account.current_balance -= points
        account.total_redeemed += points

        db.session.commit()

        # Send notification only if explicitly requested
        if not skip_notification:
            self._send_points_notification(user_id, points, "redeemed", notification_type_str)

        return transaction

    def get_user_points(self, user_id: int) -> int:
        """Get user's current points balance"""
        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        return account.current_balance if account else 0

    def get_available_points(self, user_id: int) -> int:
        """Get user's available (non-expired) points"""
        # Remove expired points first
        self._remove_expired_points(user_id)
        return self.get_user_points(user_id)

    def get_loyalty_history(self, user_id: int, page: int = 1, per_page: int = 20) -> Dict[str, Any]:
        """Get user's loyalty transaction history"""
        query = LoyaltyTransaction.query.filter_by(user_id=user_id)
        query = query.order_by(LoyaltyTransaction.created_at.desc())

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        transactions = []
        for transaction in pagination.items:
            extra_data = transaction.extra_data or {}
            transactions.append(
                {
                    "id": transaction.id,
                    "type": (
                        transaction.transaction_type.value
                        if hasattr(transaction.transaction_type, "value")
                        else transaction.transaction_type
                    ),
                    "points": transaction.points,
                    "description": transaction.description,
                    "action_type": extra_data.get("action_type"),
                    "created_at": transaction.created_at.isoformat(),
                    "expires_at": transaction.expires_at.isoformat() if transaction.expires_at else None,
                    "is_expired": transaction.is_expired if hasattr(transaction, "is_expired") else False,
                }
            )

        return {
            "transactions": transactions,
            "total": pagination.total,
            "pages": pagination.pages,
            "current_page": page,
            "per_page": per_page,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }

    def process_referral(self, referrer_code: str, referee_user_id: int) -> Dict[str, Any]:
        """
        Process referral when new user signs up

        Args:
            referrer_code: Referral code of the referring user
            referee_user_id: ID of the new user being referred

        Returns:
            Dictionary with referral processing results
        """
        # Find referrer
        referrer_account = LoyaltyPoints.query.filter_by(referral_code=referrer_code).first()
        if not referrer_account:
            raise ValidationError("Invalid referral code")

        # Check if referee already used a referral
        referee = User.query.get(referee_user_id)
        if not referee:
            raise NotFoundError("Referee user not found")

        if referee.referred_by:
            raise ConflictError("User has already used a referral code")

        # Cannot refer yourself
        if referrer_account.user_id == referee_user_id:
            raise ValidationError("Cannot refer yourself")

        # Create referral record
        referral = ReferralProgram(
            referrer_id=referrer_account.user_id,
            referee_id=referee_user_id,
            referral_code=referrer_code,
            status="completed",
            completed_at=datetime.now(timezone.utc),
        )

        db.session.add(referral)

        # Update referee record
        referee.referred_by = referrer_account.user_id

        # Award points to referrer
        referrer_points = self.referral_bonus
        self.award_points(
            referrer_account.user_id,
            referrer_points,
            f"Referral bonus for {referee.first_name} {referee.last_name}",
            LoyaltyActionType.REFERRAL,
            referral.id,
        )

        # Award points to referee
        referee_points = self.referral_bonus // 2  # Half points for referee
        self.award_points(
            referee_user_id, referee_points, "Referral signup bonus", LoyaltyActionType.REFERRAL, referral.id
        )

        db.session.commit()

        return {"referrer_points": referrer_points, "referee_points": referee_points, "referral_id": referral.id}

    def create_reward(
        self,
        name: str,
        description: str,
        points_required: int,
        reward_type: str = "discount",
        reward_value: int = 0,
        is_active: bool = True,
        expires_at: datetime = None,
    ) -> LoyaltyReward:
        """Create a new loyalty reward"""
        reward = LoyaltyReward(
            name=name,
            description=description,
            points_required=points_required,
            reward_type=reward_type,
            reward_value=reward_value,
            is_active=is_active,
            expires_at=expires_at,
        )

        db.session.add(reward)
        db.session.commit()

        return reward

    def redeem_reward(
        self, user_id: int, reward_id: int, delivery_address_id: int = None, notes: str = None
    ) -> Dict[str, Any]:
        """Redeem a loyalty reward"""
        reward = LoyaltyReward.query.get(reward_id)
        if not reward or not reward.is_active:
            raise NotFoundError("Reward not found or inactive")

        # System rewards cannot be manually redeemed - they are applied automatically
        if reward.is_system_reward:
            raise ValidationError("This reward is automatically applied by the system and cannot be manually redeemed")

        # Check expiry - normalize naive datetimes to UTC for safe comparison.
        valid_until = reward.valid_until
        if valid_until and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)

        if valid_until and valid_until < datetime.now(timezone.utc):
            raise ValidationError("Reward has expired")

        # Get points cost (actual model field name)
        points_cost = reward.points_cost or 0

        # Check if user has enough points
        available_points = self.get_available_points(user_id)
        if available_points < points_cost:
            raise ValidationError(f"Insufficient points. Required: {points_cost}, Available: {available_points}")

        # Deduct points (skip notification - the API sends reward_redeemed notification separately)
        transaction = self.deduct_points(
            user_id,
            points_cost,
            f"Redeemed reward: {reward.name}",
            reward.id,
            skip_notification=True,  # API handles the proper reward_redeemed notification
        )

        # Increment redemption count
        reward.redemptions_used = (reward.redemptions_used or 0) + 1
        db.session.commit()

        # Generate reward code/voucher
        reward_code = self._generate_reward_code(reward, user_id)

        # Return redemption data as a dictionary
        return {
            "id": transaction.id,
            "reward_id": reward.id,
            "reward_name": reward.name,
            "points_spent": points_cost,
            "status": "pending",
            "redemption_code": reward_code,
            "expires_at": valid_until.isoformat() if valid_until else None,
            "transaction_id": transaction.id,
        }

    def get_available_rewards(self, user_id: int) -> List[Dict[str, Any]]:
        """Get rewards available to user"""
        user_points = self.get_available_points(user_id)

        rewards = LoyaltyReward.query.filter(
            LoyaltyReward.is_active == True,
            and_(LoyaltyReward.expires_at.is_(None), LoyaltyReward.expires_at > datetime.now(timezone.utc)),
        ).all()

        available_rewards = []
        for reward in rewards:
            available_rewards.append(
                {
                    "id": reward.id,
                    "name": reward.name,
                    "description": reward.description,
                    "points_required": reward.points_required,
                    "reward_type": reward.reward_type,
                    "reward_value": reward.reward_value,
                    "can_redeem": user_points >= reward.points_required,
                    "expires_at": reward.expires_at.isoformat() if reward.expires_at else None,
                }
            )

        return available_rewards

    def validate_discount_code(self, discount_code: str, user_id: int) -> int:
        """Validate and return discount amount for a code"""
        # This could be a loyalty reward code or other discount code
        # Implementation depends on your discount code system
        return 0  # Placeholder

    def get_user_tier_info(self, user_id: int) -> Dict[str, Any]:
        """Get user's tier information and benefits"""
        account = self.get_or_create_loyalty_account(user_id)

        tier_benefits = self._get_tier_benefits(account.current_tier)
        next_tier_info = self._get_next_tier_info(account)
        requalification = self.get_requalification_info(user_id)

        return {
            "current_tier": account.current_tier,
            "tier_valid_until": account.tier_valid_until.isoformat() if account.tier_valid_until else None,
            "points_balance": account.current_balance,
            "lifetime_points_earned": account.total_earned,
            "tier_benefits": tier_benefits,
            "next_tier": next_tier_info,
            "requalification": requalification,
            "streak": {
                "current_streak": account.current_streak,
                "orders_this_month": account.streak_orders_this_month,  # Tracking internal or display
            },
            "referral_code": getattr(account, "referral_code", f"REF{user_id}"),
            "referrals_count": self._get_referrals_count(user_id),
        }

    def get_loyalty_analytics(self, start_date: datetime = None, end_date: datetime = None) -> Dict[str, Any]:
        """Get loyalty program analytics"""
        query = LoyaltyTransaction.query

        if start_date:
            query = query.filter(LoyaltyTransaction.created_at >= start_date)
        if end_date:
            query = query.filter(LoyaltyTransaction.created_at <= end_date)

        transactions = query.all()

        # Calculate metrics - positive points are awarded, negative are redeemed
        total_points_awarded = sum(t.points for t in transactions if t.points > 0)
        total_points_redeemed = abs(sum(t.points for t in transactions if t.points < 0))

        # Active users
        active_accounts = LoyaltyPoints.query.count()

        # Tier distribution
        tier_distribution = (
            db.session.query(LoyaltyPoints.current_tier, func.count(LoyaltyPoints.id))
            .group_by(LoyaltyPoints.current_tier)
            .all()
        )

        # Referral metrics
        try:
            referrals = ReferralProgram.query.filter_by(status="completed")
            if start_date:
                referrals = referrals.filter(ReferralProgram.completed_at >= start_date)
            if end_date:
                referrals = referrals.filter(ReferralProgram.completed_at <= end_date)

            referral_count = referrals.count()
        except Exception:
            # Database schema mismatch or table doesn't exist
            db.session.rollback()
            referral_count = 0

        return {
            "total_points_awarded": total_points_awarded,
            "total_points_redeemed": total_points_redeemed,
            "net_points_issued": total_points_awarded - total_points_redeemed,
            "active_loyalty_members": active_accounts,
            "referrals_completed": referral_count,
            "tier_distribution": dict(tier_distribution),
            "redemption_rate": (total_points_redeemed / total_points_awarded) * 100 if total_points_awarded > 0 else 0,
        }

    def expire_points(self) -> Dict[str, int]:
        """Expire old loyalty points (called by scheduled task)"""
        # Find earned/bonus points that have expired
        expired_transactions = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.transaction_type.in_([LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS]),
            LoyaltyTransaction.expires_at < datetime.now(timezone.utc),
            LoyaltyTransaction.is_expired == False,
        ).all()

        total_expired_points = 0
        users_affected = set()

        for transaction in expired_transactions:
            # Mark transaction as expired
            transaction.is_expired = True

            # Create expiry transaction
            expiry_transaction = LoyaltyTransaction(
                user_id=transaction.user_id,
                transaction_type=LoyaltyTransactionType.EXPIRED,
                points=-transaction.points,  # Negative for deductions
                description=f"Points expired from transaction #{transaction.id}",
                extra_data={"original_transaction_id": transaction.id},
            )

            db.session.add(expiry_transaction)

            # Update user's balance
            account = LoyaltyPoints.query.filter_by(user_id=transaction.user_id).first()
            if account:
                account.current_balance -= transaction.points
                account.updated_at = datetime.now(timezone.utc)

            total_expired_points += transaction.points
            users_affected.add(transaction.user_id)

        db.session.commit()

        # Send notifications to affected users
        for user_id in users_affected:
            self._send_points_expiry_notification(user_id)

        return {"total_expired_points": total_expired_points, "affected_users": len(users_affected)}

    # Private helper methods
    def _remove_expired_points(self, user_id: int):
        """Remove expired points for a specific user"""
        expired_transactions = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.transaction_type.in_([LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS]),
            LoyaltyTransaction.expires_at < datetime.now(timezone.utc),
            LoyaltyTransaction.is_expired == False,
        ).all()

        total_expired = 0
        for transaction in expired_transactions:
            transaction.is_expired = True
            total_expired += transaction.points

        if total_expired > 0:
            # Update account balance
            account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
            if account:
                account.current_balance -= total_expired

            db.session.commit()

    def calculate_qualifying_points(self, user_id: int) -> int:
        """Calculate qualifying points earned in the last 180 days"""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=180)

        result = (
            db.session.query(func.sum(LoyaltyTransaction.points))
            .filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.transaction_type
                != LoyaltyTransactionType.BONUS,  # Only earned points count for tier
                LoyaltyTransaction.created_at >= cutoff_date,
            )
            .scalar()
        )

        return result or 0

    def _check_tier_upgrade(self, account: LoyaltyPoints):
        """
        Check if user qualifies for tier upgrade or needs downgrade update.
        Logic:
        1. Calculate Qualifying Points (Earned in last 180 days).
        2. Determine target tier based on Qualifying Points.
        3. Implementation of Rules:
           - Upgrade: Immediate. Lock for 180 days.
           - Downgrade: Only IF tier_valid_until < Now AND Qualifying Points < Current Tier Threshold.
        """
        qualifying_points = self.calculate_qualifying_points(account.user_id)
        current_tier_name = account.current_tier

        # Get tier configs
        current_tier_config = LoyaltyTierConfig.query.filter_by(
            name=current_tier_name, program_id=account.program_id, is_active=True
        ).first()

        # Use centralized tier determination
        target_tier_config = LoyaltyTierConfig.get_tier_for_points(qualifying_points, account.program_id)

        # Default to Bronze logic if tiers missing
        if not target_tier_config:
            # Fallback if no tiers configured
            return

        target_tier_name = target_tier_config.name

        # If current tier relies on non-existent config (e.g. data mismatch), treat as lowest
        current_weight = current_tier_config.display_order if current_tier_config else -1
        target_weight = target_tier_config.display_order

        now = datetime.now(timezone.utc)

        # CASE 1: Upgrade
        if target_weight > current_weight:
            account.current_tier = target_tier_name
            account.tier_valid_until = now + timedelta(days=180)  # Lock for 180 days

            # Update points to next tier
            self._update_points_to_next_tier(account, target_tier_config)

            self._send_tier_upgrade_notification(account.user_id, target_tier_name)

        # CASE 2: Downgrade Check
        # Only downgrade if lock expired AND qualifying points are insufficient
        elif target_weight < current_weight:
            if not account.tier_valid_until or account.tier_valid_until < now:
                # Lock expired, and points support lower tier -> Downgrade
                account.current_tier = target_tier_name
                account.tier_valid_until = None

                # Recalculate next tier target
                self._update_points_to_next_tier(account, target_tier_config)

        # CASE 3: Same tier - still need to update points_to_next_tier
        # as user's qualifying_points may have changed
        else:
            self._update_points_to_next_tier(account, target_tier_config)

    def _update_points_to_next_tier(self, account: LoyaltyPoints, current_tier_config: LoyaltyTierConfig):
        """Helper to recalculate points needed for next level"""
        # Find next tier by display order
        next_tier = (
            LoyaltyTierConfig.query.filter(
                LoyaltyTierConfig.program_id == account.program_id,
                LoyaltyTierConfig.is_active == True,
                LoyaltyTierConfig.display_order > current_tier_config.display_order,
            )
            .order_by(LoyaltyTierConfig.display_order.asc())
            .first()
        )

        if next_tier:
            qualifying_points = self.calculate_qualifying_points(account.user_id)
            account.points_to_next_tier = max(0, next_tier.min_points - qualifying_points)
        else:
            account.points_to_next_tier = 0

    def check_tier_expiration(self, user_id: int):
        """Public method to trigger tier expiration check manually or via cron"""
        account = self.get_or_create_loyalty_account(user_id)
        self._check_tier_upgrade(account)
        db.session.commit()

    def get_requalification_info(self, user_id: int) -> Dict[str, Any]:
        """
        Get info about what user needs to do to keep their tier.
        Returns: { 'tier': Str, 'valid_until': Str, 'qualifying_points': Int, 'points_needed_to_keep': Int }
        """
        account = self.get_or_create_loyalty_account(user_id)
        qualifying_points = self.calculate_qualifying_points(user_id)

        # Get config from DB
        current_tier_config = LoyaltyTierConfig.query.filter_by(
            name=account.current_tier, program_id=account.program_id
        ).first()

        min_points_to_keep = current_tier_config.min_points if current_tier_config else 0

        points_needed = max(0, min_points_to_keep - qualifying_points)

        return {
            "tier": account.current_tier,
            "valid_until": account.tier_valid_until,  # DateTime object
            "qualifying_points": qualifying_points,
            "points_needed_to_keep": points_needed,
        }

    def update_streak(self, user_id: int, commit: bool = True):
        """
        Updates streak for a user.
        Logic:
        - Streaks are monthly based (e.g. 3 orders in 30 days logic, but user simplified to "3 orders in 30 days" earlier).  # noqa: E501
        - Let's implement: count orders in last 30 days. If >= 3, +1 Streak Count (if not already incremented this period).  # noqa: E501
        - OR simplified from planning: "3 orders in 30 days → +300 pts".
        - "6 consecutive months → Free 10L bottle".

        Implementation:
        - Check orders in last 30 days.
        - If >= 3 AND streak_orders_this_month < 3 (tracking flag):
             - Award +300 pts "Streak Bonus: 3 Orders in 30 days"
             - Increment current_streak (months)
             - Reset streak_orders_this_month = 3 (marker)
        """
        account = self.get_or_create_loyalty_account(user_id)
        now = datetime.now(timezone.utc)

        # Calculate orders in last 30 days
        thirty_days_ago = now - timedelta(days=30)
        recent_orders_count = Order.query.filter(
            Order.user_id == user_id, Order.status == "delivered", Order.created_at >= thirty_days_ago
        ).count()

        # Logic for "3 orders in 30 days" bonus
        # We need to ensure we don't award this multiple times for the same sliding window excessively.
        # Simplified robust logic:
        # If user hit 3 orders, and we haven't awarded streak bonus recently (e.g. last 30 days).

        # Check last streak reward
        last_streak_tx = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.description == "Streak Bonus: 3 Orders in 30 days",
            LoyaltyTransaction.created_at >= thirty_days_ago,
        ).first()

        if recent_orders_count >= 3 and not last_streak_tx:
            # Award Bonus
            self.award_points(
                user_id, 300, "Streak Bonus: 3 Orders in 30 days", LoyaltyActionType.STREAK_BONUS, commit=commit
            )

            # Update Streak Counter (Consecutive Months Logic is complex without Monthly Job,
            # but we can approximate: increment streak count)
            account.current_streak += 1
            account.last_streak_update = now

            # Check for 6-month Milestone
            if account.current_streak % 6 == 0:
                # "6 consecutive months" -> In this model, every 6th streak increment.
                # Award Free Bottle Reward Coupon or Points equivalent.
                # For simplicity now: Huge Bonus Points equivalent to bottle price (e.g. 15000 UZS -> 60 coins? No, that's small.  # noqa: E501
                # Value of 10L bottle ~? Let's say 500 bonus points or Create a special Reward).
                # User said "Free 10L bottle". We'll award points for it for now to be safe or a voucher.
                self.award_points(
                    user_id, 1000, "6-Month Streak Milestone Bonus", LoyaltyActionType.STREAK_BONUS, commit=commit
                )

            if commit:
                db.session.commit()

    def check_surprise_reward(self, user_id: int, commit: bool = True):
        """
        Randomly award surprise points (5-10% chance).
        """
        import random

        # 5% chance
        if random.random() < 0.05:
            bonus = random.choice([50, 100, 200])  # Small delight
            self.award_points(
                user_id,
                bonus,
                "Surprise Reward! Thanks for being loyal 💙",
                LoyaltyActionType.SURPRICE_REWARD,
                commit=commit,
            )

    def _get_tier_benefits(self, tier_name: str, program_id: int = None) -> Dict[str, Any]:
        """Get benefits for a specific tier using centralized config"""
        tier_config = LoyaltyTierConfig.query.filter_by(name=tier_name, is_active=True)
        if program_id:
            tier_config = tier_config.filter_by(program_id=program_id)

        tier_config = tier_config.first()

        if not tier_config:
            return {"discount_percentage": 0, "points_multiplier": 1.0, "benefits": [], "color": "#CD7F32"}

        return {
            "discount_percentage": tier_config.discount_percentage,
            "points_multiplier": tier_config.points_multiplier,
            "benefits": tier_config.benefits,
            "color": tier_config.color,
        }

    def _get_next_tier_info(self, account: LoyaltyPoints) -> Optional[Dict[str, Any]]:
        """Get information about the next tier using centralized config"""
        # Get current tier first to find its display order
        current_tier = LoyaltyTierConfig.query.filter_by(
            name=account.current_tier, program_id=account.program_id
        ).first()

        current_order = current_tier.display_order if current_tier else -1

        # Find next tier
        next_tier = (
            LoyaltyTierConfig.query.filter(
                LoyaltyTierConfig.program_id == account.program_id,
                LoyaltyTierConfig.is_active == True,
                LoyaltyTierConfig.display_order > current_order,
            )
            .order_by(LoyaltyTierConfig.display_order.asc())
            .first()
        )

        if next_tier:
            points_needed = next_tier.min_points - account.total_earned
            return {"tier": next_tier.name, "points_needed": max(0, points_needed), "threshold": next_tier.min_points}

        return None

    def _get_referrals_count(self, user_id: int) -> int:
        """Get count of successful referrals by user"""
        try:
            return ReferralProgram.query.filter_by(referrer_id=user_id, status="completed").count()
        except Exception:
            # Database schema mismatch or table doesn't exist
            db.session.rollback()
            return 0

    def _generate_reward_code(self, reward: LoyaltyReward, user_id: int) -> str:
        """Generate unique reward code"""
        import hashlib
        import time

        data = f"{reward.id}{user_id}{time.time()}"
        hash_object = hashlib.md5(data.encode())
        return f"RWD{hash_object.hexdigest()[:8].upper()}"

    def _send_points_notification(self, user_id: int, points: int, action: str, notification_type_str: str = None):
        """Send points notification

        Args:
            user_id: User to notify
            points: Number of points
            action: Action type (earned, redeemed, etc.)
            notification_type_str: String value of NotificationType enum to use
        """
        from ..tasks.notification_tasks import send_loyalty_notification_task

        send_loyalty_notification_task.delay(user_id, action, {"points": points}, notification_type_str)

    def _send_tier_upgrade_notification(self, user_id: int, new_tier: str):
        """Send tier upgrade notification"""
        from ..tasks.notification_tasks import send_loyalty_notification_task

        send_loyalty_notification_task.delay(user_id, "tier_upgrade", {"tier": new_tier})

    def _send_points_expiry_notification(self, user_id: int):
        """Send points expiry notification"""
        from ..tasks.notification_tasks import send_loyalty_notification_task

        send_loyalty_notification_task.delay(user_id, "points_expired", {})

    def create_loyalty_account(self, user_id: int) -> LoyaltyPoints:
        """Create a new loyalty account for user (alias for get_or_create_loyalty_account)"""
        return self.get_or_create_loyalty_account(user_id)

    def calculate_tier_progress(self, user_id: int) -> Dict[str, Any]:
        """Calculate tier progress for user"""
        account = self.get_or_create_loyalty_account(user_id)
        next_tier_info = self._get_next_tier_info(account)

        if next_tier_info:
            progress_percentage = max(0, min(100, (account.total_earned / next_tier_info["threshold"]) * 100))
        else:
            progress_percentage = 100  # Already at highest tier

        return {
            "current_tier": account.current_tier,
            "current_points": account.total_earned,
            "next_tier": next_tier_info["tier"] if next_tier_info else None,
            "points_to_next_tier": next_tier_info["points_needed"] if next_tier_info else 0,
            "progress_percentage": progress_percentage,
        }

    def get_reward_categories(self) -> List[str]:
        """Get all available reward categories"""
        categories = db.session.query(LoyaltyReward.reward_type).distinct().all()
        return [category[0] for category in categories if category[0]]

    def can_redeem_reward(self, user_id: int, reward_id: int) -> bool:
        """Check if user can redeem a specific reward"""
        reward = LoyaltyReward.query.get(reward_id)
        if not reward or not reward.is_active:
            return False

        # Check points balance
        available_points = self.get_available_points(user_id)
        if available_points < reward.points_cost:
            return False

        # Check expiry
        if reward.valid_until and reward.valid_until < datetime.now(timezone.utc).date():
            return False

        # Check redemption limits
        if reward.max_redemptions and reward.redemptions_used >= reward.max_redemptions:
            return False

        # Check user-specific usage limit
        if reward.max_uses_per_user:
            user_redemptions = LoyaltyTransaction.query.filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.transaction_type == LoyaltyTransactionType.REDEEMED,
                LoyaltyTransaction.description.contains(f"Redeemed reward: {reward.name}"),
            ).count()

            if user_redemptions >= reward.max_uses_per_user:
                return False

        return True

    def get_action_points(self, action: str) -> int:
        """Get points for specific action"""
        action_points = {
            "referral_signup": 500,
            "social_share": 50,
            "review_submitted": 100,
            "birthday_bonus": 200,
            "survey_completed": 75,
            "app_install": 100,
            "newsletter_signup": 25,
        }
        return action_points.get(action, 0)

    def get_user_referral_code(self, user_id: int) -> str:
        """Get user's referral code"""
        account = self.get_or_create_loyalty_account(user_id)
        return getattr(account, "referral_code", f"REF{user_id}")

    def get_referral_statistics(self, user_id: int) -> Dict[str, Any]:
        """Get referral statistics for user"""
        total_referrals = 0
        pending_referrals = 0
        referral_points = 0

        try:
            total_referrals = ReferralProgram.query.filter_by(referrer_id=user_id, status="completed").count()

            pending_referrals = ReferralProgram.query.filter_by(referrer_id=user_id, status="pending").count()
        except Exception:
            # Database schema mismatch or table doesn't exist
            db.session.rollback()

        # Calculate total points earned from referrals
        # Use a simpler query that doesn't rely on JSON path operators
        try:
            transactions = LoyaltyTransaction.query.filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.transaction_type == LoyaltyTransactionType.BONUS,
                LoyaltyTransaction.description.ilike("%referral%"),
                LoyaltyTransaction.points > 0,
            ).all()

            referral_points = sum(t.points for t in transactions)
        except Exception:
            db.session.rollback()

        return {
            "total_referrals": total_referrals,
            "pending_referrals": pending_referrals,
            "total_points_earned": referral_points,
        }

    def get_referral_points_earned(self, referrer_id: int, referee_id: int) -> int:
        """Get points earned from specific referral"""
        # Find referral program for this referee
        referral = ReferralProgram.query.filter(
            ReferralProgram.referrer_id == referrer_id, ReferralProgram.referee_id == referee_id
        ).first()

        if not referral:
            return 0

        # Find the transaction for this referral (stored in extra_data)
        transaction = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == referrer_id,
            LoyaltyTransaction.extra_data["action_type"].astext == LoyaltyActionType.REFERRAL.value,
            LoyaltyTransaction.description.contains("referral"),
        ).first()

        return transaction.points if transaction else 0

    def get_referrer_bonus_points(self) -> int:
        """Get referrer bonus points amount"""
        return self.referral_bonus

    def get_referee_bonus_points(self) -> int:
        """Get referee bonus points amount"""
        return self.referral_bonus // 2

    def get_tier_history(self, user_id: int) -> List[Dict[str, Any]]:
        """Get user's tier upgrade history"""
        # This would typically come from a tier_history table
        # For now, return current tier info
        account = self.get_or_create_loyalty_account(user_id)
        return [
            {
                "tier": account.current_tier,
                "achieved_at": account.created_at.isoformat(),
                "points_at_upgrade": account.total_earned,
            }
        ]

    def get_user_challenges(self, user_id: int) -> List[Dict[str, Any]]:
        """Get user's current challenges"""
        # Implement challenge system - for now return empty list
        return []

    def get_tier_benefits(self, tier: str) -> Dict[str, Any]:
        """Get benefits for specific tier"""
        return self._get_tier_benefits(tier)

    def get_tier_upgrade_requirements(self, user_id: int) -> Dict[str, Any]:
        """Get tier upgrade requirements for user"""
        account = self.get_or_create_loyalty_account(user_id)
        next_tier_info = self._get_next_tier_info(account)

        if next_tier_info:
            return {
                "current_tier": account.current_tier,
                "next_tier": next_tier_info["tier"],
                "points_needed": next_tier_info["points_needed"],
                "current_points": account.total_earned,
                "target_points": next_tier_info["threshold"],
            }
        else:
            return {
                "current_tier": account.current_tier,
                "next_tier": None,
                "points_needed": 0,
                "current_points": account.total_earned,
                "target_points": None,
                "message": "You have reached the highest tier!",
            }

    def gift_points(
        self, sender_id: int, recipient_id: int, points_amount: int, message: str = ""
    ) -> LoyaltyTransaction:
        """Gift points from one user to another"""
        # Check sender's balance
        sender_points = self.get_available_points(sender_id)
        if sender_points < points_amount:
            raise ValidationError(f"Insufficient points. Available: {sender_points}, Required: {points_amount}")

        # Deduct from sender
        self.deduct_points(sender_id, points_amount, f"Gift to user #{recipient_id}: {message}", recipient_id)

        # Award to recipient
        credit_transaction = self.award_points(
            recipient_id,
            points_amount,
            f"Gift from user #{sender_id}: {message}",
            LoyaltyActionType.WELCOME_BONUS,  # Using this as gift type
            sender_id,
        )

        return credit_transaction

    def process_pending_referrals(self) -> Dict[str, Any]:
        """Process eligible pending referrals and award bonuses."""
        pending_referrals = ReferralProgram.query.filter_by(status="pending").all()
        processed_count = 0
        total_points_awarded = 0

        for referral in pending_referrals:
            if not referral.referee_id:
                continue

            first_order = referral.first_order
            if not first_order:
                first_order = (
                    Order.query.filter_by(user_id=referral.referee_id).order_by(Order.created_at.asc()).first()
                )
                if first_order:
                    referral.first_order_id = first_order.id

            if not first_order:
                continue

            if first_order.status != OrderStatus.DELIVERED:
                continue

            referral.status = "completed"
            referral.completed_at = datetime.now(timezone.utc)

            referrer_points = referral.referrer_bonus_points or self.get_referrer_bonus_points()
            referee_points = referral.referee_bonus_points or self.get_referee_bonus_points()

            self.award_points(
                referral.referrer_id,
                referrer_points,
                f"Referral bonus for user #{referral.referee_id}",
                LoyaltyActionType.REFERRAL,
                referral.first_order_id,
            )
            self.award_points(
                referral.referee_id,
                referee_points,
                "Referral signup bonus",
                LoyaltyActionType.REFERRAL,
                referral.first_order_id,
            )

            referral.referrer_bonus_points = referrer_points
            referral.referee_bonus_points = referee_points
            processed_count += 1
            total_points_awarded += referrer_points + referee_points

        if pending_referrals:
            db.session.commit()

        return {
            "processed_count": processed_count,
            "total_points_awarded": total_points_awarded,
        }

    def get_points_expiring_soon(self, days: int = 7) -> List[Dict[str, Any]]:
        """Return users with positive earned points expiring soon."""
        now = datetime.now(timezone.utc)
        end_window = now + timedelta(days=days)

        rows = (
            db.session.query(
                LoyaltyTransaction.user_id,
                func.sum(LoyaltyTransaction.points),
                func.min(LoyaltyTransaction.expires_at),
            )
            .filter(
                LoyaltyTransaction.transaction_type.in_(
                    [
                        LoyaltyTransactionType.EARNED,
                        LoyaltyTransactionType.BONUS,
                    ]
                ),
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.is_expired.is_(False),
                LoyaltyTransaction.expires_at.isnot(None),
                LoyaltyTransaction.expires_at >= now,
                LoyaltyTransaction.expires_at <= end_window,
            )
            .group_by(
                LoyaltyTransaction.user_id,
            )
            .all()
        )

        return [
            {
                "user_id": user_id,
                "expiring_points": int(expiring_points or 0),
                "expiry_date": expiry_date,
            }
            for user_id, expiring_points, expiry_date in rows
            if expiring_points
        ]

    def update_all_tiers(self) -> Dict[str, List[Dict[str, Any]]]:
        """Recompute all loyalty tiers and report upgrades/downgrades."""
        upgrades: List[Dict[str, Any]] = []
        downgrades: List[Dict[str, Any]] = []

        accounts = LoyaltyPoints.query.all()
        for account in accounts:
            old_tier = account.current_tier
            previous_points_to_next = account.points_to_next_tier
            self._check_tier_upgrade(account)

            if account.current_tier != old_tier:
                target_collection = upgrades
                old_config = LoyaltyTierConfig.query.filter_by(
                    name=old_tier,
                    program_id=account.program_id,
                ).first()
                new_config = LoyaltyTierConfig.query.filter_by(
                    name=account.current_tier,
                    program_id=account.program_id,
                ).first()
                if old_config and new_config and new_config.display_order < old_config.display_order:
                    target_collection = downgrades

                target_collection.append(
                    {
                        "user_id": account.user_id,
                        "old_tier": old_tier,
                        "new_tier": account.current_tier,
                        "benefits": self.get_tier_benefits(account.current_tier).get("benefits", []),
                        "points_needed_for_restore": account.points_to_next_tier or previous_points_to_next or 0,
                    }
                )

        db.session.commit()
        return {
            "upgrades": upgrades,
            "downgrades": downgrades,
        }
