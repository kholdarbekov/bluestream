"""The admin pay audit trail (§4.17): every verb, emitted by a route, in its one envelope.

C27 fixes the complete list. Task 4 audits seven verbs, Task 7 five and Task 8 six, all
through `pay_period_service.audit_pay_write`. One October, driven through the admin routes the
Compensation page calls, emits all eighteen and no other `sales_pay_*` action. Task 4's
`pay_audit` keeps only SETTINGS_CHANGED events at HIGH severity, so a verb logged in any other
envelope does not count here. No snapshot key contains a word the audit logger redacts
("key", "token", "secret"). The `self_decided` flag is pinned in
tests/integration/test_sales_pay_self_decision.py.
"""

import pytest

from tests.integration.sales_pay_builders import (
    PENALTY_TYPE_NAMES,
    PROPOSALS_URL,
    incident_body,
    local_at,
    move_clock,
    october_world,
    pay_call,
    pay_route,
    pay_audit,
    publish_version,
)
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401 -- a fixture

pytestmark = pytest.mark.integration

# §4.17, in the spec's order. A new admin pay write adds its verb here, in the same change.
PAY_AUDIT_VERBS = (
    "start",
    "period_closed",
    "period_recalculated",
    "period_approved",
    "period_paid",
    "holidays_set",
    "plan_created",
    "plan_version_created",
    "terms_added",
    "employment_set",
    "unpaid_days_set",
    "penalty_type_created",
    "penalty_type_updated",
    "penalty_confirmed",
    "penalty_rejected",
    "penalty_cancelled",
    "penalty_created",
    "adjustment_created",
)
REDACTED_FRAGMENTS = ("key", "token", "secret")


def _keys(values) -> set:
    """Every dict key at any depth of an audit snapshot."""
    if isinstance(values, dict):
        return set(values).union(*(_keys(value) for value in values.values()))
    if isinstance(values, list):
        return set().union(*(_keys(item) for item in values))
    return set()


