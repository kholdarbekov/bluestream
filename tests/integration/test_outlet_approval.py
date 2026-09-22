"""Request → approve creates customer, address and (grocery) AMOUNT contract, resumably."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.sales import OUTLET_STAGES, Outlet, OutletContact, OutletStageHistory, SalesAgentProfile
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.exceptions import ConflictError, ForbiddenError, ValidationError
from business_app.utils.service_factory import get_corporate_contract_service
from shared.enums import EntitySubtype, OrderStatus, PaymentMethod, PaymentStatus, UserType
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_outlet_dedupe import PIN, _customer
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


def _address(db, user, full_address, district):
    db.session.add(
        UserAddress(
            user_id=user.id,
            full_address=full_address,
            district=district,
            latitude=PIN[0],
            longitude=PIN[1],
            is_default=True,
        )
    )


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
