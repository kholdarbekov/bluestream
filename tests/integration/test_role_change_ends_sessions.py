"""A role change ends every login of the account (owner decision R9c, 2026-10-20).

A token carries the role it was issued with, and the app re-mints a signed-in cookie with the same
claims, so a manager an admin demoted kept the manager's role in every open admin-panel session.
Every admin door that changes a person's role or staff roles now ends that person's logins through
`SessionRevocationService.revoke_all(reason="role_changed")`; their next login carries the new role.
An edit that leaves the roles as they were signs nobody out. With Redis down the change fails
loudly and changes nothing.

Each door is driven the way the admin panel calls it (admin_ui/src/services/staffService.js and
adminService.performBulkAction), with the admin's own login; the person changed signs in on the
website or the admin panel. Only Redis outages are replaced.
"""

import pytest
from flask_jwt_extended import decode_token

import business_app.services.session_revocation_service as revocation_module
from business_app.models.user import User
from tests.integration.test_admin_order_reschedule import audit_events  # noqa: F401  (a fixture)
from tests.integration.test_customer_telegram_link_api import _DownRedis
from tests.integration.test_session_revocation import (  # noqa: F401  (production_access_lifetime is a fixture)
    PROFILE,
    SIGNED_OUT,
    _revocations,
    assert_staff_signed_in,
    doors,
    handed_back_login,
    production_access_lifetime,
    sign_in,
)
from tests.integration.test_web_password_reset_and_change import OLD_PASSWORD, _logged_in_browser

pytestmark = pytest.mark.integration

ADMIN_PASSWORD = "AdminPassword123!"  # tests/conftest.py admin_user
MANAGER_PASSWORD = "ManagerPassword123!"  # manager_user
OPERATOR_PASSWORD = "OperatorPassword123!"  # operator_user

BULK_ACTIONS = "/api/v1/admin/bulk-actions"
OPERATORS = "/api/v1/admin/staff/operators"
SALES_AGENTS = "/api/v1/admin/staff/sales-agents"
DELIVERY_PERSONS = "/api/v1/admin/staff/delivery-persons"


def staff_roles_url(user_id):
    return f"/api/v1/admin/staff/users/{user_id}/roles"


def admin_headers(app, admin_user):
    """The admin's own login, the way the panel's requests carry it."""
    return {"Authorization": f"Bearer {sign_in(app, admin_user.email, ADMIN_PASSWORD)['access_token']}"}


def assign_role(user_id, role):
    """adminService.performBulkAction('assign_role', 'user', [id], {role})."""
    return {"action": "assign_role", "target_type": "user", "target_ids": [user_id], "parameters": {"role": role}}


def role_claim(app, tokens):
    with app.app_context():
        return decode_token(tokens["access_token"])["role"]


ROLE_CHANGED = {"reason": "role_changed"}


def ended_logins(audit_events):
    """Whose logins were ended, and why: the `all_sessions_revoked` audit rows."""
    return [(event["resource_id"], event["additional_data"]) for event in _revocations(audit_events)]


