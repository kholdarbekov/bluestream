"""Golden test: Aziz, October 2026 (compensation spec §10.2; Example A of §1.2).

All configuration goes through the admin pay routes with an admin claim, and the clocks are frozen
by `move_clock`. Orders, day plans and visits come from the builders, which call the real
writers; the stores are grocery-store accounts or tier-less shops, so no order carries a tier
discount. The steps run in time order:

| When (local)       | Step | What                                                               |
|--------------------|------|--------------------------------------------------------------------|
| 01.09 09:00        | 1    | penalty type `late_report` (100,000); plan "Standard" v1 from 2026-09 (19 L 1,500 a unit, 3 %, default bands, 20 due, bonus 150,000 / 60 d / 2 orders >= 300,000 or 5 orders / 180 d); employment from 01.09; terms 5,000,000; start 2026-09; Bobur configured from 01.10 (3,000,000) |
| 29.09 / 30.09 22:00| 2    | S1 = 6 x 19 L = 120,000, delivered and paid 29.09; A4 September    |
| 02.10 10:00        | 3    | A3 close and A5 approve September; A2 October has 31 working days  |
| 05.10 10:00        | 4    | S1's cash corrected to 0; A4 October: a late -9,000 reversal; later orders go to other shops |
| 07.10 / 09.10 / 11.10 | 10 | Malika proposes P1 (confirmed), P2 (rejected), P3 (left pending)  |
| 10.10              | 5    | unpaid 12.10 and 13.10                                             |
| October            | 6    | 18 commission orders, 19 commission lines                          |
| October            | 7    | N1-N5, which must not count (N5 is Bobur's, a second configured agent) |
| -                  | 8    | day plans and visits: 37 of 50 = 74.0 %                            |
| 01.10 and after    | 9    | O1-O8, approved by an operator through the staff route             |
| 31.10 20:00        | 11   | S1 as Aziz: the estimate below; `/me/stats` agrees on 74.0 %       |
| 01.11 00:00:30     | 12   | N6 delivered and paid (19:00:30 UTC on the 31st)                   |
| 02.11 10:00        | 13   | A3 close October: A8 repeats step 11; N6 sits in November          |
| 03.11 / 05.11      | 14   | A5 approve (one push, total 5,666,419); A6 paid 05.11              |

The statement (steps 11 and 13):
- working days 31 (no holiday; every day is a working day, C2 v5), unpaid 2, worked 29; the four
  Sundays (4th, 11th, 18th, 25th) are worked with empty frozen due sets, so the gate stays 37 / 50;
  base 5,000,000 x 29 / 31 = 4,677,419.35 -> 4,677,419;
- commission: 7 lines of 60 x 19 L = 420 units x 1,500 = 630,000, and 12 lines of 20 x 25,000 =
  6,000,000 net at 3 % = 180,000: 810,000 over 18 orders (one order carries both lines, and the
  10,000 delivery fee of another is in no net figure);
- gate 37 / 50 = 74.0 % -> x0.8: 648,000; September's -9,000 at September's frozen x1.0 (no day
  plans, `below_min_due`): gated commission 639,000;
- bonuses O1, O2, O3: 450,000; adjustments 0; penalties P1: 100,000;
- variable 648,000 - 9,000 + 450,000 - 100,000 = 989,000; gross 4,677,419 + 989,000 = 5,666,419,
  paid in full; nothing carried in, out or owed.

Two declared test writes beyond §10.1's builder rule: the delivered-history backdate every
builder makes, and here each operator approval's `OutletStageHistory.created_at`, which the
column stamps with the wall clock. A8 files a window-less check (O4's prior-customer verdict) under
the month it was activated in, so the approval is dated where the scenario puts it (01.10 10:00).
"""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from business_app import db as database
from business_app.models.sales import OutletStageHistory
from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayPeriod, SalesPayPlanVersion, SalesPayStatement
from business_app.models.sales_visits import AgentOrderApproval
from business_app.services.cash_collection_service import CashCollectionService
from shared.enums import EntitySubtype, OrderStatus
from shared.staff_constants import SALES_EVENT_PAY_STATEMENT_APPROVED, SALES_PAY_COMMISSION_KINDS
from tests.integration.sales_pay_builders import (
    PAY_API,
    PROPOSALS_URL,
    add_penalty_type,
    adjust_cash,
    claim_headers,
    configure_agent,
    incident_body,
    ledger_lines,
    live_cash_events,
    local_at,
    make_agent_order,
    make_day_plan,
    make_order,
    make_outlet,
    make_visit,
    move_clock,
    pay_agent,
    pay_call,
    pay_route,
    pay_store,
    pay_world,
    pushes_of,
    spy_sales_pushes,
    staff_agent,
    statement_id,
    utc_at,
    water_order,
)
from tests.integration.test_sales_pay_agent_earnings_api import (
    EARNINGS,
    LINES,
    STATS,
    lapse_throttle,
    my_earnings,
    set_status,
)
from tests.integration.test_sales_pay_agent_statements_api import STATEMENTS
from tests.unit.test_outlet_dedupe import PIN, _customer

pytestmark = pytest.mark.integration

