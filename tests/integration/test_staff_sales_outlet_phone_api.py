"""PUT /api/v1/staff/sales/outlets/<id>/primary-phone: an agent sets or fixes an outlet's phone (D31.4, R4).

The reported bug, end to end (spec §1): outlet #9 "Abc market" was created with the phone step
skipped, both of its Request-activation taps were refused with SALES_ACTIVATION_PHONE_REQUIRED,
and nothing in the staff bot could add the phone. Driven over HTTP with a real sales-agent token
and the body the bot sends; the operator notification is the only thing replaced, by a recording
spy.

Every outlet the PUT is sent to is built as a ROW (spec §7.1): `OutletService.create` now refuses
a phoneless outlet, and most of the stages under test cannot be reached through create at all.
The last test goes through `POST /sales/outlets` itself, because what it pins is that reply's
`can_set_phone`.
"""
import pytest

from business_app.models.sales import Outlet, OutletContact
from business_app.services.sales import notifications
from tests.integration.test_staff_sales_outlets_api import BOT_PAYLOAD, OUTLETS
from tests.unit.test_outlet_dedupe import PIN
from tests.unit.test_sales_agent_role import make_sales_agent_user
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.integration

E164 = "+998901234567"
OLD_PHONE = "+998901112266"


def _row(db, owner_id, *, stage="prospect", outlet_type="grocery_store", contacts=()):
    """A pinned outlet that `owner_id` registered and owns. `contacts` are `(name, phone, is_primary)`,
    oldest first."""
    outlet = Outlet(
        name="Abc market",
        outlet_type=outlet_type,
        stage=stage,
        latitude=PIN[0],
        longitude=PIN[1],
        assigned_agent_user_id=owner_id,
        onboarded_by_user_id=owner_id,
    )
    for name, phone, is_primary in contacts:
        outlet.contacts.append(OutletContact(name=name, phone=phone, role="owner", is_primary=is_primary))
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _put_phone(client, headers, outlet_id, body):
    return client.put(f"{OUTLETS}/{outlet_id}/primary-phone", json=body, headers=headers)


