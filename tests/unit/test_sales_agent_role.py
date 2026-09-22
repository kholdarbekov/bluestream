"""The sales_agent role must survive every place a staff role is normalised.

Regression for the allowlist trap: STAFF_BOT_ROLES is the filter inside
StaffService._normalize_staff_roles_input, so a role that is not listed there
is silently dropped on write and erased on login.
"""
from datetime import UTC, datetime

import pytest
from flask_jwt_extended import create_refresh_token

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.user import User
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import ForbiddenError, ValidationError
from business_app.utils.password_security import hash_password
from business_app.utils.user_types import normalize_user_type
from shared.enums import UserRole, UserStatus, UserType
from shared.staff_constants import STAFF_ACTIONS, STAFF_BOT_ROLES


def make_sales_agent_user(db, phone="+998901234573", role=UserRole.SALES_AGENT, staff_roles=None):
    """A staff user carrying the sales_agent role. Phone +998901234573 is the first
    number after the conftest sequence (568-572), so it never collides with the
    shared admin/driver/operator fixtures."""
    user = User(
        phone=phone,
        password_hash=hash_password("AgentPassword123!"),
        first_name="Sardor",
        last_name="Agent",
        user_type=UserType.STAFF,
        role=role,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    if staff_roles is not None:
        user.staff_roles = staff_roles
    db.session.add(user)
    db.session.commit()
    return user


def test_sales_agent_is_a_staff_bot_role():
    assert UserRole.SALES_AGENT.value == "sales_agent"
    assert "sales_agent" in STAFF_BOT_ROLES
    assert STAFF_ACTIONS["OUTLET_CREATED"] == "outlet_created"
    assert STAFF_ACTIONS["OUTLET_APPROVED"] == "outlet_approved"


def test_update_staff_roles_persists_sales_agent(db):
    user = make_sales_agent_user(db, role=UserRole.CUSTOMER)
    StaffService.update_staff_roles(user.id, ["sales_agent"])
    assert User.query.get(user.id).staff_roles == ["sales_agent"]


def test_extract_staff_roles_reads_the_role_column(db):
    user = make_sales_agent_user(db, staff_roles=[])
    assert StaffService._extract_staff_roles(user) == ["sales_agent"]


def test_normalize_user_type_classifies_sales_agent_as_staff():
    assert normalize_user_type(None, role="sales_agent") == "staff"
    assert normalize_user_type(None, staff_roles=["sales_agent"]) == "staff"


def test_admin_bulk_assign_role_refuses_sales_agent(client, db, admin_user, sample_user, admin_auth_headers):
    """Bulk `assign_role` must NOT be able to mint a sales agent.

    It writes `users.role` and nothing else, so a user promoted here carries the role with no
    `SalesAgentProfile` behind it — no districts, no visit cadence — and every agent-scoped path
    (`OutletService._assert_agent`, the staff-bot sales hub) then refuses the person the admin
    just "promoted". Agents are created only through `POST /admin/staff/sales-agents`, which
    creates the profile in the same call, so the profile always exists.

    This deliberately REVERSES the Task 1 decision to add the role to that allowlist. The role
    belongs on every allowlist that NORMALISES an existing staff role (STAFF_BOT_ROLES,
    SecurityValidator.VALID_ROLES, the create-user gate — all still asserted in this file) and on
    no allowlist that CREATES one out of a bare role string.
    """
    original_role = sample_user.role
    assert original_role != UserRole.SALES_AGENT

    response = client.post(
        "/api/v1/admin/bulk-actions",
        json={
            "action": "assign_role",
            "target_type": "user",
            "target_ids": [sample_user.id],
            "parameters": {"role": "sales_agent"},
            "reason": "Promoting a customer to the sales team",
        },
        headers=admin_auth_headers,
    )

    # The endpoint answers 200 with per-user results; the refusal is the same shape the service
    # already uses for every other role it does not accept.
    assert response.status_code == 200, response.get_json()
    results = response.get_json()["data"]["results"]
    assert results["success_count"] == 0 and results["failed_count"] == 1
    assert results["errors"] == [{"user_id": sample_user.id, "error": "Invalid role"}]
    assert User.query.get(sample_user.id).role == original_role


def test_update_operator_keeps_profile_backed_roles_the_payload_omits(client, db, admin_user, admin_claim_headers):
    """`PUT /admin/staff/operators/<id>` is the SECOND write path onto `staff_roles`.

    The operator editor posts only the roles it can show, so a dual operator+agent edited for a
    phone number came back a plain operator: the `SalesAgentProfile` stayed live while the person
    lost the staff-bot role that reaches it (and the same held for a driver's `DeliveryPerson`).
    `update_staff_roles` already force-keeps a role whose profile row exists; this path must apply
    the very same rule, which is why both now go through one helper.
    """
    from business_app.models.delivery import DeliveryPerson

    user = make_sales_agent_user(
        db,
        phone="+998901234593",
        role=UserRole.OPERATOR,
        staff_roles=["operator", "sales_agent", "delivery_driver"],
    )
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.add(DeliveryPerson(user_id=user.id, full_name="Sardor Agent", phone=user.phone, is_active=True))
    db.session.commit()

    response = client.put(
        f"/api/v1/admin/staff/operators/{user.id}",
        json={"full_name": "Sardor Agent", "staff_roles": ["operator"]},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    assert sorted(User.query.get(user.id).staff_roles) == ["delivery_driver", "operator", "sales_agent"]


def test_security_validator_accepts_the_sales_agent_role():
    """`SecurityValidator.VALID_ROLES` is the third write-path allowlist."""
    from business_app.utils.security_validators import SecurityValidator

    is_valid, message = SecurityValidator.validate_role("sales_agent")
    assert is_valid, message


def test_admin_create_user_does_not_reject_the_sales_agent_role(client, db, admin_user, admin_auth_headers):
    """`POST /api/v1/auth/admin/create-user` must not turn the role away at its gate.

    That endpoint keeps its OWN `valid_roles` literal (business_app/api/auth.py)
    and answers 400 `Invalid role. Must be one of: ...` for anything missing
    from it, before `SecurityValidator.VALID_ROLES` is ever consulted.

    The assertion stops at the gate rather than at 201 because this route is
    broken for every non-admin role independently of this change:
    `AuthService.register_user` puts `role` in its `valid_user_fields` and then
    passes `role=UserRole.CUSTOMER.value, **filtered_kwargs` into `User(...)`,
    so any caller supplying a role gets `TypeError: got multiple values for
    keyword argument 'role'` -> 500. No test drove this endpoint before, which
    is why the bug was invisible. Fixing it is out of scope here; this test
    still fails if `sales_agent` is dropped from the endpoint's allowlist.

    The guard is `admin_required` -> `require_role` (auth_middleware), which
    compares `user.status`/`user.role` against the enums' `.value` strings.
    Both are plain Enums, so a DB-loaded member never equals the string it is
    checked against; pinning the string form is arrangement to get past the
    door, the same workaround tests/integration/test_admin_record_collection_e2e.py
    uses for `status`.
    """
    admin_user.status = UserStatus.ACTIVE.value
    admin_user.role = UserRole.ADMIN.value
    db.session.flush()

    response = client.post(
        "/api/v1/auth/admin/create-user",
        json={
            "email": "sales.agent@example.com",
            "password": "AgentPassword123!",
            "first_name": "Sardor",
            "last_name": "Agent",
            "phone": "+998901234574",
            "role": "sales_agent",
        },
        headers=admin_auth_headers,
    )

    assert response.status_code != 400, response.get_json()
    assert "Invalid role" not in (response.get_json() or {}).get("message", "")


def make_profile(db, user, is_active=True):
    profile = SalesAgentProfile(user_id=user.id, is_active=is_active, districts=["yunusabad"])
    db.session.add(profile)
    db.session.commit()
    return profile


def test_prebound_login_returns_sales_agent_role_and_profile_id(app, db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    user.telegram_id = "555000111"
    db.session.commit()
    profile = make_profile(db, user)

    # TokenService.generate_tokens reads `request.platform`, so a successful
    # login needs a request context (same arrangement as
    # tests/unit/test_staff_inactive_status_gating.py).
    with app.test_request_context():
        result = StaffService.authenticate_and_link_staff(telegram_id="555000111")

    assert result["user"]["staff_roles"] == ["sales_agent"]
    assert result["user"]["sales_agent_profile_id"] == profile.id
    assert result["user"]["delivery_person_id"] is None


def test_inactive_profile_blocks_login_but_not_the_customer_account(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    user.telegram_id = "555000112"
    db.session.commit()
    make_profile(db, user, is_active=False)

    with pytest.raises(ForbiddenError) as excinfo:
        StaffService.authenticate_and_link_staff(telegram_id="555000112")
    assert excinfo.value.error_code == "STAFF_ACCOUNT_DEACTIVATED"
    assert User.query.get(user.id).status.value == "active"  # User.status untouched: customer bot still works


def test_inactive_profile_blocks_refresh(app, client, db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    make_profile(db, user, is_active=False)
    with app.app_context():
        refresh = create_refresh_token(identity=str(user.id))

    resp = client.post("/api/v1/staff/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})

    assert resp.status_code == 403, resp.get_data(as_text=True)
    assert "STAFF_ACCOUNT_DEACTIVATED" in resp.get_data(as_text=True)


def test_active_profile_passes_the_gate(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    make_profile(db, user, is_active=True)
    StaffService.assert_staff_active(user)  # must not raise
    StaffService.assert_staff_active_by_user_id(user.id)


def test_outlet_pin_outside_the_delivery_zone_is_refused(db):
    """Pin the TASHKENT_POLYGON backstop on Outlet.

    The rule lives in geo_validation.register_delivery_zone_listeners, shared
    with UserAddress; this passes both before and after that extraction, which
    is the point — it holds the behaviour still across the refactor.
    """
    db.session.add(Outlet(name="Out", outlet_type="grocery_store", latitude=40.0, longitude=65.0))
    with pytest.raises(ValidationError):
        db.session.commit()
    db.session.rollback()

    inside = Outlet(name="In", outlet_type="grocery_store", latitude=41.3111, longitude=69.2797)
    db.session.add(inside)
    db.session.commit()

    assert inside.id is not None