AZIZ_CHAT = "777000501"
LATE_REPORT = {"en": "Late report", "uz": "Hisobot kech topshirildi", "ru": "Отчёт сдан с опозданием"}
# The 25 Monday-to-Saturday days Aziz works (the 12th and the 13th are unpaid); the visit slices
# below index them. With the four SUNDAYS, worked days too (C2 v5), he works 29 of 31.
WORKED = [date(2026, 10, day) for day in (1, 2, 3, 5, 6, 7, 8, 9, 10, 14, 15, 16, 17, 19, 20, 21, 22, 23, 24, 26,
                                          27, 28, 29, 30, 31)]
UNPAID = [date(2026, 10, 12), date(2026, 10, 13)]
SUNDAYS = [date(2026, 10, day) for day in (4, 11, 18, 25)]
N6_AT = datetime(2026, 10, 31, 19, 0, 30, tzinfo=timezone.utc)  # 00:00:30 on 1 November, local


def bonus_pin(index: int):
    """Distinct pins about 0.8 km apart inside the delivery polygon, so no approval meets a
    neighbour."""
    return (41.2911 + 0.006 * index, 69.2497 + 0.006 * index)


def agent_order(world, lines, day, *, fee: int = 0, paid: bool = True, store=None):
    """An order Aziz placed at his own shop (`world.store`, or `store`), delivered at 10:00 on
    `day` and, unless `paid=False`, paid that minute. The clock is moved to 08:00 that day first."""
    move_clock(world, local_at(day.year, day.month, day.day, 8))
    at = utc_at(day.year, day.month, day.day, 10)
    return make_agent_order(world.db, world.agent, store or world.store, lines, delivered_at=at,
                            paid_at=at if paid else None, delivery_fee=Decimal(fee))


def shop_order(world, outlet, bottles: int, day: date, *, source: str = "telegram", staff=None, paid: bool = True):
    """An order for an onboarded outlet's account, from `source`, delivered at 10:00 on `day`."""
    move_clock(world, local_at(day.year, day.month, day.day, 8))
    at = utc_at(day.year, day.month, day.day, 10)
    return make_order(world.db, customer=outlet.user, lines=[(world.products.water, bottles)], source=source,
                      created_by_staff=staff, outlet=outlet, delivered_at=at, paid_at=at if paid else None)


def pay_later(world, order, day: date):
    """The shop pays at the counter on `day`, through the cash writer the builders use:
    `occurred_at` becomes `Order.paid_at` (I-2)."""
    move_clock(world, local_at(day.year, day.month, day.day, 10))
    CashCollectionService().post_collection(
        customer_id=order.user_id,
        amount=order.total_amount,
        source="standalone_meeting",
        recorded_by_user_id=world.admin.id,
        order_id=order.id,
        notes="Paid at the counter",
        occurred_at=utc_at(day.year, day.month, day.day, 10),
    )
    database.session.refresh(order)
    assert order.is_paid is True


def onboard(world, operator_headers, index: int, *, approved: bool = True):
    """Outlet O<index>: a grocery store Aziz onboarded on 1 October at 09:00, approved by the
    operator through `POST /staff/sales/outlets/<id>/approve` at 10:00. `approved=False` is O5, an
    imported outlet with no onboarder, already active."""
    customer = _customer(database, f"+99890123860{index}", company=f"O{index} market",
                         subtype=EntitySubtype.GROCERY_STORE)
    outlet = make_outlet(
        database,
        onboarded_by=world.agent if approved else None,
        created_at=utc_at(2026, 10, 1, 9),
        user=customer,
        pin=bonus_pin(index),
        stage="activation_requested" if approved else "active",
        assigned_agent=world.agent,
    )
    if approved:
        move_clock(world, local_at(2026, 10, 1, 10))
        pay_call(world.client, "POST", f"/api/v1/staff/sales/outlets/{outlet.id}/approve", operator_headers, {})
        # Declared test write (module docstring): date the approval where the scenario puts it.
        history = OutletStageHistory.query.filter_by(outlet_id=outlet.id, reason_code="approved").one()
        history.created_at = utc_at(2026, 10, 1, 10)
        database.session.commit()
        database.session.refresh(outlet)
        assert outlet.stage == "active" and outlet.address_id is not None
    return outlet


def visit(world, outlet, day: date, *, hour: int = 11, seconds: int = 120, **fields):
    """One visit on `day`, verified unless `fields` say otherwise: completed, checked in at the
    start, in range, `seconds` long."""
    started = utc_at(day.year, day.month, day.day, hour)
    make_visit(world.db, **{
        "outlet_id": outlet.id, "agent_user_id": world.agent.id, "status": "completed", "planned": True,
        "started_at": started, "checkin_at": started, "ended_at": started + timedelta(seconds=seconds),
        "in_radius": True, "distance_m": 12.0, "checkin_skipped": False, **fields,
    })


def statement_columns(agent, month: date) -> dict:
    """Every column of the agent's stored statement for `month`, re-read from the database."""
    database.session.expire_all()
    row = (SalesPayStatement.query.join(SalesPayPeriod, SalesPayStatement.period_id == SalesPayPeriod.id)
           .filter(SalesPayStatement.agent_user_id == agent.id, SalesPayPeriod.month_start == month).one())
    return {column.name: getattr(row, column.name) for column in SalesPayStatement.__table__.columns}


