"""/api/v1/admin/sales/outlets/<id>/contacts -- D30, driven the way the drawer's Contacts tab calls it."""
import pytest

from business_app.models.sales import Outlet, OutletContact, SalesAgentProfile
from business_app.models.user import User
from business_app.services.sales.outlet_service import OutletService
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

URL = "/api/v1/admin/sales/outlets"


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


@pytest.fixture
def outlet(db, agent):
    """GROCERY's outlet: one primary contact, Olim aka +998901112266."""
    return OutletService.create(agent.id, dict(GROCERY))


def _contacts(client, headers, outlet_id):
    body = client.get(f"{URL}/{outlet_id}", headers=headers).get_json()["data"]["outlet"]
    return [(c["name"], c["phone"], c["role"], c["is_primary"]) for c in body["contacts"]]


def test_an_admin_adds_a_second_contact_that_is_not_primary(client, admin_claim_headers, outlet):
    added = client.post(
        f"{URL}/{outlet.id}/contacts",
        json={"name": "Zafar", "phone": "+998 90 111 22 99", "role": "receiver", "presence_window": "09-13"},
        headers=admin_claim_headers,
    )

    assert added.status_code == 201, added.get_data(as_text=True)
    assert added.get_json()["data"]["contact"]["phone"] == "+998901112299"
    assert _contacts(client, admin_claim_headers, outlet.id) == [
        ("Olim aka", "+998901112266", "owner", True),
        ("Zafar", "+998901112299", "receiver", False),
    ]


def test_an_admin_corrects_a_contact_and_bad_values_are_refused(client, admin_claim_headers, outlet):
    contact_id = outlet.contacts[0].id

    fixed = client.put(f"{URL}/{outlet.id}/contacts/{contact_id}",
                       json={"phone": "+998901112277", "role": "decision_maker"}, headers=admin_claim_headers)
    bad_phone = client.put(f"{URL}/{outlet.id}/contacts/{contact_id}", json={"phone": "12"}, headers=admin_claim_headers)
    bad_role = client.put(f"{URL}/{outlet.id}/contacts/{contact_id}", json={"role": "cousin"}, headers=admin_claim_headers)

    assert fixed.status_code == 200, fixed.get_data(as_text=True)
    assert bad_phone.status_code == 400 and bad_phone.get_json()["error_code"] == "SALES_CONTACT_PHONE_INVALID"
    assert bad_role.status_code == 400 and bad_role.get_json()["error_code"] == "SALES_CONTACT_ROLE_INVALID"
    assert _contacts(client, admin_claim_headers, outlet.id) == [("Olim aka", "+998901112277", "decision_maker", True)]


def test_making_a_contact_primary_moves_the_one_flag(client, admin_claim_headers, outlet):
    second = client.post(f"{URL}/{outlet.id}/contacts", json={"name": "Zafar", "phone": "+998901112299"},
                         headers=admin_claim_headers).get_json()["data"]["contact"]

    moved = client.put(f"{URL}/{outlet.id}/contacts/{second['id']}", json={"is_primary": True}, headers=admin_claim_headers)

    assert moved.status_code == 200, moved.get_data(as_text=True)
    assert [row[3] for row in _contacts(client, admin_claim_headers, outlet.id)] == [False, True]


def test_the_primary_flag_cannot_simply_be_switched_off(client, admin_claim_headers, outlet):
    refused = client.put(f"{URL}/{outlet.id}/contacts/{outlet.contacts[0].id}", json={"is_primary": False},
                         headers=admin_claim_headers)

    assert refused.status_code == 400
    assert refused.get_json()["error_code"] == "SALES_CONTACT_PRIMARY_REQUIRED"


def test_deleting_the_primary_promotes_the_oldest_remaining(client, admin_claim_headers, outlet):
    for name, phone in (("Zafar", "+998901112299"), ("Kamola", "+998901112288")):
        client.post(f"{URL}/{outlet.id}/contacts", json={"name": name, "phone": phone}, headers=admin_claim_headers)

    deleted = client.delete(f"{URL}/{outlet.id}/contacts/{outlet.contacts[0].id}", headers=admin_claim_headers)

    assert deleted.status_code == 200, deleted.get_data(as_text=True)
    assert _contacts(client, admin_claim_headers, outlet.id) == [
        ("Zafar", "+998901112299", "owner", True),
        ("Kamola", "+998901112288", "owner", False),
    ]


def test_with_no_contact_left_approval_asks_for_a_phone(client, admin_claim_headers, db, agent, outlet):
    OutletService.request_activation(outlet, agent.id)

    client.delete(f"{URL}/{outlet.id}/contacts/{outlet.contacts[0].id}", headers=admin_claim_headers)
    approved = client.post(f"{URL}/{outlet.id}/approve", json={}, headers=admin_claim_headers)

    assert OutletContact.query.filter_by(outlet_id=outlet.id).count() == 0
    assert approved.status_code == 400, approved.get_data(as_text=True)
    assert approved.get_json()["error_code"] == "SALES_ACTIVATION_PHONE_REQUIRED"


def test_a_contact_of_another_outlet_is_not_found(client, admin_claim_headers, db, agent, outlet):
    other = Outlet(name="Navruz market", outlet_type="grocery_store", stage="prospect", district="chilanzar")
    db.session.add(other)
    db.session.commit()

    missing = client.put(f"{URL}/{other.id}/contacts/{outlet.contacts[0].id}", json={"name": "X"},
                         headers=admin_claim_headers)

    assert missing.status_code == 404 and missing.get_json()["error_code"] == "SALES_CONTACT_NOT_FOUND"


def test_editing_a_contact_never_touches_the_customer_account(client, admin_claim_headers, agent, outlet):
    """D30: the contact list is the agent's address book, not the customer's login."""
    OutletService.request_activation(outlet, agent.id)
    assert client.post(f"{URL}/{outlet.id}/approve", json={}, headers=admin_claim_headers).status_code == 200
    account = User.query.get(Outlet.query.get(outlet.id).user_id)
    login_phone = account.phone

    client.put(f"{URL}/{outlet.id}/contacts/{outlet.contacts[0].id}", json={"phone": "+998901112277"},
               headers=admin_claim_headers)

    assert User.query.get(account.id).phone == login_phone


def test_contacts_are_for_managers_only(client, sales_agent_auth_headers, outlet):
    assert client.post(f"{URL}/{outlet.id}/contacts", json={"name": "X"}, headers=sales_agent_auth_headers).status_code == 403
