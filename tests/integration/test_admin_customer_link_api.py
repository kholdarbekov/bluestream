from datetime import datetime, UTC

import pytest

from business_app import db as _db
from business_app.models.user import User, UserAddress
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from shared.enums import EntitySubtype, UserRole, UserStatus, UserType
from business_app.utils.password_security import hash_password


def _customer(email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    _db.session.add(u); _db.session.commit()
    return u


def _address(user_id, full_address="A"):
    a = UserAddress(user_id=user_id, full_address=full_address, city="Tashkent",
                    latitude=41.31, longitude=69.28)
    _db.session.add(a); _db.session.commit()
    return a


@pytest.mark.integration
def test_admin_link_and_unlink_flow(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")

    resp = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"secondaryUserId": u2.id, "reason": "same person"},
                       headers=admin_auth_headers)
    assert resp.status_code in (200, 201)
    data = resp.get_json()["data"]
    assert sorted(data["member_user_ids"]) == sorted([u1.id, u2.id])
    assert CustomerLinkEvent.query.filter_by(event_type="link").count() == 1

    resp2 = client.post(f"/api/v1/admin/users/{u1.id}/unlink",
                        json={"reason": "mislink"}, headers=admin_auth_headers)
    assert resp2.status_code == 200
    _db.session.refresh(u1)
    assert u1.canonical_customer_id is None


@pytest.mark.integration
def test_linked_accounts_response_shape_is_pinned(client, db, admin_auth_headers):
    """Contract guard: the admin UI reads these exact keys off this endpoint."""
    u1 = _customer("s1@example.com", "+998900020001")
    u2 = _customer("s2@example.com", "+998900020002")

    # Unlinked account: singleton cluster, no canonical, no primary.
    solo = client.get(f"/api/v1/admin/users/{u1.id}/linked-accounts",
                      headers=admin_auth_headers)
    assert solo.status_code == 200, solo.get_json()
    solo_data = solo.get_json()["data"]
    assert set(solo_data) == {"canonical_customer_id", "primary_user_id", "members"}
    assert solo_data["canonical_customer_id"] is None
    assert solo_data["primary_user_id"] is None
    assert [m["id"] for m in solo_data["members"]] == [u1.id]

    link = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"secondaryUserId": u2.id, "reason": "same person"},
                       headers=admin_auth_headers)
    assert link.status_code in (200, 201), link.get_json()
    canonical_id = link.get_json()["data"]["canonical_customer_id"]

    resp = client.get(f"/api/v1/admin/users/{u1.id}/linked-accounts",
                      headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert set(data) == {"canonical_customer_id", "primary_user_id", "members"}
    assert data["canonical_customer_id"] == canonical_id
    assert data["primary_user_id"] == u1.id
    assert sorted(m["id"] for m in data["members"]) == sorted([u1.id, u2.id])
    for member in data["members"]:
        assert set(member) == {"id", "first_name", "last_name", "phone"}
    member_by_id = {m["id"]: m for m in data["members"]}
    assert member_by_id[u2.id]["phone"] == "+998900020002"
    assert member_by_id[u2.id]["first_name"] == "T"
    assert member_by_id[u2.id]["last_name"] == "U"


@pytest.mark.integration
def test_admin_link_requires_admin(client, db, auth_headers):
    # auth_headers is a plain customer JWT -> must be rejected by validate_admin_action.
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    resp = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"secondaryUserId": u2.id, "reason": "x"}, headers=auth_headers)
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Task 7: the address-groups route now mints OWNERLESS place groups. Same URL,
# same {address_group_id, address_ids} response shape; the canonical_id path
# param no longer scopes anything.
# --------------------------------------------------------------------------- #

@pytest.mark.integration
def test_address_groups_route_creates_ownerless_group_spanning_customers(
    client, db, admin_auth_headers
):
    u1 = _customer("g1@example.com", "+998900010001")
    u2 = _customer("g2@example.com", "+998900010002")
    a1 = _address(u1.id, "A")
    a2 = _address(u2.id, "B")
    # 999 is a canonical id that does not exist -- the param must not scope anything.
    resp = client.post("/api/v1/admin/canonical-customers/999/address-groups",
                       json={"addressIds": [a1.id, a2.id], "reason": "same door",
                             "label": "home"},
                       headers=admin_auth_headers)
    assert resp.status_code == 201, resp.get_json()
    data = resp.get_json()["data"]
    assert data["address_ids"] == sorted([a1.id, a2.id])

    group = AddressGroup.query.get(data["address_group_id"])
    assert group is not None
    # Ownerless: the group is NOT fenced to any canonical customer.
    assert group.canonical_customer_id is None
    _db.session.refresh(a1); _db.session.refresh(a2)
    assert a1.address_group_id == a2.address_group_id == group.id


@pytest.mark.integration
def test_address_groups_route_propagates_place_group_error_code(
    client, db, admin_auth_headers
):
    """A fence's error_code must reach the client, not just its prose (plan 2c branches on it)."""
    u1 = _customer("g3@example.com", "+998900010003")
    a1 = _address(u1.id, "A")

    resp = client.post("/api/v1/admin/canonical-customers/1/address-groups",
                       json={"addressIds": [a1.id], "reason": "r"},
                       headers=admin_auth_headers)
    assert resp.status_code == 400
    assert resp.get_json()["data"]["error_code"] == "PLACE_GROUP_MIN_ADDRESSES"


@pytest.mark.integration
def test_address_groups_route_rejects_already_grouped_address_with_code(
    client, db, admin_auth_headers
):
    u1 = _customer("g4@example.com", "+998900010004")
    u2 = _customer("g5@example.com", "+998900010005")
    u3 = _customer("g6@example.com", "+998900010006")
    a1, a2, a3 = _address(u1.id, "A"), _address(u2.id, "B"), _address(u3.id, "C")

    first = client.post("/api/v1/admin/canonical-customers/1/address-groups",
                        json={"addressIds": [a1.id, a2.id], "reason": "r"},
                        headers=admin_auth_headers)
    assert first.status_code == 201

    # Phase 2 rejects re-homing instead of silently moving the address.
    second = client.post("/api/v1/admin/canonical-customers/1/address-groups",
                         json={"addressIds": [a1.id, a3.id], "reason": "r"},
                         headers=admin_auth_headers)
    assert second.status_code == 400
    assert second.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"


@pytest.mark.integration
def test_link_route_propagates_grocery_fence_error_code(client, db, admin_auth_headers):
    """CUSTOMER_LINK_GROCERY_ACCOUNT must survive the route's ValidationError catch."""
    u1 = _customer("g7@example.com", "+998900010007")
    grocery = _customer("g8@example.com", "+998900010008")
    grocery.user_type = UserType.ENTITY
    grocery.entity_subtype = EntitySubtype.GROCERY_STORE
    grocery.company_name = "Shop"
    _db.session.commit()

    resp = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"secondaryUserId": grocery.id, "reason": "r"},
                       headers=admin_auth_headers)
    assert resp.status_code == 400
    assert resp.get_json()["data"]["error_code"] == "CUSTOMER_LINK_GROCERY_ACCOUNT"


@pytest.mark.integration
def test_validation_error_response_without_code_keeps_legacy_shape(
    client, db, admin_auth_headers
):
    """Guard-rail: plain field validations must not sprout an empty data envelope."""
    u1 = _customer("g9@example.com", "+998900010009")
    resp = client.post(f"/api/v1/admin/users/{u1.id}/link",
                       json={"reason": "r"}, headers=admin_auth_headers)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"] == ["secondaryUserId is required"]
    assert "data" not in body
