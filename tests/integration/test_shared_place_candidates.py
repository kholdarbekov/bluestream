"""The candidate finder: which nearby places a driver is offered (spec 2026-11-10 section 4.3)."""

import pytest

from business_app.services.shared_place.candidates import find_candidates
from shared.enums import OrderStatus, UserRole, UserType
from tests.integration.reorder_reminder_builders import at, delivered_order, link, live_order, make_customer, store_fixture  # noqa: F401
from tests.integration.shared_place_builders import BASE_LAT, BASE_LNG, customer, empties_at, group_of, pinned_address

pytestmark = pytest.mark.integration

# Latitude offsets worth ~14, 28, 42, 49 and 60 m.
M14, M28, M42, M49, M60 = 0.000126, 0.000252, 0.000378, 0.00044, 0.00054


def _delivery_address(db, *, apartment=None):
    buyer = customer(db, "Buyer")
    return pinned_address(db, buyer, BASE_LAT, BASE_LNG, apartment=apartment)


def _neighbour(db, name, offset, n=2, *, apartment=None, **kwargs):
    address = pinned_address(db, customer(db, name, **kwargs), BASE_LAT + offset, BASE_LNG, apartment=apartment)
    empties_at(address, n)
    return address


def test_no_pin_returns_no_pin(db):
    address = _delivery_address(db)
    address.latitude = address.longitude = None
    db.session.commit()
    _neighbour(db, "Near", M14)

    result = find_candidates(address)

    assert (result.candidates, result.empty_reason) == ((), "no_pin")


def test_nearest_three_within_50m_in_order(db):
    address = _delivery_address(db)
    for name, offset in (("A", M14), ("B", M28), ("C", M42), ("D", M49), ("E", M60)):
        _neighbour(db, name, offset)

    result = find_candidates(address)

    assert [c.distance_m for c in result.candidates] == [14, 28, 42]
    assert [c.first_name for c in result.candidates] == ["A", "B", "C"]
    assert result.empty_reason is None


def test_places_without_empties_are_not_offered(db):
    address = _delivery_address(db)
    pinned_address(db, customer(db, "NoBottles"), BASE_LAT + M14, BASE_LNG)  # no balance row at all
    _neighbour(db, "Zero", M28, 0)
    _neighbour(db, "Has", M42, 3)

    result = find_candidates(address)

    assert [(c.first_name, c.empties) for c in result.candidates] == [("Has", 3)]


def test_group_is_one_row_with_group_balance_and_nearest_member(store):
    address = _delivery_address(store)
    near = pinned_address(store, customer(store, "Near"), BASE_LAT + M28, BASE_LNG)
    far = pinned_address(store, customer(store, "Far"), BASE_LAT + M49, BASE_LNG)
    group = group_of(store, near, far)
    empties_at(near, 5)
    # an ungrouped neighbour further out keeps the group row honest about being ONE row
    _neighbour(store, "Solo", M42, 2)

    result = find_candidates(address)

    assert [(c.ref_kind, c.ref_id, c.empties, c.distance_m) for c in result.candidates] == [
        ("group", group.id, 5, 28),
        ("address", result.candidates[1].ref_id, 2, 42),
    ]
    assert result.candidates[0].member_address_ids == (near.id, far.id)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda db: customer(db, "Shop", user_type=UserType.ENTITY), id="entity"),
        pytest.param(lambda db: make_customer(db, first_name="Staff", role=UserRole.OPERATOR), id="staff"),
        pytest.param(lambda db: customer(db, "Grocer", grocery=True), id="grocery"),
    ],
)
def test_ineligible_owners_are_not_offered(store, build):
    address = _delivery_address(store)
    owner = build(store)
    empties_at(pinned_address(store, owner, BASE_LAT + M14, BASE_LNG), 4)

    result = find_candidates(address)

    assert (result.candidates, result.empty_reason) == ((), "none_eligible")


def test_group_with_one_ineligible_member_is_not_offered(store):
    address = _delivery_address(store)
    nearby = pinned_address(store, customer(store, "Nearby"), BASE_LAT + M14, BASE_LNG)
    # the ineligible member is FAR from the pin: the rule covers every member, not just the nearby ones
    shop = pinned_address(store, customer(store, "Shop", user_type=UserType.ENTITY), BASE_LAT + 0.01, BASE_LNG)
    group_of(store, nearby, shop)
    empties_at(nearby, 6)

    result = find_candidates(address)

    assert (result.candidates, result.empty_reason) == ((), "none_eligible")


