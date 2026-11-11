"""Return a customer's free prepaid credit to the customer as money (spec 2026-11-10).

A façade over :class:`CashCollectionService`'s credit ledger, the way ``OrderCashEditService`` is
one over its event adjustments. The ledger primitives (the pool, the event locks, the two
classifying clauses) stay there; this module owns the planner, the write and the payout reads.

What a return writes: one :class:`CustomerCreditPayout` header and one
:class:`CustomerCreditPayoutDebit` per credit event it draws, each lowering only that event's
``unapplied_amount``. Nothing rewrites, voids or negates a collection, so every balance read
(``get_customer_prepaid_balance``) follows by itself.

ONE planner, :meth:`PrepaymentPayoutService._plan`, answers both the preview and the apply, so the
two cannot drift. With ``lock=True`` it takes the locks in the feature's fixed order: lock 1, the
pool's credit events (``_locked_cluster_credit_events``), then lock 2, the grocery contract accounts
that mirror the drawn credit (``payout_mirror_plan``), then lock 3, the driver cash session. Every
figure it decides is read from those rows.

A grocery store on a money contract sees its credit twice, as prepaid credit and as contract credit;
a return posts the matching contract entry in the same transaction, or blocks (spec §4.2).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.corporate import CorporateContract
from business_app.models.delivery import DeliveryPerson
from business_app.models.order import Order
from business_app.models.payment import (
    CashCollectionEvent,
    CustomerCreditPayout,
    CustomerCreditPayoutDebit,
    DriverCashSession,
)
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.serializers.prepayment_payout_serializers import (
    ContractEffect,
    DrawnCreditRow,
    LiveSubscriptionWarning,
    PayoutApplyResponse,
    PayoutDebitRow,
    PayoutPlanSummary,
    PayoutRow,
    PayoutUndoResponse,
    PayoutWarnings,
    PendingOrderWarning,
    PlaceDebtWarning,
    SessionEffect,
)
from business_app.services.cash_collection_service import (
    PREPAYMENT_RETURN_PERMISSIONS,
    CashCollectionService,
    card_click_origin_event_clause,
    customer_credit_rebook_event_clause,
)
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.decorators import has_admin_permission
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.service_factory import get_corporate_contract_service
from business_app.utils.transactions import atomic_transaction
from business_app.utils.validation_helpers import strip_reason
from shared.enums import LIVE_SUBSCRIPTION_STATUSES

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
_AMOUNT_TEXT = re.compile(r"\d+(\.\d{1,2})?", re.ASCII)
REASON_MIN_LENGTH = 5
IDEMPOTENCY_KEY_MAX_LENGTH = 64


# --------------------------------------------------------------------------- input rules


def parse_payout_amount(value: Any, *, allow_zero: bool = False) -> Decimal:
    """A money figure from a JSON body: an int, a float, or digits with at most two decimals.

    More than zero, or zero too with ``allow_zero`` (a set-balance target). A bool, a sign, a space,
    a third decimal or anything else is refused with PAYOUT_AMOUNT_INVALID, never rounded.
    """
    invalid = ValidationError(
        (
            "The amount must be a number of at least 0 with at most two decimals"
            if allow_zero
            else "The amount must be a number above 0 with at most two decimals"
        ),
        error_code="PAYOUT_AMOUNT_INVALID",
    )
    if isinstance(value, bool):
        raise invalid
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(str(value))
    elif isinstance(value, str) and _AMOUNT_TEXT.fullmatch(value):
        number = Decimal(value)
    else:
        raise invalid
    try:
        if not number.is_finite() or number != number.quantize(CENT):
            raise invalid
        number = number.quantize(CENT)
    except InvalidOperation:  # too many digits to hold at all
        raise invalid
    if number < ZERO or (number == ZERO and not allow_zero):
        raise invalid
    return number


def parse_paid_at(value: Any, *, now: datetime) -> datetime:
    """When the money changed hands, in UTC. None is ``now``.

    Otherwise an ISO-8601 date-time that carries its offset (a trailing ``Z`` is UTC). A value
    without one, one that does not parse, one that leaves the calendar in UTC (year 1 at a positive
    offset) or one later than ``now`` is refused with PAYOUT_PAID_AT_INVALID.
    """
    if value is None:
        return now
    invalid = ValidationError(
        "Paid at must be a past date-time with its UTC offset", error_code="PAYOUT_PAID_AT_INVALID"
    )
    if not isinstance(value, str):
        raise invalid
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise invalid
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise invalid
    try:
        moment = moment.astimezone(UTC)
    except OverflowError:
        raise invalid
    if moment > now:
        raise invalid
    return moment


def require_payout_reason(value: Any) -> str:
    """The reason a return (or its undo) is recorded with: text, stripped, 5 to 500 characters.

    Not text, blank or shorter than 5 → PAYOUT_REASON_REQUIRED; longer than the column holds →
    PAYOUT_REASON_TOO_LONG (``strip_reason`` refuses it, never cuts it).
    """
    if value is not None and not isinstance(value, str):
        raise ValidationError("The reason must be text", error_code="PAYOUT_REASON_REQUIRED")
    reason = strip_reason(value, max_length=CustomerCreditPayout.REASON_MAX_LENGTH, error_code="PAYOUT_REASON_TOO_LONG")
    if reason is None or len(reason) < REASON_MIN_LENGTH:
        raise ValidationError(
            f"A reason of at least {REASON_MIN_LENGTH} characters is required", error_code="PAYOUT_REASON_REQUIRED"
        )
    return reason


def require_idempotency_key(value: Any) -> str:
    """The client's token for one return: text of 1 to 64 characters (PAYOUT_IDEMPOTENCY_KEY_REQUIRED)."""
    if not isinstance(value, str) or not value.strip() or len(value) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise ValidationError(
            f"An idempotency key of at most {IDEMPOTENCY_KEY_MAX_LENGTH} characters is required",
            error_code="PAYOUT_IDEMPOTENCY_KEY_REQUIRED",
        )
    return value


