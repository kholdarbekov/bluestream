"""/api/v1/admin/sales/outlets driven the way Outlets.js drives it."""
from datetime import UTC, datetime

import pytest

from business_app.models.corporate import CorporateContract
from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.user import UserAddress
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.constants import MAX_PAGE_SIZE
from business_app.utils.service_factory import get_corporate_contract_service
from shared.enums import EntitySubtype
from tests.integration.test_outlet_approval import _address
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_outlet_dedupe import FAR, PIN, _customer
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


def test_the_approve_modals_body_with_no_number_auto_numbers_the_contract(client, admin_claim_headers, db, agent):
    """The approve modal's exact body when the operator leaves *Contract number* empty.

    `salesService.approveOutlet` always sends both keys -- `{contract_number: null, attach:
    false}` -- so the explicit null is what the door must read as "no number": a new account
    and the auto-numbered `SA-<outlet_id>-<yyyymmdd>` contract, exactly as a contract-less
    approve has always produced.
    """
    outlet = OutletService.create(agent.id, dict(GROCERY))
    OutletService.request_activation(outlet, agent.id)
    day_before = f"{datetime.now(UTC):%Y%m%d}"

    approved = client.post(
        f"{URL}/{outlet.id}/approve", json={"contract_number": None, "attach": False}, headers=admin_claim_headers
    )

    day_after = f"{datetime.now(UTC):%Y%m%d}"  # the pair only matters across a UTC midnight
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["outlet"]["stage"] == "active"
    row = Outlet.query.get(outlet.id)
    contract = get_corporate_contract_service().get_active_amount_contract_for_user(row.user_id)
    assert contract is not None
    assert contract.contract_number in {f"SA-{outlet.id}-{day_before}", f"SA-{outlet.id}-{day_after}"}


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


def test_import_creates_one_outlet_per_address_and_backfills_on_a_re_run(client, admin_claim_headers, db):
    """The Outlets toolbar button, with the payload Outlets.js sends: POST with an empty body.

    The reply counts OUTLETS, so a two-branch chain reads 2 the first time and 0 the second --
    and an address added later is back-filled by pressing the button again. That re-run IS the
    prod remediation for the chains imported under the old one-outlet-per-account rule.
    """
    chain = _customer(db, "+998901117744", company="Chinor", subtype=EntitySubtype.GROCERY_STORE)
    _address(db, chain, "Chilonzor 9", "chilanzar", title="Bosh do'kon", is_default=True)
    _address(db, chain, "Yunusobod 4", "Yunusabad", title="Filial-2", is_default=False)
    db.session.commit()

    first = client.post(f"{URL}/import-existing-customers", json={}, headers=admin_claim_headers)
    assert first.status_code == 200, first.get_data(as_text=True)
    assert first.get_json()["data"] == {"created": 2}

    again = client.post(f"{URL}/import-existing-customers", json={}, headers=admin_claim_headers)
    assert again.status_code == 200, again.get_data(as_text=True)
    assert again.get_json()["data"] == {"created": 0}

    _address(db, chain, "Sergeli 3", "sergeli", title="Filial-3", is_default=False)
    db.session.commit()
    third = client.post(f"{URL}/import-existing-customers", json={}, headers=admin_claim_headers)
    assert third.status_code == 200, third.get_data(as_text=True)
    assert third.get_json()["data"] == {"created": 1}

    listed = client.get(URL, headers=admin_claim_headers, query_string={"search": "Chinor", "per_page": 50})
    assert listed.status_code == 200, listed.get_data(as_text=True)
    rows = listed.get_json()["data"]["items"]
    assert sorted(row["name"] for row in rows) == [
        "Chinor, Bosh do'kon",
        "Chinor, Filial-2",
        "Chinor, Filial-3",
    ]
    assert sorted(row["district"] for row in rows) == ["chilanzar", "sergeli", "yunusabad"]
    assert {row["user_id"] for row in rows} == {chain.id}
    assert len({row["address_id"] for row in rows}) == 3


