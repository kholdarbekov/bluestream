"""The agent's own approved statements: S3 `GET /staff/sales/me/statements` and S4 `.../statements/<month>`.

S3 lists the agent's approved-or-paid months, newest first, at most twelve; S4 opens one of them in
full. Both read the rows S1's last statement reads (`latest_approved_statement`'s selection), and
S4's `statement` is `_agent_estimate` of the frozen row: S1's estimate shape with A8's figures, so
the bot's statement screen and the admin drawer cannot disagree. S4's `is_latest`, `owed` and
`owed_netted_in` are `_last_statement`'s for the same row, so the bot prints the balance note S1
prints, and only for the latest statement: an older one's `owed` and `carry_out` are history, since
netted or carried by a later statement. `owed_netted_in` is never set unless `is_latest` is, and
every S4 answer here is checked for it.

A closed month is under review and shows no numbers (I-14): S3 never lists it, and S4 refuses it,
an open month and a month with no period alike with SALES_PAY_STATEMENT_NOT_AVAILABLE. Two routes,
no agent id: the token decides whose pay it is, as for S1 and S2 (§5.6).
"""

import inspect
from datetime import date

import pytest

from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_statement_service import SalesPayStatementService
from business_app.utils import local_windows
from tests.integration.sales_pay_builders import (
    add_penalty_type,
    carry_case,
    claim_headers,
    close_and_approve,
    configure_agent,
    incident_body,
    ledger_lines,
    local_at,
    make_outlet,
    move_clock,
    october_world,
    pay_agent,
    pay_call,
    pay_route,
    pay_world,
    plan_days,
    refusal,
    utc_at,
    water_order,
)
from tests.integration.test_sales_pay_agent_earnings_api import LINES, NOTHING_CARRIED, lapse_throttle, my_earnings

pytestmark = pytest.mark.integration

STATEMENTS = "/api/v1/staff/sales/me/statements"
NOT_AVAILABLE = "SALES_PAY_STATEMENT_NOT_AVAILABLE"
# S4's head: the part of `_last_statement`'s line the statement screen prints above the rows.
HEAD = ("month", "status", "paid_on", "is_shadow", "is_latest", "owed", "owed_netted_in")


# ---------------------------------------------------------------------------- helpers


def my_statements(client, headers) -> dict:
    """S3 as the bot calls it (no query string)."""
    return pay_call(client, "GET", STATEMENTS, headers)


def my_statement(client, headers, month: str) -> dict:
    """S4 as the bot calls it, for a month S1, S3 or the approval push gave it. Every answer holds
    the netting invariant: only the latest statement's balance can be in deduction
    (`outstanding_owed`)."""
    answer = pay_call(client, "GET", f"{STATEMENTS}/{month}", headers)
    assert answer["owed_netted_in"] is None or answer["is_latest"] is True, answer
    return answer


def latest_flags(client, headers) -> list:
    """S3's months, newest first, each with S4's `is_latest` for it."""
    return [
        (item["month"], my_statement(client, headers, item["month"])["is_latest"])
        for item in my_statements(client, headers)["items"]
    ]


def not_available(client, headers, month: str) -> None:
    """S4's one refusal: the month holds no approved or paid statement of the caller's."""
    assert refusal(client, "GET", f"{STATEMENTS}/{month}", headers, status=404, code=NOT_AVAILABLE) == {
        "month": month
    }


def head(answer: dict) -> dict:
    return {key: answer[key] for key in HEAD}


def call_spy(monkeypatch, owner, name: str) -> list:
    """Record each call of `owner.<name>` as its bound arguments, then run the real one. The call
    is bound against the real signature first, so a call the service would refuse raises here."""
    real = getattr(owner, name)
    signature = inspect.signature(real)
    calls = []

    def spy(*args, **kwargs):
        calls.append(dict(signature.bind(*args, **kwargs).arguments))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, spy)
    return calls


def sync_spies(monkeypatch) -> dict:
    """`call_spy` on each call S4 makes only for the balance an estimate can net: the sync
    (`ensure_periods`, `ensure_fresh`) and S1's open-month estimates (`_open_estimates`)."""
    return {
        name: call_spy(monkeypatch, owner, name)
        for owner, name in (
            (SalesPayPeriodService, "ensure_periods"),
            (SalesPayLedgerService, "ensure_fresh"),
            (SalesPayStatementService, "_open_estimates"),
        )
    }


