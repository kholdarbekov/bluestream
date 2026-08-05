"""A1 — the estate-wide read surface behind the "Grouped Addresses" tab.

Today an admin can only reach a place group by first finding and opening one
specific customer (PlaceGroupPanel lives inside the Users detail modal,
admin_ui/src/pages/Users.js:1200). There is no way to see ALL groups, or to ask
for suggestions without already suspecting a customer. That is the usability gap
A1 closes; the suggestion ENGINE itself already exists.

Both routes are READ-ONLY. Suggestions must stay suggestions: spec §2.1
documents seven ways auto-grouping fails dangerously, so
`test_neither_route_creates_a_group` pins that neither GET can write an
`AddressGroup`.

Auth harness copied from `tests/integration/test_admin_place_group_api.py`
(`admin_auth_headers`) — deliberately not a new auth path.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.bottle import BottleBalance
from business_app.models.customer_link import AddressGroup
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserStatus,
    UserType,
)

# Tashkent-centre coordinates — the UserAddress before_insert zone listener
# rejects anything outside TASHKENT_POLYGON.
LAT, LNG = 41.3111, 69.2797
# 0.00001 deg of latitude is 1.1107 m here, so this offset is ~5.998 m: inside
# the shipped 10 m PLACE_SUGGESTION_RADIUS_M and therefore a genuine candidate.
SIX_METRES = 0.000054


def _customer(email, phone):
    user = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name=email.split("@")[0],
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def _address(user, lat=LAT, lng=LNG, group_id=None):
    address = UserAddress(
        user_id=user.id,
        title="work",
        full_address="Office",
        latitude=lat,
        longitude=lng,
        address_group_id=group_id,
    )
    _db.session.add(address)
    _db.session.commit()
    return address


def _group(label):
    group = AddressGroup(label=label)
    _db.session.add(group)
    _db.session.commit()
    return group


def _delivered_cod_debt(user, address, order_number, amount=Decimal("35000.00")):
    """One DELIVERED cash order with outstanding debt, delivered to `address`.

    Mirrors `tests/unit/test_place_cod_read_surfaces.py::_delivered_cod_debt` —
    that shape is what the place COD readers select on (CASH payment,
    outstanding > 0, order DELIVERED, delivery_address_id in the group).
    """
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.DELIVERED,
        subtotal=amount,
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=amount,
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.flush()
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CASH,
        amount=amount,
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"pay_{order_number}",
        outstanding_amount=amount,
        created_at=datetime.now(UTC),
    )
    _db.session.add(payment)
    _db.session.commit()
    return order, payment


@pytest.mark.integration
def test_list_place_groups_returns_every_group_with_its_exposure(
    client, db, admin_auth_headers, sample_user, place
):
    """Label, member count, open COD debt total, unpaid COD orders, BOTTLES.

    The exposure columns are the mitigation, not decoration: with Plan E's gate
    ON, grouping two addresses has money consequences, so an admin must see what
    a group is carrying BEFORE touching it. Both halves must ride along —
    grouping pools the place's bottles into one indivisible pool exactly as it
    pools its COD debt, so a row showing only the money shows half the
    consequence of the act it is about to make easier.

    🔴 `member_count` is distinct address OWNERS, never a raw address count. The
    `place` fixture alone is 2 owners × 1 address each, so the two definitions
    are numerically indistinguishable there and `func.count(UserAddress.id)`
    would pass as `member_count`. `sample_user` therefore contributes a SECOND
    address to the same office below, forcing owners (2) and addresses (3) to
    diverge — this is the plan's named regression ("a 3-person office where one
    person contributed two addresses renders '(4 members)'").
    """
    _delivered_cod_debt(sample_user, place["a1"], "ORD-A1-2-001")
    # Same physical office, same owner as place["a1"] — a second door, not a
    # second person. Carries no order, so the COD figures below are unaffected.
    _address(sample_user, lat=41.2746, lng=69.2061, group_id=place["group"].id)
    # The place's ONE pool (BottleScope's grouped arm keys on address_group_id).
    # 6 is distinct from every other figure on the row, so a swapped field
    # cannot pass.
    _db.session.add(BottleBalance(address_group_id=place["group"].id, balance=Decimal("6.00")))
    _db.session.commit()

    response = client.get("/api/v1/admin/place-groups", headers=admin_auth_headers)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]

    row = next(r for r in body["items"] if r["id"] == place["group"].id)
    assert row["label"] == "office"
    assert row["member_count"] == 2                       # distinct address OWNERS
    assert row["address_count"] == 3                      # ...and addresses are counted separately
    assert row["place_open_cod_debt_total"] == 35000.0
    assert isinstance(row["place_open_cod_debt_total"], (int, float))  # C6 — never "35000.00"
    assert row["active_cod_debt_count"] == 1
    # The other half of the exposure: what the place HOLDS.
    assert row["bottle_exposure"] == 6.0
    # A bare Decimal renders as the STRING "6.00" and the admin UI's arithmetic
    # becomes NaN — same rule as the money field above.
    assert isinstance(row["bottle_exposure"], (int, float))
    assert row["bottle_exposure"] == BottleTrackingService.get_place_balance(place["a1"].id)


@pytest.mark.integration
def test_list_place_groups_reports_zero_bottles_without_minting_a_balance_row(
    client, db, admin_auth_headers
):
    """A place that never moved a bottle renders 0.0 — and STAYS row-less.

    Two things at once, because they fail in opposite directions:

    * the column must not be blank/absent for a group with no `bottle_balances`
      row (the batch reader returns a MISS, not a zero, by design), and
    * looking at a place must never mint that row — `orphaned_place_balances`
      is a real class of bug and `get_balance_row`'s docstring exists because
      of it. A read route is the last place that should create ledger state.
    """
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    group = _group("bottle-less office")
    _address(u1, group_id=group.id)
    _address(u2, group_id=group.id)

    before = BottleBalance.query.count()
    body = client.get("/api/v1/admin/place-groups", headers=admin_auth_headers).get_json()["data"]

    row = next(r for r in body["items"] if r["id"] == group.id)
    assert row["bottle_exposure"] == 0.0
    assert isinstance(row["bottle_exposure"], (int, float))
    assert BottleBalance.query.count() == before


@pytest.mark.integration
def test_list_place_groups_paginates_and_caps_per_page_at_100(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    u3 = _customer("c@example.com", "+998900000003")
    u4 = _customer("d@example.com", "+998900000004")
    g1, g2 = _group("first office"), _group("second office")
    _address(u1, group_id=g1.id)
    _address(u2, group_id=g1.id)
    _address(u3, group_id=g2.id)
    _address(u4, group_id=g2.id)

    body = client.get(
        "/api/v1/admin/place-groups?page=1&per_page=500", headers=admin_auth_headers
    ).get_json()["data"]
    assert body["pagination"]["per_page"] == 100          # clamped, not honoured
    assert len(body["items"]) <= 100
    assert body["pagination"]["total"] == 2

    first = client.get(
        "/api/v1/admin/place-groups?page=1&per_page=1", headers=admin_auth_headers
    ).get_json()["data"]
    assert len(first["items"]) == 1
    assert first["pagination"]["page"] == 1


@pytest.mark.integration
def test_list_place_groups_requires_admin(app):
    # A FRESH client — the session-scoped one can carry a JWT cookie from an
    # earlier web-login test (see tests/unit/test_staff_cod_debtors_api.py:63-66).
    assert app.test_client().get("/api/v1/admin/place-groups").status_code == 401
    assert app.test_client().get("/api/v1/admin/place-group-suggestions").status_code == 401


@pytest.mark.integration
def test_global_suggestions_returns_candidates_with_no_user_anchor(
    client, db, admin_auth_headers
):
    """The service already supports user_id=None; only the route was missing."""
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1 = _address(u1)
    a2 = _address(u2, lat=LAT + SIX_METRES)

    response = client.get("/api/v1/admin/place-group-suggestions", headers=admin_auth_headers)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]
    assert len(body) >= 1
    assert body[0]["distinct_customer_count"] == 2
    assert sorted(body[0]["address_ids"]) == sorted([a1.id, a2.id])


@pytest.mark.integration
def test_global_suggestions_omits_dismissed_points(client, db, admin_auth_headers, admin_user):
    """PlaceSuggestionDismissal suppression must survive the un-anchored path."""
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1 = _address(u1)
    a2 = _address(u2, lat=LAT + SIX_METRES)
    CustomerLinkService().dismiss_place_suggestion(a1.id, a2.id, admin_user.id, "no")

    body = client.get(
        "/api/v1/admin/place-group-suggestions", headers=admin_auth_headers
    ).get_json()["data"]
    assert body == []


@pytest.mark.integration
def test_neither_route_creates_a_group(client, db, admin_auth_headers):
    """🔴 Spec 2.1/2.2: suggestions NEVER auto-group."""
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    _address(u1)
    _address(u2, lat=LAT + SIX_METRES)

    before = AddressGroup.query.count()
    listing = client.get("/api/v1/admin/place-groups", headers=admin_auth_headers)
    suggestions = client.get("/api/v1/admin/place-group-suggestions", headers=admin_auth_headers)
    assert listing.status_code == 200, listing.get_json()
    assert suggestions.status_code == 200, suggestions.get_json()
    # The pair IS a live candidate — so the read genuinely had something it
    # could have auto-grouped, and still did not.
    assert len(suggestions.get_json()["data"]) == 1
    assert AddressGroup.query.count() == before
