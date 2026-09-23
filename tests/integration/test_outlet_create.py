"""OutletService.create: prospects, duplicate refusal, forced creation, linking, individual auto-activation."""
import pytest

from business_app.models.sales import Outlet, OutletStageHistory
from business_app.models.user import User, UserAddress
from business_app.services.auth_service import TELEGRAM_SELF_LINK_SOURCES, AuthService
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.exceptions import ConflictError, ValidationError
from shared.enums import EntitySubtype, UserRole, UserType
from tests.unit.test_outlet_dedupe import FAR, NEAR, PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

MID = (41.3121, 69.2797)          # ~111 m from PIN: inside SALES_DEDUPE_RADIUS_M, farther than NEAR

GROCERY = {
    "name": "Bahor market",
    "outlet_type": "grocery_store",
    "channel": "dokon",
    "contact": {"name": "Olim aka", "phone": "+998901112266", "role": "owner"},
    "latitude": PIN[0],
    "longitude": PIN[1],
    "address_text": "Chilonzor 5-kvartal, 12",
    "district": "chilanzar",
    "outlet_class": "B",
    "notes": "Corner shop",
}


@pytest.fixture
def agent(db):
    return make_sales_agent_user(db, staff_roles=["sales_agent"])


def _history(outlet):
    rows = OutletStageHistory.query.filter_by(outlet_id=outlet.id).order_by(OutletStageHistory.id).all()
    return [(h.from_stage, h.to_stage, h.reason_code) for h in rows]


def test_create_prospect_records_contact_history_and_attribution(db, agent):
    outlet = OutletService.create(agent.id, dict(GROCERY))

    assert outlet.stage == "prospect"
    assert outlet.assigned_agent_user_id == agent.id and outlet.onboarded_by_user_id == agent.id
    assert outlet.outlet_class == "B" and outlet.district == "chilanzar"
    assert outlet.primary_contact.phone == "+998901112266"
    history = OutletStageHistory.query.filter_by(outlet_id=outlet.id).all()
    assert [(h.from_stage, h.to_stage, h.reason_code, h.actor_user_id) for h in history] == [(None, "prospect", "created", agent.id)]


def test_duplicate_is_refused_unless_forced_and_candidates_are_kept(db, agent):
    OutletService.create(agent.id, dict(GROCERY))
    again = {**GROCERY, "latitude": NEAR[0], "longitude": NEAR[1], "contact": {"name": "Other", "phone": None}}

    with pytest.raises(ConflictError) as excinfo:
        OutletService.create(agent.id, dict(again))
    assert excinfo.value.error_code == "SALES_OUTLET_DUPLICATE"
    assert excinfo.value.details["candidates"][0]["reason"] == "name_nearby"

    forced = OutletService.create(agent.id, dict(again), force=True)
    assert forced.dedupe_candidates[0]["reason"] == "name_nearby"
    assert Outlet.query.count() == 2


