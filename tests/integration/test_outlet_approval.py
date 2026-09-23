"""Request → approve creates customer, address and (grocery) AMOUNT contract, resumably."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.sales import OUTLET_STAGES, Outlet, OutletContact, OutletStageHistory, SalesAgentProfile
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import OutletService, _import_outlet_name
from business_app.utils.exceptions import ConflictError, ForbiddenError, ValidationError
from business_app.utils.service_factory import get_corporate_contract_service
from shared.enums import EntitySubtype, OrderStatus, PaymentMethod, PaymentStatus, UserType
from tests.integration.test_outlet_create import GROCERY, MID
from tests.unit.test_outlet_dedupe import NEAR, PIN, _customer
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


@pytest.fixture
def requested(db, agent):
    outlet = OutletService.create(agent.id, dict(GROCERY))
    return OutletService.request_activation(outlet, agent.id)


def test_request_activation_needs_phone_and_pin(db, agent):
    no_phone = OutletService.create(agent.id, {**GROCERY, "name": "No phone", "contact": {"name": "X"}})
    with pytest.raises(ValidationError) as excinfo:
        OutletService.request_activation(no_phone, agent.id)
    assert excinfo.value.error_code == "SALES_ACTIVATION_PHONE_REQUIRED"

    no_pin = OutletService.create(
        agent.id,
        {
            **GROCERY,
            "name": "No pin",
            "latitude": None,
            "longitude": None,
            "contact": {"name": "Y", "phone": "+998901113311"},
        },
    )
    with pytest.raises(ValidationError) as excinfo:
        OutletService.request_activation(no_pin, agent.id)
    assert excinfo.value.error_code == "SALES_ACTIVATION_PIN_REQUIRED"


def test_request_moves_stage_and_lists_for_approvers(db, agent, requested):
    assert requested.stage == "activation_requested" and requested.activation_requested_at is not None
    assert [o.id for o in OutletService.list_activation_requests()] == [requested.id]


def test_approve_creates_customer_address_and_amount_contract(db, admin_user, requested):
    outlet = OutletService.approve(requested.id, actor_id=admin_user.id)

    user = User.query.get(outlet.user_id)
    assert outlet.stage == "active" and outlet.approved_by_user_id == admin_user.id
    assert user.user_type == UserType.ENTITY and user.entity_subtype == EntitySubtype.GROCERY_STORE
    assert user.company_name == "Bahor market" and user.registration_source == "sales_agent"
    assert user.first_name == "Olim" and user.last_name == "aka"
    address = UserAddress.query.get(outlet.address_id)
    assert address.is_business is True and address.latitude == PIN[0]

    contracts = get_corporate_contract_service()
    contract = contracts.get_active_amount_contract_for_user(user.id)
    assert contract is not None and contract.contract_number.startswith(f"SA-{outlet.id}-")

    # The exact call that raised for every store before this feature: charging a delivered order.
    order = Order(
        user_id=user.id,
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("90000"),
        total_amount=Decimal("90000"),
        delivery_address_id=address.id,
        order_source="web",
    )
    db.session.add(order)
    db.session.commit()
    ledger = contracts.charge_on_delivery(order, actor_user_id=admin_user.id)
    assert ledger is not None and Decimal(str(ledger.amount)) == Decimal("90000")

    history = [
        (h.from_stage, h.to_stage)
        for h in OutletStageHistory.query.filter_by(outlet_id=outlet.id).order_by(OutletStageHistory.id)
    ]
    assert history == [(None, "prospect"), ("prospect", "activation_requested"), ("activation_requested", "active")]


def test_approve_is_idempotent_and_resumes_after_a_failed_step(db, admin_user, requested, monkeypatch):
    contracts = get_corporate_contract_service()
    real_create = type(contracts).create_contract

    def boom(self, payload, actor_user_id=None):
        raise ValidationError("contract_number already exists")

    monkeypatch.setattr(type(contracts), "create_contract", boom)
    with pytest.raises(ValidationError) as excinfo:
        OutletService.approve(requested.id, actor_id=admin_user.id)
    assert excinfo.value.error_code == "SALES_APPROVAL_STEP_FAILED" and excinfo.value.details == {"step": "contract"}

    half = Outlet.query.get(requested.id)
    assert half.stage == "activation_requested" and half.user_id is not None and half.address_id is not None
    user_count = User.query.count()

    monkeypatch.setattr(type(contracts), "create_contract", real_create)
    done = OutletService.approve(requested.id, actor_id=admin_user.id)
    assert done.stage == "active"
    assert User.query.count() == user_count  # no second customer
    assert OutletService.approve(requested.id, actor_id=admin_user.id).stage == "active"  # third call: no-op


def test_approve_links_an_existing_matching_customer_and_refuses_a_mismatch(db, admin_user, agent):
    existing = _customer(db, "+998901113322", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    outlet = OutletService.create(agent.id, {**GROCERY, "contact": {"name": "Z", "phone": "+998901113322"}}, force=True)
    OutletService.request_activation(outlet, agent.id)
    assert OutletService.approve(outlet.id, actor_id=admin_user.id).user_id == existing.id

    _customer(db, "+998901113333", company="Office", subtype=EntitySubtype.WORKPLACE)
    clash = OutletService.create(
        agent.id, {**GROCERY, "name": "Clash", "contact": {"name": "Z", "phone": "+998901113333"}}, force=True
    )
    OutletService.request_activation(clash, agent.id)
    with pytest.raises(ConflictError) as excinfo:
        OutletService.approve(clash.id, actor_id=admin_user.id)
    assert excinfo.value.error_code == "SALES_APPROVAL_PHONE_TAKEN"


def test_reject_returns_to_prospect_with_reason(db, admin_user, requested):
    outlet = OutletService.reject(requested.id, actor_id=admin_user.id, reason="Duplicate of #12")
    assert outlet.stage == "prospect" and outlet.rejected_reason == "Duplicate of #12"


def test_update_rejects_a_malformed_delivery_window(db, agent, requested):
    with pytest.raises(ValidationError) as excinfo:
        OutletService.update(requested, {"delivery_window_start": "25:99"}, agent.id)
    assert excinfo.value.error_code == "SALES_WINDOW_INVALID"

    OutletService.update(requested, {"delivery_window_start": "09:00", "delivery_window_end": ""}, agent.id)
    assert requested.delivery_window_start == time(9, 0) and requested.delivery_window_end is None


def test_update_never_nulls_the_not_null_preferred_language(db, agent, requested):
    """`notes` is nullable and clearable; `preferred_language` is NOT NULL and must survive a null."""
    OutletService.update(requested, {"preferred_language": None, "notes": None}, agent.id)
    assert requested.preferred_language == "uz" and requested.notes is None

    OutletService.update(requested, {"preferred_language": "ru"}, agent.id)
    assert requested.preferred_language == "ru"


def test_agent_scope_lists_and_ownership(db, agent, admin_user):
    mine = OutletService.create(agent.id, dict(GROCERY))
    other_agent = make_sales_agent_user(db, phone="+998901234576", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other_agent.id, districts=["yunusabad"]))
    db.session.commit()
    # A different district on purpose: bulk assignment below must move `mine` and NOT this one.
    theirs = OutletService.create(
        other_agent.id,
        {**GROCERY, "name": "Navruz", "contact": None, "latitude": 41.35, "longitude": 69.20, "district": "yunusabad"},
    )

    items, total = OutletService.list_for_agent(agent.id, "all")
    assert [o.id for o in items] == [mine.id] and total == 1
    assert OutletService.get_for_agent(agent.id, mine.id).id == mine.id
    with pytest.raises(ForbiddenError) as excinfo:
        OutletService.get_for_agent(agent.id, theirs.id)
    assert excinfo.value.error_code == "SALES_OUTLET_NOT_ASSIGNED"

    OutletService.assign(theirs.id, agent.id, actor_id=admin_user.id)
    assert OutletService.get_for_agent(agent.id, theirs.id).assigned_agent_user_id == agent.id
    assert OutletService.bulk_assign_by_district("chilanzar", other_agent.id, actor_id=admin_user.id) == 1
    assert Outlet.query.get(mine.id).assigned_agent_user_id == other_agent.id
    assert Outlet.query.get(theirs.id).assigned_agent_user_id == agent.id


def _address(db, user, full_address, district, *, title=None, street_address=None, is_default=True, pin=PIN):
    address = UserAddress(
        user_id=user.id,
        full_address=full_address,
        district=district,
        title=title,
        street_address=street_address,
        latitude=pin[0],
        longitude=pin[1],
        is_default=is_default,
    )
    db.session.add(address)
    db.session.flush()
    return address


def test_import_existing_customers_creates_active_outlets_once(db, admin_user, agent):
    """`addresses.district` is free text; an outlet's district must land on a canonical key or NULL,
    because a display name like "Chilanzar" is unreachable by every district filter and by
    bulk_assign_by_district."""
    grocery = _customer(db, "+998901113344", company="Chinor", subtype=EntitySubtype.GROCERY_STORE)
    _address(db, grocery, "Sergeli 3", "sergeli")  # already canonical
    display_name = _customer(db, "+998901113388", company="Zarafshon", subtype=EntitySubtype.GROCERY_STORE)
    _address(db, display_name, "Chilonzor 5", "Chilanzar")  # en display name
    unknown = _customer(db, "+998901113399", company="Chorsu", subtype=EntitySubtype.WORKPLACE)
    _address(db, unknown, "Somewhere 1", "Nowhere")
    _customer(db, "+998901113355")  # individual: not imported
    db.session.commit()

    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 3
    outlet = Outlet.query.filter_by(user_id=grocery.id).one()
    assert outlet.stage == "active" and outlet.outlet_type == "grocery_store" and outlet.outlet_class == "C"
    assert outlet.district == "sergeli" and outlet.address_id is not None
    assert Outlet.query.filter_by(user_id=display_name.id).one().district == "chilanzar"
    assert Outlet.query.filter_by(user_id=unknown.id).one().district is None
    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 0

    # The point of canonicalising: the imported store is now reachable by district.
    assert OutletService.bulk_assign_by_district("chilanzar", agent.id, actor_id=admin_user.id) == 1


OUT_OF_ZONE = (39.6270, 66.9750)  # Samarkand -- far outside the delivery polygon


def test_import_existing_customers_drops_out_of_zone_pins_instead_of_aborting(db, admin_user):
    """One legacy address must not cost every other store its outlet.

    `register_delivery_zone_listeners(Outlet)` refuses an out-of-zone INSERT, and a
    `user_addresses` row written before that backstop existed can still carry one. Copying the
    pin straight onto the new Outlet therefore raised mid-loop and rolled the whole backfill
    back. The pin is the only part that is unusable, so it is the only part dropped.
    """
    good = _customer(db, "+998901114411", company="Yaqin", subtype=EntitySubtype.GROCERY_STORE)
    _address(db, good, "Chilonzor 5", "chilanzar")
    legacy = _customer(db, "+998901114422", company="Uzoq", subtype=EntitySubtype.GROCERY_STORE)
    _address(db, legacy, "Samarqand 1", "chilanzar")
    db.session.commit()
    # A bulk UPDATE, so the `before_update` backstop (an ORM mapper event) never fires: the only
    # way to reproduce the pre-guard row this backfill actually meets in production.
    UserAddress.query.filter_by(user_id=legacy.id).update(
        {"latitude": OUT_OF_ZONE[0], "longitude": OUT_OF_ZONE[1]}, synchronize_session=False
    )
    db.session.commit()

    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 2

    in_zone = Outlet.query.filter_by(user_id=good.id).one()
    assert (in_zone.latitude, in_zone.longitude) == (PIN[0], PIN[1])
    dropped = Outlet.query.filter_by(user_id=legacy.id).one()
    assert dropped.latitude is None and dropped.longitude is None
    # Everything that is not the pin survives: the outlet is still linked, addressed and filterable.
    assert dropped.address_id is not None and dropped.address_text == "Samarqand 1"
    assert dropped.district == "chilanzar"


def test_mark_lost_requires_a_known_reason(db, admin_user, requested):
    with pytest.raises(ValidationError) as excinfo:
        OutletService.mark_lost(requested.id, actor_id=admin_user.id, reason="meh", note=None)
    assert excinfo.value.error_code == "SALES_LOST_REASON_INVALID"
    lost = OutletService.mark_lost(
        requested.id, actor_id=admin_user.id, reason="has_supplier", note="Signed with Chortoq"
    )
    assert lost.stage == "lost" and lost.lost_reason == "has_supplier"


def test_card_carries_money_and_bottle_fields(db, admin_user, requested):
    # A prospect has no customer account and no address row, so it has no wallet and no bottle
    # ledger. Those figures are NOT APPLICABLE, and the card says so with null -- a 0 here reads
    # as a settled bill and an empty crate on a store that has never ordered.
    prospect = OutletService.card(requested)
    assert prospect["open_receivable"] is None and prospect["bottle_balance"] is None
    assert prospect["last_orders"] == []

    outlet = OutletService.approve(requested.id, actor_id=admin_user.id)
    card = OutletService.card(outlet)
    assert card["id"] == outlet.id
    assert card["open_receivable"] == 0.0 and card["bottle_balance"] == 0.0
    assert card["last_orders"] == []
    assert card["contacts"][0]["phone"] == "+998901112266"


def _order_with_payment(db, user, status, amount):
    order = Order(
        user_id=user.id,
        status=status,
        subtotal=Decimal(amount),
        total_amount=Decimal(amount),
        order_source="web",
    )
    db.session.add(order)
    db.session.flush()
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


def test_card_open_receivable_counts_only_delivered_orders(db, admin_user, requested):
    """A COD payment is born PENDING with amount_collected=0, so `net_open_receivable_amount` alone
    would bill the store for orders it has not taken delivery of (and for cancelled ones)."""
    outlet = OutletService.approve(requested.id, actor_id=admin_user.id)
    user = User.query.get(outlet.user_id)
    delivered = _order_with_payment(db, user, OrderStatus.DELIVERED, "90000")
    _order_with_payment(db, user, OrderStatus.CONFIRMED, "60000")
    _order_with_payment(db, user, OrderStatus.CANCELLED, "40000")

    card = OutletService.card(outlet)

    assert card["open_receivable"] == 90000.0
    # last_orders stays unfiltered: the card shows the store's recent history, not just its debts.
    assert len(card["last_orders"]) == 3
    assert delivered.id in [o["id"] for o in card["last_orders"]]


def _seed_outlet(db, **kwargs):
    outlet = Outlet(**{"outlet_type": "grocery_store", "stage": "prospect", **kwargs})
    db.session.add(outlet)
    db.session.commit()
    return outlet


def test_list_admin_filters_and_summary(db, agent):
    now = datetime.now(UTC)
    stale = _seed_outlet(
        db,
        name="Bahor market",
        stage="active",
        outlet_class="A",
        district="chilanzar",
        assigned_agent_user_id=agent.id,
        last_visit_at=now - timedelta(days=40),
    )
    fresh = _seed_outlet(
        db,
        name="Navruz do'kon",
        outlet_type="workplace",
        stage="prospect",
        outlet_class="B",
        district="yunusabad",
        last_visit_at=now - timedelta(days=2),
    )
    db.session.add(OutletContact(outlet_id=fresh.id, name="Zafar", phone="+998901113377", is_primary=True))
    never = _seed_outlet(db, name="Chinor", stage="active", outlet_class="C", district="sergeli")
    db.session.commit()

    def ids(filters):
        items, total, _ = OutletService.list_admin(filters, page=1, per_page=50)
        assert total == len(items)
        return sorted(o.id for o in items)

    assert ids({"search": "Бахор"}) == [stale.id]  # transliterated name variant
    assert ids({"search": "1113377"}) == [fresh.id]  # phone digits reach OutletContact
    assert ids({"stage": "active"}) == sorted([stale.id, never.id])
    assert ids({"outlet_class": "B"}) == [fresh.id]
    assert ids({"outlet_type": "workplace"}) == [fresh.id]
    assert ids({"district": "sergeli"}) == [never.id]
    assert ids({"agent_user_id": agent.id}) == [stale.id]
    # Active outlets not visited for 30+ days, plus the active one never visited at all.
    assert ids({"unvisited_days": 30}) == sorted([stale.id, never.id])

    items, total, summary = OutletService.list_admin({}, page=1, per_page=50)
    assert total == 3 and len(items) == 3
    assert set(summary) == set(OUTLET_STAGES)
    assert summary["active"] == 2 and summary["prospect"] == 1
    assert all(summary[stage] == 0 for stage in OUTLET_STAGES if stage not in ("active", "prospect"))


def test_import_creates_one_outlet_per_address_with_branch_names(db, admin_user):
    """An outlet is one PLACE; an ACCOUNT may own several (D25 rule 2).

    A chain is one customer account with several delivery addresses. Importing only the default
    address left every other branch invisible: the agent checked in "out of range", and an order
    on behalf went to the head shop. So the backfill walks ADDRESSES. The branch half of the name
    is the address's own label -- its street, else a title no other address of the account
    carries, else the first 40 characters of the free text (R41) -- because two rows called
    "Chinor" are two rows nobody can tell apart in a list, and the customer bot's canned titles
    ("Ish", "Uy", "Boshqa") repeat inside one chain. Only a CHAIN is named that way: an account
    with one address keeps the bare name the earlier one-outlet-per-account import gave it (R22),
    pinned by test_import_names_a_single_address_account_without_a_branch_suffix.
    """
    chain = _customer(db, "+998901115511", company="Chinor", subtype=EntitySubtype.GROCERY_STORE)
    # Three branches, three pins: a shared coordinate could not tell "pinned at its own address"
    # from "pinned at the head shop", and the head shop's pin is what made agents check in
    # "out of range" at every other branch.
    head = _address(
        db, chain, "Chilonzor 9", "chilanzar", title="Ish", street_address="Chilonzor 9-kvartal, 12", is_default=True
    )
    second = _address(db, chain, "Yunusobod 4", "Yunusabad", title="Sklad", is_default=False, pin=NEAR)
    third = _address(
        db,
        chain,
        "Sergeli tumani, 3-mavze, 41-uy, 2-qavat, 7-xona",
        "sergeli",
        title="Ish",
        is_default=False,
        pin=MID,
    )
    db.session.commit()

    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 3

    outlets = Outlet.query.filter_by(user_id=chain.id).order_by(Outlet.id).all()
    assert [o.address_id for o in outlets] == [head.id, second.id, third.id]
    assert [o.name for o in outlets] == [
        # the street wins, even over a unique title (pinned by the parametrised case)
        "Chinor, Chilonzor 9-kvartal, 12",
        "Chinor, Sklad",                                    # no street: a title no other address repeats
        "Chinor, Sergeli tumani, 3-mavze, 41-uy, 2-qavat",  # "Ish" repeats: 40 chars of the free text, comma trimmed
    ]
    assert [o.district for o in outlets] == ["chilanzar", "yunusabad", "sergeli"]
    assert {o.stage for o in outlets} == {"active"} and {o.outlet_class for o in outlets} == {"C"}
    # Each branch carries ITS address's pin and text, never the head shop's (R4).
    assert [(o.latitude, o.longitude, o.address_text) for o in outlets] == [
        (address.latitude, address.longitude, address.full_address) for address in (head, second, third)
    ]
    assert len({(o.latitude, o.longitude) for o in outlets}) == 3
    # Every branch carries the ACCOUNT's contact: one chain, one number the agent calls.
    contacts = (
        OutletContact.query.filter(OutletContact.outlet_id.in_([o.id for o in outlets]))
        .order_by(OutletContact.id)
        .all()
    )
    assert [(c.phone, c.is_primary) for c in contacts] == [("+998901115511", True)] * 3
    # Each imported outlet enters the funnel once, as an import, by the admin who pressed the button.
    history = (
        OutletStageHistory.query.filter(OutletStageHistory.outlet_id.in_([o.id for o in outlets]))
        .order_by(OutletStageHistory.outlet_id, OutletStageHistory.id)
        .all()
    )
    assert [(h.outlet_id, h.from_stage, h.to_stage, h.reason_code, h.actor_user_id) for h in history] == [
        (o.id, None, "active", "imported", admin_user.id) for o in outlets
    ]

    # Idempotent: every address already has its outlet, so a second press creates nothing and
    # renames nothing (`name` is agent-editable -- a rewrite here would stomp the agent's edit).
    before = [(o.id, o.name) for o in outlets]
    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 0
    assert [(o.id, o.name) for o in Outlet.query.filter_by(user_id=chain.id).order_by(Outlet.id)] == before


def test_import_skips_an_address_that_already_has_an_outlet_and_creates_the_rest(db, admin_user):
    """`uq_outlets_address_id` is the surviving invariant, so the skip is per ADDRESS.

    An agent who already onboarded one branch by hand must not have it duplicated, renamed or
    re-staged by the backfill -- and the account's other addresses must still get theirs.
    """
    chain = _customer(db, "+998901115522", company="Navruz", subtype=EntitySubtype.GROCERY_STORE)
    head = _address(db, chain, "Chilonzor 1", "chilanzar", title="Bosh", is_default=True)
    taken = _address(db, chain, "Yunusobod 2", "yunusabad", title="Filial", is_default=False)
    spare = _address(db, chain, "Sergeli 3", "sergeli", title="Bozor", is_default=False)
    db.session.add(
        Outlet(
            name="Navruz filiali (agent)",
            outlet_type="grocery_store",
            stage="at_risk",
            outlet_class="A",
            user_id=chain.id,
            address_id=taken.id,
            latitude=PIN[0],
            longitude=PIN[1],
        )
    )
    db.session.commit()

    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 2

    by_address = {o.address_id: o for o in Outlet.query.filter_by(user_id=chain.id).all()}
    assert set(by_address) == {head.id, taken.id, spare.id}
    assert by_address[head.id].name == "Navruz, Bosh" and by_address[head.id].stage == "active"
    assert by_address[spare.id].name == "Navruz, Bozor" and by_address[spare.id].outlet_class == "C"
    # The agent's own row is left exactly as it was: not renamed, not re-staged, not re-classed.
    assert by_address[taken.id].name == "Navruz filiali (agent)"
    assert by_address[taken.id].stage == "at_risk" and by_address[taken.id].outlet_class == "A"


def test_import_still_gives_an_account_with_no_address_its_one_outlet(db, admin_user):
    """CHARACTERIZATION. Walking addresses must not drop the account that has none.

    Proved by mutation: delete the `if not addresses:` arm of `import_existing_customers` and
    this test fails with `assert 0 == 1`. The mutation is never staged.
    """
    bare = _customer(db, "+998901115533", company="Ombor", subtype=EntitySubtype.WORKPLACE)
    db.session.commit()

    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 1
    outlet = Outlet.query.filter_by(user_id=bare.id).one()
    assert outlet.name == "Ombor"           # no address, so no branch half
    assert outlet.address_id is None and outlet.latitude is None and outlet.district is None
    assert outlet.stage == "active" and outlet.outlet_type == "workplace"
    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 0


def test_import_names_a_single_address_account_without_a_branch_suffix(db, admin_user):
    """CHARACTERIZATION (R22). One shop, one name -- the one the earlier one-outlet-per-account import wrote.

    The suffix exists to tell a CHAIN's branches apart. A customer with one address has nothing
    to tell apart, and every outlet already standing on prod carries the bare name: appending to
    it here would either rename rows the agents may have edited (`name` is in
    `AGENT_EDITABLE_OUTLET_FIELDS`) or hand only NEW accounts the long form.

    Proved by mutation: drop the `branch_mode` guard from `_import_outlet_name` so the suffix is
    unconditional, and this test fails on the name assertion, reading
    "Yakka do'kon, Do'kon" where "Yakka do'kon" is expected. The mutation is never staged.
    """
    solo = _customer(db, "+998901115544", company="Yakka do'kon", subtype=EntitySubtype.GROCERY_STORE)
    only = _address(db, solo, "Chilonzor 7", "chilanzar", title="Do'kon", is_default=True)
    db.session.commit()

    assert OutletService.import_existing_customers(actor_id=admin_user.id) == 1
    outlet = Outlet.query.filter_by(user_id=solo.id).one()
    assert outlet.name == "Yakka do'kon"      # the account's own name, no ", Do'kon"
    assert outlet.address_id == only.id and outlet.district == "chilanzar"


@pytest.mark.parametrize(
    "street_address, title, account_titles, full_address, expected",
    [
        # A whitespace-only street is no street: the address's unique title names it.
        ("   ", "Sklad", ["Sklad", "Ish"], "Yunusobod 4", "Chinor, Sklad"),
        # A whitespace-only title is no title: with no street either, the free text names it.
        (None, "   ", ["   ", "Ish"], "  Yunusobod 4, 7-uy  ", "Chinor, Yunusobod 4, 7-uy"),
        # A whitespace-only title never stands in front of a real street.
        ("Amir Temur 12", "   ", ["   ", "Sklad"], "Yunusobod 4", "Chinor, Amir Temur 12"),
        # A street beats even a title no other address of the account carries.
        ("Amir Temur 12", "Sklad", ["Sklad", "Ish"], "Yunusobod 4", "Chinor, Amir Temur 12"),
        # The free text is stripped BEFORE the 40-character cut, so leading blanks cost no characters.
        (
            None,
            None,
            [None, "Ish"],
            "   Sergeli tumani, 3-mavze, 41-uy, 2-qavat, 7-xonadon",
            "Chinor, Sergeli tumani, 3-mavze, 41-uy, 2-qavat",
        ),
        # Uniqueness compares STRIPPED titles: " Ish" and "Ish " are the same canned title ...
        (None, " Ish", [" Ish", "Ish "], "Yunusobod 4", "Chinor, Yunusobod 4"),
        # ... and compares them case-sensitively: "Ish" and "ish" are two titles.
        (None, "Ish", ["Ish", "ish"], "Yunusobod 4", "Chinor, Ish"),
    ],
    ids=[
        "blank-street-to-unique-title",
        "blank-title-to-free-text",
        "street-over-blank-title",
        "street-over-unique-title",
        "free-text-stripped-before-cut",
        "stripped-titles-collide",
        "titles-case-sensitive",
    ],
)
def test_import_outlet_name_labels_a_branch_by_street_then_unique_title_then_free_text(
    street_address, title, account_titles, full_address, expected
):
    """R41: the first NON-BLANK of street, a title unique on the account, 40 chars of the free text.

    Pure: the composer is handed the account's titles and never touches the session.
    """
    address = UserAddress(street_address=street_address, title=title, full_address=full_address)
    assert _import_outlet_name("Chinor", address, branch_mode=True, account_titles=account_titles) == expected