# ---------------------------------------------------------------------------- what S4 prints


def test_an_approved_trial_month_on_base_alone_is_listed_and_opened_in_full(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """The dev September shape: a trial (shadow) month with no credited order and one confirmed
    penalty, approved. Base 1,500,000 with 7 of September's 30 working days unpaid (24-30
    September): 1,500,000 x 23 / 30 = 1,150,000. 21 days each plan 2 outlets and none is
    visited: 0 of 42, 0.0 %, x0.5 on a commission of 0. The 100,000 penalty takes the total to
    1,050,000, and a trial month neither carries nor owes (I-27).

    S2 opens the same month's credited orders: approved, available, and nothing in it."""
    agent = sales_agent_user
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, agent, start="2026-09",
                      base=1500000, is_shadow=True)
    due = [make_outlet(db, onboarded_by=agent, created_at=utc_at(2026, 8, 1), assigned_agent=agent).id
           for _ in range(2)]
    plan_days(db, agent, [date(2026, 9, day) for day in range(1, 22)], due)
    pay_route(world, "PUT", f"/periods/2026-09/agents/{agent.id}/unpaid-days",
              {"days": [{"date": f"2026-09-{day}"} for day in range(24, 31)]})
    penalty_type_id = add_penalty_type(world, default_amount=100000)
    move_clock(world, local_at(2026, 9, 25, 10))
    pay_route(world, "POST", "/penalties", incident_body(agent, penalty_type_id, "2026-09-10"), status=201)
    close_and_approve(world, "2026-09", at=local_at(2026, 10, 2, 10))

    assert my_statements(client, sales_agent_auth_headers) == {
        "items": [{"month": "2026-09", "total": 1050000.0, "status": "approved", "paid_on": None, "is_shadow": True}],
    }
    assert my_statement(client, sales_agent_auth_headers, "2026-09") == {
        "month": "2026-09", "status": "approved", "paid_on": None, "is_shadow": True, "is_latest": True, "owed": 0.0,
        "owed_netted_in": None,
        "statement": {
            "base": {"monthly": 1500000.0, "amount": 1150000.0, "worked_days": 23, "working_days": 30},
            "commission": {"gross": 0.0, "orders": 0, "after_gate": 0.0, "products": []},
            "late": {"after_gate": 0.0, "lines": 0, "groups": []},
            "gate": {"compliance_pct": 0.0, "visits_due": 42, "visits_counted": 0, "multiplier": 0.5, "min_due": 20,
                     "rule": "band", "provisional": False},
            "new_outlets": {"amount": 0.0, "count": 0},
            "adjustments": {"amount": 0.0, "items": []},
            "penalties": {"amount": 100000.0, "count": 1},
            "variable": -100000.0,
            "carry_in": NOTHING_CARRIED,
            "gross_total": 1050000.0, "total": 1050000.0, "carry_out": 0.0, "owed": 0.0,
        },
    }
    assert pay_call(client, "GET", f"{LINES}?month=2026-09", sales_agent_auth_headers) == {
        "month": "2026-09", "status": "approved", "available": True, "page": 1, "has_more": False, "items": [],
    }