def test_link_existing_grocery_customer_makes_the_outlet_active(db, agent):
    customer = _customer(db, "+998901112277", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    address = UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    db.session.add(address)
    db.session.commit()

    outlet = OutletService.create(agent.id, {**GROCERY, "contact": None}, link_user_id=customer.id)

    assert outlet.user_id == customer.id and outlet.address_id == address.id and outlet.stage == "active"
    assert _history(outlet) == [(None, "prospect", "created"), ("prospect", "active", "linked")]

    # R39 (ii), the branch case: branch #2 of the same chain, pinned ~4 km from every address
    # the account has. Nothing is within SALES_DEDUPE_RADIUS_M of its pin, so it is neither a
    # same-place duplicate nor an adoption — it gets a non-default address of its own.
    branch = OutletService.create(
        agent.id,
        {
            **GROCERY,
            "name": "Bahor market, Yunusobod",
            "contact": None,
            "latitude": FAR[0],
            "longitude": FAR[1],
            "address_text": "Yunusobod 19-kvartal, 4",
        },
        link_user_id=customer.id,
    )

    assert branch.id != outlet.id and branch.user_id == customer.id and branch.stage == "active"
    assert _history(branch) == [(None, "prospect", "created"), ("prospect", "active", "linked")]
    branch_address = UserAddress.query.get(branch.address_id)
    assert branch_address.id != address.id
    assert (branch_address.user_id, branch_address.is_default, branch_address.is_business) == (customer.id, False, True)
    assert (branch_address.title, branch_address.full_address) == ("Bahor market, Yunusobod", "Yunusobod 19-kvartal, 4")
    assert (branch_address.latitude, branch_address.longitude) == (FAR[0], FAR[1])
    assert {o.id for o in Outlet.query.filter_by(user_id=customer.id)} == {outlet.id, branch.id}
    assert {a.id for a in UserAddress.query.filter_by(user_id=customer.id)} == {address.id, branch_address.id}


def test_link_creates_its_own_address_when_the_free_default_is_somewhere_else(db, agent):
    """R39: the pin decides which address this shop IS. The account's default is free (no
    outlet holds it) but ~4 km away, so adopting it — what "a free one, else its own" did —
    would point every order on behalf and the branch history at the wrong place."""
    customer = _customer(db, "+998901112345", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    office = UserAddress(user_id=customer.id, full_address="Yunusobod 19", latitude=FAR[0], longitude=FAR[1], is_default=True)
    db.session.add(office)
    db.session.commit()
    office_id = office.id

    outlet = OutletService.create(agent.id, {**GROCERY, "contact": None}, link_user_id=customer.id)

    assert outlet.stage == "active" and outlet.address_id not in (None, office_id)
    shop = UserAddress.query.get(outlet.address_id)
    assert (shop.user_id, shop.is_default, shop.title, shop.full_address) == (
        customer.id,
        False,
        "Bahor market",
        "Chilonzor 5-kvartal, 12",
    )
    assert (shop.latitude, shop.longitude) == PIN
    assert Outlet.query.filter_by(address_id=office_id).first() is None  # the office stays free


def test_link_adopts_the_nearest_free_address_and_never_a_held_one(db, agent):
    """R39: among the account's addresses within SALES_DEDUPE_RADIUS_M of the pin, the NEAREST
    one no outlet holds is adopted — no new row. The one AT the pin is held by a branch whose
    own pin is elsewhere (an address edited after its outlet was pinned — outlet pins are
    write-once), so taking it would break `uq_outlets_address_id`; the ~111 m one is free but
    farther than the ~56 m one."""
    customer = _customer(db, "+998901112346", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    held = UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    mid = UserAddress(user_id=customer.id, full_address="Chilonzor 6", latitude=MID[0], longitude=MID[1])
    near = UserAddress(user_id=customer.id, full_address="Chilonzor 5, back door", latitude=NEAR[0], longitude=NEAR[1])
    # Inserted in this order on purpose: `mid` gets the lower id, so "the first free address"
    # and "the NEAREST free address" answer differently here.
    for address in (held, mid, near):
        db.session.add(address)
        db.session.flush()
    db.session.add(
        Outlet(
            name="Bahor market, Yunusobod",
            outlet_type="grocery_store",
            stage="active",
            user_id=customer.id,
            address_id=held.id,
            latitude=FAR[0],
            longitude=FAR[1],
        )
    )
    db.session.commit()
    before = {a.id for a in UserAddress.query.filter_by(user_id=customer.id)}

    outlet = OutletService.create(agent.id, {**GROCERY, "contact": None}, link_user_id=customer.id)

    assert (outlet.stage, outlet.address_id) == ("active", near.id)
    assert {a.id for a in UserAddress.query.filter_by(user_id=customer.id)} == before


def test_a_pin_less_link_falls_back_to_the_free_default_and_takes_its_pin(db, agent):
    """R39's pin-less branch (API-only — the bot always sends a pin): with no place to compare,
    the same-place check is skipped and the account's default is adopted while it is free,
    lending the outlet its pin."""
    customer = _customer(db, "+998901112347", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    home = UserAddress(user_id=customer.id, full_address="Chilonzor 5", latitude=PIN[0], longitude=PIN[1], is_default=True)
    db.session.add(home)
    db.session.commit()

    outlet = OutletService.create(
        agent.id, {**GROCERY, "contact": None, "latitude": None, "longitude": None}, link_user_id=customer.id
    )

    assert (outlet.stage, outlet.address_id) == ("active", home.id)
    assert (outlet.latitude, outlet.longitude) == PIN
    assert UserAddress.query.filter_by(user_id=customer.id).count() == 1


def test_linking_an_account_with_no_address_gives_the_outlet_one(db, agent):
    """The hole branch mode made visible: `_link_customer` used to leave `address_id` NULL
    whenever it could not adopt an address, and `VisitService._assert_orderable` refuses
    exactly that — a shop at stage `active` nobody can place an order for."""
    customer = _customer(db, "+998901112344", company="Navruz", subtype=EntitySubtype.GROCERY_STORE)

    outlet = OutletService.create(agent.id, {**GROCERY, "name": "Navruz market", "contact": None}, link_user_id=customer.id)

    assert outlet.stage == "active" and outlet.address_id is not None
    address = UserAddress.query.get(outlet.address_id)
    assert (address.user_id, address.is_default, address.title) == (customer.id, True, "Navruz market")


def test_link_refuses_a_customer_of_another_type(db, agent):
    workplace = _customer(db, "+998901112288", company="Office", subtype=EntitySubtype.WORKPLACE)
    with pytest.raises(ConflictError) as excinfo:
        OutletService.create(agent.id, {**GROCERY, "contact": None}, link_user_id=workplace.id)
    assert excinfo.value.error_code == "SALES_APPROVAL_PHONE_TAKEN"


def test_individual_with_phone_is_activated_immediately(db, agent):
    outlet = OutletService.create(agent.id, {
        "name": "Dilnoza opa", "outlet_type": "individual",
        "contact": {"name": "Dilnoza Rahimova", "phone": "+998901112299"},
        "latitude": PIN[0], "longitude": PIN[1], "address_text": "Yunusobod 4", "district": "yunusabad",
    })

    user = User.query.get(outlet.user_id)
    assert outlet.stage == "active"
    assert _history(outlet) == [(None, "prospect", "created"), ("prospect", "active", "activated")]
    assert user.user_type == UserType.INDIVIDUAL and user.role == UserRole.CUSTOMER
    assert user.registration_source == "sales_agent"
    assert user.first_name == "Dilnoza" and user.last_name == "Rahimova"
    address = UserAddress.query.get(outlet.address_id)
    assert address.user_id == user.id and address.is_default is True and address.is_business is False


def test_individual_without_phone_stays_a_prospect(db, agent):
    outlet = OutletService.create(agent.id, {"name": "Someone", "outlet_type": "individual", "latitude": PIN[0], "longitude": PIN[1]})
    assert outlet.stage == "prospect" and outlet.user_id is None


def test_phone_only_contact_borrows_the_outlet_name(db, agent):
    """`outlet_contacts.name` is NOT NULL and nothing else guards it."""
    outlet = OutletService.create(agent.id, {**GROCERY, "contact": {"phone": "+998901112311"}})

    assert outlet.primary_contact.name == "Bahor market"
    assert outlet.primary_contact.phone == "+998901112311"


def test_unknown_contact_role_is_refused(db, agent):
    with pytest.raises(ValidationError) as excinfo:
        OutletService.create(agent.id, {**GROCERY, "contact": {"name": "X", "phone": "+998901112322", "role": "cousin"}})
    assert excinfo.value.error_code == "SALES_CONTACT_ROLE_INVALID"


def test_pin_outside_the_zone_is_refused_with_a_code(db, agent):
    with pytest.raises(ValidationError) as excinfo:
        OutletService.create(agent.id, {**GROCERY, "latitude": 40.0, "longitude": 65.0})
    assert excinfo.value.error_code == "SALES_OUTLET_OUTSIDE_ZONE"


def test_sales_agent_registration_source_is_self_linkable():
    assert "sales_agent" in TELEGRAM_SELF_LINK_SOURCES and "admin_created" in TELEGRAM_SELF_LINK_SOURCES


def test_create_user_by_admin_accepts_registration_source(db, admin_user):
    user = AuthService().create_user_by_admin(phone="+998901113300", first_name="Src", created_by_admin_id=admin_user.id, registration_source="sales_agent")
    assert user.registration_source == "sales_agent"


def test_create_user_by_admin_still_defaults_to_admin_created(db, admin_user):
    user = AuthService().create_user_by_admin(phone="+998901113311", first_name="Src", created_by_admin_id=admin_user.id)
    assert user.registration_source == "admin_created"
