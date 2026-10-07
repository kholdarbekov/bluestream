"""The agent's own pay: S1 `GET /staff/sales/me/earnings` and S2 `.../me/earnings/lines`.

Compensation spec §4.8 (the pipeline), §5.4 (both routes and their shapes), §10.3 (T-E2E-1,
T-SYNC-5, T-DIFF-8, T-PIPE-1, T-PIPE-2, T-VIS-1, T-VIS-2, T-PEN-2, T-CARRY-1/2, T-OWED-1/2) and
Review Focus 4.

The bot computes nothing: every figure, month, state and next step it prints is one of these
keys. Two routes, no agent id: the token decides whose pay it is (§5.6; the isolation half is
`test_sales_pay_access_matrix.py`).

The on-demand throttle (SALES_PAY_ONDEMAND_SYNC_SECONDS) counts REAL seconds while these tests
jump the frozen calendar by days, and the admin reads a builder makes (A0's answer, A2, A8) claim
the agent's window as they sync. `my_earnings` therefore lets the window lapse before each look,
as the real days between two looks would; only T-SYNC-5 looks twice inside one window.
"""

import inspect
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import redis

from business_app import db as database
from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.sales_pay import SalesPayLedgerLine
from business_app.services.sales import pay_ledger_service
from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
from business_app.services.sales.visit_rules import short_visit_threshold
from business_app.utils import local_windows
from shared.enums import DeliveryStatus, OrderStatus
from shared.redis_keyspace import RedisKeyspace
from tests.integration.sales_pay_builders import (
    APPROVALS,
    PAY_API,
    PENALTY_TYPE_NAMES,
    PROPOSALS_URL,
    adjust_cash,
    approved_outlet,
    carry_case,
    claim_headers,
    configure_agent,
    deliver,
    freeze_local_now,
    grocery_store,
    incident_body,
    ledger_lines,
    live_cash_events,
    local_at,
    make_agent_order,
    make_outlet,
    make_product,
    make_store,
    move_clock,
    october_world,
    pay_agent,
    pay_call,
    pay_route,
    pay_store,
    pay_world,
    place_visit_order,
    plan_days,
    refusal,
    utc_at,
    verified_visits,
    version_payload,
    water_order,
)
from tests.integration.sales_pay_builders import (  # noqa: F401 -- field_week is a fixture
    FIELD_WEEK_COUNTED,
    FIELD_WEEK_DUE,
    FIELD_WEEK_PCT,
    field_week,
)
from tests.unit.test_outlet_dedupe import PIN

pytestmark = pytest.mark.integration

EARNINGS = "/api/v1/staff/sales/me/earnings"
LINES = "/api/v1/staff/sales/me/earnings/lines"
STATS = "/api/v1/staff/sales/me/stats"
ADMIN_STATUS = "/api/v1/admin/orders/{}/status"
COLLECTIONS = "/api/v1/admin/staff/cash-reconciliation/collections"
NOTHING_CARRIED = {"amount": 0.0, "from_month": None, "source": None}
# "Water 19L" as `basis_inputs` freezes it: the builders' products carry no translation rows, so
# every language falls back to the name column.
WATER_NAME = {"en": "Water 19L", "uz": "Water 19L", "ru": "Water 19L"}


# ---------------------------------------------------------------------------- helpers


def lapse_throttle(agent) -> None:
    """Let the agent's on-demand throttle lapse, as the real time between two looks would.

    The key's TTL is real seconds; the frozen calendar moves days in milliseconds. Task 5's
    T-DIFF-8 test deletes it the same way.
    """
    pay_ledger_service.redis_client.delete(RedisKeyspace.sales_pay_fresh(agent.id))


def my_earnings(client, headers, agent) -> dict:
    """S1 as the bot calls it (no query string), after the throttle has lapsed."""
    lapse_throttle(agent)
    return pay_call(client, "GET", EARNINGS, headers)


def my_lines(client, headers, month: str, page: int = 1) -> dict:
    """S2 as the bot calls it."""
    return pay_call(client, "GET", f"{LINES}?month={month}&page={page}", headers)


def shape(order):
    """(kind, cause, money level) of every pay line of the order, in posting order. A commission line
    records a level, never an amount (v5); a reversal records none."""
    lines = ledger_lines(order)
    assert [line.amount for line in lines] == [None] * len(lines)
    return [(line.kind, line.cause, line.snapshot.get("received_applied")) for line in lines]


def without_products(commission: dict) -> dict:
    return {key: value for key, value in commission.items() if key != "products"}


def wall_clock_pay(db, client, headers, agent, operator) -> SimpleNamespace:
    """Pay started for the CURRENT local month through the admin routes, with no clock frozen.

    For the scenarios §10.3 runs on the wall clock (T-E2E-1, T-PIPE-2): the visit routes decide
    the same-day rule and the delivery date on the real day. Plan "Standard" (19 L at 1,500 per
    unit, everything else 3 %), terms of 3,000,000 from the 1st, and a shop the agent onboarded
    with a pin to check in at. The juice is stored in the 5L size, as `pay_world`'s is:
    `product_size_enum` has no 1L, and only the water carries a product rate.

    The shop is Task 6's `approved_outlet`: activated by the operator through the real
    `OutletService.approve`, which gives it the address `place_order` needs
    (`_assert_orderable`), on a grocery-store account, so no tier discount reaches a total.
    """
    now = datetime.now(timezone.utc)
    month = local_windows.format_month(local_windows.local_month(now))
    water = make_product(db, name="Water 19L", price="20000", size="19L")
    juice = make_product(db, name="Juice 1L", price="25050", size="5L")
    plan = pay_call(
        client,
        "POST",
        f"{PAY_API}/plans",
        headers,
        {"name": "Standard", "version": version_payload(month, water.id)},
        status=201,
    )["plan"]
    configure_agent(
        client, headers, agent, plan_id=plan["id"], base=3000000, effective_month=month, employment_start=f"{month}-01"
    )
    pay_call(client, "POST", f"{PAY_API}/start", headers, {"month": month, "is_shadow": False}, status=201)
    store = approved_outlet(
        db,
        agent=agent,
        operator=operator,
        customer=grocery_store(db, "+998901238901", "Baraka"),
        onboarded_at=now - timedelta(days=30),
        pin=PIN,
    )
    return SimpleNamespace(month=month, water=water, juice=juice, store=store)


def set_status(client, headers, order, status: str, **extra) -> None:
    """The admin Orders page's status dropdown: PUT /api/v1/admin/orders/<id>/status."""
    pay_call(client, "PUT", ADMIN_STATUS.format(order.id), headers, {"status": status, **extra})


