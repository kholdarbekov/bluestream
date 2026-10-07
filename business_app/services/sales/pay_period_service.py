"""Sales-agent pay months: the company-wide period rows, and every rule about which month a pay
write may touch (spec §4.9, §4.10 "One editability rule", §3.4).

`sales_pay_periods` has one row per local month. A statement has no status of its own; it takes
its period's. This module owns:
- the epoch (`start`) and the rows after it (`ensure_periods`, which never opens a future month);
- the period-row locks every pay write serializes on (`lock_writable`, `lock_open_periods`,
  `assert_month_editable`, `assert_inputs_writable`), so that on Postgres "approve" and "add a
  penalty to that month" queue behind each other;
- the lifecycle guards. The lifecycle runs them before it writes, and `allowed_actions` runs the
  SAME functions in check mode, so a button is published exactly when its route would accept it;
- a month's holidays.

`FOR UPDATE` is a no-op on SQLite. The SQLite suite proves the guards' logic; `pg_db` proves the races.
"""

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, NoReturn, Optional, Sequence, Tuple

from flask import current_app
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from business_app import db
from business_app.models.sales_pay import SalesPayAdjustment, SalesPayPeriod, SalesPayStatement
from business_app.services.sales import work_calendar
from business_app.utils import local_windows
from business_app.utils.audit_logger import AuditEventType, AuditSeverity, audit_logger
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from business_app.utils.transactions import atomic_transaction
from shared.staff_constants import SALES_PAY_PERIOD_ACTIONS

if TYPE_CHECKING:  # annotation only: the ledger service imports this module
    from business_app.services.sales.pay_ledger_service import SyncStats

# A month is unapproved while it is open or closed. Its inputs (holidays, unpaid days, employment,
# adjustments, penalties) may still change, and the earliest such month is the one that nets an owed
# balance (I-28). Approved and paid months are final (C9).
UNAPPROVED_STATUSES = ("open", "closed")


def month_range(first: date, last: date) -> List[date]:
    """Every first-of-month from `first` to `last`, inclusive, in month order."""
    months = []
    month = first
    while month <= last:
        months.append(month)
        month = local_windows.next_month(month)
    return months


def refuse_day(day: date, reason: str) -> NoReturn:
    """Refuse one date of a holidays or unpaid-days list; `reason` names the rule it broke."""
    raise ValidationError(
        f"{day.isoformat()} cannot be used here: {reason}",
        error_code="SALES_PAY_DATE_INVALID",
        details={"field": "days", "date": day.isoformat(), "reason": reason},
    )


def month_days(month: date, days: Sequence[Mapping[str, Any]]) -> List[Tuple[date, Optional[str]]]:
    """`[{"date", "note"}]` for one month -> `[(date, note)]` in date order.

    Every date must be inside the month and listed once. Holidays and unpaid days both replace a
    whole month's list, so both start here, and each then adds its own working-day rule.
    """
    last = local_windows.month_end(month)
    entries: Dict[date, Optional[str]] = {}
    for item in days:
        day = item["date"]
        if not month <= day <= last:
            refuse_day(day, "outside_month")
        if day in entries:
            refuse_day(day, "duplicate")
        entries[day] = (item.get("note") or "").strip() or None
    return sorted(entries.items())


def audit_pay_write(
    verb: str,
    *,
    resource_type: str,
    resource_id: Any,
    old_values: Optional[Dict[str, Any]] = None,
    new_values: Optional[Dict[str, Any]] = None,
    self_decided: bool = False,
) -> None:
    """The audit row of one admin pay write (spec §4.17). Called AFTER the write commits.

    `self_decided` is what `check_self_decision` returned (D-Q11): an admin deciding about their own
    pay is allowed, and the row says so. `audit_logs` drops non-critical rows after 90 days, so the
    permanent trail is the actor and time columns on the pay tables themselves. No key here may
    contain "key", "token" or "secret": the logger redacts those.
    """
    values = dict(new_values or {})
    if self_decided:
        values["self_decided"] = True
    audit_logger.log_event(
        event_type=AuditEventType.SETTINGS_CHANGED,
        action=f"sales_pay_{verb}",
        severity=AuditSeverity.HIGH,
        resource_type=resource_type,
        resource_id=str(resource_id),
        old_values=old_values,
        new_values=values,
    )


