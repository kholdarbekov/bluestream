"""/api/v1/admin/sales/visits and /api/v1/admin/sales/plan-vs-fact, driven the way Visits.js drives them.

The subject here is the supervisor's READ surface, not the visit loop — `test_staff_sales_visits_api.py`
owns that. Which visits a window contains, which filters narrow it, what one row publishes, and what a
day with no plan snapshot answers. The visits are therefore built as rows: `VisitService.start` stamps
the wall clock, so a loop-driven fixture cannot put a visit on a chosen local day at all.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from business_app.models.order import Order
from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_visits import SalesAgentDayPlan, Visit
from business_app.services.sales.outlet_service import OutletService
from business_app.utils import local_windows
from business_app.utils.local_windows import days_inclusive, local_date, parse_date_range
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_outlet_dedupe import NEAR
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import OrderStatus
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

VISITS = "/api/v1/admin/sales/visits"
PLAN_VS_FACT = "/api/v1/admin/sales/plan-vs-fact"
# Asserted verbatim: the point of V10 is that the published string IS the order's number and
# not something the serializer could invent or drop.
ORDER_NUMBER = "SA_000501_26"

# Tashkent is UTC+5, so a stored instant's UTC date and its LOCAL date disagree for exactly one
# band: local 00:00-04:59. `STARTED_DAY_TWO_MIDNIGHT` is the instant that lives in it, and it is
# the only one here that can catch a UTC-midnight window or a `.date()` taken on the stored UTC
# instant (the `_driver_day_start_utc` lesson) -- the other four are ordinary working hours,
# where both calendars agree and a broken day boundary still passes. The assertion that makes it
# bite is `test_a_visit_just_after_local_midnight_belongs_to_the_local_day_it_started`.
DAY_ZERO = date(2026, 9, 13)
DAY_ONE = date(2026, 9, 14)
DAY_TWO = date(2026, 9, 15)
STARTED_DAY_TWO = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)            # 09:00 local, day two
STARTED_DAY_TWO_MIDNIGHT = datetime(2026, 9, 14, 20, 30, tzinfo=UTC)  # 01:30 local, day two; UTC says day ONE
STARTED_DAY_ONE_LATE = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)       # 14:00 local, day one
STARTED_DAY_ONE_EARLY = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)      # 11:00 local, day one
# The crossing pair (V09/R2): 23:50 LOCAL on day one, closed 00:20 LOCAL on day two. In UTC
# both instants sit on 2026-09-14, so only the LOCAL boundary 2026-09-14T19:00Z separates
# them -- which is exactly what makes swapping the window column from `started_at` to
# `ended_at` visible here and nowhere else in the file.
STARTED_DAY_ONE_NIGHT = datetime(2026, 9, 14, 18, 50, tzinfo=UTC)    # 23:50 local, day one
ENDED_DAY_TWO_EARLY = datetime(2026, 9, 14, 19, 20, tzinfo=UTC)      # 00:20 local, day TWO
OUTSIDE = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)                     # outside every window below
SNAPSHOT_AT = datetime(2026, 9, 14, 20, 20, tzinfo=UTC)              # 01:20 local, the job's own hour

PERIOD = {"start_date": DAY_ONE.isoformat(), "end_date": DAY_TWO.isoformat()}
PLAN_PERIOD = {"start_date": DAY_ZERO.isoformat(), "end_date": DAY_TWO.isoformat()}


@pytest.fixture
def frozen_local_now(monkeypatch):
    """Freeze the clock `local_windows` reads — the same fixture, for the same reason, as
    Task 3's (M13).

    `test_a_request_with_no_period_answers_the_default_local_week` reads the wall clock TWICE:
    once inside the route and once in its own `parse_date_range(None, None)` call. Unfrozen,
    the two reads can straddle local midnight and the test fails once a day, at 00:00 Tashkent,
    for no reason anybody can reproduce. The module binds `local_now` by NAME, so patch it in
    `local_windows`, never in `delivery_window`.

    The frozen value is a LOCAL-zone datetime, exactly like the one the real `local_now()`
    returns: `local_windows._tz()` borrows its `tzinfo`, so freezing it to a UTC-tzinfo instant
    would quietly make the whole module's "local day" mean UTC day for the duration of the test
    -- the very thing this file exists to pin (`tests/unit/test_local_windows.py:47-50` freezes
    the same way, for the same reason).
    """
    frozen = datetime(2026, 9, 17, 7, 0, tzinfo=UTC).astimezone(ZoneInfo(DISPLAY_TIMEZONE))  # local 12:00
    monkeypatch.setattr(local_windows, "local_now", lambda: frozen)
    return frozen


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


@pytest.fixture
def second_agent(db):
    """A second agent with a DIFFERENT name: the rows are sorted by agent name, and two
    `Sardor Agent`s would let a broken sort pass."""
    user = make_sales_agent_user(db, phone="+998901234575", staff_roles=["sales_agent"])
    user.first_name, user.last_name = "Nodira", "Rep"
    db.session.commit()
    return user


@pytest.fixture
def outlets(db, agent):
    bahor = OutletService.create(agent.id, dict(GROCERY))
    navruz = OutletService.create(
        agent.id,
        {
            **GROCERY,
            "name": "Navruz market",
            "latitude": NEAR[0],
            "longitude": NEAR[1],
            "contact": {"name": "Zafar", "phone": "+998901112299", "role": "owner"},
        },
    )
    return bahor, navruz


def _visit(db, *, agent, outlet, started_at, outcome, planned, in_radius=None, distance_m=None,
           checkin_skipped=False, ended_at=None):
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=agent.id,
        status="completed",
        planned=planned,
        current_step="close",
        started_at=started_at,
        checkin_at=started_at + timedelta(minutes=4),
        # Derived from `started_at` unless a caller overrides it. Every default-shaped row in
        # this file therefore has all three instants on ONE local day, which is why the
        # crossing-midnight fixture below has to hand its own `ended_at` in: without it no
        # fixture here can tell `started_at` and `ended_at` apart (V09).
        ended_at=ended_at if ended_at is not None else started_at + timedelta(minutes=25),
        outcome=outcome,
        in_radius=in_radius,
        distance_m=distance_m,
        checkin_skipped=checkin_skipped,
    )
    db.session.add(visit)
    db.session.commit()
    return visit


@pytest.fixture
def seeded(db, agent, second_agent, outlets, sample_user):
    """Five completed visits: two local days, two agents, two outlets, one outside the window.

    One of them carries a real `Order` (V10): `serialize_visit_admin_row` lifts
    `order["order_number"]` to the top level for the table's Order column, and until this row
    existed every backend fixture in the estate left `visit.order` NULL — so the populated
    branch of that lift had never once been executed, and a serializer that returned None
    unconditionally was invisible to the whole suite.
    """
    bahor, navruz = outlets
    midnight = _visit(db, agent=agent, outlet=bahor, started_at=STARTED_DAY_TWO_MIDNIGHT,
                      outcome="order_placed", planned=True)
    db.session.add(
        Order(
            order_number=ORDER_NUMBER,
            user_id=sample_user.id,
            status=OrderStatus.CONFIRMED,
            subtotal=Decimal("120000.00"),
            total_amount=Decimal("120000.00"),
            order_source="sales_agent",
            created_by_staff_id=agent.id,
            visit_id=midnight.id,
        )
    )
    db.session.commit()
    return {
        "bahor": bahor,
        "navruz": navruz,
        "in_range": _visit(db, agent=agent, outlet=bahor, started_at=STARTED_DAY_TWO,
                           outcome="order_placed", planned=True, in_radius=True, distance_m=41.0),
        # 01:30 LOCAL on day two, 20:30 UTC on day one -- the one instant whose two calendars
        # disagree, and therefore the only row that can fail a UTC-midnight day boundary.
        # It is also the row that carries the order.
        "midnight": midnight,
        "out_of_range": _visit(db, agent=agent, outlet=navruz, started_at=STARTED_DAY_ONE_EARLY,
                               outcome="no_order", planned=False, in_radius=False, distance_m=812.0),
        "unmeasured": _visit(db, agent=second_agent, outlet=bahor, started_at=STARTED_DAY_ONE_LATE,
                             outcome="owner_absent", planned=False, checkin_skipped=True),
        "outside": _visit(db, agent=agent, outlet=bahor, started_at=OUTSIDE,
                          outcome="order_placed", planned=True, in_radius=True, distance_m=10.0),
    }


def test_the_visits_list_publishes_the_row_the_table_draws(client, admin_claim_headers, db, agent, seeded):
    response = client.get(VISITS, headers=admin_claim_headers, query_string=PERIOD)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]

    # Newest first, and the 2026-09-01 visit is not in a 09-14..09-15 window.
    assert [row["id"] for row in body["visits"]] == [
        seeded["in_range"].id,
        seeded["midnight"].id,
        seeded["unmeasured"].id,
        seeded["out_of_range"].id,
    ]
    assert body["start_date"] == "2026-09-14" and body["end_date"] == "2026-09-15"
    assert body["meta"] == {
        "page": 1, "per_page": 20, "total": 4, "pages": 1, "has_next": False, "has_prev": False,
    }

    # The Order column, BOTH branches. The lift is the only expression of "this visit sold
    # something" the table reads, and a rename inside the nested `order` block would degrade
    # it to "—" on every visit that produced a sale.
    assert body["visits"][1]["order_number"] == ORDER_NUMBER
    assert body["visits"][1]["order"]["order_number"] == ORDER_NUMBER

    first = body["visits"][0]
    assert first["outlet_id"] == seeded["bahor"].id and first["outlet_name"] == "Bahor market"
    assert first["agent_user_id"] == agent.id and first["agent_name"] == "Sardor Agent"
    assert first["order_number"] is None  # published, not inferred from the nested `order` block
    assert first["outcome"] == "order_placed" and first["planned"] is True
    assert first["in_radius"] is True and first["distance_m"] == 41.0
    assert first["started_at"] == "2026-09-15T04:00:00+00:00"

    other_agents = body["visits"][2]
    assert other_agents["agent_name"] == "Nodira Rep" and other_agents["outlet_name"] == "Bahor market"
    assert other_agents["in_radius"] is None and other_agents["checkin_skipped"] is True


def test_every_filter_narrows_the_list_and_in_radius_stays_tri_state(
    client, admin_claim_headers, db, agent, second_agent, seeded
):
    def ids(**extra):
        response = client.get(VISITS, headers=admin_claim_headers, query_string={**PERIOD, **extra})
        assert response.status_code == 200, response.get_data(as_text=True)
        return [row["id"] for row in response.get_json()["data"]["visits"]]

    assert ids(agent_id=second_agent.id) == [seeded["unmeasured"].id]
    assert ids(outlet_id=seeded["navruz"].id) == [seeded["out_of_range"].id]
    assert ids(outcome="no_order") == [seeded["out_of_range"].id]
    assert ids(in_radius="true") == [seeded["in_range"].id]
    # `in_radius=false` is NOT "not in radius". Two visits in this window have a NULL column --
    # one agent skipped the check-in, one phone sent no pin -- and `false` must exclude BOTH as
    # firmly as it excludes the in-range one: an unmeasured visit is not an out-of-range
    # exception, and publishing it as one would put a clean day in a supervisor's alert list.
    assert ids(in_radius="false") == [seeded["out_of_range"].id]
    # ...and with no `in_radius` at all, every visit in the window is listed, NULLs included.
    assert len(ids()) == 4


def test_a_visit_just_after_local_midnight_belongs_to_the_local_day_it_started(
    client, admin_claim_headers, db, agent, seeded
):
    """The rule the whole module exists to protect (R2), pinned end to end and on BOTH surfaces.

    `STARTED_DAY_TWO_MIDNIGHT` is 01:30 LOCAL on 2026-09-15 and 20:30 UTC on the 14th. Asking
    for the single local day 09-15 therefore has exactly two right answers and two wrong ones:

      * the visits list must CONTAIN it, because the local window is
        [09-14T19:00Z, 09-15T19:00Z) -- a UTC-midnight window would start at 09-15T00:00Z and
        drop it, and the supervisor's day would silently lose its first call;
      * plan-vs-fact must file it under `day: "2026-09-15"` -- a `.date()` taken on the stored
        UTC instant would open a second row for the 14th and split one agent's morning across
        two days of the grid.

    Every other instant in this file is 09:00-14:00 local, where the UTC and local calendars
    agree and both mistakes still pass. This test is the one that fails.
    """
    listed = client.get(
        VISITS,
        headers=admin_claim_headers,
        query_string={"start_date": DAY_TWO.isoformat(), "end_date": DAY_TWO.isoformat()},
    )
    assert listed.status_code == 200, listed.get_data(as_text=True)
    assert [row["id"] for row in listed.get_json()["data"]["visits"]] == [
        seeded["in_range"].id,
        seeded["midnight"].id,
    ]

    # No `snapshots` fixture here on purpose: what is under test is which DAY the fact lands on,
    # and a day with visits and no stored plan still publishes its row (`plan_source: "none"`).
    rows = client.get(
        PLAN_VS_FACT,
        headers=admin_claim_headers,
        query_string={"start_date": DAY_TWO.isoformat(), "end_date": DAY_TWO.isoformat()},
    ).get_json()["data"]["rows"]
    assert [(row["agent_user_id"], row["day"], row["completed"], row["plan_source"]) for row in rows] == [
        (agent.id, "2026-09-15", 2, "none")
    ]


def test_the_page_meta_is_the_one_the_table_pages_on(client, admin_claim_headers, db, agent, seeded):
    body = client.get(
        VISITS, headers=admin_claim_headers, query_string={**PERIOD, "per_page": 2, "page": 2}
    ).get_json()["data"]

    assert [row["id"] for row in body["visits"]] == [seeded["unmeasured"].id, seeded["out_of_range"].id]
    assert body["meta"] == {
        "page": 2, "per_page": 2, "total": 4, "pages": 2, "has_next": False, "has_prev": True,
    }


def test_the_pager_is_clamped_not_refused(client, admin_claim_headers, db, agent, seeded):
    """antd's pager sends whatever it is holding, so both ends have to survive it.

    The bound lives on `PaginatedRangeQuery` (sales_serializers.py:238), which is also what
    `pagination_meta` re-asserts through `PaginationMeta`: an unclamped `?per_page=500` reaches
    that model as a validation error and the table gets a 500 where a hundred rows belong. This
    pins the RESOURCE's bound, so moving the clamp between the route and the model cannot
    quietly change the answer.
    """
    capped = client.get(VISITS, headers=admin_claim_headers, query_string={**PERIOD, "per_page": 500})
    assert capped.status_code == 200, capped.get_data(as_text=True)
    assert capped.get_json()["data"]["meta"]["per_page"] == 100

    floored = client.get(VISITS, headers=admin_claim_headers, query_string={**PERIOD, "page": -1})
    assert floored.status_code == 200, floored.get_data(as_text=True)
    assert floored.get_json()["data"]["meta"]["page"] == 1


def test_a_request_with_no_period_answers_the_default_local_week(
    app, client, admin_claim_headers, db, agent, frozen_local_now
):
    # The default is read from the production helper, never retyped here: a test that
    # re-implements the window keeps passing while the route drifts. `parse_date_range`
    # reads SALES_METRICS_MAX_RANGE_DAYS off `current_app.config`, so the call needs an
    # explicit app context — the test client's context is gone by the time we ask.
    # `frozen_local_now` is load-bearing (M13): the route and the assertion below each read
    # the clock, and unfrozen they can land on opposite sides of local midnight.
    with app.app_context():
        expected_start, expected_end = parse_date_range(None, None)
    body = client.get(VISITS, headers=admin_claim_headers).get_json()["data"]

    assert body["start_date"] == expected_start.isoformat()
    assert body["end_date"] == expected_end.isoformat()
    assert expected_end == local_date() and days_inclusive(expected_start, expected_end) == 7


@pytest.mark.parametrize(
    "query,code",
    [
        ({"start_date": "2026-09-15", "end_date": "2026-09-14"}, "SALES_DATE_RANGE_INVALID"),
        ({"start_date": "2026-01-01", "end_date": "2026-09-15"}, "SALES_DATE_RANGE_INVALID"),
        ({"start_date": "15-09-2026", "end_date": "2026-09-15"}, "SALES_DATE_RANGE_INVALID"),
        ({"start_date": "2026-09-14", "end_date": "2026-09-15", "outcome": "sold_out"},
         "SALES_VISIT_OUTCOME_INVALID"),
    ],
    ids=["inverted", "longer-than-the-cap", "unparsable", "unknown-outcome"],
)
def test_a_query_the_backend_refuses_is_a_400_with_a_code(client, admin_claim_headers, db, agent, query, code):
    response = client.get(VISITS, headers=admin_claim_headers, query_string=query)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == code


@pytest.fixture
def snapshots(db, agent):
    """Three stored plan rows for ONE agent: what the nightly job left behind.

    Written as rows on purpose. What plan-vs-fact reads is the stored plan (R1); driving
    `AgentDayPlanService.snapshot_all` here would be testing Task 1's due-counting instead of
    this route's join, and it could not produce a row for a past day at all.
    """
    rows = [
        SalesAgentDayPlan(agent_user_id=agent.id, plan_date=DAY_ZERO, due_count=3, overdue_count=0,
                          snapshot_at=SNAPSHOT_AT),
        SalesAgentDayPlan(agent_user_id=agent.id, plan_date=DAY_ONE, due_count=4, overdue_count=1,
                          snapshot_at=SNAPSHOT_AT),
        SalesAgentDayPlan(agent_user_id=agent.id, plan_date=DAY_TWO, due_count=2, overdue_count=0,
                          snapshot_at=SNAPSHOT_AT),
    ]
    db.session.add_all(rows)
    db.session.commit()
    return rows


def test_plan_vs_fact_pairs_the_stored_plan_with_the_day_that_happened(
    client, admin_claim_headers, db, agent, second_agent, seeded, snapshots
):
    response = client.get(PLAN_VS_FACT, headers=admin_claim_headers, query_string=PLAN_PERIOD)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]

    assert body["start_date"] == "2026-09-13" and body["end_date"] == "2026-09-15"
    assert body["rows"] == [
        # The second agent has no snapshot for that day: `due` is null and `plan_source` says
        # why. A zero here would read as "nothing was due" and score a perfect plan-vs-fact for
        # every day before the snapshot job shipped.
        {"agent_user_id": second_agent.id, "agent_name": "Nodira Rep", "day": "2026-09-14",
         "due": None, "completed": 1, "unplanned": 1, "strike_rate_pct": 0.0, "plan_source": "none"},
        # A day with a plan and no visits still gets its row — three shops were due, nobody went.
        {"agent_user_id": agent.id, "agent_name": "Sardor Agent", "day": "2026-09-13",
         "due": 3, "completed": 0, "unplanned": 0, "strike_rate_pct": None, "plan_source": "snapshot"},
        {"agent_user_id": agent.id, "agent_name": "Sardor Agent", "day": "2026-09-14",
         "due": 4, "completed": 1, "unplanned": 1, "strike_rate_pct": 0.0, "plan_source": "snapshot"},
        # Two visits on day two: 09:00 local, and 01:30 local -- whose stored instant is on the
        # 14th in UTC. A UTC-date bucket would move that one into the 09-14 row above.
        {"agent_user_id": agent.id, "agent_name": "Sardor Agent", "day": "2026-09-15",
         "due": 2, "completed": 2, "unplanned": 0, "strike_rate_pct": 100.0, "plan_source": "snapshot"},
    ]


def test_plan_vs_fact_filters_by_agent_and_refuses_a_range_nobody_can_read(
    client, admin_claim_headers, db, agent, second_agent, seeded, snapshots
):
    mine = client.get(
        PLAN_VS_FACT, headers=admin_claim_headers, query_string={**PLAN_PERIOD, "agent_id": agent.id}
    ).get_json()["data"]["rows"]
    assert [(row["agent_user_id"], row["day"], row["due"], row["completed"]) for row in mine] == [
        (agent.id, "2026-09-13", 3, 0),
        (agent.id, "2026-09-14", 4, 1),
        (agent.id, "2026-09-15", 2, 2),
    ]

    # Unpaginated by design, so the 92-day cap is the only thing standing between a supervisor
    # and a full-table scan — the SAME parser and the SAME code as the visits list.
    refused = client.get(
        PLAN_VS_FACT, headers=admin_claim_headers,
        query_string={"start_date": "2026-01-01", "end_date": "2026-09-15"},
    )
    assert refused.status_code == 400
    assert refused.get_json()["error_code"] == "SALES_DATE_RANGE_INVALID"


@pytest.fixture
def crossing_midnight(db, agent, outlets):
    """ONE visit that starts at 23:50 local on day one and closes at 00:20 local on day two.

    Its own fixture rather than a sixth row in `seeded`: `seeded` is what several pins in this
    file count, and a row that deliberately straddles a day boundary is the wrong thing to
    have sitting inside a list every other assertion reads.
    """
    bahor, _ = outlets
    return _visit(db, agent=agent, outlet=bahor, started_at=STARTED_DAY_ONE_NIGHT,
                  ended_at=ENDED_DAY_TWO_EARLY, outcome="order_placed", planned=True,
                  in_radius=True, distance_m=18.0)


def test_a_visit_that_closes_after_midnight_stays_on_the_day_it_started(
    client, admin_claim_headers, db, agent, crossing_midnight, snapshots
):
    """R2, the phase's most-repeated rule, with a fixture that can finally fail it (V09).

    "A visit belongs to the local day it STARTED" is restated by R2, R8 and R25 so the Visits
    page, the 08:00 exception count and the KPI tab cannot disagree about which day a visit
    happened on. Until this row, every fixture in the estate stamped `checkin_at` and
    `ended_at` a few minutes after `started_at`, so all three columns landed on one local day
    and windowing on ANY of them passed every assertion. This visit's `started_at` is
    2026-09-14T18:50Z and its `ended_at` is 19:20Z — 30 minutes apart, with the local day
    boundary (19:00Z) between them.

    So the day-one window must CONTAIN it and the day-two window must NOT, on both surfaces:
    the auto-abandon sweep closes a forgotten visit hours later on a different day
    (`visit_service.py:768`), and under an `ended_at` window that agent's Tuesday call would
    silently move into Wednesday's grid.
    """
    day_one = client.get(
        VISITS, headers=admin_claim_headers,
        query_string={"start_date": DAY_ONE.isoformat(), "end_date": DAY_ONE.isoformat()},
    )
    assert day_one.status_code == 200, day_one.get_data(as_text=True)
    assert [row["id"] for row in day_one.get_json()["data"]["visits"]] == [crossing_midnight.id]

    day_two = client.get(
        VISITS, headers=admin_claim_headers,
        query_string={"start_date": DAY_TWO.isoformat(), "end_date": DAY_TWO.isoformat()},
    )
    assert day_two.status_code == 200, day_two.get_data(as_text=True)
    assert [row["id"] for row in day_two.get_json()["data"]["visits"]] == []

    # ...and the fact is bucketed the same way: day one owns the completion, day two is empty
    # even though that is the calendar day the visit was closed on.
    rows = client.get(
        PLAN_VS_FACT, headers=admin_claim_headers, query_string=PLAN_PERIOD
    ).get_json()["data"]["rows"]
    assert [(row["day"], row["completed"], row["due"]) for row in rows] == [
        ("2026-09-13", 0, 3),
        ("2026-09-14", 1, 4),
        ("2026-09-15", 0, 2),
    ]