def placed(world, bottles: int):
    """An agent order of `bottles` x 19 L at the world's shop, placed today and confirmed the way
    an operator confirms it; nothing delivered, nothing paid."""
    order = make_agent_order(world.db, world.agent, world.store, [(world.products.water, bottles)],
                             delivered_at=None, paid_at=None)
    if order.status == OrderStatus.PENDING:  # the shop's own confirmation question is still open
        set_status(world.client, world.headers, order, "confirmed")
    database.session.refresh(order)
    assert order.status == OrderStatus.CONFIRMED
    return order


def park_delivery(order, delivery_status: DeliveryStatus) -> None:
    """Leave the order's delivery `failed` (waiting for a new date, commit 6062f02) or `rescheduled`.

    Declared test write: the row is written the way
    `test_forward_move_guard_awaiting_new_date._order` and `test_admin_order_reschedule._seed` write
    these shapes. No pay rule reads a delivery row; the pipeline lists the order because its ORDER
    status is live, and that is what this proves.
    """
    delivery = Delivery.query.filter_by(order_id=order.id).one_or_none()
    if delivery is None:
        delivery = Delivery(order_id=order.id, scheduled_date=datetime.now(timezone.utc), scheduled_time_slot="anytime")
        database.session.add(delivery)
    delivery.status = delivery_status
    delivery.delivery_person_id = None
    if delivery_status == DeliveryStatus.FAILED:
        delivery.failed_delivery_reason = "customer_unavailable"
        delivery.delivery_attempts = 1
    database.session.commit()


