"""A0–A7 and the month lifecycle, driven through the admin pay routes (§4.9, §5.2).

Pay is configured the way an admin configures it (`pay_world`: A13, A18, A17, A0). Orders come
from Task 5's builders (the real order, delivery and cash writers) and plan days from Task 2's,
and every step moves the frozen clock. Months then move only through A3 close, A4 recalculate,
A5 approve and A6 mark paid.

Carry and owed are produced here with A11 adjustments, the one pay input Task 7 owns; Task 8
re-drives the same approve steps with A23 penalties (T-CARRY-*, T-OWED-*).
"""

from datetime import date
from decimal import Decimal

import pytest

from business_app.models.sales_pay import SalesPayPeriod, SalesPayPlanVersion
from business_app.services.sales import pay_ledger_service
from business_app.tasks.sales_agent_tasks import sync_pay_ledger
from tests.integration.sales_pay_builders import (
    NOV,
    NOVEMBER_WORKING,
    OCT,
    PAY_API,
    TEACHERS_DAY,
    adjust_cash,
    below_zero_october,
    carry_rows,
    collect_cash_at,
    configure_agent,
    freeze_local_now,
    frozen_statements,
    ledger_lines,
    live_cash_events,
    local_at,
    move_clock,
    pay_agent,
    pay_audit,
    pay_call,
    pay_route,
    pay_store,
    pay_world,
    publish_version,
    utc_at,
    version_payload,
    water_order,
)
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture

pytestmark = pytest.mark.integration


@pytest.fixture
def october(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user):
    """Pay started for October 2026 (not shadow): base 3,000,000, 19 L at 1,500, employed from the 1st."""
    return pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)


# --------------------------------------------------------------------------- #
# Start, list, detail
# --------------------------------------------------------------------------- #


