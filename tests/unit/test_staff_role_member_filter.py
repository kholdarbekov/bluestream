"""`StaffService.staff_role_member_filter` is the ONE predicate for "holds staff role X".

`_is_operator_staff_member` (business_app/api/admin.py) and
`SalesAgentAccountService.member_filter` used to each carry their own
`or_(User.role == ..., cast(User.staff_roles, String).ilike(...))` copy; the
sales tasks needed a third. The HTTP halves below drive the two endpoints that
read the operator predicate, so a regression in the shared filter shows up on
the surfaces a human actually looks at, not only in a service-level unit test.
"""

from datetime import UTC, datetime

import pytest

from business_app.models.user import User
from business_app.services.staff_service import StaffService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


def _make_user(db, phone, role, staff_roles=None, user_type=UserType.INDIVIDUAL):
    user = User(
        phone=phone,
        password_hash=hash_password("FilterPassword123!"),
        first_name="Filter",
        last_name=phone[-3:],
        user_type=user_type,
        role=role,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    if staff_roles is not None:
        user.staff_roles = staff_roles
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def staff_roles_operator(db):
    """A customer-role account whose operator access lives only in staff_roles."""
    return _make_user(
        db, "+998901234581", UserRole.CUSTOMER, staff_roles=["operator"], user_type=UserType.STAFF
    )


@pytest.fixture
def plain_customer(db):
    return _make_user(db, "+998901234582", UserRole.CUSTOMER)


def test_filter_matches_both_the_column_and_the_staff_roles_json(
    db, staff_roles_operator, plain_customer, operator_user
):
    matched = User.query.filter(StaffService.staff_role_member_filter("operator")).all()

    assert sorted(u.id for u in matched) == sorted([staff_roles_operator.id, operator_user.id])
    assert plain_customer.id not in {u.id for u in matched}


def test_staff_operators_endpoint_lists_a_staff_roles_only_operator(
    client, admin_claim_headers, staff_roles_operator, plain_customer
):
    response = client.get("/api/v1/admin/staff/operators", headers=admin_claim_headers)

    assert response.status_code == 200
    body = response.get_json()
    ids = [row["id"] for row in body["data"]["items"]]
    assert ids == [staff_roles_operator.id]
    assert body["meta"]["summary"]["total_operators"] == 1


def test_staff_overview_counts_a_staff_roles_only_operator(
    client, admin_claim_headers, staff_roles_operator, plain_customer
):
    response = client.get("/api/v1/admin/staff/overview", headers=admin_claim_headers)

    assert response.status_code == 200
    assert response.get_json()["data"]["overview"]["total_operators"] == 1
