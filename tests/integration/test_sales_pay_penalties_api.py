"""Penalty types (A19-A21) and penalties (A22-A26), driven the way the admin UI drives them.

Compensation spec §4.11 (every guard), §5.2 (the wire shapes), §7.5 (the agent's push),
C6 (a manager proposes, an admin decides), I-12 (the type's amount is a default), I-13 (the
month a penalty lands in) and I-16 (a shadow month past `closed` takes no penalty).

Pay is configured through the admin routes: Task 7's `pay_world`, and `october_world`, which is
Illustration B's October on it. Every amount is a hand-computed literal, with the arithmetic in
the docstring. The "late" and "target month" answers are `route_manual`'s own, read back from the
routes that publish them.
"""

from datetime import date
from urllib.parse import urlencode

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from business_app.models.sales_pay import SalesAgentPayTerms, SalesPayAdjustment, SalesPayPenalty, SalesPayPenaltyType
from business_app.models.translation import Translation
from shared.staff_constants import SALES_EVENT_PAY_PENALTY_CONFIRMED
from tests.integration.sales_pay_builders import (
    PAY_API,
    PENALTIES_URL,
    PENALTY_TYPE_NAMES,
    PENALTY_TYPES_URL,
    PROPOSALS_URL,
    add_penalty_type,
    incident_body,
    local_at,
    logged_push_failures,
    move_clock,
    october_world,
    pay_audit,
    pay_call,
    pay_route,
    pay_world,
    pushes_of,
    refusal,
    spy_sales_pushes,
    staff_agent,
    water_order,
)
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture

pytestmark = pytest.mark.integration

PROPOSAL_KEYS = {
    "id",
    "agent",
    "type",
    "incident_date",
    "reason",
    "evidence",
    "status",
    "origin",
    "proposed_by",
    "proposed_at",
    "decided_at",
}
ADMIN_PENALTY_KEYS = PROPOSAL_KEYS | {
    "amount",
    "default_amount",
    "posting_month",
    "is_late",
    "target_month",
    "target_is_late",
    "decided_by",
    "decision_note",
    "cancelled_at",
    "cancel_reason",
    "self_decided",
    "can_confirm",
    "can_reject",
    "can_cancel",
}
AMOUNT_REFUSED = {"field": "amount", "validation_errors": []}


@pytest.fixture
def commit_log():
    """Every session commit, in order. The push spy appends to the same list (`trail=`)."""
    log = []

    def _committed(_session):
        log.append("commit")

    event.listen(Session, "after_commit", _committed)
    yield log
    event.remove(Session, "after_commit", _committed)


def _statement(world, agent, month="2026-10"):
    return pay_route(world, "GET", f"/periods/{month}/agents/{agent.id}")


def _penalty_rows(world, **query):
    path = f"/penalties?{urlencode(query)}" if query else "/penalties"
    return {row["id"]: row for row in pay_route(world, "GET", path)["items"]}


# --------------------------------------------------------------------------- #
# Penalty types (A19-A21)
# --------------------------------------------------------------------------- #


def test_a_penalty_type_is_named_in_three_languages_and_priced_for_admins_only(
    client, db, admin_claim_headers, manager_claim_headers
):
    """A20 writes the canonical English name AND all three `Translation` rows, `en` included
    (§3.2: the loyalty-guide gotcha, where the column held English and no `en` row existed).
    A19 lists the type back. A manager's claim is refused on all three routes, by the decorator:
    a default amount is pay (C11)."""
    body = {"names": PENALTY_TYPE_NAMES, "default_amount": 150000}

    created = pay_call(client, "POST", PENALTY_TYPES_URL, admin_claim_headers, body, status=201)["type"]

    assert created == {"id": created["id"], "names": PENALTY_TYPE_NAMES, "default_amount": 150000, "is_active": True}
    row = db.session.get(SalesPayPenaltyType, created["id"])
    assert row.name == "Missed planned visits"
    stored = {t.language: t.value for t in Translation.query.filter_by(key=f"SalesPayPenaltyType.name.{row.id}")}
    assert stored == PENALTY_TYPE_NAMES
    assert pay_call(client, "GET", PENALTY_TYPES_URL, admin_claim_headers)["items"] == [created]

    refused = [
        pay_call(client, "GET", PENALTY_TYPES_URL, manager_claim_headers, status=403),
        pay_call(client, "POST", PENALTY_TYPES_URL, manager_claim_headers, body, status=403),
        pay_call(client, "PATCH", f"{PENALTY_TYPES_URL}/{row.id}", manager_claim_headers, {"is_active": False}, status=403),
    ]
    assert [answer.get("error_code") for answer in refused] == [None, None, None]
    assert SalesPayPenaltyType.query.count() == 1


