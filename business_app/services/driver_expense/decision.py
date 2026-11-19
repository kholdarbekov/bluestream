"""The office decides a driver's expense claim (spec 2026-11-17 §6.4): the preview, approve (in full or for less),
reject, and undo an approval.

One ``plan`` serves the preview and the approve, so the dialog's effect line is what the approval does. Every refusal
comes from ``rules``, the functions the admin row publishes its flags from, so a button can never promise what its
route refuses. Each write locks the expense row first and the trip's session row last (§6.6), refreshes the trip, then
decides and writes; the audit entries and the driver's push go out only after the commit.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from business_app import db
from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import DriverCashSession
from business_app.models.user import User
from business_app.services.driver_expense import rules
from business_app.services.driver_expense.review import DriverExpenseReview
from business_app.services.driver_reconciliation_service import CashFigures, DriverReconciliationService
from business_app.services.self_decision import check_self_decision
from business_app.tasks.staff_tasks import push_expense_event
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.transactions import atomic_transaction
from business_app.utils.validation_helpers import parse_money_amount, strip_reason

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class DecisionEffect:
    """What approving the claim does to its trip: on hand and "Hand in now" before and after, and whether the trip
    closes (``session_closes``, the handoff's own rule)."""

    session_id: int
    on_hand_before: Decimal
    on_hand_after: Decimal
    hand_in_now_before: Decimal
    hand_in_now_after: Decimal
    will_close: bool


@dataclass(frozen=True)
class DecisionPlan:
    """An approval's effect, every reason it is refused now (empty when it may go ahead), and its maximum."""

    effect: DecisionEffect
    blocking_codes: Tuple[str, ...]
    approve_max_amount: Decimal


def _require_reason(value: Any) -> str:
    """The reason a reject or an undo is recorded with: text, stripped, ``REASON_MIN_LENGTH`` to ``REASON_MAX_LENGTH``
    characters. Not text, blank or too short → DRIVER_EXPENSE_REASON_REQUIRED; too long → ..._REASON_TOO_LONG."""
    if value is not None and not isinstance(value, str):
        raise ValidationError("The reason must be text", error_code="DRIVER_EXPENSE_REASON_REQUIRED")
    reason = strip_reason(
        value, max_length=DriverCashExpense.REASON_MAX_LENGTH, error_code="DRIVER_EXPENSE_REASON_TOO_LONG"
    )
    if reason is None or len(reason) < DriverCashExpense.REASON_MIN_LENGTH:
        raise ValidationError(
            f"A reason of at least {DriverCashExpense.REASON_MIN_LENGTH} characters is required",
            error_code="DRIVER_EXPENSE_REASON_REQUIRED",
        )
    return reason


def _parse_approved_amount(value: Any) -> Decimal:
    return parse_money_amount(value, error_code="DRIVER_EXPENSE_AMOUNT_INVALID")


def _not_found() -> NotFoundError:
    return NotFoundError("Expense claim not found", error_code="DRIVER_EXPENSE_NOT_FOUND")


def _blocked(code: str) -> ConflictError:
    return ConflictError(
        f"This can't be done to the claim now: {code}",
        error_code="DRIVER_EXPENSE_DECISION_BLOCKED",
        details={"block_code": code},
    )


def _money(value: Optional[Any]) -> Optional[float]:
    return float(value) if value is not None else None


def _effect_answer(effect: DecisionEffect) -> Dict[str, Any]:
    """The effect as the route publishes it: money as floats."""
    return {key: float(value) if isinstance(value, Decimal) else value for key, value in asdict(effect).items()}


def _lock(
    recon: DriverReconciliationService, expense_id: int, actor_user_id: int
) -> Tuple[DriverCashExpense, User, bool, DriverCashSession]:
    """The claim and its trip under their locks, in the feature's order (the expense row, then the session row), the
    trip refreshed, after the self-decision guard (403). Returns (claim, actor, whether it is his own, trip)."""
    expense = DriverCashExpense.query.filter_by(id=expense_id).with_for_update().populate_existing().first()
    if expense is None:
        raise _not_found()
    actor = db.session.get(User, actor_user_id)
    self_decided = check_self_decision(
        expense.driver_user_id,
        actor,
        error_code="DRIVER_EXPENSE_SELF_DECISION",
        message="You cannot decide your own expense",
        details={"expense_id": expense.id},
    )
    session = recon.lock_session(expense.driver_cash_session_id)
    recon.refresh_expected_cash(session)
    return expense, actor, self_decided, session


def _audit(action: str, expense: DriverCashExpense, *, actor_user_id: int, self_decided: bool, **extra: Any) -> None:
    audit_logger.log_event(
        event_type=AuditEventType.PAYMENT_PROCESSED,
        action=action,
        severity=AuditSeverity.MEDIUM,
        resource_type="driver_cash_expense",
        resource_id=str(expense.id),
        additional_data={
            "expense_id": expense.id,
            "driver_user_id": expense.driver_user_id,
            "driver_cash_session_id": expense.driver_cash_session_id,
            "amount": str(expense.amount),
            "approved_amount": str(expense.approved_amount) if expense.approved_amount is not None else None,
            "self_decided": self_decided,
            "actor_user_id": actor_user_id,
            **extra,
        },
    )


def _push(expense: DriverCashExpense, event: str, *, reason: Optional[str], hand_in_now: Decimal) -> None:
    """Tell the driver in his staff-bot chat, when he has one. After the commit: a broker that refuses the publish
    must not turn a recorded decision into an error; his My expenses screen still shows it."""
    driver = expense.driver_user
    if not driver.telegram_id:
        return
    # What an approval for less leaves the driver to hand in; the bot prints it and never subtracts.
    difference = (
        float(Decimal(str(expense.amount)) - Decimal(str(expense.approved_amount)))
        if event == "approved_less" and expense.approved_amount is not None
        else None
    )
    payload = {
        "expense_id": expense.id,
        "category": expense.category,
        "amount": float(expense.amount),
        "approved_amount": _money(expense.approved_amount),
        "difference": difference,
        "reason": reason,
        "hand_in_now": float(hand_in_now),
    }
    try:
        push_expense_event.delay(int(driver.telegram_id), event, payload)
    except Exception:  # noqa: BLE001 -- after the commit
        logger.exception("[EXPENSE] %s push for claim %s was not queued", event, expense.id)


def _tell_driver(
    recon: DriverReconciliationService,
    expense: DriverCashExpense,
    session: DriverCashSession,
    actor: User,
    *,
    event: str,
    reason: Optional[str],
) -> Tuple[Dict[str, Any], Decimal]:
    """After a decision has committed: push it to the driver with his trip's "Hand in now" (never below 0), and
    return the claim's row as ``actor`` now reads it, with that figure."""
    held_now = recon.cash_held_now(session)
    hand_in_now = max(ZERO, held_now)
    _push(expense, event, reason=reason, hand_in_now=hand_in_now)
    return DriverExpenseReview.row(expense, viewer=actor, held_now=held_now), hand_in_now


class DriverExpenseDecision:
    """The approve dialog's preview and the decision writes behind the admin Driver expenses page."""

    @staticmethod
    def plan(expense: DriverCashExpense, approved_amount: Decimal, actor: User, *, held_now: Decimal) -> DecisionPlan:
        """What approving ``approved_amount`` of the claim would do, and why ``actor`` may not now. ``held_now`` is
        its trip's ``cash_held_now``. Tests no condition itself: the codes are ``rules.decide_blocks`` followed by
        ``rules.approve_amount_block_code``."""
        codes = rules.decide_blocks(expense, actor)
        amount_code = rules.approve_amount_block_code(expense, approved_amount, held_now)
        if amount_code is not None:
            codes += (amount_code,)
        figures = CashFigures.from_session(expense.session)
        on_hand_after = figures.on_hand - approved_amount
        effect = DecisionEffect(
            session_id=expense.driver_cash_session_id,
            on_hand_before=figures.on_hand,
            on_hand_after=on_hand_after,
            hand_in_now_before=held_now,
            hand_in_now_after=held_now + (Decimal(str(expense.amount)) - approved_amount),
            will_close=DriverReconciliationService.session_closes(
                handed_in=figures.handed_in, on_hand=on_hand_after, has_live_payout=True
            ),
        )
        return DecisionPlan(
            effect=effect, blocking_codes=codes, approve_max_amount=rules.approve_max_amount(expense, held_now)
        )

    @staticmethod
    def preview(expense_id: int, approved_amount: Any, actor_user_id: int) -> Dict[str, Any]:
        """The approve dialog's effect line, read like the row: from the trip's stored figures, without a lock, and
        writing nothing. Approve plans again under the lock."""
        amount = _parse_approved_amount(approved_amount)
        expense = db.session.get(DriverCashExpense, expense_id)
        if expense is None:
            raise _not_found()
        held_now = DriverReconciliationService().cash_held_now(expense.session)
        plan = DriverExpenseDecision.plan(expense, amount, db.session.get(User, actor_user_id), held_now=held_now)
        return {
            "effect": _effect_answer(plan.effect),
            "blocking_codes": list(plan.blocking_codes),
            "approve_max_amount": float(plan.approve_max_amount),
        }

    @staticmethod
    def approve(expense_id: int, approved_amount: Any, actor_user_id: int) -> Dict[str, Any]:
        """Approve the claim for ``approved_amount`` (at most the claim), refused with the plan's first code. The
        amount comes off the trip's on hand; when nothing is left to hand in the trip closes and the next one opens
        (``apply_payout_effect``, the one non-handoff closer)."""
        amount = _parse_approved_amount(approved_amount)
        recon = DriverReconciliationService()
        with atomic_transaction():
            expense, actor, self_decided, session = _lock(recon, expense_id, actor_user_id)
            plan = DriverExpenseDecision.plan(expense, amount, actor, held_now=recon.cash_held_now(session))
            if plan.blocking_codes:
                raise _blocked(plan.blocking_codes[0])
            expense.status = DriverCashExpense.APPROVED
            expense.approved_amount = amount
            expense.decided_by_user_id = actor.id
            expense.decided_at = datetime.now(UTC)
            expense.self_decided = self_decided
            db.session.flush()
            next_session = recon.apply_payout_effect(session, actor_user_id=actor.id)

        _audit("driver_expense_approved", expense, actor_user_id=actor.id, self_decided=self_decided)
        if next_session is not None:
            DriverReconciliationService.audit_session_closed_by_cash_out(
                session, next_session, actor_user_id=actor.id, cause="expense", cause_id=expense.id
            )
        event = "approved" if amount == Decimal(str(expense.amount)) else "approved_less"
        row, _ = _tell_driver(recon, expense, session, actor, event=event, reason=None)
        return {
            "expense": row,
            "effect": _effect_answer(plan.effect),
            "next_session_id": next_session.id if next_session is not None else None,
        }

    @staticmethod
    def reject(expense_id: int, reason: Any, actor_user_id: int) -> Dict[str, Any]:
        """Reject the claim with a reason. It leaves the waiting line, so its amount goes back to "Hand in now".
        Refused with ``rules.decide_block_code``, the row's own flag (``not_pending`` or ``trip_not_active``: the
        self-decision guard answers first)."""
        reason = _require_reason(reason)
        recon = DriverReconciliationService()
        with atomic_transaction():
            expense, actor, self_decided, session = _lock(recon, expense_id, actor_user_id)
            code = rules.decide_block_code(expense, actor)
            if code is not None:
                raise _blocked(code)
            expense.status = DriverCashExpense.REJECTED
            expense.decided_by_user_id = actor.id
            expense.decided_at = datetime.now(UTC)
            expense.self_decided = self_decided
            expense.decision_reason = reason
            db.session.flush()
            recon.refresh_expected_cash(session)

        _audit("driver_expense_rejected", expense, actor_user_id=actor.id, self_decided=self_decided, reason=reason)
        row, hand_in_now = _tell_driver(recon, expense, session, actor, event="rejected", reason=reason)
        return {"expense": row, "hand_in_now": float(hand_in_now)}

    @staticmethod
    def undo(expense_id: int, reason: Any, actor_user_id: int) -> Dict[str, Any]:
        """Put an approved claim back to waiting, with a reason; the office then decides it again. Refused with
        ``rules.expense_undo_block_reason``, read under the lock after the refresh. Never reopens or closes a trip;
        the audit entry keeps the undone approval."""
        reason = _require_reason(reason)
        recon = DriverReconciliationService()
        with atomic_transaction():
            expense, actor, self_decided, session = _lock(recon, expense_id, actor_user_id)
            code = rules.expense_undo_block_reason(expense, recon.cash_held_now(session))
            if code is not None:
                raise _blocked(code)
            undone_approved_amount = str(expense.approved_amount)
            expense.status = DriverCashExpense.PENDING
            expense.approved_amount = None
            expense.decided_by_user_id = None
            expense.decided_at = None
            expense.decision_reason = None
            expense.self_decided = False
            db.session.flush()
            recon.refresh_expected_cash(session)

        _audit(
            "driver_expense_approval_undone",
            expense,
            actor_user_id=actor.id,
            self_decided=self_decided,
            reason=reason,
            undone_approved_amount=undone_approved_amount,
        )
        row, hand_in_now = _tell_driver(recon, expense, session, actor, event="approval_undone", reason=reason)
        return {"expense": row, "hand_in_now": float(hand_in_now)}