def refreeze_if_closed(
    period: SalesPayPeriod, agent_user_ids: Optional[Sequence[int]], *, actor_id: int, now: datetime
) -> None:
    """§4.9 "Re-freeze on input change": an input written into a CLOSED month re-freezes the
    affected statements in the same transaction, and the same agents' later closed months with
    them (those read this month's gate). `None` means every agent eligible for the month, which a
    holiday touches. A listed agent with no statement there yet gets one at revision 1 (a leaver's
    recorded repayment). An open month needs nothing: its estimate is read live.

    The one call every input writer makes (holidays, unpaid days, employment, adjustments, and
    Task 8's penalty confirm and cancel), so "which statements a change re-freezes" has one answer.
    """
    if period.status != "closed":
        return
    # Imported here: the statement service imports this module.
    from business_app.services.sales.pay_statement_service import SalesPayStatementService

    db.session.flush()
    agents = SalesPayStatementService.eligible_agent_ids(period) if agent_user_ids is None else agent_user_ids
    SalesPayStatementService.refreeze(period, agents, actor_id=actor_id, now=now)


def _refuse_unless_status(period: SalesPayPeriod, allowed_statuses: Sequence[str]) -> SalesPayPeriod:
    if period.status not in allowed_statuses:
        raise ConflictError(
            f"{local_windows.format_month(period.month_start)} is {period.status} and can no longer be changed",
            error_code="SALES_PAY_MONTH_LOCKED",
            details={"month": local_windows.format_month(period.month_start), "status": period.status},
        )
    return period


def _refuse_action(
    period: SalesPayPeriod, action: str, allowed: Sequence[str], reason: Optional[str] = None
) -> NoReturn:
    details: Dict[str, Any] = {
        "resource": "period",
        "status": period.status,
        "action": action,
        "allowed": list(allowed),
    }
    if reason is not None:
        details["reason"] = reason
    raise ConflictError(
        f"{local_windows.format_month(period.month_start)} is {period.status}: {action} is not possible",
        error_code="SALES_PAY_STATE_INVALID",
        details=details,
    )


def _locked_by_month(month: date) -> Optional[SalesPayPeriod]:
    # `populate_existing` is load-bearing: a row already in the identity map would otherwise keep the
    # status it had BEFORE the lock was granted, which is exactly the stale read the lock exists to stop.
    return SalesPayPeriod.query.filter_by(month_start=month).with_for_update().populate_existing().one_or_none()


def _lock_period(month: date) -> SalesPayPeriod:
    """The month's row, locked FOR UPDATE; a month with no row refuses through `get`."""
    return _locked_by_month(month) or SalesPayPeriodService.get(month)


def _raise_if_sync_incomplete(stats: "SyncStats") -> None:
    """A close, or a closed month's recalculate, needs every earned order posted (§4.9): one the
    sync could not post, or a slot a concurrent run won, would freeze a statement without it."""
    if not stats.failed and not stats.conflicts:
        return
    details = {
        "orders": sorted({f["order_id"] for f in stats.failed if f.get("order_id") is not None}),
        "agents": sorted({f["agent_id"] for f in stats.failed}),
    }
    if not stats.failed:
        # Only lost races: no order to name, so the admin is told why (final-review M3).
        details["reason"] = "concurrent"
    raise ConflictError(
        "Some earnings could not be synced; nothing was frozen",
        error_code="SALES_PAY_SYNC_INCOMPLETE",
        details=details,
    )


