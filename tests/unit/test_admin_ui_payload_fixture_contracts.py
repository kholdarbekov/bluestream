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
OPERATIONS_MAP_TEST = REPO_ROOT / "admin_ui/src/components/OperationsMap.test.jsx"
ADMIN_UI_SRC = REPO_ROOT / "admin_ui/src"
SALES_SERVICE = REPO_ROOT / "admin_ui/src/services/salesService.js"
SALES_ORDER_APPROVALS_PAGE = REPO_ROOT / "admin_ui/src/pages/SalesOrderApprovals.js"
SALES_ORDER_APPROVALS_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/SalesOrderApprovals.test.js"
PENALTY_PROPOSALS_TEST = REPO_ROOT / "admin_ui/src/__tests__/pages/PenaltyProposals.test.js"
PENALTY_PROPOSALS_PAGE = REPO_ROOT / "admin_ui/src/pages/PenaltyProposals.js"
SALES_AGENTS_PAGE = REPO_ROOT / "admin_ui/src/pages/SalesAgents.js"
TRYOUTS_PAGE = REPO_ROOT / "admin_ui/src/pages/Tryouts.js"
SALES_COMPENSATION_PAGE = REPO_ROOT / "admin_ui/src/pages/SalesCompensation.js"
PAY_COMPONENTS_DIR = REPO_ROOT / "admin_ui/src/components/sales/pay"
SALES_PAY_SERVICE = REPO_ROOT / "admin_ui/src/services/salesPayService.js"

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


@pytest.mark.integration
def test_operations_map_order_entry_fixture_matches_the_live_snapshot(
    client, db, admin_claim_headers, sample_user, delivery_driver
):
    """`GET /admin/dispatch/snapshot` `orders[]`: what every order pin on the dispatch map
    (and the customer map's orders layer) is drawn from.

    Every order fixture in `OperationsMap.test.jsx` is built with `orderEntry`, which throws on
    a key outside `ORDER_ENTRY_KEYS`, and the F8 "needs a new date" pin turns on one published
    flag, `awaiting_new_date`. Rename it in `DispatchService._order_entry` and the map goes back
    to drawing a failed order as filled (a driver is bringing it) and ringed (it is late), while
    every vitest case stays green on its hand-written fixture. Driven through the route with an
    order that really awaits a new date, so the flag's value is pinned as well as its spelling.
    """
    from datetime import date, datetime, timezone

    from business_app.models.delivery import Delivery
    from business_app.models.order import Order
    from business_app.models.user import UserAddress
    from shared.enums import DeliveryStatus, OrderStatus

    board_day = date(2026, 9, 25)
    address = UserAddress(
        user_id=sample_user.id, full_address="Chilonzor 9", city="Tashkent",
        latitude=41.3111, longitude=69.2797,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=sample_user.id,
        status=OrderStatus.CONFIRMED,
        total_amount=42000,
        delivery_address_id=address.id,
        delivery_date=board_day,
        order_source="admin",
    )
    db.session.add(order)
    db.session.flush()
    # A failed delivery keeps its driver: that is what the regular pin would have misread.
    db.session.add(
        Delivery(
            order_id=order.id,
            delivery_person_id=delivery_driver.id,
            status=DeliveryStatus.FAILED,
            failed_delivery_reason="customer_unavailable",
            delivery_attempts=1,
            scheduled_date=datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc),
            scheduled_time_slot="09:00-12:00",
        )
    )
    db.session.commit()

    resp = client.get(f"/api/v1/admin/dispatch/snapshot?date={board_day.isoformat()}", headers=admin_claim_headers)
    assert resp.status_code == 200, resp.get_json()
    entry = next(o for o in resp.get_json()["data"]["orders"] if o["order_id"] == order.id)

    assert entry["awaiting_new_date"] is True
    assert entry["driver_id"] == delivery_driver.id
    assert _declared_key_set(OPERATIONS_MAP_TEST, "ORDER_ENTRY_KEYS") == set(entry), _explain(
        OPERATIONS_MAP_TEST, "ORDER_ENTRY_KEYS"
    )


# --------------------------------------------------------------------------- #
# Sales-agent pay (spec §8.1): the seed against its producers and its pages
# --------------------------------------------------------------------------- #

