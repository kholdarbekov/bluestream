"""The working-day calendar sales-agent pay and plan compliance read (spec 2026-09-28 §4.2, C2).

Read-only. Every day of the week is a working day (`WORKING_WEEKDAYS`, the one home of that default,
read only through `is_working_weekday`) minus the company holidays an admin put on the month's pay
period. An agent's day then narrows: outside their employment dates it is `not_employed`, and a
working day an admin marked unpaid is `unpaid`. The base's worked-days fraction, the compliance day
set and a statement's frozen calendar all come from here, so they cannot count a different month.

Before pay starts there are no period rows and so no holidays: the calendar is then every day
minus unpaid days.
"""

from datetime import date, timedelta
from typing import Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple

from business_app import db
from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesAgentUnpaidDay, SalesPayPeriod
from business_app.utils.local_windows import month_start
from shared.staff_constants import SALES_PAY_DAY_STATUSES

# C2 (v5, D-DAYS): every day, Monday 0 ... Sunday 6 (`date.weekday()`). The one home of this
# default; `is_working_weekday` is its only reader.
WORKING_WEEKDAYS = frozenset(range(7))

DAY_WORKED, DAY_NON_WORKING, DAY_HOLIDAY, DAY_UNPAID, DAY_NOT_EMPLOYED = SALES_PAY_DAY_STATUSES

Employment = Tuple[Optional[date], Optional[date]]
_UNBOUNDED: Employment = (None, None)


def is_working_weekday(day: date) -> bool:
    """Whether `day`'s weekday is in the working week: always, under the seven-day default.
    `_calendar_status` and `SalesPayPeriodService.set_holidays` ask here, through this module at
    call time, so narrowing the week again (or a test doing so) is one change that both follow."""
    return day.weekday() in WORKING_WEEKDAYS


