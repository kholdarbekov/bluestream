"""/api/v1/staff/sales/* driven with the payloads the staff bot sends."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.api.staff_sales import AGENT_EDITABLE_OUTLET_FIELDS
from business_app.models.order import Order, OrderItem, OrderStatusHistory
from business_app.models.payment import Payment
from business_app.models.sales import OUTLET_STAGES, Outlet, OutletContact, SalesAgentProfile
from business_app.models.user import User, UserAddress
from business_app.services.sales import notifications
from shared.enums import EntitySubtype, OrderStatus, PaymentMethod, PaymentStatus
from tests.unit.test_outlet_dedupe import FAR, NEAR, PIN, _customer
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.integration

OUTLETS = "/api/v1/staff/sales/outlets"
BOT_PAYLOAD = {
    "name": "Bahor market",
    "outlet_type": "grocery_store",
    "contact": {"name": "Olim aka", "phone": "+998901112266", "role": "owner"},
    "latitude": PIN[0],
    "longitude": PIN[1],
    "address_text": "Chilonzor 5-kvartal, 12",
    "district": "chilanzar",
    "class": "B",
    "notes": "Corner shop",
}


def _create(client, headers, payload=BOT_PAYLOAD):
    resp = client.post(OUTLETS, json=payload, headers=headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["data"]["outlet"]


def test_create_list_dedupe_and_card(client, sales_agent_auth_headers, sales_agent_user, db):
    outlet = _create(client, sales_agent_auth_headers)
    assert outlet["stage"] == "prospect" and outlet["class"] == "B"
    assert outlet["assigned_agent_user_id"] == sales_agent_user.id

    listed = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": "prospects"})
    assert listed.status_code == 200, listed.get_data(as_text=True)
    assert [row["id"] for row in listed.get_json()["data"]["items"]] == [outlet["id"]]

    dedupe = client.get(
        f"{OUTLETS}/dedupe",
        headers=sales_agent_auth_headers,
        query_string={"name": "bahor", "latitude": NEAR[0], "longitude": NEAR[1]},
    )
    assert dedupe.status_code == 200
    candidates = dedupe.get_json()["data"]["candidates"]
    assert [c["outlet_id"] for c in candidates] == [outlet["id"]] and candidates[0]["reason"] == "name_nearby"

    dup = client.post(
        OUTLETS,
        json={**BOT_PAYLOAD, "latitude": NEAR[0], "longitude": NEAR[1]},
        headers=sales_agent_auth_headers,
    )
    assert dup.status_code == 409 and "SALES_OUTLET_DUPLICATE" in dup.get_data(as_text=True)
    forced = _create(
        client,
        sales_agent_auth_headers,
        {**BOT_PAYLOAD, "latitude": NEAR[0], "longitude": NEAR[1], "force": True},
    )
    assert forced["dedupe_candidates"][0]["outlet_id"] == outlet["id"]

    card = client.get(f"{OUTLETS}/{outlet['id']}", headers=sales_agent_auth_headers)
    assert card.status_code == 200
    body = card.get_json()["data"]["outlet"]
    # Null, not 0: this prospect has no customer account and no address row yet, so the bot has a
    # single field to gate the money lines on instead of re-deriving "is this applicable" itself.
    assert body["open_receivable"] is None and body["bottle_balance"] is None and body["last_orders"] == []
    assert body["contacts"][0]["phone"] == "+998901112266"


def test_agent_put_cannot_reach_the_admin_only_fields(client, sales_agent_auth_headers, db):
    """`UpdateOutletPayload` is shared with the ADMIN route, so every field an admin may edit
    parsed cleanly out of an AGENT's PUT and was written: an agent could grant an outlet its own
    credit terms, re-district it out of a colleague's list, or clear the status warning an
    operator had raised. The schema stays shared -- one shape for one resource -- and the ROUTE
    decides who may write what.
    """
    outlet = _create(client, sales_agent_auth_headers)
    assert outlet["payment_terms"] == "cash" and outlet["district"] == "chilanzar"

    response = client.put(
        f"{OUTLETS}/{outlet['id']}",
        json={
            "notes": "Visited Tuesday",
            "payment_terms": "business_account",
            "district": "yunusabad",
            "tax_id": "302123456",
            "legal_form": "yatt",
            "cadence_days_override": 3,
            "status_warning": "cleared",
        },
        headers=sales_agent_auth_headers,
    )

    # 200, not 403: the agent-editable half of the payload is applied, the rest is simply not
    # theirs to send. An error here would block the notes edit the visit was actually about.
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]["outlet"]
    assert body["notes"] == "Visited Tuesday"  # proof the write really happened
    assert body["payment_terms"] == "cash" and body["district"] == "chilanzar"
    assert body["tax_id"] is None and body["legal_form"] is None
    assert body["cadence_days_override"] is None and body["status_warning"] is None

    fresh = Outlet.query.get(outlet["id"])
    assert fresh.payment_terms == "cash" and fresh.district == "chilanzar"


def test_request_activation_then_operator_approves(client, sales_agent_auth_headers, operator_auth_headers, db):
    outlet = _create(client, sales_agent_auth_headers)

    requested = client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)
    assert requested.status_code == 200, requested.get_data(as_text=True)
    assert requested.get_json()["data"]["outlet"]["stage"] == "activation_requested"

    forbidden = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=sales_agent_auth_headers)
    assert forbidden.status_code == 403 and "STAFF_NO_ROLE" in forbidden.get_data(as_text=True)

    queue = client.get("/api/v1/staff/sales/activation-requests", headers=operator_auth_headers)
    assert queue.status_code == 200, queue.get_data(as_text=True)
    assert [row["id"] for row in queue.get_json()["data"]["items"]] == [outlet["id"]]

    approved = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=operator_auth_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["outlet"]["stage"] == "active"
    assert Outlet.query.get(outlet["id"]).user_id is not None


def test_operator_rejects_with_reason(client, sales_agent_auth_headers, operator_auth_headers, db):
    outlet = _create(client, sales_agent_auth_headers)
    client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)
    rejected = client.post(
        f"{OUTLETS}/{outlet['id']}/reject", json={"reason": "Duplicate of #3"}, headers=operator_auth_headers
    )
    assert rejected.status_code == 200, rejected.get_data(as_text=True)
    assert rejected.get_json()["data"]["outlet"]["stage"] == "prospect"
    assert rejected.get_json()["data"]["outlet"]["rejected_reason"] == "Duplicate of #3"


def test_ownership_and_role_gates(client, sales_agent_auth_headers, sales_agent_user, driver_auth_headers, db, app):
    from flask_jwt_extended import create_access_token

    from tests.unit.test_sales_agent_role import make_sales_agent_user

    outlet = _create(client, sales_agent_auth_headers)
    other = make_sales_agent_user(db, phone="+998901234578", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    db.session.commit()
    with app.app_context():
        other_headers = {"Authorization": f"Bearer {create_access_token(identity=str(other.id))}"}

    resp = client.get(f"{OUTLETS}/{outlet['id']}", headers=other_headers)
    assert resp.status_code == 403 and "SALES_OUTLET_NOT_ASSIGNED" in resp.get_data(as_text=True)

    driver = client.get(OUTLETS, headers=driver_auth_headers)
    assert driver.status_code == 403

    profile = SalesAgentProfile.query.filter_by(user_id=sales_agent_user.id).one()
    profile.is_active = False
    db.session.commit()
    off = client.get(OUTLETS, headers=sales_agent_auth_headers)
    assert off.status_code == 403 and "STAFF_ACCOUNT_DEACTIVATED" in off.get_data(as_text=True)


def test_a_geocoders_district_string_is_canonicalised_not_refused(client, sales_agent_auth_headers, db):
    """The staff bot posts what the reverse-geocoder said, VERBATIM.

    That is never a canonical key — live Nominatim answers "Yashnobod Tumani",
    "Mirzo Ulug\u2018bek Tumani" (U+2018, not the apostrophe our own constants use),
    "Shayhontohur Tumani". This endpoint used to compare that against `TASHKENT_DISTRICTS` keys
    and answer `SALES_DISTRICT_INVALID`, which failed the whole field walk-in at the final Confirm
    tap, on a field the agent never typed. The mapping belongs here and only here — the bot posts
    the raw string.
    """
    for raw, expected in (
        ("Chilanzar District", "chilanzar"),
        ("Chilonzor tumani", "chilanzar"),
        ("Юнусабадский район", "yunusabad"),
        ("Yashnobod Tumani", "yashnobod"),
        ("Mirzo Ulug\u2018bek Tumani", "mirzo_ulugbek"),
        ("Shayhontohur Tumani", "shaykhontohur"),
    ):
        outlet = _create(
            client,
            sales_agent_auth_headers,
            {**BOT_PAYLOAD, "district": raw, "force": True},
        )
        assert outlet["district"] == expected, raw


def test_a_district_that_names_nothing_is_stored_as_null_not_refused(client, sales_agent_auth_headers, db):
    """The walk-in must survive a hint we cannot parse.

    On this path the district is a machine-derived, OPTIONAL classification: nobody types it, and
    the outlet's real location is the pin, already validated against the delivery polygon. Refusing
    a completed field visit at the final Confirm tap — over a string the agent never entered and
    cannot correct — loses a customer; a NULL an admin fixes in the Outlets drawer costs a click.
    That repair is real and pinned: see
    tests/integration/test_admin_sales_outlets_api.py::test_district_is_editable_from_the_drawer_and_a_typo_is_refused.
    A geocoder that starts answering in a new shape must degrade, not block the road.

    Note this is specific to the OUTLET WRITE path. `bulk_assign_by_district` and
    `staff_service._validate_districts` still raise SALES_DISTRICT_INVALID, because there a HUMAN
    typed the district and a typo has to be told.
    """
    outlet = _create(
        client,
        sales_agent_auth_headers,
        {**BOT_PAYLOAD, "district": "Nowhere District", "force": True},
    )
    assert outlet["district"] is None


def test_a_typed_district_is_still_refused_where_a_human_typed_it(client, admin_auth_headers, db):
    """The ratchet on the ruling above: relaxing the outlet write path must not relax the paths
    where the district is an ADMIN's own input. Covered end to end in
    tests/integration/test_sales_agent_admin_api.py; asserted here so the two rulings stay visibly
    different rather than drifting into one."""
    from business_app.services.sales.outlet_service import OutletService
    from business_app.utils.exceptions import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        OutletService.bulk_assign_by_district("Nowhere District", 1, 1)
    assert excinfo.value.error_code == "SALES_DISTRICT_INVALID"


def test_an_inverted_standing_delivery_window_is_refused(client, sales_agent_auth_headers, db):
    """L50: `update` parsed both edges and checked neither against the other.

    An 18:00-09:00 store is storable nonsense, and ruling 34 then offers that window as the
    DEFAULT on every order the agent places there -- where `parse_and_validate_schedule` refuses
    it, silently, so the store's own hours are dropped on every single order instead. The rule
    is `validate_schedule`'s (delivery_window.py:106), asked here with no date so only the
    window-order clause can speak; it is never restated.
    """
    outlet = _create(client, sales_agent_auth_headers)
    ok = client.put(
        f"{OUTLETS}/{outlet['id']}",
        json={"delivery_window_start": "09:00", "delivery_window_end": "12:00"},
        headers=sales_agent_auth_headers,
    )
    assert ok.status_code == 200, ok.get_data(as_text=True)

    inverted = client.put(
        f"{OUTLETS}/{outlet['id']}",
        json={"delivery_window_start": "18:00", "delivery_window_end": "09:00"},
        headers=sales_agent_auth_headers,
    )

    assert inverted.status_code == 400, inverted.get_data(as_text=True)
    assert inverted.get_json()["error_code"] == "SALES_WINDOW_INVALID"

    # One edge at a time is the same defect: the RESULTING pair is what has to hold. And NOTHING
    # of a refused edit may reach the row -- not the window, not the fields that travelled with
    # it. `update` assigns name/channel/class/notes/district/tax_id/payment_terms BEFORE it ever
    # looks at the window, so a raise at the window left those assignments sitting on the session
    # object, and the next successful write committed them: an edit the server said 400 to,
    # applied anyway, minutes later, under a different request.
    half = client.put(
        f"{OUTLETS}/{outlet['id']}",
        json={"delivery_window_start": "18:00", "notes": "evenings only"},
        headers=sales_agent_auth_headers,
    )
    assert half.status_code == 400, half.get_data(as_text=True)
    assert half.get_json()["error_code"] == "SALES_WINDOW_INVALID"

    # The next legitimate edit is what makes a dirty session visible: it commits.
    renamed = client.put(
        f"{OUTLETS}/{outlet['id']}",
        json={"name": "Bahor market"},
        headers=sales_agent_auth_headers,
    )
    assert renamed.status_code == 200, renamed.get_data(as_text=True)

    fresh = Outlet.query.get(outlet["id"])
    assert fresh.name == "Bahor market"
    assert fresh.notes != "evenings only"
    assert (fresh.delivery_window_start.strftime("%H:%M"), fresh.delivery_window_end.strftime("%H:%M")) == (
        "09:00",
        "12:00",
    )


def test_nearby_orders_the_agents_own_pinned_outlets_by_distance(
    client, sales_agent_auth_headers, sales_agent_user, db
):
    """R1: *Nearby* is "what is around me", and the BACKEND answers it.

    Distance is a rule (D15): the bot prints `distance_m` and owns no haversine of its own,
    so the metres, the ORDER and the MEMBERSHIP are all asserted through HTTP. A bot-side
    sort would be green in every backend test ever written and wrong the first time an
    outlet was reassigned, marked lost, or saved without a pin.
    """
    at_pin = Outlet(
        name="Zulfiya do'kon", outlet_type="grocery_store", stage="active",
        latitude=PIN[0], longitude=PIN[1], assigned_agent_user_id=sales_agent_user.id,
    )
    close_by = Outlet(
        name="Anvar market", outlet_type="grocery_store", stage="at_risk",
        latitude=NEAR[0], longitude=NEAR[1], assigned_agent_user_id=sales_agent_user.id,
    )
    far_away = Outlet(
        name="Botir supermarket", outlet_type="grocery_store", stage="prospect",
        latitude=FAR[0], longitude=FAR[1], onboarded_by_user_id=sales_agent_user.id,
    )
    # Three outlets that must NOT appear, each for its own reason.
    lost_next_door = Outlet(
        name="Yopilgan do'kon", outlet_type="grocery_store", stage="lost",
        latitude=PIN[0], longitude=PIN[1], assigned_agent_user_id=sales_agent_user.id,
    )
    unpinned = Outlet(
        name="Pinsiz do'kon", outlet_type="grocery_store", stage="active",
        assigned_agent_user_id=sales_agent_user.id,
    )
    someone_elses = Outlet(
        name="Boshqa agent do'koni", outlet_type="grocery_store", stage="active",
        latitude=PIN[0], longitude=PIN[1],
    )
    db.session.add_all([at_pin, close_by, far_away, lost_next_door, unpinned, someone_elses])
    db.session.commit()

    listed = client.get(
        OUTLETS,
        headers=sales_agent_auth_headers,
        query_string={"scope": "nearby", "lat": PIN[0], "lng": PIN[1]},
    )

    assert listed.status_code == 200, listed.get_data(as_text=True)
    rows = listed.get_json()["data"]["items"]
    assert [row["id"] for row in rows] == [at_pin.id, close_by.id, far_away.id]
    # Whole metres, PROVED: `serialize_outlet_agent_row` rounds, and the staff bot prints
    # what it is given. A float here would be a contract change nothing else in this file
    # would notice. The pin itself is 0, not null -- "no distance" and "you are standing on
    # it" are different answers and the bot renders them differently.
    assert all(isinstance(row["distance_m"], int) for row in rows), [
        (row["id"], row["distance_m"], type(row["distance_m"]).__name__) for row in rows
    ]
    assert rows[0]["distance_m"] == 0
    assert 45 <= rows[1]["distance_m"] <= 70
    assert 3800 <= rows[2]["distance_m"] <= 4200
    # Every stage except `lost` is visible (R1) -- a prospect the agent is standing outside
    # of is exactly what this screen is for.
    assert [row["stage"] for row in rows] == ["active", "at_risk", "prospect"]
    # And it is still the ordinary agent row: the bot uses ONE renderer for all four scopes.
    assert rows[0]["has_open_visit"] is False and rows[0]["open_visit_id"] is None
    assert rows[0]["name"] == "Zulfiya do'kon"


def test_every_other_scope_publishes_a_null_distance(client, sales_agent_auth_headers, sales_agent_user, db):
    """The KEY is always there; only the VALUE is scope-dependent.

    A row renderer that has to ask "which scope drew me" before reading a field is a second
    expression of the scope, in the client, where nothing tests it.
    """
    # One outlet per scope, because a scope that returns NOTHING asserts nothing: `due` needs a
    # due date and `prospects` needs a prospect stage, and `all` carries both.
    db.session.add(Outlet(
        name="Zulfiya do'kon", outlet_type="grocery_store", stage="active",
        next_visit_due_at=datetime.now(timezone.utc) - timedelta(days=1),
        latitude=PIN[0], longitude=PIN[1], assigned_agent_user_id=sales_agent_user.id,
    ))
    db.session.add(Outlet(
        name="Yangi do'kon", outlet_type="grocery_store", stage="prospect",
        latitude=PIN[0], longitude=PIN[1], assigned_agent_user_id=sales_agent_user.id,
    ))
    db.session.commit()

    for scope in ("all", "due", "prospects"):
        listed = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string={"scope": scope})
        assert listed.status_code == 200, listed.get_data(as_text=True)
        rows = listed.get_json()["data"]["items"]
        assert rows, scope
        for row in rows:
            assert row["distance_m"] is None, scope


def test_nearby_without_a_usable_pin_is_refused_with_one_code(client, sales_agent_auth_headers, db):
    """Missing, unparseable and off-the-globe are ONE refusal.

    `request.args.get(type=float)` answers None for a missing pin and for "abc" alike, and
    `calculate_distance` raises ValueError on lat=91 -- a 500 with a geopy traceback in it.
    The bot branches on `error_code`, so all three have to arrive as the same string.
    """
    for query in (
        {"scope": "nearby"},
        {"scope": "nearby", "lat": PIN[0]},
        {"scope": "nearby", "lat": "abc", "lng": PIN[1]},
        {"scope": "nearby", "lat": 91.0, "lng": PIN[1]},
        {"scope": "nearby", "lat": PIN[0], "lng": 181.0},
    ):
        refused = client.get(OUTLETS, headers=sales_agent_auth_headers, query_string=query)
        assert refused.status_code == 400, (query, refused.get_data(as_text=True))
        assert refused.get_json()["error_code"] == "SALES_NEARBY_PIN_REQUIRED", query


def test_nearby_is_capped_by_the_configured_limit(client, app, monkeypatch, sales_agent_auth_headers,
                                                  sales_agent_user, db):
    """The ceiling is a COUNT read from config, not a literal in the route (R12).

    Set to 20 in `shared/business_config.py`; proved here by moving it, because a hardcoded
    20 passes every test that only ever seeds three outlets.
    """
    for index in range(4):
        db.session.add(Outlet(
            name=f"Do'kon {index}", outlet_type="grocery_store", stage="active",
            latitude=PIN[0] + index * 0.0005, longitude=PIN[1],
            assigned_agent_user_id=sales_agent_user.id,
        ))
    db.session.commit()
    monkeypatch.setitem(app.config, "SALES_NEARBY_LIMIT", 2)

    listed = client.get(
        OUTLETS, headers=sales_agent_auth_headers,
        query_string={"scope": "nearby", "lat": PIN[0], "lng": PIN[1], "per_page": 100, "page": 3},
    )

    assert listed.status_code == 200, listed.get_data(as_text=True)
    body = listed.get_json()
    assert [row["name"] for row in body["data"]["items"]] == ["Do'kon 0", "Do'kon 1"]
    # A single top-N screen: `page`/`per_page` are ignored, and the meta says so rather than
    # echoing a page the agent never asked for.
    assert body["meta"]["page"] == 1 and body["meta"]["per_page"] == 2 and body["meta"]["total"] == 2


def test_linking_a_second_shop_to_one_account_creates_a_branch_address(client, sales_agent_auth_headers, db):
    """The bot's 🔗 Link tap on branch #2, driven with the body `new_outlet.py:_payload()` sends.

    R39 (ii): one account, two outlets, two addresses. The first shop is pinned at the
    account's address and adopts it; the second is pinned ~4 km from every address the account
    has, so it gets its own. The delivery address is what makes an order on behalf land at the
    right branch (`visit_service.py:602`), so a branch with no address of its own is a branch
    in name only.
    """
    customer = _customer(db, "+998901112277", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    home = UserAddress(
        user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True
    )
    db.session.add(home)
    db.session.commit()
    home_id = home.id

    first = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": customer.id})
    second = _create(
        client,
        sales_agent_auth_headers,
        {
            **BOT_PAYLOAD,
            "name": "Bahor market, Yunusobod",
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 19-kvartal, 4",
            "link_user_id": customer.id,
        },
    )

    assert first["address_id"] == home_id
    assert second["address_id"] not in (None, home_id)
    assert (first["stage"], second["stage"]) == ("active", "active")
    assert (first["user_id"], second["user_id"]) == (customer.id, customer.id)
    branch = UserAddress.query.get(second["address_id"])
    assert (branch.user_id, branch.is_default, branch.title) == (customer.id, False, "Bahor market, Yunusobod")
    assert sorted(o.id for o in Outlet.query.filter_by(user_id=customer.id)) == sorted([first["id"], second["id"]])


def test_dedupe_labels_a_chains_own_branch_and_still_offers_the_account(
    client, sales_agent_auth_headers, db
):
    """`GET /sales/outlets/dedupe` is what `new_outlet.py:_fetch_candidates` calls before the
    confirm screen; the 409 on create carries the same rows. Registering branch #3 must show
    one 🔗 Link row (the account) and one 🏢 branch row, not two "duplicate" refusals."""
    account = _customer(db, "+998901112266", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    first = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": account.id})

    dedupe = client.get(
        f"{OUTLETS}/dedupe",
        headers=sales_agent_auth_headers,
        query_string={"name": "Bahor market, Sergeli", "phone": "+998901112266"},
    )
    assert dedupe.status_code == 200, dedupe.get_data(as_text=True)
    candidates = dedupe.get_json()["data"]["candidates"]
    assert {(c["kind"], c["reason"]) for c in candidates} == {
        ("customer", "phone"),
        ("sibling", "same_account_branch"),
    }
    assert [c["outlet_id"] for c in candidates if c["kind"] == "sibling"] == [first["id"]]

    blocked = client.post(
        OUTLETS,
        json={**BOT_PAYLOAD, "name": "Bahor market, Sergeli", "latitude": FAR[0], "longitude": FAR[1]},
        headers=sales_agent_auth_headers,
    )
    assert blocked.status_code == 409, blocked.get_data(as_text=True)
    assert blocked.get_json()["error_code"] == "SALES_OUTLET_DUPLICATE"
    # `ErrorResponse.build_error_response` (business_app/utils/error_handlers.py:64-76) emits a
    # FLAT envelope — {"error": <type>, "message", "details", "error_code"} — so `details` is a
    # top-level key, not a child of `error`. Task 3's approve tests read the same shape.
    candidates = blocked.get_json()["details"]["candidates"]
    assert {(c["kind"], c["reason"]) for c in candidates} == {
        ("customer", "phone"),
        ("sibling", "same_account_branch"),
    }


def test_linking_a_shop_the_account_already_has_at_this_pin_is_refused_as_a_duplicate(
    client, sales_agent_auth_headers, db
):
    """R39 (i): 🔗 Link skips the dedupe lookup (the agent has already chosen the account), so
    the link path carries its own same-place check. Tapping Link again for a shop that is
    already linked — the same pin, or a second visit's GPS fix ~56 m off — must answer with the
    existing outlet, not mint a second outlet and a second address row for the same place.
    `force` does reach past it (R40) — that override is pinned by the next test."""
    account = _customer(db, "+998901112288", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    db.session.add(
        UserAddress(user_id=account.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    )
    db.session.commit()
    first = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": account.id})
    outlets_before = Outlet.query.count()
    addresses_before = {a.id for a in UserAddress.query.filter_by(user_id=account.id)}

    same_pin = {**BOT_PAYLOAD, "link_user_id": account.id}
    near_pin = {**same_pin, "latitude": NEAR[0], "longitude": NEAR[1]}
    for body, distance_m in ((same_pin, 0), (near_pin, 56)):
        refused = client.post(OUTLETS, json=body, headers=sales_agent_auth_headers)

        assert refused.status_code == 409, refused.get_data(as_text=True)
        payload = refused.get_json()
        assert payload["error_code"] == "SALES_OUTLET_DUPLICATE"
        assert payload["details"]["candidates"] == [
            {
                "kind": "outlet",
                "user_id": account.id,
                "outlet_id": first["id"],
                "name": "Bahor market",
                "phone": "+998901112266",
                "distance_m": distance_m,
                "reason": "same_place",
            }
        ]
    assert Outlet.query.count() == outlets_before
    assert {a.id for a in UserAddress.query.filter_by(user_id=account.id)} == addresses_before


def test_a_forced_link_at_the_same_place_is_created_and_keeps_the_same_place_candidate(
    client, sales_agent_auth_headers, db
):
    """R40: `force` is the agent's explicit override on the Link path exactly as on the plain
    create — the bot's ➕ Create anyway re-sends `link_user_id` with `force: true`. The outlet is
    created and linked, and the same-place hit it overrode is kept in `dedupe_candidates`, the
    way the non-link path keeps its candidates, so the override stays visible to the office."""
    account = _customer(db, "+998901112288", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    db.session.add(
        UserAddress(user_id=account.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    )
    db.session.commit()
    first = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": account.id})
    outlets_before = Outlet.query.filter_by(user_id=account.id).count()

    forced = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": account.id, "force": True})

    assert (forced["stage"], forced["user_id"]) == ("active", account.id)
    assert forced["dedupe_candidates"] == [
        {
            "kind": "outlet",
            "user_id": account.id,
            "outlet_id": first["id"],
            "name": "Bahor market",
            "phone": "+998901112266",
            "distance_m": 0,
            "reason": "same_place",
        }
    ]
    assert Outlet.query.filter_by(user_id=account.id).count() == outlets_before + 1


def test_linking_at_a_free_address_of_the_account_adopts_it_not_the_default(client, sales_agent_auth_headers, db):
    """R39 (iii): the chain registered its office as the default and its shop as a second
    address. Linking the shop, pinned at that second address, adopts THAT row — no new address
    row, and not the office ~4 km away that "the default, while it is free" used to take."""
    account = _customer(db, "+998901112299", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    office = UserAddress(
        user_id=account.id, full_address="Yunusobod 19", latitude=FAR[0], longitude=FAR[1], is_default=True
    )
    shop = UserAddress(user_id=account.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1])
    db.session.add_all([office, shop])
    db.session.commit()
    shop_id = shop.id
    addresses_before = {a.id for a in UserAddress.query.filter_by(user_id=account.id)}

    outlet = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": account.id})

    assert (outlet["stage"], outlet["user_id"], outlet["address_id"]) == ("active", account.id, shop_id)
    assert {a.id for a in UserAddress.query.filter_by(user_id=account.id)} == addresses_before


def test_attach_without_a_matching_account_is_refused_by_its_own_code(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """`attach: true` is a claim about an account that exists. When the phone matches none, the
    operator hears that — instead of silently getting a brand-new account from an Attach button.
    """
    outlet = _create(client, sales_agent_auth_headers)
    client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)

    refused = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={"attach": True}, headers=operator_auth_headers)
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_ATTACH_NO_ACCOUNT"
    row = Outlet.query.get(outlet["id"])
    assert row.stage == "activation_requested" and row.user_id is None

    # And the ordinary approval on the same outlet still creates the account it always did.
    approved = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=operator_auth_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert Outlet.query.get(outlet["id"]).user_id is not None


def test_activation_requests_name_the_account_and_the_operator_attaches_the_branch(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """The operator's queue, end to end (D25 rule 3, published per R6).

    The ROW carries the account, because the bot renders its review card out of this list and
    holds no second fetch — and because a plain Approve on exactly these rows is the 409. After
    the attach, the card says whose account the money belongs to and how many branches it has.
    """
    account = _customer(db, "+998901112266", company="Chinor", subtype=EntitySubtype.GROCERY_STORE)
    address = UserAddress(
        user_id=account.id,
        full_address="Chilonzor 9",
        district="chilanzar",
        latitude=NEAR[0],
        longitude=NEAR[1],
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    db.session.add(
        Outlet(
            name="Chinor, Chilonzor 9",
            outlet_type="grocery_store",
            stage="active",
            user_id=account.id,
            address_id=address.id,
            latitude=NEAR[0],
            longitude=NEAR[1],
        )
    )
    db.session.commit()

    outlet = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "force": True})
    client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)

    queue = client.get("/api/v1/staff/sales/activation-requests", headers=operator_auth_headers)
    assert queue.status_code == 200, queue.get_data(as_text=True)
    row = queue.get_json()["data"]["items"][0]
    assert row["id"] == outlet["id"]
    assert row["account_candidate"] == {"user_id": account.id, "name": "Chinor", "outlet_count": 1}
    # Published per viewer (spec §4.12): this operator did not register the shop.
    assert row["can_approve"] is True

    refused = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=operator_auth_headers)
    assert refused.status_code == 409, refused.get_data(as_text=True)
    assert refused.get_json()["details"]["account"]["user_id"] == account.id

    attached = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={"attach": True}, headers=operator_auth_headers)
    assert attached.status_code == 200, attached.get_data(as_text=True)
    joined = Outlet.query.get(outlet["id"])
    assert joined.user_id == account.id and joined.address_id != address.id

    card = client.get(f"{OUTLETS}/{outlet['id']}", headers=sales_agent_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    body = card.get_json()["data"]["outlet"]
    assert body["account_name"] == "Chinor" and body["branch_count"] == 2
    # Branch mode is PUBLISHED, not re-derived: `is_branch` is the same answer
    # `OutletService.is_branch` gives, and it is what both renderers gate on, so the bot and the
    # admin page can never disagree about whether this shop is a branch.
    assert body["is_branch"] is True
    # The wallet is the chain's, the crate is this branch's: the card says which is which, and
    # the renderers only label what is published here.
    assert body["open_receivable_scope"] == "account"
    assert body["account_candidate"] is None  # it HAS an account now; nothing left to attach
    assert body["can_approve"] is False  # active, and the viewer is the agent who registered it


def test_a_silent_link_adopts_the_customers_free_address_at_the_pin(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """R42: the plain Approve's silent link of a customer with NO outlet is still a LINK, so it
    takes its address the way every link does -- the customer's free address at the pin (~55 m
    here) is adopted, not duplicated by a second row minted from the pin. The bot sends `{}`."""
    customer = _customer(db, "+998901112266", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    home = UserAddress(
        user_id=customer.id,
        full_address="Chilonzor 5",
        district="chilanzar",
        latitude=NEAR[0],
        longitude=NEAR[1],
        is_default=True,
    )
    db.session.add(home)
    db.session.commit()
    home_id = home.id

    outlet = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "force": True})
    client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)
    queue = client.get("/api/v1/staff/sales/activation-requests", headers=operator_auth_headers)
    assert queue.get_json()["data"]["items"][0]["account_candidate"] is None  # R24: nothing to attach

    approved = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=operator_auth_headers)

    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["outlet"]["stage"] == "active"
    row = Outlet.query.get(outlet["id"])
    assert (row.user_id, row.address_id) == (customer.id, home_id)
    assert UserAddress.query.filter_by(user_id=customer.id).count() == 1


def test_attach_links_a_customer_with_no_outlet_instead_of_refusing(
    client, sales_agent_auth_headers, operator_auth_headers, db
):
    """R24's 400 boundary, the linking side: `SALES_ATTACH_NO_ACCOUNT` is about a missing
    ACCOUNT, not a missing candidate. A same-type customer holding 0 outlets IS an account, so
    an Attach on its phone links it -- it is never refused as "no account"."""
    customer = _customer(db, "+998901112266", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    outlet = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "force": True})
    client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)

    attached = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={"attach": True}, headers=operator_auth_headers)

    assert attached.status_code == 200, attached.get_data(as_text=True)
    assert attached.get_json()["data"]["outlet"]["stage"] == "active"
    assert Outlet.query.get(outlet["id"]).user_id == customer.id


def test_attach_on_a_phone_of_another_customer_type_is_the_phone_taken_conflict(
    client, sales_agent_auth_headers, operator_auth_headers, admin_claim_headers, db
):
    """R24's 400 boundary, the refusing side: a phone held by a customer of ANOTHER type is not
    "no account" -- there is an account, it just may not own a grocery store. That is
    `_assert_linkable`'s 409 SALES_APPROVAL_PHONE_TAKEN, never the attach 400.

    The office already holds an outlet of its own, which is what makes it a would-be candidate
    at all (`account_candidate` returns None for a 0-outlet customer before it looks at the
    type). So this is where the type guard inside `account_candidate` is pinned from both
    sides it protects: no surface may offer an Attach the write always refuses, and the refusal
    must stay a quiet None -- a raise there would 409 the operator's WHOLE queue and the admin
    drawer for this outlet, not just this one row.
    """
    office = _customer(db, "+998901112266", company="Office", subtype=EntitySubtype.WORKPLACE)
    db.session.add(Outlet(name="Office canteen", outlet_type="workplace", stage="active", user_id=office.id))
    db.session.commit()
    outlet = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "force": True})
    client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)

    queue = client.get("/api/v1/staff/sales/activation-requests", headers=operator_auth_headers)
    assert queue.status_code == 200, queue.get_data(as_text=True)
    rows = queue.get_json()["data"]["items"]
    assert [row["id"] for row in rows] == [outlet["id"]]
    assert rows[0]["account_candidate"] is None
    drawer = client.get(f"/api/v1/admin/sales/outlets/{outlet['id']}", headers=admin_claim_headers)
    assert drawer.status_code == 200, drawer.get_data(as_text=True)
    assert drawer.get_json()["data"]["outlet"]["account_candidate"] is None

    # The bot's plain ✅ Approve body: refused by the type check, naming no account to join.
    approved = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={}, headers=operator_auth_headers)
    assert approved.status_code == 409, approved.get_data(as_text=True)
    assert approved.get_json()["error_code"] == "SALES_APPROVAL_PHONE_TAKEN"
    assert "account" not in (approved.get_json().get("details") or {})

    refused = client.post(f"{OUTLETS}/{outlet['id']}/approve", json={"attach": True}, headers=operator_auth_headers)

    assert refused.status_code == 409, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_APPROVAL_PHONE_TAKEN"
    row = Outlet.query.get(outlet["id"])
    assert row.stage == "activation_requested" and row.user_id is None


def _branch_history(db, user, address, *, product, quantity, amount, number, days_ago):
    """A DELIVERED order that landed at ONE branch address, plus the COD payment the
    ACCOUNT still owes for it.

    `orders` has no `delivered_at` column, so the DELIVERED status-history row is what every
    replenishment query reads as "when it landed"; `delivery_address_id` is what says WHICH
    shop it landed at. The payment is born PENDING with nothing collected -- a real COD row --
    which is what `net_open_receivable_amount` turns into "still owes".
    """
    landed = datetime.now(timezone.utc) - timedelta(days=days_ago)
    order = Order(
        user_id=user.id,
        order_number=number,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal(amount),
        total_amount=Decimal(amount),
        delivery_address_id=address.id,
        order_source="sales_agent",
        created_at=landed,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * quantity,
        )
    )
    db.session.add(
        OrderStatusHistory(
            order_id=order.id,
            old_status=OrderStatus.OUT_FOR_DELIVERY,
            new_status=OrderStatus.DELIVERED,
            changed_at=landed,
            created_at=landed,
        )
    )
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=user.id,
            amount=Decimal(amount),
            amount_collected=Decimal("0"),
            payment_method=PaymentMethod.CASH,
            status=PaymentStatus.PENDING,
        )
    )
    db.session.commit()
    return order


def test_a_branch_card_shows_its_own_history_and_the_whole_accounts_money(
    client, db, sales_agent_auth_headers, operator_auth_headers, sample_product
):
    """D25 rules 5-6 through the door the staff bot really opens.

    Two shops of one chain, built the production way: the first outlet is approved into a new
    account, the second is approved with `attach` onto that same account (R5). Afterwards the
    agent opens each card. What the shop CONSUMES is the branch's -- its own deliveries, its
    own rate, its own suggested quantity, its own last orders -- and what the chain OWES is
    the account's, identical on both cards, because there is one wallet behind both shelves.
    """
    main = _create(client, sales_agent_auth_headers)
    assert client.post(
        f"{OUTLETS}/{main['id']}/request-activation", headers=sales_agent_auth_headers
    ).status_code == 200
    approved = client.post(f"{OUTLETS}/{main['id']}/approve", json={}, headers=operator_auth_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)

    # Same chain, same contact phone, a different door 4 km away. `force` is the agent
    # answering the duplicate screen: yes, this really is another branch.
    branch = _create(
        client,
        sales_agent_auth_headers,
        {
            **BOT_PAYLOAD,
            "name": "Bahor market, Yunusobod",
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 12",
            "force": True,
        },
    )
    assert client.post(
        f"{OUTLETS}/{branch['id']}/request-activation", headers=sales_agent_auth_headers
    ).status_code == 200
    attached = client.post(
        f"{OUTLETS}/{branch['id']}/approve", json={"attach": True}, headers=operator_auth_headers
    )
    assert attached.status_code == 200, attached.get_data(as_text=True)

    main_row, branch_row = Outlet.query.get(main["id"]), Outlet.query.get(branch["id"])
    assert main_row.user_id == branch_row.user_id
    assert main_row.address_id is not None and branch_row.address_id != main_row.address_id

    account = User.query.get(main_row.user_id)
    sample_product.in_sales_stock_check = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    db.session.commit()
    _branch_history(
        db, account, UserAddress.query.get(main_row.address_id),
        product=sample_product, quantity=60, amount="90000", number="SA_000801_26", days_ago=5,
    )
    _branch_history(
        db, account, UserAddress.query.get(branch_row.address_id),
        product=sample_product, quantity=30, amount="60000", number="SA_000802_26", days_ago=3,
    )

    main_card = client.get(f"{OUTLETS}/{main['id']}", headers=sales_agent_auth_headers)
    branch_card = client.get(f"{OUTLETS}/{branch['id']}", headers=sales_agent_auth_headers)
    assert main_card.status_code == 200 and branch_card.status_code == 200
    main_body = main_card.get_json()["data"]["outlet"]
    branch_body = branch_card.get_json()["data"]["outlet"]

    # History: each shelf's own orders, never the sibling's. Exact lists: the approval chain
    # above leaves no order of its own on the account, so each branch's history is precisely
    # the one order that landed at its door (account-wide, both cards listed both).
    assert [o["order_number"] for o in main_body["last_orders"]] == ["SA_000801_26"]
    assert [o["order_number"] for o in branch_body["last_orders"]] == ["SA_000802_26"]
    # The D7/D15 figures that fall out of that history. SALES_RATE_HISTORY_DAYS is 60, class B
    # cadence is 14 and SALES_DELIVERY_LEAD_DAYS 1, safety factor 1.5:
    #   main   60/60 = 1.000/day -> ceil(1.0 * 15 * 1.5) = 23
    #   branch 30/60 = 0.500/day -> ceil(0.5 * 15 * 1.5) = 12
    # Account-wide the two shops would have read 90/60 = 1.5 and 34 -- the same number twice.
    assert (main_body["rate_per_day"], main_body["suggested_now"]) == (1.0, 23)
    assert (branch_body["rate_per_day"], branch_body["suggested_now"]) == (0.5, 12)
    # Money is the ACCOUNT's, and both cards say the same thing (D25 rule 6: it is LABELLED
    # account-level, never split per branch).
    assert main_body["open_receivable"] == 150000.0
    assert branch_body["open_receivable"] == 150000.0


def test_marking_a_junk_branch_lost_takes_the_real_shop_out_of_branch_mode(
    client, sales_agent_auth_headers, admin_claim_headers, db
):
    """R45 through the doors the people involved really use.

    The agent links two shops to one account with the bot's 🔗 Link (the second ~4 km away, so it
    gets an address of its own); the office decides the second is junk and marks it lost from the
    admin drawer; the agent reopens the first card. It is one shop again: `branch_count` counts
    the outlets that still trade and `is_branch` -- the one predicate both renderers gate the
    account line and the "(account)" money label on -- is False.
    """
    account = _customer(db, "+998901112211", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    shop = _create(client, sales_agent_auth_headers, {**BOT_PAYLOAD, "link_user_id": account.id})
    junk = _create(
        client,
        sales_agent_auth_headers,
        {
            **BOT_PAYLOAD,
            "name": "Bahor market, Yunusobod",
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 19-kvartal, 4",
            "link_user_id": account.id,
        },
    )
    before = client.get(f"{OUTLETS}/{shop['id']}", headers=sales_agent_auth_headers)
    assert before.status_code == 200, before.get_data(as_text=True)
    before_body = before.get_json()["data"]["outlet"]
    assert (before_body["branch_count"], before_body["is_branch"]) == (2, True)

    lost = client.post(
        f"/api/v1/admin/sales/outlets/{junk['id']}/mark-lost",
        json={"reason": "closed", "note": "Stale address from the import"},
        headers=admin_claim_headers,
    )
    assert lost.status_code == 200, lost.get_data(as_text=True)
    assert lost.get_json()["data"]["outlet"]["stage"] == "lost"

    after = client.get(f"{OUTLETS}/{shop['id']}", headers=sales_agent_auth_headers)
    assert after.status_code == 200, after.get_data(as_text=True)
    body = after.get_json()["data"]["outlet"]
    assert (body["account_name"], body["branch_count"], body["is_branch"]) == ("Bahor", 1, False)


def test_an_agent_cannot_change_an_outlets_class(client, db, sales_agent_user, sales_agent_auth_headers):
    """T-VIS-10, D-CLASS (owner-confirmed): `outlet_class` is not agent-editable.

    The class sets the cadence, the cadence sets the due date, and the due date decides which
    shops enter the 01:20 frozen plan (spec §4.1.2), so an agent reclassing a shop between the
    snapshot and the visit was editing their own compliance. Dropped, not refused, like every
    other admin-only field: the notes in the same PUT still land. The bot never calls this route.
    An admin still changes the class and moves the due date with it (D29,
    tests/integration/test_admin_sales_outlets_api.py::test_a_class_change_moves_the_due_date_now).
    """
    outlet = Outlet(
        name="Bahor market", outlet_type="grocery_store", stage="active", outlet_class="C",
        district="chilanzar", assigned_agent_user_id=sales_agent_user.id,
        last_visit_at=datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc),
    )
    db.session.add(outlet)
    db.session.commit()

    response = client.put(
        f"{OUTLETS}/{outlet.id}",
        json={"class": "B", "notes": "Asked for weekly visits"},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()["data"]["outlet"]
    assert body["notes"] == "Asked for weekly visits"   # proof the write really happened
    assert body["class"] == "C" and body["next_visit_due_at"] is None
    assert Outlet.query.get(outlet.id).outlet_class == "C"
    # The allowlist itself, pinned: a key added here is a new thing agents may write, and that is
    # a decision, not a refactor.
    assert AGENT_EDITABLE_OUTLET_FIELDS == frozenset(
        {
            "name",
            "channel",
            "opening_hours",
            "preferred_visit_window",
            "delivery_window_start",
            "delivery_window_end",
            "competitor_note",
            "notes",
            "preferred_language",
        }
    )


def test_create_without_a_contact_phone_is_refused_before_the_duplicate_and_link_checks(
    client, sales_agent_auth_headers, db
):
    """D31 R1: an agent-created outlet carries a phone on its primary contact.

    Checked BEFORE `find_duplicates` and the 🔗 Link branch. The shop below already stands at this
    pin under this account, so in any other order the plain, Link and name-only bodies would get
    the 409 the bot answers with "Create anyway" / "Link" -- buttons that could only end in this
    refusal -- and the forced one would be stored. A contact with a name but no phone has no phone,
    and a phone of only spaces is missing, not malformed: the rule reads the raw value, before
    `_contact_fields` and its format check could call it "invalid".
    """
    customer = _customer(db, "+998901112277", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    db.session.add(
        Outlet(
            name="Bahor market",
            outlet_type="grocery_store",
            stage="active",
            user_id=customer.id,
            latitude=PIN[0],
            longitude=PIN[1],
        )
    )
    db.session.commit()
    phoneless = {**BOT_PAYLOAD, "contact": None}

    for body in (
        phoneless,
        {**phoneless, "force": True},
        {**phoneless, "link_user_id": customer.id},
        {**BOT_PAYLOAD, "contact": {"name": "Olim aka", "role": "owner"}},
        {**BOT_PAYLOAD, "contact": {"name": "Olim aka", "phone": "   ", "role": "owner"}},
    ):
        refused = client.post(OUTLETS, json=body, headers=sales_agent_auth_headers)

        assert refused.status_code == 400, (body, refused.get_data(as_text=True))
        assert refused.get_json()["error_code"] == "SALES_OUTLET_PHONE_REQUIRED", body
        assert Outlet.query.count() == 1, body  # only the shop that was already there


@pytest.mark.parametrize(
    "missing",
    [("latitude", "longitude"), ("longitude",), ("latitude", "longitude", "contact")],
    ids=["no-pin", "half-pin", "no-pin-and-no-phone"],
)
def test_create_without_both_coordinates_is_refused(client, sales_agent_auth_headers, db, missing):
    """D31 R3: the API now demands the pin the walk-in already could not skip. One code for no pin
    and for half a pin, which the bot maps to one sentence. With no phone either, the pin is still
    the answer: `_validate_common` runs before the phone rule."""
    body = {key: value for key, value in BOT_PAYLOAD.items() if key not in missing}

    refused = client.post(OUTLETS, json=body, headers=sales_agent_auth_headers)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_OUTLET_PIN_REQUIRED"
    assert Outlet.query.count() == 0


@pytest.mark.parametrize("phone", ["71 123 45 67", "+79161234567"], ids=["tashkent-landline", "foreign-mobile"])
def test_a_number_that_is_not_an_uzbek_mobile_is_refused_at_create(client, sales_agent_auth_headers, db, phone):
    """D31 R2: the customer account is opened on this number, so it is held to the shared rule
    (`shared.validators.normalize_phone_number`), not to "parses as a phone number". Both of these
    ARE valid numbers -- a Tashkent fixed line, a Russian mobile -- which is exactly why the old
    format-only check let them in."""
    body = {**BOT_PAYLOAD, "contact": {"name": "Olim aka", "phone": phone, "role": "owner"}}

    refused = client.post(OUTLETS, json=body, headers=sales_agent_auth_headers)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_CONTACT_PHONE_INVALID"
    assert Outlet.query.count() == 0


@pytest.mark.parametrize("typed", ["90 123 45 67", "+998 90 123-45-67", "998901234567", "901234567"])
def test_every_everyday_spelling_of_a_mobile_is_stored_as_one_e164_number(
    client, sales_agent_auth_headers, db, typed
):
    """Review focus 1: what agents really type. R2 refuses landlines without refusing any of
    these, and the primary contact stores the one E.164 number `_require_primary_phone` returns."""
    body = {**BOT_PAYLOAD, "contact": {"name": "Olim aka", "phone": typed, "role": "owner"}}

    created = _create(client, sales_agent_auth_headers, body)

    assert [(contact["phone"], contact["is_primary"]) for contact in created["contacts"]] == [
        ("+998901234567", True)
    ]
    assert Outlet.query.get(created["id"]).primary_contact.phone == "+998901234567"


def _outlet_row(db, agent, *, stage="prospect", outlet_type="grocery_store", contact=None, pinned=True):
    """An outlet built directly, in a stage or a shape `POST /sales/outlets` does not produce.

    Since D31 the create route refuses a shop with no phone or no pin, but rows from before it
    remain: dev outlet #9 "Abc market" has no contact at all. `contact` (OutletContact fields)
    becomes the PRIMARY contact. Assigned to `agent`, so `get_for_agent` opens it in any stage.
    """
    outlet = Outlet(
        name="Abc market",
        outlet_type=outlet_type,
        stage=stage,
        latitude=PIN[0] if pinned else None,
        longitude=PIN[1] if pinned else None,
        assigned_agent_user_id=agent.id,
    )
    if contact is not None:
        outlet.contacts.append(OutletContact(role="owner", is_primary=True, **contact))
    db.session.add(outlet)
    db.session.commit()
    return outlet


@pytest.mark.parametrize("stage", OUTLET_STAGES)
def test_the_card_offers_request_activation_only_to_a_prospect_or_a_trial(
    client, db, sales_agent_auth_headers, sales_agent_user, stage
):
    """R5 (D31) over every stage, read where the staff bot reads it: the agent's card. The shop has
    everything else activation needs -- a primary phone and a pin -- so the stage is the only
    variable. The two stages are spelled out, not imported, so a drift in `REQUESTABLE_STAGES`
    shows against the ruling rather than against itself.
    """
    outlet = _outlet_row(db, sales_agent_user, stage=stage, contact={"name": "Olim aka", "phone": "+998901112266"})

    card = client.get(f"{OUTLETS}/{outlet.id}", headers=sales_agent_auth_headers)

    assert card.status_code == 200, card.get_data(as_text=True)
    assert card.get_json()["data"]["outlet"]["can_request_activation"] is (stage in ("prospect", "trial"))


@pytest.mark.parametrize(
    "outlet_type, contact, pinned, error_code, message",
    [
        # A private customer is activated at creation, never on request. The bot never offered the
        # button; now the backend refuses the request as well.
        (
            "individual",
            {"name": "Dilnoza Rahimova", "phone": "+998901112299"},
            True,
            "SALES_OUTLET_STAGE_INVALID",
            "Only a shop or workplace goes through activation",
        ),
        # The same with no phone: the shop-or-workplace check comes before the phone.
        (
            "individual",
            None,
            True,
            "SALES_OUTLET_STAGE_INVALID",
            "Only a shop or workplace goes through activation",
        ),
        # Dev outlet #9: the phone step was skipped with no name typed, so there is no contact.
        (
            "grocery_store",
            None,
            True,
            "SALES_ACTIVATION_PHONE_REQUIRED",
            "A contact phone is required to request activation",
        ),
        # A name was typed and the phone skipped: the PRIMARY contact has no phone.
        (
            "grocery_store",
            {"name": "Olim aka", "phone": None},
            True,
            "SALES_ACTIVATION_PHONE_REQUIRED",
            "A contact phone is required to request activation",
        ),
        (
            "grocery_store",
            {"name": "Olim aka", "phone": "+998901112266"},
            False,
            "SALES_ACTIVATION_PIN_REQUIRED",
            "A pin is required to request activation",
        ),
        # Neither: the phone is asked for first, as it always was.
        (
            "grocery_store",
            None,
            False,
            "SALES_ACTIVATION_PHONE_REQUIRED",
            "A contact phone is required to request activation",
        ),
    ],
    ids=[
        "individual", "individual_without_a_phone", "no_contact", "name_only_contact", "no_pin",
        "no_contact_and_no_pin",
    ],
)
def test_the_card_hides_request_activation_exactly_where_the_request_is_refused(
    client, db, monkeypatch, sales_agent_auth_headers, sales_agent_user, outlet_type, contact, pinned, error_code, message
):
    """R5 (D31) has one expression, `OutletService._activation_refusal`: the card publishes
    `can_request_activation: false` for exactly the outlets `POST /request-activation` refuses,
    and the request gets the first refusal in R5's order -- the new shop-or-workplace check, then
    a phone on the PRIMARY contact, then a pin -- each with today's code. These are legacy shapes
    the create route no longer produces, so they are built directly; a refusal writes nothing and
    alerts no operator.
    """
    notified = _spy(monkeypatch, notifications, "notify_activation_requested")
    outlet = _outlet_row(db, sales_agent_user, outlet_type=outlet_type, contact=contact, pinned=pinned)
    outlet_id = outlet.id

    card = client.get(f"{OUTLETS}/{outlet_id}", headers=sales_agent_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    assert card.get_json()["data"]["outlet"]["can_request_activation"] is False

    refused = client.post(f"{OUTLETS}/{outlet_id}/request-activation", headers=sales_agent_auth_headers)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert (refused.get_json()["error_code"], refused.get_json()["message"]) == (error_code, message)
    row = Outlet.query.get(outlet_id)
    assert (row.stage, row.activation_requested_at) == ("prospect", None)
    assert notified == []


@pytest.mark.parametrize(
    "payload, stage, can_request",
    [
        ({**BOT_PAYLOAD, "force": False, "link_user_id": None}, "prospect", True),
        (
            {
                **BOT_PAYLOAD,
                "name": "Yashnobod ofis",
                "outlet_type": "workplace",
                "class": "A",
                "force": False,
                "link_user_id": None,
            },
            "prospect",
            True,
        ),
        # A private customer with a phone is activated at creation: there is nothing to request.
        (
            {
                **BOT_PAYLOAD,
                "name": "Dilnoza opa",
                "outlet_type": "individual",
                "contact": {"name": "Dilnoza Rahimova", "phone": "+998901112299", "role": "owner"},
                "class": "C",
                "force": False,
                "link_user_id": None,
            },
            "active",
            False,
        ),
    ],
    ids=["grocery_store", "workplace", "individual"],
)
def test_create_replies_with_the_card(client, db, sales_agent_auth_headers, payload, stage, can_request):
    """P6 (D31): `POST /sales/outlets` answers with `OutletService.card`, not the bare record. The
    bot draws the receipt and its buttons from this reply, so it must carry what the card
    publishes and say exactly what a re-fetch says. One case per type on the bot's type keyboard,
    each posted with every key `new_outlet.py:_payload()` sends.
    """
    created = _create(client, sales_agent_auth_headers, payload)

    assert (created["stage"], created["can_request_activation"]) == (stage, can_request)
    # Card-only keys, as their creator reads them: no visit is open here, and nobody approves a
    # shop they registered themselves.
    assert (created["open_visit_id"], created["can_approve"]) == (None, False)
    card = client.get(f"{OUTLETS}/{created['id']}", headers=sales_agent_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    assert card.get_json()["data"]["outlet"] == created


def test_request_activation_replies_with_the_moved_card(client, db, monkeypatch, sales_agent_auth_headers):
    """P6 (D31): the bot redraws the card from this reply, so the reply IS the card -- already at
    `activation_requested`, and so no longer offering the request it has just made. A second tap
    from the old card is the stage refusal, and the operators are alerted once.
    """
    notified = _spy(monkeypatch, notifications, "notify_activation_requested")
    outlet = _create(client, sales_agent_auth_headers)

    requested = client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)

    assert requested.status_code == 200, requested.get_data(as_text=True)
    body = requested.get_json()["data"]["outlet"]
    assert (body["id"], body["stage"], body["can_request_activation"]) == (outlet["id"], "activation_requested", False)
    assert (body["open_visit_id"], body["can_approve"]) == (None, False)
    card = client.get(f"{OUTLETS}/{outlet['id']}", headers=sales_agent_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    assert card.get_json()["data"]["outlet"] == body

    again = client.post(f"{OUTLETS}/{outlet['id']}/request-activation", headers=sales_agent_auth_headers)

    assert again.status_code == 400, again.get_data(as_text=True)
    assert (again.get_json()["error_code"], again.get_json()["message"]) == (
        "SALES_OUTLET_STAGE_INVALID",
        "Outlet is not a prospect or trial",
    )
    assert [args[0].id for args, _kwargs in notified] == [outlet["id"]]
