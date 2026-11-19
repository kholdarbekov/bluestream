"""A driver records a cash expense claim on his active trip (spec 2026-11-17 §6.3).

The claim is capped by the trip's "Hand in now" (``DriverReconciliationService.cash_held_now``), the one answer to
"how much cash does he still hold for the office". The options read publishes that figure from the stored figures
without a lock, so the bot can refuse a bigger number as he types it; ``submit`` checks it again under the session
lock after a refresh, and that check is the rule.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.delivery import DeliveryPerson
from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import DriverCashSession
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.tasks.staff_tasks import alert_driver_expense_claimed
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc_or_none
from business_app.utils.transactions import atomic_transaction
from business_app.utils.validation_helpers import parse_money_amount, strip_reason
from shared.business_config import DRIVER_EXPENSE_DECIDED_SHOWN
from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES, DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES

ZERO = Decimal("0.00")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimResult:
    """A submit's outcome: the claim (the one already recorded under this key when ``replayed``), and its trip's
    "Hand in now" (never below 0) and waiting total, for the bot's confirmation screen."""

    expense: DriverCashExpense
    replayed: bool
    hand_in_now: Decimal
    expenses_waiting_total: Decimal


@dataclass(frozen=True)
class _ClaimInputs:
    """One request's validated body."""

    category: str
    amount: Decimal
    note: Optional[str]
    receipt_file_id: Optional[str]
    idempotency_key: str


def _parse_body(body: Mapping[str, Any]) -> _ClaimInputs:
    """Validate the body in the spec's order. Each refusal is a 400 with its own code."""
    if not isinstance(body, Mapping):
        body = {}
    category = body.get("category")
    if category not in DRIVER_EXPENSE_CATEGORIES:
        raise ValidationError(
            f"The category must be one of: {', '.join(DRIVER_EXPENSE_CATEGORIES)}",
            error_code="DRIVER_EXPENSE_CATEGORY_INVALID",
        )
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise ValidationError("The note must be text", error_code="DRIVER_EXPENSE_NOTE_REQUIRED")
    # Blank (spaces, newlines, tabs) is no note: decided here, because Postgres trim() strips only spaces.
    note = strip_reason(note, max_length=DriverCashExpense.NOTE_MAX_LENGTH, error_code="DRIVER_EXPENSE_NOTE_TOO_LONG")
    if note is None and category in DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES:
        raise ValidationError("Write what the money was paid for", error_code="DRIVER_EXPENSE_NOTE_REQUIRED")
    amount = parse_money_amount(body.get("amount"), error_code="DRIVER_EXPENSE_AMOUNT_INVALID")
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= DriverCashExpense.IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValidationError(
            f"An idempotency key of 1 to {DriverCashExpense.IDEMPOTENCY_KEY_MAX_LENGTH} characters is required",
            error_code="DRIVER_EXPENSE_IDEMPOTENCY_KEY_REQUIRED",
        )
    receipt = body.get("receipt_file_id")
    if receipt is not None and (
        not isinstance(receipt, str) or not 1 <= len(receipt) <= DriverCashExpense.RECEIPT_FILE_ID_MAX_LENGTH
    ):
        raise ValidationError(
            f"The receipt must be a file id of 1 to {DriverCashExpense.RECEIPT_FILE_ID_MAX_LENGTH} characters",
            error_code="DRIVER_EXPENSE_RECEIPT_INVALID",
        )
    return _ClaimInputs(category=category, amount=amount, note=note, receipt_file_id=receipt, idempotency_key=key)


def _trip_cash(recon: DriverReconciliationService, session: DriverCashSession) -> Tuple[Decimal, Decimal]:
    """(the trip's "Hand in now", unclamped; the claims waiting on it), read from its stored figures."""
    waiting = recon.expense_totals_for_sessions([session.id])[session.id].waiting
    return recon.cash_held_now(session, waiting=waiting), waiting


def _claim_by_key(driver_user_id: int, idempotency_key: str) -> Optional[DriverCashExpense]:
    """The claim this driver already recorded under this key (unique per driver), or None."""
    return DriverCashExpense.query.filter_by(driver_user_id=driver_user_id, idempotency_key=idempotency_key).first()


def _replay(recon: DriverReconciliationService, expense: DriverCashExpense, inputs: _ClaimInputs) -> ClaimResult:
    """The answer to a key ``expense`` already holds: that claim again, when this is the same claim.

    The same claim means the same category and amount. The note and the photo are not compared, as the payout
    ignores its reason. Anything else under a used key is refused, never answered with a claim the driver did not
    send (``PrepaymentPayoutService._replay``).
    """
    if expense.category != inputs.category or Decimal(str(expense.amount)) != inputs.amount:
        raise ConflictError(
            f"This idempotency key already recorded claim {expense.id}, which differs from this request; "
            "start a new claim",
            error_code="DRIVER_EXPENSE_IDEMPOTENCY_KEY_REUSED",
        )
    held_now, waiting = _trip_cash(recon, expense.session)
    return ClaimResult(expense=expense, replayed=True, hand_in_now=max(ZERO, held_now), expenses_waiting_total=waiting)