def _chain_account(db, admin_user, *, phone="+998901115511", company="Chinor"):
    """An existing chain, in the shape the two prod accounts have: one customer account, one
    address, one outlet already standing on it, and the AMOUNT contract its first branch's
    approval minted. `attach` must JOIN this, not duplicate it."""
    account = _customer(db, phone, company=company, subtype=EntitySubtype.GROCERY_STORE)
    address = UserAddress(
        user_id=account.id,
        full_address="Chilonzor 9",
        district="chilanzar",
        latitude=PIN[0],
        longitude=PIN[1],
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    db.session.add(
        Outlet(
            name=f"{company}, Chilonzor 9",
            outlet_type="grocery_store",
            stage="active",
            user_id=account.id,
            address_id=address.id,
            latitude=PIN[0],
            longitude=PIN[1],
        )
    )
    db.session.commit()
    get_corporate_contract_service().create_contract(
        {"user_id": account.id, "contract_number": "CHAIN-0001", "name": f"{company} chain"},
        actor_user_id=admin_user.id,
    )
    db.session.commit()
    # The whole "the contract is reused, no second number is minted" assertion rests on this row
    # being the account's ACTIVE amount contract (status + effective window come from
    # `create_contract`'s defaults). Asserted here so a fixture problem fails as a fixture
    # problem, not as a false "attach minted a second contract".
    assert get_corporate_contract_service().get_active_amount_contract_for_user(account.id) is not None
    return account


def _branch_outlet(agent, *, name="Chinor, Yunusobod 4", phone="+998901115511"):
    """The second shop of that chain, registered from the field: same phone, its own pin."""
    outlet = OutletService.create(
        agent.id,
        {
            **GROCERY,
            "name": name,
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 4-kvartal, 7",
            "contact": {"name": "Olim aka", "phone": phone, "role": "owner"},
        },
        force=True,
    )
    return OutletService.request_activation(outlet, agent.id)


def test_approve_refuses_a_chain_phone_and_attaches_the_branch_on_demand(
    client, admin_claim_headers, db, agent, admin_user
):
    """D25 rule 3, through the door Outlets.js uses.

    A phone that already has an account is not a mistake to be refused — it is the account the
    new shop belongs to. The plain Approve still refuses (joining an account is a DECISION a
    human makes), but the refusal now names the account; `attach: true` joins it: one more
    branch on the same wallet, its own non-default address, the chain's contract reused.
    """
    account = _chain_account(db, admin_user)
    branch = _branch_outlet(agent)

    refused = client.post(f"{URL}/{branch.id}/approve", json={}, headers=admin_claim_headers)
    assert refused.status_code == 409, refused.get_data(as_text=True)
    body = refused.get_json()
    assert body["error_code"] == "SALES_APPROVAL_PHONE_TAKEN"
    assert body["details"]["account"] == {"user_id": account.id, "name": "Chinor", "outlet_count": 1}
    assert Outlet.query.get(branch.id).stage == "activation_requested"
    assert Outlet.query.get(branch.id).user_id is None

    # The attach modal's exact body (salesService.approveOutlet in attach mode, R27): no number
    # field is drawn, so the contract number always travels as an explicit null.
    attached = client.post(
        f"{URL}/{branch.id}/approve",
        json={"contract_number": None, "attach": True},
        headers=admin_claim_headers,
    )
    assert attached.status_code == 200, attached.get_data(as_text=True)
    assert attached.get_json()["data"]["outlet"]["stage"] == "active"

    joined = Outlet.query.get(branch.id)
    assert joined.user_id == account.id
    branch_address = UserAddress.query.get(joined.address_id)
    assert branch_address.user_id == account.id and branch_address.is_default is False
    assert branch_address.full_address == "Yunusobod 4-kvartal, 7"
    assert UserAddress.query.filter_by(user_id=account.id).count() == 2
    # One account, one contract: a branch reuses the chain's and never mints a second one.
    assert [c.contract_number for c in CorporateContract.query.filter_by(user_id=account.id).all()] == [
        "CHAIN-0001"
    ]

    history = client.get(f"{URL}/{branch.id}", headers=admin_claim_headers).get_json()["data"]["stage_history"]
    assert [h["reason_code"] for h in history] == ["created", "requested", "attached"]

    # Idempotent, like every other approval step: a second tap re-addresses nothing.
    again = client.post(
        f"{URL}/{branch.id}/approve", json={"contract_number": None, "attach": True}, headers=admin_claim_headers
    )
    assert again.status_code == 200 and again.get_json()["data"]["outlet"]["stage"] == "active"
    assert UserAddress.query.filter_by(user_id=account.id).count() == 2
    history = client.get(f"{URL}/{branch.id}", headers=admin_claim_headers).get_json()["data"]["stage_history"]
    assert [h["reason_code"] for h in history] == ["created", "requested", "attached"]


def test_attach_adopts_the_accounts_free_address_at_the_branch_pin(
    client, admin_claim_headers, db, agent, admin_user
):
    """R39 on the attach door: the chain already registered this shop's address (a FREE,
    non-default row ~55 m from the branch pin), so the attach adopts THAT row instead of
    minting a second address for the same place — the helper `_link_customer` uses."""
    account = _chain_account(db, admin_user)
    shop = UserAddress(
        user_id=account.id,
        full_address="Yunusobod 4-kvartal, 7",
        district="yunusabad",
        latitude=FAR[0] + 0.0004,
        longitude=FAR[1] + 0.0004,
        is_default=False,
    )
    db.session.add(shop)
    db.session.commit()
    shop_id = shop.id
    addresses_before = UserAddress.query.filter_by(user_id=account.id).count()
    branch = _branch_outlet(agent)

    attached = client.post(
        f"{URL}/{branch.id}/approve", json={"contract_number": None, "attach": True}, headers=admin_claim_headers
    )

    assert attached.status_code == 200, attached.get_data(as_text=True)
    assert attached.get_json()["data"]["outlet"]["stage"] == "active"
    joined = Outlet.query.get(branch.id)
    assert (joined.user_id, joined.address_id) == (account.id, shop_id)
    assert UserAddress.query.filter_by(user_id=account.id).count() == addresses_before == 2


def test_the_admin_drawer_publishes_the_account_and_the_attach_candidate(
    client, admin_claim_headers, db, agent, admin_user
):
    """R9/R6: the drawer draws an Account line and an Attach button from FIELDS, never from a
    phone it looks up itself. A pending branch has no account yet — hence the nulls — but it
    does name the one it would join."""
    account = _chain_account(db, admin_user)
    branch = _branch_outlet(agent)

    pending = client.get(f"{URL}/{branch.id}", headers=admin_claim_headers).get_json()["data"]["outlet"]
    assert pending["account_name"] is None and pending["branch_count"] is None
    assert pending["is_branch"] is False  # no account yet, so nothing to be a branch OF
    assert pending["open_receivable_scope"] == "account"
    assert pending["account_candidate"] == {"user_id": account.id, "name": "Chinor", "outlet_count": 1}

    attached = client.post(
        f"{URL}/{branch.id}/approve", json={"contract_number": None, "attach": True}, headers=admin_claim_headers
    )
    assert attached.status_code == 200, attached.get_data(as_text=True)

    joined = client.get(f"{URL}/{branch.id}", headers=admin_claim_headers).get_json()["data"]["outlet"]
    assert joined["account_name"] == "Chinor" and joined["branch_count"] == 2
    assert joined["is_branch"] is True
    assert joined["account_candidate"] is None
    # The list rows stay lean on purpose (no per-row account lookup on a 100-row page).
    listed = client.get(URL, headers=admin_claim_headers, query_string={"per_page": 50}).get_json()
    assert all(
        "account_name" not in row and "is_branch" not in row and "account_candidate" not in row
        for row in listed["data"]["items"]
    )
