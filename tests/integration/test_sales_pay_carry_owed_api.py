"""What an agent is paid when a month goes below zero, and what the approval tells them.

Compensation spec C1 (nothing is forgiven), I-26 (carried while employed next month, owed
otherwise), I-27 (a shadow month neither carries nor owes), I-28 and Q15 (an owed balance is
netted by the agent's later statements, once, in month order), §4.9 (approve and its
re-freezes) and §7.5 (the approval push). Scenarios T-CARRY-1, T-CARRY-2, T-OWED-1,
T-OWED-2 (a)(b)(c) with the invariant guard, T-OWED-3 (a)(b)(c)(f), T-SHADOW-1's approval
payload and T-VISIB-4's recipients.

The shortfall comes from real inputs, never an injected figure. `carry_case` (the Task 8
builders) is §1.2's carry case through the admin routes: a reversal of September's credit and
a penalty booked through A23. The S1 halves of these scenarios are Task 10's, on the same
builder. Money on the wire is a float (`to_wire`); money in a push is a whole-UZS int.
"""

from datetime import date
from decimal import Decimal

import pytest

from business_app.models.sales import SalesAgentProfile
from business_app.models.sales_pay import SalesPayPeriod
from business_app.services.sales.pay_statement_service import SalesPayStatementService
from shared.staff_constants import SALES_EVENT_PAY_STATEMENT_APPROVED
from tests.integration.sales_pay_builders import (
    DEC,
    NOV,
    NOVEMBER_WORKING,
    OCT,
    PAY_API,
    carry_case,
    carry_rows,
    configure_agent,
    frozen_statements,
    incident_body,
    ledger_lines,
    local_at,
    logged_push_failures,
    move_clock,
    october_world,
    pay_call,
    pay_route,
    pushes_of,
    refusal,
    spy_sales_pushes,
    staff_agent,
    statement_id,
    water_order,
)
from tests.integration.test_sales_pay_penalties_api import commit_log  # noqa: F401 -- a fixture

pytestmark = pytest.mark.integration

JAN = date(2027, 1, 1)
FIGURES = ("base_amount", "variable", "gross_total", "total", "carry_out", "owed")
NOTHING_BROUGHT_IN = {"amount": 0.0, "from_month": None, "source": None}
AZIZ_CHAT = 777000301  # `carry_case`'s agent


def _statement(world, month: str) -> dict:
    return pay_route(world, "GET", f"/periods/{month}/agents/{world.agent.id}")


def _summary(world, month: str) -> dict:
    return _statement(world, month)["summary"]


def _figures(summary: dict) -> dict:
    return {key: summary[key] for key in FIGURES}


def _only(summary: dict, expected: dict) -> dict:
    """The summary's values of exactly the keys a scenario states."""
    return {key: summary[key] for key in expected}


def _owed_to_date(world) -> dict:
    """A16's `owed_to_date`: the admin surface that says what a leaver still owes (R34)."""
    return pay_route(world, "GET", f"/agents/{world.agent.id}/terms")["owed_to_date"]


def _sync_now(world, month: str) -> None:
    """The Pay page's "Sync now" (A4 on an open month), at the frozen clock of the order just placed.

    Two fixture facts make it necessary. An estimate read syncs an agent at most once per
    SALES_PAY_ONDEMAND_SYNC_SECONDS, and `carry_case` already spent that. And the sync's
    candidate window is measured from the ORDER's wall-clock `created_at`, which no freeze moves,
    so a later close run months ahead of the wall clock sees the order only through the line this
    sync posts into its open month (§4.4.1).
    """
    pay_route(world, "POST", f"/periods/{month}/recalculate")


def _close(world, month: str, *, at) -> dict:
    move_clock(world, at)
    return pay_route(world, "POST", f"/periods/{month}/close")


def _close_and_approve(world, month: str, *, at) -> dict:
    _close(world, month, at=at)
    return pay_route(world, "POST", f"/periods/{month}/approve")


def _set_end(world, end, *, status: int = 200):
    """A18 as the Pay tab sends it: the start the builder set, and the new end (or None)."""
    return pay_route(
        world, "PUT", f"/agents/{world.agent.id}/employment", {"start": "2026-09-01", "end": end}, status=status
    )


def _deactivate(world, agent) -> None:
    """The staff page's switch, `PUT /admin/staff/sales-agents/<id>/active`."""
    pay_call(
        world.client, "PUT", f"/api/v1/admin/staff/sales-agents/{agent.id}/active", world.headers, {"is_active": False}
    )


def _commission(order) -> dict:
    """The push's `commission` for a month whose one credited order is `order` (§7.5): 19 L alone on
    the world's single tier of 1,000 a unit, as A8 publishes it (whole-UZS ints; `next_tier` null, a
    frozen statement has none), under the name the credit froze."""
    (item,) = ledger_lines(order)[0].snapshot["items"]
    units = item["quantity"]
    return {
        "gross": units * 1000,
        "products": [{
            "product_id": item["product_id"], "product_name": item["product_name"], "uses_default_tiers": False,
            "units": units, "total": units * 1000,
            "tiers": [{"from_unit": 1, "to_unit": None, "units": units, "mode": "per_unit", "value": 1000.0,
                       "net": float(item["net"]), "amount_full": units * 1000, "amount": units * 1000}],
            "next_tier": None,
        }],
    }


