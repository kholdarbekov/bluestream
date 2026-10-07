"""`WorkCalendarService`: the one working-day calendar pay and plan compliance read (spec 2026-09-28 §4.2).

October 2026 is the month every pay example uses: the 1st is a Thursday and the Sundays are the 4th,
11th, 18th and 25th. Every day is a working day by default (C2 v5, D-DAYS), so October has 31.
Every expected status and count below is read off that calendar by hand; nothing here re-derives a
weekday rule.

The narrowed-default tests put the week back to Monday to Saturday with the one patch the calendar
allows, `monkeypatch.setattr(work_calendar, "is_working_weekday", ...)`, and get v4's figures: the
weekday branch still works, it is only unreachable under the default (I-37).

The rows are written directly: their writers (`set_holidays`, `set_unpaid_days`, `set_employment`)
refuse some of the shapes used here on purpose (see the precedence test).
"""

import ast
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesAgentUnpaidDay, SalesPayPeriod
from business_app.services.sales import work_calendar
from business_app.services.sales.work_calendar import WORKING_WEEKDAYS, WorkCalendarService, is_working_weekday
from shared.staff_constants import SALES_PAY_DAY_STATUSES
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CALENDAR_HOME = ROOT / "business_app" / "services" / "sales" / "work_calendar.py"
OCT_1, OCT_31 = date(2026, 10, 1), date(2026, 10, 31)
OCTOBER_SUNDAYS = {date(2026, 10, day) for day in (4, 11, 18, 25)}


def _oct(day):
    return date(2026, 10, day)


def _period(db, month, holidays):
    db.session.add(SalesPayPeriod(month_start=month, status="open", is_shadow=False, holidays=holidays))
    db.session.commit()


def _employed_agent(db, phone, *, start=None, end=None):
    agent = make_sales_agent_user(db, phone=phone, staff_roles=["sales_agent"])
    db.session.add(
        SalesAgentProfile(
            user_id=agent.id, districts=["chilanzar"], employment_start_date=start, employment_end_date=end
        )
    )
    db.session.commit()
    return agent


def _unpaid(db, agent, day, admin, note=None):
    db.session.add(
        SalesAgentUnpaidDay(agent_user_id=agent.id, unpaid_date=day, note=note, created_by_user_id=admin.id)
    )
    db.session.commit()


def _precedence_month(db, admin):
    """Employed 5-28 October. Holidays on the 1st, the 11th (a Sunday) and the 14th; unpaid rows on
    the 3rd, 11th, 14th and 15th. `set_unpaid_days` refuses an unpaid day on a holiday (spec §4.10),
    so the rows on the 11th and 14th stand for a hand-edited month: the calendar still gives each
    day exactly one answer."""
    agent = _employed_agent(db, "+998901234581", start=_oct(5), end=_oct(28))
    _period(
        db,
        OCT_1,
        [
            {"date": "2026-10-01", "note": "Teachers' Day"},
            {"date": "2026-10-11", "note": "hand edit"},
            {"date": "2026-10-14", "note": None},
        ],
    )
    for day, note in ((3, None), (11, None), (14, None), (15, "Family")):
        _unpaid(db, agent, _oct(day), admin, note)
    return agent


def test_every_day_of_the_week_is_a_working_day():
    """C2 v5, written once (I-37). `date.weekday()` numbers Monday 0 and Sunday 6; 5-11 October
    2026 runs Monday to Sunday."""
    week = [_oct(5) + timedelta(days=offset) for offset in range(7)]

    assert WORKING_WEEKDAYS == frozenset(range(7))
    assert [day.weekday() for day in week] == [0, 1, 2, 3, 4, 5, 6]
    assert [is_working_weekday(day) for day in week] == [True] * 7


def test_before_pay_starts_every_day_is_a_working_day(db):
    """No period rows yet, so no holidays: all 31 October days work, the four Sundays among them.
    Plan compliance reads the calendar this way until the first pay month is started."""
    assert WorkCalendarService.holidays(OCT_1, OCT_31) == {}
    working = WorkCalendarService.working_dates(OCT_1, OCT_31)
    assert len(working) == 31
    assert OCTOBER_SUNDAYS <= working
    assert {OCT_1, OCT_31} <= working  # a Thursday and a Saturday