def test_a_statement_prints_the_admins_figures_for_the_same_month(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """One source of truth. Illustration B's October (Task 8's `october_world`), approved: base
    3,000,000 x 28 / 30 = 2,800,000; 100 + 50 bottles of 19 L at 1,000 a unit = 150,000 over 2
    orders on the product's one tier, x1.0 (no day plans, `below_min_due`); an admin adjustment of
    +50,000 and a 150,000 penalty: 2,850,000. S4 repeats A8, the admin drawer, for the same
    statement: base, commission (gross, after the gate, the product and its tier), the late groups,
    the gate, bonuses, adjustments, penalties, variable, carry-in and the totals."""
    agent = sales_agent_user
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, agent)
    water_order(world, 100, date(2026, 10, 5))
    water_order(world, 50, date(2026, 10, 6))
    move_clock(world, local_at(2026, 10, 15, 10))
    pay_route(world, "POST", f"/periods/2026-10/agents/{agent.id}/adjustments",
              {"amount": 50000, "reason": "August correction"}, status=201)
    pay_route(world, "POST", "/penalties", incident_body(agent, world.penalty_type_id, "2026-10-14"), status=201)
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))

    answer = my_statement(client, sales_agent_auth_headers, "2026-10")
    a8 = pay_route(world, "GET", f"/periods/2026-10/agents/{agent.id}")

    statement, summary = answer["statement"], a8["summary"]
    assert head(answer) == {
        "month": "2026-10", "status": "approved", "paid_on": None, "is_shadow": False, "is_latest": True, "owed": 0.0,
        "owed_netted_in": None,
    }
    assert (statement["base"]["amount"], statement["commission"]["gross"], statement["commission"]["after_gate"],
            statement["adjustments"]["amount"], statement["penalties"]["amount"], statement["total"]) == (
        2800000.0, 150000.0, 150000.0, 50000.0, 150000.0, 2850000.0,
    )
    assert [(product["units"], len(product["tiers"])) for product in statement["commission"]["products"]] == [(150, 1)]
    assert {
        "base": statement["base"]["amount"], "commission": statement["commission"], "late": statement["late"]["groups"],
        "new_outlets": statement["new_outlets"], "adjustments": statement["adjustments"]["amount"],
        "penalties": statement["penalties"]["amount"], "variable": statement["variable"],
        "carry_in": statement["carry_in"], "gross_total": statement["gross_total"], "total": statement["total"],
        "carry_out": statement["carry_out"], "owed": statement["owed"],
    } == {
        "base": summary["base_amount"], "commission": summary["commission"], "late": summary["late"],
        "new_outlets": summary["new_outlets"], "adjustments": summary["adjustments"],
        "penalties": summary["penalties"], "variable": summary["variable"], "carry_in": summary["carry_in"],
        "gross_total": summary["gross_total"], "total": summary["total"], "carry_out": summary["carry_out"],
        "owed": summary["owed"],
    }
    gate_keys = ("compliance_pct", "visits_due", "visits_counted", "multiplier", "rule", "provisional")
    assert {key: statement["gate"][key] for key in gate_keys} == {key: summary["gate"][key] for key in gate_keys}
    assert statement["gate"]["min_due"] == a8["plan"]["gate_min_due"]
    assert (statement["base"]["monthly"], statement["base"]["worked_days"], statement["base"]["working_days"]) == (
        a8["inputs"]["base_salary"], a8["inputs"]["worked_days"], a8["inputs"]["working_days"],
    )
    assert statement["adjustments"]["items"] == [
        {"amount": row["amount"], "reason": row["reason"]} for row in a8["adjustments"] if row["source"] == "admin"
    ]
    assert statement["penalties"]["count"] == sum(1 for row in a8["penalties"] if row["status"] == "confirmed")