def test_start_opens_the_first_month_and_the_list_publishes_it(
    client, db, monkeypatch, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """A0 before anything else: the list says pay has not started and which month may start it;
    a start for another month is refused; the start publishes the open month's detail (its agent
    has no terms yet, so it is listed as unconfigured); a second start is refused."""
    freeze_local_now(monkeypatch, local_at(2026, 10, 1, 9))

    before = pay_call(client, "GET", f"{PAY_API}/periods", admin_claim_headers)
    wrong = pay_call(
        client, "POST", f"{PAY_API}/start", admin_claim_headers, {"month": "2026-09", "is_shadow": False}, status=400
    )
    detail = pay_call(
        client, "POST", f"{PAY_API}/start", admin_claim_headers, {"month": "2026-10", "is_shadow": False}, status=201
    )
    again = pay_call(
        client, "POST", f"{PAY_API}/start", admin_claim_headers, {"month": "2026-10", "is_shadow": False}, status=409
    )
    listed = pay_call(client, "GET", f"{PAY_API}/periods", admin_claim_headers)
    manager = client.get(f"{PAY_API}/periods", headers=manager_claim_headers)

    assert (before["started"], before["startable_month"], before["items"]) == (False, "2026-10", [])
    assert wrong["error_code"] == "SALES_PAY_MONTH_INVALID"
    assert (detail["month"], detail["status"], detail["is_shadow"], detail["is_estimate"]) == (
        "2026-10",
        "open",
        False,
        True,
    )
    assert (detail["start_date"], detail["end_date"], detail["calendar"]["working_days"]) == (
        "2026-10-01",
        "2026-10-31",
        31,
    )
    assert (detail["agents"], detail["can_edit_inputs"]) == ([], True)
    assert detail["unconfigured_agents"] == [{"agent_user_id": sales_agent_user.id, "agent_name": "Sardor Agent"}]
    assert again["error_code"] == "SALES_PAY_ALREADY_STARTED"
    assert (listed["started"], listed["startable_month"]) == (True, None)
    assert [(row["month"], row["next_actions"]) for row in listed["items"]] == [("2026-10", ["recalculate"])]
    assert listed["statuses"] == ["open", "closed", "approved", "paid"]
    assert listed["actions"] == ["close", "recalculate", "approve", "mark_paid"]
    assert listed["as_of"] is not None
    assert manager.status_code == 403


def test_a_month_closes_only_once_its_visits_can_no_longer_move(october):
    """closable_from = the month's end (31 Oct 19:00Z) + SALES_VISIT_AUTO_ABANDON_HOURS (6) + 1 h
    = 1 Nov 02:00Z, 07:00 local. Before it the row offers no Close and A3 refuses naming the
    instant; from it both agree."""
    move_clock(october, local_at(2026, 11, 1, 6, 59))
    row = pay_route(october, "GET", "/periods/2026-10")
    early = pay_route(october, "POST", "/periods/2026-10/close", status=409)
    move_clock(october, local_at(2026, 11, 1, 7, 0))
    ready = pay_route(october, "GET", "/periods/2026-10")
    closed = pay_route(october, "POST", "/periods/2026-10/close")

    assert (row["closable_from"], "close" in row["next_actions"]) == ("2026-11-01T02:00:00+00:00", False)
    assert (early["error_code"], early["details"]["closable_from"]) == (
        "SALES_PAY_PERIOD_NOT_ENDED",
        "2026-11-01T02:00:00+00:00",
    )
    assert ready["next_actions"] == ["close", "recalculate"]
    assert (closed["status"], closed["is_estimate"], closed["can_edit_inputs"]) == ("closed", False, True)


def test_close_syncs_first_so_a_last_minute_order_lands_in_its_own_month(october):
    """T-LATE-2: delivered and paid at 23:50 local on 31 October (18:50Z), with no nightly run and
    no read in between. The close's own forced sync credits it into October, not late, and
    creates November on the way."""
    order = water_order(october, 6, date(2026, 10, 31), at=(23, 50))
    move_clock(october, local_at(2026, 11, 2, 10))

    pay_route(october, "POST", "/periods/2026-10/close")
    lines = pay_route(october, "GET", f"/periods/2026-10/agents/{october.agent.id}/lines")
    periods = pay_route(october, "GET", "/periods")

    assert [
        (i["row"], i["order_id"], [e["kind"] for e in i["events"]], i["earned_month"], i["is_late"], i["amount"])
        for i in lines["items"]
    ] == [("order", order.id, ["commission_credit"], "2026-10", False, 9000.0)]
    assert [row["month"] for row in periods["items"]] == ["2026-11", "2026-10"]


def test_a_closed_month_is_approved_then_marked_paid_and_each_step_is_audited(october, audit_events):
    """close → approve → mark paid, with the stamps and actors on each row, the refusals of a
    second approve and of a paid date outside [month start, today], and one audit event per step.

    October: base 3,000,000 (31 of 31 days) + 6 × 19 L at 1,500 = 9,000 at ×1.0 (nothing due)
    = 3,009,000."""
    water_order(october, 6, date(2026, 10, 20))
    move_clock(october, local_at(2026, 11, 2, 10))

    closed = pay_route(october, "POST", "/periods/2026-10/close")
    approved = pay_route(october, "POST", "/periods/2026-10/approve")
    again = pay_route(october, "POST", "/periods/2026-10/approve", status=409)
    too_early = pay_route(october, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-09-30"}, status=400)
    future = pay_route(october, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-11-03"}, status=400)
    paid = pay_route(october, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-11-02"})

    [row] = closed["agents"]
    assert (closed["status"], closed["closed_by"]["id"]) == ("closed", october.admin.id)
    assert (row["revision"], row["commission"], row["gated_commission"], row["total"]) == (1, 9000.0, 9000.0, 3009000.0)
    assert (approved["status"], approved["approved_by"]["id"], approved["next_actions"]) == (
        "approved",
        october.admin.id,
        ["mark_paid"],
    )
    assert again["error_code"] == "SALES_PAY_STATE_INVALID"
    assert (too_early["error_code"], too_early["details"]["field"], too_early["details"]["reason"]) == (
        "SALES_PAY_DATE_INVALID",
        "paid_on",
        "before_month",
    )
    assert (future["error_code"], future["details"]["reason"]) == ("SALES_PAY_DATE_INVALID", "future")
    assert (paid["status"], paid["paid_on"], paid["paid_by"]["id"], paid["next_actions"]) == (
        "paid",
        "2026-11-02",
        october.admin.id,
        [],
    )
    period_id = str(SalesPayPeriod.query.filter_by(month_start=OCT).one().id)
    assert pay_audit(audit_events, "period_closed") == [
        (
            "sales_pay_period",
            period_id,
            {"status": "open"},
            {"month": "2026-10", "status": "closed", "statements": 1, "actor_user_id": october.admin.id},
        )
    ]
    assert pay_audit(audit_events, "period_approved") == [
        (
            "sales_pay_period",
            period_id,
            {"status": "closed"},
            {"month": "2026-10", "status": "approved", "carries": 0, "actor_user_id": october.admin.id},
        )
    ]
    assert pay_audit(audit_events, "period_paid") == [
        (
            "sales_pay_period",
            period_id,
            {"status": "approved"},
            {"month": "2026-10", "status": "paid", "paid_on": "2026-11-02", "actor_user_id": october.admin.id},
        )
    ]


def test_months_close_and_approve_in_month_order(october):
    """T-ORDER-1: on 2 December October and November are both open. November offers no Close and
    A3 refuses it while October is open; once both are closed, November offers no Approve and A5
    refuses it until October is approved."""
    move_clock(october, local_at(2026, 12, 2, 10))

    listed = pay_route(october, "GET", "/periods")
    refused_close = pay_route(october, "POST", "/periods/2026-11/close", status=409)
    pay_route(october, "POST", "/periods/2026-10/close")
    pay_route(october, "POST", "/periods/2026-11/close")
    both_closed = pay_route(october, "GET", "/periods/2026-11")
    refused_approve = pay_route(october, "POST", "/periods/2026-11/approve", status=409)
    pay_route(october, "POST", "/periods/2026-10/approve")
    ready = pay_route(october, "GET", "/periods/2026-11")
    approved = pay_route(october, "POST", "/periods/2026-11/approve")

    november = next(row for row in listed["items"] if row["month"] == "2026-11")
    assert "close" not in november["next_actions"]
    assert (refused_close["error_code"], refused_close["details"]["month"]) == (
        "SALES_PAY_PREVIOUS_PERIOD_OPEN",
        "2026-10",
    )
    assert "approve" not in both_closed["next_actions"]
    assert (refused_approve["error_code"], refused_approve["details"]["month"]) == (
        "SALES_PAY_PREVIOUS_PERIOD_NOT_APPROVED",
        "2026-10",
    )
    assert "approve" in ready["next_actions"]
    assert approved["status"] == "approved"


def test_a_deactivated_agent_is_still_frozen_and_days_without_a_plan_row_are_tolerated(october, client):
    """T-BASE-2: the profile is switched off on 20 October; the close still freezes their October
    (terms in force and employment overlapping the month, §4.8). Their worked days never got a
    day-plan row, so each is `plan_source: "none"` with nothing due and the gate is ×1.0."""
    move_clock(october, local_at(2026, 10, 20, 10))
    pay_call(
        client, "PUT", f"/api/v1/admin/staff/sales-agents/{october.agent.id}/active", october.headers,
        {"is_active": False},
    )
    move_clock(october, local_at(2026, 11, 2, 10))

    closed = pay_route(october, "POST", "/periods/2026-10/close")
    statement = pay_route(october, "GET", f"/periods/2026-10/agents/{october.agent.id}")

    assert [(a["agent_user_id"], a["revision"], a["base_amount"]) for a in closed["agents"]] == [
        (october.agent.id, 1, 3000000.0)
    ]
    assert {d["plan_source"] for d in statement["days"] if d["day_status"] == "worked"} == {"none"}
    assert all(d["due"] is None and d["counted"] == 0 for d in statement["days"])
    assert (statement["summary"]["gate"]["rule"], statement["summary"]["gate"]["visits_due"]) == ("below_min_due", 0)


# --------------------------------------------------------------------------- #
# The close refuses an unfinished ledger and an unconfigured agent
# --------------------------------------------------------------------------- #


def test_one_order_the_sync_cannot_post_keeps_the_month_open(october):
    """T-SYNC-2, the A3 half: one order's commission basis raises. The other order still posts in
    its own SAVEPOINT, A3 answers SALES_PAY_SYNC_INCOMPLETE naming the order and the agent, and
    October stays open. With the fault gone the next close posts it (4 × 1,500 = 6,000) and
    succeeds."""
    good = water_order(october, 6, date(2026, 10, 20))
    broken = water_order(october, 4, date(2026, 10, 21))
    move_clock(october, local_at(2026, 11, 2, 10))
    real_basis = pay_ledger_service.basis_inputs

    def basis(order):
        if order.id == broken.id:
            raise RuntimeError("basis unavailable for this order")
        return real_basis(order)

    with october.monkeypatch.context() as patched:
        patched.setattr(pay_ledger_service, "basis_inputs", basis)
        refused = pay_route(october, "POST", "/periods/2026-10/close", status=409)
        still_open = pay_route(october, "GET", "/periods/2026-10")
    posted_meanwhile = [line.order_id for line in ledger_lines(good) + ledger_lines(broken)]
    closed = pay_route(october, "POST", "/periods/2026-10/close")

    assert (refused["error_code"], refused["details"]["orders"], refused["details"]["agents"]) == (
        "SALES_PAY_SYNC_INCOMPLETE",
        [broken.id],
        [october.agent.id],
    )
    assert still_open["status"] == "open"
    assert posted_meanwhile == [good.id]
    assert closed["status"] == "closed"
    assert [(line.kind, line.amount, line.snapshot["received_applied"]) for line in ledger_lines(broken)] == [
        ("commission_credit", None, "80000.00")
    ]
    assert {row["agent_user_id"]: row["commission"] for row in closed["agents"]}[october.agent.id] == 15000.0  # 9,000 + 4 × 1,500


def test_an_agent_without_october_terms_blocks_the_close_until_terms_are_added(october, db, client):
    """T-TERMS-1 (sync, A3 and A17 halves): Bekzod is hired on 6 October with terms only from
    November, so the sync leaves his October order in `skipped_no_terms`, the month lists him as
    unconfigured, and A3 refuses naming him. A17 effective October (still open) lets the next
    close's sync credit the order, and the close goes through with his statement."""
    newcomer = pay_agent(db, phone="+998901230712", first_name="Bekzod")
    configure_agent(
        client, october.headers, newcomer, plan_id=october.plan["id"], base=2500000,
        effective_month="2026-11", employment_start="2026-10-06",
    )
    order = water_order(october, 6, date(2026, 10, 20), agent=newcomer)
    move_clock(october, local_at(2026, 10, 25, 10))
    pay_route(october, "POST", "/periods/2026-10/recalculate")
    detail = pay_route(october, "GET", "/periods/2026-10")
    move_clock(october, local_at(2026, 11, 2, 10))

    refused = pay_route(october, "POST", "/periods/2026-10/close", status=409)
    pay_call(
        client, "POST", f"{PAY_API}/agents/{newcomer.id}/terms", october.headers,
        {"effective_month": "2026-10", "base_salary": "2500000", "plan_id": october.plan["id"]}, status=201,
    )
    closed = pay_route(october, "POST", "/periods/2026-10/close")

    assert detail["sync"]["stats"]["skipped_no_terms"] == [order.id]
    assert detail["unconfigured_agents"] == [{"agent_user_id": newcomer.id, "agent_name": "Bekzod Agent"}]
    assert (refused["error_code"], refused["details"]["agents"]) == ("SALES_PAY_TERMS_MISSING", [newcomer.id])
    assert sorted(a["agent_user_id"] for a in closed["agents"]) == sorted([october.agent.id, newcomer.id])
    assert [(line.kind, line.amount, line.period.month_start) for line in ledger_lines(order)] == [
        ("commission_credit", None, OCT)
    ]
    assert {row["agent_user_id"]: row["commission"] for row in closed["agents"]}[newcomer.id] == 9000.0


# --------------------------------------------------------------------------- #
# What a closed and an approved month still accept
# --------------------------------------------------------------------------- #


def test_an_approved_month_refuses_every_input(october):
    """T-LOCK-1 without its penalty items (Task 8's): once October is approved, A3 and A4 are
    STATE_INVALID, and A7, A10, A11 and an A15 version effective October are MONTH_LOCKED; the
    nightly job changes nothing in the frozen statement."""
    water_order(october, 6, date(2026, 10, 20))
    move_clock(october, local_at(2026, 11, 2, 10))
    pay_route(october, "POST", "/periods/2026-10/close")
    pay_route(october, "POST", "/periods/2026-10/approve")
    agent = october.agent.id
    frozen = pay_route(october, "GET", f"/periods/2026-10/agents/{agent}")

    refusals = {
        "A3": pay_route(october, "POST", "/periods/2026-10/close", status=409)["error_code"],
        "A4": pay_route(october, "POST", "/periods/2026-10/recalculate", status=409)["error_code"],
        "A7": pay_route(october, "PUT", "/periods/2026-10/holidays", TEACHERS_DAY, status=409)["error_code"],
        "A10": pay_route(
            october, "PUT", f"/periods/2026-10/agents/{agent}/unpaid-days", {"days": [{"date": "2026-10-12"}]},
            status=409,
        )["error_code"],
        "A11": pay_route(
            october, "POST", f"/periods/2026-10/agents/{agent}/adjustments",
            {"amount": 5000, "reason": "Late correction"}, status=409,
        )["error_code"],
        "A15": pay_route(
            october, "POST", f"/plans/{october.plan['id']}/versions",
            version_payload("2026-10", october.products.water.id), status=409,
        )["error_code"],
    }
    sync_pay_ledger.run()
    after = pay_route(october, "GET", f"/periods/2026-10/agents/{agent}")

    assert refusals == {
        "A3": "SALES_PAY_STATE_INVALID",
        "A4": "SALES_PAY_STATE_INVALID",
        "A7": "SALES_PAY_MONTH_LOCKED",
        "A10": "SALES_PAY_MONTH_LOCKED",
        "A11": "SALES_PAY_MONTH_LOCKED",
        "A15": "SALES_PAY_MONTH_LOCKED",
    }
    assert after == frozen


def test_a_closed_month_stays_editable_and_its_change_cascades_into_the_next_closed_month(october):
    """T-LOCK-2: October and November are both closed. An A11 into October re-freezes October
    (revision 2: 3,000,000 + 9,000 + 50,000 = 3,059,000) and, because November reads October's gate
    for any October-earned line, November too (revision 2). A15 for either closed month is refused
    with no version row, and A6 before approval is a STATE_INVALID."""
    water_order(october, 6, date(2026, 10, 20))
    move_clock(october, local_at(2026, 11, 2, 10))
    pay_route(october, "POST", "/periods/2026-10/close")
    water_order(october, 4, date(2026, 11, 10))
    move_clock(october, local_at(2026, 12, 2, 10))
    pay_route(october, "POST", "/periods/2026-11/close")
    agent = october.agent.id
    versions_before = SalesPayPlanVersion.query.count()

    added = pay_route(
        october, "POST", f"/periods/2026-10/agents/{agent}/adjustments",
        {"amount": 50000, "reason": "Guarantee top-up"}, status=201,
    )
    october_statement = pay_route(october, "GET", f"/periods/2026-10/agents/{agent}")
    november_statement = pay_route(october, "GET", f"/periods/2026-11/agents/{agent}")
    locked = [
        pay_route(
            october, "POST", f"/plans/{october.plan['id']}/versions",
            version_payload(month, october.products.water.id), status=409,
        )["error_code"]
        for month in ("2026-10", "2026-11")
    ]
    unpaid = pay_route(october, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-12-02"}, status=409)

    assert added["adjustment"]["posting_month"] == "2026-10"
    assert (
        october_statement["revision"],
        october_statement["summary"]["adjustments"],
        october_statement["summary"]["total"],
    ) == (2, 50000.0, 3059000.0)
    assert november_statement["revision"] == 2
    assert locked == ["SALES_PAY_MONTH_LOCKED", "SALES_PAY_MONTH_LOCKED"]
    assert SalesPayPlanVersion.query.count() == versions_before
    assert unpaid["error_code"] == "SALES_PAY_STATE_INVALID"


def test_a_shadow_months_late_lines_count_nowhere_else_and_it_is_never_paid(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """T-SHADOW-1, the lifecycle half: October is the shadow month. After its approval a November
    reversal of an October credit (cash corrected to 0 on 3 November) and a late October credit
    (paid on 31 October, recorded on 4 November) are posted to November; November computes their
    group 10.2026 (the reversal −9,000, the late credit +9,000 in the same units: a change of 0)
    with `counted: false`, and it adds 0 there (T-TIER-16); October's own statement counts its
    credit; A6 on October is refused with reason "shadow" and Mark paid is never offered.

    The late payment goes through Task 6's `collect_cash_at` (`CashCollectionService.post_collection`,
    the builders' backdating writer; it pays the whole 120,000): the admin collections route passes
    `occurred_at` through unparsed, so it cannot backdate. The late order is for a second shop: the
    cash writer settles a customer's OLDEST delivered debt first, which on one shop would be the
    credited order whose cash was just corrected to 0."""
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, is_shadow=True)
    credited = water_order(world, 6, date(2026, 10, 20))
    chorsu = pay_store(db, world.agent, name="Chorsu", onboarded=local_at(2026, 10, 1, 9))
    late = water_order(world, 6, date(2026, 10, 30), paid=False, store=chorsu)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    approved = pay_route(world, "POST", "/periods/2026-10/approve")
    move_clock(world, local_at(2026, 11, 3, 10))
    adjust_cash(world, live_cash_events(credited)[0].id, 0)
    move_clock(world, local_at(2026, 11, 4, 10))
    collect_cash_at(db, late, at=utc_at(2026, 10, 31, 12), actor=admin_user)
    pay_route(world, "POST", "/periods/2026-11/recalculate")
    agent = world.agent.id

    november_lines = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}/lines")
    november = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")
    shadow = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")
    refused = pay_route(world, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-11-04"}, status=409)

    assert sorted(
        (i["order_id"], [e["kind"] for e in i["events"]], i["earned_month"], i["is_late"], i["counted"], i["amount"])
        for i in november_lines["items"]
    ) == sorted(
        [
            (credited.id, ["commission_reversal"], "2026-10", True, False, -9000.0),
            (late.id, ["commission_credit"], "2026-10", True, False, 9000.0),
        ]
    )
    (shadow_group,) = november["summary"]["late"]
    assert (shadow_group["earned_month"], shadow_group["counted"], shadow_group["gross"], shadow_group["after_gate"]) == (
        "2026-10", False, 0.0, 0.0,
    )
    assert november["summary"]["gated_commission"] == 0.0
    assert (shadow["is_shadow"], shadow["summary"]["commission"]["gross"], shadow["summary"]["gated_commission"]) == (
        True,
        9000.0,
        9000.0,
    )
    assert approved["next_actions"] == []
    assert (refused["error_code"], refused["details"]["reason"]) == ("SALES_PAY_STATE_INVALID", "shadow")


def test_a_later_plan_version_leaves_a_closed_month_on_its_own_version(october, audit_events):
    """T-PLAN-1 and T-DIFF-2's closed half: October is closed on v1 (19 L at 1,500). A version
    effective October is refused with MONTH_LOCKED and writes no row. A15 publishes v2 effective
    November (19 L at 2,000). Recalculating October keeps v1 in its statement; November's six
    bottles earn 6 × 2,000 = 12,000 on v2, and its estimate names v2."""
    water_order(october, 6, date(2026, 10, 20))
    move_clock(october, local_at(2026, 11, 2, 10))
    pay_route(october, "POST", "/periods/2026-10/close")
    v1 = october.plan["version_in_force"]["id"]
    versions_before = SalesPayPlanVersion.query.count()
    refused = pay_route(
        october, "POST", f"/plans/{october.plan['id']}/versions",
        version_payload("2026-10", october.products.water.id, water_rate=2000), status=409,
    )
    versions_after_refusal = SalesPayPlanVersion.query.count()
    v2 = publish_version(october, "2026-11", rate_19l=2000)
    pay_route(october, "POST", "/periods/2026-10/recalculate")
    agent = october.agent.id
    october_statement = pay_route(october, "GET", f"/periods/2026-10/agents/{agent}")
    november_order = water_order(october, 6, date(2026, 11, 10))
    move_clock(october, local_at(2026, 11, 12, 10))
    pay_route(october, "POST", "/periods/2026-11/recalculate")
    november_statement = pay_route(october, "GET", f"/periods/2026-11/agents/{agent}")

    assert (refused["error_code"], versions_after_refusal) == ("SALES_PAY_MONTH_LOCKED", versions_before)
    assert (
        october_statement["revision"],
        october_statement["plan"]["version_id"],
        october_statement["summary"]["commission"]["gross"],
    ) == (2, v1, 9000.0)
    assert (november_statement["plan"]["version_id"], november_statement["plan"]["version_no"]) == (v2["id"], 2)
    assert [(line.amount, line.plan_version_id) for line in ledger_lines(november_order)] == [(None, v2["id"])]
    assert november_statement["summary"]["commission"]["gross"] == 12000.0  # 6 × 2,000 on v2's tier
    # A4 on a closed month re-freezes it (one statement); on an open month it only syncs.
    assert [(new["month"], new["status"], new["statements"]) for *_head, new in pay_audit(
        audit_events, "period_recalculated"
    )] == [("2026-10", "closed", 1), ("2026-11", "open", 0)]


# --------------------------------------------------------------------------- #
# Approve: the carry row, the owed balance and its netting (§4.9 steps 1–5)
# --------------------------------------------------------------------------- #


def test_approve_posts_the_shortfall_into_next_month_once_and_a_month_still_below_zero_carries_again(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """C1, approve step 3 (the adjustment-driven half of T-CARRY-1/2). October pays 0 and carries
    −100,000. A5 posts exactly one carry_forward row into November (its reason a system literal,
    its actor the approving admin); November's statement brings it in: 3,000,000 − 100,000 =
    2,900,000. A second A5 is refused and posts nothing. The chain: a November with every working
    day unpaid has gross −100,000 and carries it on into December."""
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, base=3000000)
    below_zero_october(world)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    agent = world.agent.id

    october = pay_route(world, "GET", f"/periods/2026-10/agents/{agent}")
    pay_route(world, "POST", "/periods/2026-10/approve")
    again = pay_route(world, "POST", "/periods/2026-10/approve", status=409)
    november = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")
    carries_after_october = [
        (c.agent_user_id, c.amount, c.period.month_start, c.carried_from_period.month_start, c.reason, c.created_by_user_id)
        for c in carry_rows()
    ]

    summary = october["summary"]
    assert (summary["base_amount"], summary["variable"], summary["gross_total"]) == (200000.0, -300000.0, -100000.0)
    assert (summary["total"], summary["carry_out"], summary["owed"]) == (0.0, -100000.0, 0.0)
    assert again["error_code"] == "SALES_PAY_STATE_INVALID"
    assert carries_after_october == [(agent, Decimal("-100000.00"), NOV, OCT, "carry_forward 2026-10", admin_user.id)]
    assert [
        (a["source"], a["carried_from_month"], a["posting_month"], a["amount"]) for a in november["adjustments"]
    ] == [("carry_forward", "2026-10", "2026-11", -100000.0)]
    assert november["summary"]["carry_in"] == {"amount": -100000.0, "from_month": "2026-10", "source": "carry_forward"}
    assert (
        november["summary"]["adjustments"],
        november["summary"]["base_amount"],
        november["summary"]["gross_total"],
    ) == (0.0, 3000000.0, 2900000.0)

    pay_route(world, "PUT", f"/periods/2026-11/agents/{agent}/unpaid-days", {"days": [{"date": d} for d in NOVEMBER_WORKING]})
    move_clock(world, local_at(2026, 12, 2, 10))
    pay_route(world, "POST", "/periods/2026-11/close")
    pay_route(world, "POST", "/periods/2026-11/approve")
    december = pay_route(world, "GET", f"/periods/2026-12/agents/{agent}")

    assert december["summary"]["carry_in"] == {"amount": -100000.0, "from_month": "2026-11", "source": "carry_forward"}
    assert len(carry_rows()) == 2


def test_a_leavers_shortfall_is_owed_and_the_next_statement_nets_it(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """C1, I-26, I-28 (the adjustment-driven half of T-OWED-1): the same October for an agent whose
    employment ends on 31 October owes 100,000 and posts no carry. A16 publishes the balance and
    the month that nets it. November, the first unapproved month, nets it: a +50,000 adjustment
    leaves gross 0 + 50,000 − 100,000 = −50,000, paid 0, owed 50,000, and the frozen statement
    points at October's. Once November is approved the balance is 50,000, never 150,000."""
    world = pay_world(
        db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, base=3000000,
        employment_end="2026-10-31",
    )
    below_zero_october(world)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    pay_route(world, "POST", "/periods/2026-10/approve")
    agent = world.agent.id

    owing = pay_route(world, "GET", f"/agents/{agent}/terms")
    pay_route(
        world, "POST", f"/periods/2026-11/agents/{agent}/adjustments",
        {"amount": 50000, "reason": "October orders paid late"}, status=201,
    )
    estimate = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")
    move_clock(world, local_at(2026, 12, 2, 10))
    pay_route(world, "POST", "/periods/2026-11/close")
    frozen = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")
    statements = frozen_statements(agent)
    pay_route(world, "POST", "/periods/2026-11/approve")
    settled = pay_route(world, "GET", f"/agents/{agent}/terms")

    assert carry_rows() == []
    assert owing["owed_to_date"] == {"amount": 100000.0, "month": "2026-10", "nets_in": "2026-11"}
    expected = {
        "base_amount": 0.0,
        "variable": 50000.0,
        "carry_in": {"amount": -100000.0, "from_month": "2026-10", "source": "owed"},
        "gross_total": -50000.0,
        "total": 0.0,
        "carry_out": 0.0,
        "owed": 50000.0,
    }
    for summary in (estimate["summary"], frozen["summary"]):
        assert {key: summary[key] for key in expected} == expected
    assert [a["source"] for a in frozen["adjustments"]] == ["admin"]
    assert statements[NOV].owed_in_statement_id == statements[OCT].id
    assert settled["owed_to_date"] == {"amount": 50000.0, "month": "2026-11", "nets_in": "2026-12"}


def test_approving_a_month_refreezes_the_closed_month_after_it_to_net_the_new_balance(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """Approve step 5 (T-OWED-3 (a)'s mechanics): October (owed 100,000) and November (+50,000) are
    both closed before October is approved, so November first nets nothing (total 50,000). A5 on
    October re-freezes November in the same transaction to net October's balance (revision 2,
    gross −50,000, paid 0, owed 50,000).
    Then step 1's guard: with November's pointer cleared behind the service's back, A5 on
    November is a 500 and nothing is approved or posted."""
    world = pay_world(
        db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, base=3000000,
        employment_end="2026-10-31",
    )
    below_zero_october(world)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    agent = world.agent.id
    pay_route(
        world, "POST", f"/periods/2026-11/agents/{agent}/adjustments",
        {"amount": 50000, "reason": "October orders paid late"}, status=201,
    )
    move_clock(world, local_at(2026, 12, 2, 10))
    pay_route(world, "POST", "/periods/2026-11/close")

    before = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")
    pay_route(world, "POST", "/periods/2026-10/approve")
    after = pay_route(world, "GET", f"/periods/2026-11/agents/{agent}")

    assert (before["revision"], before["summary"]["carry_in"], before["summary"]["total"]) == (
        1,
        {"amount": 0.0, "from_month": None, "source": None},
        50000.0,
    )
    assert (after["revision"], after["summary"]["carry_in"]) == (
        2,
        {"amount": -100000.0, "from_month": "2026-10", "source": "owed"},
    )
    assert (after["summary"]["gross_total"], after["summary"]["total"], after["summary"]["owed"]) == (
        -50000.0,
        0.0,
        50000.0,
    )

    statements = frozen_statements(agent)
    statements[NOV].owed_in_statement_id = None
    db.session.commit()
    response = client.post(f"{PAY_API}/periods/2026-11/approve", json={}, headers=world.headers)

    db.session.expire_all()
    assert response.status_code == 500
    assert SalesPayPeriod.query.filter_by(month_start=NOV).one().status == "closed"
    assert carry_rows() == []


def test_a_shadow_month_below_zero_pays_0_and_carries_nothing(
    db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """I-27: the shadow October with the same figures shows gross −100,000 and pays 0, and its
    approval posts no carry and leaves nothing owed."""
    world = pay_world(
        db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, base=3000000, is_shadow=True
    )
    below_zero_october(world)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    pay_route(world, "POST", "/periods/2026-10/approve")

    summary = pay_route(world, "GET", f"/periods/2026-10/agents/{world.agent.id}")["summary"]

    assert (summary["gross_total"], summary["total"], summary["carry_out"], summary["owed"]) == (
        -100000.0,
        0.0,
        0.0,
        0.0,
    )
    assert carry_rows() == []
