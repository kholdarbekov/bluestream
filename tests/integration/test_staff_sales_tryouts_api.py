"""POST /api/v1/staff/sales/outlets/<id>/tryouts -- the try-out an agent leaves at the counter.

Driven through HTTP with the body the staff bot sends (D8; spec *Try-out from the field*): a
basket, and nothing else. Everything that makes a try-out deliverable -- who to call, where to
carry it, which stage the outlet moves to, who gets the audit line -- is built by the BACKEND
from the outlet, so a service-level test cannot see a route that forgot the ownership guard, the
stage move or the `outlet_id` link. The hand-off task is proved where a driver really reads it:
the pool endpoint, with a driver's own token.
"""
import pytest
from flask_jwt_extended import create_access_token

from business_app.models.sales import Outlet, OutletStageHistory, SalesAgentProfile
from business_app.models.staff import StaffActivityLog
from business_app.models.tryout import ProductTryout
from business_app.services.sales.replenishment_service import effective_line_qty_max
from tests.integration.test_staff_sales_outlets_api import BOT_PAYLOAD, OUTLETS
from tests.unit.test_outlet_dedupe import PIN
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

POOL = "/api/v1/staff/tryout-tasks/pool"


def _tryouts(outlet_id: int) -> str:
    return f"{OUTLETS}/{outlet_id}/tryouts"


