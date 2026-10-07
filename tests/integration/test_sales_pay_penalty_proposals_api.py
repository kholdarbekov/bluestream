"""The manager's two penalty routes, M1 and M2: amount-free by construction.

Compensation spec §5.3, C6 (a manager proposes), C11 (a manager never sees pay), review
gaming F9 (M1 lists proposals only) and T-VISIB-2. A `ProposalRow` has an EXACT key set,
pinned here. A recursive scan of every M1/M2 body finds no pay key: not the amount, not the
type's default, not the admin's decision note or cancel reason (an admin's free text may quote
a figure).
"""

import inspect

import pytest

from business_app.models.notification import Notification
from business_app.models.sales_pay import SalesPayPenalty
from business_app.services.notification_service import NotificationService
from business_app.services.sales.agent_metrics_service import METRIC_KEYS
from tests.integration.sales_pay_builders import (
    PENALTY_TYPE_NAMES,
    PROPOSALS_URL,
    add_penalty_type,
    incident_body,
    october_world,
    pay_call,
    pay_route,
    spy_sales_pushes,
)
from tests.integration.test_sales_pay_penalties_api import PROPOSAL_KEYS

pytestmark = pytest.mark.integration

# T-VISIB-2's list. A key, anywhere in a manager's body, is a leak.
PAY_KEYS = {
    "amount",
    "default_amount",
    "total",
    "base",
    "commission",
    "rate",
    "bonus",
    "pay",
    "salary",
    "decision_note",
    "cancel_reason",
}


def _pay_keys(value, path=()):
    """Every pay key under `value`, as a path. `meta` is skipped: pagination's `total` is a row count."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "meta":
                continue
            if key in PAY_KEYS:
                found.append((*path, key))
            found.extend(_pay_keys(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_pay_keys(child, (*path, index)))
    return found


def _spy_in_app_notifications(monkeypatch):
    """`NotificationService.send_notification`, bound against its real signature: any in-app
    notification a proposal raised would be recorded here, and a malformed one would raise."""
    signature = inspect.signature(NotificationService.send_notification)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append((args, kwargs))

    monkeypatch.setattr(NotificationService, "send_notification", fake)
    return calls


def test_a_manager_proposal_is_amount_free_and_silent(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-PEN-1.

    M2 gives 201 with `proposed`. The row has exactly the ProposalRow keys, and no pay key
    appears anywhere in the body. An `amount` in the payload is refused (400, `extra="forbid"`)
    and writes nothing. The proposal is in no estimate. Nobody is told anything: no push, no
    in-app notification, no notification row.
    """
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    body = incident_body(sales_agent_user, world.penalty_type_id, "2026-10-14")
    pushes = spy_sales_pushes(monkeypatch)
    in_app = _spy_in_app_notifications(monkeypatch)
    notifications_before = Notification.query.count()

    answer = pay_call(client, "POST", PROPOSALS_URL, manager_claim_headers, body, status=201)

    proposal = answer["proposal"]
    assert set(proposal) == PROPOSAL_KEYS
    assert proposal["type"] == {"id": world.penalty_type_id, "names": PENALTY_TYPE_NAMES}
    assert proposal["agent"] == {"user_id": sales_agent_user.id, "name": "Sardor Agent"}
    assert (proposal["status"], proposal["origin"], proposal["decided_at"]) == ("proposed", "proposal", None)
    assert _pay_keys(answer) == []

    pay_call(client, "POST", PROPOSALS_URL, manager_claim_headers, {**body, "amount": 150000}, status=400)

    assert SalesPayPenalty.query.count() == 1
    assert (pushes, in_app, Notification.query.count()) == ([], [], notifications_before)
    assert pay_route(world, "GET", f"/periods/2026-10/agents/{sales_agent_user.id}")["summary"]["penalties"] == 0
    assert pay_route(world, "GET", "/periods")["pending_penalty_count"] == 1


