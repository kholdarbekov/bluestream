"""Read/list/activate sales agents for the admin UI and the bot."""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_

from business_app import db
from business_app.models.sales import SalesAgentProfile
from business_app.models.user import User
from business_app.services.sales.agent_metrics_service import AgentMetricsService
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import NotFoundError
from business_app.utils.user_search import build_user_search_filter
from shared.enums import UserRole, UserStatus
from shared.staff_constants import STAFF_ACTIONS


class SalesAgentAccountService:
    @staticmethod
    def member_filter():
        """SQL predicate: users who hold the sales_agent role (column or staff_roles JSON)."""
        return StaffService.staff_role_member_filter(UserRole.SALES_AGENT.value)

    @staticmethod
    def active_agent_clauses():
        """The clauses that define "an active sales agent", as a tuple to be splatted.

        ONE spelling (R21). `active_agents()` and the list's *Active* filter both splat this,
        so a fourth clause added here reaches both readers instead of landing in one of them
        and quietly leaving the table and the card answering different questions.

        The caller joins `SalesAgentProfile` itself — these clauses reference it.
        """
        return (
            SalesAgentAccountService.member_filter(),
            SalesAgentProfile.is_active.is_(True),
            User.status == UserStatus.ACTIVE.value,
        )

    @staticmethod
    def active_agents() -> List[User]:
        """Every agent the business currently expects to work, ascending by id.

        The role, an ACTIVE profile and an active user row — ONE expression, because
        the nightly plan snapshot (which must write a row per agent, including a
        zero-due one), the *Sales agents* summary card and Task 2's
        `AgentMetricsService.rows_for_period` count the same population. A page whose
        card disagrees with its own table is a page nobody trusts, and a snapshot that
        held a denominator for somebody the report never lists is worse.

        USER rows, not ids: the metrics rows publish `agent_name` and `phone` beside
        the numbers, and a second id-only helper beside this one would be exactly the
        duplicate expression it exists to prevent. Callers that want ids write
        `[agent.id for agent in active_agents()]`.

        `telegram_id` is deliberately not part of it: an agent with no linked chat
        gets no morning digest, but their day still has a plan and their KPIs still
        count — which is exactly why this is not the digest task's loop.
        """
        return (
            User.query.join(SalesAgentProfile, SalesAgentProfile.user_id == User.id)
            .filter(*SalesAgentAccountService.active_agent_clauses())
            .order_by(User.id.asc())
            .all()
        )

    @staticmethod
    def profile_for(user_id: int) -> Optional[SalesAgentProfile]:
        return SalesAgentProfile.query.filter_by(user_id=user_id).first()

    @staticmethod
    def serialize(
        user: User, profile: Optional[SalesAgentProfile], counts: Optional[Dict[str, int]] = None
    ) -> Dict[str, Any]:
        counts = counts or {}
        return {
            "user_id": user.id,
            "full_name": user.full_name,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "email": user.email,
            "status": user.status.value if hasattr(user.status, "value") else user.status,
            "staff_roles": user.staff_roles or [],
            "telegram_linked": bool(user.telegram_id),
            "is_active": bool(profile.is_active) if profile else False,
            "districts": list(profile.districts or []) if profile else [],
            "weekly_new_outlet_target": profile.weekly_new_outlet_target if profile else None,
            "employment_type": profile.employment_type if profile else None,
            "notes": profile.notes if profile else None,
            # The published NAMES stay `outlets_assigned` / `outlets_active`: SalesAgents.js
            # prints them and the admin API test pins them. Their VALUES now come from
            # AgentMetricsService, which spells the same two counts `assigned_outlets` /
            # `active_outlets` — one rule, two spellings, translated in this ONE place.
            "outlets_assigned": counts.get("assigned_outlets", 0),
            "outlets_active": counts.get("active_outlets", 0),
            "visits_today": counts.get("visits_today", 0),
            "orders_today": counts.get("orders_today", 0),
            "last_login": user.last_login.isoformat() if user.last_login else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }

    @staticmethod
    def list_agents(
        page: int, per_page: int, search: Optional[str] = None, status: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], int, Dict[str, int]]:
        query = User.query.filter(SalesAgentAccountService.member_filter())
        if search:
            clause = build_user_search_filter(search, include_company=False)
            if clause is not None:
                query = query.filter(clause)
        if status in ("active", "inactive"):
            query = query.join(SalesAgentProfile, SalesAgentProfile.user_id == User.id)
            if status == "active":
                # The very clauses the *Active agents* card counts (R21), splatted rather than
                # retyped: the table and the card cannot answer differently.
                query = query.filter(*SalesAgentAccountService.active_agent_clauses())
            else:
                # R36: "inactive" is the exact COMPLEMENT — every agent with a profile who is
                # not in the roster. `is_active IS FALSE` alone left a suspended agent whose
                # profile was still switched on out of BOTH filters, i.e. a row the page could
                # only show with the filter cleared.
                query = query.filter(
                    or_(SalesAgentProfile.is_active.is_(False), User.status != UserStatus.ACTIVE.value)
                )
        pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
        user_ids = [u.id for u in pagination.items]
        profiles = (
            {p.user_id: p for p in SalesAgentProfile.query.filter(SalesAgentProfile.user_id.in_(user_ids)).all()}
            if user_ids
            else {}
        )
        counts = AgentMetricsService.today_counters(user_ids)
        items = [SalesAgentAccountService.serialize(u, profiles.get(u.id), counts.get(u.id)) for u in pagination.items]

        total_agents = User.query.filter(SalesAgentAccountService.member_filter()).count()
        # `active_agent_users`, NOT `active_agents`: a local of that name inside this method would
        # shadow the staticmethod for the rest of the scope, so the call below would try to invoke
        # the list it had just assigned. The published KEY stays "active_agents".
        active_agent_users = SalesAgentAccountService.active_agents()
        # R33: the *Sales agents* cards read these four keys. today_counters takes the whole roster
        # in one call — the same roster (R21) the snapshot and the weekly report count — so the card
        # and the report can never disagree, and paging the table never moves a card.
        estate_counters = AgentMetricsService.today_counters([user.id for user in active_agent_users])
        return (
            items,
            pagination.total,
            {
                "total_agents": int(total_agents),
                "active_agents": len(active_agent_users),
                "visits_today": sum(int(row.get("visits_today", 0)) for row in estate_counters.values()),
                "orders_today": sum(int(row.get("orders_today", 0)) for row in estate_counters.values()),
            },
        )

    @staticmethod
    def get_agent(user_id: int) -> Dict[str, Any]:
        user = User.query.get(user_id)
        profile = SalesAgentAccountService.profile_for(user_id)
        if user is None or profile is None:
            raise NotFoundError("Sales agent not found", error_code="STAFF_USER_NOT_FOUND")
        counts = AgentMetricsService.today_counters([user_id]).get(user_id)
        return SalesAgentAccountService.serialize(user, profile, counts)

    @staticmethod
    def set_active(user_id: int, is_active: bool, actor_id: int) -> Dict[str, Any]:
        profile = SalesAgentAccountService.profile_for(user_id)
        if profile is None:
            raise NotFoundError("Sales agent not found", error_code="STAFF_USER_NOT_FOUND")
        profile.is_active = bool(is_active)
        db.session.commit()
        StaffService._log_activity(
            user_id=actor_id,
            action=STAFF_ACTIONS["DELIVERY_STATUS_UPDATED"],
            entity_type="user",
            entity_id=user_id,
            metadata_={"operation": "sales_agent_set_active", "is_active": bool(is_active)},
        )
        return SalesAgentAccountService.get_agent(user_id)
