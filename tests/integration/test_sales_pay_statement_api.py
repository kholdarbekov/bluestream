"""A8-A11, one agent in one month, driven through the admin pay routes (§4.7-§4.9, §5.2).

The ledger lines come from Task 5's sync, fed by the real order, cash, edit and collection
routes. The statement is read through A8 (live for an open month, frozen otherwise) and its
lines through A9. Every figure is a literal computed by hand in the test's docstring.

Commission lines carry no amount (v5): what a line did is read as its order's share on A9 (the
order rows) and its earned month's tier totals on A8 (`summary.commission`, `summary.late[]`).
Each order here is alone in its month and product on a single tier, so a v4 "credit +9,000" is a
share of 9,000 (§10.3, the reading note).

Clocks: each step moves the frozen business clock (`move_clock`). The writers stamp
created_at/updated_at with the wall clock, which is at or after 2026-09-28; every frozen `now`
here is on or before 2027-02-10. Only the last step of T-DIFF-3 passes the sync's 120-day
creation window, so that one sync runs with `full=True`. Admin edits of a delivered order are
refused 72 h after its delivery by the WALL clock, so T-DIFF-3 widens ORDER_EDIT_WINDOW_HOURS;
otherwise it would start failing once the real calendar passed October 2026.
"""

from datetime import date

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesAgentUnpaidDay, SalesPayAdjustment, SalesPayStatement
from business_app.tasks.sales_agent_tasks import sync_pay_ledger
from shared.enums import UserRole
from tests.integration.sales_pay_builders import (
    DEC,
    NOV,
    OCT,
    PAY_API,
    TEACHERS_DAY,
    adjust_cash,
    approved_outlet,
    close_and_approve,
    collect_cash,
    collect_cash_at,
    freeze_local_now,
    grocery_store,
    ledger_lines,
    live_cash_events,
    local_at,
    make_agent_order,
    make_outlet,
    make_store,
    move_clock,
    outlet_order,
    pay_agent,
    pay_audit,
    pay_call,
    pay_route,
    pay_store,
    pay_world,
    plan_days,
    publish_version,
    start_pay,
    sync_agent,
    units_applied_of,
    utc_at,
    verified_visits,
    water,  # noqa: F401 -- a fixture
    water_order,
)
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture
from tests.unit.test_outlet_dedupe import FAR, PIN

pytestmark = pytest.mark.integration

SEP, JAN = date(2026, 9, 1), date(2027, 1, 1)
# Ten October days with a plan row (1 October is a Thursday). Sundays 4 and 11 are working days
# too (C2 v5), but they have no plan row here, so they leave both halves of the gate (R47).
TEN_OCTOBER_DAYS = [date(2026, 10, d) for d in (1, 2, 3, 5, 6, 7, 8, 9, 10, 12)]
LINE_KINDS = ["commission_credit", "commission_reversal", "commission_difference", "new_outlet_bonus"]


@pytest.fixture
def make_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    """`pay_world` for Sardor, with the options a test names (start month, rate, base, dates)."""

    def make(**options):
        return pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, **options)

    return make


def october_gate(world, *, counted_days: int) -> None:
    """Twenty visits due in October: two outlets on each of TEN_OCTOBER_DAYS. Both are visited
    (verified) on the first `counted_days` of those days: 0 -> 0.0 % (x0.5), 7 -> 14 / 20 =
    70.0 % (x0.8)."""
    second = pay_store(world.db, world.agent, name="Baraka", onboarded=local_at(2026, 10, 1, 9))
    outlets = [world.store.id, second.id]
    plan_days(world.db, world.agent, TEN_OCTOBER_DAYS, outlets)
    verified_visits(world.db, world.agent, TEN_OCTOBER_DAYS[:counted_days], outlets)


def summary(world, month: str) -> dict:
    return pay_route(world, "GET", f"/periods/{month}/agents/{world.agent.id}")["summary"]


def shape(order):
    """(kind, cause, money level, earned month, month posted to, key) per line, in posting order. A
    commission line records a level and no amount (v5); a reversal records no level (None)."""
    lines = ledger_lines(order)
    assert [line.amount for line in lines] == [None] * len(lines)
    return [
        (
            line.kind,
            line.cause,
            line.snapshot.get("received_applied"),
            line.earned_month,
            line.period.month_start,
            line.idempotency_key,
        )
        for line in lines
    ]


def late_figures(summary_: dict) -> list:
    """A8 `summary.late[]`'s money per late group, without its per-product breakdown (§5.2)."""
    return [
        {key: group[key] for key in ("earned_month", "gross", "multiplier", "source", "after_gate")}
        for group in summary_["late"]
    ]


def share_now(world, order, *months):
    """The order's share at the latest cut that lists it: the first of `months` (newest first) whose
    A9 has a row for it. A month in which nothing moved the order does not list it (§4.8), so its
    share stands where the last listing left it."""
    for month in months:
        rows = pay_route(world, "GET", f"/periods/{month}/agents/{world.agent.id}/lines?per_page=100")["items"]
        for row in rows:
            if row["row"] == "order" and row["order_id"] == order.id:
                return int(row["share"])
    raise AssertionError(f"order {order.id} is listed in none of {months}")


# --------------------------------------------------------------------------- #
# Late lines keep the discipline of the month they were earned in (Q1)
# --------------------------------------------------------------------------- #


def test_a_reversal_after_the_close_is_gated_at_the_month_it_was_earned_in(make_world):
    """T-REV-1. Plan 19 L at 5,000: 20 bottles (400,000 cash) credit 100,000 in October.
    October's gate: 20 due, 0 counted = 0.0 % -> x0.5, so October pays 50,000 of it. After
    October's approval the cash is corrected to 0 on 3 November: a reversal of -100,000
    (`nothing_received`), earned 2026-10, posted to November, late, and gated at October's x0.5 =
    -50,000 in November, although November itself (nothing due) is x1.0."""
    world = make_world(rate_19l=5000)
    october_gate(world, counted_days=0)
    order = water_order(world, 20, date(2026, 10, 20))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 3))
    adjust_cash(world, live_cash_events(order)[0].id, 0)
    pay_route(world, "POST", "/periods/2026-11/recalculate")

    october = summary(world, "2026-10")
    november = summary(world, "2026-11")
    [row] = pay_route(world, "GET", f"/periods/2026-11/agents/{world.agent.id}/lines")["items"]

    assert (
        {key: october["commission"][key] for key in ("gross", "orders", "after_gate")},
        october["gate"]["multiplier"],
        october["gated_commission"],
    ) == ({"gross": 100000.0, "orders": 1, "after_gate": 50000.0}, 0.5, 50000.0)
    assert late_figures(november) == [
        {"earned_month": "2026-10", "gross": -100000.0, "multiplier": 0.5, "source": "statement", "after_gate": -50000.0}
    ]
    assert [(p["units_before"], p["units"], p["change"]) for p in november["late"][0]["products"]] == [(20, 0, -100000.0)]
    assert (november["gate"]["multiplier"], november["gate"]["rule"], november["gated_commission"]) == (
        1.0,
        "below_min_due",
        -50000.0,
    )
    assert {
        key: row[key]
        for key in ("row", "order_id", "earned_month", "is_late", "counted", "tier_shift", "share_before", "share", "amount")
    } == {
        "row": "order",
        "order_id": order.id,
        "earned_month": "2026-10",
        "is_late": True,
        "counted": True,
        "tier_shift": False,
        "share_before": 100000.0,
        "share": 0.0,
        "amount": -100000.0,
    }
    assert [(event["kind"], event["cause"]) for event in row["events"]] == [("commission_reversal", "nothing_received")]
    assert [item["quantity"] for item in row["items"]] == [20]  # the first credit's frozen lines (I-19)
    assert row["money"] == {"received_ref": 400000.0, "received": 0.0, "received_applied": None}


