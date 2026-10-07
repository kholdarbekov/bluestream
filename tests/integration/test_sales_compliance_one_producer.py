"""One visit rule, one compliance producer, every surface that prints it (spec §4.1, C4).

`visit_rules.not_verified_reason` decides whether a visit counts. `AgentDayPlanService.compliance`
is the only place that turns visits and the 01:20 frozen due set into a numerator and a
denominator. This file seeds ONE week that exercises every reason and every excluded day, then
reads it back through every surface the spec names (T-VIS-1): the agent's own card
(`/staff/sales/me/stats`), both admin metrics routes, the plan-vs-fact table (whose `counted` must
sum to the numerator), and `rows_for_period` (the weekly email and the Analytics CSV); the
short-visit exception feed reads the same week (T-VIS-3). The pay-gate half of T-VIS-1 is Task 10's.

The week is `sales_pay_builders.field_week` (its Task 2 section, shared with Task 10's pay gate):
October 2026, the clock frozen at Thursday 10-08 18:00 Tashkent, so `period=month` is
10-01..10-08. Every number is written down here, never recomputed from the rows:

| Day | Day status | Frozen due set | What happened | Counted |
|---|---|---|---|---|
| Thu 10-01 | worked | one, two, three | one exactly the threshold; two 1 s under; three skipped check-in | 1 |
| Fri 10-02 | worked | one, four, pinless, six | one out of range; four verified twice; pinless unmeasured; six no check-in | 1 |
| Sat 10-03 | worked | one, two | one abandoned; two never visited; three (NOT due) verified and planned | 0 |
| Sun 10-04 | worked | one | one verified | 1 |
| Mon 10-05 | unpaid | one, two | one verified | excluded |
| Tue 10-06 | worked, NO snapshot row | none | one verified | excluded |
| Wed 10-07 | worked, legacy row (due 2) | not frozen | one, two, three verified + planned; four verified, unplanned | 2 |
| Thu 10-08 | holiday | one | one verified | excluded |

Measured days are 10-01, 10-02, 10-03, 10-04 and 10-07: due = 3 + 4 + 2 + 1 + 2 = 12, counted =
1 + 1 + 0 + 1 + 2 = 5, and 5 / 12 = 41.66...% publishes 41.7 (`AgentMetricsService.pct`, one
decimal). Every day is a working day (C2 v5), so the Sunday is measured like any other (I-39).

The unpaid day and the holiday are model rows: their writers (`set_unpaid_days`, `set_holidays`)
arrive with the pay routes, and T-VIS-2 (Task 10) drives the same kinds of day over HTTP.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_visits import SalesAgentDayPlan, Visit
from business_app.services.sales.agent_metrics_service import AgentMetricsService
from business_app.services.sales.day_plan_service import AgentDayPlanService, DayCompliance
from business_app.services.sales.visit_rules import not_verified_reason, presence_seconds, short_visit_threshold
from shared.constants import DISPLAY_TIMEZONE
from tests.integration.sales_pay_builders import (
    FIELD_WEEK_COUNTED,
    FIELD_WEEK_DUE,
    FIELD_WEEK_PCT,
    field_week,  # noqa: F401 -- a fixture
    freeze_local_now,
    make_day_plan,
    make_outlet,
    make_visit,
)
from tests.unit.test_outlet_dedupe import PIN
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
START_DATE, END_DATE = date(2026, 10, 1), date(2026, 10, 8)
WINDOW = {"start_date": "2026-10-01", "end_date": "2026-10-08"}
# The shops T-VIS-4, T-VIS-7 and T-VIS-9 build for themselves predate October.
LONG_AGO = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)

STATS = "/api/v1/staff/sales/me/stats"
AGENTS_METRICS = "/api/v1/admin/sales/agents/metrics"
PLAN_VS_FACT = "/api/v1/admin/sales/plan-vs-fact"
EXCEPTIONS = "/api/v1/admin/sales/exceptions"
STAFF_OUTLETS = "/api/v1/staff/sales/outlets"
STAFF_VISITS = "/api/v1/staff/sales/visits"
ADMIN_OUTLETS = "/api/v1/admin/sales/outlets"

# (day, due, counted, day_status, plan_source) for every plan-vs-fact row of the week, read off
# the table in the module docstring.
EXPECTED_DAYS = [
    ("2026-10-01", 3, 1, "worked", "snapshot"),
    ("2026-10-02", 4, 1, "worked", "snapshot"),
    ("2026-10-03", 2, 0, "worked", "snapshot"),
    ("2026-10-04", 1, 1, "worked", "snapshot"),
    ("2026-10-05", 2, 0, "unpaid", "snapshot"),
    ("2026-10-06", None, 0, "worked", "none"),
    ("2026-10-07", 2, 2, "worked", "snapshot"),
    ("2026-10-08", 1, 0, "holiday", "snapshot"),
]


def test_compliance_counts_verified_visits_to_the_frozen_set_on_worked_days_only(db, field_week):
    result = AgentDayPlanService.compliance(field_week.agent.id, START_DATE, END_DATE)

    assert (result.due, result.counted, result.pct) == (FIELD_WEEK_DUE, FIELD_WEEK_COUNTED, FIELD_WEEK_PCT)
    assert result.min_seconds == field_week.threshold
    assert [
        (day.day.isoformat(), day.due, day.counted, day.day_status, day.plan_source) for day in result.days
    ] == EXPECTED_DAYS
    # Why each due outlet was not counted: one reason per outlet per day, the FIRST failure of
    # that outlet's latest visit, or `not_visited`. Unmeasured days and the legacy row say nothing.
    assert {day.day.isoformat(): day.not_counted for day in result.days if day.not_counted} == {
        "2026-10-01": {"short": 1, "skipped": 1},
        "2026-10-02": {"out_of_range": 1, "no_location": 1, "no_checkin": 1},
        "2026-10-03": {"not_completed": 1, "not_visited": 1},
    }
    assert [day.day.isoformat() for day in result.days if day.legacy] == ["2026-10-07"]


def test_a_day_without_a_snapshot_leaves_both_halves_and_a_legacy_row_is_capped(db, field_week):
    """T-VIS-6. 10-06 has a verified visit and no row: it adds nothing to the numerator (4, not 5)
    and nothing to the denominator. 10-07's row predates the frozen set: three verified PLANNED
    outlets against a due count of 2 count min(3, 2) = 2, and the unplanned visit counts for
    nothing."""
    days = {day.day: day for day in AgentDayPlanService.compliance(field_week.agent.id, START_DATE, END_DATE).days}

    assert days[date(2026, 10, 6)] == DayCompliance(
        day=date(2026, 10, 6), day_status="worked", plan_source="none", due=None, counted=0, not_counted={},
        legacy=False,
    )
    assert days[date(2026, 10, 7)] == DayCompliance(
        day=date(2026, 10, 7), day_status="worked", plan_source="snapshot", due=2, counted=2, not_counted={},
        legacy=True,
    )
    alone = AgentDayPlanService.compliance(field_week.agent.id, date(2026, 10, 6), date(2026, 10, 6))
    assert (alone.due, alone.counted, alone.pct) == (0, 0, None)


def test_every_surface_publishes_the_one_compliance_figure(
    client, db, field_week, admin_claim_headers, sales_agent_auth_headers
):
    """T-VIS-1, metrics half: the same 12 due, 5 counted, 41.7% everywhere it is printed."""
    agent = field_week.agent

    mine = client.get(STATS, headers=sales_agent_auth_headers, query_string={"period": "month"})
    assert mine.status_code == 200, mine.get_data(as_text=True)
    card = mine.get_json()["data"]
    assert (card["start_date"], card["end_date"]) == ("2026-10-01", "2026-10-08")
    assert (card["metrics"]["planned_visits"], card["metrics"]["plan_vs_fact_pct"]) == (FIELD_WEEK_DUE, FIELD_WEEK_PCT)

    listed = client.get(AGENTS_METRICS, headers=admin_claim_headers, query_string=WINDOW)
    assert listed.status_code == 200, listed.get_data(as_text=True)
    (row,) = [row for row in listed.get_json()["data"]["agents"] if row["agent_user_id"] == agent.id]
    assert (row["planned_visits"], row["plan_vs_fact_pct"]) == (FIELD_WEEK_DUE, FIELD_WEEK_PCT)

    one = client.get(f"/api/v1/admin/sales/agents/{agent.id}/metrics", headers=admin_claim_headers, query_string=WINDOW)
    assert one.status_code == 200, one.get_data(as_text=True)
    metrics = one.get_json()["data"]["metrics"]
    assert (metrics["planned_visits"], metrics["plan_vs_fact_pct"]) == (FIELD_WEEK_DUE, FIELD_WEEK_PCT)

    table = client.get(PLAN_VS_FACT, headers=admin_claim_headers, query_string={**WINDOW, "agent_id": agent.id})
    assert table.status_code == 200, table.get_data(as_text=True)
    rows = table.get_json()["data"]["rows"]
    assert [(r["day"], r["due"], r["counted"], r["day_status"], r["plan_source"]) for r in rows] == EXPECTED_DAYS
    # The numerator IS the table's column, and the denominator is the snapshotted worked days' `due`.
    assert sum(r["counted"] for r in rows) == FIELD_WEEK_COUNTED
    assert (
        sum(r["due"] for r in rows if r["day_status"] == "worked" and r["plan_source"] == "snapshot")
        == FIELD_WEEK_DUE
    )

    # The weekly email and the Analytics CSV read this table.
    (report_row,) = AgentMetricsService.rows_for_period(START_DATE, END_DATE, agent_user_ids=[agent.id])
    assert (report_row["planned_visits"], report_row["plan_vs_fact_pct"]) == (FIELD_WEEK_DUE, FIELD_WEEK_PCT)


def test_the_short_visit_feed_names_one_second_under_and_not_the_threshold_itself(
    client, db, field_week, admin_claim_headers
):
    """T-VIS-3: the feed and the verified-visit rule measure one span against one threshold."""
    response = client.get(EXCEPTIONS, headers=admin_claim_headers, query_string={**WINDOW, "type": "short_visit"})
    assert response.status_code == 200, response.get_data(as_text=True)

    assert [(row["visit_id"], row["detail"]) for row in response.get_json()["data"]["exceptions"]] == [
        (
            field_week.one_second_short.id,
            {"seconds": field_week.threshold - 1, "threshold_seconds": field_week.threshold},
        )
    ]
    assert not_verified_reason(field_week.one_second_short, min_seconds=field_week.threshold) == "short"
    assert not_verified_reason(field_week.at_threshold, min_seconds=field_week.threshold) is None


def test_a_ratio_just_under_eighty_publishes_eighty(client, db, monkeypatch, sales_agent_user, admin_claim_headers):
    """T-VIS-4, publish half. 323 of 404 due outlets reached: 32300 / 404 = 79.9504...%, which one
    decimal publishes as 80.0 (323/404 is the smallest pair that rounds to 80.0 from below; the
    spec's 1999/2500 = 79.96% rounds the same way). The gate compares THIS published figure
    (Task 7 asserts the x1.0), so the % on screen and the multiplier cannot disagree at an edge."""
    freeze_local_now(monkeypatch, datetime(2026, 10, 8, 18, 0, tzinfo=TASHKENT))
    agent = sales_agent_user
    shops = [
        make_outlet(db, onboarded_by=agent, created_at=LONG_AGO, assigned_agent=agent, pin=PIN) for _ in range(404)
    ]
    make_day_plan(db, agent, date(2026, 10, 1), [shop.id for shop in shops])
    # A verified visit each: 10:00 local on 10-01 (stored in UTC), checked in two minutes later
    # in range, ten minutes long.
    started_at = datetime(2026, 10, 1, 10, 0, tzinfo=TASHKENT).astimezone(timezone.utc)
    for shop in shops[:323]:
        make_visit(
            db,
            outlet_id=shop.id,
            agent_user_id=agent.id,
            started_at=started_at,
            checkin_at=started_at + timedelta(minutes=2),
            ended_at=started_at + timedelta(minutes=12),
            in_radius=True,
            distance_m=14.0,
        )

    result = AgentDayPlanService.compliance(agent.id, date(2026, 10, 1), date(2026, 10, 1))
    assert (result.due, result.counted, result.pct) == (404, 323, 80.0)

    published = client.get(
        f"/api/v1/admin/sales/agents/{agent.id}/metrics",
        headers=admin_claim_headers,
        query_string={"start_date": "2026-10-01", "end_date": "2026-10-01"},
    )
    assert published.status_code == 200, published.get_data(as_text=True)
    assert published.get_json()["data"]["metrics"]["plan_vs_fact_pct"] == 80.0


# The columns the real loop decides and the verified-visit rule reads.
VISIT_SHAPE = (
    "outlet_id",
    "agent_user_id",
    "status",
    "planned",
    "current_step",
    "checkin_skipped",
    "in_radius",
    "outcome",
    "no_order_reason",
)


def _drive_the_real_visit(client, db, headers, outlet, checkin):
    """Start, check in, count the shelf (nothing to count) and close with no order, over HTTP."""
    started = client.post(f"{STAFF_OUTLETS}/{outlet.id}/visits", headers=headers)
    assert started.status_code == 201, started.get_data(as_text=True)
    visit_id = started.get_json()["data"]["visit"]["id"]
    for step, body in (
        ("checkin", checkin),
        ("stock-check", {"items": []}),
        ("close", {"outcome": "no_order", "no_order_reason": "sufficient_stock"}),
    ):
        response = client.post(f"{STAFF_VISITS}/{visit_id}/{step}", json=body, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    return db.session.get(Visit, visit_id)


@pytest.mark.parametrize(
    "pinned,checkin,check_in_facts",
    [
        (True, {"latitude": PIN[0], "longitude": PIN[1], "horizontal_accuracy": 8.0}, {"in_radius": True}),
        (True, {"skipped": True}, {"checkin_skipped": True}),
        (False, {"latitude": PIN[0], "longitude": PIN[1]}, {}),
    ],
    ids=["in-range", "skipped", "unmeasured-at-a-pinless-outlet"],
)
def test_the_visit_builder_writes_what_the_real_visit_loop_writes(
    client, db, sales_agent_user, sales_agent_auth_headers, pinned, checkin, check_in_facts
):
    """T-VIS-7. The builder is handed only the inputs (the outlet, the agent, the three instants
    and the check-in facts the server derived from the pin); everything else comes from its own
    defaults and must equal what the loop wrote, down to the rule's verdict on the pair."""
    outlet = make_outlet(
        db,
        onboarded_by=sales_agent_user,
        created_at=LONG_AGO,
        assigned_agent=sales_agent_user,
        pin=PIN if pinned else None,
    )
    real = _drive_the_real_visit(client, db, sales_agent_auth_headers, outlet, checkin)

    built = make_visit(
        db,
        outlet_id=outlet.id,
        agent_user_id=sales_agent_user.id,
        started_at=real.started_at,
        checkin_at=real.checkin_at,
        ended_at=real.ended_at,
        **check_in_facts,
    )

    assert {name: getattr(built, name) for name in VISIT_SHAPE} == {name: getattr(real, name) for name in VISIT_SHAPE}
    threshold = short_visit_threshold()
    assert presence_seconds(built) == presence_seconds(real)
    assert not_verified_reason(built, min_seconds=threshold) == not_verified_reason(real, min_seconds=threshold)


def test_the_plan_is_the_frozen_set_and_follows_ownership(
    client, db, monkeypatch, sales_agent_user, sales_agent_auth_headers, admin_claim_headers
):
    """T-VIS-9 (review gaming F1, F2; Q10).

    * A shop handed to another agent BEFORE the 01:20 job leaves this agent's frozen set and due
      list and joins the new owner's, yet its card still opens for the onboarder.
    * A shop that turns due AFTER the job (an admin raises its class C -> A, so the 7-day
      `SALES_CADENCE_DAYS_A` puts it three days overdue) is on today's due list, and a verified
      visit to it still does not count: it was not in the day's plan.
    * Two verified visits to one due shop count once.
    """
    freeze_local_now(monkeypatch, datetime(2026, 10, 7, 18, 0, tzinfo=TASHKENT))
    agent = sales_agent_user
    other = make_sales_agent_user(db, phone="+998901234593", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["chilanzar"]))
    db.session.commit()

    reclassed = make_outlet(
        db, onboarded_by=agent, created_at=LONG_AGO, assigned_agent=agent, outlet_class="C", pin=PIN
    )
    twice = make_outlet(db, onboarded_by=agent, created_at=LONG_AGO, assigned_agent=agent, pin=PIN)
    handed_over = make_outlet(db, onboarded_by=agent, created_at=LONG_AGO, assigned_agent=agent, pin=PIN)
    # 10:00 local on each day, stored in UTC as the visit loop stores it.
    ten_am = {day: datetime(2026, 10, day, 10, 0, tzinfo=TASHKENT).astimezone(timezone.utc) for day in (5, 6, 7)}
    reclassed.last_visit_at = ten_am[7] - timedelta(days=10)
    reclassed.next_visit_due_at = ten_am[7] + timedelta(days=20)   # not due on the 7th
    twice.next_visit_due_at = ten_am[6]
    handed_over.next_visit_due_at = ten_am[5]
    db.session.commit()

    moved = client.post(
        f"{ADMIN_OUTLETS}/{handed_over.id}/assign", json={"agent_user_id": other.id}, headers=admin_claim_headers
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)

    AgentDayPlanService.snapshot_all(now=datetime(2026, 10, 7, 1, 20, tzinfo=TASHKENT))

    plans = {row.agent_user_id: row for row in SalesAgentDayPlan.query.filter_by(plan_date=date(2026, 10, 7)).all()}
    assert (plans[agent.id].due_outlet_ids, plans[agent.id].due_count) == ([twice.id], 1)
    assert (plans[other.id].due_outlet_ids, plans[other.id].due_count) == ([handed_over.id], 1)

    reclass = client.put(f"{ADMIN_OUTLETS}/{reclassed.id}", json={"class": "A"}, headers=admin_claim_headers)
    assert reclass.status_code == 200, reclass.get_data(as_text=True)

    due_list = client.get(STAFF_OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": "due"})
    assert due_list.status_code == 200, due_list.get_data(as_text=True)
    assert [row["id"] for row in due_list.get_json()["data"]["items"]] == [reclassed.id, twice.id]
    card = client.get(f"{STAFF_OUTLETS}/{handed_over.id}", headers=sales_agent_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)

    # Three verified visits on the 7th (checked in two minutes after the start, in range, ten
    # minutes long): the planned one to the reclassed shop, and two to the one due shop.
    for shop, hour, planned in ((reclassed, 10, True), (twice, 10, False), (twice, 16, False)):
        started_at = datetime(2026, 10, 7, hour, 0, tzinfo=TASHKENT).astimezone(timezone.utc)
        make_visit(
            db,
            outlet_id=shop.id,
            agent_user_id=agent.id,
            started_at=started_at,
            checkin_at=started_at + timedelta(minutes=2),
            ended_at=started_at + timedelta(minutes=12),
            in_radius=True,
            distance_m=14.0,
            planned=planned,
        )

    result = AgentDayPlanService.compliance(agent.id, date(2026, 10, 7), date(2026, 10, 7))
    assert (result.due, result.counted, result.pct) == (1, 1, 100.0)
    assert result.days[0].not_counted == {}
    rows = client.get(
        PLAN_VS_FACT,
        headers=admin_claim_headers,
        query_string={"start_date": "2026-10-07", "end_date": "2026-10-07", "agent_id": agent.id},
    ).get_json()["data"]["rows"]
    assert [(row["due"], row["counted"], row["completed"]) for row in rows] == [(1, 1, 3)]


def test_a_visit_is_planned_only_when_the_outlet_is_on_the_visiting_agents_due_list(
    client, app, db, monkeypatch, sales_agent_user, sales_agent_auth_headers, admin_claim_headers
):
    """T2-R1 (Q10): `Visit.planned` is stamped by the ONE due rule, `OutletService.due_scope`.

    `unplanned_visits` (the bot's My stats, the Analytics tab and CSV, the weekly email) and
    plan-vs-fact's `unplanned` column read that stamp, so it must say what the due list says:

    * the owner starts a visit at their overdue shop: planned;
    * the agent who onboarded it before a manager handed it over walks in too: it is on neither
      their due list nor their frozen set, so the visit is a drop-in, although they may still
      open the shop;
    * the owner starts a visit at a shop not due until Saturday: not planned.

    Each verdict is held to that agent's own due list (`GET /sales/outlets?scope=due`) at the same
    frozen instant, so the stamp and the list cannot part company.
    """
    freeze_local_now(monkeypatch, datetime(2026, 10, 7, 18, 0, tzinfo=TASHKENT))
    onboarder = sales_agent_user
    owner = make_sales_agent_user(db, phone="+998901234594", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=owner.id, districts=["chilanzar"]))
    db.session.commit()
    with app.app_context():
        owner_headers = {
            "Authorization": f"Bearer {create_access_token(identity=str(owner.id))}",
            "Content-Type": "application/json",
        }

    handed_over = make_outlet(db, onboarded_by=onboarder, created_at=LONG_AGO, assigned_agent=onboarder, pin=PIN)
    not_yet_due = make_outlet(db, onboarded_by=owner, created_at=LONG_AGO, assigned_agent=owner, pin=PIN)
    # 10:00 local, stored in UTC: a day overdue, and three days ahead.
    handed_over.next_visit_due_at = datetime(2026, 10, 6, 10, 0, tzinfo=TASHKENT).astimezone(timezone.utc)
    not_yet_due.next_visit_due_at = datetime(2026, 10, 10, 10, 0, tzinfo=TASHKENT).astimezone(timezone.utc)
    db.session.commit()
    moved = client.post(
        f"{ADMIN_OUTLETS}/{handed_over.id}/assign", json={"agent_user_id": owner.id}, headers=admin_claim_headers
    )
    assert moved.status_code == 200, moved.get_data(as_text=True)

    def due_list(headers):
        response = client.get(STAFF_OUTLETS, headers=headers, query_string={"scope": "due"})
        assert response.status_code == 200, response.get_data(as_text=True)
        return [row["id"] for row in response.get_json()["data"]["items"]]

    def start(headers, outlet):
        response = client.post(f"{STAFF_OUTLETS}/{outlet.id}/visits", headers=headers)
        assert response.status_code == 201, response.get_data(as_text=True)
        return response.get_json()["data"]["visit"]

    assert (due_list(owner_headers), due_list(sales_agent_auth_headers)) == ([handed_over.id], [])

    early = start(owner_headers, not_yet_due)
    assert early["planned"] is False
    # One open visit per agent: the owner gives this one up before walking to the due shop.
    abandoned = client.post(f"{STAFF_VISITS}/{early['id']}/abandon", headers=owner_headers)
    assert abandoned.status_code == 200, abandoned.get_data(as_text=True)

    assert start(owner_headers, handed_over)["planned"] is True
    assert start(sales_agent_auth_headers, handed_over)["planned"] is False
