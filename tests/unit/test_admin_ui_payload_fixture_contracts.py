"""The admin_ui fixtures declare the backend's key sets BY HAND. Pin them.

This module exists because of a specific, repeated failure. The admin_ui tests
mock `adminService` wholesale, so no admin_ui test ever sees a real payload;
each one instead fabricates a literal object and validates it against a key set
copied out of the backend by a human. That is a snapshot, not a contract:

  * `BottleTracking*.test.js` fabricated `user_id` / `customer_name` /
    `customer_phone` / `bottle_balance_id`,
  * `PlaceGroupPanel.test.jsx` fabricated `place_union_balance` and a per-member
    `balance`,

and every one of those files stayed GREEN through the whole
(user, address) -> PLACE re-key, while the balances table, the detail drawer and
the place panel were broken in production.

The JS-side validators reject a fixture that disagrees with its declared key set.
They cannot detect the case that actually happens: the backend renames a field,
so the fixture AND the hand-copied set go stale TOGETHER and agree with each
other perfectly. Only something holding the live payload can see that — hence
these tests, which parse the `new Set([...])` declarations straight out of the
JS sources and diff them against the real thing.

A rename in the backend therefore fails HERE, naming the JS file to fix.
"""

import pathlib
import re
from decimal import Decimal

import pytest

from business_app.serializers.bottle_serializers import serialize_bottle_balance
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import BottleLedgerEventType

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

BOTTLE_TRACKING_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/BottleTracking.test.js"
BOTTLE_DETAIL_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/BottleTracking.bottleDetail.test.js"
BOTTLE_PLACE_WRITE_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/BottleTracking.placeWrite.test.js"
PLACE_GROUP_PANEL_TEST = REPO_ROOT / "admin_ui/src/components/PlaceGroupPanel.test.jsx"
CUSTOMER_MAP_TEST = REPO_ROOT / "admin_ui/src/components/CustomerMap.test.jsx"

_LINE_COMMENT = re.compile(r"//[^\n]*")
_QUOTED = re.compile(r"'([^']*)'")