def test_money_back_before_the_reversal_settles_is_re_credited_in_the_same_month(make_world):
    """T-REV-2 (a). Credit 100,000 in October at x1.0 (nothing due); October approved. Cash
    corrected to 0 on 3 November: reversal -100,000 into November. The money is collected again on
    20 November while November is open: a re-credit +100,000 earned 2026-10, posted to November,
    so November's 2026-10 group is -100,000 + 100,000 = 0 at October's x1.0. The approved
    statements pay 100,000 + 0 = 100,000 for the order; keys commission:{id}:1..3.

    A9's order rows mark only the November credit event as a re-credit (PR8: the sync's snapshot
    `recredit`, published as `is_recredit`); October's first credit is not one."""
    world = make_world(rate_19l=5000)
    order = water_order(world, 20, date(2026, 10, 20))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 3))
    adjust_cash(world, live_cash_events(order)[0].id, 0)
    pay_route(world, "POST", "/periods/2026-11/recalculate")
    move_clock(world, local_at(2026, 11, 20))
    collect_cash(world, order, 400000)
    pay_route(world, "POST", "/periods/2026-11/recalculate")
    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2))

    october, november = summary(world, "2026-10"), summary(world, "2026-11")
    october_lines = pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}/lines")["items"]
    november_lines = pay_route(world, "GET", f"/periods/2026-11/agents/{world.agent.id}/lines")["items"]

    assert shape(order) == [
        ("commission_credit", None, "400000.00", OCT, OCT, f"commission:{order.id}:1"),
        ("commission_reversal", "nothing_received", None, OCT, NOV, f"commission:{order.id}:2"),
        ("commission_credit", None, "400000.00", OCT, NOV, f"commission:{order.id}:3"),
    ]
    assert [(e["kind"], e["is_recredit"]) for row in october_lines for e in row["events"]] == [
        ("commission_credit", False)
    ]
    assert sorted((e["kind"], e["is_recredit"]) for row in november_lines for e in row["events"]) == [
        ("commission_credit", True),
        ("commission_reversal", False),
    ]
    assert late_figures(november) == [
        {"earned_month": "2026-10", "gross": 0.0, "multiplier": 1.0, "source": "statement", "after_gate": 0.0}
    ]
    assert october["gated_commission"] + november["gated_commission"] == 100000.0


def test_a9_publishes_which_lines_follow_the_money(make_world):
    """Final-review I3 (controller ruling T14-R1): every event of an A9 order row carries
    `follows_money`, the D-Q3 display rule (§6.2 item 2) answered by `pay_rules.follows_money`, so the drill-down never
    decides it. October stays open throughout:
    - S, 6 x 19 L = 120,000 cash, delivered and paid 20 Oct: a first credit (false). 21 Oct its
      cash is corrected to 90,000: `received_reduced` (true); 22 Oct back to 120,000:
      `received_restored` (true).
    - R, the same order at another shop, 20 Oct: credited. 23 Oct its cash is corrected to 0: a
      `nothing_received` reversal (false); 24 Oct 120,000 is collected again: a re-credit (true).
    - 25 Oct A15 publishes a same-month version, 19 L at 2,000: v5 writes no line for it (§4.10);
      the calculator rates the month with it.
    The bonus half (false) is in `test_the_drawer_lists_each_outlet_check_the_month_touches`."""
    world = make_world()
    s = water_order(world, 6, date(2026, 10, 20))
    chorsu = pay_store(world.db, world.agent, name="Chorsu", onboarded=local_at(2026, 10, 1, 9))
    r = water_order(world, 6, date(2026, 10, 20), store=chorsu)

    def recalculate(day):
        move_clock(world, local_at(2026, 10, day))
        pay_route(world, "POST", "/periods/2026-10/recalculate")

    recalculate(20)
    s_event = adjust_cash(world, live_cash_events(s)[0].id, 90000)
    recalculate(21)
    adjust_cash(world, s_event, 120000)
    recalculate(22)
    adjust_cash(world, live_cash_events(r)[0].id, 0)
    recalculate(23)
    collect_cash(world, r, 120000)
    recalculate(24)
    move_clock(world, local_at(2026, 10, 25))
    publish_version(world, "2026-10", rate_19l=2000)
    recalculate(25)

    rows = pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}/lines?per_page=50")["items"]
    events = [event for row in rows for event in row["events"]]

    assert {(e["kind"], e["cause"], e["is_recredit"], e["follows_money"]) for e in events} == {
        ("commission_credit", None, False, False),
        ("commission_difference", "received_reduced", False, True),
        ("commission_difference", "received_restored", False, True),
        ("commission_reversal", "nothing_received", False, False),
        ("commission_credit", None, True, True),
    }
    assert len(events) == 6  # S: credit, reduced, restored; R: credit, reversal, re-credit. The version wrote none.


def test_money_back_after_the_reversal_settled_writes_nothing(make_world):
    """T-REV-2 (b). The same reversal, but November is closed on 2 December before the money
    comes back on 5 December: the reversal is settled (I-18), so no line is written and the
    approved statements pay 100,000 - 100,000 = 0."""
    world = make_world(rate_19l=5000)
    order = water_order(world, 20, date(2026, 10, 20))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 3))
    adjust_cash(world, live_cash_events(order)[0].id, 0)
    pay_route(world, "POST", "/periods/2026-11/recalculate")
    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2))
    move_clock(world, local_at(2026, 12, 5))
    collect_cash(world, order, 400000)
    pay_route(world, "POST", "/periods/2026-12/recalculate")

    assert shape(order) == [
        ("commission_credit", None, "400000.00", OCT, OCT, f"commission:{order.id}:1"),
        ("commission_reversal", "nothing_received", None, OCT, NOV, f"commission:{order.id}:2"),
    ]
    assert summary(world, "2026-10")["gated_commission"] + summary(world, "2026-11")["gated_commission"] == 0.0