_PAY_VISITS_KEYS = ("visits.plan_vs_fact.counted", "visits.plan_vs_fact.not_counted_day")
# A static key the page names. Template-literal keys (`pay.kind.${kind}`) are the families below.
_PAY_LITERAL_KEY = re.compile(r"'sales_agents:(pay\.[a-z0-9_.]+|visits\.plan_vs_fact\.(?:counted|not_counted_day))'")
# `t('sales_agents:pay.x', 'Default')` or `t('sales_agents:pay.x', { defaultValue: "Default", … })`.
_PAY_T_DEFAULT = re.compile(
    r"t\(\s*'sales_agents:(pay\.[a-z0-9_.]+|visits\.plan_vs_fact\.(?:counted|not_counted_day))'\s*,\s*"
    r"(?:\{\s*defaultValue:\s*)?(['\"])((?:(?!\2).)*)\2"
)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
# A literal raise site: `error_code="SALES_PAY_X"` (spacing around `=` and either quote allowed).
_RAISED_CODE = re.compile(r"""error_code\s*=\s*["']([A-Z][A-Z0-9_]+)["']""")


def _raised_codes(paths) -> set:
    """Every error code a literal raise site in `paths` names."""
    codes = set()
    for path in paths:
        codes |= set(_RAISED_CODE.findall(path.read_text(encoding="utf-8")))
    return codes


def _pay_sources():
    """Every admin_ui source that renders a `sales_agents:pay.*` string."""
    return [
        SALES_COMPENSATION_PAGE,
        PENALTY_PROPOSALS_PAGE,
        SALES_AGENTS_PAGE,
        VISITS_PAGE,
        TRYOUTS_PAGE,
        *sorted(PAY_COMPONENTS_DIR.glob("*.js")),
        *sorted(PAY_COMPONENTS_DIR.glob("*.jsx")),
    ]


def _pay_families():
    """`pay.<family>.<value>` -> the producer's tuple (§8.1's "Pinned to" column)."""
    from business_app.services.sales.pay_bonus_service import SALES_PAY_REVIEW_FLAGS
    from business_app.services.sales.pay_statement_service import NOT_COUNTED_REASONS
    from shared.staff_constants import (
        SALES_PAY_ADJUSTMENT_SOURCES,
        SALES_PAY_BONUS_RULES,
        SALES_PAY_DAY_STATUSES,
        SALES_PAY_DIFFERENCE_CAUSES,
        SALES_PAY_GATE_RULES,
        SALES_PAY_LEDGER_KINDS,
        SALES_PAY_OUTLET_CHECK_STATUSES,
        SALES_PAY_PENALTY_ORIGINS,
        SALES_PAY_PENALTY_STATUSES,
        SALES_PAY_PERIOD_ACTIONS,
        SALES_PAY_PERIOD_STATUSES,
        SALES_PAY_RATE_MODES,
        SALES_PAY_REVERSAL_CAUSES,
        SALES_PAY_SELF_DECISION_ACTIONS,
        SALES_PAY_SELF_DECISION_INPUTS,
    )

    return {
        "status": set(SALES_PAY_PERIOD_STATUSES),
        # `sync_now` is the open month's label for `recalculate` (§6.2), not a period action.
        "action": set(SALES_PAY_PERIOD_ACTIONS) | {"sync_now"},
        "kind": set(SALES_PAY_LEDGER_KINDS),
        "cause": set(SALES_PAY_REVERSAL_CAUSES) | set(SALES_PAY_DIFFERENCE_CAUSES),
        "day_status": set(SALES_PAY_DAY_STATUSES),
        "not_counted": set(NOT_COUNTED_REASONS),
        "penalty_status": set(SALES_PAY_PENALTY_STATUSES),
        "penalty_origin": set(SALES_PAY_PENALTY_ORIGINS),
        "adjustment_source": set(SALES_PAY_ADJUSTMENT_SOURCES),
        "rate_mode": set(SALES_PAY_RATE_MODES),
        "bonus_rule": set(SALES_PAY_BONUS_RULES),
        "gate_rule": set(SALES_PAY_GATE_RULES),
        "check_status": set(SALES_PAY_OUTLET_CHECK_STATUSES),
        "self_decided.input": set(SALES_PAY_SELF_DECISION_INPUTS),
        "self_decided.action": set(SALES_PAY_SELF_DECISION_ACTIONS),
        "flag": set(SALES_PAY_REVIEW_FLAGS),
        # A8 `days[].weekday` is Python's `date.weekday()`, Monday = 0.
        "weekday": {str(day) for day in range(7)},
    }


