"""Nobody approves an outlet they onboarded (C5; spec §4.12, §5.5), on any approval path.

`OutletService.is_self_approval` is the ONE expression of the rule and `can_approve` its check-mode
twin. Every approval path asks the first; every surface that draws an Approve or Convert button
publishes the second, so no client re-derives it (S-8, S-9):
- the operator's bot route `POST /staff/sales/outlets/<id>/approve`, with and without `attach`;
- the admin drawer's `POST /admin/sales/outlets/<id>/approve`;
- `POST /admin/tryouts/<id>/convert`, which becomes the linked outlet's first activation;
- the operator queue, both outlet cards and both try-out reads.

The refusal lands before `approve`'s first step, so a refused call leaves no customer account,
address or contract behind. The onboarders are real dual-role users (an operator, an admin or a
manager who also holds a sales-agent profile): the only people the rule can bite. T-SELF-5, whose
verdicts are outlet checks, lives in tests/integration/test_sales_pay_new_outlet_bonus.py; the
notification half of T-SELF-4 is in tests/unit/test_sales_agent_tasks.py.

Spec: docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md (§4.12, §5.5, T-SELF-1..4).
"""

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.corporate import CorporateContract
from business_app.models.sales import OUTLET_STAGES, Outlet, SalesAgentProfile
from business_app.models.tryout import ProductTryout
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import APPROVABLE_STAGES, OutletService
from business_app.services.tryout_service import TryoutService
from shared.enums import EntitySubtype, TryoutOutcome, UserRole
from tests.integration.test_staff_sales_outlets_api import BOT_PAYLOAD, OUTLETS
from tests.unit.test_outlet_dedupe import FAR, NEAR, PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

ADMIN_OUTLETS = "/api/v1/admin/sales/outlets"
ACTIVATION_QUEUE = "/api/v1/staff/sales/activation-requests"
ADMIN_TRYOUTS = "/api/v1/admin/tryouts"
# The admin approve modal's exact body: `salesService.approveOutlet` always sends both keys.
ADMIN_APPROVE_BODY = {"contract_number": None, "attach": False}