def test_the_proposal_list_shows_proposals_and_active_types_only(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """M1: `origin = "proposal"` rows only, since a direct booking is the admin's own (F9); the
    status and agent filters; the active types as `{id, names}`, with no default amount."""
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, manager = sales_agent_user, manager_claim_headers
    proposal = pay_call(
        client, "POST", PROPOSALS_URL, manager, incident_body(agent, world.penalty_type_id, "2026-10-14"), status=201
    )["proposal"]
    pay_route(
        world, "POST", "/penalties", incident_body(agent, world.penalty_type_id, "2026-10-09", amount=70000), status=201
    )
    retired = add_penalty_type(
        world, default_amount=50000, names={"en": "Late start", "uz": "Kech boshlash", "ru": "Поздний выход"}
    )
    pay_route(world, "PATCH", f"/penalty-types/{retired}", {"is_active": False})

    listed = pay_call(client, "GET", PROPOSALS_URL, manager)

    assert [row["id"] for row in listed["items"]] == [proposal["id"]]
    assert set(listed["items"][0]) == PROPOSAL_KEYS
    assert listed["statuses"] == ["proposed", "confirmed", "rejected", "cancelled"]
    assert listed["types"] == [{"id": world.penalty_type_id, "names": PENALTY_TYPE_NAMES}]
    assert listed["meta"]["total"] == 1

    pay_route(world, "POST", f"/penalties/{proposal['id']}/confirm", {})

    confirmed = pay_call(client, "GET", f"{PROPOSALS_URL}?status=confirmed", manager)
    assert [(row["id"], row["status"]) for row in confirmed["items"]] == [(proposal["id"], "confirmed")]
    assert confirmed["items"][0]["decided_at"] is not None
    assert pay_call(client, "GET", f"{PROPOSALS_URL}?status=proposed", manager)["items"] == []
    assert pay_call(client, "GET", f"{PROPOSALS_URL}?agent_id={agent.id + 1000}", manager)["items"] == []


def test_the_manager_routes_carry_no_pay(
    client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user
):
    """T-VISIB-2. Scanned: one row confirmed at an overridden amount, one rejected and one
    cancelled, each with a note that quotes a figure; M1 unfiltered and by status; M2's own
    answer. No pay key appears in any of them. The manager-readable metrics still publish
    exactly `METRIC_KEYS`: no pay figure joined them."""
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    agent, type_id, manager = sales_agent_user, world.penalty_type_id, manager_claim_headers

    def propose(day):
        return pay_call(client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, day), status=201)["proposal"]

    confirmed, rejected, cancelled = propose("2026-10-14"), propose("2026-10-13"), propose("2026-10-12")
    pay_route(world, "POST", f"/penalties/{confirmed['id']}/confirm", {"amount": 90000})
    pay_route(world, "POST", f"/penalties/{rejected['id']}/reject", {"note": "Fine of 150,000 waived"})
    pay_route(world, "POST", f"/penalties/{cancelled['id']}/confirm", {})
    pay_route(world, "POST", f"/penalties/{cancelled['id']}/cancel", {"note": "Refund 150,000 UZS"})
    fresh = pay_call(client, "POST", PROPOSALS_URL, manager, incident_body(agent, type_id, "2026-10-09"), status=201)

    bodies = [
        fresh,
        pay_call(client, "GET", PROPOSALS_URL, manager),
        *(pay_call(client, "GET", f"{PROPOSALS_URL}?status={status}", manager) for status in ("confirmed", "rejected", "cancelled")),
    ]

    assert [_pay_keys(body) for body in bodies] == [[], [], [], [], []]
    metrics = pay_call(client, "GET", "/api/v1/admin/sales/agents/metrics", manager)["agents"]
    assert metrics and all(set(row) - {"agent_user_id", "agent_name", "phone"} == set(METRIC_KEYS) for row in metrics)


def test_only_managers_and_admins_reach_the_proposal_routes(
    client, db, monkeypatch, admin_user, admin_claim_headers, operator_auth_headers, sales_agent_user, sales_agent_auth_headers
):
    world = october_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    body = incident_body(sales_agent_user, world.penalty_type_id, "2026-10-14")

    for headers in (sales_agent_auth_headers, operator_auth_headers):
        pay_call(client, "GET", PROPOSALS_URL, headers, status=403)
        pay_call(client, "POST", PROPOSALS_URL, headers, body, status=403)

    assert pay_call(client, "GET", PROPOSALS_URL, admin_claim_headers)["items"] == []
    assert SalesPayPenalty.query.count() == 0