def test_every_pay_vocabulary_is_seeded_trilingual_and_nothing_else():
    """§8.1: the pay pages build a label key from every published value
    (``t(`sales_agents:pay.kind.${line.kind}`, line.kind)``), so each family must hold exactly one
    row per value of its producer's tuple, in every language. A value added on the backend would
    reach an admin as its raw name; a retired one would leave a row nobody can see. vitest never
    notices either: it mocks i18next and reads the raw value as the default.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    for language, block in UI_SALES_TRANSLATIONS.items():
        for family, values in _pay_families().items():
            prefix = f"pay.{family}."
            seeded = {key[len(prefix):] for key in block if key.startswith(prefix) and "." not in key[len(prefix):]}
            assert seeded == values, (
                f"\nThe {language} block of scripts/seed_ui_sales_translations.py no longer carries exactly "
                f"one pay.{family}.* row per value of its producer.\n"
                f"missing: {sorted(values - seeded)}\nextra:   {sorted(seeded - values)}"
            )
            assert all(block[f"{prefix}{value}"].strip() for value in values), f"empty {language} row in pay.{family}.*"


def test_pay_handled_codes_are_raised_codes_each_with_its_sentence():
    """§6.4 (review SSOT I12): `PAY_HANDLED_CODES` names the refusals a pay modal explains itself,
    so every name must be a code the pay services really raise, or a modal waits for a refusal
    that never comes while the real one is toasted. Each needs its `pay.error.*` sentence in
    three languages; `sales_outlet_self_approval` is the Tryouts page's one extra (§6.6),
    `sales_pay_sync_incomplete_concurrent` is SYNC_INCOMPLETE's `details.reason: "concurrent"`
    sentence (final-review M3), and `tier_position` is the fragment a plan refusal appends when
    `details.tier` names a tier (D-BANDS, §6.2), not a code.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    codes = _declared_array(SALES_PAY_SERVICE, "PAY_HANDLED_CODES")
    sources = [*sorted((REPO_ROOT / "business_app/services/sales").glob("pay_*.py")), REPO_ROOT / "business_app/utils/local_windows.py"]
    raised = _raised_codes(sources)

    assert len(codes) == len(set(codes)) == 18, codes
    assert all(code.startswith("SALES_PAY_") for code in codes), codes
    assert set(codes) <= raised, (
        f"PAY_HANDLED_CODES names codes no pay service raises: {sorted(set(codes) - raised)}. "
        "A modal would wait for a refusal that never comes."
    )
    expected = {code.lower() for code in codes} | {
        "sales_outlet_self_approval",
        "sales_pay_sync_incomplete_concurrent",
        "tier_position",
    }
    for language, block in UI_SALES_TRANSLATIONS.items():
        seeded = {key[len("pay.error."):] for key in block if key.startswith("pay.error.")}
        assert seeded == expected, (
            f"{language}: missing {sorted(expected - seeded)}, extra {sorted(seeded - expected)}"
        )