def _approval_push(world, month: str, **figures) -> tuple:
    """One recorded `pay_statement_approved` push to Aziz, as §7.5 spells it."""
    payload = {
        "statement_id": statement_id(world, month, world.agent),
        "month": month,
        "is_shadow": False,
        "base": 0,
        "commission": {"gross": 0, "products": []},
        "commission_after_gate": 0,
        "late": 0,
        "new_outlets": 0,
        "adjustments": 0,
        "penalties": 0,
        "carry_in": {"amount": 0, "from_month": None, "source": None},
        "total": 0,
        "carry_out": 0,
        "owed": 0,
    }
    payload.update(figures)
    return (AZIZ_CHAT, payload, f"statement-approved:{payload['statement_id']}")


def _keys_anywhere(body) -> set:
    """Every dict key at any depth of a JSON body."""
    if isinstance(body, dict):
        return set(body).union(*(_keys_anywhere(value) for value in body.values()))
    if isinstance(body, list):
        return set().union(*(_keys_anywhere(item) for item in body))
    return set()


def _months() -> dict:
    return {period.month_start: period for period in SalesPayPeriod.query.all()}


def _november_balance(client, monkeypatch, admin_user, headers):
    """T-OWED-1's leaver after November's approval, which T-OWED-2 continues.

    October is approved owing 100,000. 50 x 19 L delivered and paid on 5 November credits
    50,000 at x1.0, so November nets October's balance: -50,000, paid 0, owed 50,000.
    """
    world = carry_case(client, headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    _close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))
    water_order(world, 50, date(2026, 11, 5))
    _close_and_approve(world, "2026-11", at=local_at(2026, 12, 2, 10))
    return world


# --------------------------------------------------------------------------- #
# The approval push (§7.5): who hears, when, and exactly what
# --------------------------------------------------------------------------- #


def test_the_approval_tells_each_active_agent_with_a_chat_once_after_commit(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, commit_log
):
    """§7.5 and T-VISIB-4. Three agents have an October statement: the world's agent, with a
    chat; Bekzod, with none; Dilnoza, with a chat but deactivated after the close and before the
    approval. Only the first is told, once, after the approval committed, with the domain id
    `statement-approved:<id>`. A second approve is a 409 and tells nobody.

    Illustration B's October base (3,000,000 x 28 / 30 = 2,800,000), 100 x 19 L at 1,000 =
    100,000 at x1.0 (nothing was due), an A11 +50,000 and a 150,000 penalty through A23:
    2,800,000 + 100,000 + 50,000 - 150,000 = 2,800,000, with nothing carried in or out.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent = sales_agent_user
    bekzod = staff_agent(db, phone="+998901238311", first_name="Bekzod")
    dilnoza = staff_agent(db, phone="+998901238312", first_name="Dilnoza", telegram_id="777000203")
    for other in (bekzod, dilnoza):
        configure_agent(
            client, admin_claim_headers, other, plan_id=world.plan["id"], base=2700000,
            effective_month="2026-10", employment_start="2026-10-01",
        )
    order = water_order(world, 100, date(2026, 10, 20))
    pay_route(
        world, "POST", f"/periods/2026-10/agents/{agent.id}/adjustments",
        {"amount": 50000, "reason": "Guarantee top-up"}, status=201,
    )
    pay_route(world, "POST", "/penalties", incident_body(agent, world.penalty_type_id, "2026-10-14"), status=201)
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    _deactivate(world, dilnoza)
    pushes = spy_sales_pushes(monkeypatch, trail=commit_log)
    commit_log.clear()

    pay_route(world, "POST", "/periods/2026-10/approve")

    listed = {row["agent_user_id"] for row in pay_route(world, "GET", "/periods/2026-10")["agents"]}
    assert listed == {agent.id, bekzod.id, dilnoza.id}
    sid = statement_id(world, "2026-10", agent)
    assert pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == [
        (
            777000201,
            {
                "statement_id": sid,
                "month": "2026-10",
                "is_shadow": False,
                "base": 2800000,
                "commission": _commission(order),
                "commission_after_gate": 100000,
                "late": 0,
                "new_outlets": 0,
                "adjustments": 50000,
                "penalties": 150000,
                "carry_in": {"amount": 0, "from_month": None, "source": None},
                "total": 2800000,
                "carry_out": 0,
                "owed": 0,
            },
            f"statement-approved:{sid}",
        )
    ]
    assert "commit" in commit_log[: commit_log.index("push")]

    again = pay_route(world, "POST", "/periods/2026-10/approve", status=409)

    assert again["error_code"] == "SALES_PAY_STATE_INVALID"
    assert len(pushes) == 1


def test_a_push_the_broker_refuses_never_undoes_the_approval_or_silences_the_next_agent(
    client, db, monkeypatch, caplog, admin_user, admin_claim_headers, sales_agent_user
):
    """§7.5: the approval pushes go out after the commit, each best effort. Two agents have an
    October statement: the world's agent (chat 777000201, the lower id, pushed first) and Dilnoza
    (chat 777000203). The broker refuses the first push. A5 still answers 200 and October is
    approved; Dilnoza is still told; the refused push is logged once, naming its statement. A 500
    here would leave the admin an approved month and an error, and the retry a 409.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    dilnoza = staff_agent(db, phone="+998901238313", first_name="Dilnoza", telegram_id="777000203")
    configure_agent(
        client, admin_claim_headers, dilnoza, plan_id=world.plan["id"], base=2700000,
        effective_month="2026-10", employment_start="2026-10-01",
    )
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    pushes = spy_sales_pushes(monkeypatch, fail_for=(777000201,))

    with logged_push_failures(caplog) as failures:
        approved = pay_route(world, "POST", "/periods/2026-10/approve")

    assert approved["status"] == "approved"
    assert _months()[OCT].status == "approved"
    assert [(chat, event_id) for chat, _payload, event_id in pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED)] == [
        (777000203, f"statement-approved:{statement_id(world, '2026-10', dilnoza)}")
    ]
    [failure] = failures
    assert f"statement {statement_id(world, '2026-10', sales_agent_user)} " in failure.getMessage()


