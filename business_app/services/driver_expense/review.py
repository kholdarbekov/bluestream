"""The admin's view of driver expense claims (spec 2026-11-17 §6.8): the Driver expenses page's list, one claim, and
the flags each row publishes.

Read-only and without a lock, like the session screens: a trip's "Hand in now" is ``cash_held_now`` over its stored
figures, and the decision writes check again under the lock. Every flag comes from ``rules``, the decision routes' own
rules, so the page derives nothing. The list reads the claims waiting on its page's trips in one grouped read, and the
detail reads its trip the same way, so the list and the dialog publish the same figures.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Query, joinedload

from business_app import db
from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import DriverCashSession
from business_app.models.user import User
from business_app.serializers.driver_expense_serializers import DriverExpenseListQuery
from business_app.serializers.sales_serializers import _iso
from business_app.services.driver_expense import rules
from business_app.services.driver_expense.claims import DriverExpenseClaims
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.self_decision import self_decision_refused
from business_app.utils.exceptions import NotFoundError
from business_app.utils.local_windows import local_date, local_day_bounds
from shared.enums import DriverCashSessionStatus
from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES

# How many of the driver's closed trips the approve dialog shows.
RECENT_CLOSED_TRIPS = 2

_ROW_LOADS = (
    joinedload(DriverCashExpense.session),
    joinedload(DriverCashExpense.driver_user),
    joinedload(DriverCashExpense.decided_by),
)


def _recorded_in(query: Query, window: Optional[Tuple[datetime, datetime]]) -> Query:
    """``query`` narrowed to the claims recorded (``submitted_at``) in ``[start, end)``; unchanged without a window."""
    if window is None:
        return query
    start, end = window
    return query.filter(DriverCashExpense.submitted_at >= start, DriverCashExpense.submitted_at < end)


def _held_now_by_session(sessions: Iterable[DriverCashSession]) -> Dict[int, Decimal]:
    """Each trip's "Hand in now" (unclamped) from its stored figures, with ONE grouped read of the claims waiting."""
    recon = DriverReconciliationService()
    by_id = {session.id: session for session in sessions}
    totals = recon.expense_totals_for_sessions(list(by_id))
    return {sid: recon.cash_held_now(session, waiting=totals[sid].waiting) for sid, session in by_id.items()}


def _approved_by_category(window: Optional[Tuple[datetime, datetime]]) -> List[Dict[str, Any]]:
    """The amount approved per category on the claims recorded in ``window``: every category, in tuple order."""
    query = db.session.query(DriverCashExpense.category, func.sum(DriverCashExpense.approved_amount)).filter(
        DriverCashExpense.status == DriverCashExpense.APPROVED
    )
    sums = dict(_recorded_in(query, window).group_by(DriverCashExpense.category).all())
    return [
        {"category": category, "approved_total": float(sums.get(category) or 0)}
        for category in DRIVER_EXPENSE_CATEGORIES
    ]


def _possible_duplicates(expense: DriverCashExpense) -> List[Dict[str, Any]]:
    """The driver's other waiting or approved claims for the same amount, recorded on the same Tashkent day."""
    day = local_day_bounds(local_date(expense.submitted_at))
    others = _recorded_in(
        DriverCashExpense.query.filter(
            DriverCashExpense.driver_user_id == expense.driver_user_id,
            DriverCashExpense.amount == expense.amount,
            DriverCashExpense.status.in_([DriverCashExpense.PENDING, DriverCashExpense.APPROVED]),
            DriverCashExpense.id != expense.id,
        ),
        day,
    ).order_by(DriverCashExpense.submitted_at, DriverCashExpense.id)
    return [
        {
            "id": other.id,
            "amount": float(other.amount),
            "status": other.status,
            "submitted_at": _iso(other.submitted_at),
        }
        for other in others
    ]


def _recent_closed_trips(driver_user_id: int) -> List[Dict[str, Any]]:
    """The driver's last ``RECENT_CLOSED_TRIPS`` trips that are no longer running, newest first, with the office's
    check result (``verified_variance``: a shortfall is negative)."""
    active = [DriverCashSessionStatus(status) for status in sorted(DriverReconciliationService.ACTIVE_STATUSES)]
    sessions = (
        DriverCashSession.query.filter(
            DriverCashSession.driver_user_id == driver_user_id,
            DriverCashSession.status.notin_(active),
        )
        .order_by(DriverCashSession.session_started_at.desc(), DriverCashSession.id.desc())
        .limit(RECENT_CLOSED_TRIPS)
        .all()
    )
    return [
        {
            "session_id": session.id,
            "status": DriverCashSessionStatus(session.status).value,
            "verified_variance": float(session.verified_variance or 0),
            "session_ended_at": _iso(session.session_ended_at),
        }
        for session in sessions
    ]


