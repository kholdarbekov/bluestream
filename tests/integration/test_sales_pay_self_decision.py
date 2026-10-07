"""The self-decision rule (D-Q11) on every pay write that names an agent, driven as the UI drives it.

Compensation spec §4.13 and §4.17: T-SELFDEC-1, and the statement half of T-NEW-11 (also
T-HOLD-10's statement half).

Anvar is an ADMIN who also holds a sales-agent profile. Every decision about his own pay is
allowed and stored with him as its actor. It is tagged on the statement it feeds (A2
`self_decided`, A8 `self_decisions`), on the penalty row (`self_decided`) and on its audit row.
Mansur is a MANAGER who holds a profile: M2, the one pay write a manager reaches, refuses him
about himself, and the admin routes refuse him at the decorator. The guard is
`pay_rules.check_self_decision`; its one-raise-site pin is in tests/unit/test_sales_pay_guards.py.
"""

import pytest

from business_app.models.sales_pay import (
    SalesAgentPayTerms,
    SalesAgentUnpaidDay,
    SalesPayAdjustment,
    SalesPayPenalty,
    SalesPayStatement,
)
from business_app.models.sales_visits import AgentOrderApproval
from business_app.serializers.sales_serializers import _iso
from shared.enums import UserRole
from tests.integration.sales_pay_builders import (
    APPROVALS,
    ILLUSTRATION_B_UNPAID,
    PAY_API,
    PROPOSALS_URL,
    add_penalty_type,
    admin_agent,
    approved_outlet,
    claim_headers,
    collect_cash_at,
    deliver,
    freeze_local_now,
    grocery_store,
    incident_body,
    local_at,
    move_clock,
    pay_call,
    pay_route,
    pay_world,
    place_two_same_day_orders,
    refusal,
    staff_agent,
    start_pay,
    sync_agent,
    utc_at,
    water,  # noqa: F401 -- a fixture
)
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture
from tests.unit.test_outlet_dedupe import PIN

pytestmark = pytest.mark.integration

# The pay writes Anvar makes about himself below, each audited with `self_decided: true`.
# Proposals (M2) are not audited (§4.17 has no proposal verb).
SELF_AUDITED_VERBS = (
    "employment_set",
    "terms_added",
    "unpaid_days_set",
    "adjustment_created",
    "penalty_created",
    "penalty_confirmed",
    "penalty_rejected",
)


def _rows(world, status: str) -> dict:
    """A22 filtered by status, as the Penalties tab's status select sends it."""
    return {row["id"]: row for row in pay_route(world, "GET", f"/penalties?status={status}")["items"]}


