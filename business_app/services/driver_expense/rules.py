"""Whether an expense claim can be decided or its approval undone, and for how much (spec 2026-11-17 §6.4, §6.8).

The one home of these rules. The admin row publishes its flags from them, and the decision writes refuse with the
same codes, so a button can never promise what its route refuses. Pure: every figure comes in as an argument.
``held_now`` is the trip's "Hand in now" (``DriverReconciliationService.cash_held_now``, unclamped), which already
counts every claim waiting on the trip, this one included while it waits.
"""

from decimal import Decimal
from typing import Optional, Tuple

from business_app.models.driver_expense import DriverCashExpense
from business_app.models.user import User
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.self_decision import self_decision_refused
from shared.enums import DriverCashSessionStatus

ZERO = Decimal("0.00")
# A non-admin's own claim: a decide code here, and the admin row's undo code for the same viewer.
SELF_DECISION = "self_decision"


def _amount(expense: DriverCashExpense) -> Decimal:
    return Decimal(str(expense.amount))


def _trip_active(expense: DriverCashExpense) -> bool:
    """Whether the claim's trip is still running (open, partial or overdue)."""
    return DriverCashSessionStatus(expense.session.status).value in DriverReconciliationService.ACTIVE_STATUSES


def decide_blocks(expense: DriverCashExpense, viewer: User) -> Tuple[str, ...]:
    """Every reason ``viewer`` may not approve or reject the claim now, in this order: ``not_pending`` (already
    decided or withdrawn), ``trip_not_active`` (its trip is closed or checked), ``self_decision`` (a non-admin's own
    claim). Empty when he may. Nothing else tests these three conditions."""
    codes = []
    if expense.status != DriverCashExpense.PENDING:
        codes.append("not_pending")
    if not _trip_active(expense):
        codes.append("trip_not_active")
    if self_decision_refused(expense.driver_user_id, viewer):
        codes.append(SELF_DECISION)
    return tuple(codes)


def decide_block_code(expense: DriverCashExpense, viewer: User) -> Optional[str]:
    """The first of ``decide_blocks``, or None when ``viewer`` may decide the claim."""
    blocks = decide_blocks(expense, viewer)
    return blocks[0] if blocks else None


def approve_max_amount(expense: DriverCashExpense, held_now: Decimal) -> Decimal:
    """The most the office may approve: the claim, capped by the trip cash the driver holds before this claim
    (``held_now`` plus its own amount, never below 0). Another claim waiting on the trip stays subtracted."""
    amount = _amount(expense)
    return min(amount, max(ZERO, held_now + amount))


def approve_amount_block_code(expense: DriverCashExpense, approved_amount: Decimal, held_now: Decimal) -> Optional[str]:
    """None when ``approved_amount`` is within ``approve_max_amount``; else ``amount_above_claim`` when it exceeds the
    claim, otherwise ``amount_above_trip_cash``. Both codes come from that one maximum, so the dialog's limit and the
    refusal cannot disagree."""
    if approved_amount <= approve_max_amount(expense, held_now):
        return None
    return "amount_above_claim" if approved_amount > _amount(expense) else "amount_above_trip_cash"


def approve_block_code(expense: DriverCashExpense, viewer: User, held_now: Decimal) -> Optional[str]:
    """Why Approve is off on the row, or None: the first ``decide_blocks`` code, else the amount code when the trip
    cash cannot cover any of the claim (``approve_max_amount`` 0). Reject and undo read ``decide_block_code`` only."""
    if decide_blocks(expense, viewer):
        return decide_block_code(expense, viewer)
    if approve_max_amount(expense, held_now) <= 0:
        return approve_amount_block_code(expense, _amount(expense), held_now)
    return None


def expense_undo_block_reason(expense: DriverCashExpense, held_now: Decimal) -> Optional[str]:
    """Why the approval can't be undone (the first that applies), or None: ``not_approved``; ``session_checked``
    (the trip is closed or checked); ``cash_already_handed_in`` (back at its full amount, the claim would take
    "Hand in now" below 0: the driver already handed in the difference an approval for less left him to hand in)."""
    if expense.status != DriverCashExpense.APPROVED:
        return "not_approved"
    if not _trip_active(expense):
        return "session_checked"
    if held_now - (_amount(expense) - Decimal(str(expense.approved_amount))) < 0:
        return "cash_already_handed_in"
    return None