def test_every_pay_string_the_admin_ui_renders_is_seeded_as_the_page_says_it():
    """The seed docstring's rule, in code, for the pay pages: once a row exists its text wins in
    every language, so the English row must BE the inline default, and every static key a page
    names must exist in all three languages. The placeholders must agree across languages too:
    a `{{month}}` dropped from one translation ships the literal braces to that admin.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    en = UI_SALES_TRANSLATIONS["en"]
    missing, mismatched = [], []
    for path in _pay_sources():
        source = path.read_text(encoding="utf-8")
        name = path.relative_to(REPO_ROOT)
        for key in sorted(set(_PAY_LITERAL_KEY.findall(source))):
            for language in ("en", "uz", "ru"):
                if not UI_SALES_TRANSLATIONS[language].get(key, "").strip():
                    missing.append(f"{name}: {key} [{language}]")
        for key, _quote, default in _PAY_T_DEFAULT.findall(source):
            if key in en and en[key] != default:
                mismatched.append(f"{name}: {key}: page {default!r} != seed {en[key]!r}")
    assert not missing, "unseeded pay keys:\n" + "\n".join(missing)
    assert not mismatched, "inline defaults that differ from the English seed:\n" + "\n".join(mismatched)

    for key in (key for key in en if key.startswith("pay.") or key in _PAY_VISITS_KEYS):
        tokens = {
            language: frozenset(_PLACEHOLDER.findall(UI_SALES_TRANSLATIONS[language][key])) for language in ("en", "uz", "ru")
        }
        assert len(set(tokens.values())) == 1, f"{key!r} carries different placeholders per language: {tokens}"


def test_the_drill_down_reads_the_published_follows_money_and_names_no_cause():
    """Ruling T14-R1 (final-review I3): which order rows carry the `pay.hint.settled_final` hint is
    the backend's answer, A9 `events[].follows_money` (`pay_rules.follows_money`, pinned through A9
    in tests/integration/test_sales_pay_statement_api.py). `PayLinesTable.jsx` reads the field on
    each event and spells no difference cause, so a cause added to `SALES_PAY_DIFFERENCE_CAUSES`
    reaches the hint without a UI edit; every PayLinesTable event fixture carries the field, as A9
    sends it.
    """
    from shared.staff_constants import SALES_PAY_DIFFERENCE_CAUSES

    source = (PAY_COMPONENTS_DIR / "PayLinesTable.jsx").read_text(encoding="utf-8")
    assert "event.follows_money" in source
    assert "SETTLED_CAUSES" not in source
    spelled = [cause for cause in SALES_PAY_DIFFERENCE_CAUSES if cause in source]
    assert not spelled, f"PayLinesTable.jsx decides by cause again: {spelled}"

    fixtures = (REPO_ROOT / "admin_ui/src/__tests__/components/sales/pay/PayLinesTable.test.jsx").read_text(
        encoding="utf-8"
    )
    assert "follows_money: false, occurred_at" in fixtures, "the base A9 event fixture lacks follows_money"


# V13's Task 5 rows: spec §8.1's D-BANDS table, its D-Q3 / OQ-B3 rows, and the rows the tier
# editor and tables keep drawing. And §8.1's "Deleted seed rows (v5)".
TIER_COPY_KEYS = (
    "pay.form.default_tiers",
    "pay.form.product_tiers",
    "pay.form.from_unit",
    "pay.form.add_tier",
    "pay.form.add_product",
    "pay.form.rate_mode",
    "pay.form.rate_value",
    "pay.formula.product_units",
    "pay.formula.default_tiers_tag",
    "pay.formula.tier_per_unit",
    "pay.formula.tier_percent",
    "pay.formula.tier_scaled",
    "pay.formula.next_tier",
    "pay.formula.late_product",
    "pay.formula.money_line",
    "pay.formula.units_line",
    "pay.formula.reversal",
    "pay.lines.tier_shift",
    "pay.lines.rate_per_unit",
    "pay.hint.tier_shift",
    "pay.hint.tiers",
    "pay.hint.settled_final",
    "pay.col.tier",
    "pay.col.units",
    "pay.col.rate",
    "pay.col.share",
    "pay.col.share_before",
    "pay.error.tier_position",
    "pay.empty.plans",
)
RETIRED_PAY_KEYS = (
    "pay.cause.plan_changed",
    "pay.formula.proportional",
    "pay.form.default_rate",
    "pay.form.product_rates",
    "pay.form.add_rate",
)


def test_the_tier_copy_is_rendered_and_seeded_and_the_retired_rows_are_gone():
    """§8.1 (D-BANDS, OQ-B3): every tier row is drawn by a pay screen and seeded in three
    languages (that its English row is the inline default is the generic test above). The rows v5
    retires are in no language block and no admin_ui file, so no screen asks for a row nobody
    seeds any more.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    rendered = set()
    for path in _pay_sources():
        rendered |= set(_PAY_LITERAL_KEY.findall(path.read_text(encoding="utf-8")))
    assert set(TIER_COPY_KEYS) <= rendered, f"tier rows no pay screen draws: {sorted(set(TIER_COPY_KEYS) - rendered)}"

    for language, block in UI_SALES_TRANSLATIONS.items():
        unseeded = [key for key in TIER_COPY_KEYS if not block.get(key, "").strip()]
        assert not unseeded, f"{language}: unseeded tier rows {unseeded}"
        still = sorted(set(RETIRED_PAY_KEYS) & set(block))
        assert not still, f"{language}: retired rows still seeded {still}"

    named = []
    for path in ADMIN_UI_SRC.rglob("*"):
        if path.suffix not in {".js", ".jsx"}:
            continue
        text = path.read_text(encoding="utf-8")
        named += [f"{path.relative_to(REPO_ROOT)}: {key}" for key in RETIRED_PAY_KEYS if key in text]
    assert not named, "retired pay rows still named in admin_ui:\n" + "\n".join(named)


# ---- C14 order approvals and the manager proposals page ----------------------------------------

