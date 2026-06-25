"""
Loyalty service for the Water Business Platform
Handles loyalty points, rewards, referrals, and customer retention programs
"""

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from flask import current_app
from sqlalchemy import func, and_, or_

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyTransaction,
    LoyaltyReward,
    LoyaltyProgram,
    LoyaltyStreakRule,
    LoyaltyConsecutiveStrikeRule,
    ReferralProgram,
    LoyaltyTierConfig,
)
from business_app.models.user import User
from business_app.models.order import Order
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.translations import get_translation
from business_app.utils.validators import normalize_phone_number
from business_app.utils.constants import (
    LoyaltyActionType,
    LoyaltyTransactionType,
    RewardStatus,
)
from shared.enums import (
    OrderStatus,
    UserType,
)
from business_app import db


class LoyaltyService:
    """Service for managing loyalty programs"""

    # TODO: Note for mylself: correct loyalty points for user_id in (8,9,1, 12,7,13,10,31,22);

    @staticmethod
    def is_user_loyalty_eligible(user) -> bool:
        """Single source of truth: may this user use the loyalty program?

        Individuals and staff are always eligible. Entity (corporate) users are
        eligible only if they hold at least one corporate contract that is
        currently active AND flagged is_loyalty_points_eligible.
        """
        if user is None:
            return False
        if not getattr(user, "is_entity_user", False):
            return True
        return any(c.is_currently_active and c.is_loyalty_points_eligible for c in user.corporate_contracts)

    def __init__(self):
        # All economics are DB-driven via LoyaltyProgram (uzs_per_point,
        # points_expiry_days, signup/referral/birthday bonus). No instance caches
        # and no config fallbacks. Points are spent only on rewards (rewards-only).
        pass

    def _get_default_program(self) -> Optional[LoyaltyProgram]:
        """The authoritative program for program-level economics (bonus amounts)."""
        return (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )

    def _program_bonus(self, field: str, default: int) -> int:
        """Read a bonus amount from the default LoyaltyProgram (single source of truth)."""
        program = self._get_default_program()
        value = getattr(program, field, None) if program else None
        return int(value) if value is not None else default

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

    def get_or_create_loyalty_account(self, user_id: int, commit: bool = True) -> LoyaltyPoints:
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
            if commit:
                db.session.commit()
            else:
                db.session.flush()

        return account

    def grant_welcome_bonus(self, user_id: int) -> int:
        """Grant the one-time welcome (signup) bonus from the DB SSOT.

        Idempotent: a user who already has a WELCOME_BONUS transaction is skipped,
        so it is safe to call from every registration path. Returns points granted
        (0 if none). Kept OUT of get_or_create_loyalty_account so read/GET paths
        never mutate the ledger.
        """
        account = self.get_or_create_loyalty_account(user_id)
        signup_bonus = (account.program.signup_bonus if account.program else 0) or 0
        if signup_bonus <= 0:
            return 0

        already = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.transaction_type == LoyaltyTransactionType.BONUS,
        ).all()
        if any((t.extra_data or {}).get("action_type") == LoyaltyActionType.WELCOME_BONUS.value for t in already):
            return 0

        self.award_points(user_id, signup_bonus, "Welcome bonus", action_type=LoyaltyActionType.WELCOME_BONUS)
        return signup_bonus

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

        # Tier progress is measured against the qualifying-points basis (rolling
        # 365-day EARNED+BONUS), NOT the spendable balance — so redeeming points
        # never appears to drop the customer's tier progress.
        qualifying_points = self.calculate_qualifying_points(user_id)
        if next_tier_config:
            next_tier_points = next_tier_config.min_points
            points_needed = max(0, next_tier_points - qualifying_points)
            current_progress = qualifying_points - current_tier_points
            next_tier_progress_target = next_tier_points - current_tier_points
        else:
            next_tier_points = current_tier_points
            points_needed = 0
            current_progress = qualifying_points
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
            # Live per-rule streak progress — consumed by the customer loyalty page
            # (static/js/pages/loyalty.js fetches /loyalty/account).
            "streak_progress": self.get_streak_progress(user_id),
            "consecutive_strike_progress": self.get_consecutive_strike_progress(user_id),
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
        # Hide misconfigured manually-redeemable rewards (broken discount/free_product)
        # so they are never advertised; any other (legacy/system) reward type is
        # left listed exactly as before — defensive, no such types are creatable now.
        visible = [
            r for r in rewards if r.reward_type not in ("discount", "free_product") or self.is_reward_configured(r)
        ]
        can_redeem_by_id = {r.id: self.can_redeem_reward(user_id, r.id) for r in visible}
        return {
            "rewards": visible,
            "can_redeem_by_id": can_redeem_by_id,
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
            "can_redeem": self.can_redeem_reward(user_id, reward_id),
            "points_needed": max(0, reward.points_cost - user_points),
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

    def get_referral_info_for_user(self, user_id: int) -> Dict[str, Any]:
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

        # Public, shareable signup link built from the COMPANY_WEBSITE config SSOT
        # — not request.host_url, which over the bot's internal Docker call resolves
        # to an empty host (http:///register?ref=...). Bot clients build their own
        # t.me deep link from the code; this web link serves web/admin surfaces.
        site = current_app.config.get("COMPANY_WEBSITE", "https://aqua-element.uz").rstrip("/")

        return {
            "referral_code": referral_code,
            "referral_link": f"{site}/register?ref={referral_code}",
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
                if (
                    txn.transaction_type == LoyaltyTransactionType.EARNED
                    # created_at can be tz-naive when read back from some backends;
                    # normalize to UTC so the aware month-window comparison never raises.
                    and month_start <= ensure_utc(txn.created_at) < month_end
                )
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

        # JWT identities arrive as strings; coerce so the recipient==sender guard
        # below (an int comparison) actually fires and self-gifting is blocked.
        sender_id = int(sender_id)

        sender_account = LoyaltyPoints.query.filter_by(user_id=sender_id).first()
        if not sender_account or sender_account.current_balance < points_amount:
            raise ValidationError("Insufficient points")

        normalized_recipient_phone = normalize_phone_number(recipient_phone)
        if not normalized_recipient_phone:
            raise ValidationError(get_translation("error.validation.invalid_phone"))

        recipient = User.query.filter_by(phone=normalized_recipient_phone).first()
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

    def has_purchase_award(self, order_id: int) -> bool:
        """Whether an order has already earned its purchase AquaCoins.

        Idempotency probe for the (delivered AND fully paid) award path. Only a
        PURCHASE award writes an ``EARNED`` transaction carrying an ``order_id``
        (see ``award_points``); order-edit clawbacks use ``ADJUSTMENT`` and the
        various bonuses use ``BONUS`` with ``order_id=None`` — so an ``EARNED``
        row for this ``order_id`` uniquely marks the initial purchase accrual.
        """
        if not order_id:
            return False
        return bool(
            db.session.query(
                LoyaltyTransaction.query.filter(
                    LoyaltyTransaction.order_id == order_id,
                    LoyaltyTransaction.transaction_type == LoyaltyTransactionType.EARNED,
                ).exists()
            ).scalar()
        )

    def award_points(
        self,
        user_id: int,
        points: int,
        description: str,
        action_type: LoyaltyActionType = LoyaltyActionType.PURCHASE,
        reference_id: int = None,
        expires_at: datetime = None,
        extra_data: dict = None,
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

        account = self.get_or_create_loyalty_account(user_id, commit=commit)

        # Set expiry date if not provided, using the program's configured window
        # (DB SSOT) — falling back to the bootstrap default only if unset.
        if expires_at is None:
            expiry_days = (account.program.points_expiry_days if account.program else None) or 365
            expires_at = datetime.now(timezone.utc) + timedelta(days=expiry_days)

        # Create transaction
        # Map transaction_type to enum value expected by model
        transaction_type_enum = LoyaltyTransactionType.EARNED
        if action_type == LoyaltyActionType.REFERRAL:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.BIRTHDAY_BONUS:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.WELCOME_BONUS:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.SURPRICE_REWARD:
            transaction_type_enum = LoyaltyTransactionType.BONUS
        elif action_type == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS:
            transaction_type_enum = LoyaltyTransactionType.BONUS

        transaction = LoyaltyTransaction(
            user_id=user_id,
            transaction_type=transaction_type_enum,
            points=points,
            description=description,
            order_id=reference_id if action_type == LoyaltyActionType.PURCHASE else None,
            expires_at=expires_at,
            # New FIFO lot: the full award is initially unspent.
            remaining_points=points,
            extra_data={
                "action_type": action_type.value if hasattr(action_type, "value") else action_type,
                **(extra_data or {}),
            },
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

    def reverse_earnings(
        self,
        user_id: int,
        order_id: int,
        old_points_earned: int,
        new_points_earned: int,
        *,
        clamp: bool = True,
        description: Optional[str] = None,
        commit: bool = True,
    ) -> Dict[str, Any]:
        """Adjust a previous order's loyalty earnings after an order edit.

        Called when an admin edits an order whose loyalty points were already
        awarded (e.g. quantity decreased post-delivery). The difference
        between old and new earnings is clawed back from the user's current
        balance.

        ``clamp=True`` (the policy chosen during brainstorming) never lets the
        balance go negative: if the user has already spent some of the
        previously-earned points, we claw back only what's available and
        return the uncollectible delta so the caller can record it in the
        audit log. ``clamp=False`` clawbacks the full diff (may go negative).
        ``diff < 0`` (new earnings higher than old, e.g. quantity increased)
        produces a positive adjustment instead, awarding the extra points.
        Result keys are always present so callers can render a uniform
        cascade summary.
        """
        if old_points_earned < 0 or new_points_earned < 0:
            raise ValidationError("AquaCoins totals must be non-negative")

        account = self.get_or_create_loyalty_account(user_id)
        result: Dict[str, Any] = {
            "diff": old_points_earned - new_points_earned,
            "clawback": 0,
            "uncollectible": 0,
            "award": 0,
            "transaction_id": None,
        }

        diff = old_points_earned - new_points_earned
        if diff == 0:
            return result

        if diff > 0:
            # We need to claw back `diff` points.
            available_balance = max(0, int(account.current_balance or 0))
            if clamp:
                clawback = min(diff, available_balance)
            else:
                clawback = diff
            uncollectible = diff - clawback
            result["clawback"] = clawback
            result["uncollectible"] = uncollectible

            if clawback > 0:
                # Use ADJUSTMENT transaction type with negative points (mirrors
                # the EXPIRED reversal pattern in expire_points()).
                txn = LoyaltyTransaction(
                    user_id=user_id,
                    transaction_type=LoyaltyTransactionType.ADJUSTMENT,
                    points=-clawback,
                    description=description or f"Order #{order_id} edit clawback",
                    order_id=order_id,
                    extra_data={
                        "action_type": "order_edit_reversal",
                        "old_points_earned": old_points_earned,
                        "new_points_earned": new_points_earned,
                        "uncollectible": uncollectible,
                        "clamped": bool(clamp),
                    },
                )
                db.session.add(txn)
                # Draw the clawback down the FIFO lots so the ledger and the
                # cached balance stay consistent (no-op when there are no lots,
                # e.g. legacy accounts seeded without a transaction history).
                self._consume_lots_fifo(user_id, clawback)
                account.current_balance = available_balance - clawback
                # A clawback reverses earning — reduce lifetime total_earned
                # (symmetric with the award branch). total_redeemed stays reserved
                # for actual reward redemptions (M1).
                account.total_earned = max(0, (account.total_earned or 0) - clawback)
                db.session.flush()
                result["transaction_id"] = txn.id
        else:
            # diff < 0: new earnings exceed old, award the extra.
            award = -diff
            # Positive adjustment is a new spendable lot — it MUST carry an
            # expiry (same DB-driven window as awards) or the expiry job can
            # never sweep it and the points leak forever.
            expiry_days = (account.program.points_expiry_days if account.program else None) or 365
            txn = LoyaltyTransaction(
                user_id=user_id,
                transaction_type=LoyaltyTransactionType.ADJUSTMENT,
                points=award,
                description=description or f"Order #{order_id} edit award",
                order_id=order_id,
                remaining_points=award,
                expires_at=datetime.now(timezone.utc) + timedelta(days=expiry_days),
                extra_data={
                    "action_type": "order_edit_award",
                    "old_points_earned": old_points_earned,
                    "new_points_earned": new_points_earned,
                },
            )
            db.session.add(txn)
            account.current_balance = int(account.current_balance or 0) + award
            account.total_earned = (account.total_earned or 0) + award
            db.session.flush()
            result["award"] = award
            result["transaction_id"] = txn.id

        if commit:
            db.session.commit()
        return result

    def deduct_points(
        self,
        user_id: int,
        points: int,
        description: str,
        reference_id: int = None,
        skip_notification: bool = True,
        notification_type_str: str = None,
        commit: bool = True,
    ) -> LoyaltyTransaction:
        """Deduct loyalty points from user

        Args:
            user_id: User to deduct points from
            points: Number of points to deduct (positive number)
            description: Description of the deduction
            reference_id: Optional reference ID (e.g., order_id)
            skip_notification: If True, don't send points notification (default: True since callers usually handle their own)  # noqa: E501
            notification_type_str: String value of NotificationType enum to use for notification
            commit: If True, commit the transaction; if False, flush only so the
                caller controls the enclosing transaction (e.g. apply_reward_to_order).
                A notification is only ever sent on an actual commit.
        """
        if points <= 0:
            raise ValidationError("Points must be positive")

        account = self.get_or_create_loyalty_account(user_id, commit=commit)

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

        # Draw the spent points down the FIFO lots (oldest first) so expiry
        # never re-counts points the user already spent.
        self._consume_lots_fifo(user_id, points)

        # Update account balance cache
        account.current_balance -= points
        account.total_redeemed += points

        if commit:
            db.session.commit()
        else:
            db.session.flush()

        # Send notification only on an actual commit (and only if requested), so a
        # caller-controlled transaction that may still roll back never pushes a
        # premature notification to the customer.
        if commit and not skip_notification:
            self._send_points_notification(user_id, points, "redeemed", notification_type_str)

        return transaction

    def _consume_lots_fifo(self, user_id: int, amount: int, now: datetime = None) -> int:
        """Draw ``amount`` points down the user's live earn lots, oldest first.

        A "live" lot is a positive transaction that is not expired and whose
        expiry (if any) is still in the future. Returns the amount actually
        consumed (may be less than ``amount`` only for inconsistent/legacy data).
        """
        if amount <= 0:
            return 0
        now = now or datetime.now(timezone.utc)

        lots = (
            LoyaltyTransaction.query.filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.is_expired == False,  # noqa: E712
                or_(LoyaltyTransaction.expires_at.is_(None), LoyaltyTransaction.expires_at > now),
            )
            .order_by(LoyaltyTransaction.created_at.asc(), LoyaltyTransaction.id.asc())
            .all()
        )

        to_consume = amount
        for lot in lots:
            if to_consume <= 0:
                break
            lot_remaining = lot.remaining_points if lot.remaining_points is not None else lot.points
            if lot_remaining <= 0:
                continue
            take = min(lot_remaining, to_consume)
            lot.remaining_points = lot_remaining - take
            to_consume -= take

        return amount - to_consume

    def get_user_points(self, user_id: int) -> int:
        """Get user's current points balance"""
        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        return account.current_balance if account else 0

    def get_available_points(self, user_id: int) -> int:
        """Get user's available (spendable) points.

        Pure read: derived directly from the FIFO ledger — the sum of unspent
        remainders of lots that are neither flagged expired nor past their
        expiry date. Performs NO writes, so it is safe to call from read paths
        and cannot race the scheduled expiry job.
        """
        now = datetime.now(timezone.utc)
        total = (
            db.session.query(func.sum(func.coalesce(LoyaltyTransaction.remaining_points, LoyaltyTransaction.points)))
            .filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.is_expired == False,  # noqa: E712
                or_(LoyaltyTransaction.expires_at.is_(None), LoyaltyTransaction.expires_at > now),
            )
            .scalar()
        )
        return int(total or 0)

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
        # Resolve referrer by the persisted User.referral_code (single source of truth).
        referrer = User.query.filter_by(referral_code=referrer_code).first()
        if not referrer:
            raise ValidationError("Invalid referral code")

        referee = User.query.get(referee_user_id)
        if not referee:
            raise NotFoundError("Referee user not found")

        if referrer.id == referee_user_id:
            raise ValidationError("Cannot refer yourself")

        if referee.referred_by_user_id:
            raise ConflictError("User has already used a referral code")

        # Snapshot the bonus amounts (DB SSOT) onto the row at creation, so a later
        # program change can't retroactively alter an in-flight referral's payout.
        referrer_points = self.get_referrer_bonus_points()
        referee_points = self.get_referee_bonus_points()

        # Create the referral as PENDING. Bonuses are granted by
        # process_pending_referrals once the referee's first order is both
        # delivered and fully paid.
        referral = ReferralProgram(
            referrer_id=referrer.id,
            referee_id=referee_user_id,
            referral_code=referrer_code,
            status="pending",
            referrer_bonus_points=referrer_points,
            referee_bonus_points=referee_points,
        )
        db.session.add(referral)
        referee.referred_by_user_id = referrer.id
        db.session.commit()

        return {
            "referral_id": referral.id,
            "status": "pending",
            "referrer_points": referrer_points,
            "referee_points": referee_points,
        }

    def _generate_unique_referral_code(self) -> str:
        """Generate a short, unique, shareable referral code."""
        from business_app.utils.helpers import generate_random_string

        for _ in range(10):
            code = f"REF{generate_random_string(6).upper()}"
            if not User.query.filter_by(referral_code=code).first():
                return code
        # Extremely unlikely fallback: longer code.
        return f"REF{generate_random_string(10).upper()}"

    def _compute_reward_discount(self, reward, subtotal):
        """UZS discount for a discount reward, capped at subtotal.

        ``subtotal`` is already a Decimal supplied by the caller.
        """
        from decimal import Decimal, ROUND_HALF_UP

        value = reward.discount_value or Decimal("0")
        if (reward.discount_type or "fixed") == "percentage":
            discount = subtotal * Decimal(str(value)) / Decimal("100")
        else:
            discount = Decimal(str(value))
        discount = discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return min(discount, subtotal)

    def apply_reward_to_order(self, order, reward_id, *, commit=True):
        """Atomically redeem a reward and apply its benefit to an order."""
        from decimal import Decimal
        from business_app.models.loyalty import RewardRedemption
        from business_app.models.order import OrderItem

        reward = LoyaltyReward.query.with_for_update().get(reward_id)
        if not reward or not reward.is_active:
            raise NotFoundError("Reward not found or inactive")
        if reward.is_system_reward:
            raise ValidationError("This reward is applied automatically and cannot be redeemed")
        if reward.reward_type not in ("discount", "free_product"):
            raise ValidationError(f"Unsupported reward type: {reward.reward_type}")

        now = datetime.now(timezone.utc)
        valid_until = reward.valid_until
        if valid_until and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        valid_from = reward.valid_from
        if valid_from and valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_from and now < valid_from:
            raise ValidationError("Reward is not yet available")
        if valid_until and now > valid_until:
            raise ValidationError("Reward has expired")

        subtotal = Decimal(str(order.subtotal or 0))
        if reward.min_order_value and subtotal < Decimal(str(reward.min_order_value)):
            raise ValidationError("Order does not meet the minimum value for this reward")

        points_cost = reward.points_cost or 0
        if self.get_available_points(order.user_id) < points_cost:
            raise ValidationError(f"Insufficient points. Required: {points_cost}")

        # Only one reward may be applied per order. Checked before the per-user /
        # max-redemptions limits so a repeat application on the same order reports
        # the precise reason rather than a generic limit message.
        if RewardRedemption.query.filter_by(order_id=order.id, status="applied").first():
            raise ValidationError("A reward has already been applied to this order")

        if reward.max_uses_per_user:
            used = RewardRedemption.query.filter_by(
                reward_id=reward.id, user_id=order.user_id, status="applied"
            ).count()
            if used >= reward.max_uses_per_user:
                raise ValidationError("Per-user redemption limit reached")
        if reward.max_redemptions and (reward.redemptions_used or 0) >= reward.max_redemptions:
            raise ValidationError("Reward redemption limit reached")

        discount_amount = None
        free_product_id = None
        if reward.reward_type == "discount":
            discount_amount = self._compute_reward_discount(reward, subtotal)
            order.loyalty_discount = discount_amount
            order.total_amount = max(
                Decimal("0.00"),
                subtotal
                - Decimal(str(order.discount_amount or 0))
                - discount_amount
                + Decimal(str(order.delivery_fee or 0)),
            )
        else:  # free_product
            if not self.is_reward_configured(reward):
                raise ValidationError("Reward is not available")
            free_product_id = reward.free_product_id
            order.order_items.append(
                OrderItem(
                    order_id=order.id,
                    product_id=free_product_id,
                    quantity=reward.free_product_quantity or 1,
                    unit_price=Decimal("0.00"),
                    total_price=Decimal("0.00"),
                    is_reward_item=True,
                )
            )

        if points_cost > 0:
            # commit=False keeps every mutation in the single transaction this
            # method (and its callers) controls; flush only, no notification.
            self.deduct_points(
                order.user_id,
                points_cost,
                f"Redeemed reward: {reward.name}",
                order.id,
                skip_notification=True,
                commit=False,
            )

        redemption = RewardRedemption(
            reward_id=reward.id,
            user_id=order.user_id,
            order_id=order.id,
            reward_type=reward.reward_type,
            points_spent=points_cost,
            discount_amount=discount_amount,
            free_product_id=free_product_id,
            code=self._generate_reward_code(reward, order.user_id),
            status="applied",
        )
        db.session.add(redemption)
        reward.redemptions_used = (reward.redemptions_used or 0) + 1

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return redemption

    def cancel_redemption_for_order(self, order_id, *, commit=True):
        """Reverse an applied reward redemption when its order is cancelled:
        refund the spent points (as a non-tier-qualifying ADJUSTMENT credit lot),
        flip status to cancelled, and decrement the reward's usage counter.
        No-op if there is no applied redemption. Idempotent."""
        from business_app.models.loyalty import RewardRedemption, LoyaltyTransaction

        redemption = RewardRedemption.query.with_for_update().filter_by(order_id=order_id, status="applied").first()
        if not redemption:
            return None

        refund = redemption.points_spent or 0
        if refund > 0:
            account = self.get_or_create_loyalty_account(redemption.user_id, commit=False)
            expiry_days = (account.program.points_expiry_days if account.program else None) or 365
            txn = LoyaltyTransaction(
                user_id=redemption.user_id,
                transaction_type=LoyaltyTransactionType.ADJUSTMENT,
                points=refund,
                remaining_points=refund,
                description=f"Refund of redeemed reward (order #{order_id})",
                order_id=order_id,
                expires_at=datetime.now(timezone.utc) + timedelta(days=expiry_days),
                extra_data={"action_type": "reward_refund"},
            )
            db.session.add(txn)
            # Reverse the deduction's bookkeeping: restore spendable balance and
            # un-redeem the lifetime counter. Do NOT touch total_earned (a refund
            # is not earning). ADJUSTMENT type keeps it out of tier qualification.
            account.current_balance = (account.current_balance or 0) + refund
            account.total_redeemed = max(0, (account.total_redeemed or 0) - refund)

        redemption.status = "cancelled"
        reward = LoyaltyReward.query.with_for_update().get(redemption.reward_id)
        if reward:
            reward.redemptions_used = max(0, (reward.redemptions_used or 0) - 1)

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return redemption

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
            "referral_code": self.get_user_referral_code(user_id),
            "referrals_count": self._get_referrals_count(user_id),
            "streak_progress": self.get_streak_progress(user_id),
            "consecutive_strike_progress": self.get_consecutive_strike_progress(user_id),
        }

    # NOTE: LoyaltyService.get_loyalty_analytics was removed (loyalty SSOT, Phase 2).
    # It was an orphan duplicate; AdminLoyaltyService.get_analytics is the single
    # wired analytics source (and applies correct transaction-type filtering).

    def expire_points(self) -> Dict[str, int]:
        """Expire old loyalty points (called by the scheduled daily task).

        Iterates affected users and commits PER USER so a failure (or the
        non-negative balance CHECK constraint) for one user can never roll back
        the whole run for everyone. Expiry is represented solely by the lot's
        ``is_expired`` flag — no synthetic negative EXPIRED transaction — so the
        ledger has exactly one representation per event.
        """
        now = datetime.now(timezone.utc)

        user_ids = [
            row[0]
            for row in db.session.query(LoyaltyTransaction.user_id)
            .filter(
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.is_expired == False,  # noqa: E712
                LoyaltyTransaction.expires_at.isnot(None),
                LoyaltyTransaction.expires_at <= now,
            )
            .distinct()
            .all()
        ]

        total_expired_points = 0
        affected_users = []

        for user_id in user_ids:
            try:
                expired = self._expire_user_points(user_id, now=now)
                db.session.commit()
                if expired > 0:
                    total_expired_points += expired
                    affected_users.append(user_id)
            except Exception as exc:  # one user's failure must not abort the batch
                db.session.rollback()
                current_app.logger.error(f"Failed to expire loyalty points for user {user_id}: {exc}")

        # Notify only users who actually lost points (after successful commits).
        # Guarded so a single failed enqueue (e.g. broker outage) cannot drop the
        # remaining already-committed users' notifications.
        for user_id in affected_users:
            try:
                self._send_points_expiry_notification(user_id)
            except Exception as exc:
                current_app.logger.error(f"Failed to enqueue loyalty expiry notification for user {user_id}: {exc}")

        return {"total_expired_points": total_expired_points, "affected_users": len(affected_users)}

    def _expire_user_points(self, user_id: int, now: datetime = None) -> int:
        """Expire a single user's lapsed lots. Does NOT commit (caller controls).

        For each positive lot past its expiry date and not yet flagged:
          * flag ``is_expired`` (closes the lot, prevents re-querying),
          * add only its UNSPENT remainder to the expired total (a lot already
            spent via FIFO has nothing left to expire — no double-counting),
          * zero its ``remaining_points``.
        Decrements the cached balance (floored at 0), bumps ``total_expired``,
        and stamps ``last_expiry_check``. Returns the points actually expired.
        """
        now = now or datetime.now(timezone.utc)

        lots = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.points > 0,
            LoyaltyTransaction.is_expired == False,  # noqa: E712
            LoyaltyTransaction.expires_at.isnot(None),
            LoyaltyTransaction.expires_at <= now,
        ).all()

        if not lots:
            return 0

        total_expired = 0
        for lot in lots:
            lot_remaining = lot.remaining_points if lot.remaining_points is not None else lot.points
            lot.is_expired = True
            if lot_remaining > 0:
                total_expired += lot_remaining
                lot.remaining_points = 0

        account = LoyaltyPoints.query.filter_by(user_id=user_id).first()
        if account:
            if total_expired > 0:
                account.current_balance = max(0, (account.current_balance or 0) - total_expired)
                account.total_expired = (account.total_expired or 0) + total_expired
            account.last_expiry_check = now
            account.updated_at = now

        return total_expired

    # Private helper methods
    def _remove_expired_points(self, user_id: int):
        """Backward-compatible per-user expiry sweep (delegates to the SSOT helper).

        Commits only when points were actually expired, so a no-op sweep never
        flushes unrelated pending session work.
        """
        if self._expire_user_points(user_id) > 0:
            db.session.commit()

    # Tier qualification window: 1 year (rolling). Owner decision 2026-06-14.
    TIER_QUALIFYING_WINDOW_DAYS = 365

    def calculate_qualifying_points(self, user_id: int) -> int:
        """Points that count toward tier qualification.

        Single tier basis (owner decision 2026-06-14): all EARNED + BONUS points
        credited in the trailing 365 days. Bonuses DO count; refund/clawback
        ADJUSTMENT rows and EXPIRED rows do not.
        """
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.TIER_QUALIFYING_WINDOW_DAYS)

        result = (
            db.session.query(func.sum(LoyaltyTransaction.points))
            .filter(
                LoyaltyTransaction.user_id == user_id,
                LoyaltyTransaction.points > 0,
                LoyaltyTransaction.transaction_type.in_([LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS]),
                LoyaltyTransaction.created_at >= cutoff_date,
            )
            .scalar()
        )

        return result or 0

    def _check_tier_upgrade(self, account: LoyaltyPoints):
        """
        Check if user qualifies for tier upgrade or needs downgrade update.
        Logic:
        1. Calculate Qualifying Points (rolling 365-day EARNED+BONUS window).
        2. Determine target tier based on Qualifying Points.
        3. Implementation of Rules:
           - Upgrade: Immediate. Lock for 365 days.
           - Requalify (same tier): refresh the 365-day lock so a continuously
             active customer is never downgraded mid-window.
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

        lock_until = now + timedelta(days=self.TIER_QUALIFYING_WINDOW_DAYS)

        # CASE 1: Upgrade
        if target_weight > current_weight:
            account.current_tier = target_tier_name
            account.tier_valid_until = lock_until  # Lock for 365 days

            # Update points to next tier
            self._update_points_to_next_tier(account, target_tier_config)

            self._send_tier_upgrade_notification(account.user_id, target_tier_name)

        # CASE 2: Downgrade Check
        # Only downgrade if lock expired AND qualifying points are insufficient.
        # ensure_utc guards against a tz-naive tier_valid_until (some backends drop
        # tzinfo on read) so this comparison never raises offset-naive/aware errors.
        elif target_weight < current_weight:
            if not account.tier_valid_until or ensure_utc(account.tier_valid_until) < now:
                # Lock expired, and points support lower tier -> Downgrade
                account.current_tier = target_tier_name
                account.tier_valid_until = None

                # Recalculate next tier target
                self._update_points_to_next_tier(account, target_tier_config)

        # CASE 3: Same tier - the user still qualifies, so refresh the lock and
        # recompute points_to_next_tier (qualifying_points may have changed).
        else:
            account.tier_valid_until = lock_until
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

    def _qualifying_order_count(self, user_id: int, rule, now: datetime) -> int:
        """Delivered orders for ``user_id`` within the rule's trailing window,
        counting only orders whose total meets ``min_order_amount`` when set."""
        window_start = now - timedelta(days=rule.window_days)
        q = Order.query.filter(
            Order.user_id == user_id,
            Order.status == OrderStatus.DELIVERED,
            Order.created_at >= window_start,
        )
        if rule.min_order_amount is not None:
            q = q.filter(Order.total_amount >= rule.min_order_amount)
        return q.count()

    def get_streak_progress(self, user_id: int):
        """Live progress toward each active, currently-effective streak rule for the
        default program. Computed from recent orders — no stored counter."""
        from business_app.utils.helpers import get_current_language

        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program:
            return []
        now = datetime.now(timezone.utc)
        lang = get_current_language()
        out = []
        rules = (
            LoyaltyStreakRule.query.filter_by(program_id=program.id, is_active=True)
            .order_by(LoyaltyStreakRule.display_order.asc())
            .all()
        )
        for rule in rules:
            if not rule.is_effective(now):
                continue
            count = self._qualifying_order_count(user_id, rule, now)
            out.append(
                {
                    "name": rule.get_translated("name", lang),
                    "required_orders": rule.required_orders,
                    "current_orders": min(count, rule.required_orders),
                    "window_days": rule.window_days,
                    "min_order_amount": float(rule.min_order_amount) if rule.min_order_amount is not None else None,
                    "bonus_points": rule.bonus_points,
                }
            )
        return out

    def update_streak(self, user_id: int, commit: bool = True):
        """Award every active, currently-effective streak rule the user now
        satisfies. Each rule is independent and re-awardable at most once per its
        own ``window_days`` (cooldown), tracked via ``streak_rule_id`` in the
        award transaction's ``extra_data``.
        """
        # Entity-eligibility gate (product-owner decision 2026-06-24): an
        # ineligible entity user (entity with no active loyalty-eligible
        # corporate contract) must earn NOTHING. Clean early-return — never
        # raise — so the order/delivery flow that triggered this is unaffected.
        user = User.query.get(user_id)
        if not self.is_user_loyalty_eligible(user):
            return

        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program:
            return

        now = datetime.now(timezone.utc)
        rules = LoyaltyStreakRule.query.filter_by(program_id=program.id, is_active=True).all()
        awarded = False

        for rule in rules:
            if not rule.is_effective(now):
                continue
            if self._qualifying_order_count(user_id, rule, now) < rule.required_orders:
                continue
            if self._streak_rule_in_cooldown(user_id, rule, now):
                continue
            self.award_points(
                user_id,
                rule.bonus_points,
                rule.name,
                LoyaltyActionType.STREAK_BONUS,
                extra_data={"streak_rule_id": rule.id},
                commit=False,
            )
            awarded = True

        # A new order-strike achievement is the only event that can advance a
        # consecutive-strike run, so evaluate those rules in the same transaction.
        consec_awarded = self.update_consecutive_strikes(user_id, commit=False)

        if (awarded or consec_awarded) and commit:
            db.session.commit()

    def _streak_rule_in_cooldown(self, user_id: int, rule, now: datetime) -> bool:
        """True if this rule was already awarded to the user within the last
        ``window_days``."""
        window_start = now - timedelta(days=rule.window_days)
        recent = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.created_at >= window_start,
        ).all()
        for t in recent:
            ed = t.extra_data or {}
            if ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value and ed.get("streak_rule_id") == rule.id:
                return True
        return False

    def _strike_achievement_times(self, user_id: int, strike_rule_id: int) -> List[datetime]:
        """All UTC achievement timestamps for one order-strike rule, newest-first.

        Reads the loyalty ledger directly (an achievement = a STREAK_BONUS award
        carrying this ``streak_rule_id`` in ``extra_data``). Filtered in Python to
        stay portable across the JSON ``extra_data`` column, mirroring
        ``_streak_rule_in_cooldown``.
        """
        txns = LoyaltyTransaction.query.filter(LoyaltyTransaction.user_id == user_id).all()
        times: List[datetime] = []
        for t in txns:
            ed = t.extra_data or {}
            if (
                ed.get("action_type") == LoyaltyActionType.STREAK_BONUS.value
                and ed.get("streak_rule_id") == strike_rule_id
                and t.created_at is not None
            ):
                times.append(ensure_utc(t.created_at))
        times.sort(reverse=True)
        return times

    def _strike_consecutive_run(self, user_id: int, strike_rule, now: datetime):
        """Current consecutive-achievement run for one strike, ending at the latest
        achievement. Two adjacent achievements are "consecutive" iff their gap is
        ``< 2 * window_days`` (no fully-skipped period). Returns
        ``(run_length, earliest_run_timestamp)``; ``(0, None)`` if never achieved.
        """
        times = self._strike_achievement_times(user_id, strike_rule.id)
        if not times:
            return 0, None
        limit = timedelta(days=2 * strike_rule.window_days)
        run = [times[0]]
        for prev in times[1:]:
            if run[-1] - prev < limit:
                run.append(prev)
            else:
                break
        return len(run), run[-1]

    def get_consecutive_strike_progress(self, user_id: int):
        """Live progress toward each active, currently-effective consecutive-strike
        rule for the default program. Computed from the ledger — no stored counter."""
        from business_app.utils.helpers import get_current_language

        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program:
            return []
        now = datetime.now(timezone.utc)
        lang = get_current_language()
        out = []
        rules = (
            LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id, is_active=True)
            .order_by(LoyaltyConsecutiveStrikeRule.display_order.asc())
            .all()
        )
        for rule in rules:
            if not rule.is_effective(now) or not rule.strikes:
                continue
            n = rule.required_consecutive
            per_strike = []
            counts = []
            for s in rule.strikes:
                count, _ = self._strike_consecutive_run(user_id, s, now)
                last_times = self._strike_achievement_times(user_id, s.id)
                active = bool(last_times) and (now - last_times[0]) < timedelta(days=2 * s.window_days)
                counts.append(count)
                per_strike.append(
                    {
                        "strike_name": s.get_translated("name", lang),
                        "current": min(count, n),
                        "target": n,
                        "window_days": s.window_days,
                        "active": active,
                    }
                )
            combined = max(counts) if rule.combine_mode == "any" else min(counts)
            out.append(
                {
                    "name": rule.get_translated("name", lang),
                    "required_consecutive": n,
                    "combine_mode": rule.combine_mode,
                    "bonus_points": rule.bonus_points,
                    "combined_current": min(combined, n),
                    "per_strike": per_strike,
                }
            )
        return out

    def _consecutive_awards_since(self, user_id: int, rule_id: int, since_dt: datetime) -> int:
        """Number of meta-bonus awards already granted for this rule at or after
        ``since_dt`` (the start of the current combined run). Drives idempotency
        and 'repeat every N' with zero stored state."""
        count = 0
        for t in LoyaltyTransaction.query.filter(LoyaltyTransaction.user_id == user_id).all():
            ed = t.extra_data or {}
            if (
                ed.get("action_type") == LoyaltyActionType.CONSECUTIVE_STREAK_BONUS.value
                and ed.get("consecutive_strike_rule_id") == rule_id
                and t.created_at is not None
                and ensure_utc(t.created_at) >= since_dt
            ):
                count += 1
        return count

    def update_consecutive_strikes(self, user_id: int, commit: bool = True):
        """Award every active, currently-effective consecutive-strike rule the user
        now satisfies. ``combine_mode='all'`` needs every attached strike to reach
        ``required_consecutive``; ``'any'`` needs one. Awards ``bonus_points`` per
        completed multiple of N (repeat every N); idempotent via
        ``_consecutive_awards_since``."""
        # Entity-eligibility gate (product-owner decision 2026-06-24): an
        # ineligible entity user earns NOTHING. Return False (this method's
        # bool contract = "nothing awarded") — never raise.
        user = User.query.get(user_id)
        if not self.is_user_loyalty_eligible(user):
            return False

        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program:
            return False

        now = datetime.now(timezone.utc)
        rules = LoyaltyConsecutiveStrikeRule.query.filter_by(program_id=program.id, is_active=True).all()
        awarded = False

        for rule in rules:
            if not rule.is_effective(now) or not rule.strikes:
                continue
            # Skip mis-configured rules: award_points(0) raises ValidationError,
            # which would abort the whole consecutive evaluation for this user.
            if rule.bonus_points <= 0:
                continue
            runs = [self._strike_consecutive_run(user_id, s, now) for s in rule.strikes]
            counts = [c for c, _ in runs]
            starts = [rs for _, rs in runs if rs is not None]
            n = rule.required_consecutive

            if rule.combine_mode == "any":
                combined = max(counts)
                idx = counts.index(combined)
                run_start = runs[idx][1]
            else:  # 'all'
                combined = min(counts)
                # All attached strikes must be currently running; the binding run
                # start is the latest-starting among them.
                run_start = max(starts) if len(starts) == len(rule.strikes) else None

            if combined < n or run_start is None:
                continue

            target_awards = combined // n
            already = self._consecutive_awards_since(user_id, rule.id, run_start)
            for milestone in range(already + 1, target_awards + 1):
                self.award_points(
                    user_id,
                    rule.bonus_points,
                    rule.name,
                    LoyaltyActionType.CONSECUTIVE_STREAK_BONUS,
                    extra_data={"consecutive_strike_rule_id": rule.id, "milestone": milestone},
                    commit=False,
                )
                awarded = True

        if awarded and commit:
            db.session.commit()
        return awarded

    @staticmethod
    def _parse_surprise_amounts(raw) -> List[int]:
        """Parse the admin-configured CSV (e.g. '50,100,200') into positive ints.
        Bad/blank entries are dropped; an empty result means 'no award'."""
        amounts: List[int] = []
        for part in str(raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value > 0:
                amounts.append(value)
        return amounts

    def _user_in_surprise_cooldown(self, user_id: int, cooldown_days: int) -> bool:
        """True if the user received a surprise reward within the last
        ``cooldown_days`` (relative to now — the award moment). Anchoring on now
        keeps the batch idempotent: a re-run sees the award it just made and skips
        the user, so a duplicate/retried run never double-awards."""
        if cooldown_days <= 0:
            return False
        since = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
        rows = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.user_id == user_id,
            LoyaltyTransaction.transaction_type == LoyaltyTransactionType.BONUS,
            LoyaltyTransaction.created_at >= since,
        ).all()
        return any((t.extra_data or {}).get("action_type") == LoyaltyActionType.SURPRICE_REWARD.value for t in rows)

    def process_daily_surprise_rewards(self, for_date=None) -> Dict[str, Any]:
        """Share surprise rewards for one delivery day's eligible orders.

        Run nightly by a Celery beat task. Scans orders that were DELIVERED on the
        target day (``Delivery.delivered_at``) AND are fully paid by the end of that
        day — so a prepaid order and a COD order paid the same day both qualify,
        while a COD order paid the next day does not. For each eligible INDIVIDUAL
        customer (one roll per user per day), not in cooldown and under the global
        daily cap, the configured win-chance roll may grant a random configured
        amount as a BONUS (action_type=surprise_reward).

        ``for_date`` is a business-calendar date; defaults to yesterday.
        Returns ``{"candidates": int, "awarded": int}``.
        """
        import random
        from zoneinfo import ZoneInfo
        from shared.constants import DISPLAY_TIMEZONE
        from shared.enums import DeliveryStatus
        from business_app.models.delivery import Delivery

        program = (
            LoyaltyProgram.query.filter_by(is_default=True, is_active=True).first()
            or LoyaltyProgram.query.filter_by(is_active=True).first()
        )
        if not program or not program.surprise_enabled:
            return {"candidates": 0, "awarded": 0}

        amounts = self._parse_surprise_amounts(program.surprise_amounts)
        chance = program.surprise_chance_percent or 0
        if not amounts or chance <= 0:
            return {"candidates": 0, "awarded": 0}

        tz = ZoneInfo(DISPLAY_TIMEZONE)
        if for_date is None:
            for_date = (datetime.now(tz) - timedelta(days=1)).date()
        day_start = datetime(for_date.year, for_date.month, for_date.day, tzinfo=tz).astimezone(timezone.utc)
        day_end = (datetime(for_date.year, for_date.month, for_date.day, tzinfo=tz) + timedelta(days=1)).astimezone(
            timezone.utc
        )

        # Candidate orders for the day: their Delivery is marked DELIVERED with a
        # delivered_at on the target day, and the order is fully paid by the end of
        # that day (prepaid: paid earlier; COD-same-day: paid within the day; a
        # COD order paid the next day has paid_at >= day_end → excluded).
        candidates = (
            Order.query.join(Delivery, Delivery.order_id == Order.id)
            .filter(
                Delivery.status == DeliveryStatus.DELIVERED,
                Delivery.delivered_at >= day_start,
                Delivery.delivered_at < day_end,
                Order.is_paid.is_(True),
                Order.paid_at.isnot(None),
                Order.paid_at < day_end,
            )
            .order_by(Delivery.delivered_at.asc())
            .all()
        )

        daily_cap = program.surprise_daily_cap or 0
        cooldown_days = program.surprise_cooldown_days or 0
        awarded = 0
        seen_users = set()
        for order in candidates:
            if daily_cap and awarded >= daily_cap:
                break
            uid = order.user_id
            if uid in seen_users:
                continue  # one roll per eligible user per day
            seen_users.add(uid)

            user = User.query.get(uid)
            if not user or user.user_type != UserType.INDIVIDUAL:
                continue
            if self._user_in_surprise_cooldown(uid, cooldown_days):
                continue
            if random.random() >= chance / 100.0:
                continue

            self.award_points(
                uid,
                random.choice(amounts),
                "Surprise Reward! Thanks for being loyal 💙",
                LoyaltyActionType.SURPRICE_REWARD,
                commit=True,
            )
            awarded += 1

        return {"candidates": len(candidates), "awarded": awarded}

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
            qualifying_points = self.calculate_qualifying_points(account.user_id)
            points_needed = next_tier.min_points - qualifying_points
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
        """Generate a random, collision-resistant, unique reward code.

        Random (not time-derived) so two same-second redemptions cannot produce
        the same code and violate the UNIQUE constraint on
        ``reward_redemptions.code``; each candidate is checked against existing
        rows before use.
        """
        import secrets
        from business_app.models.loyalty import RewardRedemption

        for _ in range(10):
            code = f"RWD{secrets.token_hex(4).upper()}"  # RWD + 8 hex chars
            if not RewardRedemption.query.filter_by(code=code).first():
                return code
        return f"RWD{secrets.token_hex(8).upper()}"  # extremely unlikely fallback

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
        qualifying_points = self.calculate_qualifying_points(user_id)

        if next_tier_info and next_tier_info["threshold"]:
            progress_percentage = max(0, min(100, (qualifying_points / next_tier_info["threshold"]) * 100))
        else:
            progress_percentage = 100  # Already at highest tier

        return {
            "current_tier": account.current_tier,
            "current_points": qualifying_points,
            "next_tier": next_tier_info["tier"] if next_tier_info else None,
            "points_to_next_tier": next_tier_info["points_needed"] if next_tier_info else 0,
            "progress_percentage": progress_percentage,
        }

    def get_reward_categories(self) -> List[str]:
        """Get all available reward categories"""
        categories = db.session.query(LoyaltyReward.reward_type).distinct().all()
        return [category[0] for category in categories if category[0]]

    def is_reward_configured(self, reward) -> bool:
        """Structural usability of a reward, independent of the user.

        A reward that is not configured must never be offered for redemption.
        """
        from decimal import Decimal
        from business_app.models.product import Product

        if reward.reward_type == "discount":
            if reward.discount_type not in ("percentage", "fixed"):
                return False
            return reward.discount_value is not None and Decimal(str(reward.discount_value)) > 0

        if reward.reward_type == "free_product":
            if not reward.free_product_id:
                return False
            if (reward.free_product_quantity or 0) < 1:
                return False
            product = Product.query.get(reward.free_product_id)
            return bool(product and product.is_active)

        # Removed/legacy reward types (free_delivery, voucher) are not redeemable.
        return False

    def can_redeem_reward(self, user_id, reward_id):
        from business_app.models.loyalty import RewardRedemption

        reward = LoyaltyReward.query.get(reward_id)
        if not reward or not reward.is_active or reward.is_system_reward:
            return False
        # is_reward_configured() also rejects other types, but this fast-exit
        # avoids the Product DB lookup for structurally unsupported types.
        if reward.reward_type not in ("discount", "free_product"):
            return False
        if not self.is_reward_configured(reward):
            return False
        if self.get_available_points(user_id) < (reward.points_cost or 0):
            return False

        now = datetime.now(timezone.utc)
        valid_until = reward.valid_until
        if valid_until and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        if valid_until and valid_until < now:
            return False
        valid_from = reward.valid_from
        if valid_from and valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_from and now < valid_from:
            return False

        if reward.max_redemptions and (reward.redemptions_used or 0) >= reward.max_redemptions:
            return False
        if reward.max_uses_per_user:
            used = RewardRedemption.query.filter_by(reward_id=reward.id, user_id=user_id, status="applied").count()
            if used >= reward.max_uses_per_user:
                return False
        return True

    def get_action_points(self, action: str) -> int:
        """Points for a given engagement action.

        referral / birthday come from the LoyaltyProgram DB columns (single
        source of truth). The remaining engagement actions are not modeled on
        LoyaltyProgram, so they keep operational defaults.
        """
        if action == "referral_signup":
            return self._program_bonus("referral_bonus", 50)
        if action == "birthday_bonus":
            return self._program_bonus("birthday_bonus", 25)

        engagement_defaults = {
            "social_share": 50,
            "review_submitted": 100,
            "survey_completed": 75,
            "app_install": 100,
            "newsletter_signup": 25,
        }
        return engagement_defaults.get(action, 0)

    def get_user_referral_code(self, user_id: int) -> str:
        """Get the user's persisted referral code, generating it once on first use."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")
        if not user.referral_code:
            user.referral_code = self._generate_unique_referral_code()
            db.session.commit()
        return user.referral_code

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
            # Key matches the exception-fallback dict + both consumers (bot + web).
            "points_earned_from_referrals": referral_points,
        }

    def get_referral_points_earned(self, referrer_id: int, referee_id: int) -> int:
        """Get points earned from specific referral"""
        # Find referral program for this referee
        referral = ReferralProgram.query.filter(
            ReferralProgram.referrer_id == referrer_id, ReferralProgram.referee_id == referee_id
        ).first()

        if not referral:
            return 0

        # Find the transaction for this referral. action_type lives in the JSON
        # extra_data; filter it in Python (matching grant_welcome_bonus) rather than
        # via a JSON-path SQL operator, which is not portable across DB backends.
        candidates = (
            LoyaltyTransaction.query.filter(
                LoyaltyTransaction.user_id == referrer_id,
                LoyaltyTransaction.description.contains("referral"),
            )
            .order_by(LoyaltyTransaction.created_at.asc())
            .all()
        )
        for transaction in candidates:
            if (transaction.extra_data or {}).get("action_type") == LoyaltyActionType.REFERRAL.value:
                return transaction.points

        return 0

    def get_referrer_bonus_points(self) -> int:
        """Referrer bonus = LoyaltyProgram.referral_bonus (DB SSOT)."""
        return self._program_bonus("referral_bonus", 50)

    def get_referee_bonus_points(self) -> int:
        """Referee bonus = half the referral bonus (DB SSOT)."""
        return self._program_bonus("referral_bonus", 50) // 2

    # NOTE: get_tier_history and get_user_challenges were removed (loyalty SSOT,
    # Phase 2). Both were stubs with no backing model/table; the challenges API
    # route and the synthetic tier_history payload were removed with them.

    def get_tier_benefits(self, tier: str) -> Dict[str, Any]:
        """Get benefits for specific tier"""
        return self._get_tier_benefits(tier)

    def get_tier_upgrade_requirements(self, user_id: int) -> Dict[str, Any]:
        """Get tier upgrade requirements for user"""
        account = self.get_or_create_loyalty_account(user_id)
        next_tier_info = self._get_next_tier_info(account)
        qualifying_points = self.calculate_qualifying_points(user_id)

        if next_tier_info:
            return {
                "current_tier": account.current_tier,
                "next_tier": next_tier_info["tier"],
                "points_needed": next_tier_info["points_needed"],
                "current_points": qualifying_points,
                "target_points": next_tier_info["threshold"],
            }
        else:
            return {
                "current_tier": account.current_tier,
                "next_tier": None,
                "points_needed": 0,
                "current_points": qualifying_points,
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

            # Qualify on the referee's FIRST order that is both DELIVERED and
            # fully PAID. is_paid is the payment SSOT (set only when payment is
            # COMPLETED — including COD cash collection — and cleared on refund),
            # so a delivered-but-unpaid order (e.g. COD with collection still
            # pending) must NOT pay out. Do NOT pin first_order_id to a
            # non-qualifying order — that would freeze the referral forever and a
            # later delivered+paid order would never pay out.
            first_qualifying = (
                Order.query.filter_by(user_id=referral.referee_id, status=OrderStatus.DELIVERED, is_paid=True)
                .order_by(Order.created_at.asc())
                .first()
            )
            if not first_qualifying:
                continue

            referral.first_order_id = first_qualifying.id
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

    def grant_birthday_bonuses(self) -> Dict[str, int]:
        """Grant the birthday bonus to users whose birthday is today.

        Amount comes from the LoyaltyProgram DB column (SSOT). Idempotent within
        a calendar year: a user who already received a birthday bonus this year
        is skipped, so re-runs (or a backfill) never double-grant.
        """
        from zoneinfo import ZoneInfo
        from shared.constants import DISPLAY_TIMEZONE

        bonus = self._program_bonus("birthday_bonus", 25)
        if bonus <= 0:
            return {"granted": 0}

        # Compare birthdays on the BUSINESS calendar, not UTC. date_of_birth is a
        # timestamptz storing local midnight (so its UTC instant lands on the prior
        # day for UTC+ zones); matching by UTC EXTRACT would fire a day early.
        business_tz = ZoneInfo(DISPLAY_TIMEZONE)
        today_local = datetime.now(business_tz)

        granted = 0
        for user in User.query.filter(User.date_of_birth.isnot(None)).all():
            dob = user.date_of_birth
            if dob.tzinfo is None:
                dob = dob.replace(tzinfo=timezone.utc)
            dob_local = dob.astimezone(business_tz)
            if dob_local.month != today_local.month or dob_local.day != today_local.day:
                continue

            # Idempotency check in Python (cross-DB; avoids backend-specific JSON
            # operators): has this user already received a birthday bonus this year?
            year_bonuses = LoyaltyTransaction.query.filter(
                LoyaltyTransaction.user_id == user.id,
                LoyaltyTransaction.transaction_type == LoyaltyTransactionType.BONUS,
            ).all()
            already = any(
                (t.extra_data or {}).get("action_type") == LoyaltyActionType.BIRTHDAY_BONUS.value
                and t.created_at
                and t.created_at.year == today_local.year
                for t in year_bonuses
            )
            if already:
                continue
            try:
                self.award_points(user.id, bonus, "Birthday bonus", action_type=LoyaltyActionType.BIRTHDAY_BONUS)
                granted += 1
            except Exception as exc:
                db.session.rollback()
                current_app.logger.error(f"Failed to grant birthday bonus to user {user.id}: {exc}")

        return {"granted": granted}

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