def test_one_month_through_the_admin_routes_emits_every_pay_audit_verb(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user, audit_events
):
    """The audit pin: all eighteen verbs, each from the route that writes it.

    `october_world` (Task 7's `pay_world` plus Illustration B's inputs): A13 plan_created,
    A18 employment_set, A17 terms_added, A0 start, A7 holidays_set, A10 unpaid_days_set, A20
    penalty_type_created. Then: A21 re-prices the type to 120,000; A15 publishes a November
    version; A11 +50,000; A23 books a direct penalty at the new default; Malika proposes twice
    (M2, not audited) and the admin confirms one (A24) and rejects the other (A25); A26 cancels
    the direct one. On 2 November: A3, A4, A5, A6.

    The six Task 8 verbs and the two plan verbs (with their tiers, v5) are compared whole: resource, before and after.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id = sales_agent_user, world.penalty_type_id
    pay_route(world, "PATCH", f"/penalty-types/{type_id}", {"default_amount": 120000})
    v2 = publish_version(world, "2026-11", rate_19l=1200)
    pay_route(
        world, "POST", f"/periods/2026-10/agents/{agent.id}/adjustments",
        {"amount": 50000, "reason": "Guarantee top-up"}, status=201,
    )
    direct = pay_route(world, "POST", "/penalties", incident_body(agent, type_id, "2026-10-09"), status=201)["penalty"]
    proposals = [
        pay_call(client, "POST", PROPOSALS_URL, manager_claim_headers, incident_body(agent, type_id, day), status=201)[
            "proposal"
        ]
        for day in ("2026-10-12", "2026-10-13")
    ]
    pay_route(world, "POST", f"/penalties/{proposals[0]['id']}/confirm", {})
    pay_route(world, "POST", f"/penalties/{proposals[1]['id']}/reject", {"note": "Visits were logged late"})
    pay_route(world, "POST", f"/penalties/{direct['id']}/cancel", {"note": "Booked against the wrong day"})
    move_clock(world, local_at(2026, 11, 2, 10))
    for action in ("close", "recalculate", "approve"):
        pay_route(world, "POST", f"/periods/2026-10/{action}")
    pay_route(world, "POST", "/periods/2026-10/mark-paid", {"paid_on": "2026-11-02"})

    pay_events = [event for event in audit_events if event["action"].startswith("sales_pay_")]
    assert len(PAY_AUDIT_VERBS) == len(set(PAY_AUDIT_VERBS)) == 18
    assert {event["action"] for event in pay_events} == {f"sales_pay_{verb}" for verb in PAY_AUDIT_VERBS}
    assert sum(len(pay_audit(audit_events, verb)) for verb in PAY_AUDIT_VERBS) == len(pay_events)
    for event in pay_events:
        leaked = sorted(
            name
            for name in _keys(event["old_values"]) | _keys(event["new_values"])
            if any(fragment in name.lower() for fragment in REDACTED_FRAGMENTS)
        )
        assert leaked == [], event["action"]

    type_before = {"names": PENALTY_TYPE_NAMES, "default_amount": 150000, "is_active": True}
    type_after = {"names": PENALTY_TYPE_NAMES, "default_amount": 120000, "is_active": True}
    assert pay_audit(audit_events, "penalty_type_created") == [
        ("sales_pay_penalty_type", str(type_id), None, type_before)
    ]
    assert pay_audit(audit_events, "penalty_type_updated") == [
        ("sales_pay_penalty_type", str(type_id), type_before, type_after)
    ]
    assert pay_audit(audit_events, "penalty_created") == [
        (
            "sales_pay_penalty",
            str(direct["id"]),
            None,
            {
                "agent_user_id": agent.id,
                "penalty_type_id": type_id,
                "incident_date": "2026-10-09",
                "amount": 120000,
                "posting_month": "2026-10",
                "is_late": False,
                "origin": "direct",
            },
        )
    ]
    assert pay_audit(audit_events, "penalty_confirmed") == [
        (
            "sales_pay_penalty",
            str(proposals[0]["id"]),
            {"status": "proposed"},
            {"status": "confirmed", "agent_user_id": agent.id, "amount": 120000, "posting_month": "2026-10", "is_late": False},
        )
    ]
    assert pay_audit(audit_events, "penalty_rejected") == [
        ("sales_pay_penalty", str(proposals[1]["id"]), {"status": "proposed"}, {"status": "rejected", "agent_user_id": agent.id})
    ]
    assert pay_audit(audit_events, "penalty_cancelled") == [
        (
            "sales_pay_penalty",
            str(direct["id"]),
            {"status": "confirmed"},
            {"status": "cancelled", "agent_user_id": agent.id, "amount": 120000, "posting_month": "2026-10"},
        )
    ]
    water_id = world.products.water.id
    assert pay_audit(audit_events, "plan_created") == [
        (
            "sales_pay_plan",
            str(world.plan["id"]),
            None,
            {
                "name": "Standard",
                "version_id": world.plan["version_in_force"]["id"],
                "version_no": 1,
                "effective_month": "2026-10",
                "default_tiers": [{"from_unit": 1, "mode": "percent", "value": "3.00"}],
                "rates": [{"product_id": water_id, "tiers": [{"from_unit": 1, "mode": "per_unit", "value": "1000.00"}]}],
            },
        )
    ]
    assert pay_audit(audit_events, "plan_version_created") == [
        (
            "sales_pay_plan",
            str(world.plan["id"]),
            None,
            {
                "version_id": v2["id"],
                "version_no": 2,
                "effective_month": "2026-11",
                "default_tiers": [{"from_unit": 1, "mode": "percent", "value": "3.00"}],
                "rates": [{"product_id": water_id, "tiers": [{"from_unit": 1, "mode": "per_unit", "value": "1200.00"}]}],
            },
        )
    ]