def test_a_penalty_type_is_renamed_repriced_and_retired_never_deleted(client, db, admin_claim_headers):
    """A21 changes the names, the default and the active flag, each on its own when sent alone.
    An unknown type is a coded 404."""
    created = pay_call(
        client, "POST", PENALTY_TYPES_URL, admin_claim_headers, {"names": PENALTY_TYPE_NAMES, "default_amount": 150000},
        status=201,
    )["type"]
    renamed = {"en": "Late start", "uz": "Kech boshlash", "ru": "Поздний выход"}

    updated = pay_call(
        client,
        "PATCH",
        f"{PENALTY_TYPES_URL}/{created['id']}",
        admin_claim_headers,
        {"names": renamed, "default_amount": 80000, "is_active": False},
    )["type"]

    assert updated == {"id": created["id"], "names": renamed, "default_amount": 80000, "is_active": False}
    db.session.expire_all()
    assert db.session.get(SalesPayPenaltyType, created["id"]).name == "Late start"
    only_price = pay_call(
        client, "PATCH", f"{PENALTY_TYPES_URL}/{created['id']}", admin_claim_headers, {"default_amount": 90000}
    )["type"]
    assert (only_price["names"], only_price["default_amount"], only_price["is_active"]) == (renamed, 90000, False)
    missing = refusal(
        client, "PATCH", f"{PENALTY_TYPES_URL}/999999", admin_claim_headers, {"is_active": True},
        status=404, code="SALES_PAY_NOT_FOUND",
    )
    assert missing == {"resource": "penalty_type", "id": 999999}


def test_a_penalty_type_needs_three_names_and_a_whole_positive_default(client, db, admin_claim_headers):
    """All three names are required (a 400 from the schema); the default is whole UZS above zero
    (SALES_PAY_AMOUNT_INVALID, refused, never rounded); a mistyped key is refused, not ignored."""
    no_uz = {"en": "Missed planned visits", "ru": "Пропущенные плановые визиты"}

    pay_call(client, "POST", PENALTY_TYPES_URL, admin_claim_headers, {"names": no_uz, "default_amount": 1000}, status=400)
    for bad in (0, -5000, "1500.5"):
        details = refusal(
            client,
            "POST",
            PENALTY_TYPES_URL,
            admin_claim_headers,
            {"names": PENALTY_TYPE_NAMES, "default_amount": bad},
            status=400,
            code="SALES_PAY_AMOUNT_INVALID",
        )
        assert details == {"field": "default_amount", "validation_errors": []}
    pay_call(
        client, "POST", PENALTY_TYPES_URL, admin_claim_headers, {"names": PENALTY_TYPE_NAMES, "defaultAmount": 1000},
        status=400,
    )
    assert SalesPayPenaltyType.query.count() == 0


# --------------------------------------------------------------------------- #
# Penalties (A22-A26) and the rows the admin reads them through
# --------------------------------------------------------------------------- #