def test_a_contract_order_switched_to_cash_and_paid_after_the_reversal_settled_writes_nothing(app, make_world):
    """T-BA-3, the settled half (Task 5 proves the open-month half). A contract order for a
    workplace shop, 6 x 19 L = 120,000 on the business account, is marked paid when placed, so it
    is credited 9,000 in October on its 3 October delivery (nothing due: x1.0). October is closed
    and approved on 2 November. On 3 November the admin switches it to cash: nothing is received,
    so November takes a reversal of -9,000 (`nothing_received`). November is closed and approved
    on 2 December, which settles the reversal (I-18); the shop pays the 120,000 at the office on
    5 December and December's sync writes nothing. The approved statements pay
    9,000 - 9,000 = 0 for the order.

    Like Task 5's `pay` fixture, the wall-clock edit window is widened, so the switch keeps
    working once the real calendar passes October 2026."""
    world = make_world()
    world.monkeypatch.setitem(app.config, "ORDER_EDIT_WINDOW_HOURS", 24 * 3650)  # the wall-clock edit window
    contract_shop = make_outlet(
        world.db,
        onboarded_by=world.agent,
        created_at=utc_at(2026, 9, 1),
        user=make_store(world.db, name="Bahor market", workplace=True),
        assigned_agent=world.agent,
    )
    move_clock(world, local_at(2026, 10, 3, 8))
    order = make_agent_order(
        world.db,
        world.agent,
        contract_shop,
        [(world.products.water, 6)],
        delivered_at=utc_at(2026, 10, 3, 11),
        paid_at=None,
        method="business_account",
    )
    move_clock(world, local_at(2026, 10, 5))
    pay_route(world, "POST", "/periods/2026-10/recalculate")
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    move_clock(world, local_at(2026, 11, 3))
    pay_call(
        world.client,
        "POST",
        f"/api/v1/admin/orders/{order.id}/payment-method",
        world.headers,
        {"new_method": "cash", "reason": "Contract paused, the shop pays cash", "bypass_cod_check": False},
    )
    pay_route(world, "POST", "/periods/2026-11/recalculate")
    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2))
    move_clock(world, local_at(2026, 12, 5))
    collect_cash(world, order, 120000)
    pay_route(world, "POST", "/periods/2026-12/recalculate")

    assert shape(order) == [
        ("commission_credit", None, "120000.00", OCT, OCT, f"commission:{order.id}:1"),
        ("commission_reversal", "nothing_received", None, OCT, NOV, f"commission:{order.id}:2"),
    ]
    assert summary(world, "2026-10")["gated_commission"] + summary(world, "2026-11")["gated_commission"] == 0.0


def test_the_owners_sequence_never_pays_more_than_the_money_received(app, make_world):
    """T-DIFF-3 (D-Q3, D-Q1). Order S: 6 x 19 L = 120,000 cash, delivered and paid 20 October:
    credit +9,000. October's gate 14 / 20 = 70.0 % -> x0.8, so October pays 7,200.

    - 5 Nov: cash 120,000 -> 90,000: difference -2,250 (9,000 x 90,000 / 120,000 = 6,750).
    - 6 Nov: back to 120,000: +2,250 (`received_restored`).
    - 8 Nov: 90,000 again: -2,250. November closed and approved: its 2026-10 group is -2,250
      at October's x0.8 = -1,800.
    - 3 Dec: edited up to 7 bottles (140,000): no line (the lines are frozen, I-19).
    - 4 Dec: 50,000 collected: no line (received 140,000, applied = the settled 90,000).
    - 5 Dec: both cash events corrected to 0: reversal -6,750 into December.
    - 10 Dec: 140,000 collected again: a re-credit of 6,750, not 9,000 and not 10,500
      (applied 90,000, the six frozen bottles). December's 2026-10 group is 0.
    The order's share at each step's cut (A9): 9,000 / 6,750 / 9,000 / 6,750 / 6,750 / 6,750 / 0 /
    6,750, never above 9,000 (§10.3); no line has an amount.
    Sigma gated October-December = 7,200 - 1,800 + 0 = 5,400 = 6,750 x 0.8.
    Settled reversal: 5 Jan corrected to 0 again: reversal -6,750 into January; January closed;
    10 Feb collected again: no line.
    The nightly job runs twice after every step and writes nothing the second time."""
    world = make_world()
    world.monkeypatch.setitem(app.config, "ORDER_EDIT_WINDOW_HOURS", 24 * 3650)  # the wall-clock edit window
    october_gate(world, counted_days=7)
    order = water_order(world, 6, date(2026, 10, 20))
    nets = []

    def step(when, *months, full=False):
        move_clock(world, when)
        sync_pay_ledger.run(full=full)
        posted = len(ledger_lines(order))
        sync_pay_ledger.run(full=full)
        assert len(ledger_lines(order)) == posted, when
        nets.append(share_now(world, order, *months))

    step(local_at(2026, 10, 25), "2026-10")
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    event = live_cash_events(order)[0].id
    event = adjust_cash(world, event, 90000)
    step(local_at(2026, 11, 5), "2026-11")
    event = adjust_cash(world, event, 120000)
    step(local_at(2026, 11, 6), "2026-11")
    event = adjust_cash(world, event, 90000)
    step(local_at(2026, 11, 8), "2026-11")
    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2))
    november_row = pay_route(world, "GET", f"/periods/2026-11/agents/{world.agent.id}/lines")["items"][0]

    move_clock(world, local_at(2026, 12, 3))
    world.db.session.refresh(order)
    pay_call(
        world.client,
        "POST",
        f"/api/v1/admin/orders/{order.id}/edit",
        world.headers,
        {
            "items": [{"orderItemId": order.order_items[0].id, "productId": world.products.water.id, "quantity": 7}],
            "reason": "The shop took one more bottle",
        },
    )
    step(local_at(2026, 12, 3), "2026-12", "2026-11")
    move_clock(world, local_at(2026, 12, 4))
    collect_cash(world, order, 50000)
    step(local_at(2026, 12, 4), "2026-12", "2026-11")
    move_clock(world, local_at(2026, 12, 5))
    for cash in live_cash_events(order):
        adjust_cash(world, cash.id, 0)
    step(local_at(2026, 12, 5), "2026-12", "2026-11")
    move_clock(world, local_at(2026, 12, 10))
    collect_cash(world, order, 140000)
    step(local_at(2026, 12, 10), "2026-12", "2026-11")
    close_and_approve(world, "2026-12", at=local_at(2027, 1, 2))
    lines = ledger_lines(order)
    october, november, december = (summary(world, month) for month in ("2026-10", "2026-11", "2026-12"))

    move_clock(world, local_at(2027, 1, 5))
    for cash in live_cash_events(order):
        adjust_cash(world, cash.id, 0)
    step(local_at(2027, 1, 5), "2027-01", "2026-12")
    move_clock(world, local_at(2027, 2, 2))
    pay_route(world, "POST", "/periods/2027-01/close")
    move_clock(world, local_at(2027, 2, 10))
    collect_cash(world, order, 140000)
    # Past the 120-day creation window of a windowed run: only `full` still looks at the order,
    # and even it must write nothing for a settled reversal (I-18).
    step(local_at(2027, 2, 10), "2027-02", "2027-01", full=True)

    assert nets == [9000, 6750, 9000, 6750, 6750, 6750, 0, 6750, 0, 0]
    assert [(line.kind, line.cause, line.snapshot.get("received_applied"), line.period.month_start) for line in lines] == [
        ("commission_credit", None, "120000.00", OCT),
        ("commission_difference", "received_reduced", "90000.00", NOV),
        ("commission_difference", "received_restored", "120000.00", NOV),
        ("commission_difference", "received_reduced", "90000.00", NOV),
        ("commission_reversal", "nothing_received", None, DEC),
        ("commission_credit", None, "90000.00", DEC),
    ]
    assert [line.amount for line in lines] == [None] * 6
    assert [line.idempotency_key for line in lines] == [f"commission:{order.id}:{n}" for n in range(1, 7)]
    assert {line.earned_month for line in lines} == {OCT}
    # The re-credit: the six frozen bottles at the settled 90,000, never the edited seven (I-19, I-41).
    assert (lines[5].snapshot["recredit"], units_applied_of(lines[5])) == (True, {world.products.water.id: 6})
    assert november_row["money"] == {"received_ref": 120000.0, "received": 90000.0, "received_applied": 90000.0}
    assert (november_row["share_before"], november_row["share"], november_row["amount"]) == (9000.0, 6750.0, -2250.0)
    assert [event["cause"] for event in november_row["events"]] == ["received_reduced", "received_restored", "received_reduced"]
    assert (october["gate"]["multiplier"], october["commission"]["after_gate"]) == (0.8, 7200.0)
    assert late_figures(november) == [
        {"earned_month": "2026-10", "gross": -2250.0, "multiplier": 0.8, "source": "statement", "after_gate": -1800.0}
    ]
    assert late_figures(december) == [
        {"earned_month": "2026-10", "gross": 0.0, "multiplier": 0.8, "source": "statement", "after_gate": 0.0}
    ]
    assert october["gated_commission"] + november["gated_commission"] + december["gated_commission"] == 5400.0
    assert [(line.kind, line.amount, line.period.month_start) for line in ledger_lines(order)[6:]] == [
        ("commission_reversal", None, JAN)
    ]