def test_demoting_a_manager_signs_their_admin_panel_out_at_once_and_their_next_login_has_the_new_role(
    app, db, admin_user, manager_user, audit_events, production_access_lifetime
):
    """The manager works in the admin panel: cookie and CSRF, re-minted on every request. Before,
    the bulk door blacklisted only the login's own tokens, so the re-minted cookie kept the
    manager's role."""
    panel, _csrf = _logged_in_browser(app, sign_in(app, manager_user.email, MANAGER_PASSWORD)["access_token"])
    working = panel.get(OPERATORS)
    assert working.status_code == 200 and handed_back_login(working)

    resp = app.test_client().post(
        BULK_ACTIONS, headers=admin_headers(app, admin_user), json=assign_role(manager_user.id, "operator")
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["results"]["success_count"] == 1
    assert panel.get(PROFILE).status_code == 401  # the panel holds the re-minted cookie
    fresh = sign_in(app, manager_user.email, MANAGER_PASSWORD)
    assert role_claim(app, fresh) == "operator"
    now_an_operator = {"Authorization": f"Bearer {fresh['access_token']}"}
    assert app.test_client().get(OPERATORS, headers=now_an_operator).status_code == 403
    assert ended_logins(audit_events) == [(str(manager_user.id), ROLE_CHANGED)]


def test_assigning_the_role_someone_already_has_signs_nobody_out(app, db, admin_user, manager_user, audit_events):
    login = sign_in(app, manager_user.email, MANAGER_PASSWORD)

    resp = app.test_client().post(
        BULK_ACTIONS, headers=admin_headers(app, admin_user), json=assign_role(manager_user.id, "manager")
    )

    assert resp.get_json()["data"]["results"]["success_count"] == 1, resp.get_json()
    assert_staff_signed_in(app, login)
    assert ended_logins(audit_events) == []


# What each admin-panel form sends when the typed phone belongs to someone who already has an
# account: the backend attaches the staff role to that person (staff_service._create_or_attach_staff_user).
def _operators_form(person):
    return "POST", OPERATORS, {
        "first_name": "Test",
        "last_name": "User",
        "phone": person.phone,
        "staff_roles": ["operator"],
    }


def _sales_agents_form(person):
    return "POST", SALES_AGENTS, {
        "full_name": "Test User",
        "phone": person.phone,
        "districts": [],
        "weekly_new_outlet_target": None,
        "employment_type": None,
        "notes": None,
    }


def _delivery_persons_form(person):
    return "POST", DELIVERY_PERSONS, {
        "full_name": "Test User",
        "phone": person.phone,
        "email": None,
        "vehicle_type": None,
        "vehicle_number": None,
    }


def _staff_management_roles(person):
    return "PUT", staff_roles_url(person.id), {"staff_roles": ["operator"]}


@pytest.mark.parametrize(
    "door",
    [_operators_form, _sales_agents_form, _delivery_persons_form, _staff_management_roles],
    ids=["operators_form", "sales_agents_form", "delivery_persons_form", "staff_management_roles"],
)
def test_making_a_signed_in_customer_staff_signs_them_out_everywhere(
    app, db, admin_user, sample_user, audit_events, door
):
    login = sign_in(app, sample_user.email, OLD_PASSWORD)
    method, url, body = door(sample_user)

    resp = app.test_client().open(url, method=method, headers=admin_headers(app, admin_user), json=body)

    assert resp.status_code in (200, 201), resp.get_json()
    assert doors(app, login) == SIGNED_OUT
    assert_staff_signed_in(app, sign_in(app, sample_user.email, OLD_PASSWORD))
    assert ended_logins(audit_events) == [(str(sample_user.id), ROLE_CHANGED)]


@pytest.mark.parametrize(
    "staff_roles, signed_out, reasons",
    [(["operator", "delivery_driver"], True, ["role_changed"]), (["operator"], False, [])],
    ids=["adds_a_role", "keeps_the_roles"],
)
def test_editing_an_operator_signs_them_out_only_when_their_roles_change(
    app, db, admin_user, operator_user, audit_events, staff_roles, signed_out, reasons
):
    """The Operators edit form always sends the roles box; a name fix leaves it as it was."""
    login = sign_in(app, operator_user.email, OPERATOR_PASSWORD)
    body = {
        "first_name": "Renamed",
        "last_name": "User",
        "phone": operator_user.phone,
        "email": operator_user.email,
        "staff_roles": staff_roles,
    }

    resp = app.test_client().put(
        f"{OPERATORS}/{operator_user.id}", headers=admin_headers(app, admin_user), json=body
    )

    assert resp.status_code == 200, resp.get_json()
    if signed_out:
        assert doors(app, login) == SIGNED_OUT
    else:
        assert_staff_signed_in(app, login)
    assert [data["reason"] for _user, data in ended_logins(audit_events)] == reasons


@pytest.fixture
def cut_off_store_down(monkeypatch):
    """Redis refuses the cut-off write."""
    monkeypatch.setattr(revocation_module, "redis_client", _DownRedis())


def test_with_redis_down_a_staff_role_change_fails_loudly_and_changes_nothing(
    app, db, admin_user, sample_user, cut_off_store_down
):
    headers = admin_headers(app, admin_user)

    resp = app.test_client().put(staff_roles_url(sample_user.id), headers=headers, json={"staff_roles": ["operator"]})

    assert resp.status_code == 500, resp.get_json()
    db.session.expire_all()
    assert not db.session.get(User, sample_user.id).staff_roles


def test_with_redis_down_a_bulk_role_change_fails_loudly_and_changes_nothing(
    app, db, admin_user, manager_user, cut_off_store_down
):
    headers = admin_headers(app, admin_user)

    resp = app.test_client().post(BULK_ACTIONS, headers=headers, json=assign_role(manager_user.id, "operator"))

    results = resp.get_json()["data"]["results"]
    assert (results["success_count"], results["failed_count"]) == (0, 1), results
    db.session.expire_all()
    assert db.session.get(User, manager_user.id).role.value == "manager"
