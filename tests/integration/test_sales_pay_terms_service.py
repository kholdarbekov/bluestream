"""Sales-agent pay terms, employment dates and unpaid days (spec §4.10, §3.3, §4.13).

These are service-level tests. A17 (terms), A18 (employment) and A10 (unpaid days) arrive in Task 7
with the A16 and StatementDetail read models, and their HTTP halves live in
tests/integration/test_sales_pay_terms_api.py and tests/integration/test_sales_pay_statement_api.py.
What is pinned here is each write's rules, the rows it leaves, and its audit. The audit carries
`self_decided: true` when an admin decided about their own pay (D-Q11). A manager deciding about
their own pay is refused, and nothing is written.

Every call passes `now=`. 2026-10 facts used below: 1 October is a Thursday, so the 4th and 11th
are Sundays, the 5th and 12th Mondays, the 6th and 13th Tuesdays, the 14th a Wednesday and the 15th
a Thursday. 1 November is a Sunday and 2 November a Monday. Every day is a working day (C2 v5), so
a Sunday is worked unless it is a holiday or unpaid.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesAgentPayTerms, SalesAgentUnpaidDay
from business_app.serializers.sales_pay_serializers import VersionPayload
from business_app.services.sales import work_calendar
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_rules import Rate, Tier, TierSchedule
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.utils.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import ensure_utc
from tests.integration.sales_pay_builders import pay_audit, tier
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture
from tests.integration.test_admin_sales_pay_plans_api import plan_version_payload
from tests.integration.test_sales_pay_period_service import (
    DEC,
    IN_DECEMBER,
    IN_OCTOBER,
    NOV,
    OCT,
    SEP,
    move_period,
    open_months,
)

pytestmark = pytest.mark.integration


def _version(effective_month="2026-10", **overrides):
    """The version as the A13/A15 routes hand it to the service: a `VersionPayload` dump."""
    body = plan_version_payload(effective_month, **overrides)
    return VersionPayload.model_validate(body).model_dump(exclude_unset=True)


@pytest.fixture
def standard_plan(db, admin_user):
    """Example A's plan (spec §1.2), version 1 from October, through the service A13 calls."""
    return SalesPayPlanService.create_plan("Standard", _version(), actor_id=admin_user.id, now=IN_OCTOBER)


def _employ(agent, actor, start=date(2026, 6, 1), end=None, now=IN_OCTOBER):
    return SalesPayTermsService.set_employment(agent.id, start=start, end=end, actor_id=actor.id, now=now)


def _add_terms(agent, actor, plan, month=OCT, base="5000000", note=None, now=IN_OCTOBER):
    return SalesPayTermsService.add_terms(
        agent.id,
        effective_month=month,
        base_salary=Decimal(base),
        plan_id=plan.id,
        note=note,
        actor_id=actor.id,
        now=now,
    )


def _set_unpaid(agent, actor, days, month=OCT, now=IN_OCTOBER):
    return SalesPayTermsService.set_unpaid_days(
        agent.id, month, [{"date": day, "note": note} for day, note in days], actor_id=actor.id, now=now
    )


def _give_profile(db, user):
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()


def _refusal(excinfo):
    return excinfo.value.error_code, excinfo.value.details


# --------------------------------------------------------------------------- #
# Terms
# --------------------------------------------------------------------------- #


def test_the_terms_in_force_are_the_latest_month_then_the_latest_row(
    db, admin_user, sales_agent_user, standard_plan, audit_events
):
    """C2: a raise is a new row effective November, so October keeps its base. A second October row
    is a correction, and it wins for October."""
    _employ(sales_agent_user, admin_user)

    october = _add_terms(sales_agent_user, admin_user, standard_plan, note="  hired  ")
    november = _add_terms(sales_agent_user, admin_user, standard_plan, month=NOV, base="5500000")
    october_fix = _add_terms(sales_agent_user, admin_user, standard_plan, base="5100000")

    assert SalesPayTermsService.terms_for(sales_agent_user.id, SEP) is None
    assert SalesPayTermsService.terms_for(sales_agent_user.id, OCT).id == october_fix.id
    assert [SalesPayTermsService.terms_for(sales_agent_user.id, month).id for month in (NOV, DEC)] == [
        november.id,
        november.id,
    ]
    assert (october.note, october.base_salary, october.plan_id, october.created_by_user_id) == (
        "hired",
        Decimal("5000000"),
        standard_plan.id,
        admin_user.id,
    )
    assert SalesPayTermsService.agent_ids_with_terms() == [sales_agent_user.id]
    assert pay_audit(audit_events, "terms_added")[0] == (
        "sales_agent",
        str(sales_agent_user.id),
        None,
        {"terms_id": october.id, "effective_month": "2026-10", "base_salary": 5000000, "plan_id": standard_plan.id},
    )