def _card(client, headers, outlet_id):
    """The card as the bot fetches it before drawing the buttons."""
    response = client.get(f"{OUTLETS}/{outlet_id}", headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["outlet"]


def _stored_contacts(outlet_id):
    """The outlet's contacts as the TABLE holds them, oldest first: `(name, phone, role, is_primary)`."""
    rows = OutletContact.query.filter_by(outlet_id=outlet_id).order_by(OutletContact.id).all()
    return [(row.name, row.phone, row.role, row.is_primary) for row in rows]


@pytest.mark.parametrize(
    "contacts,expected",
    [
        # Both steps skipped: no contact row at all -- outlet #9 on dev.
        pytest.param((), [("Abc market", E164, "owner", True)], id="no-contact"),
        # A name typed, the phone skipped: a PRIMARY contact with no phone. `POST /contacts` could
        # never repair this one -- `add_contact` adds a NON-primary row once a contact exists.
        pytest.param((("Olim aka", None, True),), [("Olim aka", E164, "owner", True)], id="name-only-primary"),
    ],
)
def test_a_prospect_left_without_a_phone_gets_one_and_is_sent_for_activation(
    client, db, monkeypatch, sales_agent_user, sales_agent_auth_headers, contacts, expected
):
    """The agent types the number on the card's phone screen; the reply is the card the bot
    redraws, Request activation is on offer, and the request goes through to the operators."""
    notified = _spy(monkeypatch, notifications, "notify_activation_requested")
    outlet = _row(db, sales_agent_user.id, contacts=contacts)
    activate = f"{OUTLETS}/{outlet.id}/request-activation"

    # The dead end as the agent met it: activation refuses, and the card now offers the phone.
    card = _card(client, sales_agent_auth_headers, outlet.id)
    assert (card["can_set_phone"], card["can_request_activation"]) == (True, False)
    stuck = client.post(activate, headers=sales_agent_auth_headers)
    assert stuck.status_code == 400, stuck.get_data(as_text=True)
    assert stuck.get_json()["error_code"] == "SALES_ACTIVATION_PHONE_REQUIRED"

    saved = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": "90 123 45 67"})

    assert saved.status_code == 200, saved.get_data(as_text=True)
    card = saved.get_json()["data"]["outlet"]
    assert (card["id"], card["stage"]) == (outlet.id, "prospect")
    assert (card["can_set_phone"], card["can_request_activation"]) == (True, True)
    assert [(c["name"], c["phone"], c["role"], c["is_primary"]) for c in card["contacts"]] == expected
    assert _stored_contacts(outlet.id) == expected

    requested = client.post(activate, headers=sales_agent_auth_headers)

    assert requested.status_code == 200, requested.get_data(as_text=True)
    card = requested.get_json()["data"]["outlet"]
    assert card["stage"] == "activation_requested"
    # D31.2: the agent's phone action ends where activation begins, and the reply already says so.
    assert (card["can_set_phone"], card["can_request_activation"]) == (False, False)
    assert [(args[0].id, kwargs) for args, kwargs in notified] == [(outlet.id, {})]


@pytest.mark.parametrize(
    "typed",
    [
        "90 123 45 67",
        "+998 90 123-45-67",
        "998901234567",
        "901234567",
        "+998901234567",  # the bot's own body: `validate_phone` hands it the normalised number
    ],
)
def test_every_everyday_format_is_stored_as_one_e164_number(
    client, db, sales_agent_user, sales_agent_auth_headers, typed
):
    """Review Focus 1: `shared.validators.normalize_phone_number` decides (R2), and the row holds
    E.164 whatever was typed -- the string the customer account will later be opened on."""
    outlet = _row(db, sales_agent_user.id, contacts=[("Olim aka", None, True)])

    saved = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": typed})

    assert saved.status_code == 200, saved.get_data(as_text=True)
    assert _stored_contacts(outlet.id) == [("Olim aka", E164, "owner", True)]


@pytest.mark.parametrize(
    "typed",
    [
        pytest.param("71 123 45 67", id="tashkent-landline"),
        pytest.param("+7 916 123 45 67", id="russian-mobile"),
    ],
)
def test_a_number_that_is_not_an_uzbek_mobile_is_refused_and_nothing_is_written(
    client, db, sales_agent_user, sales_agent_auth_headers, typed
):
    """R2: the phone becomes the customer's login at approval, so a landline or a foreign number is
    refused HERE, where the agent can retype it, rather than at approval."""
    outlet = _row(db, sales_agent_user.id, contacts=[("Olim aka", None, True)])

    refused = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": typed})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_CONTACT_PHONE_INVALID"
    assert _stored_contacts(outlet.id) == [("Olim aka", None, "owner", True)]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no-phone-key"),
        pytest.param({"phone": None}, id="null"),
        pytest.param({"phone": ""}, id="empty"),
        pytest.param({"phone": "   "}, id="blank"),
    ],
)
def test_a_missing_phone_is_the_coded_phone_required_refusal(
    client, db, sales_agent_user, sales_agent_auth_headers, body
):
    """`SetPrimaryPhonePayload.phone` is Optional on purpose: a pydantic-required field answers an
    UNCODED 400, which the bot can only show as generic copy. The service refuses with a code, and
    BEFORE it would create the contact an outlet without one gets: nothing is written. A blank
    phone gets the answer `POST /sales/outlets` gives it: both doors ask `_require_primary_phone`."""
    outlet = _row(db, sales_agent_user.id)

    refused = _put_phone(client, sales_agent_auth_headers, outlet.id, body)

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_OUTLET_PHONE_REQUIRED"
    assert _stored_contacts(outlet.id) == []