def _fresh_location(driver_user_id: int, *, now: datetime) -> Tuple[Optional[float], Optional[float]]:
    """The driver's last known position (his ``DeliveryPerson`` row) when the one freshness rule
    (``RouteOptimizationService.location_status_for_person``) calls it fresh as of ``now``; (None, None) otherwise."""
    person = DeliveryPerson.query.filter_by(user_id=driver_user_id).first()
    if (
        RouteOptimizationService.location_status_for_person(person, now=now) != "fresh"
        or person.current_location_lng is None
    ):
        return None, None
    return person.current_location_lat, person.current_location_lng


def _audit_submitted(expense: DriverCashExpense) -> None:
    audit_logger.log_event(
        event_type=AuditEventType.PAYMENT_PROCESSED,
        action="driver_expense_submitted",
        severity=AuditSeverity.MEDIUM,
        resource_type="driver_cash_expense",
        resource_id=str(expense.id),
        additional_data={
            "expense_id": expense.id,
            "driver_user_id": expense.driver_user_id,
            "driver_cash_session_id": expense.driver_cash_session_id,
            "category": expense.category,
            "amount": str(expense.amount),
        },
    )


def _audit_withdrawn(expense: DriverCashExpense) -> None:
    audit_logger.log_event(
        event_type=AuditEventType.PAYMENT_PROCESSED,
        action="driver_expense_withdrawn",
        severity=AuditSeverity.MEDIUM,
        resource_type="driver_cash_expense",
        resource_id=str(expense.id),
        additional_data={
            "expense_id": expense.id,
            "driver_user_id": expense.driver_user_id,
            "driver_cash_session_id": expense.driver_cash_session_id,
            "amount": str(expense.amount),
        },
    )