def test_version_for_resolves_the_plan_of_the_terms_in_force(db, admin_user, sales_agent_user, standard_plan):
    """The agent is paid on their terms' plan, resolved for the month (spec §4.10): v1 (3%) for
    October, v2 (4%) from November. Without terms there is no plan."""
    _employ(sales_agent_user, admin_user)
    _add_terms(sales_agent_user, admin_user, standard_plan)
    SalesPayPlanService.create_version(
        standard_plan.id,
        _version("2026-11", default_tiers=[tier(1, "percent", 4)]),
        actor_id=admin_user.id,
        now=IN_OCTOBER,
    )

    october = SalesPayPlanService.version_for(sales_agent_user.id, OCT)
    november = SalesPayPlanService.version_for(sales_agent_user.id, NOV)

    assert (october.plan_name, october.version_no, october.config.default_tiers) == (
        "Standard",
        1,
        TierSchedule(tiers=(Tier(from_unit=1, rate=Rate(mode="percent", value=Decimal("3"))),)),
    )
    assert (november.version_no, november.config.default_tiers) == (
        2,
        TierSchedule(tiers=(Tier(from_unit=1, rate=Rate(mode="percent", value=Decimal("4"))),)),
    )
    assert SalesPayPlanService.version_for(sales_agent_user.id, SEP) is None
    assert SalesPayPlanService.version_for(admin_user.id, OCT) is None


def test_add_terms_needs_an_employment_start_date(db, admin_user, sales_agent_user, standard_plan):
    with pytest.raises(ValidationError) as refused:
        _add_terms(sales_agent_user, admin_user, standard_plan)

    assert _refusal(refused) == ("SALES_PAY_TERMS_INVALID", {"field": "employment_start_date", "reason": "required"})
    assert SalesAgentPayTerms.query.count() == 0


@pytest.mark.parametrize("base", ["1500000.5", "-1"], ids=["fractional", "negative"])
def test_add_terms_refuses_a_base_that_is_not_whole_non_negative_uzs(
    db, admin_user, sales_agent_user, standard_plan, base
):
    _employ(sales_agent_user, admin_user)

    with pytest.raises(ValidationError) as refused:
        _add_terms(sales_agent_user, admin_user, standard_plan, base=base)

    assert _refusal(refused) == ("SALES_PAY_AMOUNT_INVALID", {"field": "base_salary"})
    assert SalesAgentPayTerms.query.count() == 0


def test_add_terms_refuses_an_unknown_plan_or_a_user_who_is_not_an_agent(
    db, admin_user, sales_agent_user, sample_user, standard_plan
):
    _employ(sales_agent_user, admin_user)

    with pytest.raises(NotFoundError) as no_plan:
        SalesPayTermsService.add_terms(
            sales_agent_user.id,
            effective_month=OCT,
            base_salary=Decimal("5000000"),
            plan_id=999999,
            note=None,
            actor_id=admin_user.id,
            now=IN_OCTOBER,
        )
    with pytest.raises(NotFoundError) as no_agent:
        _add_terms(sample_user, admin_user, standard_plan)

    assert _refusal(no_plan) == ("SALES_PAY_NOT_FOUND", {"resource": "plan", "id": 999999})
    assert _refusal(no_agent) == ("SALES_PAY_NOT_FOUND", {"resource": "agent", "id": sample_user.id})
    assert SalesAgentPayTerms.query.count() == 0


def test_add_terms_refuses_a_month_before_the_first_editable_one(db, admin_user, sales_agent_user, standard_plan):
    """Terms for a month that can no longer change are refused (spec §4.10). Before start, that is
    any month before the current one."""
    _employ(sales_agent_user, admin_user)

    with pytest.raises(ConflictError) as refused:
        _add_terms(sales_agent_user, admin_user, standard_plan, month=SEP)

    assert _refusal(refused) == (
        "SALES_PAY_MONTH_LOCKED",
        {"month": "2026-09", "status": None, "editable_from_month": "2026-10"},
    )
    assert SalesAgentPayTerms.query.count() == 0


