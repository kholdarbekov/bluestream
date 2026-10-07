"""`can_manage_sales_pay`: the flag the admin UI reads to show Compensation (spec §5.6).

It is driven through the login route the admin UI calls (`authService.login` stores
`data.permissions`) and through the profile route the UI re-reads. `super_admin_required`, on every
`/api/v1/admin/sales/pay/*` route, states the same rule, and the last test ties the two together: the
token whose flag is true reaches A12, and the token whose flag is false is refused. Task 10
generalises that tie over every pay route.
"""

import pytest

from tests.integration.sales_pay_builders import APPROVALS

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
PROFILE = "/api/v1/auth/profile"
PLANS = "/api/v1/admin/sales/pay/plans"


@pytest.mark.parametrize(
    "user_fixture,identifier_field,password,expected",
    [
        ("admin_user", "email", "AdminPassword123!", True),
        ("manager_user", "email", "ManagerPassword123!", False),
        ("operator_user", "email", "OperatorPassword123!", False),
        ("sales_agent_user", "phone", "AgentPassword123!", False),
    ],
    ids=["admin", "manager", "operator", "sales-agent"],
)
def test_login_publishes_the_pay_flag_to_admins_only(
    request, client, db, user_fixture, identifier_field, password, expected
):
    user = request.getfixturevalue(user_fixture)

    response = client.post(LOGIN, json={"identifier": getattr(user, identifier_field), "password": password})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["permissions"]["can_manage_sales_pay"] is expected


def test_the_profile_the_admin_ui_rereads_carries_the_same_flag(client, db, admin_claim_headers, manager_claim_headers):
    flags = [
        client.get(PROFILE, headers=headers).get_json()["data"]["permissions"]["can_manage_sales_pay"]
        for headers in (admin_claim_headers, manager_claim_headers)
    ]

    assert flags == [True, False]


def test_the_flag_and_the_pay_routes_state_one_rule(client, db, admin_claim_headers, manager_claim_headers):
    for headers, reaches_pay in ((admin_claim_headers, True), (manager_claim_headers, False)):
        flag = client.get(PROFILE, headers=headers).get_json()["data"]["permissions"]["can_manage_sales_pay"]
        status = client.get(PLANS, headers=headers).status_code
        assert (flag, status) == (reaches_pay, 200 if reaches_pay else 403)


@pytest.mark.parametrize(
    "user_fixture,identifier_field,password,expected",
    [
        ("admin_user", "email", "AdminPassword123!", True),
        ("manager_user", "email", "ManagerPassword123!", True),
        ("operator_user", "email", "OperatorPassword123!", False),
        ("sales_agent_user", "phone", "AgentPassword123!", False),
    ],
    ids=["admin", "manager", "operator", "sales-agent"],
)
def test_login_publishes_the_order_approval_flag_to_admins_and_managers(
    request, client, db, user_fixture, identifier_field, password, expected
):
    """`can_review_agent_orders` (compensation spec §5.6, C14): the admin UI's Order approvals
    nav, route guard and badge query read it."""
    user = request.getfixturevalue(user_fixture)

    response = client.post(LOGIN, json={"identifier": getattr(user, identifier_field), "password": password})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["permissions"]["can_review_agent_orders"] is expected


def test_a_driver_has_no_order_approval_flag(db, delivery_driver):
    from business_app.services.auth_service import AuthService

    assert AuthService().get_user_permissions(delivery_driver.id)["can_review_agent_orders"] is False


def test_the_order_approval_flag_and_the_queue_state_one_rule(
    app, client, db, admin_claim_headers, manager_claim_headers, operator_user
):
    """T-HOLD-10's tie: the token whose flag is true reaches OA1, the one whose flag is false is
    refused by `manager_or_higher_required`."""
    from flask_jwt_extended import create_access_token

    with app.app_context():
        token = create_access_token(identity=str(operator_user.id), additional_claims={"role": "operator"})
    operator_claim_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for headers, reaches in (
        (admin_claim_headers, True),
        (manager_claim_headers, True),
        (operator_claim_headers, False),
    ):
        flag = client.get(PROFILE, headers=headers).get_json()["data"]["permissions"]["can_review_agent_orders"]
        status = client.get(APPROVALS, headers=headers).status_code
        assert (flag, status) == (reaches, 200 if reaches else 403)