def test_a_re_credit_is_paid_at_the_first_credits_plan_however_the_cash_comes_back(make_world):
    """T-DIFF-7 (I-19). Plan v1 (September): 19 L at 1,500. S1 and S2 (6 x 19 L = 120,000 each)
    are credited 9,000 in September; September and October are approved. A15 publishes v2
    effective November: 19 L at 2,000. 5 Nov: both orders' cash corrected to 0, reversals of
    -9,000 into November. 10 Nov: S1's money recorded through the collections route, S2's put
    back through the adjust route (which keeps the event's September `occurred_at`). Both get
    a re-credit that returns them to September at v1 (9,000 each, never 12,000): November's group
    09.2026 nets 0."""
    world = make_world(start="2026-09")
    v1 = world.plan["versions"][0]["id"]
    s1 = water_order(world, 6, date(2026, 9, 20))
    chorsu = pay_store(world.db, world.agent, name="Chorsu", onboarded=local_at(2026, 9, 1, 9))
    s2 = water_order(world, 6, date(2026, 9, 21), store=chorsu)
    close_and_approve(world, "2026-09", at=local_at(2026, 10, 2))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2))
    v2 = publish_version(world, "2026-11", rate_19l=2000)
    move_clock(world, local_at(2026, 11, 5))
    adjust_cash(world, live_cash_events(s1)[0].id, 0)
    s2_event = adjust_cash(world, live_cash_events(s2)[0].id, 0)
    pay_route(world, "POST", "/periods/2026-11/recalculate")
    move_clock(world, local_at(2026, 11, 10))
    collect_cash(world, s1, 120000)
    adjust_cash(world, s2_event, 120000)
    pay_route(world, "POST", "/periods/2026-11/recalculate")

    expected = [
        ("commission_credit", "120000.00", SEP, SEP, v1),
        ("commission_reversal", None, SEP, NOV, v1),
        ("commission_credit", "120000.00", SEP, NOV, v1),
    ]
    for order in (s1, s2):
        assert [
            (line.kind, line.snapshot.get("received_applied"), line.earned_month, line.period.month_start,
             line.plan_version_id)
            for line in ledger_lines(order)
        ] == expected
    assert v2["id"] != v1
    november = summary(world, "2026-11")
    assert late_figures(november) == [
        {"earned_month": "2026-09", "gross": 0.0, "multiplier": 1.0, "source": "statement", "after_gate": 0.0}
    ]
    # September stays on v1: 12 bottles × 1,500 = 18,000 before and after, never 24,000 at v2 (I-19).
    assert [(p["total_before"], p["total"]) for p in november["late"][0]["products"]] == [(18000.0, 18000.0)]
    assert november["late"][0]["plan_version_id"] == v1


def test_a_reduction_whose_month_closed_is_not_restored_by_a_later_fix(make_world):
    """T-DIFF-8, after the close (Task 5 proves the open-month half). Order S: 6 x 19 L = 120,000
    cash, delivered and paid 20 October: credit +9,000. On 25 October the cash is corrected to
    90,000: 9,000 x 90,000 / 120,000 = 6,750, a -2,250 `received_reduced` difference in October.
    October is closed on 2 November, which settles the reduction (I-18). On 3 November the cash
    is put back to 120,000: November's sync writes no line, so the order's net stays 6,750."""
    world = make_world()
    order = water_order(world, 6, date(2026, 10, 20))
    move_clock(world, local_at(2026, 10, 21))
    pay_route(world, "POST", "/periods/2026-10/recalculate")
    move_clock(world, local_at(2026, 10, 25))
    event = adjust_cash(world, live_cash_events(order)[0].id, 90000)
    pay_route(world, "POST", "/periods/2026-10/recalculate")
    reduced = shape(order)
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    move_clock(world, local_at(2026, 11, 3))
    adjust_cash(world, event, 120000)
    pay_route(world, "POST", "/periods/2026-11/recalculate")

    assert reduced == [
        ("commission_credit", None, "120000.00", OCT, OCT, f"commission:{order.id}:1"),
        ("commission_difference", "received_reduced", "90000.00", OCT, OCT, f"commission:{order.id}:2"),
    ]
    assert shape(order) == reduced
    assert share_now(world, order, "2026-11", "2026-10") == 6750


