"""Every day is a working day (C2 v5, D-DAYS): spec §10.3 T-DAYS-2 and Review Focus 3.

Driven through the routes the Pay tab and the bot call: A2 `GET /periods/<month>` (the Holidays
modal's candidates are its `calendar.days[].is_working_day`), A7 `PUT /periods/<month>/holidays`,
A8 `GET /periods/<month>/agents/<id>` (the Unpaid-days modal's candidates are its `days[]` that are
`worked` or `unpaid`), A10 `PUT /periods/<month>/agents/<id>/unpaid-days`, and S1
`GET /staff/sales/me/earnings` as the bot calls it. R41's base is pinned here too.

October 2026: the 1st is a Thursday; the Sundays are the 4th, 11th, 18th and 25th. Base 3,000,000,
employed since 1 June, pay started for October. A8's `days` run from the 1st to today (the gate
window), so the reads that look at a later Sunday move the clock past it first.

The narrowed-default test puts the week back to Monday to Saturday with the one patch the
calendar allows: the weekday refusals still exist, they are only unreachable (I-37, I-38).
"""

import pytest

from business_app.services.sales import work_calendar
from tests.integration.sales_pay_builders import local_at, move_clock, pay_call, pay_route, pay_world

pytestmark = pytest.mark.integration

EARNINGS = "/api/v1/staff/sales/me/earnings"


def _october(db, client, monkeypatch, admin, headers, agent):
    """Pay started for October 2026 (not shadow), base 3,000,000, employed since 1 June; the clock
    at 10:00 on Thursday 15 October."""
    world = pay_world(db, client, monkeypatch, admin, headers, agent, employment_start="2026-06-01")
    move_clock(world, local_at(2026, 10, 15))
    return world


def _calendar_day(detail, day):
    return next(row for row in detail["calendar"]["days"] if row["date"] == day)


def _day_statuses(statement):
    return {row["date"]: row["day_status"] for row in statement["days"]}


