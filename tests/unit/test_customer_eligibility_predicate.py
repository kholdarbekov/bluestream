"""One eligibility rule for link / place-group / suggestion paths (shared-place Task 1)."""
import inspect
from datetime import UTC, datetime
from itertools import count

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.user import User, UserAddress
from business_app.services import customer_link_service
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, UserRole, UserStatus, UserType

_seq = count(1)

CASES = [
    (UserType.INDIVIDUAL, UserRole.CUSTOMER, None, True),
    (UserType.ENTITY, UserRole.CUSTOMER, None, False),
    (UserType.ENTITY, UserRole.CUSTOMER, EntitySubtype.GROCERY_STORE, False),
    (UserType.STAFF, UserRole.DELIVERY_DRIVER, None, False),
    (UserType.INDIVIDUAL, UserRole.ADMIN, None, False),
]


def _user(user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, subtype=None):
    n = next(_seq)
    u = User(
        email=f"elig{n}@example.com", phone=f"+9987970{n:05d}",
        password_hash=hash_password("TestPassword123!"), first_name="E", last_name="L",
        user_type=user_type, role=role, entity_subtype=subtype, status=UserStatus.ACTIVE,
        is_verified=True, created_at=datetime.now(UTC),
    )
    _db.session.add(u)
    _db.session.commit()
    return u


def _address(user):
    a = UserAddress(user_id=user.id, full_address="A", city="Tashkent",
                    latitude=41.31, longitude=69.28)
    _db.session.add(a)
    _db.session.commit()
    return a


def _admin_headers():
    admin = _user(UserType.STAFF, UserRole.ADMIN)
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.unit
@pytest.mark.parametrize("user_type,role,subtype,expected", CASES)
def test_is_eligible_individual_customer(db, user_type, role, subtype, expected):
    u = _user(user_type, role, subtype)
    assert CustomerLinkService.is_eligible_individual_customer(u) is expected


@pytest.mark.unit
def test_clause_selects_exactly_the_eligible_users(db):
    users = [_user(t, r, s) for t, r, s, _ in CASES]
    ids = {u.id for u in users}
    expected = {u.id for u, (_, _, _, ok) in zip(users, CASES) if ok}
    clause = CustomerLinkService.eligible_individual_customer_clause()
    got = {u.id for u in User.query.filter(clause, User.id.in_(ids))}
    assert got == expected and len(expected) == 1


@pytest.mark.integration
def test_link_keeps_its_grocery_code(client, db):
    u1 = _user()
    grocery = _user(UserType.ENTITY, UserRole.CUSTOMER, EntitySubtype.GROCERY_STORE)
    resp = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"secondaryUserId": grocery.id, "reason": "r"},
                       headers=_admin_headers())
    assert resp.status_code == 400
    assert resp.get_json()["data"]["error_code"] == "CUSTOMER_LINK_GROCERY_ACCOUNT"


@pytest.mark.integration
def test_link_keeps_its_not_individual_code(client, db):
    u1 = _user()
    entity = _user(UserType.ENTITY, UserRole.CUSTOMER, EntitySubtype.WORKPLACE)
    resp = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"secondaryUserId": entity.id, "reason": "r"},
                       headers=_admin_headers())
    assert resp.status_code == 400
    assert resp.get_json()["data"]["error_code"] == "CUSTOMER_LINK_NOT_INDIVIDUAL"


@pytest.mark.integration
@pytest.mark.parametrize("kind,code", [
    ("grocery", "PLACE_GROUP_GROCERY_MEMBER"),
    ("entity", "PLACE_GROUP_ENTITY_MEMBER"),
])
def test_place_group_keeps_its_codes(client, db, kind, code):
    normal = _address(_user())
    bad_owner = _user(UserType.ENTITY, UserRole.CUSTOMER,
                      EntitySubtype.GROCERY_STORE if kind == "grocery" else EntitySubtype.WORKPLACE)
    bad = _address(bad_owner)
    resp = client.post("/api/v1/admin/place-groups",
                       json={"addressIds": [normal.id, bad.id], "reason": "r"},
                       headers=_admin_headers())
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["data"]["error_code"] == code


@pytest.mark.unit
def test_no_inline_copy_survives():
    src = inspect.getsource(customer_link_service)
    assert src.count("User.user_type == UserType.INDIVIDUAL") == 1
    assert "u_type != UserType.INDIVIDUAL.value" not in src
    assert "o_type != UserType.INDIVIDUAL.value" not in src