def test_confirming_a_proposal_counts_it_and_tells_the_agent_once_after_commit(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user, commit_log
):
    """T-PEN-2. The manager proposes; the admin confirms at the type's default (I-12).

    150,000 lands in October, which is open, so it is not late, and the October estimate carries
    it. One push goes out, AFTER the commit that confirmed the penalty. It carries the §7.5
    payload (the reason, never the evidence) and the domain id `penalty-confirmed:<id>`. A
    second confirm is a 409 and pushes nothing.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent = sales_agent_user
    pushes = spy_sales_pushes(monkeypatch, trail=commit_log)
    proposal = pay_call(
        client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, world.penalty_type_id, "2026-10-14"),
        status=201,
    )["proposal"]
    commit_log.clear()

    confirmed = pay_route(world, "POST", f"/penalties/{proposal['id']}/confirm", {})["penalty"]

    assert commit_log[-1] == "push" and "commit" in commit_log[:-1]
    assert pushes_of(pushes, SALES_EVENT_PAY_PENALTY_CONFIRMED) == [
        (
            777000201,
            {
                "penalty_id": proposal["id"],
                "month": "2026-10",
                "incident_date": "2026-10-14",
                "type_names": PENALTY_TYPE_NAMES,
                "reason": "Did not visit 5 planned outlets",
                "amount": 150000,
                "is_late": False,
            },
            f"penalty-confirmed:{proposal['id']}",
        )
    ]
    assert set(confirmed) == ADMIN_PENALTY_KEYS
    assert (confirmed["status"], confirmed["amount"], confirmed["posting_month"], confirmed["is_late"]) == (
        "confirmed",
        150000,
        "2026-10",
        False,
    )
    assert (confirmed["can_confirm"], confirmed["can_reject"], confirmed["can_cancel"]) == (False, False, True)
    assert _statement(world, agent)["summary"]["penalties"] == 150000

    again = refusal(
        client, "POST", f"{PENALTIES_URL}/{proposal['id']}/confirm", admin_claim_headers, {},
        status=409, code="SALES_PAY_STATE_INVALID",
    )

    assert again == {"resource": "penalty", "id": proposal["id"], "status": "confirmed", "action": "confirm", "allowed": ["proposed"]}
    assert len(pushes_of(pushes, SALES_EVENT_PAY_PENALTY_CONFIRMED)) == 1


def test_reject_direct_and_cancel_follow_the_state_machine(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-PEN-3.

    - A rejection is never counted and pushes nothing.
    - A23 books `origin: direct`, confirmed and pushed, with the admin as proposer and decider.
    - A cancellation, in an open month and then in a closed one, drops the penalty from the
      (re-frozen) statement and pushes nothing. After approval it is 409 MONTH_LOCKED.
    - M1 never lists a direct row.

    October penalties, in order: 150,000 (A23 at the default) + 60,000 (A23 with an amount) =
    210,000. Cancelling the first while October is open leaves 60,000. The close freezes 60,000
    at revision 1; cancelling the second gives revision 2 with 0. A23 of 40,000 into the closed
    month gives revision 3 with 40,000. After approval, cancelling it is refused, and the
    statement still shows 40,000.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id = sales_agent_user, world.penalty_type_id
    pushes = spy_sales_pushes(monkeypatch)
    proposal = pay_call(
        client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, type_id, "2026-10-14"), status=201
    )["proposal"]

    rejected = pay_route(world, "POST", f"/penalties/{proposal['id']}/reject", {"note": "Visits were logged late"})["penalty"]

    assert (rejected["status"], rejected["decision_note"], rejected["amount"], rejected["posting_month"]) == (
        "rejected",
        "Visits were logged late",
        None,
        None,
    )
    assert (rejected["can_confirm"], rejected["can_reject"], rejected["can_cancel"]) == (False, False, False)
    assert _statement(world, agent)["summary"]["penalties"] == 0

    first = pay_route(world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-09"), status=201)["penalty"]
    second = pay_route(
        world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-12", amount=60000), status=201
    )["penalty"]

    assert (first["origin"], first["status"], first["amount"], first["posting_month"]) == (
        "direct",
        "confirmed",
        150000,
        "2026-10",
    )
    assert first["proposed_by"]["id"] == first["decided_by"]["id"] == admin_user.id
    assert _statement(world, agent)["summary"]["penalties"] == 210000

    cancelled = pay_route(
        world, "POST", f"/penalties/{first['id']}/cancel", {"note": "Duplicate of another penalty"}
    )["penalty"]

    assert (cancelled["status"], cancelled["cancel_reason"], cancelled["can_cancel"]) == (
        "cancelled",
        "Duplicate of another penalty",
        False,
    )
    assert _statement(world, agent)["summary"]["penalties"] == 60000

    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    closed = _statement(world, agent)
    assert (closed["revision"], closed["summary"]["penalties"]) == (1, 60000)

    pay_route(world, "POST", f"/penalties/{second['id']}/cancel", {"note": "Agent was on leave that day"})
    closed = _statement(world, agent)
    assert (closed["revision"], closed["summary"]["penalties"]) == (2, 0)

    third = pay_route(
        world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-13", amount=40000), status=201
    )["penalty"]
    assert (third["posting_month"], third["is_late"]) == ("2026-10", False)
    closed = _statement(world, agent)
    assert (closed["revision"], closed["summary"]["penalties"]) == (3, 40000)

    pay_route(world, "POST", "/periods/2026-10/approve")
    assert _penalty_rows(world)[third["id"]]["can_cancel"] is False
    locked = refusal(
        client, "POST", f"{PENALTIES_URL}/{third['id']}/cancel", admin_claim_headers, {"note": "Too late to undo"},
        status=409, code="SALES_PAY_MONTH_LOCKED",
    )

    assert locked["status"] == "approved"
    db.session.expire_all()
    assert db.session.get(SalesPayPenalty, third["id"]).status == "confirmed"
    assert _statement(world, agent)["summary"]["penalties"] == 40000
    assert [event_id for _tid, _payload, event_id in pushes_of(pushes, SALES_EVENT_PAY_PENALTY_CONFIRMED)] == [
        f"penalty-confirmed:{first['id']}",
        f"penalty-confirmed:{second['id']}",
        f"penalty-confirmed:{third['id']}",
    ]
    listed = pay_call(client, "GET", PROPOSALS_URL, manager_claim_headers)["items"]
    assert [row["id"] for row in listed] == [proposal["id"]]


def test_confirming_into_a_closed_month_refreezes_its_statement(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """A24 into a CLOSED month (I-13; §4.9 "Re-freeze on input change").

    A proposal for 14 October is still pending when October closes on 2 November, so the frozen
    statement (revision 1) counts no penalty: base 2,800,000, nothing else, paid 2,800,000. A
    closed month still takes its inputs, so the pending row targets October itself, not late.
    Confirming it books October and re-freezes the statement in the same transaction: revision 2,
    penalties 150,000, paid 2,800,000 - 150,000 = 2,650,000, and A8's `penalties` lists the row.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent = sales_agent_user
    proposal = pay_call(
        client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, world.penalty_type_id, "2026-10-14"),
        status=201,
    )["proposal"]
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    before = _statement(world, agent)
    assert (before["revision"], before["summary"]["penalties"], before["summary"]["total"], before["penalties"]) == (
        1,
        0,
        2800000,
        [],
    )
    pending = _penalty_rows(world, status="proposed")[proposal["id"]]
    assert (pending["target_month"], pending["target_is_late"], pending["can_confirm"]) == ("2026-10", False, True)

    confirmed = pay_route(world, "POST", f"/penalties/{proposal['id']}/confirm", {})["penalty"]

    assert (confirmed["status"], confirmed["posting_month"], confirmed["is_late"]) == ("confirmed", "2026-10", False)
    after = _statement(world, agent)
    assert (after["status"], after["revision"], after["summary"]["penalties"], after["summary"]["total"]) == (
        "closed",
        2,
        150000,
        2650000,
    )
    assert [(row["id"], row["status"], row["amount"]) for row in after["penalties"]] == [
        (proposal["id"], "confirmed", 150000)
    ]