# --------------------------------------------------------------------------- #
# Employment dates
# --------------------------------------------------------------------------- #


def test_employment_dates_record_who_set_them_and_when(db, admin_user, sales_agent_user, audit_events):
    """The one pay decision with its own actor columns (spec §3.3); the self-decision tag reads them.
    20 October 10:00 local is 05:00 UTC."""
    profile = _employ(sales_agent_user, admin_user, start=date(2026, 6, 1), end=date(2026, 12, 31))

    assert (
        profile.employment_start_date,
        profile.employment_end_date,
        profile.employment_set_by_user_id,
        ensure_utc(profile.employment_set_at),
    ) == (date(2026, 6, 1), date(2026, 12, 31), admin_user.id, datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc))
    assert pay_audit(audit_events, "employment_set") == [
        (
            "sales_agent",
            str(sales_agent_user.id),
            {"start": None, "end": None},
            {"start": "2026-06-01", "end": "2026-12-31", "months": []},
        )
    ]


def test_an_employment_end_before_its_start_is_refused(db, admin_user, sales_agent_user):
    with pytest.raises(ValidationError) as refused:
        _employ(sales_agent_user, admin_user, start=date(2026, 6, 1), end=date(2026, 5, 31))

    assert _refusal(refused) == (
        "SALES_PAY_DATE_INVALID",
        {"field": "end", "date": "2026-05-31", "reason": "before_start"},
    )
    assert SalesAgentProfile.query.filter_by(user_id=sales_agent_user.id).one().employment_start_date is None


@pytest.mark.parametrize(
    "old,new,expected",
    [
        pytest.param((None, None), (date(2026, 9, 10), None), [SEP], id="start-set-inside-september"),
        pytest.param(
            (date(2026, 6, 1), None), (date(2026, 6, 1), date(2026, 10, 31)), [OCT, NOV, DEC], id="end-set"
        ),
        pytest.param(
            (date(2026, 6, 1), date(2026, 11, 1)),
            (date(2026, 6, 1), date(2026, 10, 31)),
            [OCT, NOV],
            id="end-moved-back",
        ),
        pytest.param(
            (date(2026, 6, 1), None), (date(2026, 6, 1), date(2026, 11, 1)), [NOV, DEC], id="end-on-1-november"
        ),
        pytest.param((date(2026, 6, 1), None), (date(2026, 6, 1), None), [], id="unchanged"),
    ],
)
def test_affected_months_are_the_months_whose_employed_days_change(db, admin_user, old, new, expected):
    """From the September epoch to December, the union of two sets (spec §4.10):
    - the months where the old and new spans cover different days. `end-moved-back` changes only
      1 November, and that counts: the span is compared as dates, never as working days;
    - the months whose `pay_calculator.employed_next_month` answer flips (I-26). An end on 31
      October leaves no November to carry into, so October joins `end-set` and `end-moved-back`
      although none of its days changed. An end on 1 November (`end-on-1-november`) keeps October
      employed next month, so October is not affected.
    """
    open_months(db, admin_user)

    assert SalesPayTermsService.affected_months(*old, *new, now=IN_DECEMBER) == expected


def test_before_pay_starts_no_month_is_affected(db):
    assert SalesPayTermsService.affected_months(None, None, date(2026, 9, 10), None, now=IN_DECEMBER) == []


def test_an_employment_change_touching_an_approved_month_is_refused_whole(
    db, admin_user, sales_agent_user, audit_events
):
    """Moving the start into September would change an approved month, so it is refused and nothing
    is written (`details.months` names it). Moving the end into October changes only a closed month
    and two open ones, which is allowed."""
    months = open_months(db, admin_user)
    _employ(sales_agent_user, admin_user, now=IN_DECEMBER)
    move_period(db, months[SEP], "approved", admin_user)
    move_period(db, months[OCT], "closed", admin_user)

    with pytest.raises(ConflictError) as refused:
        _employ(sales_agent_user, admin_user, start=date(2026, 9, 10), now=IN_DECEMBER)

    assert _refusal(refused) == ("SALES_PAY_MONTH_LOCKED", {"months": ["2026-09"]})
    profile = SalesAgentProfile.query.filter_by(user_id=sales_agent_user.id).one()
    assert (profile.employment_start_date, profile.employment_end_date) == (date(2026, 6, 1), None)

    _employ(sales_agent_user, admin_user, end=date(2026, 10, 20), now=IN_DECEMBER)

    db.session.expire_all()
    profile = SalesAgentProfile.query.filter_by(user_id=sales_agent_user.id).one()
    assert (profile.employment_start_date, profile.employment_end_date) == (date(2026, 6, 1), date(2026, 10, 20))
    assert [new["months"] for *_head, new in pay_audit(audit_events, "employment_set")] == [
        [],
        ["2026-10", "2026-11", "2026-12"],
    ]


