"""/api/v1/admin/sales/outlets driven the way Outlets.js drives it."""
import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.user import UserAddress
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.constants import MAX_PAGE_SIZE
from shared.enums import EntitySubtype
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_outlet_dedupe import PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

URL = "/api/v1/admin/sales/outlets"


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def test_list_filters_summary_pins_and_detail(client, admin_claim_headers, db, agent):
    outlet = OutletService.create(agent.id, dict(GROCERY))
    OutletService.request_activation(outlet, agent.id)

    listed = client.get(
        URL,
        headers=admin_claim_headers,
        query_string={"stage": "activation_requested", "district": "chilanzar", "page": 1, "per_page": 20},
    )
    assert listed.status_code == 200, listed.get_data(as_text=True)
    body = listed.get_json()
    assert [row["id"] for row in body["data"]["items"]] == [outlet.id]
    assert body["meta"]["summary"]["activation_requested"] == 1 and body["meta"]["summary"]["active"] == 0

    empty = client.get(URL, headers=admin_claim_headers, query_string={"stage": "active"})
    assert empty.get_json()["data"]["items"] == []

    pins = client.get(URL, headers=admin_claim_headers, query_string={"format": "pins"})
    assert pins.get_json()["data"]["pins"] == [
        {
            "id": outlet.id,
            "name": "Bahor market",
            "stage": "activation_requested",
            "outlet_type": "grocery_store",
            "lat": PIN[0],
            "lng": PIN[1],
            "assigned_agent_user_id": agent.id,
            "district": "chilanzar",
        }
    ]

    detail = client.get(f"{URL}/{outlet.id}", headers=admin_claim_headers)
    assert detail.status_code == 200
    data = detail.get_json()["data"]
    assert data["outlet"]["open_receivable"] is None  # no customer account yet: not applicable, not zero
    assert [(h["from_stage"], h["to_stage"]) for h in data["stage_history"]] == [
        (None, "prospect"),
        ("prospect", "activation_requested"),
    ]


def test_approve_reject_assign_mark_lost(client, admin_claim_headers, db, agent, admin_user):
    outlet = OutletService.create(agent.id, dict(GROCERY))
    OutletService.request_activation(outlet, agent.id)

    rejected = client.post(f"{URL}/{outlet.id}/reject", json={"reason": "Incomplete"}, headers=admin_claim_headers)
    assert rejected.status_code == 200 and rejected.get_json()["data"]["outlet"]["stage"] == "prospect"

    OutletService.request_activation(Outlet.query.get(outlet.id), agent.id)
    approved = client.post(
        f"{URL}/{outlet.id}/approve", json={"contract_number": "GS-TEST-0001"}, headers=admin_claim_headers
    )
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["outlet"]["stage"] == "active"

    other = make_sales_agent_user(db, phone="+998901234579", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id))
    db.session.commit()
    assigned = client.post(f"{URL}/{outlet.id}/assign", json={"agent_user_id": other.id}, headers=admin_claim_headers)
    assert assigned.status_code == 200 and assigned.get_json()["data"]["outlet"]["assigned_agent_user_id"] == other.id

    not_agent = client.post(
        f"{URL}/{outlet.id}/assign", json={"agent_user_id": admin_user.id}, headers=admin_claim_headers
    )
    assert not_agent.status_code == 400 and "SALES_AGENT_PROFILE_REQUIRED" in not_agent.get_data(as_text=True)

    lost = client.post(
        f"{URL}/{outlet.id}/mark-lost", json={"reason": "closed", "note": "Shop closed"}, headers=admin_claim_headers
    )
    assert lost.status_code == 200 and lost.get_json()["data"]["outlet"]["lost_reason"] == "closed"


def test_bulk_assign_and_import(client, admin_claim_headers, db, agent):
    grocery = _customer(db, "+998901113366", company="Chinor", subtype=EntitySubtype.GROCERY_STORE)
    db.session.add(
        UserAddress(
            user_id=grocery.id,
            full_address="Chilonzor 9",
            district="chilanzar",
            latitude=PIN[0],
            longitude=PIN[1],
            is_default=True,
        )
    )
    db.session.commit()

    imported = client.post(f"{URL}/import-existing-customers", json={}, headers=admin_claim_headers)
    assert imported.status_code == 200 and imported.get_json()["data"] == {"created": 1}

    bulk = client.post(
        f"{URL}/bulk-assign", json={"district": "chilanzar", "agent_user_id": agent.id}, headers=admin_claim_headers
    )
    assert bulk.status_code == 200 and bulk.get_json()["data"] == {"updated": 1}
    assert Outlet.query.filter_by(user_id=grocery.id).one().assigned_agent_user_id == agent.id