UNDO_BLOCK_TEXT = {
    "already_undone": "This return was already undone",
    "funding_event_voided": "A credit event this return drew from was voided, so its credit cannot be restored",
    "session_checked": "The driver's cash session this return came out of is already closed or checked",
}


def payout_undo_block_reason(payout: CustomerCreditPayout) -> Optional[str]:
    """The code that blocks undoing ``payout``, or None. The ONLY place undo eligibility is decided:
    the history row (``can_undo``/``undo_block_code``) and the undo route both read it. First match wins.

    A current-trip return can be undone only while the driver's funding session is still active; once
    it is SUBMITTED or later, undoing would leave a closed session with no declaration behind it.
    """
    if not payout.is_live:
        return "already_undone"
    if any(debit.event.voided_at is not None for debit in payout.debits):
        return "funding_event_voided"
    if payout.paid_by == CustomerCreditPayout.PAID_BY_DRIVER_CURRENT:
        session = payout.driver_cash_session
        if session is None or session.status.value not in DriverReconciliationService.ACTIVE_STATUSES:
            return "session_checked"
    return None


@dataclass(frozen=True)
class _PayoutInputs:
    """One request's validated inputs, as ``_plan`` takes them."""

    amount: Optional[Decimal]
    target_balance: Optional[Decimal]
    paid_by: str
    driver_user_id: Optional[int]
    paid_at: datetime


def _parse_inputs(
    *,
    amount: Any,
    target_balance: Any,
    paid_by: Any,
    driver_user_id: Any,
    paid_at: Any,
    now: datetime,
    require_amount: bool,
) -> _PayoutInputs:
    """Validate what preview and apply share. Each refusal is a 400 with its own code."""
    if amount is not None and target_balance is not None:
        raise ValidationError(
            "Give the amount to return or the balance to leave, not both", error_code="PAYOUT_AMOUNT_OR_TARGET"
        )
    if require_amount and amount is None and target_balance is None:
        raise ValidationError(
            "The amount to return or the balance to leave is required", error_code="PAYOUT_AMOUNT_INVALID"
        )
    parsed_amount = parse_payout_amount(amount) if amount is not None else None
    parsed_target = parse_payout_amount(target_balance, allow_zero=True) if target_balance is not None else None

    if paid_by not in CustomerCreditPayout.PAID_BY_VALUES:
        raise ValidationError(
            f"Paid by must be one of: {', '.join(CustomerCreditPayout.PAID_BY_VALUES)}",
            error_code="PAYOUT_PAID_BY_INVALID",
        )
    if paid_by == CustomerCreditPayout.PAID_BY_OFFICE:
        if driver_user_id is not None:
            raise ValidationError("A return paid by the office names no driver", error_code="PAYOUT_DRIVER_NOT_ALLOWED")
    elif isinstance(driver_user_id, bool) or not isinstance(driver_user_id, int):
        raise ValidationError("Choose the driver who handed the money over", error_code="PAYOUT_DRIVER_REQUIRED")

    return _PayoutInputs(
        amount=parsed_amount,
        target_balance=parsed_target,
        paid_by=paid_by,
        driver_user_id=driver_user_id,
        paid_at=parse_paid_at(paid_at, now=now),
    )


# --------------------------------------------------------------------------- the plan


@dataclass(frozen=True)
class DrawnCredit:
    """The part of one credit event a return draws, classified at plan time (P7, P9)."""

    event_id: int
    customer_id: int
    amount: Decimal
    revenue_counted: bool
    card_click: bool


