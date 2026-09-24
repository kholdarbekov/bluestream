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
from datetime import timedelta
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
OUTLETS_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/Outlets.test.js"
OUTLETS_PAGE = REPO_ROOT / "admin_ui/src/pages/Outlets.js"
VISITS_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/Visits.test.js"
VISITS_PAGE = REPO_ROOT / "admin_ui/src/pages/Visits.js"
OUTLET_VOCABULARY = REPO_ROOT / "admin_ui/src/components/sales/outletVocabulary.js"
ANALYTICS_PAGE = REPO_ROOT / "admin_ui/src/pages/Analytics.js"
ANALYTICS_AGENT_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/Analytics.agentPerformance.test.js"
DELIVERY_PAGE = REPO_ROOT / "admin_ui/src/pages/Delivery.js"

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


def _declared_array(path: pathlib.Path, name: str) -> list:
    """Extract `const <name> = ['a', 'b', ...];` from a JS source, ORDER PRESERVED.

    Sibling of `_declared_key_set`, and a list rather than a set on purpose: these arrays are
    option lists, so their order is what an operator sees in a picker, and the backend tuples they
    mirror are ordered too (`OUTLET_STAGES` is the pipeline, in pipeline order).
    """
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"const {re.escape(name)} = \[(.*?)\];", source, re.DOTALL)
    assert match, f"{name} not found in {path.relative_to(REPO_ROOT)} — did the mirrored array move?"
    body = _LINE_COMMENT.sub("", match.group(1))
    values = _QUOTED.findall(body)
    assert values, f"{name} in {path.relative_to(REPO_ROOT)} parsed as empty"
    return values


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


@pytest.mark.integration
def test_outlets_page_fixture_matches_serialize_outlet(app, db):
    """`Outlets.test.js` builds its whole outlet fixture by spreading `OUTLET_ROW_KEYS`, so the
    drawer and the table are only ever exercised against the keys that set declares.

    Same failure mode as the balance rows above: rename a column in `serialize_outlet` and the
    vitest fixture goes stale in lockstep with the hand-copied set, agreeing with itself perfectly
    while the drawer renders `undefined`. This is the only thing holding the live payload.
    """
    from business_app.models.sales import Outlet
    from business_app.serializers.sales_serializers import serialize_outlet

    outlet = Outlet(name="Fixture contract", outlet_type="grocery_store")
    db.session.add(outlet)
    db.session.commit()

    assert set(serialize_outlet(outlet)) == _declared_key_set(OUTLETS_TEST, "OUTLET_ROW_KEYS"), _explain(
        OUTLETS_TEST, "OUTLET_ROW_KEYS"
    )


@pytest.mark.integration
def test_visits_row_fixture_matches_serialize_visit_admin_row(app, db):
    """The Visits table's fixture IS the serializer's key set.

    `serialize_visit_admin_row` = `serialize_visit` + three names; a field renamed in the
    serializer and not in the fixture is a column that renders empty in Tashkent while every
    vitest case stays green, because the fixture is the only visit row vitest ever sees.

    Built from a REAL persisted Visit, not a stub: the serializer walks `visit.outlet`,
    `visit.agent` and `visit.order`, so a hand-built namespace would either need those
    relationships faked (a second expression of the serializer) or would raise before it
    could publish a key set worth pinning.
    """
    from datetime import UTC, datetime

    from business_app.models.sales import Outlet
    from business_app.models.sales_visits import Visit
    from business_app.serializers.sales_serializers import serialize_visit_admin_row
    from tests.unit.test_sales_agent_role import make_sales_agent_user

    agent = make_sales_agent_user(db, phone="+998901234599")
    outlet = Outlet(name="Fixture contract", outlet_type="grocery_store",
                    assigned_agent_user_id=agent.id)
    db.session.add(outlet)
    db.session.commit()
    moment = datetime.now(UTC)
    visit = Visit(outlet_id=outlet.id, agent_user_id=agent.id, status="completed", planned=True,
                  current_step="close", started_at=moment, checkin_at=moment, ended_at=moment,
                  outcome="no_order")
    db.session.add(visit)
    db.session.commit()

    declared = _declared_key_set(VISITS_TEST, "VISIT_ROW_KEYS")

    assert declared == set(serialize_visit_admin_row(visit).keys()), _explain(
        VISITS_TEST, "VISIT_ROW_KEYS"
    )