def _onboarder(db, phone, role, staff_roles):
    """A user who registers outlets (a sales-agent profile) and ALSO holds approval power."""
    user = make_sales_agent_user(db, phone=phone, role=role, staff_roles=staff_roles)
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def _headers(app, user):
    """One token for both route families: the staff routes read the users row, the admin routes
    read the role CLAIM and cross-check the row."""
    role = user.role.value if hasattr(user.role, "value") else user.role
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims={"role": role})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _requested(client, headers, *, name, phone, pin):
    """A shop registered and sent for activation through the bot's own routes; returns its id."""
    payload = {
        **BOT_PAYLOAD,
        "name": name,
        "contact": {"name": "Olim aka", "phone": phone, "role": "owner"},
        "latitude": pin[0],
        "longitude": pin[1],
    }
    created = client.post(OUTLETS, json=payload, headers=headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    outlet_id = created.get_json()["data"]["outlet"]["id"]
    requested = client.post(f"{OUTLETS}/{outlet_id}/request-activation", headers=headers)
    assert requested.status_code == 200, requested.get_data(as_text=True)
    return outlet_id


def _estate():
    """What a half-run approval leaves behind: the customer, its address and a grocery's contract."""
    return User.query.count(), UserAddress.query.count(), CorporateContract.query.count()


def _assert_self_approval_refused(response, outlet_id):
    assert response.status_code == 403, response.get_data(as_text=True)
    body = response.get_json()
    assert body["error_code"] == "SALES_OUTLET_SELF_APPROVAL"
    assert body["details"] == {"outlet_id": outlet_id}


@pytest.mark.parametrize("stage", OUTLET_STAGES)
def test_can_approve_is_an_approvable_stage_and_a_viewer_who_is_not_the_onboarder(app, stage):
    """S-8 and S-9 as one truth table. The spec's three stages are spelled out, so a drift in
    `APPROVABLE_STAGES` shows against the ruling rather than against itself."""
    assert APPROVABLE_STAGES == ("activation_requested", "prospect", "trial")
    outlet = Outlet(name="Oasis market", outlet_type="grocery_store", stage=stage, onboarded_by_user_id=41)
    imported = Outlet(name="Imported", outlet_type="grocery_store", stage=stage, onboarded_by_user_id=None)

    assert OutletService.can_approve(outlet, 7) is (stage in ("activation_requested", "prospect", "trial"))
    assert OutletService.can_approve(outlet, 41) is False
    # A JWT identity arrives as a string; an imported outlet has no onboarder; a read with no
    # viewer has nobody to be the onboarder.
    assert OutletService.is_self_approval(outlet, "41") is True
    assert OutletService.is_self_approval(imported, 41) is False
    assert OutletService.is_self_approval(outlet, None) is False
    # A try-out with no outlet (every admin- and operator-made one) has no onboarder to refuse.
    assert TryoutService.can_convert(ProductTryout(outlet_id=None), 41) is True


def test_t_self_1_a_dual_role_operator_is_refused_on_the_bot_path_with_and_without_attach(
    client, app, db, operator_auth_headers
):
    """T-SELF-1. An operator who is also a sales agent taps Approve, then Attach, on a shop they
    registered themselves.

    Both taps are refused before the customer step, so the estate is exactly as it was: the plain
    approve would have minted a customer, an address and (a grocery store) an AMOUNT contract.
    When another operator approves it, the ordinary path runs and writes all three. A retry by
    the onboarder afterwards is the idempotent no-op it always was: the refusal sits AFTER the
    `active` early return (§4.12).
    """
    onboarder = _onboarder(db, "+998901239101", UserRole.OPERATOR, ["operator", "sales_agent"])
    headers = _headers(app, onboarder)
    outlet_id = _requested(client, headers, name="Oasis market", phone="+998901239201", pin=PIN)
    before = _estate()

    for body in ({}, {"attach": True}):
        _assert_self_approval_refused(
            client.post(f"{OUTLETS}/{outlet_id}/approve", json=body, headers=headers), outlet_id
        )

    row = db.session.get(Outlet, outlet_id)
    assert (row.stage, row.user_id, row.address_id, row.approved_by_user_id) == (
        "activation_requested",
        None,
        None,
        None,
    )
    assert _estate() == before

    approved = client.post(f"{OUTLETS}/{outlet_id}/approve", json={}, headers=operator_auth_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["outlet"]["stage"] == "active"
    assert _estate() == (before[0] + 1, before[1] + 1, before[2] + 1)

    again = client.post(f"{OUTLETS}/{outlet_id}/approve", json={}, headers=headers)
    assert again.status_code == 200, again.get_data(as_text=True)
    assert again.get_json()["data"]["outlet"]["stage"] == "active"
    assert _estate() == (before[0] + 1, before[1] + 1, before[2] + 1)


@pytest.mark.parametrize(
    ("role", "phone"),
    [(UserRole.ADMIN, "+998901239102"), (UserRole.MANAGER, "+998901239103")],
    ids=["admin", "manager"],
)
def test_t_self_2_an_admin_or_manager_who_onboarded_is_refused_on_the_admin_path(
    client, app, db, admin_claim_headers, role, phone
):
    """T-SELF-2. An admin or a manager with an agent profile registers a shop through the staff
    route, then approves it from the admin drawer, plain or as an attach: 403, nothing written.

    The spec's "another operator ⇒ 200" cannot reach this route (`manager_or_higher_required`);
    the operator's twin is T-SELF-1's staff route. Here another ADMIN approves the same outlet.
    """
    onboarder = _onboarder(db, phone, role, ["sales_agent"])
    headers = _headers(app, onboarder)
    outlet_id = _requested(client, headers, name="Navruz market", phone="+998901239202", pin=PIN)
    before = _estate()

    for body in (ADMIN_APPROVE_BODY, {**ADMIN_APPROVE_BODY, "attach": True}):
        _assert_self_approval_refused(
            client.post(f"{ADMIN_OUTLETS}/{outlet_id}/approve", json=body, headers=headers), outlet_id
        )
    assert db.session.get(Outlet, outlet_id).stage == "activation_requested"
    assert _estate() == before

    approved = client.post(f"{ADMIN_OUTLETS}/{outlet_id}/approve", json=ADMIN_APPROVE_BODY, headers=admin_claim_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert approved.get_json()["data"]["outlet"]["stage"] == "active"


def test_t_self_3_no_surface_offers_the_onboarder_an_approve_button(
    client, app, db, sales_agent_auth_headers, operator_auth_headers, admin_claim_headers
):
    """T-SELF-3. `can_approve` is published per viewer, from the predicate the refusal raises on:
    the operator queue (a dual-role operator still SEES their own request, so they know it exists,
    without the button), the staff outlet card and the admin drawer. An outlet in a stage nothing
    can be approved from offers the button to nobody."""
    dual = _onboarder(db, "+998901239104", UserRole.OPERATOR, ["operator", "sales_agent"])
    dual_headers = _headers(app, dual)
    own = _requested(client, dual_headers, name="Oasis market", phone="+998901239203", pin=PIN)
    other = _requested(client, sales_agent_auth_headers, name="Baraka", phone="+998901239204", pin=FAR)

    def _queue(headers):
        response = client.get(ACTIVATION_QUEUE, headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        return {row["id"]: row["can_approve"] for row in response.get_json()["data"]["items"]}

    assert _queue(dual_headers) == {own: False, other: True}
    assert _queue(operator_auth_headers) == {own: True, other: True}

    card = client.get(f"{OUTLETS}/{own}", headers=dual_headers)
    assert card.status_code == 200, card.get_data(as_text=True)
    assert card.get_json()["data"]["outlet"]["can_approve"] is False

    admin_agent = _onboarder(db, "+998901239105", UserRole.ADMIN, ["sales_agent"])
    admin_agent_headers = _headers(app, admin_agent)
    mine = _requested(client, admin_agent_headers, name="Navruz market", phone="+998901239205", pin=NEAR)

    def _drawer(headers):
        response = client.get(f"{ADMIN_OUTLETS}/{mine}", headers=headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["data"]["outlet"]["can_approve"]

    assert _drawer(admin_agent_headers) is False
    assert _drawer(admin_claim_headers) is True

    approved = client.post(f"{ADMIN_OUTLETS}/{mine}/approve", json=ADMIN_APPROVE_BODY, headers=admin_claim_headers)
    assert approved.status_code == 200, approved.get_data(as_text=True)
    assert _drawer(admin_claim_headers) is False


def test_t_self_4_the_onboarder_cannot_convert_their_outlets_tryout_and_the_reads_say_so(
    client, app, db, admin_claim_headers, sample_product
):
    """T-SELF-4, the try-out half. Converting a field try-out becomes its outlet's first
    activation (`job_tryout_converted`), so it is the same decision as approving the outlet: an
    admin who left the try-out at their own shop is refused, and both admin reads say
    `can_convert: false` to them and `true` to another admin. The refusal creates no customer."""
    admin_agent = _onboarder(db, "+998901239106", UserRole.ADMIN, ["sales_agent"])
    headers = _headers(app, admin_agent)
    contact = {"name": "Olim aka", "phone": "+998901239206", "role": "owner"}
    created = client.post(OUTLETS, json={**BOT_PAYLOAD, "name": "Oasis market", "contact": contact}, headers=headers)
    assert created.status_code == 201, created.get_data(as_text=True)
    outlet_id = created.get_json()["data"]["outlet"]["id"]
    left = client.post(
        f"{OUTLETS}/{outlet_id}/tryouts",
        json={"items": [{"product_id": sample_product.id, "quantity": 2}]},
        headers=headers,
    )
    assert left.status_code == 201, left.get_data(as_text=True)
    tryout_id = left.get_json()["data"]["tryout"]["id"]

    def _can_convert(viewer_headers):
        one = client.get(f"{ADMIN_TRYOUTS}/{tryout_id}", headers=viewer_headers)
        listed = client.get(ADMIN_TRYOUTS, headers=viewer_headers)
        assert one.status_code == 200, one.get_data(as_text=True)
        assert listed.status_code == 200, listed.get_data(as_text=True)
        rows = {row["id"]: row["can_convert"] for row in listed.get_json()["data"]["items"]}
        return one.get_json()["data"]["tryout"]["can_convert"], rows[tryout_id]

    assert _can_convert(headers) == (False, False)
    assert _can_convert(admin_claim_headers) == (True, True)

    users = User.query.count()
    _assert_self_approval_refused(
        client.post(f"{ADMIN_TRYOUTS}/{tryout_id}/convert", json={}, headers=headers), outlet_id
    )
    tryout = db.session.get(ProductTryout, tryout_id)
    assert (tryout.converted_user_id, tryout.outcome) == (None, TryoutOutcome.PENDING)
    assert User.query.count() == users

    # Another admin converts. The shop's phone already names a grocery customer here, so the
    # conversion LINKS it. Minting a new account from a field try-out is refused on
    # `entity_subtype` with or without this rule (the contact carries the shop's name as its
    # company and the conversion passes no subtype): a gap outside §4.12, reported to the owner.
    shop = _customer(db, contact["phone"], company="Oasis market", subtype=EntitySubtype.GROCERY_STORE)
    converted = client.post(f"{ADMIN_TRYOUTS}/{tryout_id}/convert", json={}, headers=admin_claim_headers)
    assert converted.status_code == 200, converted.get_data(as_text=True)
    conversion = converted.get_json()["data"]["conversion"]
    assert (conversion["action"], conversion["user"]["id"]) == ("linked_existing_user", shop.id)