def test_holidays_come_from_the_period_rows_of_the_months_touched(db):
    """The union of `sales_pay_periods.holidays` over the months a range touches, cut to the
    range. A month with no period row has none. A null note reads as None, as in `unpaid_days`."""
    _period(db, date(2026, 9, 1), [{"date": "2026-09-01", "note": "Independence Day"}])
    _period(db, OCT_1, [{"date": "2026-10-01", "note": "Teachers' Day"}, {"date": "2026-10-14", "note": None}])

    assert WorkCalendarService.holidays(OCT_1, OCT_31) == {OCT_1: "Teachers' Day", _oct(14): None}
    assert len(WorkCalendarService.working_dates(OCT_1, OCT_31)) == 29  # 31 - 2
    assert WorkCalendarService.holidays(date(2026, 8, 25), OCT_1) == {
        date(2026, 9, 1): "Independence Day",
        OCT_1: "Teachers' Day",
    }
    assert WorkCalendarService.holidays(date(2026, 9, 2), date(2026, 9, 30)) == {}
    assert WorkCalendarService.holidays(date(2026, 11, 1), date(2026, 11, 30)) == {}  # no November row


def test_each_day_has_one_status_by_precedence(db, admin_user):
    """not_employed > non_working > holiday > unpaid > worked (spec §4.2); `non_working` is
    unreachable under the seven-day default.

    Out of employment: 1-4 and 29-31 = 7. Holidays inside it: the 11th (a Sunday) and the 14th = 2.
    Unpaid: the 15th. Worked: the other 24 - 2 - 1 = 21.
    """
    agent = _precedence_month(db, admin_user)

    statuses = WorkCalendarService.day_statuses(agent.id, OCT_1, OCT_31)

    assert (len(statuses), min(statuses), max(statuses)) == (31, OCT_1, OCT_31)
    assert statuses[_oct(1)] == "not_employed"  # a holiday before the start
    assert statuses[_oct(3)] == "not_employed"  # an unpaid row before the start
    assert statuses[_oct(4)] == "not_employed"  # a Sunday before the start
    assert statuses[_oct(5)] == "worked"  # the start date is employed
    assert statuses[_oct(11)] == "holiday"  # a Sunday holiday outranks its unpaid row
    assert statuses[_oct(14)] == "holiday"  # a holiday outranks its unpaid row
    assert statuses[_oct(15)] == "unpaid"
    assert statuses[_oct(18)] == "worked"  # a plain Sunday is worked
    assert statuses[_oct(28)] == "worked"  # the end date is employed
    assert statuses[_oct(29)] == "not_employed"
    assert Counter(statuses.values()) == {"worked": 21, "holiday": 2, "unpaid": 1, "not_employed": 7}
    assert set(statuses.values()) <= set(SALES_PAY_DAY_STATUSES) - {"non_working"}
    assert WorkCalendarService.worked_dates(agent.id, OCT_1, OCT_31) == frozenset(
        day for day, status in statuses.items() if status == "worked"
    )
    assert WorkCalendarService.employment(agent.id) == (_oct(5), _oct(28))
    # The rows themselves, whatever their day's status (read through `_unpaid_rows`, like `unpaid_day_rows`).
    assert WorkCalendarService.unpaid_days(agent.id, OCT_1, OCT_31) == {
        _oct(3): None,
        _oct(11): None,
        _oct(14): None,
        _oct(15): "Family",
    }


def test_an_open_or_missing_employment_is_unbounded(db, sales_agent_user, admin_user):
    """A NULL date leaves its side open, and a user with no agent profile is unbounded.

    Unbounded: all 31 days worked. Starting on the 5th: the 1st-4th are before it, leaving 27."""
    starts_on_the_5th = _employed_agent(db, "+998901234582", start=_oct(5))

    for unbounded in (sales_agent_user, admin_user):
        assert Counter(WorkCalendarService.day_statuses(unbounded.id, OCT_1, OCT_31).values()) == {"worked": 31}
        assert WorkCalendarService.employment(unbounded.id) == (None, None)
    assert Counter(WorkCalendarService.day_statuses(starts_on_the_5th.id, OCT_1, OCT_31).values()) == {
        "not_employed": 4,
        "worked": 27,
    }
    assert WorkCalendarService.employment(starts_on_the_5th.id) == (_oct(5), None)


def test_a_narrowed_week_gives_back_the_monday_to_saturday_calendar(db, admin_user, sales_agent_user, monkeypatch):
    """The weekday branch still works (I-37). Narrowed back to Monday to Saturday through the one
    reader, October with no period row has 27 working days and an unbounded agent works 27, with 4
    `non_working` Sundays. In the precedence month a Sunday outranks its holiday and its unpaid
    row, and v4's counts come back: out of employment 7; Sundays inside it 11, 18, 25 = 3; holiday
    the 14th; unpaid the 15th; worked 24 - 3 - 1 - 1 = 19."""
    monkeypatch.setattr(work_calendar, "is_working_weekday", lambda d: d.weekday() != 6)

    working = WorkCalendarService.working_dates(OCT_1, OCT_31)
    unbounded = Counter(WorkCalendarService.day_statuses(sales_agent_user.id, OCT_1, OCT_31).values())
    agent = _precedence_month(db, admin_user)
    statuses = WorkCalendarService.day_statuses(agent.id, OCT_1, OCT_31)

    assert len(working) == 27
    assert OCTOBER_SUNDAYS.isdisjoint(working)
    assert unbounded == {"worked": 27, "non_working": 4}
    assert statuses[_oct(11)] == "non_working"
    assert Counter(statuses.values()) == {"worked": 19, "non_working": 3, "holiday": 1, "unpaid": 1, "not_employed": 7}