def test_outlets_page_mirrors_the_backend_enumerations():
    """`Outlets.js` and `components/sales/outletVocabulary.js` hand-copy tuples out of
    `business_app/models/sales.py`.

    Nothing else can catch them drifting. The stage/type/class/lost-reason values are CHECK
    constraints and validator whitelists on the backend, so a value added there is simply missing
    from every admin picker (invisible — the page still renders), and a value removed there leaves
    an option that 400s only once an admin picks it. The JS suite cannot see either: it mocks the
    service, so no admin_ui test ever meets the real enumeration.

    Order is asserted too: these arrays are what an operator reads top to bottom, and
    `OUTLET_STAGES` is the pipeline in pipeline order.
    """
    from business_app.models.sales import LOST_REASONS, OUTLET_CLASSES, OUTLET_STAGES, OUTLET_TYPES
    from business_app.models.sales_visits import VISIT_OUTCOMES

    for js_name, live in (
        ("STAGES", OUTLET_STAGES),
        ("OUTLET_TYPES", OUTLET_TYPES),
        ("LOST_REASONS", LOST_REASONS),
    ):
        assert _declared_array(OUTLETS_PAGE, js_name) == list(live), (
            f"\n{js_name} in admin_ui/src/pages/Outlets.js no longer mirrors "
            f"business_app/models/sales.py. Update the JS array — a stage the backend accepts but "
            f"the page never offers is invisible, and one the page offers but the backend rejects "
            f"400s only when an admin picks it."
        )

    # D29/D30: the Edit form and the Contacts tab read three more hand-copies, from one module.
    from business_app.models.sales import CONTACT_ROLES, PAYMENT_TERMS

    for js_name, live in (
        ("OUTLET_CLASSES", OUTLET_CLASSES),
        ("PAYMENT_TERMS", PAYMENT_TERMS),
        ("CONTACT_ROLES", CONTACT_ROLES),
    ):
        assert _declared_array(OUTLET_VOCABULARY, js_name) == list(live), (
            f"\n{js_name} in admin_ui/src/components/sales/outletVocabulary.js no longer mirrors "
            "business_app/models/sales.py."
        )

    # The *Visits* page's outcome filter is the same class of hand-copy, in a second file.
    assert _declared_array(VISITS_PAGE, "VISIT_OUTCOMES") == list(VISIT_OUTCOMES), (
        "\nVISIT_OUTCOMES in admin_ui/src/pages/Visits.js no longer mirrors "
        "business_app/models/sales_visits.py. The outcome filter sends free text straight to "
        "`GET /admin/sales/visits`, which 400s on a value the CHECK constraint never allowed."
    )


def test_delivery_page_mirrors_every_delivery_status():
    """`Delivery.js` hand-copies `DeliveryStatus` into DELIVERY_STATUSES. The status
    filter, every label and the update dropdown's option names are built from it.

    A status the enum gains and the page lacks is unfilterable and renders as its raw
    value. `rescheduled` was exactly that: it needed its own filter option, label and
    colour. A status the page offers but the enum lacks is a filter the backend 400s.
    The page orders its list for reading, so only membership is pinned.

    Each fallback label is the seeded `en` value, so a label reads the same before and
    after the seed runs.
    """
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
    from shared.enums import DeliveryStatus

    declared = _declared_array(DELIVERY_PAGE, "DELIVERY_STATUSES")

    assert len(declared) == len(set(declared)), "DELIVERY_STATUSES lists a status twice"
    assert set(declared) == {status.value for status in DeliveryStatus}, (
        "\nDELIVERY_STATUSES in admin_ui/src/pages/Delivery.js no longer mirrors "
        "shared/enums.py DeliveryStatus. Add the value there, with a fallback label in "
        "DELIVERY_STATUS_FALLBACK_LABELS and a colour in deliveryStatusColors."
    )

    source = DELIVERY_PAGE.read_text(encoding="utf-8")
    block = re.search(r"const DELIVERY_STATUS_FALLBACK_LABELS = \{(.*?)\};", source, re.DOTALL)
    assert block, "DELIVERY_STATUS_FALLBACK_LABELS not found in admin_ui/src/pages/Delivery.js"
    fallbacks = dict(re.findall(r"(\w+): '([^']*)'", _LINE_COMMENT.sub("", block.group(1))))
    assert fallbacks == {
        status.value: BACKEND_TRANSLATIONS[f"ui.delivery.status_{status.value}"]["en"] for status in DeliveryStatus
    }