def test_a_push_the_broker_refuses_never_undoes_a_booked_penalty(
    client, db, monkeypatch, caplog, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user, audit_events
):
    """The penalty is committed before its push is queued (§7.5), so a broker that refuses the
    push costs the message and nothing else. A24 answers 200 with the proposal confirmed and its
    audit row written; A23 answers 201 likewise; each refused push is logged once, naming its
    penalty. A 500 here would send the admin's retry into a 409 while the agent was never told.
    October then counts both: 150,000 + 150,000 = 300,000.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id = sales_agent_user, world.penalty_type_id
    proposal = pay_call(
        client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, type_id, "2026-10-14"), status=201
    )["proposal"]
    pushes = spy_sales_pushes(monkeypatch, fail_for=(777000201,))

    with logged_push_failures(caplog) as failures:
        confirmed = pay_route(world, "POST", f"/penalties/{proposal['id']}/confirm", {})["penalty"]
        booked = pay_route(world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-09"), status=201)[
            "penalty"
        ]

    assert (confirmed["status"], booked["status"]) == ("confirmed", "confirmed")
    db.session.expire_all()
    assert [db.session.get(SalesPayPenalty, row["id"]).status for row in (confirmed, booked)] == ["confirmed"] * 2
    assert [row[1] for row in pay_audit(audit_events, "penalty_confirmed")] == [str(proposal["id"])]
    assert [row[1] for row in pay_audit(audit_events, "penalty_created")] == [str(booked["id"])]
    assert pushes == []
    assert len(failures) == 2
    assert [f"penalty {row['id']} " in failure.getMessage() for row, failure in zip((confirmed, booked), failures)] == [
        True,
        True,
    ]
    assert _statement(world, agent)["summary"]["penalties"] == 300000


def test_penalties_beyond_the_variable_reduce_the_base(
    client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """T-PEN-4 (C1, C6).

    Illustration B's terms give base 2,800,000. One order of 100 x 19 L at 1,000 per unit,
    delivered and paid on 13 October, credits 100,000 at x1.0 (no day plans, so `below_min_due`).
    One penalty of 300,000 (A23 with an amount).
    Variable = 100,000 - 300,000 = -200,000, never floored.
    Gross = 2,800,000 - 200,000 = 2,600,000, paid in full; nothing is carried or owed.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    water_order(world, 100, date(2026, 10, 13))
    move_clock(world, local_at(2026, 10, 15))
    # The Pay page's "Sync now": an estimate read syncs an agent at most once per
    # SALES_PAY_ONDEMAND_SYNC_SECONDS, and `october_world`'s reads already did.
    pay_route(world, "POST", "/periods/2026-10/recalculate")
    pay_route(
        world,
        "POST",
        "/penalties",
        incident_body(sales_agent_user, world.penalty_type_id, "2026-10-13", amount=300000),
        status=201,
    )

    summary = _statement(world, sales_agent_user)["summary"]

    assert {key: summary["commission"][key] for key in ("gross", "orders", "after_gate")} == {
        "gross": 100000, "orders": 1, "after_gate": 100000,
    }
    assert [(p["units"], p["total"]) for p in summary["commission"]["products"]] == [(100, 100000)]
    assert summary["gate"]["rule"] == "below_min_due"
    assert {
        key: summary[key] for key in ("base_amount", "penalties", "variable", "gross_total", "total", "carry_out", "owed")
    } == {
        "base_amount": 2800000,
        "penalties": 300000,
        "variable": -200000,
        "gross_total": 2600000,
        "total": 2600000,
        "carry_out": 0,
        "owed": 0,
    }


