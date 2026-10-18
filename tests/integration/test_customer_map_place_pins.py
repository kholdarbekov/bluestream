"""The admin Users map draws ONE marker per shared place, not one per member address.

Driven through the real route the map calls (`GET /api/v1/admin/customers/map-pins`),
because the camelCase keys the UI reads are minted only by `CustomerMapPinSchema`
plus the route's `model_dump(by_alias=True)`. The route swallows every error into a
500, so each test asserts the status code before it reads a pin.

The rules (spec 2026-10-17-map-place-markers-and-group-editing):
  * an order "counts" iff it is confirmed / preparing / out_for_delivery / delivered;
  * the place is dated by the newest counted order delivered to ANY current group
    address, whoever placed it — a member's order to their own home does not count;
  * the marker sits on the group address with the lowest id that has coordinates;
  * the place's debt is exactly what the Grouped Addresses list shows.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from shared.enums import OrderStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_place_group,
    make_user,
)

MAP_PINS_URL = "/api/v1/admin/customers/map-pins"
SETTLED = Decimal("0.00")


def _person(db, first, last):
    user = make_user(db)
    user.first_name, user.last_name = first, last
    db.session.commit()
    return user


def _order(db, user, address, days_ago, status=OrderStatus.DELIVERED, outstanding=SETTLED):
    """An order delivered to `address`, settled unless the test needs it unpaid."""
    order, _payment = delivered_cod_order(
        db,
        user,
        address=address,
        status=status,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
        outstanding=outstanding,
    )
    return order


def _day(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _pins(client, headers):
    resp = client.get(MAP_PINS_URL, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["pins"]


def _place_pin(pins, group):
    matches = [p for p in pins if p["placeGroupId"] == group.id]
    assert len(matches) == 1, matches
    return matches[0]


def _member(place, user):
    rows = [m for m in place["members"] if m["userId"] == user.id]
    assert len(rows) == 1, place["members"]
    return rows[0]


@pytest.mark.integration
def test_a_place_with_one_recent_and_one_stale_member_is_one_recent_pin(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=2, status=OrderStatus.CONFIRMED)
    _order(db, bob, a2, days_ago=45)

    pins = _pins(client, admin_auth_headers)

    place_pins = [p for p in pins if p["placeGroupId"] == group.id]
    assert len(place_pins) == 1 and place_pins[0]["kind"] == "place"
    # The members' own addresses no longer draw coincident pins of their own.
    assert not [p for p in pins if p["kind"] == "address" and p["addressId"] in (a1.id, a2.id)]
    place = place_pins[0]
    assert place["lastOrderDate"].startswith(_day(2))
    assert place["lastOrderBy"] == "Alice Able"
    assert place["orderCount"] == 2
    assert place["isSharedPlace"] is True
    assert place["userId"] is None
    assert [m["userId"] for m in place["members"]] == [alice.id, bob.id]
    assert _member(place, bob)["lastOrderDate"].startswith(_day(45))


@pytest.mark.integration
def test_a_members_home_order_does_not_refresh_the_place(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    home = make_address(db, bob)
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, bob, home, days_ago=1)
    _order(db, bob, a2, days_ago=60)

    pins = _pins(client, admin_auth_headers)

    place = _place_pin(pins, group)
    assert place["lastOrderDate"].startswith(_day(60))
    assert place["orderCount"] == 1
    home_pin = next(p for p in pins if p["addressId"] == home.id)
    assert home_pin["kind"] == "address" and home_pin["lastOrderDate"].startswith(_day(1))
    # Bob's only ungrouped geocoded address is his home: it is 1 of 1, not 1 of 2.
    assert (home_pin["addressIndex"], home_pin["addressCount"]) == (1, 1)
    assert home_pin["placeGroupId"] is None and home_pin["members"] == []
    # Address pins come first, then the place pins.
    kinds = [p["kind"] for p in pins]
    assert kinds == sorted(kinds, key=lambda kind: kind == "place")


@pytest.mark.integration
def test_a_place_no_counted_order_ever_reached_is_one_undated_pin(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    home = make_address(db, alice)
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    # Both members pass the gate (a non-cancelled order somewhere), but nothing
    # counted was ever delivered to a GROUP address: Alice's delivered order went
    # to her ungrouped home and Bob's order to the place is still pending.
    _order(db, alice, home, days_ago=3)
    _order(db, bob, a2, days_ago=1, status=OrderStatus.PENDING)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert place["lastOrderDate"] is None
    assert place["lastOrderBy"] is None
    assert place["orderCount"] == 0


@pytest.mark.integration
def test_a_pending_order_to_the_place_does_not_count(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=10, status=OrderStatus.CONFIRMED)
    _order(db, bob, a2, days_ago=1, status=OrderStatus.PENDING)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert place["lastOrderDate"].startswith(_day(10))
    assert place["orderCount"] == 1
    assert place["lastOrderBy"] == "Alice Able"
    assert _member(place, bob)["lastOrderDate"] is None


@pytest.mark.integration
def test_place_debt_matches_the_grouped_addresses_list(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=3, outstanding=Decimal("15000.00"))

    place = _place_pin(_pins(client, admin_auth_headers), group)
    listed = client.get("/api/v1/admin/place-groups?per_page=100", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.get_json()
    row = next(item for item in listed.get_json()["data"]["items"] if item["id"] == group.id)

    assert row["place_open_cod_debt_total"] == 15000.0  # the comparison is not 0 == 0
    assert place["outstandingDebt"] == row["place_open_cod_debt_total"]
    assert place["activeCodDebtCount"] == row["active_cod_debt_count"]
    assert place["hasDebt"] is True


@pytest.mark.integration
def test_has_debt_flags_a_place_whose_member_owes_elsewhere(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=3)
    # Unpaid, but not delivered to the place: no delivery address at all.
    _order(db, alice, None, days_ago=2, outstanding=Decimal("15000.00"))

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert place["outstandingDebt"] == 0 and place["hasDebt"] is True
    member_row = _member(place, alice)
    assert member_row["outstandingDebt"] > 0
    assert member_row["activeCodDebtCount"] == 1
    assert _member(place, bob)["outstandingDebt"] == 0


@pytest.mark.integration
def test_a_place_without_any_debt_is_not_flagged(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=3)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert place["outstandingDebt"] == 0 and place["activeCodDebtCount"] == 0
    assert place["hasDebt"] is False


@pytest.mark.integration
def test_the_place_marker_sits_on_its_lowest_id_geocoded_address(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    a1.latitude, a1.longitude = 41.3001, 69.2701
    a2.latitude, a2.longitude = 41.3202, 69.2902
    db.session.commit()
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=3)

    place = _place_pin(_pins(client, admin_auth_headers), group)
    assert place["addressId"] == a1.id
    assert (place["lat"], place["lng"]) == (41.3001, 69.2701)
    assert place["addressLabel"] == a1.full_address

    a1.latitude = None
    db.session.commit()

    place = _place_pin(_pins(client, admin_auth_headers), group)
    assert place["addressId"] == a2.id
    assert (place["lat"], place["lng"]) == (41.3202, 69.2902)
    # Alice's own address lost its coordinates, but she is still at the place.
    assert [m["userId"] for m in place["members"]] == [alice.id, bob.id]


@pytest.mark.integration
def test_a_place_whose_members_never_ordered_has_no_pin(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    # A cancelled order does not pass the "who appears" gate either.
    _order(db, alice, a1, days_ago=3, status=OrderStatus.CANCELLED)
    # A solo customer elsewhere, so the map is not simply empty.
    carol = _person(db, "Carol", "Cole")
    carol_home = make_address(db, carol)
    _order(db, carol, carol_home, days_ago=3)

    pins = _pins(client, admin_auth_headers)

    assert [p["addressId"] for p in pins if p["kind"] == "address"] == [carol_home.id]
    assert not [p for p in pins if p["placeGroupId"] == group.id]
    assert not [p for p in pins if p["addressId"] in (a1.id, a2.id)]


@pytest.mark.integration
def test_a_member_who_never_ordered_is_listed_without_a_date(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=5)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert [m["userId"] for m in place["members"]] == [alice.id, bob.id]
    bob_row = _member(place, bob)
    assert bob_row["lastOrderDate"] is None
    assert bob_row["fullName"] == "Bob Brown"
    assert bob_row["phone"] == bob.phone
    assert bob_row["codRestricted"] is False


@pytest.mark.integration
def test_an_order_by_a_non_member_still_dates_the_place(app, client, db, admin_auth_headers):
    """Review Focus 1: the marker dates by whoever ordered to the place."""
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    dave = _person(db, "Dave", "Doe")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=30)
    _order(db, dave, a1, days_ago=1)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert place["lastOrderDate"].startswith(_day(1))
    assert place["lastOrderBy"] == "Dave Doe"
    assert place["orderCount"] == 2
    assert [m["userId"] for m in place["members"]] == [alice.id, bob.id]


@pytest.mark.integration
def test_an_owner_with_two_addresses_in_the_group_is_listed_once(app, client, db, admin_auth_headers):
    """Review Focus 2: one row per OWNER, dated by the newest of their orders here."""
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a1_second, a2 = make_address(db, alice), make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a1_second, a2)
    _order(db, alice, a1, days_ago=20)
    _order(db, alice, a1_second, days_ago=3)
    _order(db, bob, a2, days_ago=10)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert [m["userId"] for m in place["members"]] == [alice.id, bob.id]
    assert _member(place, alice)["lastOrderDate"].startswith(_day(3))
    assert place["orderCount"] == 3


@pytest.mark.integration
def test_a_tie_on_the_newest_order_names_the_lowest_user_id(app, client, db, admin_auth_headers):
    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    same_moment = datetime.now(UTC) - timedelta(days=4)
    for user, address in ((bob, a2), (alice, a1)):
        delivered_cod_order(db, user, address=address, created_at=same_moment, outstanding=SETTLED)

    place = _place_pin(_pins(client, admin_auth_headers), group)

    assert alice.id < bob.id
    assert place["lastOrderBy"] == "Alice Able"
    assert [m["userId"] for m in place["members"]] == [alice.id, bob.id]


@pytest.mark.integration
def test_one_flag_lookup_covers_address_owners_and_place_members(
    app, client, db, admin_auth_headers, monkeypatch
):
    """Every person the payload names is flagged by ONE batched call, never per pin."""
    from business_app.services.cash_collection_service import CashCollectionService

    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    carol = _person(db, "Carol", "Cole")
    alice_home, carol_home = make_address(db, alice), make_address(db, carol)
    a1, a2 = make_address(db, alice), make_address(db, bob)
    make_place_group(db, a1, a2)
    _order(db, alice, alice_home, days_ago=2)
    _order(db, carol, carol_home, days_ago=2)

    real = CashCollectionService.get_cod_restricted_flags
    calls = []

    def spy(self, user_ids):
        calls.append(list(user_ids))
        return real(self, user_ids)

    monkeypatch.setattr(CashCollectionService, "get_cod_restricted_flags", spy)

    _pins(client, admin_auth_headers)

    # Alice is both an address-pin owner and a place member: asked about once.
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([alice.id, bob.id, carol.id])


@pytest.mark.integration
def test_the_map_and_the_grouped_list_read_one_place_debt_decision(
    app, client, db, admin_auth_headers, monkeypatch
):
    """Both surfaces show what `get_place_cod_exposure_by_group` decides — no copy of
    its reader call, row ceiling or miss rule elsewhere. A fabricated answer reaches
    both payloads, which a surface reading the debtor rows itself would not show."""
    from business_app.services.cash_collection_service import (
        CashCollectionService,
        PlaceCodExposure,
        PlaceCodExposureByGroup,
    )

    alice = _person(db, "Alice", "Able")
    bob = _person(db, "Bob", "Brown")
    a1, a2 = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, a1, a2)
    _order(db, alice, a1, days_ago=3)
    decided = PlaceCodExposureByGroup(
        {group.id: PlaceCodExposure(total_outstanding_amount=123.0, active_cod_debt_count=4)}
    )
    monkeypatch.setattr(CashCollectionService, "get_place_cod_exposure_by_group", lambda self: decided)

    place = _place_pin(_pins(client, admin_auth_headers), group)
    listed = client.get("/api/v1/admin/place-groups?per_page=100", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.get_json()
    row = next(item for item in listed.get_json()["data"]["items"] if item["id"] == group.id)

    assert (place["outstandingDebt"], place["activeCodDebtCount"], place["hasDebt"]) == (123.0, 4, True)
    assert (row["place_open_cod_debt_total"], row["active_cod_debt_count"]) == (123.0, 4)