@pytest.fixture
def golden(app, db, client, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, operator_auth_headers):
    aziz = staff_agent(db, phone="+998901238501", first_name="Aziz", telegram_id=AZIZ_CHAT)
    aziz_headers = claim_headers(app, aziz, "sales_agent")
    g = SimpleNamespace(aziz=aziz, aziz_headers=aziz_headers)

    # Step 1, 01.09 09:00: plan, employment, terms and start (Task 7's `pay_world`), then the type.
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, aziz, start="2026-09",
                      rate_19l=1500, base=5000000)
    g.world = world
    late_report = add_penalty_type(world, default_amount=100000, names=LATE_REPORT)
    # Bobur (N5's agent) is a configured agent too: an active profile with no terms would make
    # October's close refuse with SALES_PAY_TERMS_MISSING (`agents_missing_terms`). He is
    # configured now, as an admin sets up a new hire: an unset employment counts as employed in
    # every month, so setting it once September is approved would rewrite that month, and A18
    # refuses that (SALES_PAY_MONTH_LOCKED).
    bobur = g.bobur = pay_agent(db, phone="+998901238701", first_name="Bobur")
    configure_agent(client, admin_claim_headers, bobur, plan_id=world.plan["id"], base=3000000,
                    effective_month="2026-10", employment_start="2026-10-01")

    # Step 2: S1, delivered and paid on 29.09; A4 on the open September syncs it.
    g.s1 = water_order(world, 6, date(2026, 9, 29))
    move_clock(world, local_at(2026, 9, 30, 22))
    pay_route(world, "POST", "/periods/2026-09/recalculate")
    g.september_lines = pay_route(world, "GET", f"/periods/2026-09/agents/{aziz.id}/lines")["items"]

    # Step 3, 02.10 10:00: close (its sync opens October) and approve September.
    move_clock(world, local_at(2026, 10, 2, 10))
    pay_route(world, "POST", "/periods/2026-09/close")
    pay_route(world, "POST", "/periods/2026-09/approve")
    g.october_calendar = pay_route(world, "GET", "/periods/2026-10")["calendar"]
    g.september_before = pay_route(world, "GET", f"/periods/2026-09/agents/{aziz.id}")
    g.september_row_before = statement_columns(aziz, date(2026, 9, 1))

    # Step 4, 05.10 10:00: S1's cash corrected to 0 by the super-admin; A4 on October.
    move_clock(world, local_at(2026, 10, 5, 10))
    adjust_cash(world, live_cash_events(g.s1)[0].id, 0)
    pay_route(world, "POST", "/periods/2026-10/recalculate")
    g.october_lines_step4 = pay_route(world, "GET", f"/periods/2026-10/agents/{aziz.id}/lines")["items"]
    g.september_after = pay_route(world, "GET", f"/periods/2026-09/agents/{aziz.id}")
    g.september_row_after = statement_columns(aziz, date(2026, 9, 1))
    # "Oasis market" owes S1's 120,000 again, and the cash writer settles a customer's OLDEST
    # delivered debt first, whatever order a collection names (Task 8's `carry_case` precedent).
    # Aziz's later orders are for a second shop of his, so none of them pays S1 back behind the
    # scenario's back; S1 waits for payment in step 11's pipeline.
    world.store = pay_store(db, aziz, name="Chorsu", onboarded=local_at(2026, 10, 5, 10))

    # Step 10 (07.10, 09.10, 11.10) and step 5 (10.10), in time order.
    def propose(day):
        move_clock(world, local_at(2026, 10, day, 10))
        body = incident_body(aziz, late_report, f"2026-10-{day:02d}")
        return pay_call(client, "POST", PROPOSALS_URL, manager_claim_headers, body, status=201)["proposal"]["id"]

    g.p1 = propose(7)
    pay_route(world, "POST", f"/penalties/{g.p1}/confirm", {})
    g.p2 = propose(9)
    pay_route(world, "POST", f"/penalties/{g.p2}/reject", {"note": "The report arrived on time"})
    move_clock(world, local_at(2026, 10, 10, 10))
    pay_route(world, "PUT", f"/periods/2026-10/agents/{aziz.id}/unpaid-days",
              {"days": [{"date": "2026-10-12", "note": "Family leave"}, {"date": "2026-10-13", "note": "Family leave"}]})
    g.p3 = propose(11)

    # Step 6: 18 commission orders. Six of 60 x 19 L, eleven of 20 juices at 25,000, and one with
    # both lines that also carries a 10,000 delivery fee.
    water, juice = world.products.water, world.products.juice
    for day in (14, 15, 16, 17, 19, 20):
        agent_order(world, [(water, 60)], date(2026, 10, day))
    g.mixed = agent_order(world, [(water, 60), (juice, 20)], date(2026, 10, 21), fee=10000)
    for day in (2, 3, 5, 22, 23, 24, 26, 27, 28, 29, 30):
        agent_order(world, [(juice, 20)], date(2026, 10, day))

    # Step 7: N1 confirmed, N2 delivered unpaid, N3 cancelled, N4 a phone order Aziz keyed in,
    # N5 another agent's.
    move_clock(world, local_at(2026, 10, 28, 9))
    g.n1 = make_agent_order(db, aziz, world.store, [(water, 2)], delivered_at=None, paid_at=None)
    if g.n1.status == OrderStatus.PENDING:
        set_status(client, admin_claim_headers, g.n1, "confirmed")
    # N2 stays unpaid, so it is for a shop where nothing is collected afterwards: N4, N5 and N6's
    # collections at "Chorsu" would settle it first (the oldest debt), and a second open debt there
    # would take cash off the shop's menu (the COD cap) before N3.
    g.n2 = agent_order(world, [(water, 4)], date(2026, 10, 28), paid=False,
                       store=pay_store(db, aziz, name="Navbahor", onboarded=local_at(2026, 10, 28, 9)))
    move_clock(world, local_at(2026, 10, 28, 11))
    n3 = make_agent_order(db, aziz, world.store, [(water, 3)], delivered_at=None, paid_at=None)
    set_status(client, admin_claim_headers, n3, "cancelled", reason="The shop ordered twice by mistake")
    shop_at = utc_at(2026, 10, 27, 10)
    move_clock(world, local_at(2026, 10, 27, 8))
    make_order(db, customer=world.store.user, lines=[(water, 5)], source="phone", created_by_staff=aziz,
               outlet=world.store, delivered_at=shop_at, paid_at=shop_at)
    make_agent_order(db, bobur, world.store, [(water, 5)], delivered_at=shop_at, paid_at=shop_at)

    # Step 8: day plans and visits. V1, V2 and V4 have pins; V3 has none.
    v1, v2, v3, v4 = (
        make_outlet(db, onboarded_by=aziz, created_at=utc_at(2026, 8, 1), pin=pin, assigned_agent=aziz)
        for pin in (PIN, PIN, None, PIN)
    )
    for index, day in enumerate(WORKED):
        make_day_plan(db, aziz, day, [v1.id, v3.id] if index == 21 else [v1.id, v2.id])
    for day in UNPAID:
        make_day_plan(db, aziz, day, [v1.id, v2.id])
    for day in SUNDAYS:  # worked, with an empty frozen set: measured, due 0 (§10.2 step 8)
        make_day_plan(db, aziz, day, [])
    for day in WORKED[:18]:  # 36 counted
        visit(world, v1, day, seconds=60 if day == WORKED[0] else 120)  # the first lasts exactly 60 s
        visit(world, v2, day)
    visit(world, v1, WORKED[1], hour=15)  # a second verified visit to an outlet already counted
    visit(world, v4, WORKED[2])  # verified and planned, but V4 is not in that day's frozen set
    visit(world, v1, WORKED[18])  # the 37th
    visit(world, v2, WORKED[18], checkin_skipped=True, in_radius=None, distance_m=None)  # skipped
    visit(world, v1, WORKED[19], in_radius=False, distance_m=850.0)  # out of range
    visit(world, v2, WORKED[19], seconds=59)  # one second short
    visit(world, v1, WORKED[20], checkin_at=None, in_radius=None, distance_m=None, current_step="checkin")
    visit(world, v2, WORKED[20], status="abandoned", current_step="stock", outcome=None, no_order_reason=None)
    visit(world, v3, WORKED[21], in_radius=None, distance_m=None)  # pin-less: no_location; V1 not visited
    # WORKED[22:25]: nothing visited. Excluded days: two verified visits on the unpaid days. Sunday the
    # 4th is measured with an empty frozen set, so V1's verified visit there is outside it (I-6).
    visit(world, v1, UNPAID[0])
    visit(world, v2, UNPAID[1])
    visit(world, v1, SUNDAYS[0])

    # Step 9: O1-O8 (the fixture has no O7).
    g.outlets = {index: onboard(world, operator_auth_headers, index, approved=index != 5) for index in (1, 2, 3, 4, 5, 6, 8)}
    o = g.outlets
    g.first_delivery = {}
    o1_first = shop_order(world, o[1], 11, date(2026, 10, 6))  # 220,000
    o1_second = shop_order(world, o[1], 5, date(2026, 10, 15), paid=False)  # 100,000
    pay_later(world, o1_second, date(2026, 10, 20))
    g.first_delivery[1] = utc_at(2026, 10, 6, 10)
    shop_order(world, o[2], 8, date(2026, 10, 7), source="admin", staff=admin_user)  # 160,000
    shop_order(world, o[2], 8, date(2026, 10, 8))  # 160,000
    g.first_delivery[2] = utc_at(2026, 10, 7, 10)
    for day in (5, 6, 7, 8, 9):  # five orders of 20,000 on five days
        shop_order(world, o[3], 1, date(2026, 10, day))
    g.first_delivery[3] = utc_at(2026, 10, 5, 10)
    shop_order(world, o[4], 5, date(2026, 6, 23))  # 100 days before onboarding: a prior customer
    shop_order(world, o[6], 10, date(2026, 10, 10))  # one order only
    g.first_delivery[6] = utc_at(2026, 10, 10, 10)
    shop_order(world, o[8], 10, date(2026, 10, 9))
    shop_order(world, o[8], 10, date(2026, 10, 12), paid=False)  # the second is delivered, never paid
    g.first_delivery[8] = utc_at(2026, 10, 9, 10)
    assert (o1_first.total_amount, o1_second.total_amount) == (Decimal("220000.00"), Decimal("100000.00"))

    # Step 11, 31.10 20:00: the agent's own screen, and My stats for the month. Twenty days of
    # real time have passed since the last look, so the throttle has long lapsed.
    move_clock(world, local_at(2026, 10, 31, 20))
    g.earnings = my_earnings(client, aziz_headers, aziz)
    g.stats = pay_call(client, "GET", f"{STATS}?period=month", aziz_headers)

    # Step 12: N6, placed at 20:00 on the 31st and delivered and paid 30 seconds into 1 November, local.
    g.n6 = make_agent_order(db, aziz, world.store, [(water, 10)], delivered_at=N6_AT, paid_at=N6_AT)

    # Step 13, 02.11 10:00: close October (it syncs first).
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    g.statement = pay_route(world, "GET", f"/periods/2026-10/agents/{aziz.id}")
    g.october_detail = pay_route(world, "GET", "/periods/2026-10")
    g.october_lines = pay_route(world, "GET", f"/periods/2026-10/agents/{aziz.id}/lines?per_page=50")["items"]
    g.november_lines = pay_route(world, "GET", f"/periods/2026-11/agents/{aziz.id}/lines")["items"]
    pay_call(client, "GET", f"{PAY_API}/periods/2026-10/agents/{aziz.id}", manager_claim_headers, status=403)
    g.row = statement_columns(aziz, date(2026, 10, 1))
    g.v1_id = SalesPayPlanVersion.query.filter_by(plan_id=world.plan["id"]).one().id

    # Step 14, 03.11 10:00: approve October with the pushes watched; 05.11: paid.
    move_clock(world, local_at(2026, 11, 3, 10))
    g.pushes = spy_sales_pushes(monkeypatch)
    pay_route(world, "POST", "/periods/2026-10/approve")
    g.statement_id = statement_id(world, "2026-10", aziz)
    move_clock(world, local_at(2026, 11, 5, 10))
    pay_route(world, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-11-05"})
    g.october_paid = pay_route(world, "GET", "/periods/2026-10")
    g.earnings_after = my_earnings(client, aziz_headers, aziz)
    return g


EXAMPLE_A = {
    "base": {"monthly": 5000000.0, "amount": 4677419.0, "worked_days": 29, "working_days": 31},
    "commission": {"gross": 810000.0, "orders": 18, "after_gate": 648000.0},
    "late": {"after_gate": -9000.0, "lines": 1},
    "gate": {"compliance_pct": 74.0, "visits_due": 50, "visits_counted": 37, "multiplier": 0.8, "min_due": 20,
             "rule": "band", "provisional": True},
    "new_outlets": {"amount": 450000.0, "count": 3},
    "adjustments": {"amount": 0.0, "items": []},
    "penalties": {"amount": 100000.0, "count": 1},
    "variable": 989000.0,
    "carry_in": {"amount": 0.0, "from_month": None, "source": None},
    "gross_total": 5666419.0,
    "total": 5666419.0,
    "carry_out": 0.0,
    "owed": 0.0,
}


def without(mapping: dict, key: str) -> dict:
    return {name: value for name, value in mapping.items() if name != key}


def golden_products(g) -> list:
    """§10.2's tier asserts: 19 L, 420 bottles (7 lines of 60 at 20,000 = 8,400,000 net) in one tier
    at 1,500 = 630,000; the juice on the default single 3 % tier, 240 units (12 lines of 20 at
    25,000 = 6,000,000 net) = 180,000. Single tiers have no next tier. Names are the frozen ones."""
    water, juice = g.world.products.water.id, g.world.products.juice.id
    names = {item["product_id"]: item["product_name"] for item in ledger_lines(g.mixed)[0].snapshot["items"]}
    return [
        {"product_id": water, "product_name": names[water], "uses_default_tiers": False, "units": 420,
         "total": 630000.0,
         "tiers": [{"from_unit": 1, "to_unit": None, "units": 420, "mode": "per_unit", "value": 1500.0,
                    "net": 8400000.0, "amount_full": 630000.0, "amount": 630000.0}],
         "next_tier": None},
        {"product_id": juice, "product_name": names[juice], "uses_default_tiers": True, "units": 240,
         "total": 180000.0,
         "tiers": [{"from_unit": 1, "to_unit": None, "units": 240, "mode": "percent", "value": 3.0,
                    "net": 6000000.0, "amount_full": 180000.0, "amount": 180000.0}],
         "next_tier": None},
    ]


def golden_late(g) -> list:
    """September's late group in October (step 4): S1's 6 bottles (9,000 at v1) leave the count."""
    water = g.world.products.water.id
    (item,) = ledger_lines(g.s1)[0].snapshot["items"]
    return [
        {"earned_month": "2026-09", "plan_version_id": g.v1_id, "counted": True, "gross": -9000.0,
         "multiplier": 1.0, "source": "statement", "after_gate": -9000.0,
         "products": [
             {"product_id": water, "product_name": item["product_name"], "units_before": 6, "units": 0,
              "total_before": 9000.0, "total": 0.0, "change": -9000.0,
              "tiers_before": [{"from_unit": 1, "to_unit": None, "units": 6, "mode": "per_unit", "value": 1500.0,
                                "net": 120000.0, "amount_full": 9000.0, "amount": 9000.0}],
              "tiers": []},
         ]},
    ]


def test_aziz_is_paid_5_666_419_for_october_2026(golden):
    g = golden
    aziz = g.aziz

    # Step 2: September's line.
    assert [(row["row"], [e["kind"] for e in row["events"]], row["amount"]) for row in g.september_lines] == [
        ("order", ["commission_credit"], 9000.0)
    ]
    assert [(p["units"], p["total"]) for p in g.september_before["summary"]["commission"]["products"]] == [(6, 9000.0)]

    # Step 3: October's calendar.
    assert g.october_calendar["working_days"] == 31

    # Step 4: the late reversal, and September untouched.
    (late_row,) = [row for row in g.october_lines_step4 if row["row"] == "order" and row["is_late"]]
    assert {key: late_row[key] for key in ("order_id", "earned_month", "is_late", "share_before", "share", "amount")} == {
        "order_id": g.s1.id, "earned_month": "2026-09", "is_late": True, "share_before": 9000.0, "share": 0.0,
        "amount": -9000.0,
    }
    assert [(e["kind"], e["cause"]) for e in late_row["events"]] == [("commission_reversal", "nothing_received")]
    assert g.september_after == g.september_before
    assert g.september_row_after == g.september_row_before

    # Step 11: the estimate is Example A to the unit, and the pipeline is never in it.
    (october,) = g.earnings["open_months"]
    assert {key: october[key] for key in ("month", "status", "is_shadow", "configured", "start_date", "end_date",
                                          "gate_window_end")} == {
        "month": "2026-10", "status": "open", "is_shadow": False, "configured": True, "start_date": "2026-10-01",
        "end_date": "2026-10-31", "gate_window_end": "2026-10-31",
    }
    estimate = october["estimate"]
    assert {**estimate, "commission": without(estimate["commission"], "products"),
            "late": without(estimate["late"], "groups")} == EXAMPLE_A
    assert estimate["commission"]["products"] == golden_products(g)
    assert estimate["late"]["groups"] == golden_late(g)
    assert g.earnings["as_of"] == "2026-10-31T15:00:00+00:00"
    assert g.earnings["in_review"] == []
    assert {key: g.earnings["last_statement"][key] for key in ("month", "status", "total", "owed", "owed_netted_in")} == {
        "month": "2026-09", "status": "approved", "total": 5009000.0, "owed": 0.0, "owed_netted_in": None,
    }
    pipeline = g.earnings["pipeline"]
    assert (pipeline["count"], pipeline["estimated_total"]) == (3, 18000.0)
    assert [(row["order_number"], row["waiting_for"], row["estimated_commission"]) for row in pipeline["items"]] == [
        (g.n2.order_number, "payment", 6000.0),
        (g.n1.order_number, "delivery", 3000.0),
        (g.s1.order_number, "payment", 9000.0),
    ]
    assert [(row["id"], row["status"], row["amount"]) for row in g.earnings["penalties"]] == [(g.p1, "confirmed", 100000.0)]
    assert g.stats["metrics"]["plan_vs_fact_pct"] == 74.0 == october["estimate"]["gate"]["compliance_pct"]
    assert g.stats["metrics"]["planned_visits"] == 50

    # Step 13: the frozen statement repeats the estimate.
    summary = g.statement["summary"]
    estimate = october["estimate"]
    assert type(summary["total"]) is float and summary["total"] == 5666419
    assert {
        "base": summary["base_amount"], "commission": summary["commission"], "new_outlets": summary["new_outlets"],
        "adjustments": summary["adjustments"], "penalties": summary["penalties"], "variable": summary["variable"],
        "carry_in": summary["carry_in"], "gross_total": summary["gross_total"], "total": summary["total"],
        "carry_out": summary["carry_out"], "owed": summary["owed"],
    } == {
        "base": estimate["base"]["amount"], "commission": estimate["commission"], "new_outlets": estimate["new_outlets"],
        "adjustments": estimate["adjustments"]["amount"], "penalties": estimate["penalties"]["amount"],
        "variable": estimate["variable"], "carry_in": estimate["carry_in"], "gross_total": estimate["gross_total"],
        "total": estimate["total"], "carry_out": estimate["carry_out"], "owed": estimate["owed"],
    }
    assert summary["gated_commission"] == 639000.0
    assert summary["late"] == golden_late(g)
    assert summary["gate"] == {"compliance_pct": 74.0, "visits_due": 50, "visits_counted": 37, "multiplier": 0.8,
                               "rule": "band", "band": {"min_pct": 60.0, "multiplier": 0.8}, "provisional": False}
    assert (g.statement["self_decided"], g.statement["self_decisions"]) == (False, [])
    assert {row["agent_user_id"]: row["self_decided"] for row in g.october_detail["agents"]} == {
        aziz.id: False, g.bobur.id: False,
    }

    # The stored statement: plan v1 and the §4.8 inputs.
    inputs = g.row["inputs"]
    assert g.row["plan_version_id"] == g.v1_id
    assert (g.row["total_amount"], g.row["gross_amount"], g.row["base_amount"]) == (
        Decimal("5666419"), Decimal("5666419"), Decimal("4677419"),
    )
    assert len(inputs["calendar"]["working_dates"]) == 31
    assert inputs["calendar"]["unpaid_dates"] == ["2026-10-12", "2026-10-13"]
    assert inputs["calendar"]["holidays"] == []
    assert (inputs["gate"]["short_visit_seconds"], inputs["gate"]["geofence_radius_m"]) == (60, 250)
    assert {group["earned_month"]: (Decimal(group["gross"]), Decimal(group["multiplier"]), group["source"], group["gated"])
            for group in inputs["groups"]} == {
        "2026-10": (Decimal("810000"), Decimal("0.8"), "current", 648000),
        "2026-09": (Decimal("-9000"), Decimal("1"), "statement", -9000),
    }
    assert inputs["calculator_version"] == 2
    assert [(group["earned_month"], group["plan_version_id"], group["counted"], len(group["orders"]))
            for group in inputs["groups"]] == [("2026-09", g.v1_id, True, 1), ("2026-10", g.v1_id, True, 18)]
    days = inputs["gate"]["days"]
    assert len(days) == 31 == len(g.statement["days"])
    assert Counter(day["day_status"] for day in days) == {"worked": 29, "unpaid": 2}
    (sunday,) = [day for day in days if day["day"] == "2026-10-04"]
    assert (sunday["day_status"], sunday["due"], sunday["counted"]) == ("worked", 0, 0)
    assert sum(day["due"] or 0 for day in days if day["day_status"] == "worked") == 50
    assert sum(day["counted"] for day in days) == 37
    not_counted = Counter()
    for day in days:
        not_counted.update(day["not_counted"])
    assert not_counted == {"skipped": 1, "out_of_range": 1, "short": 1, "no_checkin": 1, "not_completed": 1,
                           "no_location": 1, "not_visited": 7}

    # The drill-down: 18 own order rows (19 item rows), S1's late row, 3 bonuses; N6 in November.
    water, juice = g.world.products.water.id, g.world.products.juice.id
    assert Counter(row["row"] for row in g.october_lines) == {"order": 19, "bonus": 3}
    assert Counter(e["kind"] for row in g.october_lines if row["row"] == "order" for e in row["events"]) == {
        "commission_credit": 18, "commission_reversal": 1,
    }
    assert [row["kind"] for row in g.october_lines if row["row"] == "bonus"] == ["new_outlet_bonus"] * 3
    assert g.statement["line_counts"] == {"commission_credit": 18, "commission_reversal": 1,
                                          "commission_difference": 0, "new_outlet_bonus": 3}
    own = [row for row in g.october_lines if row["row"] == "order" and not row["is_late"]]
    assert (len(own), sum(len(row["items"]) for row in own), sum(row["amount"] for row in own)) == (18, 19, 810000.0)
    (mixed,) = [row for row in own if row["order_id"] == g.mixed.id]
    assert sum(item["net"] for item in mixed["items"]) == 1700000.0  # the 10,000 fee is in no net figure
    assert (mixed["amount"], [product["product_id"] for product in mixed["products"]]) == (105000.0, [water, juice])
    (late_row,) = [row for row in g.october_lines if row["row"] == "order" and row["is_late"]]
    assert (late_row["order_id"], late_row["share_before"], late_row["share"]) == (g.s1.id, 9000.0, 0.0)
    assert [(row["row"], row["order_id"], row["earned_month"], row["amount"]) for row in g.november_lines] == [
        ("order", g.n6.id, "2026-11", 15000.0)
    ]
    october_period = SalesPayPeriod.query.filter_by(month_start=date(2026, 10, 1)).one()
    october_commission_lines = SalesPayLedgerLine.query.filter(
        SalesPayLedgerLine.period_id == october_period.id,
        SalesPayLedgerLine.agent_user_id == aziz.id,
        SalesPayLedgerLine.kind.in_(SALES_PAY_COMMISSION_KINDS),
    ).all()
    assert [line.amount for line in october_commission_lines] == [None] * 19  # 18 credits + 1 reversal

    # The new-outlet checks: O5 is imported and has none, and there is no O7.
    checks = {row["outlet_id"]: row for row in g.statement["new_outlets"]}
    o = g.outlets
    assert {outlet_id: row["status"] for outlet_id, row in checks.items()} == {
        o[1].id: "qualified", o[2].id: "qualified", o[3].id: "qualified", o[4].id: "prior_customer",
        o[6].id: "tracking", o[8].id: "tracking",
    }
    assert {index: (checks[o[index].id]["rule"], len(checks[o[index].id]["orders"]), checks[o[index].id]["amount"])
            for index in (1, 2, 3)} == {
        1: ("orders_with_total", 2, 150000.0), 2: ("orders_with_total", 2, 150000.0), 3: ("orders_any", 5, 150000.0),
    }
    for index, first in g.first_delivery.items():  # D-Q12: the window opens on the first delivery
        assert checks[o[index].id]["window_start"] == first.isoformat()
    assert checks[o[4].id]["window_start"] is None
    assert [(row["id"], row["status"], row["amount"]) for row in g.statement["penalties"]] == [(g.p1, "confirmed", 100000.0)]
    assert AgentOrderApproval.query.count() == 0  # builder orders never pass through `place_order`

    # Step 14: one push, to Aziz alone, after the approval; then paid.
    assert pushes_of(g.pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == [
        (
            int(AZIZ_CHAT),
            {
                "statement_id": g.statement_id, "month": "2026-10", "is_shadow": False, "base": 4677419,
                "commission": {"gross": 810000, "products": golden_products(g)},
                "commission_after_gate": 648000, "late": -9000, "new_outlets": 450000, "adjustments": 0,
                "penalties": 100000, "carry_in": {"amount": 0, "from_month": None, "source": None},
                "total": 5666419, "carry_out": 0, "owed": 0,
            },
            f"statement-approved:{g.statement_id}",
        )
    ]
    ((_chat, pushed, _event_id),) = pushes_of(g.pushes, SALES_EVENT_PAY_STATEMENT_APPROVED)
    # §7.5: the push carries A8's products as they are, `next_tier` null on a frozen statement.
    assert pushed["commission"]["products"] == summary["commission"]["products"]
    assert {type(tier_["amount"]) for product in pushed["commission"]["products"] for tier_ in product["tiers"]} == {int}
    assert (g.october_paid["status"], g.october_paid["paid_on"]) == ("paid", "2026-11-05")
    assert {key: g.earnings_after["last_statement"][key] for key in ("month", "status", "paid_on", "total", "owed")} == {
        "month": "2026-10", "status": "paid", "paid_on": "2026-11-05", "total": 5666419.0, "owed": 0.0,
    }


# ---------------------------------------------------------------------------- T-MONEY-1

# Every key under which a pay response publishes pay money. Order money is published as stored
# and is exempt: the `received*` keys, an order's `total` (a dict that also carries
# `order_number`), and an item's or a tier's `net`, `unit_price`, `total_price` and
# `discount_share` (none of them listed).
PAY_MONEY_KEYS = frozenset({
    "amount", "monthly", "base", "base_amount", "base_salary", "gross", "after_gate", "commission",
    "gated_commission", "late_commission_after_gate", "new_outlets", "adjustments", "penalties", "variable",
    "carry_in", "gross_total", "total", "carry_out", "owed", "estimated_total", "estimated_commission",
    "share", "share_before", "amount_full", "change", "total_before", "default_amount",
})


def money_values(body, path=()):
    """(path, value) of every number under a pay-money key, anywhere in `body`."""
    if isinstance(body, list):
        for index, value in enumerate(body):
            yield from money_values(value, path + (index,))
    elif isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, (dict, list)):
                yield from money_values(value, path + (key,))
            elif (key in PAY_MONEY_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool)
                  and not (key == "total" and "order_number" in body)):
                yield path + (key,), value