def test_different_apartment_flat_is_not_offered_but_blank_apartment_is(store):
    address = _delivery_address(store, apartment=" 12A ")
    _neighbour(store, "Other", M14, apartment="13")
    _neighbour(store, "Same", M28, apartment="12a")
    _neighbour(store, "Blank", M42, apartment="  ")
    _neighbour(store, "Unset", M49, apartment=None)

    result = find_candidates(address)

    assert [c.first_name for c in result.candidates] == ["Same", "Blank", "Unset"]


def test_blank_delivery_apartment_offers_every_flat(db):
    address = _delivery_address(db, apartment=None)
    _neighbour(db, "Flat", M14, apartment="13")

    assert [c.first_name for c in find_candidates(address).candidates] == ["Flat"]


def test_own_and_linked_other_address_is_offered(db):
    buyer = customer(db, "Akmal")
    address = pinned_address(db, buyer, BASE_LAT, BASE_LNG)
    own_other = pinned_address(db, buyer, BASE_LAT + M14, BASE_LNG)
    empties_at(own_other, 8)
    linked = customer(db, "Linked")
    link(db, buyer, linked)
    linked_address = pinned_address(db, linked, BASE_LAT + M28, BASE_LNG)
    empties_at(linked_address, 3)

    result = find_candidates(address)

    assert [(c.first_name, c.empties, c.distance_m) for c in result.candidates] == [("Akmal", 8, 14), ("Linked", 3, 28)]


def test_delivery_address_itself_is_not_offered(db):
    address = _delivery_address(db)
    empties_at(address, 9)

    result = find_candidates(address)

    assert (result.candidates, result.empty_reason) == ((), "none_within_radius")


def test_own_group_members_are_not_offered(store):
    buyer = customer(store, "Buyer")
    address = pinned_address(store, buyer, BASE_LAT, BASE_LNG)
    roommate = pinned_address(store, customer(store, "Roommate"), BASE_LAT + M14, BASE_LNG)
    group_of(store, address, roommate)
    empties_at(roommate, 4)

    result = find_candidates(address)

    assert (result.candidates, result.empty_reason) == ((), "none_within_radius")


def test_shown_name_is_newest_orderer_then_owner(store):
    address = _delivery_address(store)
    owner = customer(store, "Owner")
    place = pinned_address(store, owner, BASE_LAT + M14, BASE_LNG)
    empties_at(place, 2)
    delivered_order(store, customer(store, "Older"), at(1), address=place)
    delivered_order(store, customer(store, "Newer"), at(5), address=place)
    live_order(store, customer(store, "Ghost"), at(9), status=OrderStatus.CANCELLED, address=place)
    bare = pinned_address(store, customer(store, "Bare"), BASE_LAT + M28, BASE_LNG)
    empties_at(bare, 2)
    blank_owner = customer(store, "x")
    blank_owner.first_name = ""
    store.session.commit()
    blank = pinned_address(store, blank_owner, BASE_LAT + M42, BASE_LNG)
    empties_at(blank, 2)

    result = find_candidates(address)

    assert [c.first_name for c in result.candidates] == ["Newer", "Bare", "—"]


def test_empty_reasons_distinguish_radius_from_eligibility(db):
    address = _delivery_address(db)
    assert find_candidates(address).empty_reason == "none_within_radius"

    _neighbour(db, "TooFar", M60)
    assert find_candidates(address).empty_reason == "none_within_radius"

    _neighbour(db, "Shop", M14, user_type=UserType.ENTITY)
    assert find_candidates(address).empty_reason == "none_eligible"


def test_to_json_shape(db):
    address = _delivery_address(db)
    near = _neighbour(db, "Akmal", M14, 8)

    result = find_candidates(address)

    assert result.candidates[0].to_json() == {
        "ref": {"kind": "address", "id": near.id},
        "first_name": "Akmal",
        "empties": 8,
        "distance_m": 14,
    }