def test_an_admin_decides_about_his_own_pay_and_every_admin_surface_says_so(
    app, client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user, audit_events
):
    """T-SELFDEC-1, the admin half.

    Admin Bobur set pay up for October with Aziz (`pay_world`). As Anvar, about himself: A18
    200, A17 201, A10 with two dates 200, A11 +50,000 201, A23 (direct) 201. Manager Malika
    proposes P about Anvar, and Anvar confirms it (A24 200). She proposes P2, and Anvar rejects
    it (A25 200). Anvar proposes about himself through M2 (201): a proposal is not a decision
    about pay, so an admin may file one, and its row is tagged.

    A2 (read by Bobur) tags Anvar's row and not Aziz's. A8 lists exactly seven decisions,
    sorted by `at`: terms/added, employment/set, unpaid_day/set x2 (with their dates),
    adjustment/created, penalty/created (the direct one) and penalty/confirmed (P). P has no
    `proposed` entry, because Malika proposed it, and neither the rejected P2 nor the pending
    proposal is a statement input. A3 by Anvar freezes that list into `inputs.self_decisions`,
    and A8 then reads the frozen JSON, not the tables. A5 by Anvar is allowed too: close and
    approve are company-wide and are never tagged.
    """
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    type_id = add_penalty_type(world, default_amount=150000)
    move_clock(world, local_at(2026, 10, 15))
    anvar = staff_agent(db, phone="+998901238401", first_name="Anvar", role=UserRole.ADMIN)
    anvar_headers = claim_headers(app, anvar, "admin")

    def as_anvar(method, path, payload=None, *, status=200):
        return pay_call(client, method, f"{PAY_API}{path}", anvar_headers, payload, status=status)

    def as_malika(day):
        body = incident_body(anvar, type_id, day)
        return pay_call(client, "POST", PROPOSALS_URL, manager_claim_headers, body, status=201)["proposal"]

    first_event = len(audit_events)
    as_anvar("PUT", f"/agents/{anvar.id}/employment", {"start": "2026-10-01", "end": None})
    as_anvar(
        "POST", f"/agents/{anvar.id}/terms",
        {"effective_month": "2026-10", "base_salary": "3000000", "plan_id": world.plan["id"]}, status=201,
    )
    as_anvar("PUT", f"/periods/2026-10/agents/{anvar.id}/unpaid-days", ILLUSTRATION_B_UNPAID)
    as_anvar(
        "POST", f"/periods/2026-10/agents/{anvar.id}/adjustments",
        {"amount": 50000, "reason": "Guarantee top-up"}, status=201,
    )
    direct = as_anvar("POST", "/penalties", incident_body(anvar, type_id, "2026-10-09"), status=201)["penalty"]
    proposal = as_malika("2026-10-12")
    as_anvar("POST", f"/penalties/{proposal['id']}/confirm", {})
    second = as_malika("2026-10-13")
    as_anvar("POST", f"/penalties/{second['id']}/reject", {"note": "Visits were logged late"})
    own = pay_call(
        client, "POST", PROPOSALS_URL, anvar_headers, incident_body(anvar, type_id, "2026-10-14"), status=201
    )["proposal"]
    decided = audit_events[first_event:]

    tagged = sorted(
        (event["action"], event["new_values"].get("self_decided"))
        for event in decided
        if event["action"].startswith("sales_pay_")
    )
    assert tagged == sorted((f"sales_pay_{verb}", True) for verb in SELF_AUDITED_VERBS)

    rows = {**_rows(world, "confirmed"), **_rows(world, "rejected"), **_rows(world, "proposed")}
    assert [rows[penalty["id"]]["self_decided"] for penalty in (direct, proposal, second, own)] == [True] * 4
    agents = pay_route(world, "GET", "/periods/2026-10")["agents"]
    assert {row["agent_user_id"]: row["self_decided"] for row in agents} == {sales_agent_user.id: False, anvar.id: True}

    live = pay_route(world, "GET", f"/periods/2026-10/agents/{anvar.id}")
    decisions = live["self_decisions"]
    unpaid = {row.unpaid_date.isoformat(): row.id for row in SalesAgentUnpaidDay.query.filter_by(agent_user_id=anvar.id)}
    ids = {
        ("terms", "added", None): SalesAgentPayTerms.query.filter_by(agent_user_id=anvar.id).one().id,
        ("unpaid_day", "set", "2026-10-14"): unpaid["2026-10-14"],
        ("unpaid_day", "set", "2026-10-15"): unpaid["2026-10-15"],
        ("adjustment", "created", None): SalesPayAdjustment.query.filter_by(agent_user_id=anvar.id, source="admin").one().id,
        ("penalty", "created", None): direct["id"],
        ("penalty", "confirmed", None): proposal["id"],
    }
    listed = {(row["input"], row["action"], row["date"]): row["id"] for row in decisions}
    assert live["self_decided"] is True
    assert len(decisions) == 7
    assert set(listed) == set(ids) | {("employment", "set", None)}
    assert {key: listed[key] for key in ids} == ids
    assert [row["at"] for row in decisions] == sorted(row["at"] for row in decisions)

    move_clock(world, local_at(2026, 11, 2, 10))
    as_anvar("POST", "/periods/2026-10/close")

    frozen = pay_route(world, "GET", f"/periods/2026-10/agents/{anvar.id}")
    statement = SalesPayStatement.query.filter_by(agent_user_id=anvar.id).one()
    assert frozen["self_decisions"] == decisions
    assert [(row["input"], row["action"], row["id"]) for row in statement.inputs["self_decisions"]] == [
        (row["input"], row["action"], row["id"]) for row in decisions
    ]

    statement.inputs = {**statement.inputs, "self_decisions": statement.inputs["self_decisions"][:1]}
    db.session.commit()
    reread = pay_route(world, "GET", f"/periods/2026-10/agents/{anvar.id}")

    assert reread["self_decisions"] == decisions[:1]

    as_anvar("POST", "/periods/2026-10/approve")

    lifecycle = [event for event in audit_events if event["action"] in ("sales_pay_period_closed", "sales_pay_period_approved")]
    assert [("self_decided" in event["new_values"]) for event in lifecycle] == [False, False]


