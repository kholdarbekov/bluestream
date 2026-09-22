"""/api/v1/staff/sales/* driven with the payloads the staff bot sends."""
from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from tests.unit.test_outlet_dedupe import FAR, NEAR, PIN

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
        json={**BOT_PAYLOAD, "contact": None, "latitude": NEAR[0], "longitude": NEAR[1]},
        headers=sales_agent_auth_headers,
    )
    assert dup.status_code == 409 and "SALES_OUTLET_DUPLICATE" in dup.get_data(as_text=True)
    forced = _create(
        client,
        sales_agent_auth_headers,
        {**BOT_PAYLOAD, "contact": None, "latitude": NEAR[0], "longitude": NEAR[1], "force": True},
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
            {**BOT_PAYLOAD, "contact": None, "district": raw, "force": True},
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
        {**BOT_PAYLOAD, "contact": None, "district": "Nowhere District", "force": True},
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