class SalesPayPeriodService:
    @staticmethod
    def epoch() -> Optional[date]:
        """The month pay started in, or None before `start`. Nothing earned earlier is ever credited."""
        return db.session.query(func.min(SalesPayPeriod.month_start)).scalar()

    @staticmethod
    def get(month: date) -> SalesPayPeriod:
        period = SalesPayPeriod.query.filter_by(month_start=month).one_or_none()
        if period is None:
            raise NotFoundError(
                "No pay period for this month",
                error_code="SALES_PAY_NOT_FOUND",
                details={"resource": "period", "id": local_windows.format_month(month)},
            )
        return period

    @staticmethod
    def start(month: date, *, is_shadow: bool, actor_id: int, now: Optional[datetime] = None) -> SalesPayPeriod:
        """Open the first period, the EPOCH, in the current local month (spec §4.9).

        `is_shadow` is set here and nowhere else, so only the epoch can be a shadow month (C13, I-16).
        """
        now = now or local_windows.local_now()
        epoch = SalesPayPeriodService.epoch()
        if epoch is not None:
            raise ConflictError(
                "Pay has already started",
                error_code="SALES_PAY_ALREADY_STARTED",
                details={"month": local_windows.format_month(epoch)},
            )
        expected = local_windows.local_month(now)
        if month != expected:
            raise ValidationError(
                "Pay can only start in the current month",
                error_code="SALES_PAY_MONTH_INVALID",
                details={"month": local_windows.format_month(month), "expected": local_windows.format_month(expected)},
            )
        period = SalesPayPeriod(month_start=month, status="open", is_shadow=bool(is_shadow), holidays=[])
        db.session.add(period)
        try:
            db.session.commit()
        except IntegrityError:
            # Two admins started the same month at once, and the unique month refused the loser.
            db.session.rollback()
            raise ConflictError(
                "Pay has already started",
                error_code="SALES_PAY_ALREADY_STARTED",
                details={"month": local_windows.format_month(month)},
            )
        audit_pay_write(
            "start",
            resource_type="sales_pay_period",
            resource_id=period.id,
            new_values={
                "month": local_windows.format_month(month),
                "is_shadow": period.is_shadow,
                "actor_user_id": actor_id,
            },
        )
        return period

    @staticmethod
    def ensure_periods(now: datetime) -> List[SalesPayPeriod]:
        """Open a row for every month from the epoch to `local_month(now)` that has none, and never a
        future month. Returns every period in that range, in month order; [] before `start`.

        It commits on its own, so call it with nothing else in flight (every sync calls it first).
        Because the current month always exists and can never close (close needs the month to have
        ended), `route` never meets an empty list of open months (spec §4.5).
        """
        epoch = SalesPayPeriodService.epoch()
        if epoch is None:
            return []
        months = month_range(epoch, local_windows.local_month(now))
        for attempt in (1, 2):
            present = {
                month
                for (month,) in db.session.query(SalesPayPeriod.month_start).filter(
                    SalesPayPeriod.month_start.in_(months)
                )
            }
            missing = [month for month in months if month not in present]
            if not missing:
                break
            db.session.add_all(
                [SalesPayPeriod(month_start=month, status="open", is_shadow=False, holidays=[]) for month in missing]
            )
            try:
                db.session.commit()
                break
            except IntegrityError:
                # A concurrent sync opened one of these months first. Nothing else is in flight, so
                # the rollback costs nothing; the re-read adds only what is still missing.
                db.session.rollback()
                if attempt == 2:
                    raise
        return (
            SalesPayPeriod.query.filter(SalesPayPeriod.month_start.in_(months))
            .order_by(SalesPayPeriod.month_start)
            .all()
        )

    @staticmethod
    def lock_writable(period_id: int, allowed_statuses: Sequence[str]) -> SalesPayPeriod:
        """Lock one period row `FOR UPDATE` and refuse unless its status is allowed (spec §3.4)."""
        period = SalesPayPeriod.query.filter_by(id=period_id).with_for_update().populate_existing().one_or_none()
        if period is None:
            raise NotFoundError(
                "No such pay period", error_code="SALES_PAY_NOT_FOUND", details={"resource": "period", "id": period_id}
            )
        return _refuse_unless_status(period, allowed_statuses)

    @staticmethod
    def lock_open_periods() -> List[SalesPayPeriod]:
        """Every OPEN period, locked `FOR UPDATE` in month order: the one order every lock takes."""
        return (
            SalesPayPeriod.query.filter_by(status="open")
            .order_by(SalesPayPeriod.month_start)
            .with_for_update()
            .populate_existing()
            .all()
        )

    @staticmethod
    def editable_from_month(now: Optional[datetime] = None) -> date:
        """The first month a new plan version or terms may start in (spec §4.10).

        Once pay has started, this is the earliest OPEN month; before that, the current local month.
        A12 and A16 publish this same answer, so the UI's month picker never disagrees with the guard.
        """
        earliest_open = (
            db.session.query(func.min(SalesPayPeriod.month_start)).filter(SalesPayPeriod.status == "open").scalar()
        )
        if earliest_open is not None:
            return earliest_open
        return local_windows.local_month(now or local_windows.local_now())

    @staticmethod
    def assert_month_editable(month: date, *, now: Optional[datetime] = None) -> Optional[SalesPayPeriod]:
        """Refuse a plan version or terms that would start in a month that can no longer change.

        The month's period row, if it has one, is locked `FOR UPDATE` BEFORE the check. A version
        therefore cannot commit into a month that a concurrent close has just frozen (review money M2):
        the second writer waits, re-reads `closed`, and is refused. Returns the locked row, or None for
        a month with no row yet.
        """
        if month.day != 1:
            raise ValidationError(
                "A pay month is the first day of a month",
                error_code="SALES_PAY_MONTH_INVALID",
                details={"month": month.isoformat()},
            )
        period = _locked_by_month(month)
        floor = SalesPayPeriodService.editable_from_month(now)
        if month < floor or (period is not None and period.status != "open"):
            raise ConflictError(
                f"{local_windows.format_month(month)} can no longer change; the first editable month is "
                f"{local_windows.format_month(floor)}",
                error_code="SALES_PAY_MONTH_LOCKED",
                details={
                    "month": local_windows.format_month(month),
                    "status": period.status if period is not None else None,
                    "editable_from_month": local_windows.format_month(floor),
                },
            )
        return period

    @staticmethod
    def assert_inputs_writable(month: date) -> SalesPayPeriod:
        """Lock the month's period row and refuse unless it is open or closed (spec §3.4): unpaid days,
        holidays and every other statement input of an approved month are final."""
        period = _locked_by_month(month)
        if period is None:
            raise NotFoundError(
                "No pay period for this month",
                error_code="SALES_PAY_NOT_FOUND",
                details={"resource": "period", "id": local_windows.format_month(month)},
            )
        return _refuse_unless_status(period, UNAPPROVED_STATUSES)

    @staticmethod
    def first_unapproved_month() -> Optional[date]:
        """The earliest open or closed month, or None before start (I-15, I-28). It is the next month
        `approve` may act on, and the only month whose statements and estimates net an owed balance."""
        return (
            db.session.query(func.min(SalesPayPeriod.month_start))
            .filter(SalesPayPeriod.status.in_(UNAPPROVED_STATUSES))
            .scalar()
        )

    @staticmethod
    def closable_from(month: date) -> datetime:
        """When `month` may first be closed: one hour after the last abandon sweep that could still
        close one of its visits.

        The sweep closes a visit left open for `SALES_VISIT_AUTO_ABANDON_HOURS` and runs hourly, so
        one hour after that span has passed the month end, no visit of the month can still be open.
        """
        hours = int(current_app.config["SALES_VISIT_AUTO_ABANDON_HOURS"]) + 1
        return local_windows.month_bounds(month)[1] + timedelta(hours=hours)

    @staticmethod
    def _previous(period: SalesPayPeriod) -> Optional[SalesPayPeriod]:
        previous_month = local_windows.previous_month(period.month_start)
        return SalesPayPeriod.query.filter_by(month_start=previous_month).one_or_none()

    @staticmethod
    def _guard_close(period: SalesPayPeriod, now: datetime) -> None:
        if period.status != "open":
            _refuse_action(period, "close", ("open",))
        closable = SalesPayPeriodService.closable_from(period.month_start)
        if ensure_utc(now) < closable:
            raise ConflictError(
                "This month cannot be closed yet",
                error_code="SALES_PAY_PERIOD_NOT_ENDED",
                details={"closable_from": closable.isoformat()},
            )
        previous = SalesPayPeriodService._previous(period)
        if previous is not None and previous.status == "open":
            raise ConflictError(
                "Close the month before it first",
                error_code="SALES_PAY_PREVIOUS_PERIOD_OPEN",
                details={"month": local_windows.format_month(previous.month_start)},
            )

    @staticmethod
    def _guard_recalculate(period: SalesPayPeriod) -> None:
        if period.status not in UNAPPROVED_STATUSES:
            _refuse_action(period, "recalculate", UNAPPROVED_STATUSES)

    @staticmethod
    def _guard_approve(period: SalesPayPeriod) -> None:
        if period.status != "closed":
            _refuse_action(period, "approve", ("closed",))
        previous = SalesPayPeriodService._previous(period)
        if previous is not None and previous.status in UNAPPROVED_STATUSES:
            # Month order (I-15): a late line reads its earned month's frozen gate, and an owed balance
            # nets only in the first unapproved month, so an earlier month must be final first.
            raise ConflictError(
                "Approve the month before it first",
                error_code="SALES_PAY_PREVIOUS_PERIOD_NOT_APPROVED",
                details={"month": local_windows.format_month(previous.month_start), "status": previous.status},
            )

    @staticmethod
    def _guard_mark_paid(period: SalesPayPeriod) -> None:
        if period.status != "approved":
            _refuse_action(period, "mark_paid", ("approved",))
        if period.is_shadow:
            # A shadow month is paid the old way (C13, I-16), so it is never marked paid.
            _refuse_action(period, "mark_paid", ("approved",), reason="shadow")

    @staticmethod
    def allowed_actions(period: SalesPayPeriod, *, now: datetime) -> List[str]:
        """The lifecycle actions the routes would accept now, in `SALES_PAY_PERIOD_ACTIONS` order.

        It runs the same guards the lifecycle runs and keeps the ones that do not refuse. A sync
        failure or missing terms can only be known by running the action, so those surface as the
        route's own refusal (spec §4.9).
        """
        guards = {
            "close": lambda: SalesPayPeriodService._guard_close(period, now),
            "recalculate": lambda: SalesPayPeriodService._guard_recalculate(period),
            "approve": lambda: SalesPayPeriodService._guard_approve(period),
            "mark_paid": lambda: SalesPayPeriodService._guard_mark_paid(period),
        }
        allowed = []
        for action in SALES_PAY_PERIOD_ACTIONS:
            try:
                guards[action]()
            except ConflictError:
                continue
            allowed.append(action)
        return allowed

    @staticmethod
    def set_holidays(
        month: date, days: Sequence[Mapping[str, Any]], *, actor_id: int, now: Optional[datetime] = None
    ) -> SalesPayPeriod:
        """Replace a month's holidays: every date is inside the month, on a working weekday, and listed once.

        Holidays take working days away from every agent's base pro-rating and gate (C2), and are
        written while the month is open or closed. Under the seven-day default every date is on a
        working weekday, so only a narrowed week reaches that refusal (§4.9, I-37).
        """
        with atomic_transaction():
            period = SalesPayPeriodService.assert_inputs_writable(month)
            previous = list(period.holidays or [])
            entries = month_days(month, days)
            for day, _note in entries:
                if not work_calendar.is_working_weekday(day):
                    refuse_day(day, "not_working_day")
            period.holidays = [{"date": day.isoformat(), "note": note} for day, note in entries]
            refreeze_if_closed(period, None, actor_id=actor_id, now=ensure_utc(now or local_windows.local_now()))
        audit_pay_write(
            "holidays_set",
            resource_type="sales_pay_period",
            resource_id=period.id,
            old_values={"month": local_windows.format_month(month), "holidays": previous},
            new_values={
                "month": local_windows.format_month(month),
                "holidays": period.holidays,
                "actor_user_id": actor_id,
            },
        )
        return period

    # ------------------------------------------------------------------ routing (§4.5)

    @staticmethod
    def is_late(earned_month: date, period: SalesPayPeriod) -> bool:
        """The ONE expression of "late" (S-10): the line was earned in a month before the one
        it was posted to. Nothing stores it; every serializer, push and drill-down asks here."""
        return earned_month < period.month_start

    @staticmethod
    def route(
        earned_month: date,
        *,
        now: datetime,
        periods_by_month: Mapping[date, SalesPayPeriod],
        open_periods: Sequence[SalesPayPeriod],
    ) -> Optional[Tuple[SalesPayPeriod, bool]]:
        """Where an AUTOMATIC line lands: credits, reversals, differences, bonuses (Q4, S-11).

        The earned month while it is open, so a correction nets inside the month it corrects;
        otherwise the earliest open month after it, late. None for a month after `now`'s
        local month, and the caller retries on a later run.

        Never None for a past month, and `min()` never meets an empty sequence:
        `ensure_periods` opens every month from the epoch to `local_month(now)`, and the
        current month cannot be closed before it has ended (`_guard_close`).
        """
        if earned_month > local_windows.local_month(now):
            return None
        own = periods_by_month.get(earned_month)
        if own is not None and own.status == "open":
            target = own
        else:
            target = min(
                (period for period in open_periods if period.month_start > earned_month),
                key=lambda period: period.month_start,
            )
        return target, SalesPayPeriodService.is_late(earned_month, target)

    @staticmethod
    def route_manual(incident_date: date, *, now: datetime) -> Tuple[SalesPayPeriod, bool]:
        """Where a confirmed penalty lands (I-13, S-12): its incident month while that month is
        open or closed, else `route`'s answer, the earliest open month after it, late.

        Refusals:
        - an incident before the epoch, or before pay has started: SALES_PAY_DATE_INVALID
          (`before_pay_start`);
        - an incident in a shadow month that is approved or paid: SALES_PAY_MONTH_LOCKED
          (`reason: "shadow"`). A trial month's incident is booked into the trial month or
          not at all (I-16); routing it late into a real month would make it cost real money
          only because it was confirmed after the trial was approved;
        - an incident month with no period row: SALES_PAY_NOT_FOUND. Only a future month has
          none once `ensure_periods` has run for `now`, so writers run it before their
          transaction.

        Also the check-mode source of a penalty's published `target_month`, `target_is_late`
        and `can_confirm` (Task 8), so the confirm button and the refusal cannot disagree.
        """
        month = local_windows.month_start(incident_date)
        epoch = SalesPayPeriodService.epoch()
        if epoch is None or month < epoch:
            raise ValidationError(
                "The incident is before pay started",
                error_code="SALES_PAY_DATE_INVALID",
                details={"field": "incident_date", "date": incident_date.isoformat(), "reason": "before_pay_start"},
            )
        periods = SalesPayPeriod.query.order_by(SalesPayPeriod.month_start).all()
        periods_by_month = {period.month_start: period for period in periods}
        own = periods_by_month.get(month)
        if own is None:
            raise NotFoundError(
                "Pay period not found",
                error_code="SALES_PAY_NOT_FOUND",
                details={"resource": "period", "id": local_windows.format_month(month)},
            )
        if own.status in ("open", "closed"):
            return own, False
        if own.is_shadow:
            raise ConflictError(
                "A trial month's incident can only be booked into the trial month",
                error_code="SALES_PAY_MONTH_LOCKED",
                details={"month": local_windows.format_month(month), "status": own.status, "reason": "shadow"},
            )
        return SalesPayPeriodService.route(
            month,
            now=now,
            periods_by_month=periods_by_month,
            open_periods=[period for period in periods if period.status == "open"],
        )

    @staticmethod
    def record_sync(now: datetime, stats: "SyncStats") -> None:
        """Stamp every open month with this run (§3.2 `last_synced_at`, `last_sync_stats`),
        so the admin month view can say how fresh its figures are. Its own short
        transaction, after the agents' ones."""
        with atomic_transaction():
            for period in SalesPayPeriod.query.filter(SalesPayPeriod.status == "open").all():
                period.last_synced_at = now
                period.last_sync_stats = stats.as_dict()

    # ------------------------------------------------------------ lifecycle (§4.9)

    @staticmethod
    def lock_periods_from(month: date) -> List[SalesPayPeriod]:
        """This month's row and every later one, FOR UPDATE in month order (§4.15): approve's
        carry target and the closed months a cascading re-freeze rewrites."""
        return (
            SalesPayPeriod.query.filter(SalesPayPeriod.month_start >= month)
            .order_by(SalesPayPeriod.month_start)
            .with_for_update()
            .populate_existing()
            .all()
        )

    @staticmethod
    def close(month: date, *, actor_id: int, now: Optional[datetime] = None) -> SalesPayPeriod:
        """Freeze a month (§4.9).

        The guards run first, so a refused close syncs nothing. Then a forced sync of every agent
        (its own transactions): an order it could not post, or an agent the month should pay but
        has no terms for, refuses the close and the month stays open. Then one transaction: lock
        the row, re-check the guards, set `closed` and flush BEFORE freezing. A sync that queued on
        the lock re-reads `closed` and posts its lines to the next open month, late.
        """
        # Imported here: both services import this module.
        from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
        from business_app.services.sales.pay_statement_service import SalesPayStatementService

        now = ensure_utc(now or local_windows.local_now())
        SalesPayPeriodService._guard_close(SalesPayPeriodService.get(month), now)
        stats = SalesPayLedgerService.sync(now=now)
        _raise_if_sync_incomplete(stats)
        missing = SalesPayStatementService.agents_missing_terms(
            SalesPayPeriodService.get(month), skipped_order_ids=stats.skipped_no_terms
        )
        if missing:
            raise ConflictError(
                "Some agents have no pay terms for this month",
                error_code="SALES_PAY_TERMS_MISSING",
                details={"agents": missing},
            )
        with atomic_transaction():
            period = _lock_period(month)
            SalesPayPeriodService._guard_close(period, now)
            period.status = "closed"
            period.closed_at = now
            period.closed_by_user_id = actor_id
            db.session.flush()
            statements = [
                SalesPayStatementService.freeze(period, agent_user_id, actor_id=actor_id, now=now)
                for agent_user_id in SalesPayStatementService.eligible_agent_ids(period)
            ]
        audit_pay_write(
            "period_closed",
            resource_type="sales_pay_period",
            resource_id=period.id,
            old_values={"status": "open"},
            new_values={
                "month": local_windows.format_month(month),
                "status": "closed",
                "statements": len(statements),
                "actor_user_id": actor_id,
            },
        )
        return period

    @staticmethod
    def recalculate(month: date, *, actor_id: int, now: Optional[datetime] = None) -> SalesPayPeriod:
        """The admin's "Sync now" (§4.9). An open month only syncs, so its estimates refresh. A
        closed month also refuses an unfinished sync, then re-freezes every eligible agent there
        and in the later closed months (revision + 1)."""
        from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
        from business_app.services.sales.pay_statement_service import SalesPayStatementService

        now = ensure_utc(now or local_windows.local_now())
        SalesPayPeriodService._guard_recalculate(SalesPayPeriodService.get(month))
        stats = SalesPayLedgerService.sync(now=now)
        period = SalesPayPeriodService.get(month)
        statements: List[SalesPayStatement] = []
        if period.status == "closed":
            _raise_if_sync_incomplete(stats)
            with atomic_transaction():
                period = _lock_period(month)
                SalesPayPeriodService._guard_recalculate(period)
                statements = SalesPayStatementService.refreeze(
                    period, SalesPayStatementService.eligible_agent_ids(period), actor_id=actor_id, now=now
                )
        audit_pay_write(
            "period_recalculated",
            resource_type="sales_pay_period",
            resource_id=period.id,
            old_values={"status": period.status},
            new_values={
                "month": local_windows.format_month(month),
                "status": period.status,
                "statements": len(statements),
                "actor_user_id": actor_id,
            },
        )
        return period

    @staticmethod
    def approve(month: date, *, actor_id: int, now: Optional[datetime] = None) -> SalesPayPeriod:
        """Make a closed month final (§4.9 approve steps 1-5), in one transaction.

        1. Lock this month and every later one. Check the netting invariant (I-28): each statement
           nets exactly the agent's current outstanding balance. A mismatch means a re-freeze was
           missed; it is an internal error (HTTP 500, no code), and nothing is approved.
        2. `approved`, stamped and flushed.
        3. A shortfall to carry (`carry_out_amount < 0`) becomes a `carry_forward` row in the next
           month. The calculator already decided carry versus owed and shadow, so nothing here
           re-derives them.
        4. An owed balance needs no write: this month's statements are now the agents' latest
           approved ones.
        5. A closed next month is re-frozen for every eligible agent: it is now the first unapproved
           month, so it nets the new balances.
        After the commit, each agent is pushed their own approved statement (§7.5).
        """
        from business_app.services.sales.pay_statement_service import SalesPayStatementService

        now = ensure_utc(now or local_windows.local_now())
        SalesPayPeriodService.get(month)
        # Its own commit, before the transaction: next month's row always exists to take a carry.
        SalesPayPeriodService.ensure_periods(now)
        with atomic_transaction():
            locked = SalesPayPeriodService.lock_periods_from(month)
            period = locked[0]
            SalesPayPeriodService._guard_approve(period)
            statements = (
                SalesPayStatement.query.filter_by(period_id=period.id).order_by(SalesPayStatement.agent_user_id).all()
            )
            for statement in statements:
                owed = SalesPayStatementService.outstanding_owed(statement.agent_user_id)
                expected = owed.id if owed is not None else None
                if statement.owed_in_statement_id != expected:
                    raise RuntimeError(
                        f"Statement {statement.id} nets statement {statement.owed_in_statement_id}, but the "
                        f"agent's outstanding balance is statement {expected}: a re-freeze was missed"
                    )
            period.status = "approved"
            period.approved_at = now
            period.approved_by_user_id = actor_id
            db.session.flush()
            following = next(row for row in locked if row.month_start == local_windows.next_month(month))
            carries = [
                SalesPayAdjustment(
                    agent_user_id=statement.agent_user_id,
                    period_id=following.id,
                    amount=statement.carry_out_amount,
                    # A system literal: never rendered, since a carry is labelled by its source and
                    # month; it meets the 5-300 character rule every reason meets.
                    reason=f"carry_forward {local_windows.format_month(month)}",
                    source="carry_forward",
                    carried_from_period_id=period.id,
                    created_by_user_id=actor_id,
                )
                for statement in statements
                if statement.carry_out_amount < 0
            ]
            db.session.add_all(carries)
            db.session.flush()
            # Step 5 through the one closed-month re-freeze: every eligible agent of a closed next month.
            refreeze_if_closed(following, None, actor_id=actor_id, now=now)
        audit_pay_write(
            "period_approved",
            resource_type="sales_pay_period",
            resource_id=period.id,
            old_values={"status": "closed"},
            new_values={
                "month": local_windows.format_month(month),
                "status": "approved",
                "carries": len(carries),
                "actor_user_id": actor_id,
            },
        )
        # After commit (§7.5): each agent hears about their own statement, once.
        from business_app.services.sales import notifications

        notifications.notify_pay_statements_approved(period)
        return period

    @staticmethod
    def mark_paid(month: date, paid_on: date, *, actor_id: int, now: Optional[datetime] = None) -> SalesPayPeriod:
        """Record the day an approved month was paid (§4.9): inside the month or after it, never in
        the future. A shadow month is never paid (`_guard_mark_paid`)."""
        now = ensure_utc(now or local_windows.local_now())
        with atomic_transaction():
            period = _lock_period(month)
            SalesPayPeriodService._guard_mark_paid(period)
            if paid_on < period.month_start or paid_on > local_windows.local_date(now):
                raise ValidationError(
                    "The payment date must be in the month or after it, and not in the future",
                    error_code="SALES_PAY_DATE_INVALID",
                    details={
                        "field": "paid_on",
                        "date": paid_on.isoformat(),
                        "reason": "before_month" if paid_on < period.month_start else "future",
                    },
                )
            period.status = "paid"
            period.paid_on = paid_on
            period.paid_recorded_at = now
            period.paid_by_user_id = actor_id
        audit_pay_write(
            "period_paid",
            resource_type="sales_pay_period",
            resource_id=period.id,
            old_values={"status": "approved"},
            new_values={
                "month": local_windows.format_month(month),
                "status": "paid",
                "paid_on": paid_on.isoformat(),
                "actor_user_id": actor_id,
            },
        )
        return period
