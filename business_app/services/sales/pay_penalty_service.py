"""Sales-agent penalties and adjustments (spec §4.11).

This module starts with `SalesPayAdjustmentService` (A11): a signed, explained, whole-UZS
correction an admin adds to one agent's month. Adjustments are append-only; a wrong one is
corrected by another, never cancelled (T-ADJ-1). The carry-forward rows approve writes live in
the same table (`source="carry_forward"`), but only `SalesPayPeriodService.approve` writes them.

`SalesPayPenaltyService` below is the penalty catalogue and the penalty state machine.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional

from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.sales_pay import SalesPayAdjustment, SalesPayPenalty, SalesPayPenaltyType, SalesPayPeriod
from business_app.models.user import User
from business_app.services.sales.pay_period_service import (
    UNAPPROVED_STATUSES,
    SalesPayPeriodService,
    audit_pay_write,
    refreeze_if_closed,
)
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_rules import check_amount_bound, check_self_decision, is_self, round_uzs
from business_app.services.sales.pay_terms_service import _agent_profile
from business_app.services.sales.work_calendar import WorkCalendarService
from business_app.utils import local_windows
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.transactions import atomic_transaction
from business_app.utils.validation_helpers import strip_reason
from shared.staff_constants import SALES_PAY_REASON_MIN_LENGTH

ADJUSTMENT_REASON_MAX_LENGTH = 300


def _pay_text(value: Any, *, field: str, max_length: int) -> str:
    """A reason, evidence or note (§4.11): the one "trim, cap, then at least five characters"
    for every pay text (review SSOT M8), the adjustment's reason included.

    `strip_reason` trims it and refuses an over-long one; the text must then have at least
    `SALES_PAY_REASON_MIN_LENGTH` characters. Both refusals carry the same details, so the
    modal can name its limits.
    """
    details = {"field": field, "min_length": SALES_PAY_REASON_MIN_LENGTH, "max_length": max_length}
    try:
        text = strip_reason(
            value if isinstance(value, str) else None, max_length=max_length, error_code="SALES_PAY_REASON_REQUIRED"
        )
    except ValidationError as exc:
        raise ValidationError(exc.message, error_code="SALES_PAY_REASON_REQUIRED", details=details) from exc
    if text is None or len(text) < SALES_PAY_REASON_MIN_LENGTH:
        raise ValidationError(
            f"The {field} must be at least {SALES_PAY_REASON_MIN_LENGTH} characters",
            error_code="SALES_PAY_REASON_REQUIRED",
            details=details,
        )
    return text


class SalesPayAdjustmentService:
    @staticmethod
    def create(
        agent_user_id: int,
        month: date,
        amount: Decimal,
        reason: str,
        *,
        actor_id: int,
        now: Optional[datetime] = None,
    ) -> SalesPayAdjustment:
        """A11 (§4.11): `source="admin"`, into an open or closed month where the agent has terms
        in force. A CLOSED month is re-frozen for the agent in the same transaction, inserting their
        statement when they had none there (the Pay tab's recorded repayment, T-OWED-2 (b)).

        An admin adjusting their own pay is allowed and tagged (D-Q11).
        """
        now = ensure_utc(now or local_windows.local_now())
        if SalesPayPeriodService.epoch() is None:
            raise ConflictError("Pay has not started", error_code="SALES_PAY_NOT_STARTED", details={})
        amount = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        check_amount_bound(amount, field="amount")
        if amount == 0 or round_uzs(amount) != amount:
            raise ValidationError(
                "An adjustment is a non-zero whole UZS amount",
                error_code="SALES_PAY_AMOUNT_INVALID",
                details={"field": "amount"},
            )
        text = _pay_text(reason, field="reason", max_length=ADJUSTMENT_REASON_MAX_LENGTH)
        actor = db.session.get(User, actor_id)
        # Its own commit, before the transaction: the current month has a row even before the
        # first sync of the month.
        SalesPayPeriodService.ensure_periods(now)
        with atomic_transaction():
            self_decided = check_self_decision(agent_user_id, actor)
            period = SalesPayPeriodService.assert_inputs_writable(month)
            # "Terms in force" is `version_for` resolving, the test a statement is built on.
            if SalesPayPlanService.version_for(agent_user_id, month) is None:
                raise ConflictError(
                    "The agent has no pay terms for this month",
                    error_code="SALES_PAY_TERMS_MISSING",
                    details={"agents": [int(agent_user_id)]},
                )
            adjustment = SalesPayAdjustment(
                agent_user_id=agent_user_id,
                period_id=period.id,
                amount=amount,
                reason=text,
                source="admin",
                created_by_user_id=actor_id,
            )
            db.session.add(adjustment)
            db.session.flush()
            refreeze_if_closed(period, [agent_user_id], actor_id=actor_id, now=now)
        audit_pay_write(
            "adjustment_created",
            resource_type="sales_agent",
            resource_id=agent_user_id,
            new_values={
                "adjustment_id": adjustment.id,
                "month": local_windows.format_month(month),
                "amount": int(amount),
                "reason": text,
            },
            self_decided=self_decided,
        )
        return adjustment


# ---------------------------------------------------------------------------
# Penalties (compensation spec §4.11, §4.13, §7.5)
# ---------------------------------------------------------------------------

PENALTY_TYPE_NAME_LANGUAGES = ("en", "uz", "ru")
PENALTY_TEXT_MAX_LENGTHS = {"reason": 500, "evidence": 2000, "note": 500}
# The status each decision starts from (§3.2's state matrix).
PENALTY_ACTION_FROM = {"confirm": ("proposed",), "reject": ("proposed",), "cancel": ("confirmed",)}


def penalty_type_names(penalty_type: SalesPayPenaltyType) -> Dict[str, str]:
    """The type's name in every language the service writes.

    Managers pick from it, the admin table shows it, and the agent's push quotes it. A page of
    rows reads these from the instance cache that `prefetch_type_names` filled.
    """
    return {language: penalty_type.get_translated("name", language) for language in PENALTY_TYPE_NAME_LANGUAGES}


def penalty_self_decided(penalty: SalesPayPenalty) -> bool:
    """`AdminPenaltyRow.self_decided` (§4.13 point 4): `is_self` over the row's three actors.

    A proposal the agent rejected about themselves is tagged here too, although no statement
    lists it (the statement's tag reads only the inputs it used).
    """
    return any(
        is_self(penalty.agent_user_id, actor_id)
        for actor_id in (penalty.proposed_by_user_id, penalty.decided_by_user_id, penalty.cancelled_by_user_id)
    )


def prefetch_type_names(types: Iterable[Optional[SalesPayPenaltyType]]) -> None:
    """One query per language for every name on a page (`prefetch_translations`), not one per row."""
    distinct = list({penalty_type.id: penalty_type for penalty_type in types if penalty_type is not None}.values())
    for language in PENALTY_TYPE_NAME_LANGUAGES:
        SalesPayPenaltyType.prefetch_translations(distinct, fields=["name"], language=language)


def _clean_type_names(names: Mapping[str, str]) -> Dict[str, str]:
    return {language: str(names[language]).strip() for language in PENALTY_TYPE_NAME_LANGUAGES}


def _positive_whole(value: Any, *, field: str) -> Decimal:
    """A penalty amount, or a type's default: whole UZS above zero (§3.2). It is refused, never rounded."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        amount = None
    if amount is not None:
        check_amount_bound(amount, field=field)
    if amount is None or not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
        raise ValidationError(
            f"The {field} must be a whole amount above zero",
            error_code="SALES_PAY_AMOUNT_INVALID",
            details={"field": field},
        )
    return amount


def _penalty_type(type_id: int, *, lock: bool = False) -> SalesPayPenaltyType:
    penalty_type = db.session.get(SalesPayPenaltyType, type_id, with_for_update=lock, populate_existing=lock)
    if penalty_type is None:
        raise NotFoundError(
            "Penalty type not found",
            error_code="SALES_PAY_NOT_FOUND",
            details={"resource": "penalty_type", "id": int(type_id)},
        )
    return penalty_type


class _CheckedIncident(NamedTuple):
    """What the shared guards of §4.11 cleaned, and where the penalty would land (`route_manual`)."""

    penalty_type: SalesPayPenaltyType
    reason: str
    evidence: str
    self_decided: bool
    period: SalesPayPeriod
    is_late: bool


def _locked_penalty(penalty_id: int) -> SalesPayPenalty:
    penalty = db.session.get(SalesPayPenalty, penalty_id, with_for_update=True, populate_existing=True)
    if penalty is None:
        raise NotFoundError(
            "Penalty not found",
            error_code="SALES_PAY_NOT_FOUND",
            details={"resource": "penalty", "id": int(penalty_id)},
        )
    return penalty


def _state_guard(penalty: SalesPayPenalty, action: str) -> None:
    """The one statement of which decision each penalty status allows (§3.2).

    The write paths raise on it; `actions_for` reads it in check mode.
    """
    allowed = PENALTY_ACTION_FROM[action]
    if penalty.status not in allowed:
        raise ConflictError(
            f"A {penalty.status} penalty cannot take the '{action}' decision",
            error_code="SALES_PAY_STATE_INVALID",
            details={
                "resource": "penalty",
                "id": penalty.id,
                "status": penalty.status,
                "action": action,
                "allowed": list(allowed),
            },
        )


def _passes(guard, *args) -> bool:
    """A guard in check mode (the `allowed_actions` rule): True unless it refuses."""
    try:
        guard(*args)
    except (ConflictError, NotFoundError, ValidationError):
        return False
    return True


def _assert_terms(agent_user_id: int, period: SalesPayPeriod) -> None:
    """A penalty is booked only into a month the agent has terms in force for (§4.11): without
    them there is no statement to carry it. "In force" is `version_for` resolving, the test the
    sync's `plan_for`, `agents_missing_terms` and the adjustment use."""
    if SalesPayPlanService.version_for(agent_user_id, period.month_start) is None:
        raise ConflictError(
            "The agent has no pay terms in force for this month",
            error_code="SALES_PAY_TERMS_MISSING",
            details={"agents": [int(agent_user_id)], "month": local_windows.format_month(period.month_start)},
        )


class SalesPayPenaltyService:
    """The penalty catalogue and the penalty state machine (§4.11).

    A manager PROPOSES (M2); an admin confirms, rejects or cancels (A24-A26), or books a penalty
    already confirmed (A23). Every penalty write names its agent, so it first runs
    `check_self_decision` (D-Q11): a manager deciding about themselves is refused; an admin is
    allowed, and the audit row says so. A confirmed penalty lands in the month `route_manual`
    picks, the same answer `actions_for` publishes as `target_month`. A closed month is
    re-frozen in the same transaction, and the agent's push goes out after commit. Types are
    company-wide, so their writes carry no self-decision check (§4.13, exempt).
    """

    # -- penalty types (A19-A21) --

    @staticmethod
    def list_types(*, active_only: bool = False) -> List[SalesPayPenaltyType]:
        query = SalesPayPenaltyType.query
        if active_only:
            query = query.filter(SalesPayPenaltyType.is_active.is_(True))
        types = query.order_by(SalesPayPenaltyType.id).all()
        prefetch_type_names(types)
        return types

    @staticmethod
    def create_type(names: Mapping[str, str], default_amount: Decimal, *, actor_id: int) -> SalesPayPenaltyType:
        amount = _positive_whole(default_amount, field="default_amount")
        clean = _clean_type_names(names)
        with atomic_transaction():
            penalty_type = SalesPayPenaltyType(
                name=clean["en"],
                default_amount=amount,
                is_active=True,
                created_by_user_id=actor_id,
                updated_by_user_id=actor_id,
            )
            db.session.add(penalty_type)
            db.session.flush()
            # All three rows, `en` included: the column holds English, and a type with no `en`
            # row reads another language's fallback on an English screen (§3.2).
            penalty_type.set_translations({"name": clean})
        audit_pay_write(
            "penalty_type_created",
            resource_type="sales_pay_penalty_type",
            resource_id=penalty_type.id,
            new_values={"names": clean, "default_amount": int(amount), "is_active": True},
        )
        return penalty_type

    @staticmethod
    def update_type(
        type_id: int,
        *,
        names: Optional[Mapping[str, str]] = None,
        default_amount: Optional[Decimal] = None,
        is_active: Optional[bool] = None,
        actor_id: int,
    ) -> SalesPayPenaltyType:
        """Rename, reprice or retire a type. A type is deactivated, never deleted. A new default
        never touches an existing penalty, which stores its own amount (§5.2)."""
        with atomic_transaction():
            penalty_type = _penalty_type(type_id, lock=True)
            before = {
                "names": penalty_type_names(penalty_type),
                "default_amount": int(penalty_type.default_amount),
                "is_active": penalty_type.is_active,
            }
            if default_amount is not None:
                penalty_type.default_amount = _positive_whole(default_amount, field="default_amount")
            if is_active is not None:
                penalty_type.is_active = bool(is_active)
            if names is not None:
                clean = _clean_type_names(names)
                penalty_type.name = clean["en"]
                penalty_type.set_translations({"name": clean})
            penalty_type.updated_by_user_id = actor_id
        audit_pay_write(
            "penalty_type_updated",
            resource_type="sales_pay_penalty_type",
            resource_id=penalty_type.id,
            old_values=before,
            new_values={
                "names": penalty_type_names(penalty_type),
                "default_amount": int(penalty_type.default_amount),
                "is_active": penalty_type.is_active,
            },
        )
        return penalty_type

    # -- penalties (A22-A26, M1-M2) --

    @staticmethod
    def _decide_about(agent_user_id: int, actor_id: int) -> bool:
        """The penalty's agent must hold a profile (Task 4's `_agent_profile`: SALES_PAY_NOT_FOUND,
        resource "agent"). Then comes the D-Q11 guard, whose tag is returned: a manager about
        themselves is refused, an admin is allowed and tagged."""
        _agent_profile(agent_user_id)
        return check_self_decision(agent_user_id, db.session.get(User, actor_id))

    @staticmethod
    def _route_incident(agent_user_id: int, incident_date: date, *, now: datetime):
        """The incident date's rules (§4.11), with `route_manual` in the middle.

        In order: not in the future; not before pay started, and not in a shadow month past
        `closed` (`route_manual`'s own refusals, I-16); not before the agent's employment.
        `route_manual` reads the incident month's period row, which the first sync or read of a
        month creates, so every month up to `now` is opened first (`ensure_periods` commits on
        its own, and nothing else is in flight yet). Returns `route_manual`'s `(period, late)`.
        """
        if incident_date > local_windows.local_date(now):
            raise ValidationError(
                "The incident date is in the future",
                error_code="SALES_PAY_DATE_INVALID",
                details={"field": "incident_date", "date": incident_date.isoformat(), "reason": "future"},
            )
        SalesPayPeriodService.ensure_periods(now)
        routed = SalesPayPeriodService.route_manual(incident_date, now=now)
        employment_start, _employment_end = WorkCalendarService.employment(agent_user_id)
        if employment_start is None or incident_date < employment_start:
            raise ValidationError(
                "The incident date is before the agent's employment",
                error_code="SALES_PAY_DATE_INVALID",
                details={"field": "incident_date", "date": incident_date.isoformat(), "reason": "not_employed"},
            )
        return routed

    @staticmethod
    def _check_incident(
        agent_user_id: int,
        penalty_type_id: int,
        incident_date: date,
        reason: Any,
        evidence: Any,
        *,
        actor_id: int,
        now: datetime,
    ) -> _CheckedIncident:
        """§4.11 "Every penalty path", in its order.

        Pay has started, the agent exists, the self-decision guard passes, the type exists and
        is active, the incident date passes its rules, and the texts are long enough.
        """
        if SalesPayPeriodService.epoch() is None:
            raise ConflictError("Pay tracking has not started", error_code="SALES_PAY_NOT_STARTED")
        self_decided = SalesPayPenaltyService._decide_about(agent_user_id, actor_id)
        penalty_type = _penalty_type(penalty_type_id)
        if not penalty_type.is_active:
            raise ConflictError(
                "This penalty type is no longer in use",
                error_code="SALES_PAY_PENALTY_TYPE_INACTIVE",
                details={"penalty_type_id": penalty_type.id},
            )
        period, is_late = SalesPayPenaltyService._route_incident(agent_user_id, incident_date, now=now)
        return _CheckedIncident(
            penalty_type=penalty_type,
            reason=_pay_text(reason, field="reason", max_length=PENALTY_TEXT_MAX_LENGTHS["reason"]),
            evidence=_pay_text(evidence, field="evidence", max_length=PENALTY_TEXT_MAX_LENGTHS["evidence"]),
            self_decided=self_decided,
            period=period,
            is_late=is_late,
        )

    @staticmethod
    def propose(
        agent_user_id: int,
        penalty_type_id: int,
        incident_date: date,
        reason: str,
        evidence: str,
        *,
        actor_id: int,
        now: Optional[datetime] = None,
    ) -> SalesPayPenalty:
        """M2: a proposal an admin decides later (C6).

        It runs every guard `create_confirmed` runs, the shadow refusal included (I-16), so a
        manager never files a proposal that can no longer be confirmed. It sends nothing and
        audits nothing (§4.17 has no proposal verb): the agent hears about a penalty only once it
        is confirmed (T-PEN-1).
        """
        now = ensure_utc(now or local_windows.local_now())
        checked = SalesPayPenaltyService._check_incident(
            agent_user_id, penalty_type_id, incident_date, reason, evidence, actor_id=actor_id, now=now
        )
        with atomic_transaction():
            penalty = SalesPayPenalty(
                agent_user_id=agent_user_id,
                penalty_type_id=checked.penalty_type.id,
                origin="proposal",
                incident_date=incident_date,
                reason=checked.reason,
                evidence=checked.evidence,
                status="proposed",
                proposed_by_user_id=actor_id,
                proposed_at=now,
            )
            db.session.add(penalty)
        return penalty

    @staticmethod
    def create_confirmed(
        agent_user_id: int,
        penalty_type_id: int,
        incident_date: date,
        reason: str,
        evidence: str,
        amount: Optional[Decimal],
        *,
        actor_id: int,
        now: Optional[datetime] = None,
    ) -> SalesPayPenalty:
        """A23: an admin books a penalty already confirmed (`origin = "direct"`), as its proposer
        and its decider. Every propose guard, then every confirm guard, in one transaction."""
        from business_app.services.sales import notifications

        now = ensure_utc(now or local_windows.local_now())
        checked = SalesPayPenaltyService._check_incident(
            agent_user_id, penalty_type_id, incident_date, reason, evidence, actor_id=actor_id, now=now
        )
        booked = _positive_whole(checked.penalty_type.default_amount if amount is None else amount, field="amount")
        with atomic_transaction():
            period = SalesPayPeriodService.lock_writable(checked.period.id, UNAPPROVED_STATUSES)
            _assert_terms(agent_user_id, period)
            penalty = SalesPayPenalty(
                agent_user_id=agent_user_id,
                penalty_type_id=checked.penalty_type.id,
                origin="direct",
                incident_date=incident_date,
                reason=checked.reason,
                evidence=checked.evidence,
                status="confirmed",
                amount=booked,
                period_id=period.id,
                proposed_by_user_id=actor_id,
                proposed_at=now,
                decided_by_user_id=actor_id,
                decided_at=now,
            )
            db.session.add(penalty)
            db.session.flush()
            refreeze_if_closed(period, [agent_user_id], actor_id=actor_id, now=now)
        audit_pay_write(
            "penalty_created",
            resource_type="sales_pay_penalty",
            resource_id=penalty.id,
            new_values={
                "agent_user_id": agent_user_id,
                "penalty_type_id": checked.penalty_type.id,
                "incident_date": incident_date.isoformat(),
                "amount": int(booked),
                "posting_month": local_windows.format_month(period.month_start),
                "is_late": checked.is_late,
                "origin": "direct",
            },
            self_decided=checked.self_decided,
        )
        notifications.notify_pay_penalty_confirmed(penalty)
        return penalty

    @staticmethod
    def confirm(
        penalty_id: int,
        *,
        actor_id: int,
        amount: Optional[Decimal] = None,
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SalesPayPenalty:
        """A24: a proposal becomes a charge.

        The amount defaults to the type's (I-12). The month is `route_manual`'s (I-13): the
        incident month while it is open or closed, otherwise the earliest open month, labelled
        late. The target row is locked; a closed month is re-frozen in the same transaction. The
        agent is pushed after commit.
        """
        from business_app.services.sales import notifications

        now = ensure_utc(now or local_windows.local_now())
        # `route_manual` below reads period rows; open every month up to now before the transaction.
        SalesPayPeriodService.ensure_periods(now)
        with atomic_transaction():
            penalty = _locked_penalty(penalty_id)
            agent_user_id = penalty.agent_user_id
            self_decided = SalesPayPenaltyService._decide_about(agent_user_id, actor_id)
            _state_guard(penalty, "confirm")
            booked = _positive_whole(penalty.penalty_type.default_amount if amount is None else amount, field="amount")
            decision_note = (
                None if note is None else _pay_text(note, field="note", max_length=PENALTY_TEXT_MAX_LENGTHS["note"])
            )
            routed, is_late = SalesPayPeriodService.route_manual(penalty.incident_date, now=now)
            period = SalesPayPeriodService.lock_writable(routed.id, UNAPPROVED_STATUSES)
            _assert_terms(agent_user_id, period)
            period_id = period.id
            # Every value is a local by now (N-FLUSH): the booked CHECK pairs status with amount
            # and period, and the decided CHECK pairs it with the decider and the instant, so no
            # lazy read may autoflush a half-written row between these lines.
            penalty.status = "confirmed"
            penalty.amount = booked
            penalty.period_id = period_id
            penalty.decided_by_user_id = actor_id
            penalty.decided_at = now
            penalty.decision_note = decision_note
            db.session.flush()
            refreeze_if_closed(period, [agent_user_id], actor_id=actor_id, now=now)
        audit_pay_write(
            "penalty_confirmed",
            resource_type="sales_pay_penalty",
            resource_id=penalty.id,
            old_values={"status": "proposed"},
            new_values={
                "status": "confirmed",
                "agent_user_id": agent_user_id,
                "amount": int(booked),
                "posting_month": local_windows.format_month(period.month_start),
                "is_late": is_late,
            },
            self_decided=self_decided,
        )
        notifications.notify_pay_penalty_confirmed(penalty)
        return penalty

    @staticmethod
    def reject(penalty_id: int, *, actor_id: int, note: str, now: Optional[datetime] = None) -> SalesPayPenalty:
        """A25: a proposal is turned down, with a note only admins read. Nothing is counted and
        nothing is sent (§7.5: a rejection pushes nothing)."""
        now = ensure_utc(now or local_windows.local_now())
        with atomic_transaction():
            penalty = _locked_penalty(penalty_id)
            agent_user_id = penalty.agent_user_id
            self_decided = SalesPayPenaltyService._decide_about(agent_user_id, actor_id)
            _state_guard(penalty, "reject")
            decision_note = _pay_text(note, field="note", max_length=PENALTY_TEXT_MAX_LENGTHS["note"])
            penalty.status = "rejected"
            penalty.decided_by_user_id = actor_id
            penalty.decided_at = now
            penalty.decision_note = decision_note
        audit_pay_write(
            "penalty_rejected",
            resource_type="sales_pay_penalty",
            resource_id=penalty.id,
            old_values={"status": "proposed"},
            new_values={"status": "rejected", "agent_user_id": agent_user_id},
            self_decided=self_decided,
        )
        return penalty

    @staticmethod
    def cancel(penalty_id: int, *, actor_id: int, note: str, now: Optional[datetime] = None) -> SalesPayPenalty:
        """A26: a confirmed penalty is withdrawn while its month is still under review (C6).

        A closed month is re-frozen without it; an approved one refuses (MONTH_LOCKED, from
        `lock_writable` on the locked row). Nothing is sent.
        """
        now = ensure_utc(now or local_windows.local_now())
        with atomic_transaction():
            penalty = _locked_penalty(penalty_id)
            agent_user_id = penalty.agent_user_id
            self_decided = SalesPayPenaltyService._decide_about(agent_user_id, actor_id)
            _state_guard(penalty, "cancel")
            cancel_reason = _pay_text(note, field="note", max_length=PENALTY_TEXT_MAX_LENGTHS["note"])
            period = SalesPayPeriodService.lock_writable(penalty.period_id, UNAPPROVED_STATUSES)
            penalty.status = "cancelled"
            penalty.cancelled_by_user_id = actor_id
            penalty.cancelled_at = now
            penalty.cancel_reason = cancel_reason
            db.session.flush()
            refreeze_if_closed(period, [agent_user_id], actor_id=actor_id, now=now)
        audit_pay_write(
            "penalty_cancelled",
            resource_type="sales_pay_penalty",
            resource_id=penalty.id,
            old_values={"status": "confirmed"},
            new_values={
                "status": "cancelled",
                "agent_user_id": agent_user_id,
                "amount": int(penalty.amount),
                "posting_month": local_windows.format_month(period.month_start),
            },
            self_decided=self_decided,
        )
        return penalty

    @staticmethod
    def list(
        *,
        status: Optional[str] = None,
        agent_user_id: Optional[int] = None,
        month: Optional[date] = None,
        origin: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
    ) -> Dict[str, Any]:
        """A22, M1 and A8's `penalties`, newest first.

        `month` filters on the POSTING month (§5.2), so only confirmed and cancelled rows have
        one. M1 passes `origin="proposal"`: a direct booking is the admin's own and is never
        listed to managers (review gaming F9).
        """
        query = SalesPayPenalty.query
        if status is not None:
            query = query.filter(SalesPayPenalty.status == status)
        if agent_user_id is not None:
            query = query.filter(SalesPayPenalty.agent_user_id == agent_user_id)
        if origin is not None:
            query = query.filter(SalesPayPenalty.origin == origin)
        if month is not None:
            query = query.join(SalesPayPeriod, SalesPayPenalty.period_id == SalesPayPeriod.id).filter(
                SalesPayPeriod.month_start == month
            )
        total = query.count()
        items = (
            query.options(
                joinedload(SalesPayPenalty.penalty_type),
                joinedload(SalesPayPenalty.period),
                joinedload(SalesPayPenalty.agent),
                joinedload(SalesPayPenalty.proposed_by),
                joinedload(SalesPayPenalty.decided_by),
                joinedload(SalesPayPenalty.cancelled_by),
            )
            .order_by(SalesPayPenalty.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        prefetch_type_names(penalty.penalty_type for penalty in items)
        return {"items": items, "total": total, "page": page, "per_page": per_page}

    @staticmethod
    def pending_count() -> int:
        """A1 `pending_penalty_count`, the Penalties tab badge (published there only, §5.2)."""
        return SalesPayPenalty.query.filter(SalesPayPenalty.status == "proposed").count()

    @staticmethod
    def actions_for(penalty: SalesPayPenalty, *, now: datetime) -> Dict[str, Any]:
        """What an admin may do with `penalty` now, and where a confirm would land (§5.2).

        This is the write paths' own guards in check mode: `_state_guard` for each decision,
        `route_manual` for the target month, and the writable statuses `cancel` locks with.
        When `route_manual` refuses (a shadow month past `closed`, I-16), the answer is
        `target_month: null` and `can_confirm: false`. The Confirm modal's "Will count in" is
        therefore the month `confirm` will use.
        """
        actions: Dict[str, Any] = {"target_month": None, "target_is_late": False, "can_confirm": False}
        if _passes(_state_guard, penalty, "confirm"):
            try:
                target, target_is_late = SalesPayPeriodService.route_manual(penalty.incident_date, now=now)
            except (ConflictError, NotFoundError, ValidationError):
                pass
            else:
                actions.update(
                    target_month=local_windows.format_month(target.month_start),
                    target_is_late=target_is_late,
                    can_confirm=True,
                )
        period = penalty.period
        actions["can_reject"] = _passes(_state_guard, penalty, "reject")
        actions["can_cancel"] = (
            _passes(_state_guard, penalty, "cancel") and period is not None and period.status in UNAPPROVED_STATUSES
        )
        return actions