def _prospect(client, headers, payload=None):
    """A freshly registered shop: stage `prospect`, one primary contact, the outlet's own pin."""
    created = client.post(OUTLETS, json=payload or BOT_PAYLOAD, headers=headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    return created.get_json()["data"]["outlet"]


def test_a_field_tryout_is_built_from_the_outlet_and_lands_in_the_driver_pool(
    client, db, sales_agent_auth_headers, sales_agent_user, driver_auth_headers, sample_product
):
    outlet = _prospect(client, sales_agent_auth_headers)
    assert outlet["stage"] == "prospect"

    response = client.post(
        _tryouts(outlet["id"]),
        json={"items": [{"product_id": sample_product.id, "quantity": 2}], "notes": "Left two bottles"},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 201, response.get_data(as_text=True)
    body = response.get_json()["data"]
    tryout = body["tryout"]
    # Attribution the KPIs key on, and the link phase 3's exception feed and the nightly stage
    # job both read (a converted try-out activates ITS outlet, not some other one).
    assert tryout["source"] == "sales_agent"
    assert tryout["outlet_id"] == outlet["id"]
    assert tryout["created_by_user_id"] == sales_agent_user.id
    assert tryout["status"] == "scheduled" and tryout["outcome"] == "pending"
    assert tryout["notes"] == "Left two bottles"
    # Contact and address come from the OUTLET, never from the bot (D15): the store's primary
    # contact, the shop name as the company, and the outlet's own pin.
    assert tryout["trial_contact"]["phone"] == BOT_PAYLOAD["contact"]["phone"]
    assert tryout["trial_contact"]["first_name"] == BOT_PAYLOAD["contact"]["name"]
    assert tryout["trial_contact"]["company_name"] == BOT_PAYLOAD["name"]
    snapshot = tryout["address_snapshot"]
    assert snapshot["full_address"] == BOT_PAYLOAD["address_text"]
    assert snapshot["district"] == BOT_PAYLOAD["district"]
    assert (snapshot["latitude"], snapshot["longitude"]) == (PIN[0], PIN[1])
    assert [(item["product_id"], item["quantity"]) for item in tryout["items"]] == [(sample_product.id, 2)]
    # D1: the agent carries nothing. The hand-off is still OPEN and belongs to nobody, so a
    # driver can accept it -- a task pinned to a non-driver is invisible to the pool.
    assert [
        (task["task_type"], task["status"], task["assigned_driver_user_id"]) for task in tryout["tasks"]
    ] == [("handoff", "open", None)]
    assert tryout["handoff_completed_at"] is None

    # ... and a driver really sees it, through the endpoint the staff bot polls.
    pool = client.get(POOL, headers=driver_auth_headers)
    assert pool.status_code == 200, pool.get_data(as_text=True)
    pooled = [t for t in pool.get_json()["data"]["items"] if t["tryout_number"] == tryout["tryout_number"]]
    assert len(pooled) == 1 and pooled[0]["task_type"] == "handoff"

    # The outlet moved to `trial` and says WHY (spec *Try-out from the field*) -- and the reply
    # ITSELF carries the moved card, the same shape `close`/`abandon` answer with. This is what
    # lets the bot draw the receipt's outlet without a second request; a reply carrying only the
    # try-out would leave the agent looking at a card that still says `prospect`.
    assert body["outlet"]["stage"] == "trial"
    assert body["outlet"]["id"] == outlet["id"]
    card = client.get(f"{OUTLETS}/{outlet['id']}", headers=sales_agent_auth_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    # The same answer, not merely a similar one: one shaper (`OutletService.card`), so a
    # re-fetch can never tell the agent something the receipt did not.
    assert card.get_json()["data"]["outlet"] == body["outlet"]
    history = OutletStageHistory.query.filter_by(outlet_id=outlet["id"]).order_by(OutletStageHistory.id).all()
    assert [(row.from_stage, row.to_stage, row.reason_code) for row in history] == [
        (None, "prospect", "created"),
        ("prospect", "trial", "tryout"),
    ]
    assert history[-1].actor_user_id == sales_agent_user.id

    logged = StaffActivityLog.query.filter_by(action="agent_tryout_created").all()
    assert len(logged) == 1
    assert logged[0].user_id == sales_agent_user.id
    assert logged[0].entity_type == "tryout" and logged[0].entity_id == tryout["id"]
    assert logged[0].metadata_ == {"outlet_id": outlet["id"], "stage": "trial", "item_count": 1}


def test_a_tryout_needs_the_outlets_contact_phone(client, db, sales_agent_auth_headers, sample_product):
    """Spec *Failure modes*: "Try-out for a prospect without a phone -> SALES_TRYOUT_PHONE_REQUIRED".

    Try-outs upsert their contact BY PHONE (`TryoutService._get_or_create_contact`), so a
    phone-less shop cannot have one at all. Refused where the agent can still walk to the counter
    and ask, with the code Task 7 renders as "add a phone to this outlet first".
    """
    outlet = _prospect(
        client,
        sales_agent_auth_headers,
        payload={**BOT_PAYLOAD, "contact": {"name": "Olim aka", "role": "owner"}},
    )

    refused = client.post(
        _tryouts(outlet["id"]),
        json={"items": [{"product_id": sample_product.id, "quantity": 1}]},
        headers=sales_agent_auth_headers,
    )

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_TRYOUT_PHONE_REQUIRED"
    # Nothing was written: no try-out, no hand-off task for a driver to drive to, and the outlet
    # is exactly where the agent left it.
    assert ProductTryout.query.count() == 0
    assert Outlet.query.get(outlet["id"]).stage == "prospect"


def test_an_empty_or_unusable_basket_is_refused_with_one_code_the_bot_can_branch_on(
    client, db, sales_agent_auth_headers, sample_product
):
    """Four ways a basket is not a basket, one code.

    The product rule is `TryoutService._validate_and_build_items` -- the same one the admin page
    and the driver bot are held to -- so an inactive or non-eligible SKU is refused here for the
    reason it is refused there, not by a second opinion written for sales.
    """
    outlet = _prospect(client, sales_agent_auth_headers)

    empty = client.post(_tryouts(outlet["id"]), json={"items": []}, headers=sales_agent_auth_headers)
    assert empty.status_code == 400, empty.get_data(as_text=True)
    assert empty.get_json()["error_code"] == "SALES_TRYOUT_ITEMS_INVALID"

    absent = client.post(_tryouts(outlet["id"]), json={}, headers=sales_agent_auth_headers)
    assert absent.status_code == 400, absent.get_data(as_text=True)
    assert absent.get_json()["error_code"] == "SALES_TRYOUT_ITEMS_INVALID"

    unknown = client.post(
        _tryouts(outlet["id"]),
        json={"items": [{"product_id": 9_999_999, "quantity": 1}]},
        headers=sales_agent_auth_headers,
    )
    assert unknown.status_code == 400, unknown.get_data(as_text=True)
    assert unknown.get_json()["error_code"] == "SALES_TRYOUT_ITEMS_INVALID"

    sample_product.is_tryout_eligible = False
    db.session.commit()
    ineligible = client.post(
        _tryouts(outlet["id"]),
        json={"items": [{"product_id": sample_product.id, "quantity": 1}]},
        headers=sales_agent_auth_headers,
    )
    assert ineligible.status_code == 400, ineligible.get_data(as_text=True)
    assert ineligible.get_json()["error_code"] == "SALES_TRYOUT_ITEMS_INVALID"

    assert ProductTryout.query.count() == 0
    assert Outlet.query.get(outlet["id"]).stage == "prospect"


def test_a_basket_line_is_bounded_by_the_ceiling_the_order_door_already_enforces(
    client, app, db, sales_agent_auth_headers, sample_product
):
    """`effective_line_qty_max()` bounds a try-out line exactly as it bounds an order line.

    The ceiling lives in `TryoutService._validate_and_build_items`, the one rule all three
    try-out doors share, so this route inherits it rather than restating it -- and the refusal
    arrives under `SALES_TRYOUT_ITEMS_INVALID`, the code the bot branches on to land the agent
    back on the basket.
    """
    outlet = _prospect(client, sales_agent_auth_headers)
    with app.app_context():
        over_cap = effective_line_qty_max() + 1

    refused = client.post(
        _tryouts(outlet["id"]),
        json={"items": [{"product_id": sample_product.id, "quantity": over_cap}]},
        headers=sales_agent_auth_headers,
    )

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_TRYOUT_ITEMS_INVALID"
    assert ProductTryout.query.count() == 0
    assert Outlet.query.get(outlet["id"]).stage == "prospect"


def test_a_tryout_on_a_live_outlet_links_without_moving_the_stage(
    client, db, sales_agent_auth_headers, sample_product
):
    """R9: only `prospect` and `activation_requested` become `trial`.

    A shop that already buys water does not go back to being a trial because the agent left a
    sample of a second SKU -- and `trial` is a DUE stage, so a spurious move would also republish
    the outlet's next-visit date off the wrong rule.
    """
    outlet = _prospect(client, sales_agent_auth_headers)
    row = Outlet.query.get(outlet["id"])
    # Stage set directly: the approval path that normally sets it is driven end to end in
    # tests/integration/test_staff_sales_outlets_api.py, and replaying it here would only
    # re-test somebody else's route.
    row.stage = "active"
    db.session.commit()
    history_before = OutletStageHistory.query.filter_by(outlet_id=row.id).count()

    response = client.post(
        _tryouts(row.id),
        json={"items": [{"product_id": sample_product.id, "quantity": 1}]},
        headers=sales_agent_auth_headers,
    )

    assert response.status_code == 201, response.get_data(as_text=True)
    body = response.get_json()["data"]
    assert body["tryout"]["outlet_id"] == row.id
    # The card in the reply says what the row says: `active`, untouched. A receipt that
    # announced `trial` here would be the bot's only source for the screen it draws next.
    assert body["outlet"]["stage"] == "active"
    assert Outlet.query.get(row.id).stage == "active"
    assert OutletStageHistory.query.filter_by(outlet_id=row.id).count() == history_before


def test_only_the_outlets_own_agent_may_leave_a_tryout(
    client, app, db, sales_agent_auth_headers, driver_auth_headers, sample_product
):
    """The ownership rule is `OutletService.get_for_agent`, asked ON THIS ROUTE.

    A try-out books a driver's time and a crate of bottles against a shop; an agent who is
    neither assigned to it nor its onboarder, and whose districts do not cover it, gets the same
    403 every other outlet route gives -- and the route stays agent-only.
    """
    outlet = _prospect(client, sales_agent_auth_headers)
    other = make_sales_agent_user(db, phone="+998901234582", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    db.session.commit()
    with app.app_context():
        other_headers = {
            "Authorization": f"Bearer {create_access_token(identity=str(other.id))}",
            "Content-Type": "application/json",
        }

    body = {"items": [{"product_id": sample_product.id, "quantity": 1}]}
    poached = client.post(_tryouts(outlet["id"]), json=body, headers=other_headers)

    assert poached.status_code == 403, poached.get_data(as_text=True)
    assert poached.get_json()["error_code"] == "SALES_OUTLET_NOT_ASSIGNED"
    assert client.post(_tryouts(outlet["id"]), json=body, headers=driver_auth_headers).status_code == 403
    assert ProductTryout.query.count() == 0