def test_a_statements_balance_reads_as_s1_reads_it(app, client, db, monkeypatch, admin_user, admin_claim_headers):
    """S4's `is_latest`, `owed` and `owed_netted_in` are `_last_statement`'s for the same row,
    against S1's own open-month estimates (T-OWED-1, T-OWED-2 (a), S4 halves). The carry case's
    leaver owes October's 100,000 once October is approved, and November has no activity, so
    nothing nets it yet. 50 bottles credited on 10 November make November a month the close will
    freeze a statement in, and its estimate nets the balance: S4 says "2026-11" before S1 is asked
    (October is the outstanding balance, so S4 syncs the agent itself, as S1 does), and S1's last
    statement says the same.

    November approved (0 + 50,000 - 100,000: paid 0, owed 50,000) is the latest statement now.
    October still shows the 100,000 it owed when it was approved, as history: `is_latest` false,
    and nothing nets it, so S4 neither syncs nor estimates for it although it owes; November, the
    balance outstanding now, is read after S4's own sync. S3's newest item is the one statement S4
    calls latest, and S1's last statement is that same line."""
    world = carry_case(client, admin_claim_headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    headers = claim_headers(app, world.agent, "sales_agent")
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))

    october = my_statement(client, headers, "2026-10")

    assert head(october) == {
        "month": "2026-10", "status": "approved", "paid_on": None, "is_shadow": False, "is_latest": True,
        "owed": 100000.0, "owed_netted_in": None,
    }
    assert (october["statement"]["total"], october["statement"]["owed"]) == (0.0, 100000.0)
    assert head(my_earnings(client, headers, world.agent)["last_statement"]) == head(october)

    water_order(world, 50, date(2026, 11, 10))
    move_clock(world, local_at(2026, 11, 10, 18))
    lapse_throttle(world.agent)
    october = my_statement(client, headers, "2026-10")

    assert (october["is_latest"], october["owed"], october["owed_netted_in"]) == (True, 100000.0, "2026-11")
    assert head(my_earnings(client, headers, world.agent)["last_statement"]) == head(october)

    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2, 10))
    calls = sync_spies(monkeypatch)
    october = my_statement(client, headers, "2026-10")

    assert calls == {"ensure_periods": [], "ensure_fresh": [], "_open_estimates": []}

    november = my_statement(client, headers, "2026-11")

    assert [call["agent_user_ids"] for call in calls["ensure_fresh"]] == [[world.agent.id]]
    assert [call["agent_user_id"] for call in calls["_open_estimates"]] == [world.agent.id]
    assert {key: october[key] for key in ("is_latest", "owed", "owed_netted_in")} == {
        "is_latest": False, "owed": 100000.0, "owed_netted_in": None,
    }
    assert {key: november[key] for key in ("is_latest", "owed", "owed_netted_in")} == {
        "is_latest": True, "owed": 50000.0, "owed_netted_in": None,
    }
    assert latest_flags(client, headers) == [("2026-11", True), ("2026-10", False), ("2026-09", False)]
    assert head(my_earnings(client, headers, world.agent)["last_statement"]) == head(november)


def test_a_carried_shortfall_reads_as_history_once_a_later_month_is_approved(
    app, client, db, monkeypatch, admin_user, admin_claim_headers
):
    """The carry case, still employed (T-CARRY-1/2, S4 halves): October goes below zero, is paid
    0 and carries -100,000. Approved, it is the latest statement. Once November is approved too
    (3,000,000 for all 30 days, less the -100,000 carried in from October: 2,900,000), October
    still shows its own carry-out, as history: `is_latest` false. S3's newest item is the one
    statement S4 calls latest, and S1's last statement is that same line."""
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    headers = claim_headers(app, world.agent, "sales_agent")
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))

    october = my_statement(client, headers, "2026-10")

    assert (october["is_latest"], october["statement"]["total"], october["statement"]["carry_out"]) == (
        True, 0.0, -100000.0,
    )

    close_and_approve(world, "2026-11", at=local_at(2026, 12, 2, 10))
    october = my_statement(client, headers, "2026-10")
    november = my_statement(client, headers, "2026-11")

    assert head(october) == {
        "month": "2026-10", "status": "approved", "paid_on": None, "is_shadow": False, "is_latest": False,
        "owed": 0.0, "owed_netted_in": None,
    }
    assert (october["statement"]["total"], october["statement"]["carry_out"]) == (0.0, -100000.0)
    assert (november["is_latest"], november["statement"]["carry_in"], november["statement"]["total"]) == (
        True, {"amount": -100000.0, "from_month": "2026-10", "source": "carry_forward"}, 2900000.0,
    )
    assert latest_flags(client, headers) == [("2026-11", True), ("2026-10", False), ("2026-09", False)]
    assert head(my_earnings(client, headers, world.agent)["last_statement"]) == head(november)


# ---------------------------------------------------------------------------- what S4 syncs