# --------------------------------------------------------------------------- #
# Unpaid days
# --------------------------------------------------------------------------- #


@pytest.fixture
def october(db, admin_user, sales_agent_user):
    """Pay started in October; the agent is employed from Tuesday 6 October; the 14th is a holiday."""
    period = SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)
    _employ(sales_agent_user, admin_user, start=date(2026, 10, 6))
    SalesPayPeriodService.set_holidays(
        OCT, [{"date": date(2026, 10, 14), "note": "Teachers' day"}], actor_id=admin_user.id, now=IN_OCTOBER
    )
    return period


def test_unpaid_days_replace_the_agents_set_for_the_month(db, admin_user, sales_agent_user, october, audit_events):
    _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 12), "sick"), (date(2026, 10, 13), None)])

    rows = _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 15), " family ")])

    assert [(row.unpaid_date, row.note, row.created_by_user_id) for row in rows] == [
        (date(2026, 10, 15), "family", admin_user.id)
    ]
    stored = SalesAgentUnpaidDay.query.filter_by(agent_user_id=sales_agent_user.id).all()
    assert [(row.unpaid_date, row.note) for row in stored] == [(date(2026, 10, 15), "family")]
    assert pay_audit(audit_events, "unpaid_days_set") == [
        (
            "sales_agent",
            str(sales_agent_user.id),
            {"month": "2026-10", "dates": []},
            {"month": "2026-10", "dates": ["2026-10-12", "2026-10-13"]},
        ),
        (
            "sales_agent",
            str(sales_agent_user.id),
            {"month": "2026-10", "dates": ["2026-10-12", "2026-10-13"]},
            {"month": "2026-10", "dates": ["2026-10-15"]},
        ),
    ]


@pytest.mark.parametrize(
    "days,refused_date,reason",
    [
        pytest.param([date(2026, 10, 14)], "2026-10-14", "not_working_day", id="the-months-holiday"),
        pytest.param([date(2026, 11, 2)], "2026-11-02", "outside_month", id="outside-the-month"),
        pytest.param([date(2026, 10, 5)], "2026-10-05", "not_employed", id="before-the-employment-start"),
        pytest.param([date(2026, 10, 12), date(2026, 10, 12)], "2026-10-12", "duplicate", id="listed-twice"),
    ],
)
def test_unpaid_days_refuse_a_day_that_cannot_be_unpaid(
    db, admin_user, sales_agent_user, october, days, refused_date, reason
):
    """T-BASE-1's service half: a holiday is not a day the agent would otherwise work, so it cannot
    be unpaid (I-38); a day outside the month or the employment, or listed twice, is refused too."""
    _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 16), None)])

    with pytest.raises(ValidationError) as refused:
        _set_unpaid(sales_agent_user, admin_user, [(day, None) for day in days])

    assert _refusal(refused) == ("SALES_PAY_DATE_INVALID", {"field": "days", "date": refused_date, "reason": reason})
    assert [row.unpaid_date for row in SalesAgentUnpaidDay.query.all()] == [date(2026, 10, 16)]


def test_a_sunday_unpaid_day_is_stored(db, admin_user, sales_agent_user, october):
    """C2 v5, I-38: an agent's own day off may fall on any day. Sunday 11 October would otherwise
    be worked, so it is stored like a weekday. The refusal is still read from `day_statuses`, so
    there was no Sunday check to delete."""
    rows = _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 11), "Rest day")])

    assert [(row.unpaid_date, row.note) for row in rows] == [(date(2026, 10, 11), "Rest day")]
    assert [row.unpaid_date for row in SalesAgentUnpaidDay.query.all()] == [date(2026, 10, 11)]