# --------------------------------------------------------------------------- #
# T-CARRY-1 / T-CARRY-2: carried while employed next month
# --------------------------------------------------------------------------- #


def test_a_month_below_zero_pays_nothing_and_carries_the_shortfall_once(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-CARRY-1 (C1; §1.2's carry case).

    October: base 3,000,000 x 2 / 30 = 200,000; 30 x 19 L = 30,000 at x1.0 (nothing due,
    `below_min_due`); September's 300,000 credit reversed in October and gated at September's
    x1.0 (-300,000, late); a 30,000 penalty through A23. Variable 30,000 - 300,000 - 30,000 =
    -300,000; gross 200,000 - 300,000 = -100,000; paid 0. Aziz is employed in November, so the
    -100,000 is carried: A5 posts exactly one carry row into November, stamped with the approving
    admin, and tells Aziz what October paid. The pre-gate sums keep their `gross`; the pay gross is
    `gross_total`, and no v2 key survives anywhere.
    """
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    agent = world.agent
    october = _statement(world, "2026-10")
    summary = october["summary"]

    assert _figures(summary) == {
        "base_amount": 200000.0,
        "variable": -300000.0,
        "gross_total": -100000.0,
        "total": 0.0,
        "carry_out": -100000.0,
        "owed": 0.0,
    }
    assert (summary["commission"]["gross"], summary["commission"]["after_gate"], summary["penalties"]) == (
        30000.0,
        30000.0,
        30000.0,
    )
    assert [(row["earned_month"], row["gross"], row["after_gate"]) for row in summary["late"]] == [
        ("2026-09", -300000.0, -300000.0)
    ]
    assert summary["carry_in"] == NOTHING_BROUGHT_IN
    assert "gross" not in summary
    period_view = pay_route(world, "GET", "/periods/2026-10")
    assert (_keys_anywhere(october) | _keys_anywhere(period_view)) & {"dropped_penalties", "before_floor"} == set()

    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    pushes = spy_sales_pushes(monkeypatch)
    approved = pay_route(world, "POST", "/periods/2026-10/approve")

    months = _months()
    [carry] = carry_rows()
    assert approved["status"] == "approved"
    assert (
        carry.agent_user_id,
        carry.period_id,
        carry.amount,
        carry.carried_from_period_id,
        carry.reason,
        carry.created_by_user_id,
    ) == (agent.id, months[NOV].id, Decimal("-100000"), months[OCT].id, "carry_forward 2026-10", admin_user.id)
    assert pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == [
        _approval_push(
            world, "2026-10", commission=_commission(world.october), base=200000, commission_after_gate=30000,
            late=-300000, penalties=30000, carry_out=-100000,
        )
    ]
    [row] = _statement(world, "2026-11")["adjustments"]
    assert (row["source"], row["carried_from_month"], row["posting_month"], row["amount"]) == (
        "carry_forward",
        "2026-10",
        "2026-11",
        -100000.0,
    )

    again = pay_route(world, "POST", "/periods/2026-10/approve", status=409)

    assert again["error_code"] == "SALES_PAY_STATE_INVALID"
    assert len(carry_rows()) == 1
    assert len(pushes) == 1


def test_a_shadow_month_below_zero_neither_carries_nor_owes_and_the_push_says_trial(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-CARRY-1's shadow variant (I-27) and T-SHADOW-1's approval payload.

    October is the shadow epoch, so there is no earlier credit: base 200,000 and a 300,000
    penalty through A23 give variable -300,000 and gross -100,000, paid 0, and a shadow month
    neither carries nor owes. Its approval writes no carry row, and the push says `is_shadow`,
    which the bot turns into the "trial statement" headline.
    """
    world = carry_case(client, admin_claim_headers, leaver=False, shadow=True, monkeypatch=monkeypatch, admin=admin_user)

    assert _figures(_summary(world, "2026-10")) == {
        "base_amount": 200000.0,
        "variable": -300000.0,
        "gross_total": -100000.0,
        "total": 0.0,
        "carry_out": 0.0,
        "owed": 0.0,
    }

    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    pushes = spy_sales_pushes(monkeypatch)
    pay_route(world, "POST", "/periods/2026-10/approve")

    assert carry_rows() == []
    assert pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == [
        _approval_push(world, "2026-10", is_shadow=True, base=200000, penalties=300000)
    ]


def test_the_carry_is_deducted_next_month_and_the_push_shows_it(client, db, monkeypatch, admin_user, admin_claim_headers):
    """T-CARRY-2 (C1).

    November: base 3,000,000 (30 working days, all worked); 450 x 19 L at 1,000 = 450,000 at
    x1.0. The carry row brings in -100,000, which is not an admin adjustment: variable 450,000,
    gross 3,000,000 + 450,000 - 100,000 = 3,350,000, paid 3,350,000. The A2 row publishes the
    carry-in, the statement nets no owed balance, and the approval push carries the same
    `carry_in` object as A8.
    """
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    _close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))
    november_order = water_order(world, 450, date(2026, 11, 10))
    move_clock(world, local_at(2026, 12, 2, 10))
    carried = {"amount": -100000.0, "from_month": "2026-10", "source": "carry_forward"}
    expected = {
        "carry_in": carried,
        "adjustments": 0.0,
        "variable": 450000.0,
        "gross_total": 3350000.0,
        "total": 3350000.0,
        "carry_out": 0.0,
        "owed": 0.0,
    }

    _sync_now(world, "2026-11")
    estimate = _summary(world, "2026-11")
    pay_route(world, "POST", "/periods/2026-11/close")
    frozen = _summary(world, "2026-11")

    assert _only(estimate, expected) == expected
    assert _only(frozen, expected) == expected
    [row] = pay_route(world, "GET", "/periods/2026-11")["agents"]
    assert row["carry_in"] == -100000.0
    assert frozen_statements(world.agent.id)[NOV].owed_in_statement_id is None

    pushes = spy_sales_pushes(monkeypatch)
    pay_route(world, "POST", "/periods/2026-11/approve")

    assert pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == [
        _approval_push(
            world,
            "2026-11",
            commission=_commission(november_order),
            base=3000000,
            commission_after_gate=450000,
            carry_in={"amount": -100000, "from_month": "2026-10", "source": "carry_forward"},
            total=3350000,
        )
    ]


