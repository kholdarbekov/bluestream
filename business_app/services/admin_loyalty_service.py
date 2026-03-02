"""Business/query logic for admin loyalty management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func, or_

from business_app import db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTierConfig,
    LoyaltyTransaction,
)
from business_app.models.user import User
from business_app.serializers.loyalty_serializers import serialize_loyalty_transaction
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.exceptions import NotFoundError


class AdminLoyaltyService:
    """Business/query logic for admin loyalty endpoints."""

    @staticmethod
    def list_members(
        *,
        page: int = 1,
        per_page: int = 20,
        search: str = "",
        program_id: Optional[int] = None,
        tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return paginated loyalty members with summary metadata."""
        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)

        query = db.session.query(LoyaltyPoints, User).join(
            User,
            User.id == LoyaltyPoints.user_id,
        )

        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    User.first_name.ilike(term),
                    User.last_name.ilike(term),
                    User.email.ilike(term),
                    User.phone.ilike(term),
                )
            )

        if program_id:
            query = query.filter(LoyaltyPoints.program_id == program_id)

        if tier:
            query = query.filter(LoyaltyPoints.current_tier == tier)

        ordered_query = query.order_by(
            LoyaltyPoints.last_activity_date.desc(),
            LoyaltyPoints.created_at.desc(),
            LoyaltyPoints.id.desc(),
        )
        pagination = ordered_query.paginate(page=page, per_page=per_page, error_out=False)

        filtered_pairs = query.all()
        items = [
            AdminLoyaltyService.serialize_member(account, user)
            for account, user in pagination.items
        ]

        total_points_in_circulation = sum(
            max(0, account.current_balance or 0) for account, _ in filtered_pairs
        )
        total_earned = sum(account.total_earned or 0 for account, _ in filtered_pairs)

        return {
            "items": items,
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "summary": {
                "total_members": pagination.total,
                "active_members": sum(
                    1 for account, _ in filtered_pairs if (account.current_balance or 0) > 0
                ),
                "total_points_in_circulation": total_points_in_circulation,
                "total_points_earned": total_earned,
                "average_points_balance": round(
                    total_points_in_circulation / pagination.total,
                    2,
                ) if pagination.total else 0,
            },
        }

    @staticmethod
    def get_member_detail(user_id: int) -> Dict[str, Any]:
        """Return a single loyalty member detail payload."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        loyalty_service = LoyaltyService()
        account = loyalty_service.get_or_create_loyalty_account(user_id)

        transactions = (
            LoyaltyTransaction.query.filter_by(user_id=user_id)
            .order_by(LoyaltyTransaction.created_at.desc())
            .limit(10)
            .all()
        )
        redemptions = [
            txn for txn in transactions
            if txn.transaction_type == LoyaltyTransactionType.REDEEMED
        ]

        return {
            "member": AdminLoyaltyService.serialize_member(account, user),
            "recent_transactions": [
                serialize_loyalty_transaction(item) for item in transactions
            ],
            "recent_redemptions": [
                serialize_loyalty_transaction(item) for item in redemptions
            ],
            "referral_statistics": loyalty_service.get_referral_statistics(user_id),
            "tier_progress": loyalty_service.calculate_tier_progress(user_id),
            "streak": {
                "current_streak": account.current_streak or 0,
                "orders_this_month": account.streak_orders_this_month or 0,
                "last_streak_update": (
                    account.last_streak_update.isoformat()
                    if account.last_streak_update else None
                ),
            },
        }

    @staticmethod
    def serialize_member(account: LoyaltyPoints, user: User) -> Dict[str, Any]:
        """Serialize a loyalty member row."""
        program = LoyaltyProgram.query.get(account.program_id) if account.program_id else None

        last_activity = account.last_activity_date
        if not last_activity:
            last_transaction = (
                LoyaltyTransaction.query.filter_by(user_id=account.user_id)
                .order_by(LoyaltyTransaction.created_at.desc())
                .first()
            )
            last_activity = last_transaction.created_at if last_transaction else None

        return {
            "id": account.id,
            "user_id": user.id,
            "customer_name": user.full_name or user.email or user.phone or f"User #{user.id}",
            "customer_email": user.email,
            "customer_phone": user.phone,
            "program_id": account.program_id,
            "program_name": program.name if program else None,
            "current_tier": account.current_tier,
            "current_balance": account.current_balance or 0,
            "total_earned": account.total_earned or 0,
            "total_redeemed": account.total_redeemed or 0,
            "points_to_next_tier": account.points_to_next_tier or 0,
            "tier_valid_until": (
                account.tier_valid_until.isoformat()
                if account.tier_valid_until else None
            ),
            "last_activity_at": last_activity.isoformat() if last_activity else None,
            "member_since": account.created_at.isoformat() if account.created_at else None,
        }

    @staticmethod
    def list_programs(
        *,
        page: int = 1,
        per_page: int = 20,
        search: str = "",
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return paginated loyalty programs."""
        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)

        query = LoyaltyProgram.query

        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    LoyaltyProgram.name.ilike(term),
                    LoyaltyProgram.description.ilike(term),
                )
            )

        if status == "active":
            query = query.filter(LoyaltyProgram.is_active.is_(True))
        elif status == "inactive":
            query = query.filter(LoyaltyProgram.is_active.is_(False))

        pagination = query.order_by(
            LoyaltyProgram.is_default.desc(),
            LoyaltyProgram.created_at.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

        return {
            "items": [
                AdminLoyaltyService.serialize_program(program)
                for program in pagination.items
            ],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
        }

    @staticmethod
    def serialize_program(program: LoyaltyProgram) -> Dict[str, Any]:
        """Serialize a loyalty program with derived counts."""
        payload = program.to_dict()
        payload.update({
            "member_count": LoyaltyPoints.query.filter_by(program_id=program.id).count(),
            "tier_count": LoyaltyTierConfig.query.filter_by(program_id=program.id).count(),
        })
        return payload

    @staticmethod
    def list_rewards(
        *,
        page: int = 1,
        per_page: int = 20,
        program_id: Optional[int] = None,
        reward_type: Optional[str] = None,
        is_active: Optional[str] = None,
        search: str = "",
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return paginated rewards for the admin UI."""
        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)

        query = LoyaltyReward.query

        if program_id:
            query = query.filter(LoyaltyReward.program_id == program_id)

        if reward_type:
            query = query.filter(LoyaltyReward.reward_type == reward_type)

        if is_active is not None:
            query = query.filter(LoyaltyReward.is_active.is_(is_active.lower() == "true"))

        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    LoyaltyReward.name.ilike(term),
                    LoyaltyReward.description.ilike(term),
                )
            )

        pagination = query.order_by(
            LoyaltyReward.sort_order.asc(),
            LoyaltyReward.created_at.desc(),
        ).paginate(page=page, per_page=per_page, error_out=False)

        return {
            "items": [
                AdminLoyaltyService.serialize_reward(reward, language=language)
                for reward in pagination.items
            ],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
        }

    @staticmethod
    def get_reward_detail(reward_id: int, *, language: Optional[str] = None) -> Dict[str, Any]:
        """Return a single reward detail payload."""
        reward = LoyaltyReward.query.get(reward_id)
        if not reward:
            raise NotFoundError("Loyalty reward not found")
        return AdminLoyaltyService.serialize_reward(
            reward,
            language=language,
            include_all_translations=True,
            include_program=True,
            include_stats=True,
        )

    @staticmethod
    def serialize_reward(
        reward: LoyaltyReward,
        *,
        language: Optional[str] = None,
        include_all_translations: bool = False,
        include_program: bool = False,
        include_stats: bool = False,
    ) -> Dict[str, Any]:
        """Serialize a loyalty reward with admin-derived fields."""
        payload = reward.to_dict(
            language=language,
            include_all_translations=include_all_translations,
        )
        payload["program_name"] = reward.program.name if reward.program else None
        if include_program and reward.program:
            payload["program"] = AdminLoyaltyService.serialize_program(reward.program)
        if include_stats:
            payload["redemption_stats"] = {
                "total_redemptions": reward.redemptions_used or 0,
                "remaining_redemptions": (
                    reward.max_redemptions - (reward.redemptions_used or 0)
                    if reward.max_redemptions else None
                ),
                "is_available": reward.is_active and (
                    not reward.max_redemptions
                    or (reward.redemptions_used or 0) < reward.max_redemptions
                ),
            }
        return payload

    @staticmethod
    def get_analytics(
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        program_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return loyalty analytics payload for the main analytics page."""
        end_dt = AdminLoyaltyService._parse_datetime(end_date) or datetime.now(UTC)
        start_dt = AdminLoyaltyService._parse_datetime(start_date) or (end_dt - timedelta(days=30))

        account_query = LoyaltyPoints.query
        reward_query = LoyaltyReward.query
        transaction_query = LoyaltyTransaction.query.filter(
            LoyaltyTransaction.created_at >= start_dt,
            LoyaltyTransaction.created_at <= end_dt,
        )

        if program_id:
            account_query = account_query.filter(LoyaltyPoints.program_id == program_id)
            reward_query = reward_query.filter(LoyaltyReward.program_id == program_id)
            transaction_query = transaction_query.join(
                LoyaltyPoints,
                LoyaltyPoints.user_id == LoyaltyTransaction.user_id,
            ).filter(LoyaltyPoints.program_id == program_id)

        accounts = account_query.all()
        transactions = transaction_query.all()

        earned_transactions = [
            txn for txn in transactions
            if txn.transaction_type in {LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS}
            and txn.points > 0
        ]
        redeemed_transactions = [
            txn for txn in transactions
            if txn.transaction_type == LoyaltyTransactionType.REDEEMED and txn.points < 0
        ]

        total_redeemed_points = abs(sum(txn.points for txn in redeemed_transactions))
        total_earned_points = sum(txn.points for txn in earned_transactions)

        tier_distribution_query = db.session.query(
            LoyaltyPoints.current_tier,
            func.count(LoyaltyPoints.id),
        )
        if program_id:
            tier_distribution_query = tier_distribution_query.filter(
                LoyaltyPoints.program_id == program_id
            )
        tier_distribution = [
            {"tier": tier_name, "count": count}
            for tier_name, count in tier_distribution_query.group_by(
                LoyaltyPoints.current_tier,
            ).all()
        ]

        top_rewards = [
            {
                "reward_id": reward.id,
                "name": reward.name,
                "program_name": reward.program.name if reward.program else None,
                "points_cost": reward.points_cost,
                "redemptions": reward.redemptions_used or 0,
            }
            for reward in reward_query.filter(
                LoyaltyReward.redemptions_used > 0,
            ).order_by(
                LoyaltyReward.redemptions_used.desc(),
            ).limit(10).all()
        ]

        points_by_day: Dict[str, Dict[str, int]] = {}
        for txn in transactions:
            day_key = (txn.created_at or start_dt).date().isoformat()
            bucket = points_by_day.setdefault(day_key, {"earned": 0, "redeemed": 0})
            if txn.transaction_type in {LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS} and txn.points > 0:
                bucket["earned"] += txn.points
            elif txn.transaction_type == LoyaltyTransactionType.REDEEMED and txn.points < 0:
                bucket["redeemed"] += abs(txn.points)

        points_trend = [
            {
                "date": date_key,
                "earned": values["earned"],
                "redeemed": values["redeemed"],
            }
            for date_key, values in sorted(points_by_day.items())
        ]

        program_breakdown = []
        for program in LoyaltyProgram.query.order_by(LoyaltyProgram.created_at.desc()).all():
            if program_id and program.id != program_id:
                continue
            program_accounts = [account for account in accounts if account.program_id == program.id]
            program_breakdown.append({
                "program_id": program.id,
                "program_name": program.name,
                "member_count": len(program_accounts),
                "points_in_circulation": sum(
                    max(0, account.current_balance or 0) for account in program_accounts
                ),
                "reward_count": LoyaltyReward.query.filter_by(program_id=program.id).count(),
            })

        return {
            "summary": {
                "total_members": len(accounts),
                "active_members": len([
                    account for account in accounts if (account.current_balance or 0) > 0
                ]),
                "total_points_in_circulation": sum(
                    max(0, account.current_balance or 0) for account in accounts
                ),
                "points_earned": total_earned_points,
                "points_redeemed": total_redeemed_points,
                "total_redemptions": len(redeemed_transactions),
                "avg_redemption_value": round(
                    total_redeemed_points / len(redeemed_transactions),
                    2,
                ) if redeemed_transactions else 0,
            },
            "tier_distribution": tier_distribution,
            "top_rewards": top_rewards,
            "points_trend": points_trend,
            "redemption_metrics": {
                "points_earned": total_earned_points,
                "points_redeemed": total_redeemed_points,
                "total_redemptions": len(redeemed_transactions),
                "redemption_rate": round(
                    (total_redeemed_points / total_earned_points) * 100,
                    2,
                ) if total_earned_points else 0,
            },
            "program_breakdown": program_breakdown,
        }

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        """Parse ISO datetime/date strings into UTC-aware datetimes."""
        if not value:
            return None
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