def test_analytics_agent_columns_mirror_the_metric_keys():
    """`Analytics.js` hand-copies the 20 KPI keys out of `AgentMetricsService.METRIC_KEYS`,
    split into the four groups the card is read in.

    Concatenated, the four arrays ARE `METRIC_KEYS` — order included, because the CSV export
    writes its columns in exactly this order and the spec's key order is what the owner reads.
    The vitest suite cannot catch a drift: it mocks `salesService`, so no admin_ui test ever
    meets the real tuple. A key added on the backend would simply be missing from the table
    (invisible — the page still renders); a key removed there leaves a column that renders
    `undefined` for every agent.
    """
    from business_app.services.sales.agent_metrics_service import METRIC_GROUPS, METRIC_KEYS

    groups = {
        "visits": _declared_array(ANALYTICS_PAGE, "AGENT_METRICS_VISITS"),
        "outlets": _declared_array(ANALYTICS_PAGE, "AGENT_METRICS_OUTLETS"),
        "orders": _declared_array(ANALYTICS_PAGE, "AGENT_METRICS_ORDERS"),
        "discipline": _declared_array(ANALYTICS_PAGE, "AGENT_METRICS_DISCIPLINE"),
    }
    declared = [key for group in groups.values() for key in group]
    assert declared == list(METRIC_KEYS), (
        "\nThe four AGENT_METRICS_* arrays in admin_ui/src/pages/Analytics.js no longer "
        "concatenate to business_app/services/sales/agent_metrics_service.py::METRIC_KEYS. "
        "Update the JS arrays (and the English labels in AGENT_METRIC_LABELS) — a KPI the "
        "backend publishes but the tab never draws is invisible to the owner."
    )
    # R30: and each array is the backend's own group, not just a slice that happens to add up.
    # The staff bot's stats card draws the SAME four headings and is pinned to the same dict,
    # so a metric cannot quietly move group on one surface and stay put on the other.
    assert {name: tuple(keys) for name, keys in groups.items()} == {
        name: tuple(keys) for name, keys in METRIC_GROUPS.items()
    }, (
        "\nThe four AGENT_METRICS_* arrays no longer match METRIC_GROUPS. The grouping has one "
        "expression (the service); move the key there and copy it here, never the other way."
    )


@pytest.mark.integration
def test_analytics_agent_row_fixture_matches_rows_for_period(app, db, sales_agent_user):
    """`Analytics.agentPerformance.test.js` fabricates its agent rows from
    `AGENT_METRICS_ROW_KEYS`, so the tab is only ever exercised against the keys that set
    declares. Rename a KPI in `AgentMetricsService` and the fixture goes stale in lockstep with
    the hand-copied set, agreeing with itself perfectly while the table renders `undefined`.
    This is the only thing holding the live payload.
    """
    from business_app.services.sales.agent_metrics_service import AgentMetricsService
    from business_app.utils.local_windows import local_date

    today = local_date()
    rows = AgentMetricsService.rows_for_period(today - timedelta(days=6), today)

    # The fixture agent is an active sales agent, so the service must answer for him even with
    # no visits and no orders in the window.
    assert [row["agent_user_id"] for row in rows] == [sales_agent_user.id]
    assert set(rows[0]) == _declared_key_set(ANALYTICS_AGENT_TEST, "AGENT_METRICS_ROW_KEYS"), _explain(
        ANALYTICS_AGENT_TEST, "AGENT_METRICS_ROW_KEYS"
    )