def test_every_pay_money_value_on_the_wire_is_whole_uzs(client, golden, admin_claim_headers, manager_claim_headers):
    """T-MONEY-1: walk every pay response of the golden month, admin (A1, A2, A8, A9, A12, A14, A16,
    A19, A22), manager (M1) and agent (S1-S4), and find no pay amount with a fraction."""
    g = golden
    aziz = g.aziz
    version = SalesPayPlanVersion.query.filter_by(plan_id=g.world.plan["id"]).one()
    admin_paths = [
        "/periods",
        "/periods/2026-09",
        "/periods/2026-10",
        "/periods/2026-11",
        f"/periods/2026-09/agents/{aziz.id}",
        f"/periods/2026-10/agents/{aziz.id}",
        f"/periods/2026-11/agents/{aziz.id}",
        f"/periods/2026-09/agents/{aziz.id}/lines?per_page=50",
        f"/periods/2026-10/agents/{aziz.id}/lines?per_page=50",
        f"/periods/2026-11/agents/{aziz.id}/lines?per_page=50",
        "/plans",
        f"/plans/{g.world.plan['id']}/versions/{version.id}",
        f"/agents/{aziz.id}/terms",
        "/penalty-types",
        "/penalties",
    ]
    responses = {path: pay_call(client, "GET", f"{PAY_API}{path}", admin_claim_headers) for path in admin_paths}
    responses["M1"] = pay_call(client, "GET", PROPOSALS_URL, manager_claim_headers)
    lapse_throttle(aziz)
    responses["S1"] = pay_call(client, "GET", EARNINGS, g.aziz_headers)
    for month in ("2026-09", "2026-10", "2026-11"):
        responses[f"S2 {month}"] = pay_call(client, "GET", f"{LINES}?month={month}", g.aziz_headers)
    responses["S3"] = pay_call(client, "GET", STATEMENTS, g.aziz_headers)
    for month in ("2026-09", "2026-10"):  # approved, and paid
        responses[f"S4 {month}"] = pay_call(client, "GET", f"{STATEMENTS}/{month}", g.aziz_headers)

    values = [(name,) + path + (value,) for name, body in responses.items() for path, value in money_values(body)]
    seen = {entry[-2] for entry in values}

    assert {"share", "share_before", "amount_full", "change", "total_before", "total", "estimated_commission",
            "gross_total"} <= seen
    assert [entry for entry in values if float(entry[-1]) != int(entry[-1])] == []