def test_a_manager_is_refused_a_proposal_about_himself_and_an_admin_route_at_the_decorator(
    app, client, db, monkeypatch, admin_user, admin_claim_headers, sales_agent_user
):
    """T-SELFDEC-1, the manager half. Mansur is a MANAGER with an agent profile.

    M2 about himself: 403 SALES_PAY_SELF_DECISION naming him (`details.agent_user_id`), and no
    penalty row. The guard runs before the type and date rules, so Mansur needs no terms to be
    refused. M2 about Aziz: 201, with Mansur as the proposer. A17 about himself: 403 from
    `super_admin_required`, which is not the self-decision refusal.
    """
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    type_id = add_penalty_type(world, default_amount=150000)
    move_clock(world, local_at(2026, 10, 15))
    mansur = staff_agent(db, phone="+998901238402", first_name="Mansur", role=UserRole.MANAGER)
    mansur_headers = claim_headers(app, mansur, "manager")

    refused = refusal(
        client, "POST", PROPOSALS_URL, mansur_headers, incident_body(mansur, type_id, "2026-10-14"),
        status=403, code="SALES_PAY_SELF_DECISION",
    )

    assert refused == {"agent_user_id": mansur.id}
    assert SalesPayPenalty.query.filter_by(agent_user_id=mansur.id).count() == 0

    about_aziz = pay_call(
        client, "POST", PROPOSALS_URL, mansur_headers, incident_body(sales_agent_user, type_id, "2026-10-14"), status=201
    )["proposal"]
    decorator = pay_call(
        client, "POST", f"{PAY_API}/agents/{mansur.id}/terms", mansur_headers,
        {"effective_month": "2026-10", "base_salary": "3000000", "plan_id": world.plan["id"]}, status=403,
    )

    assert about_aziz["proposed_by"]["id"] == mansur.id
    assert decorator.get("error_code") != "SALES_PAY_SELF_DECISION"
    assert SalesAgentPayTerms.query.filter_by(agent_user_id=mansur.id).count() == 0


def test_an_admin_onboarder_approving_his_own_extra_order_tags_the_statement_that_pays_for_it(
    app, client, db, monkeypatch, admin_user, admin_claim_headers, operator_user, water
):
    """T-NEW-11's statement half (I-24), which is also T-HOLD-10's.

    Task 6's world: Anvar, an ADMIN holding an agent profile, onboarded "Oasis market". He
    places two orders there on one day, and the second waits (C14). He approves it himself
    through OA2, which an admin decision subject may do. Both orders are delivered and paid on
    5 October. The held order's credit and the bonus line that counts it are both posted to
    October, so Anvar's October statement lists that one decision (`order_approval / approved`,
    with the approval row's id and `decided_at`), A2 tags his row, and A8's outlet check carries
    the `self_approved_orders` badge. The placement runs on the wall clock, because the same-day
    rule reads it (Task 6); the pay reads are frozen at 6 October 2026, 12:00.
    """
    anvar, anvar_headers = admin_agent(app, db, "+998901239351")
    start_pay(client, admin_user, admin_claim_headers, anvar)
    customer = grocery_store(db, "+998901239352", "Oasis market")
    outlet = approved_outlet(
        db, agent=anvar, operator=operator_user, customer=customer, onboarded_at=utc_at(2026, 9, 25), pin=PIN
    )
    first_id, held_id = place_two_same_day_orders(client, anvar_headers, outlet, water)
    approved = client.post(f"{APPROVALS}/{held_id}/approve", json={}, headers=anvar_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    first_at, held_at = utc_at(2026, 10, 5, 10), utc_at(2026, 10, 5, 11)
    collect_cash_at(db, deliver(db, first_id, at=first_at, actor=admin_user), at=first_at, actor=admin_user)
    collect_cash_at(db, deliver(db, held_id, at=held_at, actor=admin_user), at=held_at, actor=admin_user)
    sync_agent(anvar, utc_at(2026, 10, 6))
    freeze_local_now(monkeypatch, local_at(2026, 10, 6, 12))
    approval = AgentOrderApproval.query.filter_by(order_id=held_id).one()

    statement = pay_call(client, "GET", f"{PAY_API}/periods/2026-10/agents/{anvar.id}", admin_claim_headers)
    month = pay_call(client, "GET", f"{PAY_API}/periods/2026-10", admin_claim_headers)

    assert statement["self_decided"] is True
    assert statement["self_decisions"] == [
        {"input": "order_approval", "id": approval.id, "action": "approved", "at": _iso(approval.decided_at), "date": None}
    ]
    assert {row["agent_user_id"]: row["self_decided"] for row in month["agents"]} == {anvar.id: True}
    [check] = [row for row in statement["new_outlets"] if row["outlet_id"] == outlet.id]
    assert (check["status"], check["review_flags"]["self_approved_orders"]) == ("qualified", 1)