def test_money_recorded_after_the_close_is_credited_late_at_the_earned_months_gate(make_world):
    """T-LATE-1. October's gate is 70.0 % -> x0.8. An order delivered 30 October is left unpaid;
    October is closed on 3 November. On 4 November its cash is recorded with `occurred_at` 31
    October 12:00Z (17:00 local): earned 2026-10, credited 9,000 into November, late, gated at
    October's frozen x0.8 = 7,200. October's statement is unchanged.

    The collection goes through Task 6's `collect_cash_at` (`CashCollectionService.post_collection`,
    the builders' backdating writer), which pays the order's whole total, 120,000: the admin
    collections route passes `occurred_at` through unparsed, so it cannot backdate."""
    world = make_world()
    october_gate(world, counted_days=7)
    late = water_order(world, 6, date(2026, 10, 30), paid=False)
    move_clock(world, local_at(2026, 11, 3))
    pay_route(world, "POST", "/periods/2026-10/close")
    before = pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}")
    move_clock(world, local_at(2026, 11, 4))
    collect_cash_at(world.db, late, at=utc_at(2026, 10, 31, 17), actor=world.admin)
    pay_route(world, "POST", "/periods/2026-11/recalculate")

    [row] = pay_route(world, "GET", f"/periods/2026-11/agents/{world.agent.id}/lines")["items"]
    after = pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}")

    assert ([e["kind"] for e in row["events"]], row["earned_month"], row["is_late"], row["amount"]) == (
        ["commission_credit"],
        "2026-10",
        True,
        9000.0,
    )
    assert late_figures(summary(world, "2026-11")) == [
        {"earned_month": "2026-10", "gross": 9000.0, "multiplier": 0.8, "source": "statement", "after_gate": 7200.0}
    ]
    assert after == before
    assert before["summary"]["commission"]["gross"] == 0.0


# --------------------------------------------------------------------------- #
# Nothing below the paid total is floored (C1)
# --------------------------------------------------------------------------- #


def test_a_correction_reaches_the_base_with_no_penalty_involved(make_world):
    """T-CORR-1. Illustration B's terms: base 3,000,000; A7 holiday 1 October and A10 unpaid 14
    and 15 October leave 28 of 30 working days: 3,000,000 x 28 / 30 = 2,800,000.
    September credits of 300,000 (200 x 19 L at 1,500, as two orders of 100: an order line takes at
    most MAX_QUANTITY_PER_ITEM = 100; September approved at x1.0) are corrected to 0 on 5 October:
    a late group of -150,000 - 150,000 = -300,000 at September's x1.0. Variable -300,000; gross
    2,800,000 - 300,000 = 2,500,000, paid in full; nothing carried."""
    world = make_world(start="2026-09")
    september = [water_order(world, 100, date(2026, 9, day)) for day in (20, 21)]
    close_and_approve(world, "2026-09", at=local_at(2026, 10, 2))
    pay_route(world, "PUT", "/periods/2026-10/holidays", TEACHERS_DAY)
    pay_route(
        world,
        "PUT",
        f"/periods/2026-10/agents/{world.agent.id}/unpaid-days",
        {"days": [{"date": "2026-10-14", "note": "sick"}, {"date": "2026-10-15", "note": "sick"}]},
    )
    move_clock(world, local_at(2026, 10, 5))
    for order in september:
        adjust_cash(world, live_cash_events(order)[0].id, 0)
    pay_route(world, "POST", "/periods/2026-10/recalculate")

    october = summary(world, "2026-10")

    assert late_figures(october) == [
        {"earned_month": "2026-09", "gross": -300000.0, "multiplier": 1.0, "source": "statement", "after_gate": -300000.0}
    ]
    assert {key: october[key] for key in ("base_amount", "variable", "gross_total", "total", "carry_out", "owed")} == {
        "base_amount": 2800000.0,
        "variable": -300000.0,
        "gross_total": 2500000.0,
        "total": 2500000.0,
        "carry_out": 0.0,
        "owed": 0.0,
    }


# --------------------------------------------------------------------------- #
# Adjustments (A11)
# --------------------------------------------------------------------------- #


def test_adjustments_are_signed_whole_and_explained_and_never_cancelled(app, db, make_world, manager_claim_headers, audit_events):
    """T-ADJ-1. +50,000 and -20,000 are posted (Sigma 30,000 in the statement); a 4-character
    reason, 0, 1.5 and a mistyped key are refused; an agent with no terms for October is refused
    with TERMS_MISSING; a manager is refused by the decorator; no route cancels an adjustment."""
    world = make_world()
    agent = world.agent.id
    path = f"/periods/2026-10/agents/{agent}/adjustments"
    newcomer = pay_agent(db, phone="+998901230742", first_name="Dilshod")

    bonus = pay_route(world, "POST", path, {"amount": 50000, "reason": "Guarantee top-up"}, status=201)["adjustment"]
    deduction = pay_route(world, "POST", path, {"amount": -20000, "reason": "Damaged crate"}, status=201)["adjustment"]
    short = pay_route(world, "POST", path, {"amount": 5000, "reason": "four"}, status=400)
    zero = pay_route(world, "POST", path, {"amount": 0, "reason": "Nothing at all"}, status=400)
    fraction = pay_route(world, "POST", path, {"amount": 1.5, "reason": "Half a som"}, status=400)
    pay_route(world, "POST", path, {"amount": 5000, "reason": "Mistyped key", "amont": 1}, status=400)
    no_terms = pay_route(
        world, "POST", f"/periods/2026-10/agents/{newcomer.id}/adjustments",
        {"amount": 5000, "reason": "Before any terms"}, status=409,
    )
    manager = world.client.post(
        f"{PAY_API}{path}", json={"amount": 5000, "reason": "Manager tries"}, headers=manager_claim_headers
    )
    statement = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")

    assert {key: bonus[key] for key in ("amount", "reason", "source", "carried_from_month", "posting_month")} == {
        "amount": 50000.0,
        "reason": "Guarantee top-up",
        "source": "admin",
        "carried_from_month": None,
        "posting_month": "2026-10",
    }
    assert bonus["created_by"] == {"id": world.admin.id, "name": "Admin User"}
    assert (short["error_code"], short["details"]) == (
        "SALES_PAY_REASON_REQUIRED",
        {"field": "reason", "min_length": 5, "max_length": 300, "validation_errors": []},
    )
    assert [(body["error_code"], body["details"]) for body in (zero, fraction)] == [
        ("SALES_PAY_AMOUNT_INVALID", {"field": "amount", "validation_errors": []}),
        ("SALES_PAY_AMOUNT_INVALID", {"field": "amount", "validation_errors": []}),
    ]
    assert (no_terms["error_code"], no_terms["details"]) == ("SALES_PAY_TERMS_MISSING", {"agents": [newcomer.id]})
    assert manager.status_code == 403
    assert SalesPayAdjustment.query.count() == 2
    assert statement["summary"]["adjustments"] == 30000.0
    assert [(row["id"], row["amount"]) for row in statement["adjustments"]] == [
        (bonus["id"], 50000.0),
        (deduction["id"], -20000.0),
    ]
    assert [rule.rule for rule in app.url_map.iter_rules() if "/adjustments/<" in rule.rule] == []
    assert pay_audit(audit_events, "adjustment_created") == [
        ("sales_agent", str(agent), None, {"adjustment_id": bonus["id"], "month": "2026-10", "amount": 50000, "reason": "Guarantee top-up"}),
        ("sales_agent", str(agent), None, {"adjustment_id": deduction["id"], "month": "2026-10", "amount": -20000, "reason": "Damaged crate"}),
    ]