class DriverExpenseReview:
    """The Driver expenses page's reads."""

    @staticmethod
    def row(expense: DriverCashExpense, *, viewer: User, held_now: Decimal) -> Dict[str, Any]:
        """One claim as the admin page reads it: the driver's row, who and where, the decision, and what ``viewer``
        may do with it. ``held_now`` is its trip's ``cash_held_now``. Money is a float (or None); times are UTC
        ISO-8601."""
        decide_code = rules.decide_block_code(expense, viewer)
        approve_code = rules.approve_block_code(expense, viewer, held_now)
        max_amount = rules.approve_max_amount(expense, held_now)
        undo_code = rules.expense_undo_block_reason(expense, held_now) or (
            rules.SELF_DECISION if self_decision_refused(expense.driver_user_id, viewer) else None
        )
        decided_by = expense.decided_by
        return {
            **DriverExpenseClaims.driver_row(expense),
            "driver_user_id": expense.driver_user_id,
            "driver_name": expense.driver_user.full_name,
            "latitude": expense.latitude,
            "longitude": expense.longitude,
            "decided_by_user_id": expense.decided_by_user_id,
            "decided_by_name": decided_by.full_name if decided_by else None,
            "self_decided": bool(expense.self_decided),
            "withdrawn_at": _iso(expense.withdrawn_at),
            "can_approve": approve_code is None,
            "can_reject": decide_code is None,
            "can_undo": undo_code is None,
            "decide_block_code": decide_code,
            # Approve alone is also off for a claim the trip cash cannot cover at all (maximum 0); Reject is not.
            "approve_block_code": approve_code,
            "undo_block_code": undo_code,
            "approve_max_amount": float(max_amount),
        }

    @staticmethod
    def list_for_admin(params: DriverExpenseListQuery, viewer: User) -> Dict[str, Any]:
        """One page of the claims the filters select, newest first, with the badge count (every waiting claim,
        whatever the filters) and the amount approved per category on the claims recorded in the period."""
        window = params.recorded_window()
        query = DriverCashExpense.query
        if params.status != "all":
            query = query.filter(DriverCashExpense.status == params.status)
        if params.driver_id is not None:
            query = query.filter(DriverCashExpense.driver_user_id == params.driver_id)
        if params.category is not None:
            query = query.filter(DriverCashExpense.category == params.category)
        query = _recorded_in(query, window)
        total = query.count()
        expenses = (
            query.options(*_ROW_LOADS)
            .order_by(DriverCashExpense.submitted_at.desc(), DriverCashExpense.id.desc())
            .offset((params.page - 1) * params.per_page)
            .limit(params.per_page)
            .all()
        )
        held_now = _held_now_by_session(expense.session for expense in expenses)
        return {
            "items": [
                DriverExpenseReview.row(expense, viewer=viewer, held_now=held_now[expense.driver_cash_session_id])
                for expense in expenses
            ],
            "page": params.page,
            "per_page": params.per_page,
            "total": total,
            "pending_count": DriverCashExpense.query.filter_by(status=DriverCashExpense.PENDING).count(),
            "totals_by_category": _approved_by_category(window),
            "reason_min_length": DriverCashExpense.REASON_MIN_LENGTH,
        }

    @staticmethod
    def detail(expense_id: int, viewer: User) -> Dict[str, Any]:
        """One claim, as the approve dialog opens it: its row, the warnings (information only, never blocking) and
        the reason minimum for Reject and Undo."""
        expense = DriverCashExpense.query.options(*_ROW_LOADS).filter_by(id=expense_id).first()
        if expense is None:
            raise NotFoundError("Expense claim not found", error_code="DRIVER_EXPENSE_NOT_FOUND")
        held_now = _held_now_by_session([expense.session])[expense.driver_cash_session_id]
        return {
            "expense": DriverExpenseReview.row(expense, viewer=viewer, held_now=held_now),
            "warnings": {
                "possible_duplicates": _possible_duplicates(expense),
                "recent_closed_trips": _recent_closed_trips(expense.driver_user_id),
            },
            "reason_min_length": DriverCashExpense.REASON_MIN_LENGTH,
        }
