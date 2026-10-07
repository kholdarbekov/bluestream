"""Sales-agent pay terms: what each agent is paid on and from when, their employment dates, and the
days they were not paid for (spec §4.10, §3.3, C2).

- **Terms** are insert-only and effective-dated. A raise is a new row, so no month's history is
  rewritten. `terms_for` takes the latest `effective_month <= month`, then the latest row, so a
  same-month correction wins.
- **Employment dates** live on the agent's profile. They are the one pay decision with its own
  actor columns, because the self-decision tag reads them (§4.13).
- **Unpaid days** are dates, so the gate can leave them out.

Every write here is about ONE agent's pay, so each calls `check_self_decision` (D-Q11). A manager
deciding about their own pay is refused. An admin may decide about their own, and the audit row
says so.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from business_app import db
from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesAgentPayTerms, SalesAgentUnpaidDay, SalesPayPeriod, SalesPayPlan
from business_app.models.user import User
from business_app.serializers.sales_serializers import person_ref
from business_app.services.sales.pay_calculator import employed_next_month
from business_app.services.sales.pay_period_service import (
    UNAPPROVED_STATUSES,
    SalesPayPeriodService,
    audit_pay_write,
    month_days,
    month_range,
    refreeze_if_closed,
    refuse_day,
)
from business_app.services.sales.pay_rules import check_amount_bound, check_self_decision, round_uzs
from business_app.services.sales.work_calendar import WorkCalendarService
from business_app.utils import local_windows
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.transactions import atomic_transaction


def _agent_profile(agent_user_id: int, *, for_update: bool = False) -> SalesAgentProfile:
    query = SalesAgentProfile.query.filter_by(user_id=agent_user_id)
    if for_update:
        query = query.with_for_update().populate_existing()
    profile = query.one_or_none()
    if profile is None:
        raise NotFoundError(
            "This user has no sales-agent profile",
            error_code="SALES_PAY_NOT_FOUND",
            details={"resource": "agent", "id": int(agent_user_id)},
        )
    return profile


def _employed_span(start: Optional[date], end: Optional[date], first: date, last: date) -> Optional[Tuple[date, date]]:
    """The employed days of `[first, last]` as (from, to), or None when there are none. A NULL date
    leaves that side unbounded (spec §4.2)."""
    low = max(start, first) if start is not None else first
    high = min(end, last) if end is not None else last
    return (low, high) if low <= high else None


def _lock_affected_periods(months: Sequence[date]) -> List[SalesPayPeriod]:
    """Lock every affected month's period row, in month order, and refuse the whole change when any
    of them is approved or paid (spec §4.10): its carry or owed amount is already final. A month
    with no row yet has nothing to protect."""
    if not months:
        return []
    periods = (
        SalesPayPeriod.query.filter(SalesPayPeriod.month_start.in_(list(months)))
        .order_by(SalesPayPeriod.month_start)
        .with_for_update()
        .populate_existing()
        .all()
    )
    final = [period for period in periods if period.status not in UNAPPROVED_STATUSES]
    if final:
        raise ConflictError(
            "The employment change would alter a month that is already approved",
            error_code="SALES_PAY_MONTH_LOCKED",
            details={"months": [local_windows.format_month(period.month_start) for period in final]},
        )
    return periods


class SalesPayTermsService:
    @staticmethod
    def add_terms(
        agent_user_id: int,
        *,
        effective_month: date,
        base_salary: Decimal,
        plan_id: int,
        note: Optional[str],
        actor_id: int,
        now: Optional[datetime] = None,
    ) -> SalesAgentPayTerms:
        """The agent's base and plan from `effective_month` onward (C2). Every paid agent has an
        employment start, because terms need one."""
        now = now or local_windows.local_now()
        actor = db.session.get(User, actor_id)
        with atomic_transaction():
            profile = _agent_profile(agent_user_id)
            self_decided = check_self_decision(agent_user_id, actor)
            if profile.employment_start_date is None:
                raise ValidationError(
                    "Set the employment start date before adding pay terms",
                    error_code="SALES_PAY_TERMS_INVALID",
                    details={"field": "employment_start_date", "reason": "required"},
                )
            base = base_salary if isinstance(base_salary, Decimal) else Decimal(str(base_salary))
            check_amount_bound(base, field="base_salary")
            if base < 0 or round_uzs(base) != base:
                raise ValidationError(
                    "The base salary is a whole, non-negative UZS amount",
                    error_code="SALES_PAY_AMOUNT_INVALID",
                    details={"field": "base_salary"},
                )
            if db.session.get(SalesPayPlan, plan_id) is None:
                raise NotFoundError(
                    "No such pay plan", error_code="SALES_PAY_NOT_FOUND", details={"resource": "plan", "id": plan_id}
                )
            SalesPayPeriodService.assert_month_editable(effective_month, now=now)
            terms = SalesAgentPayTerms(
                agent_user_id=agent_user_id,
                effective_month=effective_month,
                base_salary=round_uzs(base),
                plan_id=plan_id,
                note=(note or "").strip() or None,
                created_by_user_id=actor_id,
            )
            db.session.add(terms)
            db.session.flush()
        audit_pay_write(
            "terms_added",
            resource_type="sales_agent",
            resource_id=agent_user_id,
            new_values={
                "terms_id": terms.id,
                "effective_month": local_windows.format_month(effective_month),
                "base_salary": int(round_uzs(base)),
                "plan_id": plan_id,
            },
            self_decided=self_decided,
        )
        # §4.10: terms for an open month decide which plan rates the agent's credits of that
        # month, and whether their earned orders can be credited at all (`skipped_no_terms`
        # until now). Sync them after the commit; ensure_fresh never raises. Imported here:
        # the ledger service imports this module.
        from business_app.services.sales.pay_ledger_service import SalesPayLedgerService

        SalesPayLedgerService.ensure_fresh([agent_user_id], now=now, force=True)
        return terms

    @staticmethod
    def terms_for(agent_user_id: int, month: date) -> Optional[SalesAgentPayTerms]:
        """The terms in force for `month`: the latest effective month on or before it, then the
        latest row (spec §3.2)."""
        return (
            SalesAgentPayTerms.query.filter(
                SalesAgentPayTerms.agent_user_id == agent_user_id, SalesAgentPayTerms.effective_month <= month
            )
            .order_by(SalesAgentPayTerms.effective_month.desc(), SalesAgentPayTerms.id.desc())
            .first()
        )

    @staticmethod
    def agent_ids_with_terms() -> List[int]:
        return [
            agent_id
            for (agent_id,) in db.session.query(SalesAgentPayTerms.agent_user_id)
            .distinct()
            .order_by(SalesAgentPayTerms.agent_user_id)
        ]

    @staticmethod
    def affected_months(
        old_start: Optional[date],
        old_end: Optional[date],
        new_start: date,
        new_end: Optional[date],
        *,
        now: datetime,
    ) -> List[date]:
        """The months an employment change touches, from the epoch to `local_month(now)` (spec §4.10),
        the union of two sets:
        - every month whose employed days differ between the old and the new span;
        - every month whose `employed_next_month` answer flips (I-26), which moves its shortfall
          between carry and owed without changing one of its days: NULL -> 31 October flips October.
        The calculator's own function answers, so carry versus owed has one expression.
        Returns [] before pay starts."""
        epoch = SalesPayPeriodService.epoch()
        if epoch is None:
            return []
        affected = []
        for month in month_range(epoch, local_windows.local_month(now)):
            last = local_windows.month_end(month)
            days_change = _employed_span(old_start, old_end, month, last) != _employed_span(
                new_start, new_end, month, last
            )
            flips = employed_next_month(old_end, month) != employed_next_month(new_end, month)
            if days_change or flips:
                affected.append(month)
        return affected

    @staticmethod
    def set_employment(
        agent_user_id: int, *, start: date, end: Optional[date], actor_id: int, now: Optional[datetime] = None
    ) -> SalesAgentProfile:
        """The agent's one employment span (spec §3.3), stamped with who set it and when.

        It is refused whole when any month it would change is approved or paid.
        """
        now = now or local_windows.local_now()
        actor = db.session.get(User, actor_id)
        if start is None:
            raise ValidationError(
                "An employment start date is required",
                error_code="SALES_PAY_DATE_INVALID",
                details={"field": "start", "date": None, "reason": "required"},
            )
        if end is not None and end < start:
            raise ValidationError(
                "The employment end date is before its start",
                error_code="SALES_PAY_DATE_INVALID",
                details={"field": "end", "date": end.isoformat(), "reason": "before_start"},
            )
        with atomic_transaction():
            profile = _agent_profile(agent_user_id, for_update=True)
            self_decided = check_self_decision(agent_user_id, actor)
            old_start, old_end = profile.employment_start_date, profile.employment_end_date
            months = SalesPayTermsService.affected_months(old_start, old_end, start, end, now=now)
            periods = _lock_affected_periods(months)
            profile.employment_start_date = start
            profile.employment_end_date = end
            profile.employment_set_by_user_id = actor_id
            profile.employment_set_at = ensure_utc(now)
            # The earliest affected closed month is re-frozen; its cascade covers every later
            # closed month of the agent, so each affected one is re-frozen once, in month order.
            closed = [period for period in periods if period.status == "closed"]
            if closed:
                refreeze_if_closed(closed[0], [agent_user_id], actor_id=actor_id, now=ensure_utc(now))
        audit_pay_write(
            "employment_set",
            resource_type="sales_agent",
            resource_id=agent_user_id,
            old_values={
                "start": old_start.isoformat() if old_start else None,
                "end": old_end.isoformat() if old_end else None,
            },
            new_values={
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "months": [local_windows.format_month(month) for month in months],
            },
            self_decided=self_decided,
        )
        return profile

    @staticmethod
    def set_unpaid_days(
        agent_user_id: int,
        month: date,
        days: Sequence[Mapping[str, Any]],
        *,
        actor_id: int,
        now: Optional[datetime] = None,
    ) -> List[SalesAgentUnpaidDay]:
        """Replace the agent's unpaid days for `month` (spec §4.10). A date can be unpaid only if it
        would otherwise be worked: any day of the week, Sunday included, but not a holiday (C2,
        I-38). `WorkCalendarService.day_statuses` says whether it would be, so the calendar's own
        precedence names the refusal."""
        actor = db.session.get(User, actor_id)
        with atomic_transaction():
            _agent_profile(agent_user_id)
            self_decided = check_self_decision(agent_user_id, actor)
            period = SalesPayPeriodService.assert_inputs_writable(month)
            entries = month_days(month, days)
            statuses = WorkCalendarService.day_statuses(agent_user_id, month, local_windows.month_end(month))
            for day, _note in entries:
                if statuses[day] == "not_employed":
                    refuse_day(day, "not_employed")
                if statuses[day] in ("non_working", "holiday"):
                    refuse_day(day, "not_working_day")
            # PR10/C8: `WorkCalendarService.unpaid_day_rows` (built on `_unpaid_rows`) is the ONE
            # query on `sales_agent_unpaid_days`; nothing else queries the table directly.
            existing = WorkCalendarService.unpaid_day_rows(agent_user_id, month, local_windows.month_end(month))
            previous = sorted(row.unpaid_date for row in existing)
            for row in existing:
                db.session.delete(row)
            db.session.flush()
            rows = [
                SalesAgentUnpaidDay(
                    agent_user_id=agent_user_id, unpaid_date=day, note=note, created_by_user_id=actor_id
                )
                for day, note in entries
            ]
            db.session.add_all(rows)
            db.session.flush()
            refreeze_if_closed(
                period, [agent_user_id], actor_id=actor_id, now=ensure_utc(now or local_windows.local_now())
            )
        audit_pay_write(
            "unpaid_days_set",
            resource_type="sales_agent",
            resource_id=agent_user_id,
            old_values={
                "month": local_windows.format_month(month),
                "dates": [day.isoformat() for day in previous],
            },
            new_values={
                "month": local_windows.format_month(month),
                "dates": [day.isoformat() for day, _note in entries],
            },
            self_decided=self_decided,
        )
        return rows

    @staticmethod
    def agent_terms(agent_user_id: int, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """A16, and the answer of A17 and A18: the agent's Pay tab (§5.2).

        `owed_to_date` is the statement service's one outstanding balance, never a sum (I-28);
        `editable_from_month` is the guard's own function, so the month picker and the refusal agree.
        """
        # Imported here: the statement service imports this module.
        from business_app.services.sales.pay_statement_service import SalesPayStatementService

        now = now or local_windows.local_now()
        profile = _agent_profile(agent_user_id)
        user = db.session.get(User, agent_user_id)
        rows = (
            SalesAgentPayTerms.query.filter_by(agent_user_id=agent_user_id)
            .order_by(SalesAgentPayTerms.effective_month.desc(), SalesAgentPayTerms.id.desc())
            .all()
        )
        in_force = SalesPayTermsService.terms_for(agent_user_id, local_windows.local_month(now))

        def term_row(terms: SalesAgentPayTerms) -> Dict[str, Any]:
            return {
                "id": terms.id,
                "effective_month": local_windows.format_month(terms.effective_month),
                "base_salary": terms.base_salary,
                "plan": {"id": terms.plan.id, "name": terms.plan.name},
                "note": terms.note,
                "created_at": terms.created_at,
                "created_by": person_ref(terms.created_by),
            }

        return {
            "agent": {"user_id": user.id, "name": user.full_name, "phone": user.phone, "is_active": profile.is_active},
            "employment": {"start": profile.employment_start_date, "end": profile.employment_end_date},
            "owed_to_date": SalesPayStatementService.owed_to_date(agent_user_id),
            "terms": [term_row(row) for row in rows],
            "term_in_force": term_row(in_force) if in_force is not None else None,
            "plans": [{"id": plan.id, "name": plan.name} for plan in SalesPayPlan.query.order_by(SalesPayPlan.name)],
            "editable_from_month": local_windows.format_month(SalesPayPeriodService.editable_from_month(now)),
        }