@dataclass
class PayoutPlan:
    """What a return with these inputs would write, and what blocks it.

    ``warnings`` (Task 3) never block; ``session_effect`` is set for a return from the driver's current trip
    cash (Task 4); ``contract_effect`` lists the entries the return posts on grocery money contracts.
    The underscored fields are what the write acts on: on apply, the rows the planner locked, and the
    contract postings (contract id → amount) behind ``contract_effect``.
    """

    customer_id: int
    pool_user_ids: List[int]
    free_balance: Decimal
    amount: Decimal
    paid_by: str
    driver_user_id: Optional[int]
    paid_at: datetime
    target_balance: Optional[Decimal] = None
    drawn: List[DrawnCredit] = field(default_factory=list)
    warnings: PayoutWarnings = field(default_factory=PayoutWarnings)
    session_effect: Optional[SessionEffect] = None
    contract_effect: List[ContractEffect] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    _events_by_id: Dict[int, CashCollectionEvent] = field(default_factory=dict, repr=False)
    _session: Optional[DriverCashSession] = field(default=None, repr=False)
    _mirror_postings: Dict[int, Decimal] = field(default_factory=dict, repr=False)

    @property
    def card_click_amount(self) -> Decimal:
        return sum((credit.amount for credit in self.drawn if credit.card_click), ZERO)

    @property
    def revenue_counted_amount(self) -> Decimal:
        return sum((credit.amount for credit in self.drawn if credit.revenue_counted), ZERO)

    @property
    def is_applicable(self) -> bool:
        return not self.blocking_reasons

    def to_summary(self) -> Dict[str, Any]:
        return PayoutPlanSummary(
            customer_id=self.customer_id,
            pool_user_ids=self.pool_user_ids,
            free_balance=self.free_balance,
            amount=self.amount,
            target_balance=self.target_balance,
            paid_by=self.paid_by,
            driver_user_id=self.driver_user_id,
            paid_at=self.paid_at,
            drawn=[
                DrawnCreditRow(
                    event_id=credit.event_id,
                    customer_id=credit.customer_id,
                    amount=credit.amount,
                    revenue_counted=credit.revenue_counted,
                    card_click=credit.card_click,
                )
                for credit in self.drawn
            ],
            card_click_amount=self.card_click_amount,
            revenue_counted_amount=self.revenue_counted_amount,
            warnings=self.warnings,
            session_effect=self.session_effect,
            contract_effect=self.contract_effect,
            blocking_reasons=self.blocking_reasons,
            is_applicable=self.is_applicable,
        ).model_dump(mode="json")


@dataclass(frozen=True)
class PayoutPeriodTotals:
    """What live payouts recorded in a report window handed back, split by the stored debit flag (P9)."""

    revenue_counted: Decimal
    never_revenue: Decimal


@dataclass
class PayoutResult:
    """An apply's outcome. A replay carries the original payout and no plan."""

    payout: CustomerCreditPayout
    plan: Optional[PayoutPlan]
    replayed: bool
    next_session: Optional[DriverCashSession]