def test_a_manager_is_refused_every_admin_pay_write(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-PEN-5. A23-A26, A11 and A17 are `@super_admin_required`. A manager's claim gets the
    decorator's 403, never SALES_PAY_SELF_DECISION, and nothing is written."""
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id = sales_agent_user, world.penalty_type_id
    proposal = pay_call(
        client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, type_id, "2026-10-14"), status=201
    )["proposal"]

    def counts():
        return SalesPayPenalty.query.count(), SalesPayAdjustment.query.count(), SalesAgentPayTerms.query.count()

    before = counts()
    writes = [
        ("POST", PENALTIES_URL, incident_body(agent, type_id, "2026-10-09")),
        ("POST", f"{PENALTIES_URL}/{proposal['id']}/confirm", {}),
        ("POST", f"{PENALTIES_URL}/{proposal['id']}/reject", {"note": "Not a real fault"}),
        ("POST", f"{PENALTIES_URL}/{proposal['id']}/cancel", {"note": "Not a real fault"}),
        ("POST", f"{PAY_API}/periods/2026-10/agents/{agent.id}/adjustments", {"amount": 50000, "reason": "Guarantee top-up"}),
        (
            "POST",
            f"{PAY_API}/agents/{agent.id}/terms",
            {"effective_month": "2026-10", "base_salary": 1, "plan_id": world.plan["id"]},
        ),
    ]

    answers = [pay_call(client, method, path, manager_claim_headers, body, status=403) for method, path, body in writes]

    assert all(answer.get("error_code") != "SALES_PAY_SELF_DECISION" for answer in answers)
    assert counts() == before
    db.session.expire_all()
    assert db.session.get(SalesPayPenalty, proposal["id"]).status == "proposed"