def _declared_key_set(path: pathlib.Path, name: str) -> set:
    """Extract `const <name> = new Set([...])` from a JS/JSX source.

    Line comments are stripped first so prose inside the declaration can never
    be mistaken for a key.
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"const {re.escape(name)} = new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, f"{name} not found in {path.relative_to(REPO_ROOT)} — did the fixture contract move?"
    body = _LINE_COMMENT.sub("", match.group(1))
    keys = set(_QUOTED.findall(body))
    assert keys, f"{name} in {path.relative_to(REPO_ROOT)} parsed as empty"
    return keys


def _explain(path: pathlib.Path, name: str) -> str:
    return (
        f"\n{name} in {path.relative_to(REPO_ROOT)} no longer matches the live payload. "
        "Update the JS key set AND every fixture built from it — leaving both stale is "
        "exactly how the admin UI shipped broken while its tests stayed green."
    )


@pytest.mark.integration
def test_balance_row_key_sets_match_the_live_serializer(
    app, db, place, sample_user, second_sample_user, user_address
):
    """`BALANCE_ROW_KEYS` is declared identically in two test files. Both must
    equal the union of what a SHARED place row and a SOLO place row really carry
    (the serializer adds `address_title`/`full_address` only when the row has an
    address, which a shared place never does)."""
    service = BottleTrackingService()
    service._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    service._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"))
    service._create_ledger_entry(user_id=sample_user.id, address_id=user_address.id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))
    db.session.flush()

    shared = serialize_bottle_balance(BottleTrackingService.get_place_balance_row(place["a1"].id))
    solo = serialize_bottle_balance(BottleTrackingService.get_place_balance_row(user_address.id))

    # Sanity: the two rows really are the two shapes this union is made of.
    assert shared["is_shared_place"] is True and shared["address_id"] is None
    assert solo["is_shared_place"] is False and solo["address_id"] == user_address.id

    live = set(shared) | set(solo)
    for path in (BOTTLE_TRACKING_TEST, BOTTLE_DETAIL_TEST, BOTTLE_PLACE_WRITE_TEST):
        assert _declared_key_set(path, "BALANCE_ROW_KEYS") == live, _explain(path, "BALANCE_ROW_KEYS")


@pytest.mark.integration
def test_address_search_hit_key_set_matches_the_live_service(
    app, db, place, sample_user, second_sample_user
):
    """`CustomerLinkService.search_addresses` — what the bottle write modals'
    PLACE picker is built from.

    The picker folds these hits into one option per place using
    `address_group_id`, and the fine modal's balance readout keys off
    `owner.id`. Rename either and the picker silently offers one option per
    coworker again, or stops showing a balance — neither of which vitest can
    see, because the fixture is hand-written.
    """
    hits = CustomerLinkService().search_addresses("Office", exclude_grouped=False)
    assert hits, "search produced no hit to compare"

    assert _declared_key_set(BOTTLE_PLACE_WRITE_TEST, "ADDRESS_SEARCH_HIT_KEYS") == set(
        hits[0]
    ), _explain(BOTTLE_PLACE_WRITE_TEST, "ADDRESS_SEARCH_HIT_KEYS")
    assert _declared_key_set(BOTTLE_PLACE_WRITE_TEST, "ADDRESS_SEARCH_OWNER_KEYS") == set(
        hits[0]["owner"]
    ), _explain(BOTTLE_PLACE_WRITE_TEST, "ADDRESS_SEARCH_OWNER_KEYS")


@pytest.mark.integration
def test_customer_summary_key_sets_match_the_live_payload(
    app, db, place, sample_user, second_sample_user
):
    """The fine modal's balance-context payload — `get_customer_summary`, behind
    `GET /admin/bottles/balances/<user_id>`."""
    service = BottleTrackingService()
    service._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    db.session.flush()

    live = service.get_customer_summary(sample_user.id)
    assert live["addresses"] and live["cluster_scopes"], "fixture produced nothing to compare"

    for name, real in (
        ("CUSTOMER_SUMMARY_KEYS", set(live)),
        ("CUSTOMER_SUMMARY_ADDRESS_KEYS", set(live["addresses"][0])),
        ("CUSTOMER_SUMMARY_SCOPE_KEYS", set(live["cluster_scopes"][0])),
    ):
        assert _declared_key_set(BOTTLE_DETAIL_TEST, name) == real, _explain(BOTTLE_DETAIL_TEST, name)


@pytest.mark.integration
def test_place_group_detail_key_sets_match_the_live_route(
    app, client, db, admin_auth_headers, place, sample_user, second_sample_user
):
    """`GET /admin/place-groups/<id>` — driven through the real route, because
    `cod` is added by the route and not by the service."""
    created = client.post(
        "/api/v1/admin/place-groups",
        json={"addressIds": [place["a1"].id, place["a2"].id], "reason": "already one office"},
        headers=admin_auth_headers,
    )
    # `place` pre-groups these two addresses, so creation is expected to be
    # rejected; the group already exists and its audit trail is what matters.
    group_id = place["group"].id
    if created.status_code == 201:
        group_id = created.get_json()["data"]["place_group_id"]

    detail = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert detail.status_code == 200, detail.get_json()
    live = detail.get_json()["data"]

    assert _declared_key_set(PLACE_GROUP_PANEL_TEST, "GROUP_DETAIL_KEYS") == set(live), _explain(
        PLACE_GROUP_PANEL_TEST, "GROUP_DETAIL_KEYS"
    )
    assert live["members"], "no members to compare"
    assert _declared_key_set(PLACE_GROUP_PANEL_TEST, "GROUP_MEMBER_KEYS") == set(
        live["members"][0]
    ), _explain(PLACE_GROUP_PANEL_TEST, "GROUP_MEMBER_KEYS")


@pytest.mark.integration
def test_merge_preview_key_sets_the_panel_reads_are_still_served(
    app, client, db, admin_auth_headers, place, sample_user, second_sample_user
):
    """`GET /admin/place-groups/merge-preview` — the merge review's decision aid.

    A SUBSET assertion, unlike the equality checks above, and deliberately so:
    the route spreads the whole `serialize_bottle_ledger_entry` output onto every
    row, so pinning equality would force the JSX fixture to carry a dozen keys
    the panel never reads and would go red on any unrelated serializer addition.
    What must never happen is the reverse — the panel indexing by a key the
    backend stopped sending, which is invisible to vitest (the fixture is
    hand-written and would be renamed in lockstep) and renders as `undefined`.

    The concrete stakes: `preview_balance_after` is a TRANSIENT attribute
    `build_merge_preview` attaches; `projected_place_balance` is what the place
    will actually hold, which on a drifted place is NOT `resulting_balance`.
    Lose either silently and the dialog states the wrong outcome to the admin
    who is about to authorise a correction against it.
    """
    service = BottleTrackingService()
    service._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    service._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                                 event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"))
    db.session.commit()

    resp = client.get(
        "/api/v1/admin/place-groups/merge-preview",
        query_string={
            "address_ids": f"{place['a1'].id},{place['a2'].id}",
            "group_id": place["group"].id,
        },
        headers=admin_auth_headers,
    )
    assert resp.status_code == 200, resp.get_json()
    live = resp.get_json()["data"]
    assert live["entries"], "fixture produced no ledger entries to compare"

    read = _declared_key_set(PLACE_GROUP_PANEL_TEST, "MERGE_PREVIEW_KEYS")
    assert read <= set(live), _explain(PLACE_GROUP_PANEL_TEST, "MERGE_PREVIEW_KEYS")
    entry_read = _declared_key_set(PLACE_GROUP_PANEL_TEST, "MERGE_PREVIEW_ENTRY_KEYS")
    assert entry_read <= set(live["entries"][0]), _explain(
        PLACE_GROUP_PANEL_TEST, "MERGE_PREVIEW_ENTRY_KEYS"
    )


@pytest.mark.integration
def test_place_group_event_key_set_matches_the_live_audit_trail(
    app, client, db, admin_auth_headers
):
    """The audit rows are only emitted once a group has actually been created
    through the admin surface, so this drives the whole create -> read cycle."""
    from datetime import UTC, datetime

    from business_app.models.user import User, UserAddress
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserType

    owners = []
    for i in (1, 2):
        user = User(
            email=f"fixture-contract-{i}@example.com",
            phone=f"+99890111000{i}",
            password_hash=hash_password("TestPassword123!"),
            first_name="Fixture", last_name=f"Contract{i}",
            user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
            is_verified=True, created_at=datetime.now(UTC),
        )
        db.session.add(user)
        owners.append(user)
    db.session.commit()

    address_ids = []
    for user in owners:
        address = UserAddress(user_id=user.id, title="work", full_address="9 Contract St, Tashkent",
                              street_address="9 Contract St", city="Tashkent",
                              latitude=41.3111, longitude=69.2797)
        db.session.add(address)
        db.session.commit()
        address_ids.append(address.id)

    created = client.post(
        "/api/v1/admin/place-groups",
        json={"addressIds": address_ids, "label": "Contract office", "reason": "coworkers"},
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    detail = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert detail.status_code == 200, detail.get_json()
    events = detail.get_json()["data"]["events"]
    assert events, "creating a group must leave an audit row"

    assert _declared_key_set(PLACE_GROUP_PANEL_TEST, "GROUP_EVENT_KEYS") == set(events[0]), _explain(
        PLACE_GROUP_PANEL_TEST, "GROUP_EVENT_KEYS"
    )


@pytest.mark.integration
def test_customer_map_pin_key_set_matches_the_live_route(
    app, client, db, admin_auth_headers, place, sample_user, second_sample_user,
    user_address, seeded_orders_for_map
):
    """`GET /admin/customers/map-pins` — driven through the real ROUTE, because
    the camelCase the frontend consumes is produced by the alias hop and by
    nothing else.

    `CustomerMapService.get_customer_map_pins` returns snake_case; the camelCase
    that `CustomerMap.js` and `customerMapLogic.js` actually read is minted at
    the very last moment by `CustomerMapPinSchema`'s
    `alias_generator=to_camel` plus the route's `model_dump(by_alias=True)`
    (business_app/api/admin.py). A service-level pin test cannot see that hop, so
    dropping `by_alias=True` — or adding any serialization interceptor between
    the schema and the response — leaves the whole backend suite green while the
    shared-place badge vanishes, the glyph reverts to solid and `heatWeight`'s
    divisor falls back to 1, restoring the "21 bottles for one 7-bottle office"
    over-count this plan exists to remove.
    """
    resp = client.get("/api/v1/admin/customers/map-pins", headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    pins = resp.get_json()["data"]["pins"]
    assert pins, "fixture produced no pins to compare"

    # The literal spellings the frontend indexes by, asserted BEFORE anything
    # indexes by them — otherwise dropping `by_alias=True` surfaces as a bare
    # KeyError instead of naming what broke and where.
    for alias, consumer, damage in (
        ("addressId", "CustomerMap.js (pin key + popup)", "no pin renders at all"),
        ("isSharedPlace", "CustomerMap.js:156,157,192",
         "the shared-place badge disappears and the glyph reverts to solid"),
        ("placeMemberCount", "customerMapLogic.js:55 (heatWeight divisor)",
         "the divisor falls back to 1, reading one 7-bottle office as 21 bottles"),
    ):
        missing = [p for p in pins if alias not in p]
        assert not missing, (
            f"\nmap pins no longer carry the camelCase alias `{alias}` that {consumer} reads, "
            f"so {damage} — with every JS test still green, because "
            f"{CUSTOMER_MAP_TEST.relative_to(REPO_ROOT)} fabricates its pins by hand.\n"
            "The alias is minted ONLY by CustomerMapPinSchema's `alias_generator=to_camel` "
            "plus `model_dump(by_alias=True)` in get_customer_map_pins "
            "(business_app/api/admin.py). Restore both, or re-point the frontend.\n"
            f"A pin actually served: {sorted(missing[0])}"
        )

    by_address = {p["addressId"]: p for p in pins}
    shared = by_address[place["a1"].id]
    solo = by_address[user_address.id]

    # ...and the aliased values still carry the shared/solo distinction, so a
    # presence-only guard cannot pass on an all-defaults payload.
    assert shared["isSharedPlace"] is True and shared["placeMemberCount"] == 2
    assert solo["isSharedPlace"] is False and solo["placeMemberCount"] == 1

    # Whole-shape pin, same idiom as the sets above: the fixture in
    # CustomerMap.test.jsx is hand-written camelCase and would otherwise rot in
    # lockstep with any rename.
    assert _declared_key_set(CUSTOMER_MAP_TEST, "MAP_PIN_KEYS") == set(shared), _explain(
        CUSTOMER_MAP_TEST, "MAP_PIN_KEYS"
    )