def test_only_work_calendar_names_the_working_week():
    """S-37, I-37: the default has one home and one reader. No module under business_app/ but
    work_calendar.py names `WORKING_WEEKDAYS` (a bare name, an attribute or an import), and inside
    work_calendar.py only `is_working_weekday` reads it, so narrowing the week again is one line
    that every surface follows."""
    offenders = []
    for path in sorted((ROOT / "business_app").rglob("*.py")):
        if path == CALENDAR_HOME:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if (
                (isinstance(node, ast.Name) and node.id == "WORKING_WEEKDAYS")
                or (isinstance(node, ast.Attribute) and node.attr == "WORKING_WEEKDAYS")
                or (isinstance(node, ast.alias) and node.name == "WORKING_WEEKDAYS")
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    readers = {
        function.name
        for function in ast.walk(ast.parse(CALENDAR_HOME.read_text(encoding="utf-8")))
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == "WORKING_WEEKDAYS"
    }

    assert offenders == []
    assert readers == {"is_working_weekday"}


def test_a_roster_costs_the_same_three_reads_as_one_agent(db, admin_user, sales_agent_user, count_queries):
    """`plan_vs_fact` reads a whole roster's days at once: one profile read, one unpaid-day read and
    one holiday read, however many agents. Each agent's answer is `day_statuses`' answer."""
    first = _employed_agent(db, "+998901234583", start=_oct(5), end=_oct(28))
    second = _employed_agent(db, "+998901234584")
    _period(db, OCT_1, [{"date": "2026-10-14", "note": None}])
    _unpaid(db, second, _oct(15), admin_user)
    # Read before counting: the commits above expired every instance.
    ids = [first.id, second.id, sales_agent_user.id, admin_user.id]

    with count_queries() as one:
        WorkCalendarService.day_statuses_many(ids[:1], OCT_1, OCT_31)
    with count_queries() as roster:
        many = WorkCalendarService.day_statuses_many(ids, OCT_1, OCT_31)

    assert one.count == roster.count == 3, roster.statements
    assert many == {agent_id: WorkCalendarService.day_statuses(agent_id, OCT_1, OCT_31) for agent_id in ids}
    assert WorkCalendarService.day_statuses_many([], OCT_1, OCT_31) == {}


def test_unpaid_days_are_this_agents_in_this_range(db, admin_user):
    agent = _employed_agent(db, "+998901234585")
    colleague = _employed_agent(db, "+998901234586")
    _unpaid(db, agent, date(2026, 9, 30), admin_user)  # the day before the range
    _unpaid(db, agent, _oct(12), admin_user, "Doctor")
    _unpaid(db, colleague, _oct(13), admin_user)

    assert WorkCalendarService.unpaid_days(agent.id, OCT_1, OCT_31) == {_oct(12): "Doctor"}
    assert WorkCalendarService.unpaid_days(colleague.id, OCT_1, OCT_31) == {_oct(13): None}


def test_unpaid_day_rows_are_this_agents_rows_by_date(db, admin_user):
    """The one row reader (plan ruling PR10): Task 7's `self_decisions` reads who marked each day
    from it. The range is inclusive at both ends, the rows come back by date whatever order they
    were written in, a colleague's rows stay out, and each row carries `created_by_user_id`."""
    agent = _employed_agent(db, "+998901234587")
    colleague = _employed_agent(db, "+998901234588")
    admin_id = admin_user.id
    written = (
        (OCT_31, None),
        (date(2026, 11, 1), None),  # the day after the range
        (_oct(15), "Family"),
        (date(2026, 9, 30), None),  # the day before the range
        (OCT_1, None),
    )
    for day, note in written:
        _unpaid(db, agent, day, admin_user, note)
    _unpaid(db, colleague, _oct(2), admin_user)

    rows = WorkCalendarService.unpaid_day_rows(agent.id, OCT_1, OCT_31)

    assert [(row.agent_user_id, row.unpaid_date, row.note, row.created_by_user_id) for row in rows] == [
        (agent.id, OCT_1, None, admin_id),
        (agent.id, _oct(15), "Family", admin_id),
        (agent.id, OCT_31, None, admin_id),
    ]
    colleague_rows = WorkCalendarService.unpaid_day_rows(colleague.id, OCT_1, OCT_31)
    assert [(row.agent_user_id, row.unpaid_date) for row in colleague_rows] == [(colleague.id, _oct(2))]
    assert WorkCalendarService.unpaid_day_rows(agent.id, _oct(2), _oct(14)) == []