def test_s4_syncs_and_estimates_only_for_the_balance_an_estimate_can_net(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """Only the agent's outstanding balance (`outstanding_owed`: the latest statement, when it
    owes) can be netted by an open estimate, so S4 reads S1's sync and open-month estimates for
    that row alone (the leaver's October above). Any other row's `owed_netted_in` is null whatever
    an estimate says, and a frozen read does not sync (§5.2).

    September and October are approved on base alone and owe nothing: October is the latest
    statement, September is history. 6 x 19 L delivered and paid on 10 November waits for a sync
    to credit it, and the throttle has lapsed. S4 opens both months without calling
    `ensure_periods`, `ensure_fresh` or `_open_estimates`, and the order is still uncredited.
    S1 then syncs it: the spies see S1's calls, so they would have seen S4's, and the order was
    creditable all along."""
    agent, headers = sales_agent_user, sales_agent_auth_headers
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, agent, start="2026-09")
    close_and_approve(world, "2026-09", at=local_at(2026, 10, 2, 10))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))
    order = water_order(world, 6, date(2026, 11, 10))
    move_clock(world, local_at(2026, 11, 10, 18))
    lapse_throttle(agent)
    calls = sync_spies(monkeypatch)

    october = my_statement(client, headers, "2026-10")
    september = my_statement(client, headers, "2026-09")

    assert [head(answer) for answer in (october, september)] == [
        {"month": "2026-10", "status": "approved", "paid_on": None, "is_shadow": False, "is_latest": True,
         "owed": 0.0, "owed_netted_in": None},
        {"month": "2026-09", "status": "approved", "paid_on": None, "is_shadow": False, "is_latest": False,
         "owed": 0.0, "owed_netted_in": None},
    ]
    assert calls == {"ensure_periods": [], "ensure_fresh": [], "_open_estimates": []}
    assert ledger_lines(order) == []

    my_earnings(client, headers, agent)

    assert [call["agent_user_ids"] for call in calls["ensure_fresh"]] == [[agent.id]]
    assert len(calls["_open_estimates"]) == 1
    assert [line.kind for line in ledger_lines(order)] == ["commission_credit"]


# ---------------------------------------------------------------------------- which months, whose


def test_only_an_approved_or_paid_month_is_listed_or_opened(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """I-14 through a month's life. 6 x 19 L at 1,500 in October: 9,000 on a base of 3,000,000 for
    all 31 days. While October is open, and once it is closed and under review, S3 lists nothing and
    S4 refuses it, as it refuses open November and August, which has no period. Approved, October is
    listed and opened; marked paid on 5 November, both say so, and the figures do not move."""
    headers = sales_agent_auth_headers
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    water_order(world, 6, date(2026, 10, 5))
    move_clock(world, local_at(2026, 10, 20, 10))

    assert my_statements(client, headers) == {"items": []}
    for month in ("2026-10", "2026-08"):  # open; no period
        not_available(client, headers, month)

    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")

    assert my_statements(client, headers) == {"items": []}
    for month in ("2026-10", "2026-11"):  # closed, under review; open
        not_available(client, headers, month)

    pay_route(world, "POST", "/periods/2026-10/approve")
    approved = my_statement(client, headers, "2026-10")

    assert my_statements(client, headers) == {
        "items": [{"month": "2026-10", "total": 3009000.0, "status": "approved", "paid_on": None, "is_shadow": False}],
    }
    assert (approved["status"], approved["paid_on"], approved["statement"]["total"]) == ("approved", None, 3009000.0)
    not_available(client, headers, "2026-11")

    move_clock(world, local_at(2026, 11, 5, 10))
    pay_route(world, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-11-05"})
    paid = my_statement(client, headers, "2026-10")

    assert my_statements(client, headers) == {
        "items": [{"month": "2026-10", "total": 3009000.0, "status": "paid", "paid_on": "2026-11-05",
                   "is_shadow": False}],
    }
    assert (paid["status"], paid["paid_on"]) == ("paid", "2026-11-05")
    assert paid["statement"] == approved["statement"]


def test_an_agent_never_sees_another_agents_statements(
    app, client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """Sardor (the conftest agent, base 3,000,000) leaves on 30 September; Bekzod (2,000,000) stays.
    September and October are approved. Sardor's S3 lists his own September alone, and naming
    Bekzod with `agent_id` changes nothing; S4 opens his own September, and refuses October, which
    is approved and holds Bekzod's statement but none of his. September is Sardor's latest
    statement although October is the newest approved month: `is_latest` is the agent's own (the
    row his last statement and his owed balance read). Bekzod sees his own two months."""
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, start="2026-09",
                      employment_end="2026-09-30")
    bekzod = pay_agent(db, phone="+998901238702", first_name="Bekzod")
    configure_agent(client, admin_claim_headers, bekzod, plan_id=world.plan["id"], base=2000000,
                    effective_month="2026-09", employment_start="2026-09-01")
    close_and_approve(world, "2026-09", at=local_at(2026, 10, 2, 10))
    close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))
    bekzod_headers = claim_headers(app, bekzod, "sales_agent")

    def item(month, total):
        return {"month": month, "total": total, "status": "approved", "paid_on": None, "is_shadow": False}

    assert my_statements(client, sales_agent_auth_headers) == {"items": [item("2026-09", 3000000.0)]}
    assert pay_call(client, "GET", f"{STATEMENTS}?agent_id={bekzod.id}", sales_agent_auth_headers) == {
        "items": [item("2026-09", 3000000.0)],
    }
    mine = my_statement(client, sales_agent_auth_headers, "2026-09")
    assert (mine["statement"]["base"]["monthly"], mine["statement"]["total"]) == (3000000.0, 3000000.0)
    assert mine["is_latest"] is True
    assert pay_call(client, "GET", f"{STATEMENTS}/2026-09?agent_id={bekzod.id}", sales_agent_auth_headers) == mine
    not_available(client, sales_agent_auth_headers, "2026-10")
    assert my_statements(client, bekzod_headers) == {
        "items": [item("2026-10", 2000000.0), item("2026-09", 2000000.0)],
    }
    assert my_statement(client, bekzod_headers, "2026-10")["statement"]["base"]["monthly"] == 2000000.0
    assert latest_flags(client, bekzod_headers) == [("2026-10", True), ("2026-09", False)]


