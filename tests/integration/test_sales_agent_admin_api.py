"""The admin sales-agents endpoints, driven the way SalesAgents.js drives them."""
from datetime import UTC, datetime

import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import Visit

pytestmark = pytest.mark.integration

AGENTS_URL = "/api/v1/admin/staff/sales-agents"
CREATE = {
    "full_name": "Sardor Alimov",
    "phone": "+998901234574",
    "districts": ["yunusabad"],
    "weekly_new_outlet_target": 5,
    "employment_type": "employee",
}


def _create(client, headers, payload=CREATE):
    resp = client.post(AGENTS_URL, json=payload, headers=headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["data"]["sales_agent"]


def test_create_list_get_update_deactivate(client, admin_claim_headers, db):
    agent = _create(client, admin_claim_headers)
    assert agent["staff_roles"] == ["sales_agent"]
    assert agent["is_active"] is True
    assert agent["districts"] == ["yunusabad"]
    assert agent["outlets_assigned"] == 0
    assert agent["visits_today"] == 0
    assert agent["orders_today"] == 0

    listed = client.get(AGENTS_URL, headers=admin_claim_headers, query_string={"page": 1, "per_page": 20})
    assert listed.status_code == 200, listed.get_data(as_text=True)
    body = listed.get_json()
    assert [row["user_id"] for row in body["data"]["items"]] == [agent["user_id"]]
    assert body["meta"]["summary"] == {
        "total_agents": 1,
        "active_agents": 1,
        "visits_today": 0,
        "orders_today": 0,
    }

    one = client.get(f"{AGENTS_URL}/{agent['user_id']}", headers=admin_claim_headers)
    assert one.status_code == 200
    assert one.get_json()["data"]["sales_agent"]["phone"] == "+998901234574"

    updated = client.put(
        f"{AGENTS_URL}/{agent['user_id']}",
        json={"districts": ["chilanzar", "yunusabad"], "weekly_new_outlet_target": 7},
        headers=admin_claim_headers,
    )
    assert updated.status_code == 200, updated.get_data(as_text=True)
    assert updated.get_json()["data"]["sales_agent"]["districts"] == ["chilanzar", "yunusabad"]
    assert updated.get_json()["data"]["sales_agent"]["weekly_new_outlet_target"] == 7

    # The edit route is NOT a door onto `is_active`: `/active` is the single door, because it is the
    # only one that writes the dedicated `sales_agent_set_active` activity entry. `extra="ignore"`
    # drops the field here rather than half-applying it behind a generic audit line.
    ignored = client.put(
        f"{AGENTS_URL}/{agent['user_id']}", json={"is_active": False}, headers=admin_claim_headers
    )
    assert ignored.status_code == 200, ignored.get_data(as_text=True)
    assert ignored.get_json()["data"]["sales_agent"]["is_active"] is True

    off = client.put(f"{AGENTS_URL}/{agent['user_id']}/active", json={"is_active": False}, headers=admin_claim_headers)
    assert off.status_code == 200
    assert off.get_json()["data"]["sales_agent"]["is_active"] is False
    assert SalesAgentProfile.query.filter_by(user_id=agent["user_id"]).one().is_active is False


def test_invite_link_works_for_a_sales_agent(client, admin_claim_headers, db):
    agent = _create(client, admin_claim_headers)
    resp = client.post(
        "/api/v1/admin/staff/invite-link",
        json={"user_id": agent["user_id"], "role": "sales_agent"},
        headers=admin_claim_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["role"] == "sales_agent"
    assert "?start=staff_invite_" in data["invite_link"]


def test_validation_errors_carry_codes(client, admin_claim_headers, db):
    bad = client.post(AGENTS_URL, json={**CREATE, "districts": ["atlantis"]}, headers=admin_claim_headers)
    assert bad.status_code == 400
    assert "SALES_DISTRICT_INVALID" in bad.get_data(as_text=True)

    _create(client, admin_claim_headers)
    dup = client.post(AGENTS_URL, json=CREATE, headers=admin_claim_headers)
    assert dup.status_code == 409
    assert "STAFF_SALES_AGENT_EXISTS" in dup.get_data(as_text=True)


def test_role_claim_is_required(client, admin_auth_headers, db):
    resp = client.get(AGENTS_URL, headers=admin_auth_headers)
    assert resp.status_code == 403


def test_an_explicitly_sent_null_clears_the_field(client, admin_claim_headers, db):
    """The same `exclude_unset` ruling as the outlets PUT, pinned on the sales-agent PUT.

    Both routes share `_validated_payload`, and SalesAgents.js already sends `notes: null` /
    `email: null` when the admin empties those inputs. Under `exclude_none=True` the null was
    stripped before `StaffService.update_sales_agent` could see it, so its `if "notes" in updates`
    branch never ran: the modal reported success and the old note stayed on the profile.
    """
    agent = _create(client, admin_claim_headers, {**CREATE, "notes": "Trial hire"})
    assert agent["notes"] == "Trial hire"

    updated = client.put(f"{AGENTS_URL}/{agent['user_id']}", json={"notes": None}, headers=admin_claim_headers)
    assert updated.status_code == 200, updated.get_data(as_text=True)
    assert updated.get_json()["data"]["sales_agent"]["notes"] is None
    db.session.expire_all()
    assert SalesAgentProfile.query.filter_by(user_id=agent["user_id"]).one().notes is None


def test_creating_an_agent_on_an_existing_phone_keeps_that_users_email(
    client, admin_claim_headers, admin_user, db
):
    """A create form that lands on somebody's existing account must not blank their email.

    SalesAgents.js always sends the `email` key, so an untouched input arrives as `email: None`.
    `_create_or_attach_staff_user` looks an existing account up BY PHONE, so typing a number that
    already belongs to someone silently attaches the role to them -- and the null then cleared a
    column the operator never saw. That locked an admin out of the panel: `login_user` resolves the
    account by email before it ever reaches `_verify_password`, so the password looked wrong.

    The explicit-null-clears ruling stays intact on the PUT door (pinned above); it applies there
    because the caller named the person by id, which is the difference this test draws.
    """
    payload = {**CREATE, "phone": admin_user.phone, "email": None}

    resp = client.post(AGENTS_URL, json=payload, headers=admin_claim_headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)

    db.session.expire_all()
    assert admin_user.email == "admin@example.com"


def test_the_list_route_publishes_todays_counters_for_the_row_and_the_estate(client, admin_claim_headers, db):
    """`SalesAgents.js` prints `outlets_assigned / outlets_active` and (phase 3) today's
    two counters straight off this payload — so the pin is on the route, not the service.

    The two SUMMARY totals are pinned here too (R33): they are estate-wide, sitting beside
    `total_agents` / `active_agents` in `meta.summary`, and the page reads them for its two new
    cards. A page-local sum would pass every service test and still shrink on page 2.
    """
    agent = _create(client, admin_claim_headers)
    kept = Outlet(name="Bahor", outlet_type="grocery_store", stage="active",
                  assigned_agent_user_id=agent["user_id"])
    written_off = Outlet(name="Eski", outlet_type="grocery_store", stage="lost",
                         assigned_agent_user_id=agent["user_id"])
    db.session.add_all([kept, written_off])
    db.session.commit()
    moment = datetime.now(UTC)
    db.session.add(
        Visit(outlet_id=kept.id, agent_user_id=agent["user_id"], status="completed", planned=False,
              current_step="close", started_at=moment, checkin_at=moment, ended_at=moment,
              outcome="no_order")
    )
    db.session.commit()

    listed = client.get(AGENTS_URL, headers=admin_claim_headers, query_string={"page": 1, "per_page": 20})

    assert listed.status_code == 200, listed.get_data(as_text=True)
    payload = listed.get_json()
    row = payload["data"]["items"][0]
    assert row["outlets_assigned"] == 1
    assert row["outlets_active"] == 1
    assert row["visits_today"] == 1
    assert row["orders_today"] == 0

    # R33 — the cards' four numbers, all estate-wide, in the one place the page reads them.
    summary = payload["meta"]["summary"]
    assert summary["visits_today"] == 1
    assert summary["orders_today"] == 0
    assert set(summary) == {"total_agents", "active_agents", "visits_today", "orders_today"}