# Where the codes the approval queue explains are raised (spec §8.1): the queue's own service, the
# self-decision guard, the reason validator and the order's inventory confirmation.
ORDER_APPROVAL_RAISE_SITES = (
    "business_app/services/sales/agent_order_approval_service.py",
    "business_app/services/sales/pay_rules.py",
    "business_app/services/order_service.py",
    "business_app/services/inventory_service.py",
)

# The page-owned copy families, and every rendered UI row of the two new nav children and the
# Orders tag. Template-literal keys (`order_approvals.status.${…}`, `.error.${…}`) are pinned to
# their backend vocabularies by the two tests below instead.
_SALES_COPY_KEY = r"(?:order_approvals|pay\.proposals)\.[A-Za-z0-9_.]+|pay\.empty\.proposals"
C14_UI_ROWS = {
    "components/layout/AdminLayout.js": ("ui.nav.sales_penalty_proposals", "ui.nav.sales_order_approvals"),
    "pages/Orders.js": ("ui.orders.awaiting_staff_approval", "ui.orders.open_order_approvals"),
}


def _sales_copy_calls() -> dict:
    """key -> every inline English default the admin UI passes for it (a set, so drift shows)."""
    from tests.unit.test_place_group_translation_seeds import _JS_STRING, _unquote_js

    call = re.compile(
        r"""(['"])sales_agents:({k})\1\s*,\s*({s})""".format(k=_SALES_COPY_KEY, s=_JS_STRING), re.S
    )
    found = {}
    for path in ADMIN_UI_SRC.rglob("*"):
        if path.suffix not in {".js", ".jsx"} or "__tests__" in path.parts or ".test." in path.name:
            continue
        for _, key, literal in call.findall(path.read_text(encoding="utf-8")):
            found.setdefault(key, set()).add(_unquote_js(literal))
    return found


def test_order_approval_status_labels_are_the_backends_own_vocabulary():
    """C14: one `order_approvals.status.*` row per `AGENT_ORDER_APPROVAL_STATUSES` member.

    OA1 publishes `statuses` and the queue builds each label key from the published value, so a
    fifth status would reach a manager as its raw name in every language, and a retired one
    would leave a dead row. Same contract as the exception-type labels above.
    """
    from business_app.models.sales_visits import AGENT_ORDER_APPROVAL_STATUSES
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    expected = {f"order_approvals.status.{status}" for status in AGENT_ORDER_APPROVAL_STATUSES}
    for language, block in UI_SALES_TRANSLATIONS.items():
        seeded = {key for key in block if key.startswith("order_approvals.status.")}
        assert seeded == expected, (
            f"\nThe `{language}` block of scripts/seed_ui_sales_translations.py no longer carries exactly "
            f"one order_approvals.status.* row per AGENT_ORDER_APPROVAL_STATUSES.\n"
            f"missing: {sorted(expected - seeded)}\nextra:   {sorted(seeded - expected)}"
        )
        assert all(block[key].strip() for key in seeded), f"empty {language} approval status label"
    # ...and the page keys off the published value rather than a map of its own.
    assert "sales_agents:order_approvals.status.${" in SALES_ORDER_APPROVALS_PAGE.read_text(encoding="utf-8")