class PrepaymentPayoutService:
    """Preview, apply and read returns of a customer's free prepaid credit."""

    def __init__(self, *, cash_service=None, recon_service=None, contract_service=None) -> None:
        self.cash_service = cash_service or CashCollectionService()
        self.recon_service = recon_service or DriverReconciliationService()
        self.contract_service = contract_service or get_corporate_contract_service()

    # ---- preview ----
    def preview(
        self,
        *,
        customer_id: int,
        amount: Any = None,
        paid_by: Any,
        driver_user_id: Any = None,
        paid_at: Any = None,
        target_balance: Any = None,
    ) -> PayoutPlan:
        """The plan an apply with these inputs would carry out, read without locks. Writes nothing.

        An omitted amount (and no target) means the whole free balance.
        """
        inputs = _parse_inputs(
            amount=amount,
            target_balance=target_balance,
            paid_by=paid_by,
            driver_user_id=driver_user_id,
            paid_at=paid_at,
            now=datetime.now(UTC),
            require_amount=False,
        )
        return self._plan(customer_id=customer_id, **vars(inputs), lock=False)

    # ---- apply ----
    def apply(
        self,
        *,
        customer_id: int,
        amount: Any = None,
        paid_by: Any,
        driver_user_id: Any = None,
        paid_at: Any = None,
        reason: Any,
        idempotency_key: Any,
        actor_user_id: int,
        target_balance: Any = None,
    ) -> PayoutResult:
        """Record one return. A key already used for this same return answers that payout again (``replayed``)
        and writes nothing; a key already used for a different one is refused (``_replay``).

        The audit entry is published only after the transaction commits, and only for a new payout.
        """
        inputs = _parse_inputs(
            amount=amount,
            target_balance=target_balance,
            paid_by=paid_by,
            driver_user_id=driver_user_id,
            paid_at=paid_at,
            now=datetime.now(UTC),
            require_amount=True,
        )
        reason = require_payout_reason(reason)
        idempotency_key = require_idempotency_key(idempotency_key)

        replay = self._payout_by_key(idempotency_key)
        if replay is not None:
            return self._replay(replay, customer_id=customer_id, inputs=inputs)

        payout: Optional[CustomerCreditPayout] = None
        next_session: Optional[DriverCashSession] = None
        try:
            with atomic_transaction():
                plan = self._plan(customer_id=customer_id, **vars(inputs), lock=True)
                # Re-checked after the locks: a double submit waited on lock 1 behind the first.
                replay = self._payout_by_key(idempotency_key)
                if replay is None:
                    if plan.blocking_reasons:
                        raise ValidationError("; ".join(plan.blocking_reasons), error_code="PAYOUT_BLOCKED")
                    payout = self._write(
                        plan, reason=reason, idempotency_key=idempotency_key, actor_user_id=actor_user_id
                    )
                    if plan._mirror_postings:
                        # Still under lock 2: the grocery contract moves with the credit, in this transaction.
                        self.contract_service.post_payout_mirror(
                            payout.id, plan._mirror_postings, actor_user_id=actor_user_id
                        )
                    if plan._session is not None:
                        # The driver's session, still under lock 3: on hand drops; nothing left to hand in closes it.
                        next_session = self.recon_service.apply_payout_effect(
                            plan._session, actor_user_id=actor_user_id
                        )
        except IntegrityError:
            # The unique key: a concurrent apply with this key committed first.
            replay = self._payout_by_key(idempotency_key)
            if replay is None:
                raise
        if replay is not None:
            return self._replay(replay, customer_id=customer_id, inputs=inputs)

        self._audit_recorded(payout, plan, reason=reason, idempotency_key=idempotency_key, actor_user_id=actor_user_id)
        if next_session is not None:
            self._audit_session_closed(payout, next_session, actor_user_id=actor_user_id)
        self._notify_customer(payout)
        return PayoutResult(payout=payout, plan=plan, replayed=False, next_session=next_session)

    # ---- undo ----
    def undo(self, *, payout_id: int, reason: Any, actor_user_id: int) -> CustomerCreditPayout:
        """Undo one return: give back exactly the credit its debits drew, on the same events.

        Locks, in the feature's order: the payout header, its funding credit events (the ``must_hold`` arm,
        because a drained event has nothing unapplied and would not re-qualify as spendable), then the
        grocery contract accounts its entries sit on, then the driver's cash session when the return came out
        of his trip. Nothing is written before the undo is known to be allowed; then the credit, the contract
        entries (their inverse) and the session move together.
        The reservation of credit on pending orders is not redone and the customer is told nothing. The audit
        entry is published after the transaction commits.
        """
        reason = require_payout_reason(reason)
        with atomic_transaction():
            payout = (
                CustomerCreditPayout.query.filter(CustomerCreditPayout.id == payout_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if payout is None:
                raise NotFoundError("Prepayment return not found")
            debits = list(payout.debits)
            events = self.cash_service._lock_credit_events_by_ids(
                [], must_hold_event_ids=[debit.cash_collection_event_id for debit in debits]
            )
            # Lock 2: the grocery contract accounts this return posted on, before the session lock.
            mirror_rows = self.contract_service.lock_payout_mirror_accounts(payout.id)
            session = None
            if payout.driver_cash_session_id is not None:
                session = self.recon_service.lock_session(payout.driver_cash_session_id)
            block = payout_undo_block_reason(payout)
            if block is not None:
                raise ValidationError(f"{block}: {UNDO_BLOCK_TEXT[block]}", error_code="PAYOUT_UNDO_BLOCKED")
            to_dec = self.cash_service._to_decimal
            for debit in debits:
                self.cash_service._add_unapplied(events[debit.cash_collection_event_id], to_dec(debit.amount))
            self.contract_service.reverse_payout_mirror(payout.id, mirror_rows, actor_user_id=actor_user_id)
            payout.voided_at = datetime.now(UTC)
            payout.voided_by_user_id = actor_user_id
            payout.void_reason = reason
            db.session.flush()
            if session is not None:
                self.recon_service.refresh_expected_cash(session)
        self._audit_undone(payout, reason=reason, actor_user_id=actor_user_id)
        return payout

    # ---- reads ----
    def serialize_payout(self, payout: CustomerCreditPayout, *, actor: Optional[User]) -> Dict[str, Any]:
        """One history row, as ``actor`` (the admin reading it) may act on it."""
        return self._payout_row(payout, actor=actor).model_dump(mode="json")

    # ---- report reads ----
    @staticmethod
    def period_totals(start_dt: datetime, end_dt: datetime) -> PayoutPeriodTotals:
        """Live payouts RECORDED (``created_at``, not ``paid_at``) in the window, summed by the stored flag.

        The window bounds match the financial summary's cash-allocation window (inclusive both ends). The
        revenue/never-revenue split is the ``revenue_counted`` boolean stamped on each debit at apply time;
        it is never re-derived here. An undone payout leaves its period.
        """
        rows = (
            db.session.query(CustomerCreditPayoutDebit.revenue_counted, func.sum(CustomerCreditPayoutDebit.amount))
            .join(CustomerCreditPayout, CustomerCreditPayout.id == CustomerCreditPayoutDebit.payout_id)
            .filter(
                CustomerCreditPayout.is_live,
                CustomerCreditPayout.created_at >= start_dt,
                CustomerCreditPayout.created_at <= end_dt,
            )
            .group_by(CustomerCreditPayoutDebit.revenue_counted)
            .all()
        )
        by_flag = {bool(flag): Decimal(str(total or 0)) for flag, total in rows}
        return PayoutPeriodTotals(
            revenue_counted=by_flag.get(True, ZERO),
            never_revenue=by_flag.get(False, ZERO),
        )

    # ---- history reads ----
    @staticmethod
    def _pool_debits_query(pool_user_ids: Sequence[int]):
        """Debit rows drawn from events collected by the pool's accounts.

        THE rule for "whose payouts are these": the debit's EVENT decides, never ``payout.customer_id``. After
        an unlink the payout follows the account whose events were drawn. The payout rows, ``lifetime_returned``
        and ``returned_by_event`` all read this one query.
        """
        return (
            db.session.query(CustomerCreditPayoutDebit)
            .join(CashCollectionEvent, CashCollectionEvent.id == CustomerCreditPayoutDebit.cash_collection_event_id)
            .filter(CashCollectionEvent.customer_id.in_([int(uid) for uid in pool_user_ids]))
        )

    def pool_payouts(self, pool_user_ids: Sequence[int], *, include_voided: bool) -> List[CustomerCreditPayout]:
        """Payouts that drew the pool's events, newest ``paid_at`` first; undone ones only when asked for."""
        payout_ids = self._pool_debits_query(pool_user_ids).with_entities(CustomerCreditPayoutDebit.payout_id)
        query = CustomerCreditPayout.query.options(
            joinedload(CustomerCreditPayout.debits),
            joinedload(CustomerCreditPayout.driver_user),
            joinedload(CustomerCreditPayout.actor),
            joinedload(CustomerCreditPayout.driver_cash_session),
        ).filter(CustomerCreditPayout.id.in_(payout_ids))
        if not include_voided:
            query = query.filter(CustomerCreditPayout.is_live)
        return query.order_by(CustomerCreditPayout.paid_at.desc(), CustomerCreditPayout.id.desc()).all()

    def lifetime_returned(self, pool_user_ids: Sequence[int]) -> Decimal:
        """Money returned out of the pool's non-voided events by live payouts."""
        total = (
            self._pool_debits_query(pool_user_ids)
            .join(CustomerCreditPayout, CustomerCreditPayout.id == CustomerCreditPayoutDebit.payout_id)
            .filter(CustomerCreditPayout.is_live, CashCollectionEvent.voided_at.is_(None))
            .with_entities(func.coalesce(func.sum(CustomerCreditPayoutDebit.amount), ZERO))
            .scalar()
        )
        return self.cash_service._to_decimal(total)

    def returned_by_event(self, event_ids: Sequence[int]) -> Dict[int, Decimal]:
        """Live-payout returned amount per event id (events with none are absent)."""
        if not event_ids:
            return {}
        rows = (
            db.session.query(
                CustomerCreditPayoutDebit.cash_collection_event_id, func.sum(CustomerCreditPayoutDebit.amount)
            )
            .join(CustomerCreditPayout, CustomerCreditPayout.id == CustomerCreditPayoutDebit.payout_id)
            .filter(CustomerCreditPayout.is_live, CustomerCreditPayoutDebit.cash_collection_event_id.in_(event_ids))
            .group_by(CustomerCreditPayoutDebit.cash_collection_event_id)
            .all()
        )
        return {int(event_id): self.cash_service._to_decimal(total) for event_id, total in rows}

    def result_payload(self, result: PayoutResult, *, actor: Optional[User]) -> Dict[str, Any]:
        """The apply route's answer: the payout, the balance left and the funding driver session, read after
        the write; ``next_session_id`` is the driver's new session when this return closed his trip.
        ``actor`` is the admin who applied it (the payout row's ``can_undo`` is his)."""
        return PayoutApplyResponse(
            **self._payout_answer(result.payout, actor=actor),
            next_session_id=result.next_session.id if result.next_session is not None else None,
            replayed=result.replayed,
        ).model_dump(mode="json")

    def undo_payload(self, payout: CustomerCreditPayout, *, actor: Optional[User]) -> Dict[str, Any]:
        """The undo route's answer: the payout (now void), the balance after, and its funding session."""
        return PayoutUndoResponse(**self._payout_answer(payout, actor=actor)).model_dump(mode="json")

    def _payout_answer(self, payout: CustomerCreditPayout, *, actor: Optional[User]) -> Dict[str, Any]:
        """What the apply and the undo answers share: the payout row, the customer's balance after, and the
        funding driver session (a return from the driver's current trip cash) as the reconciliation screens
        serialize it."""
        funding_session = payout.driver_cash_session
        return {
            "payout": self._payout_row(payout, actor=actor),
            "available_prepayment_balance": self.cash_service.get_customer_prepaid_balance(payout.customer_id),
            "driver_cash_session": (
                self.recon_service._serialize_session(funding_session) if funding_session is not None else None
            ),
        }

    # ---- the one planner ----
    def _plan(
        self,
        *,
        customer_id: int,
        amount: Optional[Decimal],
        paid_by: str,
        driver_user_id: Optional[int],
        paid_at: datetime,
        lock: bool,
        target_balance: Optional[Decimal] = None,
    ) -> PayoutPlan:
        if User.query.get(customer_id) is None:
            raise NotFoundError("Customer not found")
        to_dec = self.cash_service._to_decimal
        pool_user_ids = self.cash_service._credit_pool_for_anchor(customer_id)

        # Lock 1: the pool's credit events, id-ordered and re-qualified, in consumption order (oldest first).
        events = (
            self.cash_service._locked_cluster_credit_events(customer_id, pool_user_ids)
            if lock
            else self.cash_service.cluster_credit_events_unlocked(customer_id, pool_user_ids)
        )

        # Every figure below is read from those rows, so on apply nothing is stale (P12).
        free_balance = sum((to_dec(event.unapplied_amount) for event in events), ZERO)
        if target_balance is not None:
            payout_amount = max(free_balance - target_balance, ZERO)
        elif amount is not None:
            payout_amount = amount
        else:
            payout_amount = free_balance

        blocking: List[str] = []
        if free_balance <= ZERO:
            blocking.append("no_free_balance: the customer has no free prepaid balance to return")
        elif target_balance is not None and target_balance >= free_balance:
            blocking.append(
                f"target_not_below_free_balance: the balance to leave ({target_balance}) is not below "
                f"the free balance ({free_balance}), so there is nothing to return"
            )
        elif payout_amount > free_balance:
            blocking.append(
                f"amount_exceeds_free_balance: {payout_amount} is more than the free balance ({free_balance})"
            )
        driver_eligible = paid_by == CustomerCreditPayout.PAID_BY_OFFICE or self._is_eligible_driver(driver_user_id)
        if not driver_eligible:
            blocking.append(f"driver_not_eligible: user {driver_user_id} is not an active driver")

        draws: List[tuple] = []
        if ZERO < payout_amount <= free_balance:
            remaining = payout_amount
            for event in events:
                if remaining <= ZERO:
                    break
                take = min(remaining, to_dec(event.unapplied_amount))
                if take > ZERO:
                    draws.append((event, take))
                    remaining -= take

        # Lock 2: the grocery money contracts that mirror the drawn credit (their accounts, by contract id).
        # A return posts the matching contract entry, or blocks: never one ledger without the other.
        mirror = self.contract_service.payout_mirror_plan(draws, lock=lock)
        blocking.extend(mirror.blocking_reasons)

        # Lock 3, always the last: the driver's cash session row, for a return from his current trip cash.
        session, session_effect = None, None
        if paid_by == CustomerCreditPayout.PAID_BY_DRIVER_CURRENT and driver_eligible:
            session, session_effect, session_blocks = self._driver_session_effect(
                driver_user_id=driver_user_id, amount=payout_amount, paid_at=paid_at, lock=lock
            )
            blocking.extend(session_blocks)

        revenue_ids, card_click_ids = self._classify([event.id for event, _ in draws])
        return PayoutPlan(
            customer_id=customer_id,
            pool_user_ids=pool_user_ids,
            free_balance=free_balance,
            amount=payout_amount,
            target_balance=target_balance,
            paid_by=paid_by,
            driver_user_id=driver_user_id,
            paid_at=paid_at,
            drawn=[
                DrawnCredit(
                    event_id=event.id,
                    customer_id=event.customer_id,
                    amount=take,
                    revenue_counted=event.id in revenue_ids,
                    card_click=event.id in card_click_ids,
                )
                for event, take in draws
            ],
            # The preview's information only: the apply path (lock=True, under the locks) never publishes it.
            warnings=PayoutWarnings() if lock else self._warnings(pool_user_ids),
            session_effect=session_effect,
            contract_effect=self._contract_effect_rows(mirror.postings),
            blocking_reasons=blocking,
            _events_by_id={event.id: event for event, _ in draws},
            _session=session,
            _mirror_postings=mirror.postings,
        )

    @staticmethod
    def _contract_effect_rows(postings: Dict[int, Decimal]) -> List[ContractEffect]:
        """The preview rows of the mirror postings, each with the contract's number and name for the admin."""
        if not postings:
            return []
        labels = {
            contract.id: contract
            for contract in CorporateContract.query.filter(CorporateContract.id.in_(list(postings))).all()
        }
        return [
            ContractEffect(
                contract_id=contract_id,
                contract_number=labels[contract_id].contract_number,
                name=labels[contract_id].name,
                amount=amount,
            )
            for contract_id, amount in sorted(postings.items())
        ]

    def _driver_session_effect(
        self, *, driver_user_id: int, amount: Decimal, paid_at: datetime, lock: bool
    ) -> Tuple[Optional[DriverCashSession], Optional[SessionEffect], List[str]]:
        """(session, effect, blocking reasons) of a return paid out of the driver's current trip cash (§4.3).

        The session is his current not-yet-closed one, under the session row lock on apply; a past session
        is never touched (P11). The return is capped at what he still holds of this trip (on hand minus
        handed in), so on hand never drops below what he handed in.
        """
        recon = self.recon_service
        session = recon.find_active_session(driver_user_id, for_update=lock)
        if session is None:
            return (
                None,
                None,
                [
                    "no_active_session: the driver has no open trip cash session; "
                    "record it as 'driver, already settled' or 'office'"
                ],
            )

        figures = recon.cash_figures(session)
        cap = figures.remaining_to_hand_in
        on_hand_after = figures.on_hand - amount
        effect = SessionEffect(
            session_id=session.id,
            status=recon._status_value(session.status),
            on_hand_before=figures.on_hand,
            handed_in=figures.handed_in,
            cap=cap,
            on_hand_after=on_hand_after,
            will_close=recon.session_closes(handed_in=figures.handed_in, on_hand=on_hand_after, has_live_payout=True),
        )
        blocking: List[str] = []
        if amount > cap:
            blocking.append(
                f"amount_exceeds_driver_cash: {amount} is more than the driver still holds from this trip ({cap}); "
                "record it as 'driver, already settled' or 'office'"
            )
        if paid_at < recon._as_aware_utc(session.session_started_at):
            blocking.append(
                "paid_before_current_trip: this cash came from an earlier trip; record it as 'driver, already settled'"
            )
        return session, effect, blocking

    def _warnings(self, pool_user_ids: List[int]) -> PayoutWarnings:
        """What the admin should know before returning money. Read-only; none of it blocks.

        * pending orders: the pool's orders still in ``RESERVABLE_ORDER_STATUSES`` plus every order
          holding the pool's credit (``held_credit_by_order``), by order id, each with what it holds;
        * live subscriptions: the pool's, in ``LIVE_SUBSCRIPTION_STATUSES``;
        * place debts: the places the pool shares that owe (info only: credit never pools across a place).

        Every lookup takes ``pool_user_ids`` (the credit pool, not the link cluster): a grocery anchor's pool is
        itself alone and an individual's pool drops grocery members (``_credit_pool_for_anchor``), so the
        pending orders and the places named here are exactly those of the accounts whose credit is returned.
        """
        held = self.cash_service.held_credit_by_order(pool_user_ids)
        pool_order_ids = {
            row[0]
            for row in db.session.query(Order.id)
            .filter(Order.user_id.in_(pool_user_ids), Order.status.in_(self.cash_service.RESERVABLE_ORDER_STATUSES))
            .all()
        }
        orders = (
            Order.query.filter(Order.id.in_(pool_order_ids | set(held))).order_by(Order.id).all()
            if (pool_order_ids or held)
            else []
        )
        subscriptions = (
            Subscription.query.filter(
                Subscription.user_id.in_(pool_user_ids), Subscription.status.in_(LIVE_SUBSCRIPTION_STATUSES)
            )
            .order_by(Subscription.id)
            .all()
        )
        return PayoutWarnings(
            pending_orders=[
                PendingOrderWarning(
                    order_id=order.id,
                    order_number=order.order_number,
                    status=order.status.value,
                    held_amount=held.get(order.id, ZERO),
                )
                for order in orders
            ],
            live_subscriptions=[
                LiveSubscriptionWarning(subscription_id=sub.id, status=sub.status.value) for sub in subscriptions
            ],
            place_debts=[
                PlaceDebtWarning(
                    place_group_id=place["place_group_id"],
                    label=place["label"],
                    amount=Decimal(str(place["place_open_cod_debt_total"])),
                )
                # The pool's ids on purpose: places of accounts outside the pool (a grocery member) never owed
                # from this credit, so naming them would mislead the admin about what the return affects.
                for place in self.cash_service._cluster_place_debts(pool_user_ids)
                if place["place_open_cod_debt_total"] > 0
            ],
        )

    @staticmethod
    def _classify(event_ids: List[int]) -> tuple:
        """(revenue-counted ids, card/Click ids) of the drawn events: one query per flag, each through
        the ONE clause that defines it, never a Python copy of the rule."""
        if not event_ids:
            return set(), set()

        def matching(clause) -> set:
            return {
                row[0]
                for row in db.session.query(CashCollectionEvent.id)
                .filter(CashCollectionEvent.id.in_(event_ids), clause)
                .all()
            }

        return matching(customer_credit_rebook_event_clause()), matching(card_click_origin_event_clause())

    @staticmethod
    def _is_eligible_driver(driver_user_id: Optional[int]) -> bool:
        """A driver is a user with an active `DeliveryPerson` row; the id is the USER id, never the row's."""
        if driver_user_id is None:
            return False
        return (
            DeliveryPerson.query.filter(
                DeliveryPerson.user_id == driver_user_id, DeliveryPerson.is_active.is_(True)
            ).first()
            is not None
        )

    # ---- the write ----
    def _write(
        self, plan: PayoutPlan, *, reason: str, idempotency_key: str, actor_user_id: int
    ) -> CustomerCreditPayout:
        """The header, then one debit per drawn event, each lowering that locked event's unapplied credit."""
        payout = CustomerCreditPayout(
            customer_id=plan.customer_id,
            amount=plan.amount,
            target_balance=plan.target_balance,
            paid_by=plan.paid_by,
            driver_user_id=plan.driver_user_id,
            driver_cash_session_id=plan._session.id if plan._session is not None else None,
            reason=reason,
            paid_at=plan.paid_at,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
        )
        db.session.add(payout)
        db.session.flush()
        for credit in plan.drawn:
            db.session.add(
                CustomerCreditPayoutDebit(
                    payout_id=payout.id,
                    cash_collection_event_id=credit.event_id,
                    amount=credit.amount,
                    revenue_counted=credit.revenue_counted,
                    card_click=credit.card_click,
                )
            )
            self.cash_service._add_unapplied(plan._events_by_id[credit.event_id], -credit.amount)
        db.session.flush()
        return payout

    @staticmethod
    def _audit_recorded(
        payout: CustomerCreditPayout, plan: PayoutPlan, *, reason: str, idempotency_key: str, actor_user_id: int
    ) -> None:
        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_REFUNDED,
            action="customer_credit_payout_recorded",
            severity=AuditSeverity.HIGH,
            resource_type="customer_credit_payout",
            resource_id=str(payout.id),
            description=f"Prepaid credit of {plan.amount} returned to customer {plan.customer_id} ({plan.paid_by})",
            additional_data={
                "customer_id": plan.customer_id,
                "amount": str(plan.amount),
                "paid_by": plan.paid_by,
                "driver_user_id": plan.driver_user_id,
                "paid_at": plan.paid_at.isoformat(),
                "actor_user_id": actor_user_id,
                "reason": reason,
                # Not "idempotency_key" nor "client_token": the audit sanitizer redacts any name containing
                # "key" or "token" (AuditLogger.sensitive_fields), which stored the value as "[REDACTED]".
                "client_request_id": idempotency_key,
                "card_click_amount": str(plan.card_click_amount),
                "revenue_counted_amount": str(plan.revenue_counted_amount),
                "debits": [{"event_id": credit.event_id, "amount": str(credit.amount)} for credit in plan.drawn],
            },
        )

    @staticmethod
    def _notify_customer(payout: CustomerCreditPayout) -> None:
        """Tell the customer (spec P8), published after the commit. The return stands if the publish fails."""
        from business_app.tasks.notification_tasks import send_prepayment_returned_notice_task

        try:
            send_prepayment_returned_notice_task.delay(payout.id)
        except Exception:  # noqa: BLE001 - the payout is committed; only the notice is lost
            logger.exception("Failed to publish the prepayment-returned notice for payout %s", payout.id)

    @staticmethod
    def _audit_undone(payout: CustomerCreditPayout, *, reason: str, actor_user_id: int) -> None:
        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_REFUNDED,
            action="customer_credit_payout_undone",
            severity=AuditSeverity.HIGH,
            resource_type="customer_credit_payout",
            resource_id=str(payout.id),
            description=f"Return of {payout.amount} to customer {payout.customer_id} undone ({payout.paid_by})",
            additional_data={
                "customer_id": payout.customer_id,
                "amount": str(payout.amount),
                "paid_by": payout.paid_by,
                "driver_user_id": payout.driver_user_id,
                "driver_cash_session_id": payout.driver_cash_session_id,
                "actor_user_id": actor_user_id,
                "reason": reason,
                "debits": [
                    {"event_id": debit.cash_collection_event_id, "amount": str(debit.amount)} for debit in payout.debits
                ],
            },
        )

    @staticmethod
    def _audit_session_closed(
        payout: CustomerCreditPayout, next_session: DriverCashSession, *, actor_user_id: int
    ) -> None:
        """The return left the driver nothing to hand in, so it closed his trip's cash session."""
        session = payout.driver_cash_session
        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="driver_cash_session_closed_by_payout",
            severity=AuditSeverity.MEDIUM,
            resource_type="driver_cash_session",
            resource_id=str(session.id),
            additional_data={
                "driver_user_id": session.driver_user_id,
                "payout_id": payout.id,
                "actor_user_id": actor_user_id,
                "expected_cash": str(session.expected_cash),
                "expected_cash_on_hand": str(session.expected_cash_on_hand),
                "declared_cash": str(session.declared_cash) if session.declared_cash is not None else None,
                "declared_variance": str(session.declared_variance),
                "status": session.status.value,
                "next_driver_cash_session_id": next_session.id,
            },
        )

    @staticmethod
    def _payout_by_key(idempotency_key: str) -> Optional[CustomerCreditPayout]:
        return CustomerCreditPayout.query.filter_by(idempotency_key=idempotency_key).first()

    def _replay(self, payout: CustomerCreditPayout, *, customer_id: int, inputs: _PayoutInputs) -> PayoutResult:
        """The answer to a key ``payout`` already holds: that payout again, if this is the same return.

        The same return means the same customer, payer and driver, and the same size: the amount asked for,
        or for "set balance" the same balance to leave (the amount was derived from it). Anything else is a
        different return under a used key, refused with PAYOUT_IDEMPOTENCY_KEY_REUSED, never answered with
        a payout the admin did not ask for (controller ruling, final fix wave). ``paid_at`` and ``reason``
        are not compared: a resend carries the same ones, and they never change what was paid.
        """
        to_dec = self.cash_service._to_decimal
        if inputs.target_balance is not None:
            same_size = payout.target_balance is not None and to_dec(payout.target_balance) == inputs.target_balance
        else:
            same_size = payout.target_balance is None and to_dec(payout.amount) == inputs.amount
        same_return = (
            same_size
            and payout.customer_id == customer_id
            and payout.paid_by == inputs.paid_by
            and payout.driver_user_id == inputs.driver_user_id
        )
        if not same_return:
            raise ValidationError(
                f"This idempotency key already recorded return {payout.id}, which differs from this request; "
                "start a new return",
                error_code="PAYOUT_IDEMPOTENCY_KEY_REUSED",
            )
        return PayoutResult(payout=payout, plan=None, replayed=True, next_session=None)

    def _payout_row(self, payout: CustomerCreditPayout, *, actor: Optional[User]) -> PayoutRow:
        """The one payout row every answer publishes.

        ``can_undo`` is the undo button's whole answer (P1): the return can be undone
        (``payout_undo_block_reason``) AND ``actor`` may undo it, by the very permission test the ADMIN-only
        undo route applies. ``undo_block_code`` stays the return's own reason, the same for every reader.
        """
        debits = list(payout.debits)
        to_dec = self.cash_service._to_decimal
        block = payout_undo_block_reason(payout)
        return PayoutRow(
            id=payout.id,
            customer_id=payout.customer_id,
            amount=to_dec(payout.amount),
            paid_by=payout.paid_by,
            driver_user_id=payout.driver_user_id,
            driver_name=payout.driver_user.full_name if payout.driver_user else None,
            driver_cash_session_id=payout.driver_cash_session_id,
            paid_at=payout.paid_at,
            reason=payout.reason,
            actor_user_id=payout.actor_user_id,
            actor_name=payout.actor.full_name if payout.actor else None,
            created_at=payout.created_at,
            voided_at=payout.voided_at,
            voided_by_user_id=payout.voided_by_user_id,
            void_reason=payout.void_reason,
            card_click_amount=sum((to_dec(debit.amount) for debit in debits if debit.card_click), ZERO),
            revenue_counted_amount=sum((to_dec(debit.amount) for debit in debits if debit.revenue_counted), ZERO),
            can_undo=block is None and has_admin_permission(actor, PREPAYMENT_RETURN_PERMISSIONS),
            undo_block_code=block,
            debits=[
                PayoutDebitRow(event_id=debit.cash_collection_event_id, amount=to_dec(debit.amount)) for debit in debits
            ],
        )