class DriverExpenseClaims:
    """The driver's claim routes: what the flow needs before it starts, and recording a claim."""

    @staticmethod
    def options(driver_user_id: int) -> Dict[str, Any]:
        """The driver's active trip and its "Hand in now" as ``max_amount`` (0 without a trip).

        Read from the stored figures, without the session lock and without a refresh: a GET takes no lock. Every
        writer of an active trip refreshes it under that lock (spec 6.1), so they are current, and ``submit`` checks
        again under the lock.
        """
        recon = DriverReconciliationService()
        session = recon.find_active_session(driver_user_id)
        if session is None:
            return {"has_active_trip": False, "session_id": None, "max_amount": 0.0, "expenses_waiting_total": 0.0}
        held_now, waiting = _trip_cash(recon, session)
        return {
            "has_active_trip": True,
            "session_id": session.id,
            "max_amount": float(max(ZERO, held_now)),
            "expenses_waiting_total": float(waiting),
        }

    @staticmethod
    def submit(driver_user_id: int, body: Mapping[str, Any]) -> ClaimResult:
        """Record one claim on the driver's active trip, in the spec's order (6.3).

        A key this driver already used answers that claim again (``replayed``) when it is the same claim, before any
        trip or cash check, so a resend after the claim used up all his cash still answers it. The audit entry is
        published only after the commit, and only for a new claim; so is the Slack alert.
        """
        inputs = _parse_body(body)
        recon = DriverReconciliationService()

        replay = _claim_by_key(driver_user_id, inputs.idempotency_key)
        if replay is not None:
            return _replay(recon, replay, inputs)

        expense: Optional[DriverCashExpense] = None
        try:
            with atomic_transaction():
                session = recon.find_active_session(driver_user_id, for_update=True)
                if session is None:
                    raise ConflictError(
                        "You have no open trip, so there is no trip cash to spend",
                        error_code="DRIVER_EXPENSE_NO_ACTIVE_TRIP",
                    )
                # Re-checked under the lock: a double tap waited here behind the first.
                replay = _claim_by_key(driver_user_id, inputs.idempotency_key)
                if replay is None:
                    recon.refresh_expected_cash(session)
                    held_now, _ = _trip_cash(recon, session)
                    if inputs.amount > held_now:
                        raise ConflictError(
                            "The amount is more than the cash the driver holds for this trip",
                            details={"max_amount": float(max(ZERO, held_now))},
                            error_code="DRIVER_EXPENSE_ABOVE_CASH_HELD",
                        )
                    now = datetime.now(UTC)
                    latitude, longitude = _fresh_location(driver_user_id, now=now)
                    expense = DriverCashExpense(
                        driver_user_id=driver_user_id,
                        driver_cash_session_id=session.id,
                        category=inputs.category,
                        amount=inputs.amount,
                        note=inputs.note,
                        receipt_file_id=inputs.receipt_file_id,
                        latitude=latitude,
                        longitude=longitude,
                        status=DriverCashExpense.PENDING,
                        idempotency_key=inputs.idempotency_key,
                        submitted_at=now,
                    )
                    db.session.add(expense)
                    db.session.flush()
                    hand_in_now, waiting = _trip_cash(recon, session)
        except IntegrityError:
            # The unique key: a concurrent claim with this key committed first.
            replay = _claim_by_key(driver_user_id, inputs.idempotency_key)
            if replay is None:
                raise
        if replay is not None:
            return _replay(recon, replay, inputs)

        _audit_submitted(expense)
        try:
            alert_driver_expense_claimed.delay(expense.id)
        except Exception:  # noqa: BLE001 -- after the commit: the claim is recorded; the office still sees it waiting
            logger.warning("[EXPENSE] Slack alert for claim %s was not queued", expense.id, exc_info=True)
        return ClaimResult(
            expense=expense, replayed=False, hand_in_now=max(ZERO, hand_in_now), expenses_waiting_total=waiting
        )

    @staticmethod
    def withdraw(driver_user_id: int, expense_id: int) -> ClaimResult:
        """The driver takes back a claim that is still waiting, which gives its amount back to "Hand in now".

        Lock order (spec 6.1): the expense row first, the trip's session row last, through ``lock_session`` and never
        ``find_active_session``, which would take the session before the expense. The audit entry is published after
        the commit.
        """
        recon = DriverReconciliationService()
        with atomic_transaction():
            expense = (
                DriverCashExpense.query.filter_by(id=expense_id, driver_user_id=driver_user_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if expense is None:
                raise NotFoundError("Expense claim not found", error_code="DRIVER_EXPENSE_NOT_FOUND")
            if expense.status != DriverCashExpense.PENDING:
                raise ConflictError(
                    f"This claim is {expense.status}, so it can't be withdrawn",
                    error_code="DRIVER_EXPENSE_NOT_WITHDRAWABLE",
                )
            session = recon.lock_session(expense.driver_cash_session_id)
            recon.refresh_expected_cash(session)
            expense.status = DriverCashExpense.WITHDRAWN
            expense.withdrawn_at = datetime.now(UTC)
            db.session.flush()
            held_now, waiting = _trip_cash(recon, session)
        _audit_withdrawn(expense)
        return ClaimResult(
            expense=expense, replayed=False, hand_in_now=max(ZERO, held_now), expenses_waiting_total=waiting
        )

    @staticmethod
    def list_for_driver(driver_user_id: int) -> Dict[str, Any]:
        """The driver's My expenses screen: every waiting claim, the last ``DRIVER_EXPENSE_DECIDED_SHOWN`` finished
        ones (approved, rejected and withdrawn, newest first; a withdrawn claim has no ``decided_at``, so it sorts by
        ``withdrawn_at``) and the active trip's figures, read like ``options`` without a lock (0.0 without a trip)."""
        waiting_rows = (
            DriverCashExpense.query.filter_by(driver_user_id=driver_user_id, status=DriverCashExpense.PENDING)
            .order_by(DriverCashExpense.submitted_at.desc(), DriverCashExpense.id.desc())
            .all()
        )
        finished_at = func.coalesce(DriverCashExpense.decided_at, DriverCashExpense.withdrawn_at)
        decided_rows = (
            DriverCashExpense.query.filter(
                DriverCashExpense.driver_user_id == driver_user_id,
                DriverCashExpense.status != DriverCashExpense.PENDING,
            )
            .order_by(finished_at.desc(), DriverCashExpense.id.desc())
            .limit(DRIVER_EXPENSE_DECIDED_SHOWN)
            .all()
        )
        figures = DriverExpenseClaims.options(driver_user_id)
        return {
            "waiting": [DriverExpenseClaims.driver_row(row) for row in waiting_rows],
            "decided": [DriverExpenseClaims.driver_row(row) for row in decided_rows],
            "hand_in_now": figures["max_amount"],
            "expenses_waiting_total": figures["expenses_waiting_total"],
        }

    @staticmethod
    def driver_row(expense: DriverCashExpense) -> Dict[str, Any]:
        """A claim as the driver's screens read it. Money is a float (or None); times are UTC ISO-8601."""
        submitted_at = ensure_utc_or_none(expense.submitted_at)
        decided_at = ensure_utc_or_none(expense.decided_at)
        return {
            "id": expense.id,
            "category": expense.category,
            "amount": float(expense.amount),
            "approved_amount": float(expense.approved_amount) if expense.approved_amount is not None else None,
            "note": expense.note,
            "has_receipt": expense.receipt_file_id is not None,
            "status": expense.status,
            "submitted_at": submitted_at.isoformat() if submitted_at else None,
            "decided_at": decided_at.isoformat() if decided_at else None,
            "decision_reason": expense.decision_reason,
            "driver_cash_session_id": expense.driver_cash_session_id,
        }

    @staticmethod
    def answer(result: ClaimResult) -> Dict[str, Any]:
        """The submit route's body."""
        return {
            "expense": DriverExpenseClaims.driver_row(result.expense),
            "replayed": result.replayed,
            "hand_in_now": float(result.hand_in_now),
            "expenses_waiting_total": float(result.expenses_waiting_total),
        }
