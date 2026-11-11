"""`can_review_shared_places` and `can_manage_users`: the flags the admin UI reads (spec 2026-11-10 §4.7, §9).

Both derive from the route guard's own test (`has_admin_permission` with `manage_users`), never hand-set, so a
manager is no longer told it may manage users. Driven through the login route the admin UI calls
(`authService.login` stores `data.permissions`) and the profile route it re-reads; the last test ties each flag to
the case routes' answer for the same token.
"""

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.shared_place_case import SharedPlaceCase
from business_app.services.auth_service import AuthService
from tests.integration.shared_place_builders import BASE_LAT, BASE_LNG, customer, pinned_address, stop_for

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
PROFILE = "/api/v1/auth/profile"
LIST = "/api/v1/admin/shared-place-cases"
DETAIL = "/api/v1/admin/shared-place-cases/{}"
FLAGS = ("can_review_shared_places", "can_manage_users")


@pytest.fixture
def operator_claim_headers(app, operator_user):
    with app.app_context():
        token = create_access_token(identity=str(operator_user.id), additional_claims={"role": "operator"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _flags(permissions):
    return {flag: permissions[flag] for flag in FLAGS}


@pytest.mark.parametrize(
    "user_fixture,password,expected",
    [
        ("admin_user", "AdminPassword123!", True),
        ("manager_user", "ManagerPassword123!", False),
        ("operator_user", "OperatorPassword123!", False),
    ],
    ids=["admin", "manager", "operator"],
)
def test_login_publishes_both_flags_to_admins_only(request, db, user_fixture, password, expected):
    user = request.getfixturevalue(user_fixture)

    response = current_app.test_client().post(LOGIN, json={"identifier": user.email, "password": password})

    assert response.status_code == 200, response.get_data(as_text=True)
    assert _flags(response.get_json()["data"]["permissions"]) == {flag: expected for flag in FLAGS}


def test_the_profile_the_admin_ui_rereads_carries_the_same_flags(
    db, admin_claim_headers, manager_claim_headers, operator_claim_headers
):
    flags = [
        _flags(current_app.test_client().get(PROFILE, headers=headers).get_json()["data"]["permissions"])
        for headers in (admin_claim_headers, manager_claim_headers, operator_claim_headers)
    ]

    assert flags == [
        {"can_review_shared_places": True, "can_manage_users": True},
        {"can_review_shared_places": False, "can_manage_users": False},
        {"can_review_shared_places": False, "can_manage_users": False},
    ]


def test_a_driver_and_a_customer_hold_neither_flag(db, delivery_driver):
    u = customer(db, "Jasur")

    assert [_flags(AuthService().get_user_permissions(user.id)) for user in (delivery_driver, u)] == [
        {"can_review_shared_places": False, "can_manage_users": False}
    ] * 2


def test_each_flag_equals_the_case_routes_answer(
    db, admin_claim_headers, manager_claim_headers, operator_claim_headers
):
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    order, _ = stop_for(u, address, None, quantity=1)
    case = SharedPlaceCase(
        order_id=order.id,
        address_id=address.id,
        returned_qty=3,
        place_balance_before=0,
        candidates=[],
        empty_reason="none_within_radius",
        status="awaiting_admin",
    )
    db.session.add(case)
    db.session.commit()

    seen = []
    for headers in (admin_claim_headers, manager_claim_headers, operator_claim_headers):
        client = current_app.test_client()
        permissions = client.get(PROFILE, headers=headers).get_json()["data"]["permissions"]
        statuses = [client.get(url, headers=headers).status_code for url in (LIST, DETAIL.format(case.id))]
        reaches = statuses == [200, 200]
        assert statuses in ([200, 200], [403, 403])
        assert _flags(permissions) == {flag: reaches for flag in FLAGS}
        seen.append(reaches)

    assert seen == [True, False, False]