def test_order_approval_handled_codes_are_raised_and_explained():
    """C14: every code the queue explains inline is one the backend raises, and has its copy.

    `ORDER_APPROVAL_HANDLED_CODES` switches off api.js's toast for its codes. A listed code no
    backend path raises is dead weight; a listed code with no `order_approvals.error.*` row would
    show a ru/uz manager the backend's English sentence; a row for an unlisted code is never read.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    handled = _declared_array(SALES_SERVICE, "ORDER_APPROVAL_HANDLED_CODES")
    assert len(handled) == len(set(handled)), handled
    raised = _raised_codes(REPO_ROOT / relative for relative in ORDER_APPROVAL_RAISE_SITES)
    assert set(handled) <= raised, f"handled but raised nowhere: {sorted(set(handled) - raised)}"

    expected = {f"order_approvals.error.{code.lower()}" for code in handled}
    for language, block in UI_SALES_TRANSLATIONS.items():
        seeded = {key for key in block if key.startswith("order_approvals.error.")}
        assert seeded == expected, (language, sorted(expected - seeded), sorted(seeded - expected))
        assert all(block[key].strip() for key in seeded), f"empty {language} refusal copy"


def test_outlet_plan_handled_codes_are_raised_and_explained_in_the_page_words():
    """Final-review I1: `OUTLET_PLAN_HANDLED_CODES` switches off api.js's toast for the outlet plan
    writes, so each code must be one the guard raises (`pay_rules.check_self_decision`, called by
    `OutletService._guard_plan_change`) and must have its `outlets.error.*` sentence in three
    languages whose English is the Outlets page's inline default, or a ru/uz manager reads the
    backend's English pay wording, or nothing at all.
    """
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS

    handled = _declared_array(SALES_SERVICE, "OUTLET_PLAN_HANDLED_CODES")
    assert handled == ["SALES_PAY_SELF_DECISION"], handled
    raised = _raised_codes([REPO_ROOT / "business_app/services/sales/pay_rules.py"])
    assert set(handled) <= raised, f"handled but raised nowhere: {sorted(set(handled) - raised)}"

    page = OUTLETS_PAGE.read_text(encoding="utf-8")
    for code in handled:
        key = f"outlets.error.{code.lower()}"
        for language in ("en", "uz", "ru"):
            assert UI_SALES_TRANSLATIONS[language].get(key, "").strip(), f"{key} [{language}]"
        assert f"t('sales_agents:{key}', '{UI_SALES_TRANSLATIONS['en'][key]}')" in page, key


def test_the_queue_and_proposal_fixtures_are_the_backends_pinned_row_shapes():
    """The two Vitest row fixtures carry exactly the keys the routes publish.

    Both backend key sets are pinned against the live routes (T-HOLD-11 for `ApprovalRow`, the M1
    tests for `ProposalRow`). Holding the fixtures to those same sets means a key a page reads but
    the route never sends fails here, not in front of a manager.
    """
    from tests.integration.test_sales_agent_order_hold_api import APPROVAL_ROW_KEYS
    from tests.integration.test_sales_pay_penalties_api import PROPOSAL_KEYS

    assert _declared_key_set(SALES_ORDER_APPROVALS_TEST, "APPROVAL_ROW_KEYS") == APPROVAL_ROW_KEYS
    assert _declared_key_set(PENALTY_PROPOSALS_TEST, "PROPOSAL_ROW_KEYS") == PROPOSAL_KEYS


def test_the_queue_and_proposal_copy_is_seeded_as_the_pages_say_it():
    """Every `order_approvals.*` / `pay.proposals.*` call site: its English default is the seeded
    `en` row byte for byte, and uz and ru exist with the same `{{tokens}}`."""
    from scripts.seed_ui_sales_translations import UI_SALES_TRANSLATIONS
    from tests.unit.test_place_group_translation_seeds import _i18next_placeholders

    calls = _sales_copy_calls()
    assert {
        "order_approvals.title", "order_approvals.hint", "order_approvals.confirm.approve.title",
        "pay.proposals.title", "pay.proposals.propose", "pay.proposals.sent", "pay.empty.proposals",
    } <= set(calls), sorted(calls)
    for key, defaults in sorted(calls.items()):
        english = UI_SALES_TRANSLATIONS["en"].get(key)
        assert defaults == {english}, (key, defaults, english)
        for language in ("uz", "ru"):
            value = UI_SALES_TRANSLATIONS[language].get(key)
            assert value and value.strip(), (key, language)
            assert _i18next_placeholders(value) == _i18next_placeholders(english), (key, language)


def test_the_c14_nav_and_orders_rows_are_seeded_as_the_ui_says_them():
    """The two nav children and the Orders tag and link: shared `ui` rows, trilingual, and the
    seeded `en` is the call site's fallback byte for byte."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
    from tests.unit.test_place_group_translation_seeds import _JS_PAIR, _JS_STRING, _unquote_js

    for relative, keys in C14_UI_ROWS.items():
        defaults = {}
        for _, key, expr in _JS_PAIR.findall((ADMIN_UI_SRC / relative).read_text(encoding="utf-8")):
            defaults.setdefault(key, set()).add("".join(_unquote_js(lit) for lit in re.findall(_JS_STRING, expr, re.S)))
        for key in keys:
            row = BACKEND_TRANSLATIONS.get(key)
            assert row is not None, f"{key} is not seeded in scripts/seed_backend_translations.py"
            assert _category_for(key) == "ui", key
            assert defaults.get(key) == {row["en"]}, (key, relative, defaults.get(key), row["en"])
            for language in ("uz", "ru"):
                assert isinstance(row[language], str) and row[language].strip(), (key, language)