def sync_spy(monkeypatch):
    """Record each `SalesPayLedgerService.sync` call and run the real one. The call is bound
    against the real signature first, so a call the service would refuse raises here."""
    real = SalesPayLedgerService.sync
    signature = inspect.signature(real)
    calls = []

    def spy(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        calls.append(list(bound.arguments["agent_user_ids"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(SalesPayLedgerService, "sync", spy)
    return calls


@pytest.fixture
def world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    """Task 7's `pay_world` for the conftest agent: pay from October 2026, 19 L at 1,500 per unit,
    base 3,000,000, the shop "Oasis market". The clock stands at 09:00 on 1 October."""
    return pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)


# ---------------------------------------------------------------------------- the S1 shape


def test_before_pay_starts_the_agent_is_told_so_and_shown_nothing(
    client, db, monkeypatch, sales_agent_user, sales_agent_auth_headers
):
    """§5.4: `state` is "not_started", every other field empty or null. S2 has no month to show."""
    freeze_local_now(monkeypatch, local_at(2026, 9, 20, 11))

    assert my_earnings(client, sales_agent_auth_headers, sales_agent_user) == {
        "state": "not_started",
        "as_of": "2026-09-20T06:00:00+00:00",
        "open_months": [],
        "in_review": [],
        "last_statement": None,
        "pipeline": None,
        "penalties": [],
    }
    assert my_lines(client, sales_agent_auth_headers, "2026-09") == {
        "month": "2026-09", "status": None, "available": False, "page": 1, "has_more": False, "items": [],
    }


def test_the_open_month_publishes_every_figure_the_bot_prints(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """S1 on Thursday 15 October 2026, 10:00, exactly (§5.4's shape).

    Illustration B's calendar (Task 8's `october_world`): base 3,000,000, holiday 1 October,
    unpaid 14 and 15 October, so 30 working days and 28 worked: 3,000,000 x 28 / 30 =
    2,800,000 (the whole month's base in an open month, §4.8). 19 L pays 1,000 a
    unit: 100 + 50 bottles = 150,000 over 2 orders; no day plans, so the gate is `below_min_due`
    at x1.0, provisional. An admin adjustment of +50,000 and a penalty of 150,000 (the type's
    default). Variable 150,000 + 50,000 - 150,000 = 50,000; gross 2,800,000 + 50,000 = 2,850,000,
    paid in full.
    """
    agent = sales_agent_user
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, agent)
    water_order(world, 100, date(2026, 10, 5))
    water_order(world, 50, date(2026, 10, 6))
    move_clock(world, local_at(2026, 10, 15, 10))
    pay_route(world, "POST", f"/periods/2026-10/agents/{agent.id}/adjustments",
              {"amount": 50000, "reason": "August correction"}, status=201)
    penalty = pay_route(world, "POST", "/penalties", incident_body(agent, world.penalty_type_id, "2026-10-14"),
                        status=201)["penalty"]

    assert my_earnings(client, sales_agent_auth_headers, agent) == {
        "state": "ok",
        "as_of": "2026-10-15T05:00:00+00:00",
        "open_months": [
            {
                "month": "2026-10", "status": "open", "is_shadow": False, "configured": True,
                "start_date": "2026-10-01", "end_date": "2026-10-31", "gate_window_end": "2026-10-15",
                "estimate": {
                    "base": {"monthly": 3000000.0, "amount": 2800000.0, "worked_days": 28, "working_days": 30},
                    "commission": {
                        "gross": 150000.0, "orders": 2, "after_gate": 150000.0,
                        "products": [{
                            "product_id": world.products.water.id, "product_name": WATER_NAME,
                            "uses_default_tiers": False, "units": 150, "total": 150000.0,
                            "tiers": [{"from_unit": 1, "to_unit": None, "units": 150, "mode": "per_unit",
                                       "value": 1000.0, "net": 3000000.0, "amount_full": 150000.0,
                                       "amount": 150000.0}],
                            "next_tier": None,
                        }],
                    },
                    "late": {"after_gate": 0.0, "lines": 0, "groups": []},
                    "gate": {"compliance_pct": None, "visits_due": 0, "visits_counted": 0, "multiplier": 1.0,
                             "min_due": 20, "rule": "below_min_due", "provisional": True},
                    "new_outlets": {"amount": 0.0, "count": 0},
                    "adjustments": {"amount": 50000.0, "items": [{"amount": 50000.0, "reason": "August correction"}]},
                    "penalties": {"amount": 150000.0, "count": 1},
                    "variable": 50000.0,
                    "carry_in": NOTHING_CARRIED,
                    "gross_total": 2850000.0, "total": 2850000.0, "carry_out": 0.0, "owed": 0.0,
                },
            }
        ],
        "in_review": [],
        "last_statement": None,
        "pipeline": {"count": 0, "estimated_total": 0.0, "items": []},
        "penalties": [
            {"id": penalty["id"], "incident_date": "2026-10-14", "type_names": PENALTY_TYPE_NAMES,
             "reason": "Did not visit 5 planned outlets", "amount": 150000.0, "status": "confirmed",
             "posting_month": "2026-10", "is_late": False},
        ],
    }


def test_confirmed_and_cancelled_penalties_are_listed_newest_first_and_never_their_evidence(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user,
    sales_agent_auth_headers,
):
    """T-PEN-2, S1 half. A manager proposes P1 (13 October) and an admin confirms it (A24): it counts
    and S1 lists it. P2 (14 October) is proposed and rejected (A25): never listed. P3 (12 October)
    is booked directly (A23) and then cancelled (A26): listed as cancelled, not counted. Newest
    incident first; the reason is shown, the evidence never (the rows carry no such key)."""
    agent = sales_agent_user
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, agent)

    def propose(day):
        return pay_call(client, "POST", PROPOSALS_URL, manager_claim_headers,
                        incident_body(agent, world.penalty_type_id, day), status=201)["proposal"]["id"]

    p1 = propose("2026-10-13")
    pay_route(world, "POST", f"/penalties/{p1}/confirm", {})
    p2 = propose("2026-10-14")
    pay_route(world, "POST", f"/penalties/{p2}/reject", {"note": "The route sheet shows every visit"})
    p3 = pay_route(world, "POST", "/penalties", incident_body(agent, world.penalty_type_id, "2026-10-12"),
                   status=201)["penalty"]["id"]
    pay_route(world, "POST", f"/penalties/{p3}/cancel", {"note": "Booked against the wrong agent"})

    earnings = my_earnings(client, sales_agent_auth_headers, agent)

    assert earnings["open_months"][0]["estimate"]["penalties"] == {"amount": 150000.0, "count": 1}
    row = {"type_names": PENALTY_TYPE_NAMES, "reason": "Did not visit 5 planned outlets", "amount": 150000.0,
           "posting_month": "2026-10", "is_late": False}
    assert earnings["penalties"] == [
        {"id": p1, "incident_date": "2026-10-13", "status": "confirmed", **row},
        {"id": p3, "incident_date": "2026-10-12", "status": "cancelled", **row},
    ]


# ---------------------------------------------------------------------------- one compliance figure


def test_the_pay_gate_publishes_the_one_compliance_figure(
    client, db, field_week, admin_claim_headers, sales_agent_auth_headers
):
    """T-VIS-1, pay-gate half: Task 2's week (every reason, exactly the threshold, one second under,
    a pin-less outlet) gives 11 due, 4 counted, 36.4 % on the metrics surfaces, and the same three
    numbers on S1's gate, whose window is the same 1-8 October."""
    agent = field_week.agent
    water = make_product(db, name="Water 19L", price="20000", size="19L")
    plan = pay_call(client, "POST", f"{PAY_API}/plans", admin_claim_headers,
                    {"name": "Standard", "version": version_payload("2026-10", water.id)}, status=201)["plan"]
    configure_agent(client, admin_claim_headers, agent, plan_id=plan["id"], base=3000000,
                    effective_month="2026-10", employment_start="2026-10-01")

    (month,) = my_earnings(client, sales_agent_auth_headers, agent)["open_months"]
    stats = pay_call(client, "GET", f"{STATS}?period=month", sales_agent_auth_headers)

    gate = month["estimate"]["gate"]
    assert month["gate_window_end"] == "2026-10-08"
    assert (gate["visits_due"], gate["visits_counted"], gate["compliance_pct"]) == (
        FIELD_WEEK_DUE,
        FIELD_WEEK_COUNTED,
        FIELD_WEEK_PCT,
    )
    assert (stats["metrics"]["planned_visits"], stats["metrics"]["plan_vs_fact_pct"]) == (
        FIELD_WEEK_DUE,
        FIELD_WEEK_PCT,
    )


# (due, counted, pct) on 8 October. The week: outlets A and B are due on 1-3 and 5-7 October; A is
# visited every due day, B on the 1st-3rd. Sunday the 4th is a working day (C2 v5) but has no plan
# row, so it leaves both halves (R47), except in the two Sunday kinds (T-DAYS-3, I-39): there A and
# B are due on the 4th too, and A is visited.
KINDS_OF_DAY = {
    "normal": (12, 9, 75.0),  # 9 / 12
    "unpaid_day_and_holiday": (8, 7, 87.5),  # the 5th unpaid, the 7th a holiday: 4 days, 7 / 8
    "short_visit": (12, 8, 66.7),  # A's visit on the 6th one second short: 8 / 12 = 66.67
    "outside_the_frozen_set": (12, 8, 66.7),  # B's visit on the 3rd made to C instead: 8 / 12
    "a_sunday": (14, 10, 71.4),  # the 4th measured like any day: 10 / 14 = 71.43
    "an_unpaid_sunday": (12, 9, 75.0),  # the 4th marked unpaid (A10) leaves both halves: 9 / 12
}
# T-DAYS-3: the 4th's `(day_status, counted)` on the admin plan-vs-fact row and in A8 `days`.
SUNDAY_ROW = {"a_sunday": ("worked", 1), "an_unpaid_sunday": ("unpaid", 0)}
PLAN_VS_FACT = "/api/v1/admin/sales/plan-vs-fact"


@pytest.mark.parametrize("kind", list(KINDS_OF_DAY))
def test_my_stats_and_the_pay_gate_agree_on_every_kind_of_day(
    kind, client, db, world, sales_agent_user, sales_agent_auth_headers
):
    """T-VIS-2 and §5.4 "One compliance figure": `/me/stats?period=month` and S1's gate are one
    `compliance()` call over one window, so they agree on a normal week, on one with an unpaid day
    and a holiday, with a short visit, with a visit to an outlet outside the frozen set, and
    (T-DAYS-3, I-39) on a week whose Sunday is measured or marked unpaid. In the Sunday kinds the
    admin plan-vs-fact row and A8's day for the 4th say the same thing."""
    agent = sales_agent_user
    a, b, c = (make_outlet(db, onboarded_by=agent, created_at=utc_at(2026, 8, 1), pin=PIN, assigned_agent=agent)
               for _ in range(3))
    planned = [date(2026, 10, day) for day in (1, 2, 3, 5, 6, 7)]
    sunday = date(2026, 10, 4)
    plan_days(db, agent, planned, [a.id, b.id])
    if kind in SUNDAY_ROW:
        plan_days(db, agent, [sunday], [a.id, b.id])
        verified_visits(db, agent, [sunday], [a.id])
    sixth = date(2026, 10, 6)
    verified_visits(db, agent, [day for day in planned if kind != "short_visit" or day != sixth], [a.id])
    if kind == "short_visit":
        verified_visits(db, agent, [sixth], [a.id], seconds=short_visit_threshold() - 1)
    if kind == "outside_the_frozen_set":
        verified_visits(db, agent, planned[:2], [b.id])
        verified_visits(db, agent, [planned[2]], [c.id])
    else:
        verified_visits(db, agent, planned[:3], [b.id])
    if kind == "unpaid_day_and_holiday":
        pay_route(world, "PUT", f"/periods/2026-10/agents/{agent.id}/unpaid-days",
                  {"days": [{"date": "2026-10-05", "note": "Sick"}]})
        pay_route(world, "PUT", "/periods/2026-10/holidays", {"days": [{"date": "2026-10-07", "note": "Company day off"}]})
    if kind == "an_unpaid_sunday":
        pay_route(world, "PUT", f"/periods/2026-10/agents/{agent.id}/unpaid-days",
                  {"days": [{"date": "2026-10-04", "note": "Rest day"}]})
    move_clock(world, local_at(2026, 10, 8, 18))

    (month,) = my_earnings(client, sales_agent_auth_headers, agent)["open_months"]
    stats = pay_call(client, "GET", f"{STATS}?period=month", sales_agent_auth_headers)

    due, counted, pct = KINDS_OF_DAY[kind]
    gate = month["estimate"]["gate"]
    assert (gate["visits_due"], gate["visits_counted"], gate["compliance_pct"]) == (due, counted, pct)
    assert (stats["metrics"]["planned_visits"], stats["metrics"]["plan_vs_fact_pct"]) == (due, pct)
    if kind in SUNDAY_ROW:
        table = pay_call(
            client, "GET", f"{PLAN_VS_FACT}?start_date=2026-10-01&end_date=2026-10-08&agent_id={agent.id}", world.headers
        )
        (row,) = [row for row in table["rows"] if row["day"] == "2026-10-04"]
        (day,) = [
            day for day in pay_route(world, "GET", f"/periods/2026-10/agents/{agent.id}")["days"]
            if day["date"] == "2026-10-04"
        ]
        assert (row["day_status"], row["counted"]) == SUNDAY_ROW[kind]
        assert (day["day_status"], day["counted"]) == SUNDAY_ROW[kind]


# ---------------------------------------------------------------------------- the on-demand sync


def test_the_line_appears_without_a_nightly_run(
    client, db, admin_user, admin_claim_headers, operator_user, sales_agent_user, sales_agent_auth_headers
):
    """T-E2E-1, on the wall clock, through the routes each actor uses: the agent's visit loop checks
    in for real at the shop's pin (in radius, not skipped) and places a cash order; the admin marks
    it delivered and records the cash at the office; the agent opens the month's credited orders.
    No nightly run has happened: S2's own on-demand sync posts it.

    The DELIVERED writer completes the order's delivery, which needs a driver at the door, so the
    admin's "delivered" is Task 6's `deliver`: the builder driver's walk to the door, then the
    office's DELIVERED, now.

    2 x 19 L on one tier at 1,500 a unit = 3,000; one juice, net 25,050, on the default single 3 %
    tier = 751.50 → 752 (each (month, product, tier) rounds once, C8): the order's share is 3,752.
    """
    pay = wall_clock_pay(db, client, admin_claim_headers, sales_agent_user, operator_user)
    placed_order = place_visit_order(
        client,
        sales_agent_auth_headers,
        pay.store,
        [{"product_id": pay.water.id, "quantity": 2}, {"product_id": pay.juice.id, "quantity": 1}],
        "cash",
        checkin={"latitude": pay.store.latitude, "longitude": pay.store.longitude, "horizontal_accuracy": 10},
    )
    visit = placed_order["visit"]
    assert (visit["checkin_skipped"], visit["in_radius"]) == (False, True)  # checked in at the pin, not skipped
    assert visit["checkin_at"] is not None and visit["distance_m"] is not None
    order = db.session.get(Order, placed_order["order"]["id"])
    if order.status == OrderStatus.PENDING:  # the shop's own confirmation question is still open
        set_status(client, admin_claim_headers, order, "confirmed")
    deliver(db, order.id, at=datetime.now(timezone.utc), actor=admin_user)
    db.session.refresh(order)
    pay_call(client, "POST", COLLECTIONS, admin_claim_headers,
             {"customer_id": order.user_id, "amount": int(order.total_amount), "order_id": order.id,
              "source": "standalone_meeting", "notes": "Paid at the office"}, status=201)
    assert SalesPayLedgerLine.query.count() == 0  # nothing has synced since the money arrived

    lapse_throttle(sales_agent_user)  # A0's answer synced the agent a moment ago
    page = my_lines(client, sales_agent_auth_headers, pay.month)

    (line,) = page["items"]
    assert {key: line[key] for key in ("row", "id", "kind", "cause", "order_number", "outlet_name", "amount",
                                       "earned_month", "is_late", "counted", "tier_shift")} == {
        "row": "order", "id": order.id, "kind": "commission_credit", "cause": None,
        "order_number": order.order_number, "outlet_name": pay.store.name, "amount": 3752.0,
        "earned_month": pay.month, "is_late": False, "counted": True, "tier_shift": False,
    }
    assert [entry["units"] for entry in line["units"]] == [2, 1]
    assert line["date"].startswith(pay.month)
    (admin_row,) = pay_call(client, "GET", f"{PAY_API}/periods/{pay.month}/agents/{sales_agent_user.id}/lines",
                            admin_claim_headers)["items"]
    assert sorted((t["mode"], t["value"], p["net"], t["share"]) for p in admin_row["products"] for t in p["tiers"]) == [
        ("per_unit", 1500.0, 40000.0, 3000.0),
        ("percent", 3.0, 25050.0, 752.0),
    ]
    assert (admin_row["amount"], [(line_.amount, line_.kind) for line_ in ledger_lines(order)]) == (
        3752.0, [(None, "commission_credit")]
    )


def test_two_looks_inside_the_throttle_sync_once_and_a_redis_outage_still_answers(
    client, db, monkeypatch, world, sales_agent_user, sales_agent_auth_headers
):
    """T-SYNC-5, HTTP half: two S1 calls within SALES_PAY_ONDEMAND_SYNC_SECONDS run one sync, and
    `sales_pay:fresh:<id>` sits in this worker's Redis. With Redis refusing, S1 syncs anyway and
    answers."""
    water_order(world, 6, date(2026, 10, 5))
    move_clock(world, local_at(2026, 10, 12, 10))
    lapse_throttle(sales_agent_user)
    calls = sync_spy(monkeypatch)

    first = pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)
    second = pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)

    assert calls == [[sales_agent_user.id]]
    assert pay_ledger_service.redis_client.exists(RedisKeyspace.sales_pay_fresh(sales_agent_user.id)) == 1
    assert [look["open_months"][0]["estimate"]["commission"]["gross"] for look in (first, second)] == [9000.0, 9000.0]

    def redis_down(*args, **kwargs):
        raise redis.exceptions.ConnectionError("redis unreachable")

    monkeypatch.setattr(pay_ledger_service.redis_client, "set", redis_down)
    third = pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)

    assert calls == [[sales_agent_user.id], [sales_agent_user.id]]
    assert third["state"] == "ok"