def test_an_adjustment_before_pay_starts_is_refused(client, db, monkeypatch, admin_claim_headers, sales_agent_user):
    freeze_local_now(monkeypatch, local_at(2026, 10, 1, 9))

    refused = pay_call(
        client,
        "POST",
        f"{PAY_API}/periods/2026-10/agents/{sales_agent_user.id}/adjustments",
        admin_claim_headers,
        {"amount": 5000, "reason": "Too early"},
        status=409,
    )

    assert refused["error_code"] == "SALES_PAY_NOT_STARTED"
    assert SalesPayAdjustment.query.count() == 0


# --------------------------------------------------------------------------- #
# The drill-down: items, outlet checks, the base, self-decisions, refusals
# --------------------------------------------------------------------------- #


def test_the_drill_down_shows_each_items_frozen_money_and_its_tier_segments(db, make_world):
    """T-ALLOC-9 and T-PLAN-3's snapshot name. One order: 2 × 19 L (40,000; one tier at 1,500 a unit
    = 3,000) and 4 × Juice 1 L (100,000 net; the default single 3 % tier = 3,000): a 6,000 share.
    The juice is renamed after the credit; the drill-down still shows the name frozen in the
    snapshot. Each item shows its gross, discount share and net and carries no rate: rates live on
    the order's tier segments (§5.2)."""
    world = make_world()
    move_clock(world, local_at(2026, 10, 20, 8))
    make_agent_order(
        db,
        world.agent,
        world.store,
        [(world.products.water, 2), (world.products.juice, 4)],
        delivered_at=utc_at(2026, 10, 20),
        paid_at=utc_at(2026, 10, 20),
    )
    move_clock(world, local_at(2026, 10, 21))
    pay_route(world, "POST", "/periods/2026-10/recalculate")
    world.products.juice.name = "Juice 1L (new recipe)"
    db.session.commit()

    [row] = pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}/lines")["items"]

    water, juice = world.products.water.id, world.products.juice.id
    assert (row["amount"], row["outlet_name"]) == (6000.0, world.store.name)
    assert [item["product_name"]["en"] for item in row["items"]] == ["Water 19L", "Juice 1L"]
    assert [{key: value for key, value in item.items() if key != "product_name"} for item in row["items"]] == [
        {"product_id": water, "quantity": 2, "unit_price": 20000.0, "total_price": 40000.0, "discount_share": 0.0,
         "net": 40000.0},
        {"product_id": juice, "quantity": 4, "unit_price": 25000.0, "total_price": 100000.0, "discount_share": 0.0,
         "net": 100000.0},
    ]
    assert [
        (p["product_id"], p["units"], p["net"],
         [(t["from_unit"], t["to_unit"], t["units"], t["mode"], t["value"], t["share"]) for t in p["tiers"]])
        for p in row["products"]
    ] == [
        (water, 2, 40000.0, [(1, None, 2, "per_unit", 1500.0, 3000.0)]),
        (juice, 4, 100000.0, [(1, None, 4, "percent", 3.0, 3000.0)]),
    ]