def _dates(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _calendar_status(day: date, holidays: Mapping[date, Optional[str]]) -> Optional[str]:
    """The company's half of a day: a non-working weekday (unreachable under the seven-day
    default), a holiday, or None for a working day. `working_dates` and every agent's day read it,
    so "a working day" is written once."""
    if not is_working_weekday(day):
        return DAY_NON_WORKING
    if day in holidays:
        return DAY_HOLIDAY
    return None


def _day_status(
    day: date,
    employment: Employment,
    holidays: Mapping[date, Optional[str]],
    unpaid: Mapping[date, Optional[str]],
) -> str:
    """not_employed > non_working > holiday > unpaid > worked (`non_working` is unreachable under
    the seven-day default, I-37). A NULL employment date leaves its side of the range open."""
    first, last = employment
    if (first is not None and day < first) or (last is not None and day > last):
        return DAY_NOT_EMPLOYED
    return _calendar_status(day, holidays) or (DAY_UNPAID if day in unpaid else DAY_WORKED)


class WorkCalendarService:
    """Working days, holidays and each agent's day statuses over an inclusive local date range."""

    @staticmethod
    def holidays(start: date, end: date) -> Dict[date, Optional[str]]:
        """Holidays in `[start, end]`, as date -> note: the union of `sales_pay_periods.holidays`
        over the months the range touches. A month with no period row has none. A null note reads
        as None, as in `unpaid_days`."""
        rows = (
            db.session.query(SalesPayPeriod.holidays)
            .filter(SalesPayPeriod.month_start >= month_start(start), SalesPayPeriod.month_start <= month_start(end))
            .all()
        )
        found: Dict[date, Optional[str]] = {}
        for (entries,) in rows:
            for entry in entries or ():
                day = date.fromisoformat(entry["date"])
                if start <= day <= end:
                    found[day] = entry.get("note")
        return found

    @staticmethod
    def working_dates(start: date, end: date) -> FrozenSet[date]:
        """Every day in `[start, end]` whose weekday works (all seven under the default), minus
        holidays. The company calendar: no agent's employment or unpaid days narrow it."""
        holidays = WorkCalendarService.holidays(start, end)
        return frozenset(day for day in _dates(start, end) if _calendar_status(day, holidays) is None)

    @staticmethod
    def day_statuses(agent_user_id: int, start: date, end: date) -> Dict[date, str]:
        """One agent's `SALES_PAY_DAY_STATUSES` value for every day in `[start, end]`."""
        return WorkCalendarService.day_statuses_many([agent_user_id], start, end)[int(agent_user_id)]

    @staticmethod
    def day_statuses_many(agent_user_ids: Sequence[int], start: date, end: date) -> Dict[int, Dict[date, str]]:
        """`day_statuses` for a roster in three reads (profiles, unpaid days, holidays), however
        many agents: `plan_vs_fact` asks for a whole page at once."""
        agent_ids = sorted({int(agent_user_id) for agent_user_id in agent_user_ids})
        if not agent_ids:
            return {}
        employment = WorkCalendarService._employment_by_agent(agent_ids)
        unpaid = WorkCalendarService._unpaid_by_agent(agent_ids, start, end)
        holidays = WorkCalendarService.holidays(start, end)
        days = list(_dates(start, end))
        return {
            agent_id: {
                day: _day_status(day, employment.get(agent_id, _UNBOUNDED), holidays, unpaid.get(agent_id, {}))
                for day in days
            }
            for agent_id in agent_ids
        }

    @staticmethod
    def worked_dates(agent_user_id: int, start: date, end: date) -> FrozenSet[date]:
        """The days the base pays for: status `worked`."""
        return frozenset(
            day
            for day, status in WorkCalendarService.day_statuses(agent_user_id, start, end).items()
            if status == DAY_WORKED
        )

    @staticmethod
    def unpaid_days(agent_user_id: int, start: date, end: date) -> Dict[date, Optional[str]]:
        """The agent's `sales_agent_unpaid_days` rows in `[start, end]`, as date -> note."""
        return WorkCalendarService._unpaid_by_agent([int(agent_user_id)], start, end).get(int(agent_user_id), {})

    @staticmethod
    def unpaid_day_rows(agent_user_id: int, start: date, end: date) -> List[SalesAgentUnpaidDay]:
        """The agent's `sales_agent_unpaid_days` rows in `[start, end]`, ordered by date. The one
        row reader: Task 7's `self_decisions` reads who marked each day (`created_by_user_id`)
        through it, and no other reader queries the table."""
        return WorkCalendarService._unpaid_rows([int(agent_user_id)], start, end)

    @staticmethod
    def employment(agent_user_id: int) -> Employment:
        """`(employment_start_date, employment_end_date)`; `(None, None)` for a user with no
        agent profile, who is unbounded."""
        return WorkCalendarService._employment_by_agent([int(agent_user_id)]).get(int(agent_user_id), _UNBOUNDED)

    @staticmethod
    def _employment_by_agent(agent_ids: Sequence[int]) -> Dict[int, Employment]:
        rows = (
            db.session.query(
                SalesAgentProfile.user_id,
                SalesAgentProfile.employment_start_date,
                SalesAgentProfile.employment_end_date,
            )
            .filter(SalesAgentProfile.user_id.in_(agent_ids))
            .all()
        )
        return {user_id: (first, last) for user_id, first, last in rows}

    @staticmethod
    def _unpaid_rows(agent_ids: Sequence[int], start: date, end: date) -> List[SalesAgentUnpaidDay]:
        """The one query on `sales_agent_unpaid_days`: the agents' rows in `[start, end]`, ordered
        by `(agent_user_id, unpaid_date)`."""
        return (
            db.session.query(SalesAgentUnpaidDay)
            .filter(
                SalesAgentUnpaidDay.agent_user_id.in_(agent_ids),
                SalesAgentUnpaidDay.unpaid_date >= start,
                SalesAgentUnpaidDay.unpaid_date <= end,
            )
            .order_by(SalesAgentUnpaidDay.agent_user_id, SalesAgentUnpaidDay.unpaid_date)
            .all()
        )

    @staticmethod
    def _unpaid_by_agent(agent_ids: Sequence[int], start: date, end: date) -> Dict[int, Dict[date, Optional[str]]]:
        found: Dict[int, Dict[date, Optional[str]]] = {}
        for row in WorkCalendarService._unpaid_rows(agent_ids, start, end):
            found.setdefault(row.agent_user_id, {})[row.unpaid_date] = row.note
        return found