def test_a_fix_between_two_looks_writes_its_difference_and_its_restoration(
    client, db, world, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """T-DIFF-8, S1 half (I-18: before the close, sync timing does not matter). Both agents' orders
    are 6 x 19 L = 120,000, credited 9,000. At 10:00 each order's cash is corrected to 90,000 and at
    10:10 put back to 120,000. The watched agent opens S1 at 10:05 and 10:15: -2,250
    `received_reduced`, then +2,250 `received_restored`, net 9,000. The control agent opens nothing
    in between: the sync at 10:15 sees 120,000 again and writes nothing."""
    control_agent = pay_agent(db, phone="+998901238701", first_name="Bobur")
    configure_agent(client, admin_claim_headers, control_agent, plan_id=world.plan["id"], base=3000000,
                    effective_month="2026-10", employment_start="2026-10-01")
    control_store = pay_store(db, control_agent, name="Navruz", onboarded=local_at(2026, 10, 1, 9))
    watched = water_order(world, 6, date(2026, 10, 5))
    control = water_order(world, 6, date(2026, 10, 5), agent=control_agent, store=control_store)
    move_clock(world, local_at(2026, 10, 12, 9))
    my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    SalesPayLedgerService.sync([control_agent.id], now=local_at(2026, 10, 12, 9))

    move_clock(world, local_at(2026, 10, 12, 10))
    watched_event = adjust_cash(world, live_cash_events(watched)[0].id, 90000)
    control_event = adjust_cash(world, live_cash_events(control)[0].id, 90000)
    move_clock(world, local_at(2026, 10, 12, 10, 5))
    my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    move_clock(world, local_at(2026, 10, 12, 10, 10))
    adjust_cash(world, watched_event, 120000)
    adjust_cash(world, control_event, 120000)
    move_clock(world, local_at(2026, 10, 12, 10, 15))
    last_look = my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    SalesPayLedgerService.sync([control_agent.id], now=local_at(2026, 10, 12, 10, 15))

    assert shape(watched) == [
        ("commission_credit", None, "120000.00"),
        ("commission_difference", "received_reduced", "90000.00"),
        ("commission_difference", "received_restored", "120000.00"),
    ]
    assert shape(control) == [("commission_credit", None, "120000.00")]
    assert last_look["open_months"][0]["estimate"]["commission"]["gross"] == 9000.0


# ---------------------------------------------------------------------------- the pipeline


def test_every_live_order_waits_in_the_pipeline_and_a_dead_one_does_not(
    client, db, world, sales_agent_user, sales_agent_auth_headers
):
    """T-PIPE-1. Confirmed (2 bottles), out for delivery (3), delivered unpaid (4), a failed delivery
    waiting for a new date (5), a rescheduled delivery (6): each is listed with what a credit would
    post if it were paid now, at 1,500 a bottle. The cancelled order (7) is dead and the credited
    one (8) already counts, so neither is listed; the month's commission is the credited 8 bottles
    alone (12,000), never the pipeline. Then 20 more confirmed orders: 25 wait, 20 are shown."""
    move_clock(world, local_at(2026, 10, 12, 9))
    confirmed = placed(world, 2)
    out = placed(world, 3)
    set_status(client, world.headers, out, "preparing")
    set_status(client, world.headers, out, "out_for_delivery")
    # The credited order is placed before the unpaid one: the cash writer settles a customer's
    # OLDEST delivered debt first, whatever order a collection names, so the other way round the
    # 8 bottles' money would pay for the 4 and leave the 8 waiting.
    water_order(world, 8, date(2026, 10, 12))
    unpaid = water_order(world, 4, date(2026, 10, 12), paid=False)
    failed = placed(world, 5)
    set_status(client, world.headers, failed, "preparing")
    set_status(client, world.headers, failed, "out_for_delivery")
    park_delivery(failed, DeliveryStatus.FAILED)
    rescheduled = placed(world, 6)
    park_delivery(rescheduled, DeliveryStatus.RESCHEDULED)
    cancelled = placed(world, 7)
    set_status(client, world.headers, cancelled, "cancelled", reason="The shop closed for renovation")
    move_clock(world, local_at(2026, 10, 12, 18))

    earnings = my_earnings(client, sales_agent_auth_headers, sales_agent_user)

    def item(order, waiting_for, estimate):
        return {"order_number": order.order_number, "outlet_name": world.store.name,
                "placed_on": local_windows.local_date(order.created_at).isoformat(), "waiting_for": waiting_for,
                "total": float(order.total_amount), "estimated_commission": estimate}

    assert earnings["pipeline"] == {
        "count": 5,
        "estimated_total": 30000.0,
        "items": [
            item(rescheduled, "delivery", 9000.0),
            item(failed, "delivery", 7500.0),
            item(unpaid, "payment", 6000.0),
            item(out, "delivery", 4500.0),
            item(confirmed, "delivery", 3000.0),
        ],
    }
    assert earnings["open_months"][0]["estimate"]["commission"]["gross"] == 12000.0

    more = [placed(world, 1) for _ in range(20)]
    pipeline = my_earnings(client, sales_agent_auth_headers, sales_agent_user)["pipeline"]

    assert (pipeline["count"], len(pipeline["items"]), pipeline["estimated_total"]) == (25, 20, 60000.0)
    assert [row["order_number"] for row in pipeline["items"]] == [order.order_number for order in reversed(more)]


def test_a_settled_correction_caps_the_estimate_and_a_settled_reversal_leaves_the_pipeline(
    client, db, world, sales_agent_user, sales_agent_auth_headers
):
    """T-PIPE-1, the settled floor (I-18, I-19). 6 x 19 L = 120,000, credited 9,000 in October. Its
    cash is corrected to 90,000 on 20 October (-2,250), and October closes: 90,000 is now the
    settled level. Corrected to 0 on 10 November, the order is reversed (-6,750, into open
    November) and waits for payment again: were it paid in full now, a re-credit would return
    it to its October place at min(120,000, 90,000), so its estimate is its share there:
    9,000 x 90,000 / 120,000 = 6,750, never the first 9,000 (I-36). Once November closes the
    reversal is settled and final: the order leaves the pipeline."""
    order = water_order(world, 6, date(2026, 10, 5))
    move_clock(world, local_at(2026, 10, 20, 10))
    my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    event = adjust_cash(world, live_cash_events(order)[0].id, 90000)
    my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    move_clock(world, local_at(2026, 11, 10, 10))
    adjust_cash(world, event, 0)

    november = my_earnings(client, sales_agent_auth_headers, sales_agent_user)

    assert shape(order) == [
        ("commission_credit", None, "120000.00"),
        ("commission_difference", "received_reduced", "90000.00"),
        ("commission_reversal", "nothing_received", None),
    ]
    (waiting,) = november["pipeline"]["items"]
    assert (waiting["order_number"], waiting["waiting_for"], waiting["estimated_commission"]) == (
        order.order_number, "payment", 6750.0,
    )
    assert november["pipeline"]["estimated_total"] == 6750.0

    move_clock(world, local_at(2026, 12, 2, 10))
    pay_route(world, "POST", "/periods/2026-11/close")

    assert my_earnings(client, sales_agent_auth_headers, sales_agent_user)["pipeline"] == {
        "count": 0, "estimated_total": 0.0, "items": [],
    }


def test_a_held_order_waits_for_approval_then_for_delivery_and_a_rejected_one_leaves(
    client, db, admin_claim_headers, operator_user, sales_agent_user, sales_agent_auth_headers
):
    """T-PIPE-2, on the wall clock (the same-day rule reads the real day). The second and third
    orders at one shop on one day wait for a manager (C14): `waiting_for` is "approval". Approved
    through OA2, the order is confirmed and waits for "delivery"; rejected through OA3, it is
    cancelled and leaves. Every held order is live, so each carries its estimate (1,500 a bottle)."""
    pay = wall_clock_pay(db, client, admin_claim_headers, sales_agent_user, operator_user)

    def place(bottles):
        data = place_visit_order(client, sales_agent_auth_headers, pay.store,
                                 [{"product_id": pay.water.id, "quantity": bottles}], "cash")
        return data["confirmation"]["state"], db.session.get(Order, data["order"]["id"])

    first_state, first = place(4)
    second_state, second = place(2)
    assert first_state != "awaiting_staff_approval" and second_state == "awaiting_staff_approval"

    def waiting():
        items = my_earnings(client, sales_agent_auth_headers, sales_agent_user)["pipeline"]["items"]
        return {row["order_number"]: (row["waiting_for"], row["estimated_commission"]) for row in items}

    assert waiting() == {first.order_number: ("delivery", 6000.0), second.order_number: ("approval", 3000.0)}

    pay_call(client, "POST", f"{APPROVALS}/{second.id}/approve", admin_claim_headers, {})
    assert waiting()[second.order_number] == ("delivery", 3000.0)

    third_state, third = place(1)
    assert third_state == "awaiting_staff_approval"
    assert waiting()[third.order_number] == ("approval", 1500.0)
    pay_call(client, "POST", f"{APPROVALS}/{third.id}/reject", admin_claim_headers,
             {"reason": "Duplicate of this morning's order"})
    assert third.order_number not in waiting()


def test_an_order_earned_before_pay_started_never_waits(client, db, world, sales_agent_user, sales_agent_auth_headers):
    """T10-R1 (§4.8, S-6: `order_is_earned` is the pipeline's rule). 5 x 19 L delivered and paid on
    25 September, before pay started in October, is inside the 120-day horizon, and the sync skips
    it for good (earned before the epoch). It is not waiting for anything: it stays out of the items,
    the count and the estimated total. The confirmed 2 bottles still wait: 2 x 1,500 = 3,000."""
    before = water_order(world, 5, date(2026, 9, 25))
    move_clock(world, local_at(2026, 10, 12, 9))
    confirmed = placed(world, 2)
    move_clock(world, local_at(2026, 10, 12, 18))

    pipeline = my_earnings(client, sales_agent_auth_headers, sales_agent_user)["pipeline"]

    assert (before.status, before.is_paid, shape(before)) == (OrderStatus.DELIVERED, True, [])
    assert (pipeline["count"], pipeline["estimated_total"]) == (1, 3000.0)
    assert [(row["order_number"], row["waiting_for"], row["estimated_commission"]) for row in pipeline["items"]] == [
        (confirmed.order_number, "delivery", 3000.0)
    ]


def test_a_delivered_business_account_order_never_waits_for_payment(app, client, db, world):
    """T10-R1, the business-account rail: a contract order is earned on delivery (`order_is_earned`),
    whatever the sync then does with it. Bekzod has no pay terms, so the sync skips his delivered
    6 x 19 L for want of them and never credits it; it is still not waiting for payment. His second
    contract order is not delivered yet, so it waits for delivery, with no estimate (no terms)."""
    bekzod = pay_agent(db, phone="+998901238703", first_name="Bekzod")
    shop = make_outlet(db, onboarded_by=bekzod, created_at=utc_at(2026, 9, 1),
                       user=make_store(db, name="Ofis", workplace=True))
    move_clock(world, local_at(2026, 10, 5, 8))
    delivered = make_agent_order(db, bekzod, shop, [(world.products.water, 6)], delivered_at=utc_at(2026, 10, 5, 10),
                                 paid_at=None, method="business_account")
    waiting = make_agent_order(db, bekzod, shop, [(world.products.water, 2)], delivered_at=None, paid_at=None,
                               method="business_account")
    move_clock(world, local_at(2026, 10, 12, 18))

    earnings = my_earnings(client, claim_headers(app, bekzod, "sales_agent"), bekzod)

    assert (delivered.status, shape(delivered)) == (OrderStatus.DELIVERED, [])
    assert [(month["month"], month["configured"]) for month in earnings["open_months"]] == [("2026-10", False)]
    assert (earnings["pipeline"]["count"], earnings["pipeline"]["estimated_total"]) == (1, 0.0)
    assert [(row["order_number"], row["waiting_for"], row["estimated_commission"])
            for row in earnings["pipeline"]["items"]] == [(waiting.order_number, "delivery", None)]


# ---------------------------------------------------------------------------- months and statements


def test_between_a_months_end_and_its_close_both_months_are_open(
    client, db, world, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """Review Focus 4, backend half. 6 x 19 L delivered and paid at 23:50 local on Saturday 31
    October is earned in October and is not late. On Sunday 1 November at 11:00, October is not
    closed yet: S1 lists November and October, newest first, each gate window inside its own
    month. A "Credited orders" button drawn then and tapped after the close gets
    `{available: false, items: []}`, and October moves to `in_review` with no numbers (I-14)."""
    water_order(world, 6, date(2026, 10, 31), at=(23, 50))
    move_clock(world, local_at(2026, 11, 1, 11))

    earnings = my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    before_close = my_lines(client, sales_agent_auth_headers, "2026-10")

    assert [(m["month"], m["start_date"], m["end_date"], m["gate_window_end"]) for m in earnings["open_months"]] == [
        ("2026-11", "2026-11-01", "2026-11-30", "2026-11-01"),
        ("2026-10", "2026-10-01", "2026-10-31", "2026-10-31"),
    ]
    november, october = (m["estimate"] for m in earnings["open_months"])
    assert without_products(october["commission"]) == {"gross": 9000.0, "orders": 1, "after_gate": 9000.0}
    assert [(p["units"], p["total"]) for p in october["commission"]["products"]] == [(6, 9000.0)]
    assert october["late"] == {"after_gate": 0.0, "lines": 0, "groups": []}
    assert november["commission"] == {"gross": 0.0, "orders": 0, "after_gate": 0.0, "products": []}
    assert earnings["in_review"] == []
    (line,) = before_close["items"]
    assert (before_close["status"], before_close["available"]) == ("open", True)
    assert (line["kind"], line["date"], line["earned_month"], line["is_late"], line["counted"], line["amount"]) == (
        "commission_credit", "2026-10-31", "2026-10", False, True, 9000.0,
    )

    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")

    assert my_lines(client, sales_agent_auth_headers, "2026-10") == {
        "month": "2026-10", "status": "closed", "available": False, "page": 1, "has_more": False, "items": [],
    }
    after_close = my_earnings(client, sales_agent_auth_headers, sales_agent_user)
    assert [m["month"] for m in after_close["open_months"]] == ["2026-11"]
    assert after_close["in_review"] == [{"month": "2026-10", "status": "closed"}]


def test_a_month_below_zero_shows_its_carry_and_the_next_month_deducts_it(
    app, client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-CARRY-1 and T-CARRY-2, S1 halves, on §1.2's carry case (Task 8's `carry_case`, 19 L at
    1,000 a unit). October: base 3,000,000 x 2 / 30 = 200,000; 30 bottles = 30,000 at x1.0; the
    September credit of 300,000 reversed late (-300,000 at September's x1.0); a 30,000 penalty.
    Variable -300,000, gross -100,000, paid 0, carried -100,000 (the bot's `carry_note`).
    September (30 working days, all worked: 3,000,000 + 300,000) is the last statement.

    After October's approval, November's estimate brings the -100,000 in as a `carry_forward`
    carry-in from October, labelled by source and month, never by the carry row's stored reason,
    and it is no admin adjustment. November has 30 working days, all worked: 3,000,000 - 100,000
    = 2,900,000."""
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    headers = claim_headers(app, world.agent, "sales_agent")

    october = my_earnings(client, headers, world.agent)

    (estimate,) = [m["estimate"] for m in october["open_months"]]
    assert {key: estimate[key] for key in ("variable", "gross_total", "total", "carry_out", "owed")} == {
        "variable": -300000.0, "gross_total": -100000.0, "total": 0.0, "carry_out": -100000.0, "owed": 0.0,
    }
    assert estimate["base"] == {"monthly": 3000000.0, "amount": 200000.0, "worked_days": 2, "working_days": 30}
    assert {key: estimate["late"][key] for key in ("after_gate", "lines")} == {"after_gate": -300000.0, "lines": 1}
    assert [(group["earned_month"], group["gross"], [p["change"] for p in group["products"]])
            for group in estimate["late"]["groups"]] == [("2026-09", -300000.0, [-300000.0])]
    assert october["last_statement"] == {
        "month": "2026-09", "status": "approved", "is_shadow": False, "paid_on": None, "is_latest": True,
        "base": 3000000.0, "gated_commission": 300000.0, "new_outlets": 0.0, "adjustments": 0.0, "penalties": 0.0,
        "carry_in": 0.0, "total": 3300000.0, "carry_out": 0.0, "owed": 0.0, "owed_netted_in": None,
    }

    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    pay_route(world, "POST", "/periods/2026-10/approve")
    november = my_earnings(client, headers, world.agent)

    (estimate,) = [m["estimate"] for m in november["open_months"]]
    assert estimate["carry_in"] == {"amount": -100000.0, "from_month": "2026-10", "source": "carry_forward"}
    assert estimate["adjustments"] == {"amount": 0.0, "items": []}
    assert (estimate["gross_total"], estimate["total"]) == (2900000.0, 2900000.0)
    assert {key: november["last_statement"][key] for key in ("month", "total", "carry_out", "owed", "owed_netted_in")} == {
        "month": "2026-10", "total": 0.0, "carry_out": -100000.0, "owed": 0.0, "owed_netted_in": None,
    }


def test_a_leavers_balance_is_shown_once_and_netted_by_the_next_statements(
    app, client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-1 and T-OWED-2 (a), S1 halves (I-26, I-28, Q15). The carry case's agent leaves on 31
    October, so October's -100,000 is owed, not carried. The balance is `last_statement.owed`, and
    `owed_netted_in` names the open month whose estimate nets it, else null; there is never an
    `owed_to_date` key (§5.4).

    Final-review M10 (owner decision 2026-10-05): S1 estimates an open month only for an agent
    the close would freeze a statement for (`eligible_agent_ids`: terms in force, and employment
    overlapping the month or activity in it). A leaver's month with no activity is omitted, so
    nothing promises a deduction that never happens, and the bot prints `owed_note`.

    - October approved: owed 100,000. November has no activity yet, so it is not listed and
      nothing nets the balance (null).
    - November: 50 bottles = 50,000 credited after the exit; base 0. November closed but not
      approved: December has no activity, so nothing nets and the key is null.
    - November approved: 0 + 50,000 - 100,000 = -50,000, so paid 0 and owed 50,000; December has
      no activity, so still null.
    - December closed and approved with no statement for the agent (an owed balance is not
      activity): November is still the last statement, and null.
    - January: 80 bottles = 80,000 make January a month with a statement to come, so its estimate
      nets the 50,000 ("2027-01"); approved: 80,000 - 50,000 = 30,000 paid, owed 0.
    """
    world = carry_case(client, admin_claim_headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    headers = claim_headers(app, world.agent, "sales_agent")

    def look():
        earnings = my_earnings(client, headers, world.agent)
        assert "owed_to_date" not in earnings
        return earnings

    def last(earnings, *keys):
        return {key: earnings["last_statement"][key] for key in ("month",) + keys}

    def carry_in(earnings):
        (month,) = earnings["open_months"]
        return month["month"], month["estimate"]["carry_in"]

    october = look()
    (estimate,) = [m["estimate"] for m in october["open_months"]]
    assert (estimate["total"], estimate["carry_out"], estimate["owed"]) == (0.0, 0.0, 100000.0)

    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    pay_route(world, "POST", "/periods/2026-10/approve")
    after_october = look()
    assert last(after_october, "total", "carry_out", "owed", "owed_netted_in") == {
        "month": "2026-10", "total": 0.0, "carry_out": 0.0, "owed": 100000.0, "owed_netted_in": None,
    }
    assert after_october["open_months"] == []

    water_order(world, 50, date(2026, 11, 10))
    move_clock(world, local_at(2026, 12, 2, 10))
    pay_route(world, "POST", "/periods/2026-11/close")
    november_closed = look()
    assert last(november_closed, "owed", "owed_netted_in") == {"month": "2026-10", "owed": 100000.0, "owed_netted_in": None}
    assert november_closed["in_review"] == [{"month": "2026-11", "status": "closed"}]
    assert november_closed["open_months"] == []

    pay_route(world, "POST", "/periods/2026-11/approve")
    after_november = look()
    assert last(after_november, "carry_in", "total", "owed", "owed_netted_in") == {
        "month": "2026-11", "carry_in": -100000.0, "total": 0.0, "owed": 50000.0, "owed_netted_in": None,
    }
    assert after_november["open_months"] == []

    move_clock(world, local_at(2027, 1, 2, 10))
    pay_route(world, "POST", "/periods/2026-12/close")
    pay_route(world, "POST", "/periods/2026-12/approve")
    after_december = look()
    assert last(after_december, "owed", "owed_netted_in") == {"month": "2026-11", "owed": 50000.0, "owed_netted_in": None}
    assert after_december["open_months"] == []

    water_order(world, 80, date(2027, 1, 11))
    # The sync's candidate lookback counts back from the frozen clock to the order's REAL
    # `created_at` (120 days), so February's close cannot see an order created on today's date:
    # the agent's own look during January syncs it, as any look within the month would.
    move_clock(world, local_at(2027, 1, 11, 18))
    january = look()
    assert last(january, "owed", "owed_netted_in") == {"month": "2026-11", "owed": 50000.0, "owed_netted_in": "2027-01"}
    assert carry_in(january) == ("2027-01", {"amount": -50000.0, "from_month": "2026-11", "source": "owed"})
    move_clock(world, local_at(2027, 2, 2, 10))
    pay_route(world, "POST", "/periods/2027-01/close")
    pay_route(world, "POST", "/periods/2027-01/approve")
    assert last(look(), "carry_in", "total", "owed", "owed_netted_in") == {
        "month": "2027-01", "carry_in": -50000.0, "total": 30000.0, "owed": 0.0, "owed_netted_in": None,
    }


# ---------------------------------------------------------------------------- S2 and query budgets


def test_s2_pages_by_ten_and_answers_a_month_it_cannot_parse_with_one_code(
    client, db, world, sales_agent_user, sales_agent_auth_headers
):
    """S2 pages by EARNINGS_PAGE_SIZE (10) inside `data` (the bot discards the envelope's `meta`),
    oldest line first. A month that is not "YYYY-MM", or no month at all, is SALES_PAY_MONTH_INVALID;
    a month with no period has nothing to show."""
    # Twelve days, one order each (the dates skip Sundays 4 and 11 from v4; any day would do now).
    orders = [water_order(world, 1, date(2026, 10, day)) for day in (1, 2, 3, 5, 6, 7, 8, 9, 10, 12, 13, 14)]
    move_clock(world, local_at(2026, 10, 15, 10))
    lapse_throttle(sales_agent_user)

    first = my_lines(client, sales_agent_auth_headers, "2026-10", page=1)
    second = my_lines(client, sales_agent_auth_headers, "2026-10", page=2)
    below_one = my_lines(client, sales_agent_auth_headers, "2026-10", page=0)

    assert (first["page"], first["has_more"], len(first["items"])) == (1, True, 10)
    assert (second["page"], second["has_more"], len(second["items"])) == (2, False, 2)
    assert [row["order_number"] for row in first["items"] + second["items"]] == [o.order_number for o in orders]
    assert below_one["page"] == 1 and below_one["items"] == first["items"]
    for query in ("?month=2026-13", "?month=2026-1", "?month=10.2026", "?page=1"):
        refusal(client, "GET", f"{LINES}{query}", sales_agent_auth_headers, status=400, code="SALES_PAY_MONTH_INVALID")
    assert my_lines(client, sales_agent_auth_headers, "2026-08") == {
        "month": "2026-08", "status": None, "available": False, "page": 1, "has_more": False, "items": [],
    }


def test_s1_and_s2_cost_the_same_queries_whatever_the_number_of_lines(
    client, db, world, count_queries, sales_agent_user, sales_agent_auth_headers
):
    """§10.1 query budget. Each route is measured on its second call, inside the throttle window,
    so the sync is not in the count: first with 2 credited orders and 1 waiting, then with 12 and
    5. The count must not move."""

    def measure():
        lapse_throttle(sales_agent_user)
        pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)  # this one syncs
        with count_queries() as s1:
            pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)
        with count_queries() as s2:
            pay_call(client, "GET", f"{LINES}?month=2026-10&page=1", sales_agent_auth_headers)
        return s1.count, s2.count

    def waiting(day):
        # Each unpaid order is for a shop of its own: a paid order's collection settles the
        # customer's OLDEST delivered debt first, and the COD cap takes cash off a shop that
        # holds two open debts.
        shop = pay_store(db, sales_agent_user, name=f"Shop {day}", onboarded=local_at(2026, 10, 1, 9))
        water_order(world, 3, date(2026, 10, day), paid=False, store=shop)

    for day in (1, 2):
        water_order(world, 2, date(2026, 10, day))
    waiting(3)
    move_clock(world, local_at(2026, 10, 21, 10))
    few = measure()

    for day in (5, 6, 7, 8, 9, 10, 12, 13, 14, 15):
        water_order(world, 2, date(2026, 10, day))
    for day in (16, 17, 19, 20):
        waiting(day)
    move_clock(world, local_at(2026, 10, 21, 10))
    many = measure()

    assert many == few