def test_the_drawer_lists_each_outlet_check_the_month_touches(
    client, db, monkeypatch, admin_user, admin_claim_headers, operator_user, sales_agent_user, water
):
    """T-NEW-10, the A8 half (Task 6 proves the check rows). Oasis and Baraka are onboarded on
    25 September by Aziz and approved by operator Olim. Oasis's first order (200,000) is
    delivered 1 October 10:00 local and left unpaid, which opens its window 2026-10-01T05:00Z to
    2026-11-30T05:00Z. Baraka is paid on delivery (200,000) and again on 25 November (150,000):
    2 orders and 350,000, so it qualifies in November (`orders_with_total`, 150,000). On 1
    December Oasis expires. A8 lists a check in every month its window overlaps, and a qualified
    one in the month of its bonus line."""
    aziz = sales_agent_user
    start_pay(client, admin_user, admin_claim_headers, aziz)
    oasis_customer = grocery_store(db, "+998901239304", "Oasis market")
    baraka_customer = grocery_store(db, "+998901239305", "Baraka")
    oasis = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=oasis_customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    baraka = approved_outlet(
        db, agent=aziz, operator=operator_user, customer=baraka_customer, onboarded_at=utc_at(2026, 9, 25), pin=FAR
    )
    order_a = outlet_order(db, oasis_customer, oasis, 20, water, delivered_at=utc_at(2026, 10, 1), paid_at=None)
    order_c = outlet_order(
        db, baraka_customer, baraka, 20, water, delivered_at=utc_at(2026, 10, 1), paid_at=utc_at(2026, 10, 1)
    )

    def drawer(month, when):
        freeze_local_now(monkeypatch, when)
        sync_agent(aziz, when)
        data = pay_call(client, "GET", f"{PAY_API}/periods/{month}/agents/{aziz.id}", admin_claim_headers)
        return {row["outlet_id"]: row for row in data["new_outlets"]}

    october = drawer("2026-10", local_at(2026, 10, 2))
    outlet_order(db, oasis_customer, oasis, 15, water, delivered_at=utc_at(2026, 11, 25), paid_at=utc_at(2026, 11, 25))
    order_d = outlet_order(
        db, baraka_customer, baraka, 15, water, delivered_at=utc_at(2026, 11, 25), paid_at=utc_at(2026, 11, 25)
    )
    november = drawer("2026-11", local_at(2026, 11, 26))
    # Final-review I3: a bonus has no money to follow, so A9 publishes `follows_money: false`.
    bonus_lines = pay_call(
        client, "GET", f"{PAY_API}/periods/2026-11/agents/{aziz.id}/lines?kind=new_outlet_bonus", admin_claim_headers
    )["items"]
    november_after_expiry = drawer("2026-11", local_at(2026, 12, 1))
    december = drawer("2026-12", local_at(2026, 12, 1))
    month_view = pay_call(client, "GET", f"{PAY_API}/periods/2026-11", admin_claim_headers)

    no_flags = {
        "dedupe_forced": False,
        "nearby_other_accounts": 0,
        "all_orders_placed_by_onboarder": False,
        "delivered_by_onboarder": False,
        "self_approved_orders": 0,
    }
    tracking = october[oasis.id]
    tracking_keys = ("status", "window_start", "window_end", "window_days", "rule", "amount", "orders")
    assert {key: tracking[key] for key in tracking_keys} == {
        "status": "tracking",
        "window_start": "2026-10-01T05:00:00+00:00",
        "window_end": "2026-11-30T05:00:00+00:00",
        "window_days": 60,  # the frozen evaluation version's window (PR9), not a re-read of the plan
        "rule": None,
        "amount": None,
        "orders": [],
    }
    assert tracking["window_opened_by"] == {
        "order_id": order_a.id,
        "order_number": order_a.order_number,
        "order_source": "telegram",
        "delivered_instant": "2026-10-01T05:00:00+00:00",
        "is_paid": False,
    }
    assert (tracking["outlet_name"], tracking["review_flags"]) == (oasis.name, no_flags)
    assert october[baraka.id]["status"] == "tracking"
    qualified = november[baraka.id]
    assert (qualified["status"], qualified["rule"], qualified["amount"], qualified["is_late"]) == (
        "qualified",
        "orders_with_total",
        150000.0,
        False,
    )
    assert [(row["order_id"], row["total"], row["staff_approved"]) for row in qualified["orders"]] == [
        (order_c.id, 200000.0, False),
        (order_d.id, 150000.0, False),
    ]
    assert [(line["kind"], line["amount"], line["follows_money"]) for line in bonus_lines] == [
        ("new_outlet_bonus", 150000.0, False)
    ]
    assert november[oasis.id]["status"] == "tracking"
    assert november_after_expiry[oasis.id]["status"] == "expired"
    assert oasis.id not in december and baraka.id not in december
    assert [(row["agent_user_id"], row["review_flags"], row["new_outlets"]) for row in month_view["agents"]] == [
        (aziz.id, 0, 150000.0)
    ]


def test_the_base_follows_employment_holidays_and_unpaid_days(make_world):
    """T-BASE-1. Employed 15-20 October on a base of 2,700,000. Every day is a working day (C2 v5):
    October has 31, and the 15th-20th are worked, Sunday the 18th included:
    2,700,000 x 6 / 31 = 522,580.65 -> 522,581. A10 on Sunday the 18th is accepted (I-38):
    5 worked, 2,700,000 x 5 / 31 = 435,483.87 -> 435,484. A7 makes Friday the 16th a holiday:
    30 working days, 4 worked: 2,700,000 x 4 / 30 = 360,000, and the 16th leaves the gate (visits
    due 4 -> 2). A10 [16, 18] is refused on the holiday (`not_working_day`) and A10 [14] outside
    the employment (`not_employed`); the unpaid set stays [18]. A raise effective November does
    not change October."""
    world = make_world(base=2700000, employment_start="2026-10-15", employment_end="2026-10-20")
    move_clock(world, local_at(2026, 10, 21))
    second = pay_store(world.db, world.agent, name="Baraka", onboarded=local_at(2026, 10, 1, 9))
    plan_days(world.db, world.agent, [date(2026, 10, 15), date(2026, 10, 16)], [world.store.id, second.id])
    agent = world.agent.id
    unpaid_days = f"/periods/2026-10/agents/{agent}/unpaid-days"

    before = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")
    sunday = pay_route(world, "PUT", unpaid_days, {"days": [{"date": "2026-10-18"}]})
    pay_route(world, "PUT", "/periods/2026-10/holidays", {"days": [{"date": "2026-10-16", "note": "Company day off"}]})
    on_the_holiday = pay_route(
        world, "PUT", unpaid_days, {"days": [{"date": "2026-10-16"}, {"date": "2026-10-18"}]}, status=400
    )
    not_employed = pay_route(world, "PUT", unpaid_days, {"days": [{"date": "2026-10-14"}]}, status=400)
    pay_route(
        world,
        "POST",
        f"/agents/{agent}/terms",
        {"effective_month": "2026-11", "base_salary": "3000000", "plan_id": world.plan["id"]},
        status=201,
    )
    after = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")

    assert (before["inputs"]["working_days"], before["inputs"]["worked_days"], before["summary"]["base_amount"]) == (
        31,
        6,
        522581.0,
    )
    assert before["summary"]["gate"]["visits_due"] == 4
    assert (sunday["inputs"]["working_days"], sunday["inputs"]["worked_days"], sunday["summary"]["base_amount"]) == (
        31,
        5,
        435484.0,
    )
    assert (on_the_holiday["error_code"], on_the_holiday["details"]) == (
        "SALES_PAY_DATE_INVALID",
        {"field": "days", "date": "2026-10-16", "reason": "not_working_day", "validation_errors": []},
    )
    assert (not_employed["error_code"], not_employed["details"]) == (
        "SALES_PAY_DATE_INVALID",
        {"field": "days", "date": "2026-10-14", "reason": "not_employed", "validation_errors": []},
    )
    assert (
        after["inputs"]["working_days"],
        after["inputs"]["worked_days"],
        after["inputs"]["base_salary"],
        after["summary"]["base_amount"],
    ) == (30, 4, 2700000.0, 360000.0)
    assert after["summary"]["gate"]["visits_due"] == 2
    days = {row["date"]: row for row in after["days"]}
    assert (days["2026-10-15"]["day_status"], days["2026-10-15"]["due"]) == ("worked", 2)
    assert (days["2026-10-16"]["day_status"], days["2026-10-16"]["due"]) == ("holiday", 2)
    assert days["2026-10-18"]["day_status"] == "unpaid"
    assert (days["2026-10-14"]["day_status"], days["2026-10-21"]["day_status"]) == ("not_employed", "not_employed")
    assert after["inputs"]["holidays"] == [{"date": "2026-10-16", "note": "Company day off"}]
    assert after["inputs"]["unpaid_days"] == [{"date": "2026-10-18", "note": None}]