def test_s3_lists_the_newest_twelve_statements_newest_first(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, sales_agent_auth_headers
):
    """Thirteen approved months, September 2026 to September 2027, each closed and approved on the
    2nd of the next: S3 lists the newest twelve, newest first, a year of statements. September
    2026 is the thirteenth and is left out."""
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, start="2026-09")
    months = [date(2026, 9, 1)]
    while len(months) < 13:
        months.append(local_windows.next_month(months[-1]))
    for month in months:
        after = local_windows.next_month(month)
        close_and_approve(world, local_windows.format_month(month), at=local_at(after.year, after.month, 2, 10))

    listed = [row["month"] for row in my_statements(client, sales_agent_auth_headers)["items"]]

    assert listed == [local_windows.format_month(month) for month in reversed(months[1:])]
    assert (len(listed), listed[0], listed[-1]) == (12, "2027-09", "2026-10")


# ---------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "10.2026", "2026-10-01"])
def test_s4_refuses_a_month_it_cannot_parse_as_s2_does(client, db, sales_agent_user, sales_agent_auth_headers, month):
    """`<month>` is parsed by S2's own helper, so a value that is not "YYYY-MM" gets S2's coded 400,
    details and all, and no refusal of its own."""
    s4 = refusal(client, "GET", f"{STATEMENTS}/{month}", sales_agent_auth_headers, status=400,
                 code="SALES_PAY_MONTH_INVALID")
    s2 = refusal(client, "GET", f"{LINES}?month={month}", sales_agent_auth_headers, status=400,
                 code="SALES_PAY_MONTH_INVALID")

    assert s4 == s2 == {"month": month, "validation_errors": []}


def test_only_a_sales_agent_reaches_s3_and_s4(
    client, db, sales_agent_user, driver_auth_headers, admin_claim_headers, manager_claim_headers
):
    """T-VISIB-3's role half for the two new routes: a driver is not a sales agent (403
    STAFF_NO_ROLE), and neither is an admin or a manager, whatever their token claims."""
    for path in (STATEMENTS, f"{STATEMENTS}/2026-10"):
        refusal(client, "GET", path, driver_auth_headers, status=403, code="STAFF_NO_ROLE")
        refusal(client, "GET", path, admin_claim_headers, status=403, code="STAFF_NO_ROLE")
        assert client.get(path, headers=manager_claim_headers).status_code == 403
