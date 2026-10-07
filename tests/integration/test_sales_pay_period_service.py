"""Sales-agent pay months (spec §4.9, §4.10 "One editability rule", §3.4). Covered here:
- `start`, the epoch;
- `ensure_periods`, the rows after the epoch;
- the period-row locks;
- which month a plan or terms may start in;
- the lifecycle guards in check mode (`allowed_actions`);
- a month's holidays.

These are service-level tests. The routes that drive these services, A0 (start), A1/A2
(`next_actions`) and A7 (holidays), arrive in Task 7 with the period read models, and their HTTP
halves live in tests/integration/test_sales_pay_months_api.py. The plan routes reach
`assert_month_editable` through HTTP in tests/integration/test_admin_sales_pay_plans_api.py.

Close, approve and mark-paid are Task 7's. Here `move_period` leaves a month closed, approved or
paid: it writes the status and the stamp columns the period CHECKs pair with it, the row shape
those transitions leave, and holds no rule of its own.

Every call passes `now=`, so nothing here reads the wall clock.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from business_app.models.sales_pay import SalesPayPeriod
from business_app.services.sales import work_calendar
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from tests.integration.sales_pay_builders import pay_audit
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture

pytestmark = pytest.mark.integration

TZ = ZoneInfo("Asia/Tashkent")
SEP, OCT, NOV, DEC, JAN = date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)
IN_SEPTEMBER = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)  # Tue 15 Sep 2026, 10:00 local
IN_OCTOBER = datetime(2026, 10, 20, 10, 0, tzinfo=TZ)  # Tue 20 Oct 2026, 10:00 local = 05:00 UTC
IN_DECEMBER = datetime(2026, 12, 3, 9, 0, tzinfo=TZ)  # Thu 3 Dec 2026, 09:00 local
STAMP = datetime(2026, 12, 2, 6, 0, tzinfo=timezone.utc)


def move_period(db, period, status, actor):
    """Leave `period` as close / approve / mark-paid do: the status plus every stamp its CHECKs pair with it.

    `actor.id` is read into a local BEFORE `period.status` is touched: `admin_user`'s attributes are
    expired by an earlier commit (e.g. `start`'s), so `actor.id` triggers its own SELECT, which
    autoflushes the session. Reading it first keeps `period.status` clean until that happens, so the
    autoflush never sees `status='closed'` paired with a still-NULL `closed_at` (the CHECK's own rule).
    """
    actor_id = actor.id
    period.status = status
    period.closed_at, period.closed_by_user_id = STAMP, actor_id
    if status in ("approved", "paid"):
        period.approved_at, period.approved_by_user_id = STAMP, actor_id
    if status == "paid":
        period.paid_on, period.paid_recorded_at, period.paid_by_user_id = date(2026, 12, 5), STAMP, actor_id
    db.session.commit()
    return period


def open_months(db, actor, through=IN_DECEMBER, *, shadow=False):
    """Pay started in September (the epoch) and every month to `through` opened, as a nightly sync leaves them."""
    SalesPayPeriodService.start(SEP, is_shadow=shadow, actor_id=actor.id, now=IN_SEPTEMBER)
    return {period.month_start: period for period in SalesPayPeriodService.ensure_periods(through)}


def _refusal(excinfo):
    return excinfo.value.error_code, excinfo.value.details


# --------------------------------------------------------------------------- #
# The epoch and the rows after it
# --------------------------------------------------------------------------- #


def test_before_start_there_is_no_epoch_and_nothing_to_open(db):
    assert SalesPayPeriodService.epoch() is None
    assert SalesPayPeriodService.first_unapproved_month() is None
    assert SalesPayPeriodService.ensure_periods(IN_DECEMBER) == []
    assert SalesPayPeriod.query.count() == 0


def test_start_opens_the_current_month_as_the_epoch(db, admin_user, audit_events):
    period = SalesPayPeriodService.start(SEP, is_shadow=True, actor_id=admin_user.id, now=IN_SEPTEMBER)

    assert (period.month_start, period.status, period.is_shadow, period.holidays) == (SEP, "open", True, [])
    assert SalesPayPeriodService.epoch() == SEP
    assert pay_audit(audit_events, "start") == [
        (
            "sales_pay_period",
            str(period.id),
            None,
            {"month": "2026-09", "is_shadow": True, "actor_user_id": admin_user.id},
        )
    ]


def test_start_refuses_any_month_but_the_current_one(db, admin_user):
    with pytest.raises(ValidationError) as refused:
        SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_SEPTEMBER)

    assert _refusal(refused) == ("SALES_PAY_MONTH_INVALID", {"month": "2026-10", "expected": "2026-09"})
    assert SalesPayPeriod.query.count() == 0


def test_start_refuses_a_second_start(db, admin_user):
    SalesPayPeriodService.start(SEP, is_shadow=False, actor_id=admin_user.id, now=IN_SEPTEMBER)

    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)

    assert _refusal(refused) == ("SALES_PAY_ALREADY_STARTED", {"month": "2026-09"})
    assert SalesPayPeriod.query.count() == 1


def test_ensure_periods_opens_every_missing_month_to_now_and_never_a_future_one(db, admin_user):
    """The epoch is a shadow September and now is 3 December. October to December open as real
    months, January does not open, a second run adds nothing, and a closed month is left alone."""
    SalesPayPeriodService.start(SEP, is_shadow=True, actor_id=admin_user.id, now=IN_SEPTEMBER)

    months = SalesPayPeriodService.ensure_periods(IN_DECEMBER)

    assert [(p.month_start, p.status, p.is_shadow, p.holidays) for p in months] == [
        (SEP, "open", True, []),
        (OCT, "open", False, []),
        (NOV, "open", False, []),
        (DEC, "open", False, []),
    ]
    assert SalesPayPeriod.query.filter_by(month_start=JAN).count() == 0

    move_period(db, months[0], "closed", admin_user)
    again = SalesPayPeriodService.ensure_periods(IN_DECEMBER)

    assert [(p.id, p.status) for p in again] == [(months[0].id, "closed")] + [(p.id, "open") for p in months[1:]]
    assert SalesPayPeriod.query.count() == 4


def test_get_names_the_month_it_could_not_find(db, admin_user):
    months = open_months(db, admin_user)

    assert SalesPayPeriodService.get(OCT).id == months[OCT].id
    with pytest.raises(NotFoundError) as missing:
        SalesPayPeriodService.get(JAN)
    assert _refusal(missing) == ("SALES_PAY_NOT_FOUND", {"resource": "period", "id": "2027-01"})


# --------------------------------------------------------------------------- #
# Locks and "is this month still unapproved"
# --------------------------------------------------------------------------- #


def test_lock_open_periods_is_the_open_months_in_month_order(db, admin_user):
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "closed", admin_user)

    assert [p.month_start for p in SalesPayPeriodService.lock_open_periods()] == [OCT, NOV, DEC]


def test_lock_writable_refuses_a_status_it_was_not_given(db, admin_user):
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "closed", admin_user)

    assert SalesPayPeriodService.lock_writable(months[OCT].id, ("open",)).month_start == OCT
    assert SalesPayPeriodService.lock_writable(months[SEP].id, ("open", "closed")).month_start == SEP
    with pytest.raises(ConflictError) as locked:
        SalesPayPeriodService.lock_writable(months[SEP].id, ("open",))
    assert _refusal(locked) == ("SALES_PAY_MONTH_LOCKED", {"month": "2026-09", "status": "closed"})
    with pytest.raises(NotFoundError) as missing:
        SalesPayPeriodService.lock_writable(999999, ("open",))
    assert _refusal(missing) == ("SALES_PAY_NOT_FOUND", {"resource": "period", "id": 999999})


def test_inputs_are_writable_while_the_month_is_open_or_closed(db, admin_user):
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "approved", admin_user)
    move_period(db, months[OCT], "closed", admin_user)

    assert SalesPayPeriodService.assert_inputs_writable(OCT).id == months[OCT].id
    assert SalesPayPeriodService.assert_inputs_writable(NOV).id == months[NOV].id
    with pytest.raises(ConflictError) as locked:
        SalesPayPeriodService.assert_inputs_writable(SEP)
    assert _refusal(locked) == ("SALES_PAY_MONTH_LOCKED", {"month": "2026-09", "status": "approved"})
    with pytest.raises(NotFoundError) as missing:
        SalesPayPeriodService.assert_inputs_writable(JAN)
    assert _refusal(missing) == ("SALES_PAY_NOT_FOUND", {"resource": "period", "id": "2027-01"})


def test_first_unapproved_month_is_the_earliest_open_or_closed_one(db, admin_user):
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "approved", admin_user)
    move_period(db, months[OCT], "closed", admin_user)

    assert SalesPayPeriodService.first_unapproved_month() == OCT
    move_period(db, months[OCT], "approved", admin_user)
    assert SalesPayPeriodService.first_unapproved_month() == NOV


# --------------------------------------------------------------------------- #
# Which month a plan version or terms may start in (spec §4.10)
# --------------------------------------------------------------------------- #


def test_before_start_the_current_month_is_the_first_editable_one(db):
    assert SalesPayPeriodService.editable_from_month(IN_OCTOBER) == OCT
    assert SalesPayPeriodService.assert_month_editable(OCT, now=IN_OCTOBER) is None
    assert SalesPayPeriodService.assert_month_editable(NOV, now=IN_OCTOBER) is None
    with pytest.raises(ConflictError) as locked:
        SalesPayPeriodService.assert_month_editable(SEP, now=IN_OCTOBER)
    assert _refusal(locked) == (
        "SALES_PAY_MONTH_LOCKED",
        {"month": "2026-09", "status": None, "editable_from_month": "2026-10"},
    )


def test_after_start_the_earliest_open_month_is_the_first_editable_one(db, admin_user):
    """In December with September closed, October is still open. Its plans and terms may change
    until it closes, and a future month may be configured ahead."""
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "closed", admin_user)

    assert SalesPayPeriodService.editable_from_month(IN_DECEMBER) == OCT
    assert SalesPayPeriodService.assert_month_editable(OCT, now=IN_DECEMBER).id == months[OCT].id
    assert SalesPayPeriodService.assert_month_editable(JAN, now=IN_DECEMBER) is None
    with pytest.raises(ConflictError) as locked:
        SalesPayPeriodService.assert_month_editable(SEP, now=IN_DECEMBER)
    assert _refusal(locked) == (
        "SALES_PAY_MONTH_LOCKED",
        {"month": "2026-09", "status": "closed", "editable_from_month": "2026-10"},
    )


def test_a_pay_month_is_the_first_of_a_month(db):
    with pytest.raises(ValidationError) as refused:
        SalesPayPeriodService.assert_month_editable(date(2026, 10, 15), now=IN_OCTOBER)

    assert _refusal(refused) == ("SALES_PAY_MONTH_INVALID", {"month": "2026-10-15"})


# --------------------------------------------------------------------------- #
# The lifecycle guards, in check mode
# --------------------------------------------------------------------------- #


def test_a_month_can_be_closed_an_hour_after_the_last_abandon_sweep_could_run(db, app, monkeypatch):
    """October ends at local midnight on 1 November, which is 2026-10-31T19:00Z. With a 6-hour
    abandon sweep, (6 + 1) hours later is 2026-11-01T02:00Z."""
    monkeypatch.setitem(app.config, "SALES_VISIT_AUTO_ABANDON_HOURS", 6)

    assert SalesPayPeriodService.closable_from(OCT) == datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)


def test_an_open_month_offers_close_only_from_closable_from(db, app, admin_user, monkeypatch):
    monkeypatch.setitem(app.config, "SALES_VISIT_AUTO_ABANDON_HOURS", 6)
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "approved", admin_user)
    just_before = datetime(2026, 11, 1, 1, 59, 59, tzinfo=timezone.utc)
    closable = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)

    assert SalesPayPeriodService.allowed_actions(months[OCT], now=just_before) == ["recalculate"]
    assert SalesPayPeriodService.allowed_actions(months[OCT], now=closable) == ["close", "recalculate"]
    with pytest.raises(ConflictError) as early:
        SalesPayPeriodService._guard_close(months[OCT], just_before)
    assert _refusal(early) == ("SALES_PAY_PERIOD_NOT_ENDED", {"closable_from": "2026-11-01T02:00:00+00:00"})


def test_a_month_cannot_close_while_the_month_before_is_open(db, admin_user):
    months = open_months(db, admin_user)
    after_october = datetime(2026, 11, 5, 9, 0, tzinfo=TZ)

    assert SalesPayPeriodService.allowed_actions(months[SEP], now=after_october) == ["close", "recalculate"]
    assert SalesPayPeriodService.allowed_actions(months[OCT], now=after_october) == ["recalculate"]
    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService._guard_close(months[OCT], after_october)
    assert _refusal(refused) == ("SALES_PAY_PREVIOUS_PERIOD_OPEN", {"month": "2026-09"})


def test_a_closed_month_offers_approve_once_the_month_before_is_approved(db, admin_user):
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "closed", admin_user)
    move_period(db, months[OCT], "closed", admin_user)

    assert SalesPayPeriodService.allowed_actions(months[SEP], now=IN_DECEMBER) == ["recalculate", "approve"]
    assert SalesPayPeriodService.allowed_actions(months[OCT], now=IN_DECEMBER) == ["recalculate"]
    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService._guard_approve(months[OCT])
    assert _refusal(refused) == ("SALES_PAY_PREVIOUS_PERIOD_NOT_APPROVED", {"month": "2026-09", "status": "closed"})

    move_period(db, months[SEP], "approved", admin_user)

    assert SalesPayPeriodService.allowed_actions(months[OCT], now=IN_DECEMBER) == ["recalculate", "approve"]
    assert SalesPayPeriodService.allowed_actions(months[SEP], now=IN_DECEMBER) == ["mark_paid"]


def test_a_shadow_month_is_never_marked_paid(db, admin_user):
    """I-16: a shadow month runs the lifecycle up to approved and is never paid. A paid month offers nothing."""
    months = open_months(db, admin_user, shadow=True)
    move_period(db, months[SEP], "approved", admin_user)
    move_period(db, months[OCT], "approved", admin_user)

    assert SalesPayPeriodService.allowed_actions(months[SEP], now=IN_DECEMBER) == []
    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService._guard_mark_paid(months[SEP])
    assert _refusal(refused) == (
        "SALES_PAY_STATE_INVALID",
        {
            "resource": "period",
            "status": "approved",
            "action": "mark_paid",
            "allowed": ["approved"],
            "reason": "shadow",
        },
    )
    assert SalesPayPeriodService.allowed_actions(months[OCT], now=IN_DECEMBER) == ["mark_paid"]
    move_period(db, months[OCT], "paid", admin_user)
    assert SalesPayPeriodService.allowed_actions(months[OCT], now=IN_DECEMBER) == []


@pytest.mark.parametrize(
    "guard,status,action,allowed",
    [
        ("_guard_close", "closed", "close", ["open"]),
        ("_guard_recalculate", "approved", "recalculate", ["open", "closed"]),
        ("_guard_approve", "open", "approve", ["closed"]),
        ("_guard_mark_paid", "closed", "mark_paid", ["approved"]),
    ],
    ids=["close-a-closed-month", "recalculate-an-approved-month", "approve-an-open-month", "pay-a-closed-month"],
)
def test_each_guard_names_the_status_it_refused(db, admin_user, guard, status, action, allowed):
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "approved", admin_user)
    period = months[OCT] if status == "open" else move_period(db, months[OCT], status, admin_user)
    check = getattr(SalesPayPeriodService, guard)

    with pytest.raises(ConflictError) as refused:
        check(period, IN_DECEMBER) if guard == "_guard_close" else check(period)

    assert _refusal(refused) == (
        "SALES_PAY_STATE_INVALID",
        {"resource": "period", "status": status, "action": action, "allowed": allowed},
    )


# --------------------------------------------------------------------------- #
# Holidays
# --------------------------------------------------------------------------- #


def test_holidays_replace_the_months_list_sorted_with_their_notes(db, admin_user, audit_events):
    """1 October 2026 is a Thursday, 14 October a Wednesday and 2 October a Friday: all working days."""
    period = SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)

    SalesPayPeriodService.set_holidays(
        OCT,
        [{"date": date(2026, 10, 14), "note": " Teachers' day "}, {"date": date(2026, 10, 1), "note": None}],
        actor_id=admin_user.id,
        now=IN_OCTOBER,
    )
    first = [{"date": "2026-10-01", "note": None}, {"date": "2026-10-14", "note": "Teachers' day"}]
    assert SalesPayPeriodService.get(OCT).holidays == first

    SalesPayPeriodService.set_holidays(OCT, [{"date": date(2026, 10, 2)}], actor_id=admin_user.id, now=IN_OCTOBER)

    assert SalesPayPeriodService.get(OCT).holidays == [{"date": "2026-10-02", "note": None}]
    assert pay_audit(audit_events, "holidays_set") == [
        (
            "sales_pay_period",
            str(period.id),
            {"month": "2026-10", "holidays": []},
            {"month": "2026-10", "holidays": first, "actor_user_id": admin_user.id},
        ),
        (
            "sales_pay_period",
            str(period.id),
            {"month": "2026-10", "holidays": first},
            {"month": "2026-10", "holidays": [{"date": "2026-10-02", "note": None}], "actor_user_id": admin_user.id},
        ),
    ]


@pytest.mark.parametrize(
    "days,refused_date,reason",
    [
        pytest.param([date(2026, 11, 2)], "2026-11-02", "outside_month", id="outside-the-month"),
        pytest.param([date(2026, 10, 5), date(2026, 10, 5)], "2026-10-05", "duplicate", id="listed-twice"),
    ],
)
def test_holidays_refuse_a_day_outside_the_month_or_a_repeat(db, admin_user, days, refused_date, reason):
    period = SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)

    with pytest.raises(ValidationError) as refused:
        SalesPayPeriodService.set_holidays(
            OCT, [{"date": day, "note": None} for day in days], actor_id=admin_user.id, now=IN_OCTOBER
        )

    assert _refusal(refused) == ("SALES_PAY_DATE_INVALID", {"field": "days", "date": refused_date, "reason": reason})
    db.session.expire_all()
    assert db.session.get(SalesPayPeriod, period.id).holidays == []


def test_a_sunday_holiday_is_stored(db, admin_user):
    """C2 v5 (I-37): every day is a working day, so Sunday 4 October takes a company holiday like
    any other day. The weekday guard is still in `set_holidays`; only a narrowed week reaches it."""
    SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)

    SalesPayPeriodService.set_holidays(
        OCT, [{"date": date(2026, 10, 4), "note": "Company day off"}], actor_id=admin_user.id, now=IN_OCTOBER
    )

    assert SalesPayPeriodService.get(OCT).holidays == [{"date": "2026-10-04", "note": "Company day off"}]


def test_a_narrowed_week_refuses_a_sunday_holiday(db, admin_user, monkeypatch):
    """The weekday branch of `set_holidays` (§4.9), unreachable under the seven-day default: with
    the week narrowed back to Monday to Saturday through the one reader, Sunday 4 October is
    refused `not_working_day` and nothing is written."""
    period = SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)
    monkeypatch.setattr(work_calendar, "is_working_weekday", lambda d: d.weekday() != 6)

    with pytest.raises(ValidationError) as refused:
        SalesPayPeriodService.set_holidays(
            OCT, [{"date": date(2026, 10, 4), "note": None}], actor_id=admin_user.id, now=IN_OCTOBER
        )

    assert _refusal(refused) == (
        "SALES_PAY_DATE_INVALID",
        {"field": "days", "date": "2026-10-04", "reason": "not_working_day"},
    )
    db.session.expire_all()
    assert db.session.get(SalesPayPeriod, period.id).holidays == []


def test_holidays_are_written_while_closed_and_refused_once_approved(db, admin_user):
    """1 September 2026 is a Tuesday."""
    months = open_months(db, admin_user)
    move_period(db, months[SEP], "closed", admin_user)

    SalesPayPeriodService.set_holidays(
        SEP, [{"date": date(2026, 9, 1), "note": "Independence Day"}], actor_id=admin_user.id, now=IN_DECEMBER
    )
    assert SalesPayPeriodService.get(SEP).holidays == [{"date": "2026-09-01", "note": "Independence Day"}]

    move_period(db, months[SEP], "approved", admin_user)
    with pytest.raises(ConflictError) as locked:
        SalesPayPeriodService.set_holidays(SEP, [], actor_id=admin_user.id, now=IN_DECEMBER)
    assert _refusal(locked) == ("SALES_PAY_MONTH_LOCKED", {"month": "2026-09", "status": "approved"})
    assert SalesPayPeriodService.get(SEP).holidays == [{"date": "2026-09-01", "note": "Independence Day"}]

    with pytest.raises(NotFoundError) as missing:
        SalesPayPeriodService.set_holidays(JAN, [], actor_id=admin_user.id, now=IN_DECEMBER)
    assert _refusal(missing) == ("SALES_PAY_NOT_FOUND", {"resource": "period", "id": "2027-01"})