def test_a_carry_the_next_month_cannot_absorb_is_carried_again_never_dropped(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-CARRY-2, "chain, never a drop": every November working day is unpaid and nothing is
    earned, so November is base 0 and carry-in -100,000: gross -100,000, paid 0, carried out
    -100,000. A5 on November posts that -100,000 into December."""
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    _close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))
    move_clock(world, local_at(2026, 12, 2, 10))
    pay_route(
        world, "PUT", f"/periods/2026-11/agents/{world.agent.id}/unpaid-days",
        {"days": [{"date": day} for day in NOVEMBER_WORKING]},
    )
    pay_route(world, "POST", "/periods/2026-11/close")

    assert _figures(_summary(world, "2026-11")) == {
        "base_amount": 0.0,
        "variable": 0.0,
        "gross_total": -100000.0,
        "total": 0.0,
        "carry_out": -100000.0,
        "owed": 0.0,
    }

    pay_route(world, "POST", "/periods/2026-11/approve")

    months = _months()
    assert [
        (carry.period_id, carry.amount, carry.carried_from_period_id, carry.reason)
        for carry in sorted(carry_rows(), key=lambda carry: carry.id)
    ] == [
        (months[NOV].id, Decimal("-100000"), months[OCT].id, "carry_forward 2026-10"),
        (months[DEC].id, Decimal("-100000"), months[NOV].id, "carry_forward 2026-11"),
    ]


# --------------------------------------------------------------------------- #
# T-OWED-1: a leaver owes the shortfall
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("deactivated", [False, True], ids=["active", "deactivated"])
def test_a_leaver_owes_the_shortfall_and_only_an_active_one_is_told(
    client, db, monkeypatch, admin_user, admin_claim_headers, deactivated
):
    """T-OWED-1, the October half, and its deactivated leaver (§7.5 recipients, R34).

    Employment ends on 31 October, so the -100,000 is owed, not carried: A5 writes no carry row.
    An active leaver is told `owed 100000`; a leaver whose profile was deactivated before the
    approval is told nothing (`assert_staff_active` raises for them), and the admin surfaces are
    identical either way: A8's `owed`, A2's row and A16's `owed_to_date`. Once October is
    approved, A18 may not move the end date back (T-OWED-1 (d), the (c) direction): 409
    MONTH_LOCKED naming October, with the profile and the statement unchanged.
    """
    world = carry_case(client, admin_claim_headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    agent = world.agent
    owing = {
        "base_amount": 200000.0,
        "variable": -300000.0,
        "gross_total": -100000.0,
        "total": 0.0,
        "carry_out": 0.0,
        "owed": 100000.0,
    }
    assert _figures(_summary(world, "2026-10")) == owing
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    if deactivated:
        _deactivate(world, agent)
    pushes = spy_sales_pushes(monkeypatch)

    pay_route(world, "POST", "/periods/2026-10/approve")

    assert carry_rows() == []
    told = [] if deactivated else [
        _approval_push(
            world, "2026-10", commission=_commission(world.october), base=200000, commission_after_gate=30000,
            late=-300000, penalties=30000, owed=100000,
        )
    ]
    assert pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == told
    assert _figures(_summary(world, "2026-10")) == owing
    [row] = pay_route(world, "GET", "/periods/2026-10")["agents"]
    assert (row["owed"], row["carry_out"]) == (100000.0, 0.0)
    assert _owed_to_date(world) == {"amount": 100000.0, "month": "2026-10", "nets_in": "2026-11"}

    refused = refusal(
        client, "PUT", f"{PAY_API}/agents/{agent.id}/employment", admin_claim_headers,
        {"start": "2026-09-01", "end": None}, status=409, code="SALES_PAY_MONTH_LOCKED",
    )

    assert refused["months"] == ["2026-10"]
    db.session.expire_all()
    assert SalesAgentProfile.query.filter_by(user_id=agent.id).one().employment_end_date == date(2026, 10, 31)
    frozen = frozen_statements(agent.id)[OCT]
    assert (frozen.revision, frozen.owed_amount, frozen.carry_out_amount) == (1, Decimal("100000"), Decimal("0"))


def test_an_end_date_moved_while_october_is_closed_moves_the_shortfall_between_carried_and_owed(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-1 (a), (c) and (b), each through A18 while October is closed (§4.10).

    Employed through 1 November (a November day, so none of October's days depends on it), October carries
    -100,000. (a) 1 November -> 31 October: revision 2, the 100,000 moves from `carry_out` to
    `owed`. (c) 31 October -> none: revision 3, carried again. (b) none -> 31 October:
    revision 4, owed again.
    """
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    _set_end(world, "2026-11-01")
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))

    def october():
        statement = _statement(world, "2026-10")
        return statement["revision"], statement["summary"]["carry_out"], statement["summary"]["owed"]

    assert october() == (1, -100000.0, 0.0)

    _set_end(world, "2026-10-31")
    assert october() == (2, 0.0, 100000.0)

    _set_end(world, None)
    assert october() == (3, -100000.0, 0.0)

    _set_end(world, "2026-10-31")
    assert october() == (4, 0.0, 100000.0)


