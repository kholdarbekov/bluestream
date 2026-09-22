"""AgentMetricsService — the twenty KPIs, on a week seeded row by row.

Frozen instants, handed IN (`now=`), the `test_agent_digest_service.py` contract: every
number here is arithmetic against one moment and nothing patches a clock.

Nothing here re-implements a rule. The expected numbers are WRITTEN DOWN, never recomputed
from the same rows the service reads — a test that re-derives `plan_vs_fact_pct` from the
fixtures would keep passing through any rewrite of the rule it is supposed to pin. The key
tuple is imported from the service, so a renamed key fails here and not in the bot at 08:30.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.sales import Outlet, OutletStageHistory, SalesAgentProfile
from business_app.models.sales_visits import SalesAgentDayPlan, Visit, VisitStockCheck
from business_app.services.sales.agent_account_service import SalesAgentAccountService
from business_app.services.sales.agent_metrics_service import METRIC_GROUPS, METRIC_KEYS, AgentMetricsService
from shared.enums import OrderStatus
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.unit

# 2026-09-15 03:30 UTC = 08:30 Tashkent. The local day 2026-09-15 began at 2026-09-14
# 19:00 UTC, so every boundary below is a real one and not an artefact of UTC midnight
# (the digest test's choice, for the same reason).
FROZEN_UTC = datetime(2026, 9, 15, 3, 30, tzinfo=timezone.utc)
START_DATE = date(2026, 9, 9)
END_DATE = date(2026, 9, 15)          # inclusive -> 7 local days
UTC = timezone.utc


def _at(day, hour, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC)


def _agent(db, *, phone, first_name, profile_active=True):
    user = make_sales_agent_user(db, phone=phone, staff_roles=["sales_agent"])
    user.first_name = first_name
    user.last_name = "Agent"
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"], is_active=profile_active))
    db.session.commit()
    return user


def _outlet(db, *, name, stage="active", assigned=None, onboarded=None, created_at):
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage=stage,
        assigned_agent_user_id=assigned.id if assigned is not None else None,
        onboarded_by_user_id=onboarded.id if onboarded is not None else None,
        created_at=created_at,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _stage_row(db, outlet, to_stage, created_at):
    db.session.add(OutletStageHistory(outlet_id=outlet.id, to_stage=to_stage, created_at=created_at))
    db.session.commit()


def _visit(db, *, agent, outlet, started_at, status="completed", planned=False, outcome=None,
           checkin_at=None, ended_at=None, in_radius=None, checkin_skipped=False):
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=agent.id,
        status=status,
        planned=planned,
        current_step="close",
        started_at=started_at,
        checkin_at=checkin_at,
        ended_at=ended_at,
        outcome=outcome,
        in_radius=in_radius,
        checkin_skipped=checkin_skipped,
    )
    db.session.add(visit)
    db.session.commit()
    return visit


def _stock_check(db, visit, product, *, suggested, accepted):
    db.session.add(
        VisitStockCheck(
            visit_id=visit.id,
            product_id=product.id,
            on_hand_qty=2,
            suggested_qty=suggested,
            accepted_qty=accepted,
        )
    )
    db.session.commit()


def _product(db, category, *, name, bottles_per_unit):
    product = Product(
        name=name,
        category_id=category.id,
        size="19L",
        base_price=Decimal("15000.00"),
        tracks_returnable_bottles=bottles_per_unit > 0,
        returnable_bottles_per_unit=Decimal(str(bottles_per_unit)),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _order(db, *, customer, agent, number, created_at, status, total, is_paid=False,
           source="sales_agent", visit=None, lines=()):
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=status,
        subtotal=total,
        total_amount=total,
        is_paid=is_paid,
        order_source=source,
        created_by_staff_id=agent.id,
        visit_id=visit.id if visit is not None else None,
        created_at=created_at,
    )
    db.session.add(order)
    db.session.flush()
    for product, quantity, is_reward in lines:
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=Decimal("15000.00"),
                total_price=Decimal("15000.00") * quantity,
                is_reward_item=is_reward,
            )
        )
    db.session.commit()
    return order


def _plan(db, agent, day, *, due, overdue=0):
    db.session.add(
        SalesAgentDayPlan(
            agent_user_id=agent.id,
            plan_date=date(2026, 9, day),
            due_count=due,
            overdue_count=overdue,
            snapshot_at=_at(day, 20, 20),
        )
    )
    db.session.commit()


@pytest.fixture
def seeded_week(db, sample_user, sample_category):
    """One deliberate week for agent A, with agent B beside them to prove isolation.

    Every row exists to move exactly one number, and the ones that must NOT move a number
    are here too: a visit started the local day BEFORE the window, an order placed from the
    admin panel (`order_source != "sales_agent"`), an imported outlet with no onboarder, a
    reactivated outlet whose FIRST activation is outside the window, and a reward line.
    """
    agent_a = _agent(db, phone="+998901234582", first_name="Aziz")
    agent_b = _agent(db, phone="+998901234583", first_name="Bekzod")

    returnable = _product(db, sample_category, name="Suv 19L", bottles_per_unit=1)
    still = _product(db, sample_category, name="Suv 0.5L", bottles_per_unit=0)

    # --- outlets -------------------------------------------------------------------
    bahor = _outlet(db, name="Bahor", assigned=agent_a, onboarded=agent_a, created_at=_at(10, 6))
    _stage_row(db, bahor, "prospect", _at(10, 6))
    _stage_row(db, bahor, "active", _at(12, 6))                      # first activation, IN window
    chorsu = _outlet(db, name="Chorsu", assigned=agent_a, created_at=datetime(2026, 8, 1, 6, tzinfo=UTC))
    dostlik = _outlet(db, name="Do'stlik", stage="at_risk", assigned=agent_a,
                      created_at=datetime(2026, 8, 1, 6, tzinfo=UTC))
    _outlet(db, name="Eski", stage="lost", assigned=agent_a, created_at=datetime(2026, 8, 1, 6, tzinfo=UTC))
    hilol = _outlet(db, name="Hilol", assigned=agent_a, onboarded=agent_a,
                    created_at=datetime(2026, 8, 15, 6, tzinfo=UTC))
    _stage_row(db, hilol, "active", datetime(2026, 8, 20, 6, tzinfo=UTC))   # FIRST activation, outside
    _stage_row(db, hilol, "dormant", _at(9, 6))
    _stage_row(db, hilol, "active", _at(13, 6))                             # a reactivation: never counts
    _outlet(db, name="Gulzor", stage="prospect", assigned=agent_b, onboarded=agent_a, created_at=_at(11, 6))
    _outlet(db, name="Farovon", assigned=agent_b, created_at=datetime(2026, 8, 1, 6, tzinfo=UTC))
    imported = _outlet(db, name="Import", created_at=_at(12, 6))            # no onboarder: nobody's credit
    _stage_row(db, imported, "active", _at(12, 6))

    # --- visits --------------------------------------------------------------------
    v1 = _visit(db, agent=agent_a, outlet=bahor, started_at=_at(15, 6), planned=True,
                outcome="order_placed", checkin_at=_at(15, 6, 5), ended_at=_at(15, 6, 30), in_radius=True)
    v2 = _visit(db, agent=agent_a, outlet=chorsu, started_at=_at(10, 5), planned=True,
                outcome="no_order", checkin_at=_at(10, 5, 2), ended_at=_at(10, 5, 17), in_radius=False)
    v3 = _visit(db, agent=agent_a, outlet=dostlik, started_at=_at(11, 5), planned=False,
                outcome="order_placed", checkin_at=_at(11, 5, 1), ended_at=_at(11, 5, 6),
                in_radius=None, checkin_skipped=True)
    _visit(db, agent=agent_a, outlet=hilol, started_at=_at(12, 5), planned=False, outcome="no_order",
           checkin_at=_at(12, 5), ended_at=_at(12, 12), in_radius=True)      # 7 h: sweep-closed, off the mean
    _visit(db, agent=agent_a, outlet=chorsu, started_at=_at(13, 5), planned=False, outcome="closed",
           checkin_at=_at(13, 5), ended_at=_at(13, 5, 0, 40), in_radius=True)   # 40 s: a short visit
    v6 = _visit(db, agent=agent_a, outlet=dostlik, started_at=_at(14, 5), status="abandoned", planned=True,
                checkin_at=_at(14, 5, 1), ended_at=_at(14, 11), in_radius=False)
    _visit(db, agent=agent_a, outlet=chorsu, started_at=_at(8, 5), planned=True, outcome="order_placed",
           checkin_at=_at(8, 5), ended_at=_at(8, 5, 30), in_radius=True)      # local day 09-08: outside
    _visit(db, agent=agent_b, outlet=chorsu, started_at=_at(11, 5), planned=True, outcome="order_placed",
           checkin_at=_at(11, 5), ended_at=_at(11, 5, 30), in_radius=True)

    _stock_check(db, v1, returnable, suggested=10, accepted=6)
    _stock_check(db, v2, returnable, suggested=10, accepted=None)     # suggested, not ordered -> 0 accepted
    _stock_check(db, v3, returnable, suggested=0, accepted=4)         # no suggestion: outside the question
    _stock_check(db, v6, returnable, suggested=20, accepted=20)       # abandoned visit: never counted

    # --- day plans (the plan-vs-fact denominator) ----------------------------------
    for day, due, overdue in ((9, 3, 1), (10, 2, 0), (11, 0, 0), (12, 1, 0), (13, 2, 0), (14, 0, 0), (15, 2, 1)):
        _plan(db, agent_a, day, due=due, overdue=overdue)
    _plan(db, agent_a, 8, due=5)                                      # outside the window
    _plan(db, agent_b, 10, due=4)

    # --- orders --------------------------------------------------------------------
    _order(db, customer=sample_user, agent=agent_a, number="SA_000101_26", created_at=_at(15, 6, 30),
           status=OrderStatus.DELIVERED, is_paid=True, total=Decimal("150000.00"), visit=v1,
           lines=((returnable, 3, False), (returnable, 1, True)))     # the reward line never counts bottles
    _order(db, customer=sample_user, agent=agent_a, number="SA_000102_26", created_at=_at(11, 5, 30),
           status=OrderStatus.DELIVERED, is_paid=False, total=Decimal("70000.00"),
           lines=((returnable, 2, False),))
    _order(db, customer=sample_user, agent=agent_a, number="SA_000103_26", created_at=_at(11, 5, 40),
           status=OrderStatus.CANCELLED, total=Decimal("50000.00"), visit=v3,
           lines=((returnable, 1, False),))                           # declined: v3 still struck
    _order(db, customer=sample_user, agent=agent_a, number="SA_000104_26", created_at=_at(12, 5, 30),
           status=OrderStatus.PENDING, total=Decimal("60000.00"))
    _order(db, customer=sample_user, agent=agent_a, number="SA_000105_26", created_at=_at(13, 5, 30),
           status=OrderStatus.DELIVERED, is_paid=True, total=Decimal("90000.00"),
           lines=((returnable, 2, False), (still, 2, False)))         # still water leaves no bottle
    _order(db, customer=sample_user, agent=agent_a, number="SA_000106_26", created_at=_at(8, 5, 30),
           status=OrderStatus.DELIVERED, is_paid=True, total=Decimal("999000.00"),
           lines=((returnable, 9, False),))                           # local day 09-08: outside
    _order(db, customer=sample_user, agent=agent_a, number="AD_000107_26", created_at=_at(12, 6),
           status=OrderStatus.DELIVERED, is_paid=True, total=Decimal("40000.00"), source="phone")
    _order(db, customer=sample_user, agent=agent_b, number="SA_000108_26", created_at=_at(12, 5, 30),
           status=OrderStatus.DELIVERED, is_paid=True, total=Decimal("30000.00"))

    return SimpleNamespace(agent_a=agent_a, agent_b=agent_b, returnable=returnable, bahor=bahor)


# The week above, read out by hand. Each number is the ruling applied to the fixtures, not
# a formula this test could re-run: that is the whole point of writing them down.
EXPECTED = {
    "planned_visits": 10,             # 3+2+0+1+2+0+2 snapshot due_counts inside the window
    "completed_visits": 5,            # the abandoned one and the pre-window one are not visits of this week
    "plan_vs_fact_pct": 20.0,         # 2 completed-and-planned / 10 planned
    "unplanned_visits": 3,
    "visits_per_day": 0.7,            # 5 / 7 inclusive local days
    "strike_rate_pct": 40.0,          # 2 of 5 ended in an order; the cancelled one still struck
    "assigned_outlets": 4,            # the `lost` shop is nobody's territory
    "active_outlets": 3,
    "active_share_pct": 75.0,
    "new_outlets_registered": 2,      # Bahor + Gulzor; Hilol predates the window, Import has no onboarder
    "new_outlets_activated": 1,       # Bahor's FIRST activation; Hilol's reactivation never counts
    "orders_placed": 5,
    "orders_delivered_paid": 2,
    "bottles_delivered_paid": 5,      # 3 + 2; the reward bottle and the still water are excluded
    "revenue_delivered_paid": 240000.0,
    "agent_orders_cancelled": 1,
    "suggested_vs_accepted_pct": 30.0,   # 6 accepted / 20 suggested, a ratio of sums
    "out_of_range_checkins": 2,          # includes the abandoned visit: discipline is not about closing
    "skipped_checkins": 1,
    "avg_visit_minutes": 11.4,           # (1500+900+300+40)s / 4; the 7-hour sweep close is dropped
}

EMPTY = {
    "planned_visits": 0,
    "completed_visits": 0,
    "plan_vs_fact_pct": None,
    "unplanned_visits": 0,
    "visits_per_day": 0.0,
    "strike_rate_pct": None,
    "assigned_outlets": 0,
    "active_outlets": 0,
    "active_share_pct": None,
    "new_outlets_registered": 0,
    "new_outlets_activated": 0,
    "orders_placed": 0,
    "orders_delivered_paid": 0,
    "bottles_delivered_paid": 0,
    "revenue_delivered_paid": 0.0,
    "agent_orders_cancelled": 0,
    "suggested_vs_accepted_pct": None,
    "out_of_range_checkins": 0,
    "skipped_checkins": 0,
    "avg_visit_minutes": None,
}


def test_the_seeded_week_answers_every_one_of_the_twenty_keys(db, seeded_week):
    metrics = AgentMetricsService.compute(seeded_week.agent_a.id, START_DATE, END_DATE, now=FROZEN_UTC)

    assert metrics == EXPECTED
    # The ORDER is the spec's order and the set is closed: a consumer that renders
    # `METRIC_KEYS` in sequence (the bot card, the email table, the CSV) gets the spec's
    # reading order for free, and a 21st key cannot appear without this failing.
    assert tuple(metrics) == METRIC_KEYS


def test_the_four_metric_groups_are_the_twenty_keys_split_once(db):
    """R30: the grouping has ONE expression, and it is this one.

    The bot card draws four sections and the Analytics tab draws four column groups; before
    this dict both hand-copied the membership and only `METRIC_KEYS` held them to the same
    twenty names. Concatenation, order and membership are asserted together because that is
    the whole promise: a consumer walking the groups sees the spec's reading order, and a key
    that joined a group without joining the tuple would render in one surface and not the other.
    """
    assert tuple(METRIC_GROUPS) == ("visits", "outlets", "orders", "discipline")
    assert sum(METRIC_GROUPS.values(), ()) == METRIC_KEYS
    assert "suggested_vs_accepted_pct" in METRIC_GROUPS["orders"]


def test_a_second_agent_sees_only_their_own_week(db, seeded_week):
    """Isolation is per agent, not per estate: B shares outlets and days with A."""
    metrics = AgentMetricsService.compute(seeded_week.agent_b.id, START_DATE, END_DATE, now=FROZEN_UTC)

    assert metrics["completed_visits"] == 1
    assert metrics["planned_visits"] == 4
    assert metrics["orders_placed"] == 1
    assert metrics["assigned_outlets"] == 2        # Farovon + Gulzor (a prospect is still territory)
    assert metrics["active_outlets"] == 1
    assert metrics["new_outlets_registered"] == 0  # Gulzor was onboarded by A and stays A's credit


def test_an_agent_with_nothing_gets_zeroes_and_nulls_never_a_crash(db):
    """Zero and None are different answers: 0.0% is a bad week, None is no question at all
    (the `open_receivable is None` precedent on the outlet card)."""
    lonely = _agent(db, phone="+998901234584", first_name="Dilshod")

    metrics = AgentMetricsService.compute(lonely.id, START_DATE, END_DATE, now=FROZEN_UTC)

    assert metrics == EMPTY
    assert tuple(metrics) == METRIC_KEYS


def test_rows_for_period_lists_every_active_agent_once_sorted_by_name(db, seeded_week):
    """The Analytics tab, the all-agents route and the weekly email all want this table.

    An inactive profile is off the report entirely — `is_active` is the switch the admin
    page's *Active* toggle writes (`SalesAgentAccountService.set_active`), and a
    deactivated agent's zero row would read as a bad week rather than as no week.
    """
    _agent(db, phone="+998901234585", first_name="Zafar", profile_active=False)

    rows = AgentMetricsService.rows_for_period(START_DATE, END_DATE, now=FROZEN_UTC)

    assert [(row["agent_user_id"], row["agent_name"], row["phone"]) for row in rows] == [
        (seeded_week.agent_a.id, "Aziz Agent", "+998901234582"),
        (seeded_week.agent_b.id, "Bekzod Agent", "+998901234583"),
    ]
    # Identity first, then the spec's twenty in order: one shape for the table, the CSV and
    # the email row, pinned against the producer's own tuple.
    assert tuple(rows[0]) == ("agent_user_id", "agent_name", "phone") + METRIC_KEYS
    assert rows[0]["completed_visits"] == EXPECTED["completed_visits"]
    assert rows[0]["revenue_delivered_paid"] == EXPECTED["revenue_delivered_paid"]


def test_rows_for_period_can_be_narrowed_to_named_agents(db, seeded_week):
    rows = AgentMetricsService.rows_for_period(
        START_DATE, END_DATE, now=FROZEN_UTC, agent_user_ids=[seeded_week.agent_b.id]
    )

    assert [row["agent_user_id"] for row in rows] == [seeded_week.agent_b.id]


def test_active_agents_is_the_roster_every_phase_three_surface_reports_on(db, seeded_week):
    """R1/SSOT: the nightly plan snapshot and the metrics rows must never disagree about
    who is an agent, or plan-vs-fact gets a denominator for somebody the report omits.

    A CHARACTERIZATION pin on Task 1's helper, not a new rule: `active_agents()` ships in
    Task 1 (role + active profile + an ACTIVE user row) and this task reads it. It is pinned
    from here because THIS is the file that would notice the day the metrics rows and the
    snapshot stopped enumerating the same people."""
    inactive = _agent(db, phone="+998901234586", first_name="Zarina", profile_active=False)
    plain = make_sales_agent_user(db, phone="+998901234587")   # role, but no profile row at all

    ids = {user.id for user in SalesAgentAccountService.active_agents()}

    assert ids == {seeded_week.agent_a.id, seeded_week.agent_b.id}
    assert inactive.id not in ids
    assert plain.id not in ids


def test_today_counters_are_the_metrics_for_today_and_not_a_second_rule(db, seeded_week):
    """The *Sales agents* row and the agent's metrics card must print ONE answer.

    `visits_today` is `compute`'s `completed_visits` over the local day and `orders_today`
    is its `orders_placed`; this test holds the two together rather than restating either,
    so a change to the visit window cannot leave the row behind.
    """
    agent_a, agent_b = seeded_week.agent_a, seeded_week.agent_b

    counters = AgentMetricsService.today_counters([agent_a.id, agent_b.id], now=FROZEN_UTC)
    today = AgentMetricsService.compute(agent_a.id, END_DATE, END_DATE, now=FROZEN_UTC)

    assert counters[agent_a.id] == {
        "visits_today": 1,
        "orders_today": 1,
        "assigned_outlets": 4,
        "active_outlets": 3,
    }
    assert counters[agent_b.id] == {
        "visits_today": 0,
        "orders_today": 0,
        "assigned_outlets": 2,
        "active_outlets": 1,
    }
    assert counters[agent_a.id]["visits_today"] == today["completed_visits"]
    assert counters[agent_a.id]["orders_today"] == today["orders_placed"]
    assert counters[agent_a.id]["assigned_outlets"] == today["assigned_outlets"]
    assert counters[agent_a.id]["active_outlets"] == today["active_outlets"]


def test_today_counters_answer_for_every_id_asked_about(db, seeded_week):
    """A page of 20 rows must get 20 rows back: an agent with no day is zeros, not a
    missing key the UI renders as `undefined`."""
    lonely = _agent(db, phone="+998901234588", first_name="Eldor")

    counters = AgentMetricsService.today_counters([lonely.id], now=FROZEN_UTC)

    assert counters == {
        lonely.id: {"visits_today": 0, "orders_today": 0, "assigned_outlets": 0, "active_outlets": 0}
    }
    assert AgentMetricsService.today_counters([], now=FROZEN_UTC) == {}


def test_the_snapshot_job_and_the_metrics_rows_read_the_same_roster(db, seeded_week):
    """R1/SSOT: "an active sales agent" has ONE expression.

    If the nightly snapshot enumerated a different roster from the report, plan-vs-fact
    would hold a denominator for somebody the report never lists (or, worse, publish
    `plan_source: "none"` for an agent who was snapshotted all along).
    """
    from business_app.services.sales.day_plan_service import AgentDayPlanService

    SalesAgentDayPlan.query.delete()
    db.session.commit()
    _agent(db, phone="+998901234589", first_name="Zuhra", profile_active=False)

    AgentDayPlanService.snapshot_all(now=FROZEN_UTC)

    snapshotted = {row.agent_user_id for row in SalesAgentDayPlan.query.all()}
    assert snapshotted == {user.id for user in SalesAgentAccountService.active_agents()}
    assert snapshotted == {
        row["agent_user_id"]
        for row in AgentMetricsService.rows_for_period(START_DATE, END_DATE, now=FROZEN_UTC)
    }


def test_plan_vs_fact_is_measured_only_on_the_days_a_plan_existed(db, seeded_week):
    """R47/V01: an unsnapshotted day leaves the ratio, it does not poison it.

    Both halves of the fraction have to come from the SAME days or the number is a lie:
    `Visit.planned` has been stamped since phase 2a, so a window straddling the night the
    01:20 job did not run would otherwise divide a seven-day numerator by a five-day
    denominator — and `cap=100.0` would dress the result up as a flawless 100.0%.

    Agent A's 09-10 snapshot is the one deleted because it is the only day besides 09-15
    that carries a completed PLANNED visit (v2), so the deletion moves both halves and the
    two failure modes are distinguishable:

      * denominator = 3+0+1+2+0+2 = 8 (09-10's due_count of 2 is gone with its row),
      * numerator   = 1 (only v1, on 09-15; v2's day has no plan behind it any more),
      * 1/8 = 12.5%.

    A numerator that kept v2 would publish 2/8 = 25.0, and the pre-R47 rule published None.
    Both are named here so neither can come back unnoticed.
    """
    SalesAgentDayPlan.query.filter_by(
        agent_user_id=seeded_week.agent_a.id, plan_date=date(2026, 9, 10)
    ).delete()
    db.session.commit()

    metrics = AgentMetricsService.compute(seeded_week.agent_a.id, START_DATE, END_DATE, now=FROZEN_UTC)

    assert metrics["plan_vs_fact_pct"] == 12.5
    # `planned_visits` is unchanged in DEFINITION -- the sum over the rows that exist -- which
    # is now 8 rather than 10. It is still a true count of the plan we have.
    assert metrics["planned_visits"] == 8
    # Nothing else moves: an unsnapshotted day is not a day that stopped happening.
    assert {
        key: value for key, value in metrics.items()
        if key not in ("planned_visits", "plan_vs_fact_pct")
    } == {
        key: value for key, value in EXPECTED.items()
        if key not in ("planned_visits", "plan_vs_fact_pct")
    }
    # No 21st key: METRIC_KEYS is pinned by the bot card, the three email templates and the
    # Analytics tab, so the caveat rides on the value and never on a new field.
    assert tuple(metrics) == METRIC_KEYS


def test_a_fully_snapshotted_window_still_publishes_the_percentage(db, seeded_week):
    """The other half of R47: narrowing is about the DAYS, not about the numbers.

    Without this, "always measure nothing" would pass the test above.
    """
    metrics = AgentMetricsService.compute(seeded_week.agent_a.id, START_DATE, END_DATE, now=FROZEN_UTC)

    assert metrics["plan_vs_fact_pct"] == EXPECTED["plan_vs_fact_pct"]


def test_plan_vs_fact_is_none_when_no_day_of_the_window_was_snapshotted(db, seeded_week):
    """The only case that still has no answer: nothing to measure against.

    This is the go-live window and the R2 rule in one -- a denominator of zero is not a
    score of zero, and printing "0.0%" would make an unmeasured week look like a failed one.
    The five completed visits are still counted; only the ratio has no subject.
    """
    SalesAgentDayPlan.query.filter_by(agent_user_id=seeded_week.agent_a.id).delete()
    db.session.commit()

    metrics = AgentMetricsService.compute(seeded_week.agent_a.id, START_DATE, END_DATE, now=FROZEN_UTC)

    assert metrics["plan_vs_fact_pct"] is None
    assert metrics["planned_visits"] == 0
    assert metrics["completed_visits"] == EXPECTED["completed_visits"]


def test_pct_is_the_one_percentage_rule_null_cap_and_one_decimal():
    """V08: the phase's single percentage expression, exercised directly.

    Every percentage the seeded fixtures produce is under 100 and exact to one decimal, so
    neither `cap=100.0` nor the choice of `round(..., 1)` changes an asserted number anywhere
    else in the suite — deleting the cap or rounding to two decimals passes it whole. These
    six lines are the only place either can fail.
    """
    # A question that does not apply has no answer — None, never 0.0.
    assert AgentMetricsService.pct(0, 0) is None
    assert AgentMetricsService.pct(7, 0, cap=100.0) is None
    # ONE decimal: `round(..., 2)` would answer 33.33 / 66.67 here.
    assert AgentMetricsService.pct(1, 3) == 33.3
    assert AgentMetricsService.pct(2, 3) == 66.7
    # The cap is a clamp, not a rule about the inputs: uncapped, the same call answers 150.0.
    assert AgentMetricsService.pct(3, 2, cap=100.0) == 100.0
    assert AgentMetricsService.pct(3, 2) == 150.0
    # A capped value below the ceiling is untouched.
    assert AgentMetricsService.pct(1, 2, cap=100.0) == 50.0