def test_an_admin_deciding_about_their_own_pay_is_tagged_live_and_frozen(app, db, make_world, audit_events):
    """The A-route half of T-SELFDEC-1 (§4.13; Task 8 adds the penalty half). Admin Anvar holds
    an agent profile and, with his own token, sets his employment (A18), his terms (A17), two
    unpaid days (A10) and a +50,000 adjustment (A11): each is allowed. The month view read by
    another admin tags Anvar's row, not Sardor's; A8 lists exactly those five decisions, sorted
    by `at`, with their ids; after the close the frozen `inputs.self_decisions` is the same list
    and A8 reads it from there; each of Anvar's audit rows carries `self_decided: true`."""
    world = make_world()
    anvar = pay_agent(db, phone="+998901230731", first_name="Anvar", role=UserRole.ADMIN)
    with app.app_context():
        token = create_access_token(identity=str(anvar.id), additional_claims={"role": "admin"})
    anvar_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def as_anvar(method, path, payload=None, *, status=200):
        return pay_call(world.client, method, f"{PAY_API}{path}", anvar_headers, payload, status=status)

    as_anvar("PUT", f"/agents/{anvar.id}/employment", {"start": "2026-10-01", "end": None})
    terms = as_anvar(
        "POST",
        f"/agents/{anvar.id}/terms",
        {"effective_month": "2026-10", "base_salary": "4000000", "plan_id": world.plan["id"]},
        status=201,
    )["term_in_force"]
    as_anvar("PUT", f"/periods/2026-10/agents/{anvar.id}/unpaid-days", {"days": [{"date": "2026-10-12"}, {"date": "2026-10-13"}]})
    adjustment = as_anvar(
        "POST", f"/periods/2026-10/agents/{anvar.id}/adjustments", {"amount": 50000, "reason": "Guarantee top-up"}, status=201
    )["adjustment"]
    month_view = pay_route(world, "GET", "/periods/2026-10")
    live = pay_route(world, "GET", f"/periods/2026-10/agents/{anvar.id}")
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    frozen = pay_route(world, "GET", f"/periods/2026-10/agents/{anvar.id}")

    profile_id = SalesAgentProfile.query.filter_by(user_id=anvar.id).one().id
    unpaid_ids = sorted(row.id for row in SalesAgentUnpaidDay.query.filter_by(agent_user_id=anvar.id))
    stored = SalesPayStatement.query.filter_by(agent_user_id=anvar.id).one().inputs["self_decisions"]
    assert {row["agent_user_id"]: row["self_decided"] for row in month_view["agents"]} == {
        world.agent.id: False,
        anvar.id: True,
    }
    assert sorted((row["input"], row["action"], row["id"], row["date"]) for row in live["self_decisions"]) == sorted(
        [
            ("terms", "added", terms["id"], None),
            ("employment", "set", profile_id, None),
            ("unpaid_day", "set", unpaid_ids[0], "2026-10-12"),
            ("unpaid_day", "set", unpaid_ids[1], "2026-10-13"),
            ("adjustment", "created", adjustment["id"], None),
        ]
    )
    assert [row["at"] for row in live["self_decisions"]] == sorted(row["at"] for row in live["self_decisions"])
    assert (live["self_decided"], frozen["self_decided"], frozen["is_estimate"]) == (True, True, False)
    assert frozen["self_decisions"] == live["self_decisions"] == stored
    for verb in ("employment_set", "terms_added", "unpaid_days_set", "adjustment_created"):
        tags = {resource_id: new.get("self_decided") for _type, resource_id, _old, new in pay_audit(audit_events, verb)}
        assert tags[str(anvar.id)] is True, verb
        assert tags.get(str(world.agent.id)) is None, verb


def test_the_drill_down_names_what_it_cannot_find_and_clamps_its_pages(make_world, manager_claim_headers):
    """A2/A8/A9 refusals and paging: a malformed month, a month with no period, an agent with no
    statement in a closed month; `per_page` clamps to 100 and `page` to 1; an unknown `kind`
    returns no rows; a manager is refused all three reads."""
    world = make_world()
    for day in (20, 21, 22):
        water_order(world, 6, date(2026, 10, day))
    move_clock(world, local_at(2026, 11, 2))
    pay_route(world, "POST", "/periods/2026-10/close")
    agent = world.agent.id
    lines = f"/periods/2026-10/agents/{agent}/lines"

    bad_month = pay_route(world, "GET", f"/periods/2026-13/agents/{agent}", status=400)
    future = pay_route(world, "GET", "/periods/2027-03", status=404)
    no_statement = pay_route(world, "GET", f"/periods/2026-10/agents/{world.admin.id}", status=404)
    second_page = pay_route(world, "GET", f"{lines}?per_page=2&page=2")
    unknown_kind = pay_route(world, "GET", f"{lines}?kind=bogus")
    clamped = pay_route(world, "GET", f"{lines}?per_page=500&page=-3")
    credits = pay_route(world, "GET", f"{lines}?kind=commission_credit")
    statement = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")
    manager = [
        world.client.get(f"{PAY_API}{path}", headers=manager_claim_headers).status_code
        for path in ("/periods/2026-10", f"/periods/2026-10/agents/{agent}", lines)
    ]

    assert (bad_month["error_code"], bad_month["details"]) == (
        "SALES_PAY_MONTH_INVALID",
        {"month": "2026-13", "validation_errors": []},
    )
    assert (future["error_code"], future["details"]) == ("SALES_PAY_NOT_FOUND", {"resource": "period", "id": "2027-03"})
    assert (no_statement["error_code"], no_statement["details"]) == (
        "SALES_PAY_NOT_FOUND",
        {"resource": "statement", "id": world.admin.id},
    )
    assert (len(second_page["items"]), second_page["meta"]["page"], second_page["meta"]["total"]) == (1, 2, 3)
    assert (unknown_kind["items"], unknown_kind["kind"], unknown_kind["meta"]["total"]) == ([], "bogus", 0)
    assert (clamped["meta"]["page"], clamped["meta"]["per_page"], len(clamped["items"])) == (1, 100, 3)
    assert credits["line_kinds"] == LINE_KINDS
    assert [(item["row"], [e["kind"] for e in item["events"]]) for item in credits["items"]] == [
        ("order", ["commission_credit"])
    ] * 3
    assert statement["line_counts"] == {
        "commission_credit": 3,
        "commission_reversal": 0,
        "commission_difference": 0,
        "new_outlet_bonus": 0,
    }
    assert (statement["line_kinds"], statement["summary"]["commission"]["orders"]) == (LINE_KINDS, 3)
    assert statement["day_statuses"] == ["worked", "non_working", "holiday", "unpaid", "not_employed"]
    assert statement["not_counted_reasons"] == [
        "not_visited", "not_completed", "no_checkin", "skipped", "out_of_range", "no_location", "short"
    ]
    assert manager == [403, 403, 403]