def test_after_approval_only_the_open_month_follows_a_new_end_date(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-1 (d), the (b) direction, then (e), on the carried branch.

    October is approved carrying -100,000 into November. (d) An end of 31 October would change
    October: 409 MONTH_LOCKED naming October, nothing written. (e) An end of 1 November touches
    only November, which is open. 1 November is a worked day (C2 v5): November's estimate has 1
    worked day, base 3,000,000 x 1 / 30 = 100,000, carry-in -100,000, gross 0, owed 0. The admin
    then applies the §4.10 fallback, A10 unpaid on 1 November: no worked day, base 0, gross
    -100,000, paid 0, and, with no job in December, owed 100,000.
    """
    world = carry_case(client, admin_claim_headers, leaver=False, monkeypatch=monkeypatch, admin=admin_user)
    agent = world.agent
    _close_and_approve(world, "2026-10", at=local_at(2026, 11, 2, 10))

    refused = refusal(
        client, "PUT", f"{PAY_API}/agents/{agent.id}/employment", admin_claim_headers,
        {"start": "2026-09-01", "end": "2026-10-31"}, status=409, code="SALES_PAY_MONTH_LOCKED",
    )

    assert refused["months"] == ["2026-10"]
    db.session.expire_all()
    assert SalesAgentProfile.query.filter_by(user_id=agent.id).one().employment_end_date is None
    assert frozen_statements(agent.id)[OCT].revision == 1

    _set_end(world, "2026-11-01")
    employed_one_day = _statement(world, "2026-11")
    pay_route(world, "PUT", f"/periods/2026-11/agents/{agent.id}/unpaid-days", {"days": [{"date": "2026-11-01"}]})
    november = _summary(world, "2026-11")

    carried = {"amount": -100000.0, "from_month": "2026-10", "source": "carry_forward"}
    assert employed_one_day["inputs"]["worked_days"] == 1
    assert employed_one_day["summary"]["carry_in"] == carried
    assert _figures(employed_one_day["summary"]) == {
        "base_amount": 100000.0,
        "variable": 0.0,
        "gross_total": 0.0,
        "total": 0.0,
        "carry_out": 0.0,
        "owed": 0.0,
    }
    assert november["carry_in"] == carried
    assert _figures(november) == {
        "base_amount": 0.0,
        "variable": 0.0,
        "gross_total": -100000.0,
        "total": 0.0,
        "carry_out": 0.0,
        "owed": 100000.0,
    }


@pytest.mark.parametrize("deactivated", [False, True], ids=["active", "deactivated"])
def test_a_later_statement_nets_what_the_leaver_owes(client, db, monkeypatch, admin_user, admin_claim_headers, deactivated):
    """T-OWED-1, the netting half (I-11, I-28, Q15), identical for a deactivated leaver.

    October is approved owing 100,000. 50 x 19 L delivered and paid on 5 November, after the
    exit, is credited 50,000 at x1.0 (`below_min_due`). November's estimate and, after A3, its
    statement: base 0, variable 50,000, carry-in -100,000 (the owed balance, from October),
    gross -50,000, paid 0, owed 50,000, netting October's statement. The netting is not an
    adjustment row, and A5 posts no carry. The push carries the same `carry_in` object; A16 then
    says 50,000 from November, netted in December.
    """
    world = carry_case(client, admin_claim_headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    agent = world.agent
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    if deactivated:
        _deactivate(world, agent)
    pay_route(world, "POST", "/periods/2026-10/approve")
    november_order = water_order(world, 50, date(2026, 11, 5))
    move_clock(world, local_at(2026, 11, 5, 12))
    expected = {
        "base_amount": 0.0,
        "variable": 50000.0,
        "carry_in": {"amount": -100000.0, "from_month": "2026-10", "source": "owed"},
        "gross_total": -50000.0,
        "total": 0.0,
        "carry_out": 0.0,
        "owed": 50000.0,
    }

    _sync_now(world, "2026-11")
    estimate = _summary(world, "2026-11")
    _close(world, "2026-11", at=local_at(2026, 12, 2, 10))
    frozen = _statement(world, "2026-11")

    assert _only(estimate, expected) == expected
    assert _only(frozen["summary"], expected) == expected
    assert frozen["adjustments"] == []
    statements = frozen_statements(agent.id)
    assert statements[NOV].owed_in_statement_id == statements[OCT].id

    pushes = spy_sales_pushes(monkeypatch)
    pay_route(world, "POST", "/periods/2026-11/approve")

    assert carry_rows() == []
    told = [] if deactivated else [
        _approval_push(
            world,
            "2026-11",
            commission=_commission(november_order),
            commission_after_gate=50000,
            carry_in={"amount": -100000, "from_month": "2026-10", "source": "owed"},
            owed=50000,
        )
    ]
    assert pushes_of(pushes, SALES_EVENT_PAY_STATEMENT_APPROVED) == told
    assert _owed_to_date(world) == {"amount": 50000.0, "month": "2026-11", "nets_in": "2026-12"}


# --------------------------------------------------------------------------- #
# T-OWED-2: two-step netting, a month with no statement, an offline repayment
# --------------------------------------------------------------------------- #


def test_a_month_without_activity_writes_no_statement_and_the_next_one_nets_the_balance(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-2 (a). November left 50,000 owed. December has no activity, so A3 and A5 write no
    December statement for Aziz (a balance is not activity), and A16 moves `nets_in` to January.
    80 x 19 L credited 80,000 in January nets November's balance, not October's: gross
    80,000 - 50,000 = 30,000, paid 30,000, owed 0. Paid October to January: 0 + 0 + 30,000 =
    50,000 + 80,000 - 100,000.
    """
    world = _november_balance(client, monkeypatch, admin_user, admin_claim_headers)
    agent = world.agent

    _close_and_approve(world, "2026-12", at=local_at(2027, 1, 2, 10))

    assert agent.id not in {row["agent_user_id"] for row in pay_route(world, "GET", "/periods/2026-12")["agents"]}
    assert DEC not in frozen_statements(agent.id)
    assert _owed_to_date(world) == {"amount": 50000.0, "month": "2026-11", "nets_in": "2027-01"}

    water_order(world, 80, date(2027, 1, 12))
    move_clock(world, local_at(2027, 1, 12, 12))
    expected = {
        "carry_in": {"amount": -50000.0, "from_month": "2026-11", "source": "owed"},
        "variable": 80000.0,
        "gross_total": 30000.0,
        "total": 30000.0,
        "owed": 0.0,
    }
    _sync_now(world, "2027-01")
    estimate = _summary(world, "2027-01")
    _close(world, "2027-01", at=local_at(2027, 2, 2, 10))
    frozen = _summary(world, "2027-01")

    assert _only(estimate, expected) == expected
    assert _only(frozen, expected) == expected
    statements = frozen_statements(agent.id)
    assert statements[JAN].owed_in_statement_id == statements[NOV].id

    pay_route(world, "POST", "/periods/2027-01/approve")

    assert _owed_to_date(world) == {"amount": 0.0, "month": None, "nets_in": None}
    paid = frozen_statements(agent.id)
    assert sum(paid[month].total_amount for month in (OCT, NOV, JAN)) == Decimal("30000")


@pytest.mark.parametrize("december", ["closed", "open"])
def test_an_offline_repayment_is_recorded_in_the_month_that_nets_the_balance(
    client, db, monkeypatch, admin_user, admin_claim_headers, december
):
    """T-OWED-2 (b). Aziz repays the 50,000 offline. The Pay tab's Record repayment posts A11
    into A16's `nets_in` month (December) with `{amount 50000, reason "Repaid offline"}`.

    December closed with no statement for him: the A11 re-freeze inserts it at revision 1 and A2
    lists him. December open: the estimate shows the same figures, and the close freezes them.
    Either way: adjustment +50,000, carry-in -50,000 (owed, from November), gross 0, paid 0,
    owed 0. After A5 nothing is outstanding, and January's 80,000 nets nothing: carry-in 0,
    paid 80,000, no pointer, and no IntegrityError at January's close.
    """
    world = _november_balance(client, monkeypatch, admin_user, admin_claim_headers)
    agent = world.agent
    if december == "closed":
        _close(world, "2026-12", at=local_at(2027, 1, 2, 10))
        assert agent.id not in {row["agent_user_id"] for row in pay_route(world, "GET", "/periods/2026-12")["agents"]}
    else:
        move_clock(world, local_at(2026, 12, 20, 10))
    owed = _owed_to_date(world)
    assert owed == {"amount": 50000.0, "month": "2026-11", "nets_in": "2026-12"}
    expected = {
        "adjustments": 50000.0,
        "carry_in": {"amount": -50000.0, "from_month": "2026-11", "source": "owed"},
        "gross_total": 0.0,
        "total": 0.0,
        "owed": 0.0,
    }

    pay_route(
        world, "POST", f"/periods/{owed['nets_in']}/agents/{agent.id}/adjustments",
        {"amount": 50000, "reason": "Repaid offline"}, status=201,
    )
    if december == "open":
        assert _only(_summary(world, "2026-12"), expected) == expected
        _close(world, "2026-12", at=local_at(2027, 1, 2, 10))

    frozen = _statement(world, "2026-12")
    assert frozen["revision"] == 1
    assert _only(frozen["summary"], expected) == expected
    statements = frozen_statements(agent.id)
    assert statements[DEC].owed_in_statement_id == statements[NOV].id
    assert agent.id in {row["agent_user_id"] for row in pay_route(world, "GET", "/periods/2026-12")["agents"]}

    pay_route(world, "POST", "/periods/2026-12/approve")

    assert _owed_to_date(world) == {"amount": 0.0, "month": None, "nets_in": None}
    assert SalesPayStatementService.outstanding_owed(agent.id) is None

    water_order(world, 80, date(2027, 1, 12))
    move_clock(world, local_at(2027, 1, 12, 12))
    _sync_now(world, "2027-01")
    january = _summary(world, "2027-01")
    assert (january["carry_in"], january["total"]) == (NOTHING_BROUGHT_IN, 80000.0)

    _close(world, "2027-01", at=local_at(2027, 2, 2, 10))

    assert frozen_statements(agent.id)[JAN].owed_in_statement_id is None


def _january_frozen_before_december_is_approved(client, monkeypatch, admin_user, headers):
    """T-OWED-2 (c)'s months: December closed with no statement for Aziz, then January closed
    with his 80,000 credit, both before December is approved."""
    world = _november_balance(client, monkeypatch, admin_user, headers)
    _close(world, "2026-12", at=local_at(2027, 1, 2, 10))
    water_order(world, 80, date(2027, 1, 12))
    move_clock(world, local_at(2027, 1, 12, 12))
    _sync_now(world, "2027-01")
    _close(world, "2027-01", at=local_at(2027, 2, 2, 10))
    return world


def test_approving_a_month_without_a_statement_refreezes_the_next_one_to_net_the_balance(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-2 (c) (§4.9 steps 1 and 5). January froze while December was the first unapproved
    month, so it netted nothing: carry-in 0, paid 80,000. A5 December re-freezes January in the
    same transaction (revision 2), although Aziz had no December statement and received no carry:
    carry-in -50,000 from November, gross 30,000, paid 30,000, owed 0. A5 January clears the
    balance, and October to January paid 30,000 as in (a)."""
    world = _january_frozen_before_december_is_approved(client, monkeypatch, admin_user, admin_claim_headers)
    agent = world.agent
    before = _statement(world, "2027-01")

    assert (before["revision"], before["summary"]["carry_in"], before["summary"]["total"]) == (
        1,
        NOTHING_BROUGHT_IN,
        80000.0,
    )
    assert frozen_statements(agent.id)[JAN].owed_in_statement_id is None

    pay_route(world, "POST", "/periods/2026-12/approve")

    after = _statement(world, "2027-01")
    expected = {
        "carry_in": {"amount": -50000.0, "from_month": "2026-11", "source": "owed"},
        "gross_total": 30000.0,
        "total": 30000.0,
        "owed": 0.0,
    }
    assert after["revision"] == 2
    assert _only(after["summary"], expected) == expected
    statements = frozen_statements(agent.id)
    assert statements[JAN].owed_in_statement_id == statements[NOV].id

    pay_route(world, "POST", "/periods/2027-01/approve")

    assert _owed_to_date(world) == {"amount": 0.0, "month": None, "nets_in": None}
    paid = frozen_statements(agent.id)
    assert sum(paid[month].total_amount for month in (OCT, NOV, JAN)) == Decimal("30000")


def test_approve_refuses_a_month_that_should_net_a_balance_and_does_not(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-2 (c), the invariant guard (§4.9 step 1). After A5 December netted November's
    balance into January, January's pointer is cleared behind the service's back. A5 January is
    a 500 (no code: an invariant, not a user error): January stays closed, and no carry row and no
    push are written."""
    world = _january_frozen_before_december_is_approved(client, monkeypatch, admin_user, admin_claim_headers)
    pay_route(world, "POST", "/periods/2026-12/approve")
    statements = frozen_statements(world.agent.id)
    statements[JAN].owed_in_statement_id = None
    db.session.commit()
    pushes = spy_sales_pushes(monkeypatch)

    response = client.post(f"{PAY_API}/periods/2027-01/approve", json={}, headers=admin_claim_headers)

    db.session.expire_all()
    assert response.status_code == 500
    assert _months()[JAN].status == "closed"
    assert carry_rows() == []
    assert pushes == []


# --------------------------------------------------------------------------- #
# T-OWED-3: a balance is netted once, in month order
# --------------------------------------------------------------------------- #


def test_a_balance_is_netted_once_whatever_order_the_months_are_closed_in(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-3 (a), (b) and (c).

    (a) October (owed 100,000) and November (+50,000) are both closed before October is
    approved, so November nets nothing (paid 50,000) and December's estimate brings nothing in.
    A5 October re-freezes November to net October: carry-in -100,000, gross -50,000, paid 0, owed
    50,000; December still brings nothing in, because November is now the first unapproved month.
    (b) A4 twice re-freezes November twice with identical figures and pointer; an A11 -10,000
    into November makes gross -60,000 and owed 60,000, still netting October once.
    (c) A5 November approves once; a second A5 is a 409 that writes nothing; December's estimate
    now nets November's balance, never October's.

    December carries a 30,000 credit (1 December) so that Aziz HAS a December estimate: a balance
    alone is not activity (I-28), and a leaver with nothing in December has no December statement
    to read (A8 answers 404). The credit is not asserted; only where December's carry-in comes from.
    """
    world = carry_case(client, admin_claim_headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    agent = world.agent
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    water_order(world, 50, date(2026, 11, 5))
    water_order(world, 30, date(2026, 12, 1))
    _close(world, "2026-11", at=local_at(2026, 12, 2, 10))

    november = _summary(world, "2026-11")
    assert (november["carry_in"], november["gross_total"], november["total"]) == (NOTHING_BROUGHT_IN, 50000.0, 50000.0)
    assert frozen_statements(agent.id)[NOV].owed_in_statement_id is None
    assert _summary(world, "2026-12")["carry_in"] == NOTHING_BROUGHT_IN

    pay_route(world, "POST", "/periods/2026-10/approve")

    netted = {
        "carry_in": {"amount": -100000.0, "from_month": "2026-10", "source": "owed"},
        "gross_total": -50000.0,
        "total": 0.0,
        "owed": 50000.0,
    }
    refrozen = _statement(world, "2026-11")
    assert refrozen["revision"] == 2
    assert _only(refrozen["summary"], netted) == netted
    statements = frozen_statements(agent.id)
    assert statements[NOV].owed_in_statement_id == statements[OCT].id
    assert _summary(world, "2026-12")["carry_in"] == NOTHING_BROUGHT_IN

    for revision in (3, 4):
        pay_route(world, "POST", "/periods/2026-11/recalculate")
        again = _statement(world, "2026-11")
        assert again["revision"] == revision
        assert _only(again["summary"], netted) == netted
        assert frozen_statements(agent.id)[NOV].owed_in_statement_id == statements[OCT].id

    pay_route(
        world, "POST", f"/periods/2026-11/agents/{agent.id}/adjustments",
        {"amount": -10000, "reason": "Stock shortfall recovered"}, status=201,
    )

    deducted = _summary(world, "2026-11")
    assert (deducted["carry_in"], deducted["gross_total"], deducted["owed"]) == (netted["carry_in"], -60000.0, 60000.0)
    assert frozen_statements(agent.id)[NOV].owed_in_statement_id == statements[OCT].id

    pay_route(world, "POST", "/periods/2026-11/approve")
    second = pay_route(world, "POST", "/periods/2026-11/approve", status=409)

    assert second["error_code"] == "SALES_PAY_STATE_INVALID"
    assert _months()[NOV].status == "approved"
    assert carry_rows() == []
    assert _summary(world, "2026-12")["carry_in"] == {"amount": -60000.0, "from_month": "2026-11", "source": "owed"}


def test_three_closed_months_net_each_balance_once_as_they_are_approved(
    client, db, monkeypatch, admin_user, admin_claim_headers
):
    """T-OWED-3 (f). October, November (+50,000) and December (+30,000) are all closed before
    any approval, so November and December freeze netting nothing (paid 50,000 and 30,000).

    A5 October: November nets October (gross -50,000, paid 0, owed 50,000), and the cascade
    re-freezes December still netting nothing (paid 30,000). A5 November: December nets
    November (carry-in -50,000, gross -20,000, paid 0, owed 20,000), with no IntegrityError:
    October's statement is netted by November's, November's by December's. A5 December leaves
    20,000 = 100,000 - 50,000 - 30,000 owed, netted in January, and nothing was paid.
    """
    world = carry_case(client, admin_claim_headers, leaver=True, monkeypatch=monkeypatch, admin=admin_user)
    agent = world.agent
    _close(world, "2026-10", at=local_at(2026, 11, 2, 10))
    water_order(world, 50, date(2026, 11, 5))
    _close(world, "2026-11", at=local_at(2026, 12, 2, 10))
    water_order(world, 30, date(2026, 12, 7))
    _close(world, "2026-12", at=local_at(2027, 1, 2, 10))

    statements = frozen_statements(agent.id)
    assert (statements[NOV].owed_in_statement_id, statements[DEC].owed_in_statement_id) == (None, None)
    assert (_summary(world, "2026-11")["total"], _summary(world, "2026-12")["total"]) == (50000.0, 30000.0)

    pay_route(world, "POST", "/periods/2026-10/approve")

    november = _summary(world, "2026-11")
    december = _statement(world, "2026-12")
    assert (november["gross_total"], november["total"], november["owed"]) == (-50000.0, 0.0, 50000.0)
    assert (december["revision"], december["summary"]["carry_in"], december["summary"]["total"]) == (
        2,
        NOTHING_BROUGHT_IN,
        30000.0,
    )
    statements = frozen_statements(agent.id)
    assert (statements[NOV].owed_in_statement_id, statements[DEC].owed_in_statement_id) == (statements[OCT].id, None)

    pay_route(world, "POST", "/periods/2026-11/approve")

    netted = {
        "carry_in": {"amount": -50000.0, "from_month": "2026-11", "source": "owed"},
        "gross_total": -20000.0,
        "total": 0.0,
        "owed": 20000.0,
    }
    assert _only(_summary(world, "2026-12"), netted) == netted
    statements = frozen_statements(agent.id)
    assert statements[DEC].owed_in_statement_id == statements[NOV].id

    pay_route(world, "POST", "/periods/2026-12/approve")

    assert _owed_to_date(world) == {"amount": 20000.0, "month": "2026-12", "nets_in": "2027-01"}
    paid = frozen_statements(agent.id)
    assert sum(paid[month].total_amount for month in (OCT, NOV, DEC)) == Decimal("0")