def test_pins_are_never_truncated_by_the_page_cap(client, admin_claim_headers, db):
    """The map is not a page of results.

    `?format=pins` must return EVERY matching pinned outlet: capping it at the page size would draw
    a 400-outlet estate as 100 pins, and a truncated map is indistinguishable from a district that
    genuinely has no outlets. An outlet without coordinates is still excluded -- it has no pin.
    """
    pinned = [
        Outlet(name=f"Pinned {i}", outlet_type="grocery_store", latitude=PIN[0], longitude=PIN[1])
        for i in range(MAX_PAGE_SIZE + 1)
    ]
    db.session.add_all(pinned)
    db.session.add(Outlet(name="No pin", outlet_type="grocery_store"))
    db.session.commit()

    pins = client.get(URL, headers=admin_claim_headers, query_string={"format": "pins"})
    assert pins.status_code == 200, pins.get_data(as_text=True)
    returned = pins.get_json()["data"]["pins"]
    assert len(returned) == MAX_PAGE_SIZE + 1
    assert [p["id"] for p in returned] == [o.id for o in pinned]


def test_summary_counts_the_whole_estate_not_the_filtered_rows(client, admin_claim_headers, db, agent):
    """`meta.summary` is estate-wide BY DESIGN, not a tally of the rows the filter returned.

    Pinned here because the phase-2 UI has to label those tiles as totals: read as "matching your
    filter" they would claim 2 prospects in a district that returned 1 row.
    """
    chilanzar = OutletService.create(agent.id, dict(GROCERY))
    db.session.add(Outlet(name="Navruz market", outlet_type="grocery_store", district="yunusabad", stage="prospect"))
    db.session.commit()

    listed = client.get(URL, headers=admin_claim_headers, query_string={"district": "chilanzar"})
    assert listed.status_code == 200, listed.get_data(as_text=True)
    body = listed.get_json()
    assert [row["id"] for row in body["data"]["items"]] == [chilanzar.id]
    assert body["meta"]["total"] == 1
    assert body["meta"]["summary"]["prospect"] == 2


def test_district_is_editable_from_the_drawer_and_a_typo_is_refused(client, admin_claim_headers, db, agent):
    """The promise the outlet CREATE path leans on, finally keepable.

    `_validate_common` stores an unresolvable geocoder district as NULL instead of killing a
    completed walk-in, explicitly because "a NULL an admin fixes in the Outlets drawer costs a
    click". That was unkeepable while `UpdateOutletPayload` carried no `district` at all: the
    drawer had nothing to send, so the NULL was permanent and the outlet stayed unreachable by
    every district filter and by `bulk_assign_by_district`.

    Editing is the MIRROR of creating, not a copy of it. Here a human picks the value out of the
    geo-config list, so an unresolvable one is a typo and has to be TOLD -- the same ruling
    `bulk_assign_by_district` and `staff_service._validate_districts` already make. Display names
    are accepted because that is what a picker shows; the canonical KEY is what gets stored.
    """
    outlet = OutletService.create(agent.id, {**GROCERY, "district": "Nowhere District"})
    assert outlet.district is None, "the create path's deliberate NULL is the premise of this test"

    fixed = client.put(f"{URL}/{outlet.id}", json={"district": "Chilanzar"}, headers=admin_claim_headers)
    assert fixed.status_code == 200, fixed.get_data(as_text=True)
    assert fixed.get_json()["data"]["outlet"]["district"] == "chilanzar"
    db.session.expire_all()
    assert Outlet.query.get(outlet.id).district == "chilanzar"

    typo = client.put(f"{URL}/{outlet.id}", json={"district": "nowhere"}, headers=admin_claim_headers)
    assert typo.status_code == 400, typo.get_data(as_text=True)
    assert "SALES_DISTRICT_INVALID" in typo.get_data(as_text=True)
    db.session.expire_all()
    assert Outlet.query.get(outlet.id).district == "chilanzar", "a refused edit must not have written"