def test_after_approval_a_confirmation_lands_late_in_the_next_open_month(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-LOCK-1, the penalty items (I-13).

    October is approved on 2 November. A pending proposal for an October incident is
    published with `target_month "2026-11"` and `target_is_late true`: the answer
    `route_manual` gives and `confirm` then uses. Confirming it books November as a late line,
    which is not an error. Cancelling a penalty booked into October is 409 MONTH_LOCKED.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id = sales_agent_user, world.penalty_type_id
    pushes = spy_sales_pushes(monkeypatch)
    booked = pay_route(world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-09"), status=201)["penalty"]
    pending = pay_call(
        client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, type_id, "2026-10-14"), status=201
    )["proposal"]
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    pay_route(world, "POST", "/periods/2026-10/approve")

    row = _penalty_rows(world, status="proposed")[pending["id"]]
    assert (row["target_month"], row["target_is_late"], row["can_confirm"]) == ("2026-11", True, True)

    confirmed = pay_route(world, "POST", f"/penalties/{pending['id']}/confirm", {})["penalty"]

    assert (confirmed["posting_month"], confirmed["is_late"]) == ("2026-11", True)
    [(_tid, payload, _event_id)] = [
        push for push in pushes_of(pushes, SALES_EVENT_PAY_PENALTY_CONFIRMED) if push[1]["penalty_id"] == pending["id"]
    ]
    assert (payload["month"], payload["is_late"]) == ("2026-11", True)
    locked = refusal(
        client, "POST", f"{PENALTIES_URL}/{booked['id']}/cancel", admin_claim_headers, {"note": "Booked by mistake"},
        status=409, code="SALES_PAY_MONTH_LOCKED",
    )
    assert locked["status"] == "approved"


def test_confirming_for_an_agent_without_terms_is_refused(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-TERMS-1, the A24 half. Dilshod was hired on 6 October and has no terms: a proposal is
    accepted (terms are not a propose guard), and confirming it is 409 TERMS_MISSING, naming the
    agent and the month. Nothing is booked and nobody is pushed."""
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    newcomer = staff_agent(db, phone="+998901238202", first_name="Dilshod", telegram_id="777000202")
    pay_route(world, "PUT", f"/agents/{newcomer.id}/employment", {"start": "2026-10-06", "end": None})
    pushes = spy_sales_pushes(monkeypatch)
    proposal = pay_call(
        client,
        "POST",
        PROPOSALS_URL,
        manager_claim_headers,
        incident_body(newcomer, world.penalty_type_id, "2026-10-09"),
        status=201,
    )["proposal"]

    refused = refusal(
        client, "POST", f"{PENALTIES_URL}/{proposal['id']}/confirm", admin_claim_headers, {},
        status=409, code="SALES_PAY_TERMS_MISSING",
    )

    assert refused == {"agents": [newcomer.id], "month": "2026-10"}
    db.session.expire_all()
    row = db.session.get(SalesPayPenalty, proposal["id"])
    assert (row.status, row.amount, row.period_id) == ("proposed", None, None)
    assert pushes_of(pushes, SALES_EVENT_PAY_PENALTY_CONFIRMED) == []


def test_every_penalty_path_checks_start_agent_type_date_and_texts(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """§4.11 "Every penalty path": each refusal through the route that meets it, with its
    details, and nothing written. Before pay starts both write routes answer NOT_STARTED. Then
    pay starts on 1 October with the agent employed from 6 October, and the clock stands at 10:00
    on Thursday 15 October 2026."""
    agent, admin, manager = sales_agent_user, admin_claim_headers, manager_claim_headers
    retired = pay_call(
        client, "POST", PENALTY_TYPES_URL, admin, {"names": PENALTY_TYPE_NAMES, "default_amount": 150000}, status=201
    )["type"]
    for route, headers in ((PENALTIES_URL, admin), (PROPOSALS_URL, manager)):
        not_started = refusal(
            client, "POST", route, headers, incident_body(agent, retired["id"], "2026-10-09"),
            status=409, code="SALES_PAY_NOT_STARTED",
        )
        assert not_started == {}

    world = pay_world(db, client, monkeypatch, admin_user, admin, agent, employment_start="2026-10-06")
    pay_route(world, "PATCH", f"/penalty-types/{retired['id']}", {"is_active": False})
    type_id = add_penalty_type(world, default_amount=150000)
    move_clock(world, local_at(2026, 10, 15))

    def refused(status, code, **overrides):
        body = {**incident_body(agent, type_id, "2026-10-09"), **overrides}
        return refusal(client, "POST", PENALTIES_URL, admin, body, status=status, code=code)

    assert refused(400, "SALES_PAY_DATE_INVALID", incident_date="2026-10-16") == {
        "field": "incident_date",
        "date": "2026-10-16",
        "reason": "future",
        "validation_errors": [],
    }
    assert refused(400, "SALES_PAY_DATE_INVALID", incident_date="2026-09-30")["reason"] == "before_pay_start"
    assert refused(400, "SALES_PAY_DATE_INVALID", incident_date="2026-10-05") == {
        "field": "incident_date",
        "date": "2026-10-05",
        "reason": "not_employed",
        "validation_errors": [],
    }
    assert refused(404, "SALES_PAY_NOT_FOUND", agent_user_id=admin_user.id) == {"resource": "agent", "id": admin_user.id}
    assert refused(404, "SALES_PAY_NOT_FOUND", penalty_type_id=999999) == {"resource": "penalty_type", "id": 999999}
    reason_limits = {"field": "reason", "min_length": 5, "max_length": 500, "validation_errors": []}
    assert refused(400, "SALES_PAY_REASON_REQUIRED", reason="Late") == reason_limits
    assert refused(400, "SALES_PAY_REASON_REQUIRED", reason="x" * 501) == reason_limits
    assert refused(400, "SALES_PAY_REASON_REQUIRED", evidence="None") == {
        "field": "evidence",
        "min_length": 5,
        "max_length": 2000,
        "validation_errors": [],
    }
    for amount in (1500.5, 0, -100):
        assert refused(400, "SALES_PAY_AMOUNT_INVALID", amount=amount) == AMOUNT_REFUSED
    for route, headers in ((PENALTIES_URL, admin), (PROPOSALS_URL, manager)):
        inactive = refusal(
            client, "POST", route, headers, incident_body(agent, retired["id"], "2026-10-09"),
            status=409, code="SALES_PAY_PENALTY_TYPE_INACTIVE",
        )
        assert inactive == {"penalty_type_id": retired["id"]}
    future = refusal(
        client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-16"),
        status=400, code="SALES_PAY_DATE_INVALID",
    )
    assert future["reason"] == "future"
    assert SalesPayPenalty.query.count() == 0

    proposal = pay_call(client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-09"), status=201)[
        "proposal"
    ]
    missing = refusal(client, "POST", f"{PENALTIES_URL}/999999/confirm", admin, {}, status=404, code="SALES_PAY_NOT_FOUND")
    assert missing == {"resource": "penalty", "id": 999999}
    terse = refusal(
        client, "POST", f"{PENALTIES_URL}/{proposal['id']}/reject", admin, {"note": "No"},
        status=400, code="SALES_PAY_REASON_REQUIRED",
    )
    assert terse == {"field": "note", "min_length": 5, "max_length": 500, "validation_errors": []}
    too_early = refusal(
        client, "POST", f"{PENALTIES_URL}/{proposal['id']}/cancel", admin, {"note": "Nothing to cancel"},
        status=409, code="SALES_PAY_STATE_INVALID",
    )
    assert too_early == {
        "resource": "penalty",
        "id": proposal["id"],
        "status": "proposed",
        "action": "cancel",
        "allowed": ["confirmed"],
    }
    db.session.expire_all()
    assert db.session.get(SalesPayPenalty, proposal["id"]).status == "proposed"


def test_the_list_the_month_badge_and_the_drawer_share_one_row(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """A22 filters on status, on agent and on the POSTING month (§5.2).

    A1 publishes the pending count, the Penalties tab badge, and nowhere else. A8's `penalties`
    lists what is posted to the agent's month, confirmed or cancelled. All three are
    `AdminPenaltyRow`s. A pending row is published with where a confirm would land.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id, manager = sales_agent_user, world.penalty_type_id, manager_claim_headers
    first = pay_call(client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-14"), status=201)[
        "proposal"
    ]
    second = pay_call(client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-13"), status=201)[
        "proposal"
    ]
    booked = pay_route(
        world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-09", amount=70000), status=201
    )["penalty"]

    assert pay_route(world, "GET", "/periods")["pending_penalty_count"] == 2
    proposed = pay_route(world, "GET", "/penalties?status=proposed")
    assert {row["id"] for row in proposed["items"]} == {first["id"], second["id"]}
    assert proposed["statuses"] == ["proposed", "confirmed", "rejected", "cancelled"]
    assert proposed["meta"]["total"] == 2
    for row in proposed["items"]:
        assert set(row) == ADMIN_PENALTY_KEYS
        assert (row["amount"], row["default_amount"], row["posting_month"], row["self_decided"]) == (None, 150000, None, False)
        assert (row["target_month"], row["target_is_late"]) == ("2026-10", False)
        assert (row["can_confirm"], row["can_reject"], row["can_cancel"]) == (True, True, False)
    assert list(_penalty_rows(world, month="2026-10")) == [booked["id"]]
    assert set(_penalty_rows(world, agent_id=agent.id)) == {first["id"], second["id"], booked["id"]}
    assert _penalty_rows(world, agent_id=agent.id + 1000) == {}
    refusal(client, "GET", f"{PENALTIES_URL}?month=2026-13", admin_claim_headers, status=400, code="SALES_PAY_MONTH_INVALID")
    pay_call(client, "GET", f"{PENALTIES_URL}?status=approved", admin_claim_headers, status=400)

    pay_route(world, "POST", f"/penalties/{first['id']}/confirm", {})

    assert pay_route(world, "GET", "/periods")["pending_penalty_count"] == 1
    drawer = _statement(world, agent)["penalties"]
    assert {row["id"]: row["status"] for row in drawer} == {booked["id"]: "confirmed", first["id"]: "confirmed"}
    assert all(set(row) == ADMIN_PENALTY_KEYS for row in drawer)


def test_a_shadow_months_incident_is_booked_into_the_trial_month_or_not_at_all(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-SHADOW-1, the penalty half (I-16). October is the shadow month.

    A proposal for 20 October confirmed on 31 October, while October is open, lands in October.
    Once October is approved (3 November) an incident dated 20 October has nowhere to go: the
    trial month is final, and routing it late into November would make it cost real money only
    because it was confirmed after the trial. A24, A23 and M2 each refuse with MONTH_LOCKED
    (reason "shadow") and write nothing, and a pending proposal publishes no target month and no
    Confirm. The approval push's `is_shadow: true` is pinned by the shadow carry case.
    """
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user, is_shadow=True)
    agent, manager = sales_agent_user, manager_claim_headers
    agent.telegram_id = "777000221"
    db.session.commit()
    type_id = add_penalty_type(world, default_amount=150000)
    move_clock(world, local_at(2026, 10, 21))
    confirmable = pay_call(
        client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-20"), status=201
    )["proposal"]
    stranded = pay_call(
        client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-20"), status=201
    )["proposal"]
    move_clock(world, local_at(2026, 10, 31))

    in_time = pay_route(world, "POST", f"/penalties/{confirmable['id']}/confirm", {})["penalty"]

    assert (in_time["posting_month"], in_time["is_late"]) == ("2026-10", False)
    move_clock(world, local_at(2026, 11, 2, 10))
    pay_route(world, "POST", "/periods/2026-10/close")
    move_clock(world, local_at(2026, 11, 3, 10))
    pay_route(world, "POST", "/periods/2026-10/approve")
    pushes = spy_sales_pushes(monkeypatch)
    shadow = {"month": "2026-10", "status": "approved", "reason": "shadow"}

    row = _penalty_rows(world, status="proposed")[stranded["id"]]
    assert (row["target_month"], row["target_is_late"], row["can_confirm"], row["can_reject"]) == (None, False, False, True)
    refusals = [
        refusal(client, "POST", f"{PENALTIES_URL}/{stranded['id']}/confirm", admin_claim_headers, {},
                status=409, code="SALES_PAY_MONTH_LOCKED"),
        refusal(client, "POST", PENALTIES_URL, admin_claim_headers, incident_body(agent, type_id, "2026-10-20"),
                status=409, code="SALES_PAY_MONTH_LOCKED"),
        refusal(client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-20"),
                status=409, code="SALES_PAY_MONTH_LOCKED"),
    ]
    assert refusals == [shadow, shadow, shadow]
    db.session.expire_all()
    assert (db.session.get(SalesPayPenalty, stranded["id"]).status, SalesPayPenalty.query.count()) == ("proposed", 2)
    assert pushes == []
