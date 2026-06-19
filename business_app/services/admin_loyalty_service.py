"""Business/query logic for admin loyalty management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import case, func, or_

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
    def _apply_member_filters(query, *, search: str = "", program_id: Optional[int] = None, tier: Optional[str] = None):
        """Apply shared loyalty-member filters to a query rooted on LoyaltyPoints/User."""
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

        return query

    @staticmethod
    def _latest_activity_subquery():
        """Return a subquery containing the latest loyalty transaction timestamp per user."""
        return (
            db.session.query(
                LoyaltyTransaction.user_id.label("user_id"),
                func.max(LoyaltyTransaction.created_at).label("last_activity_at"),
            )
            .group_by(LoyaltyTransaction.user_id)
            .subquery()
        )

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

        latest_activity_subquery = AdminLoyaltyService._latest_activity_subquery()

        row_query = (
            db.session.query(
                LoyaltyPoints,
                User,
                LoyaltyProgram.name.label("program_name"),
                latest_activity_subquery.c.last_activity_at.label("latest_activity_at"),
            )
            .join(
                User,
                User.id == LoyaltyPoints.user_id,
            )
            .outerjoin(
                LoyaltyProgram,
                LoyaltyProgram.id == LoyaltyPoints.program_id,
            )
            .outerjoin(
                latest_activity_subquery,
                latest_activity_subquery.c.user_id == LoyaltyPoints.user_id,
            )
        )
        row_query = AdminLoyaltyService._apply_member_filters(
            row_query,
            search=search,
            program_id=program_id,
            tier=tier,
        )

        ordered_query = row_query.order_by(
            LoyaltyPoints.last_activity_date.desc(),
            LoyaltyPoints.created_at.desc(),
            LoyaltyPoints.id.desc(),
        )
        pagination = ordered_query.paginate(page=page, per_page=per_page, error_out=False)

        summary_query = db.session.query(
            func.count(LoyaltyPoints.id).label("total_members"),
            func.coalesce(
                func.sum(case(((LoyaltyPoints.current_balance > 0), 1), else_=0)),
                0,
            ).label("active_members"),
            func.coalesce(
                func.sum(
                    case(
                        ((LoyaltyPoints.current_balance > 0), LoyaltyPoints.current_balance),
                        else_=0,
                    )
                ),
                0,
            ).label("total_points_in_circulation"),
            func.coalesce(func.sum(LoyaltyPoints.total_earned), 0).label("total_points_earned"),
        ).join(
            User,
            User.id == LoyaltyPoints.user_id,
        )
        summary_query = AdminLoyaltyService._apply_member_filters(
            summary_query,
            search=search,
            program_id=program_id,
            tier=tier,
        )
        summary = summary_query.one()

        items = [
            AdminLoyaltyService.serialize_member(
                account,
                user,
                program_name=program_name,
                latest_activity_at=latest_activity_at,
            )
            for account, user, program_name, latest_activity_at in pagination.items
        ]

        return {
            "items": items,
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "summary": {
                "total_members": summary.total_members,
                "active_members": summary.active_members,
                "total_points_in_circulation": summary.total_points_in_circulation,
                "total_points_earned": summary.total_points_earned,
                "average_points_balance": (
                    round(
                        summary.total_points_in_circulation / pagination.total,
                        2,
                    )
                    if pagination.total
                    else 0
                ),
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

        # Recent redemptions stay as a small at-a-glance card; the full transaction
        # ledger is served (paginated) by get_member_transactions so admins can see
        # there are more than the most recent rows.
        redemptions = (
            LoyaltyTransaction.query.filter_by(
                user_id=user_id,
                transaction_type=LoyaltyTransactionType.REDEEMED,
            )
            .order_by(LoyaltyTransaction.created_at.desc())
            .limit(10)
            .all()
        )

        return {
            "member": AdminLoyaltyService.serialize_member(account, user),
            "recent_redemptions": [serialize_loyalty_transaction(item) for item in redemptions],
            "referral_statistics": loyalty_service.get_referral_statistics(user_id),
            "tier_progress": loyalty_service.calculate_tier_progress(user_id),
            "streak_progress": loyalty_service.get_streak_progress(user_id),
        }

    @staticmethod
    def get_member_transactions(user_id: int, page: int = 1, per_page: int = 20) -> Dict[str, Any]:
        """Return a member's full loyalty transaction ledger, paginated (newest first)."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        page = max(page, 1)
        per_page = min(max(per_page, 1), 100)

        pagination = (
            LoyaltyTransaction.query.filter_by(user_id=user_id)
            .order_by(LoyaltyTransaction.created_at.desc(), LoyaltyTransaction.id.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )

        return {
            "items": [serialize_loyalty_transaction(item) for item in pagination.items],
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
        }

    @staticmethod
    def serialize_member(
        account: LoyaltyPoints,
        user: User,
        *,
        program_name: Optional[str] = None,
        latest_activity_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Serialize a loyalty member row."""
        last_activity = account.last_activity_date
        if not last_activity:
            if latest_activity_at is not None:
                last_activity = latest_activity_at
            else:
                last_transaction = (
                    LoyaltyTransaction.query.filter_by(user_id=account.user_id)
                    .order_by(LoyaltyTransaction.created_at.desc())
                    .first()
                )
                last_activity = last_transaction.created_at if last_transaction else None

        resolved_program_name = program_name
        if resolved_program_name is None and account.program_id:
            program = LoyaltyProgram.query.get(account.program_id)
            resolved_program_name = program.name if program else None

        return {
            "id": account.id,
            "user_id": user.id,
            "customer_name": user.full_name or user.email or user.phone or f"User #{user.id}",
            "customer_email": user.email,
            "customer_phone": user.phone,
            "program_id": account.program_id,
            "program_name": resolved_program_name,
            "current_tier": account.current_tier,
            "current_balance": account.current_balance or 0,
            "total_earned": account.total_earned or 0,
            "total_redeemed": account.total_redeemed or 0,
            "points_to_next_tier": account.points_to_next_tier or 0,
            "tier_valid_until": (account.tier_valid_until.isoformat() if account.tier_valid_until else None),
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

        member_counts_subquery = (
            db.session.query(
                LoyaltyPoints.program_id.label("program_id"),
                func.count(LoyaltyPoints.id).label("member_count"),
            )
            .group_by(LoyaltyPoints.program_id)
            .subquery()
        )
        tier_counts_subquery = (
            db.session.query(
                LoyaltyTierConfig.program_id.label("program_id"),
                func.count(LoyaltyTierConfig.id).label("tier_count"),
            )
            .group_by(LoyaltyTierConfig.program_id)
            .subquery()
        )
        query = (
            db.session.query(
                LoyaltyProgram,
                func.coalesce(member_counts_subquery.c.member_count, 0).label("member_count"),
                func.coalesce(tier_counts_subquery.c.tier_count, 0).label("tier_count"),
            )
            .outerjoin(
                member_counts_subquery,
                member_counts_subquery.c.program_id == LoyaltyProgram.id,
            )
            .outerjoin(
                tier_counts_subquery,
                tier_counts_subquery.c.program_id == LoyaltyProgram.id,
            )
        )

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
                AdminLoyaltyService.serialize_program(
                    program,
                    member_count=member_count,
                    tier_count=tier_count,
                )
                for program, member_count, tier_count in pagination.items
            ],
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
        }

    @staticmethod
    def serialize_program(
        program: LoyaltyProgram,
        *,
        member_count: Optional[int] = None,
        tier_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Serialize a loyalty program with derived counts."""
        payload = program.to_dict()
        payload.update(
            {
                "member_count": (
                    member_count
                    if member_count is not None
                    else LoyaltyPoints.query.filter_by(program_id=program.id).count()
                ),  # noqa: E501
                "tier_count": (
                    tier_count
                    if tier_count is not None
                    else LoyaltyTierConfig.query.filter_by(program_id=program.id).count()
                ),  # noqa: E501
            }
        )
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
            "items": [AdminLoyaltyService.serialize_reward(reward, language=language) for reward in pagination.items],
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
                    reward.max_redemptions - (reward.redemptions_used or 0) if reward.max_redemptions else None
                ),
                "is_available": reward.is_active
                and (not reward.max_redemptions or (reward.redemptions_used or 0) < reward.max_redemptions),
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
            txn
            for txn in transactions
            if txn.transaction_type in {LoyaltyTransactionType.EARNED, LoyaltyTransactionType.BONUS} and txn.points > 0
        ]
        redeemed_transactions = [
            txn for txn in transactions if txn.transaction_type == LoyaltyTransactionType.REDEEMED and txn.points < 0
        ]

        total_redeemed_points = abs(sum(txn.points for txn in redeemed_transactions))
        total_earned_points = sum(txn.points for txn in earned_transactions)

        tier_distribution_query = db.session.query(
            LoyaltyPoints.current_tier,
            func.count(LoyaltyPoints.id),
        )
        if program_id:
            tier_distribution_query = tier_distribution_query.filter(LoyaltyPoints.program_id == program_id)
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
            )
            .order_by(
                LoyaltyReward.redemptions_used.desc(),
            )
            .limit(10)
            .all()
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
            program_breakdown.append(
                {
                    "program_id": program.id,
                    "program_name": program.name,
                    "member_count": len(program_accounts),
                    "points_in_circulation": sum(max(0, account.current_balance or 0) for account in program_accounts),
                    "reward_count": LoyaltyReward.query.filter_by(program_id=program.id).count(),
                }
            )

        return {
            "summary": {
                "total_members": len(accounts),
                "active_members": len([account for account in accounts if (account.current_balance or 0) > 0]),
                "total_points_in_circulation": sum(max(0, account.current_balance or 0) for account in accounts),
                "points_earned": total_earned_points,
                "points_redeemed": total_redeemed_points,
                "total_redemptions": len(redeemed_transactions),
                "avg_redemption_value": (
                    round(
                        total_redeemed_points / len(redeemed_transactions),
                        2,
                    )
                    if redeemed_transactions
                    else 0
                ),
            },
            "tier_distribution": tier_distribution,
            "top_rewards": top_rewards,
            "points_trend": points_trend,
            "redemption_metrics": {
                "points_earned": total_earned_points,
                "points_redeemed": total_redeemed_points,
                "total_redemptions": len(redeemed_transactions),
                "redemption_rate": (
                    round(
                        (total_redeemed_points / total_earned_points) * 100,
                        2,
                    )
                    if total_earned_points
                    else 0
                ),
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