def test_the_same_put_twice_answers_200_twice_and_leaves_one_contact(
    client, db, sales_agent_user, sales_agent_auth_headers
):
    """Review Focus 2: the staff client retries a PUT after an ambiguous transport failure, and an
    agent can double-tap. The first PUT creates the contact an outlet with none lacks; the second
    lands on that same row."""
    outlet = _row(db, sales_agent_user.id)

    first = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": E164})
    second = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": E164})

    assert (first.status_code, second.status_code) == (200, 200), second.get_data(as_text=True)
    assert _stored_contacts(outlet.id) == [("Abc market", E164, "owner", True)]
    # The same row, id and all: the retry changed nothing the card shows.
    assert second.get_json()["data"]["outlet"]["contacts"] == first.get_json()["data"]["outlet"]["contacts"]


def test_a_legacy_outlet_whose_contacts_carry_no_primary_flag_gets_the_phone_on_its_first(
    client, db, monkeypatch, sales_agent_user, sales_agent_auth_headers
):
    """Review Focus 3: with no flagged row, `Outlet.primary_contact` falls back to `contacts[0]` --
    the contact activation, approval and the try-out already read. The phone lands THERE, the flag
    is written down so exactly one contact is primary, and activation then passes."""
    notified = _spy(monkeypatch, notifications, "notify_activation_requested")
    outlet = _row(
        db, sales_agent_user.id, contacts=[("Olim aka", None, False), ("Malika", "+998901112277", False)]
    )

    saved = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": "90 123 45 67"})
    requested = client.post(f"{OUTLETS}/{outlet.id}/request-activation", headers=sales_agent_auth_headers)

    assert saved.status_code == 200, saved.get_data(as_text=True)
    assert _stored_contacts(outlet.id) == [
        ("Olim aka", E164, "owner", True),
        ("Malika", "+998901112277", "owner", False),
    ]
    assert requested.status_code == 200, requested.get_data(as_text=True)
    assert requested.get_json()["data"]["outlet"]["stage"] == "activation_requested"
    assert [args[0].id for args, _kwargs in notified] == [outlet.id]


@pytest.mark.parametrize("stage,outlet_type", [("prospect", "grocery_store"), ("trial", "workplace")])
def test_a_prospect_or_trial_shop_or_workplace_takes_a_corrected_phone(
    client, db, sales_agent_user, sales_agent_auth_headers, stage, outlet_type
):
    """R4 (D31.2): the phone can be set or FIXED before activation -- an operator's rejection sends
    a wrong number back to `prospect`. The card offers it (`can_set_phone`), the PUT agrees, the
    name stays and the stage does not move."""
    outlet = _row(
        db, sales_agent_user.id, stage=stage, outlet_type=outlet_type, contacts=[("Olim aka", OLD_PHONE, True)]
    )

    assert _card(client, sales_agent_auth_headers, outlet.id)["can_set_phone"] is True
    saved = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": E164})

    assert saved.status_code == 200, saved.get_data(as_text=True)
    assert saved.get_json()["data"]["outlet"]["stage"] == stage
    assert _stored_contacts(outlet.id) == [("Olim aka", E164, "owner", True)]


@pytest.mark.parametrize(
    "stage,outlet_type",
    [
        ("activation_requested", "grocery_store"),
        ("active", "workplace"),
        ("at_risk", "grocery_store"),
        ("dormant", "workplace"),
        ("lost", "grocery_store"),
        ("prospect", "individual"),
        ("trial", "individual"),
    ],
)
def test_a_stale_card_is_refused_with_the_stage_code_and_nothing_is_written(
    client, db, sales_agent_user, sales_agent_auth_headers, stage, outlet_type
):
    """Review Focus 4, backend half: the outlet moved on while the card sat open (after activation
    the phone is the customer's login, an admin's to change), or it is a private customer. The card
    no longer offers the button, and a PUT from the stale screen is the coded 400, writing nothing."""
    outlet = _row(
        db, sales_agent_user.id, stage=stage, outlet_type=outlet_type, contacts=[("Olim aka", OLD_PHONE, True)]
    )

    assert _card(client, sales_agent_auth_headers, outlet.id)["can_set_phone"] is False
    refused = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": E164})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_OUTLET_STAGE_INVALID"
    assert _stored_contacts(outlet.id) == [("Olim aka", OLD_PHONE, "owner", True)]
    assert Outlet.query.get(outlet.id).stage == stage