def test_a_narrowed_week_refuses_a_sunday_unpaid_day(db, admin_user, sales_agent_user, october, monkeypatch):
    """With the week narrowed back to Monday to Saturday through the one reader, Sunday 11 October
    reads `non_working`, and `set_unpaid_days` names the refusal from that status:
    `not_working_day`, and the set written before stays."""
    _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 16), None)])
    monkeypatch.setattr(work_calendar, "is_working_weekday", lambda d: d.weekday() != 6)

    with pytest.raises(ValidationError) as refused:
        _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 11), None)])

    assert _refusal(refused) == (
        "SALES_PAY_DATE_INVALID",
        {"field": "days", "date": "2026-10-11", "reason": "not_working_day"},
    )
    assert [row.unpaid_date for row in SalesAgentUnpaidDay.query.all()] == [date(2026, 10, 16)]


def test_unpaid_days_need_a_month_that_is_open_or_closed(db, admin_user, sales_agent_user, october):
    with pytest.raises(NotFoundError) as no_period:
        _set_unpaid(sales_agent_user, admin_user, [(date(2026, 11, 2), None)], month=NOV)
    assert _refusal(no_period) == ("SALES_PAY_NOT_FOUND", {"resource": "period", "id": "2026-11"})

    move_period(db, october, "closed", admin_user)
    assert [row.unpaid_date for row in _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 12), None)])] == [
        date(2026, 10, 12)
    ]

    move_period(db, october, "approved", admin_user)
    with pytest.raises(ConflictError) as locked:
        _set_unpaid(sales_agent_user, admin_user, [])
    assert _refusal(locked) == ("SALES_PAY_MONTH_LOCKED", {"month": "2026-10", "status": "approved"})
    assert [row.unpaid_date for row in SalesAgentUnpaidDay.query.all()] == [date(2026, 10, 12)]


# --------------------------------------------------------------------------- #
# Self-decision (D-Q11, spec §4.13): A17, A18 and A10 at service level
# --------------------------------------------------------------------------- #


def test_an_admin_may_decide_about_their_own_pay_and_each_write_is_tagged(db, admin_user, standard_plan, audit_events):
    _give_profile(db, admin_user)
    SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)

    profile = _employ(admin_user, admin_user, start=date(2026, 10, 1))
    terms = _add_terms(admin_user, admin_user, standard_plan, base="4000000")
    rows = _set_unpaid(admin_user, admin_user, [(date(2026, 10, 12), None)])

    assert (profile.employment_set_by_user_id, terms.created_by_user_id, rows[0].created_by_user_id) == (
        admin_user.id,
        admin_user.id,
        admin_user.id,
    )
    tags = {
        verb: [new.get("self_decided") for *_head, new in pay_audit(audit_events, verb)]
        for verb in ("employment_set", "terms_added", "unpaid_days_set")
    }
    assert tags == {"employment_set": [True], "terms_added": [True], "unpaid_days_set": [True]}


def test_a_write_about_another_agent_is_not_tagged(db, admin_user, sales_agent_user, standard_plan, audit_events):
    SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)

    _employ(sales_agent_user, admin_user)
    _add_terms(sales_agent_user, admin_user, standard_plan)
    _set_unpaid(sales_agent_user, admin_user, [(date(2026, 10, 12), None)])

    written = [
        new
        for verb in ("employment_set", "terms_added", "unpaid_days_set")
        for *_head, new in pay_audit(audit_events, verb)
    ]
    assert len(written) == 3
    assert all("self_decided" not in new for new in written)


def test_a_manager_is_refused_anything_about_their_own_pay(db, admin_user, manager_user, standard_plan):
    """Only the manager-level routes reach these writes for a manager, but the services refuse on
    their own: SALES_PAY_SELF_DECISION, naming the subject, with nothing written."""
    _give_profile(db, manager_user)
    SalesPayPeriodService.start(OCT, is_shadow=False, actor_id=admin_user.id, now=IN_OCTOBER)
    writes = {
        "employment": lambda: _employ(manager_user, manager_user),
        "terms": lambda: _add_terms(manager_user, manager_user, standard_plan),
        "unpaid days": lambda: _set_unpaid(manager_user, manager_user, [(date(2026, 10, 12), None)]),
    }

    for name, write in writes.items():
        with pytest.raises(ForbiddenError) as refused:
            write()
        assert _refusal(refused) == ("SALES_PAY_SELF_DECISION", {"agent_user_id": manager_user.id}), name

    profile = SalesAgentProfile.query.filter_by(user_id=manager_user.id).one()
    assert (profile.employment_start_date, profile.employment_set_by_user_id) == (None, None)
    assert (SalesAgentPayTerms.query.count(), SalesAgentUnpaidDay.query.count()) == (0, 0)