def test_bulk_assign_refuses_an_unknown_district(client, admin_claim_headers, db, agent):
    """The other half of the same ruling, over HTTP.

    The Outlets toolbar's bulk-assign picker is fed from the geo-config district list, so its
    values are canonical keys by construction. This pins what happens when they are not: a
    silently-zero "0 outlets assigned" would read as an empty district rather than as a typo.
    """
    resp = client.post(
        f"{URL}/bulk-assign",
        json={"district": "atlantis", "agent_user_id": agent.id},
        headers=admin_claim_headers,
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert "SALES_DISTRICT_INVALID" in resp.get_data(as_text=True)


def test_an_explicitly_sent_null_clears_the_field(client, admin_claim_headers, db, agent):
    """Clearing a note has to actually clear it.

    `_validated_payload` dropped every null before the service saw it (`exclude_none=True`), so
    emptying a textarea in the drawer produced a success toast and no write -- the admin had no
    way to tell the field had not been cleared. `exclude_unset=True` keeps the distinction that
    matters instead: a field the client never mentioned is still dropped, a null it deliberately
    sent survives.
    """
    outlet = OutletService.create(agent.id, dict(GROCERY))
    assert outlet.notes == "Corner shop"

    cleared = client.put(f"{URL}/{outlet.id}", json={"notes": None}, headers=admin_claim_headers)
    assert cleared.status_code == 200, cleared.get_data(as_text=True)
    assert cleared.get_json()["data"]["outlet"]["notes"] is None
    db.session.expire_all()
    assert Outlet.query.get(outlet.id).notes is None

    # ...and a NOT-NULL column refuses the very same explicit null. `outlets.payment_terms` is
    # `nullable=False` with a default, so "clear it" has no meaning: the admin is told, rather than
    # handed a success toast for a write that could never happen (or a 500 from the flush).
    refused = client.put(f"{URL}/{outlet.id}", json={"payment_terms": None}, headers=admin_claim_headers)
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert "SALES_PAYMENT_TERMS_INVALID" in refused.get_data(as_text=True)
    db.session.expire_all()
    assert Outlet.query.get(outlet.id).payment_terms == "cash"

    # ...and an omitted field is still left alone, which is the whole reason this is not
    # `exclude_none=False`.
    untouched = client.put(f"{URL}/{outlet.id}", json={"class": "A"}, headers=admin_claim_headers)
    assert untouched.status_code == 200, untouched.get_data(as_text=True)
    db.session.expire_all()
    row = Outlet.query.get(outlet.id)
    assert row.outlet_class == "A" and row.preferred_language == "uz" and row.name == "Bahor market"


def test_the_outlet_delivery_window_is_writable_from_the_admin_page(client, admin_claim_headers, db, agent):
    """C24's writer, driven the way the Outlets drawer's modal drives it.

    The window is not decoration: `VisitService` offers it as the DEFAULT delivery window of every
    order the agent places at this outlet, and only when BOTH edges are set
    (`visit_service.py:571-580`). So the PAIR is what has to be storable, the admin modal is its
    only writer, and this is the one place the whole rule -- set, read back on the card, refuse an
    inverted pair (whichever edge the request carries), refuse nonsense, clear -- is provable.
    """
    outlet = OutletService.create(agent.id, dict(GROCERY))
    assert (outlet.delivery_window_start, outlet.delivery_window_end) == (None, None)

    saved = client.put(
        f"{URL}/{outlet.id}",
        json={"delivery_window_start": "09:30", "delivery_window_end": "18:45"},
        headers=admin_claim_headers,
    )
    assert saved.status_code == 200, saved.get_data(as_text=True)
    echoed = saved.get_json()["data"]["outlet"]
    assert (echoed["delivery_window_start"], echoed["delivery_window_end"]) == ("09:30", "18:45")

    # The drawer reads the CARD, not the PUT's echo. Both have to publish the window, or the modal
    # reopens empty over a window that is really stored -- one Save away from clearing it.
    card = client.get(f"{URL}/{outlet.id}", headers=admin_claim_headers).get_json()["data"]["outlet"]
    assert (card["delivery_window_start"], card["delivery_window_end"]) == ("09:30", "18:45")

    inverted = client.put(
        f"{URL}/{outlet.id}",
        json={"delivery_window_start": "18:45", "delivery_window_end": "09:30"},
        headers=admin_claim_headers,
    )
    assert inverted.status_code == 400, inverted.get_data(as_text=True)
    assert "SALES_WINDOW_INVALID" in inverted.get_data(as_text=True)

    # One edge sent alone is merged with the edge already on the row: 19:00 against the stored
    # 18:45 is the same inverted pair, refused with the same code -- and a refused edit must not
    # have written anything.
    half = client.put(
        f"{URL}/{outlet.id}", json={"delivery_window_start": "19:00"}, headers=admin_claim_headers
    )
    assert half.status_code == 400, half.get_data(as_text=True)
    assert "SALES_WINDOW_INVALID" in half.get_data(as_text=True)
    db.session.expire_all()
    row = Outlet.query.get(outlet.id)
    assert (row.delivery_window_start.strftime("%H:%M"), row.delivery_window_end.strftime("%H:%M")) == (
        "09:30",
        "18:45",
    )

    malformed = client.put(
        f"{URL}/{outlet.id}",
        json={"delivery_window_start": "25:99", "delivery_window_end": "18:45"},
        headers=admin_claim_headers,
    )
    assert malformed.status_code == 400, malformed.get_data(as_text=True)
    assert "SALES_WINDOW_INVALID" in malformed.get_data(as_text=True)

    # Clearing is both edges at once, as two explicit nulls -- the modal's rule, and the only
    # spelling `_validated_payload`'s exclude_unset lets through as "clear it".
    cleared = client.put(
        f"{URL}/{outlet.id}",
        json={"delivery_window_start": None, "delivery_window_end": None},
        headers=admin_claim_headers,
    )
    assert cleared.status_code == 200, cleared.get_data(as_text=True)
    assert cleared.get_json()["data"]["outlet"]["delivery_window_start"] is None
    assert cleared.get_json()["data"]["outlet"]["delivery_window_end"] is None
    db.session.expire_all()
    row = Outlet.query.get(outlet.id)
    assert (row.delivery_window_start, row.delivery_window_end) == (None, None)