def test_a_stale_card_is_told_the_stage_before_the_number_is_checked(
    client, db, sales_agent_user, sales_agent_auth_headers
):
    """P5's order: the outlet gate runs before the number is read. A landline sent from a stale
    card gets the stage refusal, which ends the screen, not the number's, which would only
    ask the agent to retype into an outlet that no longer takes a phone."""
    outlet = _row(db, sales_agent_user.id, stage="active", contacts=[("Olim aka", OLD_PHONE, True)])

    refused = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": "71 123 45 67"})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_OUTLET_STAGE_INVALID"
    assert _stored_contacts(outlet.id) == [("Olim aka", OLD_PHONE, "owner", True)]


def test_an_outlet_owned_by_another_agent_is_refused_and_nothing_is_written(
    client, db, sales_agent_auth_headers
):
    """Review Focus 4: reassigned while the card sat open. `get_for_agent` is the route's scope,
    exactly as request-activation's, so the answer is 403 SALES_OUTLET_NOT_ASSIGNED."""
    other = make_sales_agent_user(db, phone="+998901234578", staff_roles=["sales_agent"])
    outlet = _row(db, other.id, contacts=[("Olim aka", None, True)])

    refused = _put_phone(client, sales_agent_auth_headers, outlet.id, {"phone": E164})

    assert refused.status_code == 403, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_OUTLET_NOT_ASSIGNED"
    assert _stored_contacts(outlet.id) == [("Olim aka", None, "owner", True)]


def test_an_unauthenticated_put_never_reaches_the_service(app, db, sales_agent_user):
    """A FRESH client: the session-scoped `client` leaks JWT cookies into 401 tests on this repo."""
    outlet = _row(db, sales_agent_user.id, contacts=[("Olim aka", None, True)])

    response = app.test_client().put(f"{OUTLETS}/{outlet.id}/primary-phone", json={"phone": E164})

    assert response.status_code == 401, response.get_data(as_text=True)
    assert _stored_contacts(outlet.id) == [("Olim aka", None, "owner", True)]


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({}, ("prospect", True)),
        ({"name": "Yashnobod ofis", "outlet_type": "workplace", "class": "A"}, ("prospect", True)),
        # A private customer is activated at creation, on this very phone: nothing is left to set or
        # to request.
        (
            {
                "name": "Dilnoza opa",
                "outlet_type": "individual",
                "class": "C",
                "contact": {"name": "Dilnoza Rahimova", "phone": "+998901112299", "role": "owner"},
            },
            ("active", False),
        ),
    ],
    ids=["grocery_store", "workplace", "individual"],
)
def test_the_create_reply_publishes_can_set_phone(
    client, db, sales_agent_auth_headers, overrides, expected
):
    """Spec §7.1, the last create row: the bot draws the walk-in receipt's buttons from this reply
    (P6), so the reply must already say whether the phone button is on offer. Posted with every
    key `new_outlet.py:_payload()` sends; `(stage, can_set_phone)` per type on the bot's type
    keyboard. `can_request_activation` on the same reply is Task 2's
    `test_create_replies_with_the_card` -- one test per fact."""
    created = client.post(
        OUTLETS,
        json={**BOT_PAYLOAD, "force": False, "link_user_id": None, **overrides},
        headers=sales_agent_auth_headers,
    )

    assert created.status_code == 201, created.get_data(as_text=True)
    card = created.get_json()["data"]["outlet"]
    assert (card["stage"], card["can_set_phone"]) == expected