def test_the_exception_type_labels_are_the_backends_own_vocabulary():
    """M12: the seven `exceptions.type.*` rows are a hand-copy of `EXCEPTION_TYPES`.

    The feed publishes its vocabulary with every response precisely so the dropdown has no JS
    copy of it, and `Visits.js` builds the label key from the published value — which means an
    eighth type added on the backend arrives in the UI as the raw string
    `duplicate_open_tryout` in three languages, and a retired one leaves a dead row nobody can
    see. Neither shows up in vitest (it mocks the service and feeds it whatever types it likes)
    and neither shows up in the seed's own `_assert_complete`, which only checks that the three
    language blocks agree WITH EACH OTHER. This is the only thing holding the seed to the
    producer, the way `VISIT_OUTCOMES` is held above.
    """
    from business_app.services.sales.exception_feed_service import EXCEPTION_TYPES
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    expected = {f"exceptions.type.{type_}" for type_ in EXCEPTION_TYPES}
    for language, block in UI_SALES_TRANSLATIONS.items():
        seeded = {key for key in block if key.startswith("exceptions.type.")}
        assert seeded == expected, (
            f"\nThe `{language}` block of scripts/seed_ui_sales_translations.py no longer carries "
            f"exactly one exceptions.type.* row per "
            f"business_app/services/sales/exception_feed_service.py::EXCEPTION_TYPES.\n"
            f"missing: {sorted(expected - seeded)}\nextra:   {sorted(seeded - expected)}\n"
            f"An unseeded type renders to a supervisor as its raw backend name, in every language."
        )
        assert all(block[key].strip() for key in seeded), f"empty {language} exception label"

    # ...and the page really does key off the value the route published, rather than a fourth
    # hand-written map that this pin would then be watching the wrong side of.
    assert "sales_agents:exceptions.type.${" in VISITS_PAGE.read_text(encoding="utf-8"), (
        "\nadmin_ui/src/pages/Visits.js no longer builds its exception label key from the "
        "published type. If it now holds its own map of the seven names, pin THAT instead — "
        "otherwise the seed and the page can disagree with nothing to notice."
    )


BRANCH_OUTLET_KEYS = (
    "outlet_account_line",
    "open_receivable_account",
    "bottles_branch",
    "approve_modal_title",
    "attach_modal_title",
    "attach_confirm",
    "contract_number_label",
    # Not new, but its VALUE moves in this task (R29): the import Popconfirm now promises one
    # outlet per ADDRESS. Pinned here so the seed row and the inline default can never drift apart.
    "import_confirm",
)


def test_the_branch_outlet_keys_are_trilingual_and_say_what_the_page_says():
    """D25's admin copy: the seven bare `ui_sales_agents` rows the Outlets drawer and its
    approve/attach modal render, plus `import_confirm`, whose wording this task corrects (R29).

    Two silent failures neither vitest nor the seed's own `_assert_complete` can see. vitest
    mocks i18next and reads the inline English default, so it never meets a Russian row at all;
    `_assert_complete` compares key SETS, so a `{{account}}` dropped from one translation ships
    the literal `{{account}}` to that operator. The last assertion is the seed docstring's rule
    in code: once a row exists the seeded text WINS in every language, so an English row that
    differs from the page's default means the page shows one wording and its source another.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    page = OUTLETS_PAGE.read_text(encoding="utf-8")
    placeholder = re.compile(r"\{\{(\w+)\}\}")
    for key in BRANCH_OUTLET_KEYS:
        values = {}
        for language in ("en", "uz", "ru"):
            block = UI_SALES_TRANSLATIONS[language]
            assert key in block, (
                f"{key!r} is missing from the {language} block of scripts/seed_ui_sales_translations.py"
            )
            assert block[key].strip(), f"empty {language} value for {key!r}"
            values[language] = block[key]
        tokens = {language: frozenset(placeholder.findall(value)) for language, value in values.items()}
        assert len(set(tokens.values())) == 1, f"{key!r} carries different placeholders per language: {tokens}"
        assert values["en"] in page, (
            f"the English row for {key!r} is not the inline default in "
            f"admin_ui/src/pages/Outlets.js — the seeded text wins in every language, so the "
            f"page would render one wording and its own source another."
        )