def test_t_days_2_a_sunday_takes_a_holiday_and_an_unpaid_day_through_the_routes(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """T-DAYS-2. A2 October publishes 31 working days, Sunday the 4th among them. A7 makes the 4th
    a holiday: 30. A10 marks Sunday the 11th unpaid: 29 worked, base 3,000,000 x 29 / 30 =
    2,900,000. A10 on the holiday is refused `not_working_day`, and the unpaid set stays [11th].
    Read on Monday 19 October, A8's days show the 4th `holiday`, the 11th `unpaid` and Sunday the
    18th `worked`."""
    world = _october(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent = sales_agent_user.id

    before = pay_route(world, "GET", "/periods/2026-10")
    pay_route(world, "PUT", "/periods/2026-10/holidays", {"days": [{"date": "2026-10-04", "note": "Company day off"}]})
    with_holiday = pay_route(world, "GET", "/periods/2026-10")
    pay_route(world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": "2026-10-11"}]})
    on_the_holiday = pay_route(
        world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": "2026-10-04"}]}, status=400
    )
    move_clock(world, local_at(2026, 10, 19))
    statement = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")

    assert (before["calendar"]["working_days"], len(before["calendar"]["days"])) == (31, 31)
    assert _calendar_day(before, "2026-10-04") == {
        "date": "2026-10-04",
        "weekday": 6,
        "is_working_day": True,
        "holiday_note": None,
    }
    assert with_holiday["calendar"]["working_days"] == 30
    assert _calendar_day(with_holiday, "2026-10-04")["is_working_day"] is False
    assert (on_the_holiday["error_code"], on_the_holiday["details"]) == (
        "SALES_PAY_DATE_INVALID",
        {"field": "days", "date": "2026-10-04", "reason": "not_working_day", "validation_errors": []},
    )
    assert statement["inputs"]["unpaid_days"] == [{"date": "2026-10-11", "note": None}]
    assert (
        statement["inputs"]["working_days"],
        statement["inputs"]["worked_days"],
        statement["summary"]["base_amount"],
    ) == (30, 29, 2900000.0)
    days = _day_statuses(statement)
    assert (days["2026-10-04"], days["2026-10-11"], days["2026-10-18"]) == ("holiday", "unpaid", "worked")


def test_t_days_2_a_narrowed_week_refuses_sundays_and_writes_nothing(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """T-DAYS-2's narrowed default, in a fresh world: patched back to Monday to Saturday, A2
    publishes v4's 27 working days with Sunday the 18th not a working day, A7 on Sunday the 18th
    and A10 on Sunday the 25th are both refused `not_working_day`, and nothing is written: no
    holiday, no unpaid day, base 3,000,000 x 27 / 27."""
    world = _october(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent = sales_agent_user.id
    monkeypatch.setattr(work_calendar, "is_working_weekday", lambda d: d.weekday() != 6)

    month = pay_route(world, "GET", "/periods/2026-10")
    holiday = pay_route(world, "PUT", "/periods/2026-10/holidays", {"days": [{"date": "2026-10-18"}]}, status=400)
    unpaid = pay_route(
        world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": "2026-10-25"}]}, status=400
    )
    after = pay_route(world, "GET", "/periods/2026-10")
    statement = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")

    assert month["calendar"]["working_days"] == 27
    assert _calendar_day(month, "2026-10-18")["is_working_day"] is False
    for refused, day in ((holiday, "2026-10-18"), (unpaid, "2026-10-25")):
        assert (refused["error_code"], refused["details"]) == (
            "SALES_PAY_DATE_INVALID",
            {"field": "days", "date": day, "reason": "not_working_day", "validation_errors": []},
        )
    assert (after["holidays"], statement["inputs"]["holidays"], statement["inputs"]["unpaid_days"]) == ([], [], [])
    assert (
        statement["inputs"]["working_days"],
        statement["inputs"]["worked_days"],
        statement["summary"]["base_amount"],
    ) == (27, 27, 3000000.0)


def test_review_focus_3_a_sunday_holiday_and_a_sunday_unpaid_day_agree_on_every_surface(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """Review Focus 3. A7 makes Sunday 4 October a holiday and A10 marks Sunday 18 October unpaid.
    Before the writes, A2 offers both Sundays to the Holidays modal. Read on Tuesday 20 October,
    A2's agent row, A8 and S1 agree: 30 working days, 29 worked, base 3,000,000 x 29 / 30 =
    2,900,000; A8 lists the 4th `holiday`, the 18th `unpaid` and Sunday the 11th `worked` (an
    Unpaid-days candidate). The bot's "29 of 30" is the journey fixture in
    tests/staff_bot/test_sales_earnings_journey.py, fed these S1 keys."""
    world = _october(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent = sales_agent_user.id

    offered = pay_route(world, "GET", "/periods/2026-10")
    pay_route(world, "PUT", "/periods/2026-10/holidays", {"days": [{"date": "2026-10-04", "note": "Company day off"}]})
    pay_route(
        world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": "2026-10-18", "note": "Rest day"}]}
    )
    move_clock(world, local_at(2026, 10, 20))
    month = pay_route(world, "GET", "/periods/2026-10")
    statement = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")
    (open_month,) = pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)["open_months"]

    assert [_calendar_day(offered, day)["is_working_day"] for day in ("2026-10-04", "2026-10-18")] == [True, True]
    (row,) = month["agents"]
    assert (row["working_days"], row["worked_days"], row["base_amount"]) == (30, 29, 2900000.0)
    assert (
        statement["inputs"]["working_days"],
        statement["inputs"]["worked_days"],
        statement["summary"]["base_amount"],
    ) == (30, 29, 2900000.0)
    assert open_month["estimate"]["base"] == {
        "monthly": 3000000.0,
        "amount": 2900000.0,
        "worked_days": 29,
        "working_days": 30,
    }
    days = _day_statuses(statement)
    assert (days["2026-10-04"], days["2026-10-11"], days["2026-10-18"]) == ("holiday", "worked", "unpaid")


def test_r41_four_recorded_sundays_take_example_as_base_to_4_032_258(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """R41, the cost of OQ-D1's default (no paid rest day, I-38): Example A's base of 5,000,000
    with the four Sundays recorded as unpaid days besides the 12th and the 13th. October keeps 31
    working days and the agent works 25: 5,000,000 x 25 / 31 = 4,032,258.06 -> 4,032,258."""
    world = pay_world(
        db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user,
        base=5000000, employment_start="2026-06-01",
    )
    move_clock(world, local_at(2026, 10, 15))
    agent = sales_agent_user.id
    unpaid = ["2026-10-04", "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-18", "2026-10-25"]

    pay_route(world, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": day} for day in unpaid]})
    statement = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")

    assert (
        statement["inputs"]["working_days"],
        statement["inputs"]["worked_days"],
        statement["summary"]["base_amount"],
    ) == (31, 25, 4032258.0)
