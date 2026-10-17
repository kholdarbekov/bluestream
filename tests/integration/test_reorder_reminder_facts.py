"""Facts the reorder-reminder rule reads, loaded in batches (spec §4, §5, §9.3; D18, D21; Review Focus #2 and #4)."""

import logging
from dataclasses import replace

import pytest
from sqlalchemy.exc import OperationalError

from business_app.services.order_service import OrderService
from business_app.services.reorder_reminders import facts as facts_module
from business_app.services.reorder_reminders.facts import (
    DormantOutletFacts,
    LiveOrderFacts,
    load_customer_for_user,
    load_customers,
)
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from business_app.services.sales.outlet_service import OutletService
from business_app.utils.exceptions import ValidationError
from shared.enums import OrderStatus, SubscriptionStatus, UserType
from tests.integration.reorder_reminder_builders import (
    NO_ADDRESS,
    at,
    customer_with_deliveries,
    day,
    delivered_order,
    legacy_delivered_order,
    link,
    live_order,
    make_customer,
    make_episode,
    outlet,
    place,
    set_status,
    subscription,
)


def _by_key(today):
    return {customer.key: customer for customer in load_customers(today=today)}


def test_a_linked_cluster_is_one_customer_with_every_members_own_deliveries_newest_first(db):
    ali, ali_work = make_customer(db, telegram=True), make_customer(db, first_name="AliWork")
    canonical = link(db, ali, ali_work)
    first = delivered_order(db, ali, at(-14))
    second = delivered_order(db, ali_work, at(-7))
    third = delivered_order(db, ali, at(0))
    vali, (vali_order,) = customer_with_deliveries(db, (-3,), first_name="Vali")

    customers = _by_key(day(1))

    cluster = customers[f"c:{canonical.id}"]
    assert cluster.member_ids == tuple(sorted((ali.id, ali_work.id)))
    assert cluster.own_deliveries == ((third.id, at(0)), (second.id, at(-7)), (first.id, at(-14)))
    assert cluster.own_anchor == cluster.supply == (third.id, at(0))
    assert customers[f"u:{vali.id}"].own_deliveries == ((vali_order.id, at(-3)),)


def test_a_legacy_delivered_order_with_no_history_row_is_never_a_delivery(db):
    """Review Focus #2. Dev holds two DELIVERED orders with no DELIVERED history row. They have no
    delivered instant, so they are not deliveries: never a NULL date, never a crash, and they move
    neither the customer's own anchor nor a coworker's supply anchor."""
    old_only = make_customer(db, first_name="Old")
    legacy_delivered_order(db, old_only, at(-30))
    mixed = make_customer(db, first_name="Mixed")
    real = delivered_order(db, mixed, at(-10))
    coworker = make_customer(db, first_name="Co")
    office = place(db, mixed, coworker)
    legacy_delivered_order(db, mixed, at(-2), address=office[mixed.id])
    legacy_delivered_order(db, coworker, at(-1), address=office[coworker.id])

    customers = _by_key(day(0))

    assert f"u:{old_only.id}" not in customers
    assert load_customer_for_user(old_only.id, today=day(0)).own_deliveries == ()
    assert customers[f"u:{mixed.id}"].own_deliveries == ((real.id, at(-10)),)
    assert customers[f"u:{mixed.id}"].supply == (real.id, at(-10))


def test_the_supply_anchor_is_the_newest_delivery_to_any_address_of_the_place_whoever_ordered(db):
    """D18: a delivery to the shared place restarts every member's clock. A delivery dated after
    `today` does not exist yet."""
    alice = make_customer(db, first_name="Alice", telegram=True)
    bob = make_customer(db, first_name="Bob", telegram=True)
    office = place(db, alice, bob)
    delivered_order(db, alice, at(-7), address=office[alice.id])
    bobs = delivered_order(db, bob, at(-60), address=office[bob.id])
    alices = delivered_order(db, alice, at(0), address=office[alice.id])
    delivered_order(db, alice, at(5), address=office[alice.id])

    customers = _by_key(day(1))

    bob_facts = customers[f"u:{bob.id}"]
    assert (bob_facts.own_anchor, bob_facts.supply) == ((bobs.id, at(-60)), (alices.id, at(0)))
    assert bob_facts.place_group_ids == (office[bob.id].address_group_id,)
    assert customers[f"u:{alice.id}"].supply == (alices.id, at(0))


def test_an_order_with_no_delivery_address_counts_only_for_the_person_who_placed_it(db):
    alice = make_customer(db, first_name="Alice", telegram=True)
    bob = make_customer(db, first_name="Bob", telegram=True)
    office = place(db, alice, bob)
    bobs = delivered_order(db, bob, at(-3), address=office[bob.id])
    alices = delivered_order(db, alice, at(0), address=NO_ADDRESS)

    customers = _by_key(day(1))

    assert customers[f"u:{bob.id}"].supply == (bobs.id, at(-3))
    assert customers[f"u:{alice.id}"].own_anchor == (alices.id, at(0))


def test_live_orders_are_my_own_plus_anyones_on_their_way_to_my_place(db):
    alice = make_customer(db, first_name="Alice", telegram=True)
    bob = make_customer(db, first_name="Bob", telegram=True)
    office = place(db, alice, bob)
    delivered_order(db, alice, at(-7))
    own = live_order(db, alice, at(-1))
    peer = live_order(db, bob, at(-2), status=OrderStatus.CONFIRMED, address=office[bob.id])
    stuck_peer = live_order(db, bob, at(-20), address=office[bob.id], failed=True)
    live_order(db, bob, at(-1))  # Bob's home: not Alice's place
    live_order(db, alice, at(3))  # placed after `today`
    live_order(db, alice, at(-1), status=OrderStatus.CANCELLED)  # not live

    facts = load_customer_for_user(alice.id, today=day(0))

    office_id = office[bob.id].id
    assert facts.live_orders == (
        LiveOrderFacts(order_id=own.id, user_id=alice.id, delivery_address_id=own.delivery_address_id,
                       created_on=day(-1), delivery_date=None, awaiting_new_date=False, is_own=True),
        LiveOrderFacts(order_id=peer.id, user_id=bob.id, delivery_address_id=office_id,
                       created_on=day(-2), delivery_date=None, awaiting_new_date=False, is_own=False),
        LiveOrderFacts(order_id=stuck_peer.id, user_id=bob.id, delivery_address_id=office_id,
                       created_on=day(-20), delivery_date=None, awaiting_new_date=True, is_own=False),
    )


@pytest.mark.parametrize(
    "landed, still_dormant",
    [pytest.param(-10, True, id="landed-before-it-went-dormant"), pytest.param(1, False, id="landed-since")],
)
def test_the_facts_and_the_stage_job_ask_one_reactivation_rule(db, monkeypatch, sales_agent_user, landed, still_dormant):
    """Final review A1: "is this outlet still dormant" is the 01:10 stage job's OWN reactivation
    condition (an order in the outlet's `order_scope` landed after it entered its stage), asked of
    ONE OutletService predicate by both the reminder facts and the stage job, never the stored stage
    column alone (which only that job moves, hours after the delivery). The two answers agree."""
    shop, _orders = customer_with_deliveries(db, (-60, -30), first_name="Rustam")
    dormant = outlet(db, shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
    delivered_order(db, shop, at(landed))
    asked = []
    real = OutletService.reactivated_outlet_ids

    def spy(outlets):
        answer = real(outlets)
        asked.append(([row.id for row in outlets], answer))
        return answer

    monkeypatch.setattr(OutletService, "reactivated_outlet_ids", staticmethod(spy))

    facts = load_customer_for_user(shop.id, today=day(2))
    counts = OutletService.update_stages(now=at(2, 1, 10))

    assert [row.outlet_id for row in facts.dormant_outlets] == ([dormant.id] if still_dormant else [])
    assert (dormant.stage, counts["reactivated"]) == (("dormant", 0) if still_dormant else ("active", 1))
    expected = set() if still_dormant else {dormant.id}
    assert asked == [([dormant.id], expected), ([dormant.id], expected)], "facts first, then the stage job"


def test_subscriptions_outlets_and_the_setting_are_read_across_the_cluster(db, sales_agent_user):
    ali, ali_shop = make_customer(db, telegram=True), make_customer(db, first_name="AliShop")
    canonical = link(db, ali, ali_shop)
    delivered_order(db, ali, at(-7))
    subscription(db, ali_shop, status=SubscriptionStatus.PAUSED)
    outlet(db, ali_shop, stage="lost", agent=sales_agent_user)
    dormant = outlet(db, ali_shop, stage="dormant", agent=sales_agent_user, dormant_since=at(-3))
    Settings.set(ali_shop.id, False, actor_kind="customer", via="web_settings", actor_user_id=ali_shop.id, now=at(-1))
    vali, _ = customer_with_deliveries(db, (-7,), first_name="Vali")
    subscription(db, vali, status=SubscriptionStatus.CANCELLED)

    customers = _by_key(day(0))

    cluster = customers[f"c:{canonical.id}"]
    assert (cluster.has_live_subscription, cluster.has_any_outlet) == (True, True)
    assert cluster.dormant_outlets == (
        DormantOutletFacts(outlet_id=dormant.id, name=dormant.name, user_id=ali_shop.id,
                           dormant_since=at(-3), agent_user_id=sales_agent_user.id),
    )
    assert (cluster.setting.customer_off, cluster.setting.source_user_id) == (True, ali_shop.id)
    loner = customers[f"u:{vali.id}"]
    assert (loner.has_live_subscription, loner.has_any_outlet, loner.dormant_outlets, loner.setting.enabled) == (
        False, False, (), True,
    )
    assert OutletService.account_has_any_outlet([ali.id, ali_shop.id]) is True
    assert OutletService.account_has_any_outlet([vali.id]) is False


def test_member_facts_carry_the_can_order_predicate_and_the_greeting(db):
    named = make_customer(db, first_name="Dilnoza", telegram=True)
    phoneless = make_customer(db, first_name="Telegram User", phone=None)
    shop = make_customer(db, first_name="Rustam", user_type=UserType.ENTITY, company_name="Oqtepa Market")
    link(db, named, phoneless, shop)
    delivered_order(db, named, at(-3))

    members = {member.user_id: member for member in load_customer_for_user(named.id, today=day(0)).members}

    assert (members[named.id].can_order, members[named.id].greeting_name) == (True, "Dilnoza")
    assert (members[phoneless.id].can_order, members[phoneless.id].greeting_name) == (False, None)
    assert (members[shop.id].can_order, members[shop.id].greeting_name) == (False, "Oqtepa Market")
    assert members[named.id].telegram_id is not None and members[named.id].is_bot_active is True


def test_create_order_refuses_with_the_same_predicate_the_reminder_rule_reads(db):
    phoneless = make_customer(db, phone=None)
    shop = make_customer(db, first_name="Rustam", user_type=UserType.ENTITY, company_name="Oqtepa Market")
    regular = make_customer(db, first_name="Vali")
    payload = {
        "items": [{"product_id": 1, "quantity": 1}],
        "delivery_address": {"street": "1 Test St", "latitude": 41.3111, "longitude": 69.2797},
    }

    assert [OrderService.customer_can_order(user) for user in (phoneless, shop, regular)] == [False, False, True]
    with pytest.raises(ValidationError) as phone_refusal:
        OrderService().create_order(phoneless.id, payload)
    assert phone_refusal.value.message == "Phone number is required to place an order. Please update your profile."
    with pytest.raises(ValidationError) as subtype_refusal:
        OrderService().create_order(shop.id, payload)
    assert subtype_refusal.value.message == "Entity subtype must be assigned by admin before placing orders"


def test_load_customer_for_user_returns_that_users_cluster_or_none(db):
    ali, ali_work = make_customer(db), make_customer(db, first_name="AliWork")
    link(db, ali, ali_work)
    delivered_order(db, ali_work, at(-3))

    assert load_customer_for_user(ali.id, today=day(0)).member_ids == tuple(sorted((ali.id, ali_work.id)))
    assert load_customer_for_user(987654, today=day(0)) is None


def test_loading_facts_costs_the_same_queries_for_two_customers_or_six(db, count_queries, sales_agent_user):
    """Every conditional read is on the measured path: each customer is a linked cluster sharing a
    place with a peer who delivered and ordered there, with a dormant outlet, a live subscription
    and a setting row. Each peer is a customer too (its own delivery seeds it)."""
    built = {}

    def add_customer(n):
        user, _ = customer_with_deliveries(db, (-14, -7), first_name=f"Customer{n}", telegram=True)
        sibling = make_customer(db, first_name=f"Sibling{n}")
        canonical = link(db, user, sibling)
        peer = make_customer(db, first_name=f"Peer{n}")
        office = place(db, user, peer)
        peer_delivery = delivered_order(db, peer, at(-3), address=office[peer.id])
        own_live = live_order(db, user, at(-1))
        peer_live = live_order(db, peer, at(-1), address=office[peer.id])
        dormant = outlet(db, sibling, stage="dormant", agent=sales_agent_user, dormant_since=at(-5))
        subscription(db, sibling, status=SubscriptionStatus.ACTIVE)
        Settings.set(sibling.id, False, actor_kind="customer", via="web_settings", actor_user_id=sibling.id, now=at(-2))
        built[f"c:{canonical.id}"] = (peer_delivery.id, own_live.id, peer_live.id, dormant.id, sibling.id)

    def assert_every_fact_was_read(customers):
        by_key = {customer.key: customer for customer in customers}
        assert len(by_key) == 2 * len(built)
        for key, (peer_delivery_id, own_live_id, peer_live_id, dormant_id, sibling_id) in built.items():
            cluster = by_key[key]
            assert cluster.supply == (peer_delivery_id, at(-3))
            assert [(order.order_id, order.is_own) for order in cluster.live_orders] == [
                (own_live_id, True),
                (peer_live_id, False),
            ]
            assert [row.outlet_id for row in cluster.dormant_outlets] == [dormant_id]
            assert (cluster.has_live_subscription, cluster.has_any_outlet) == (True, True)
            assert (cluster.setting.customer_off, cluster.setting.source_user_id) == (True, sibling_id)

    for n in range(2):
        add_customer(n)
    with count_queries() as small:
        customers = load_customers(today=day(0))
    assert_every_fact_was_read(customers)
    for n in range(2, 6):
        add_customer(n)
    with count_queries() as large:
        customers = load_customers(today=day(0))
    assert_every_fact_was_read(customers)

    assert large.count == small.count, (small.count, large.count)


def test_one_customers_facts_raising_skip_only_that_customer_and_report_its_key(db, monkeypatch, caplog):
    """Contract amendment 17 / Review Focus #4: a FACTS-level helper raising for one cluster (here the
    outlet read, as for a deleted outlet user; a corrupt address group raises the same way from the
    place read) costs that customer alone. The batch is re-read customer by customer; the key is
    logged and reported, and every other customer still loads."""
    good, (good_order,) = customer_with_deliveries(db, (-7,), first_name="Good")
    bad, _bad_orders = customer_with_deliveries(db, (-7,), first_name="Bad")
    real_outlet_facts = OutletService.outlet_facts_by_user

    def outlet_facts_or_fail(user_ids):
        if bad.id in user_ids:
            raise RuntimeError("deleted outlet user")
        return real_outlet_facts(user_ids)

    monkeypatch.setattr(OutletService, "outlet_facts_by_user", staticmethod(outlet_facts_or_fail))
    errors = []

    # The `business_app` logger has `propagate=False` (logging_config), so caplog's root handler
    # never sees this module's records; attach it to the module logger, as sales_pay_builders does.
    facts_module.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger=facts_module.logger.name):
            customers = load_customers(today=day(0), errors=errors)
    finally:
        facts_module.logger.removeHandler(caplog.handler)

    assert [customer.key for customer in customers] == [f"u:{good.id}"]
    assert customers[0].own_deliveries == ((good_order.id, at(-7)),)
    assert [(key, str(exc)) for key, exc in errors] == [(f"u:{bad.id}", "deleted outlet user")]
    assert f"u:{bad.id}" in caplog.text and "deleted outlet user" in caplog.text
    assert [customer.key for customer in load_customers(today=day(0))] == [f"u:{good.id}"]  # no list: still isolated
    with pytest.raises(RuntimeError, match="deleted outlet user"):
        load_customer_for_user(bad.id, today=day(0))  # one customer: the fault surfaces, never "no such user"
    assert load_customer_for_user(good.id, today=day(0)).key == f"u:{good.id}"


def test_a_database_fault_is_raised_never_retried_customer_by_customer(db, monkeypatch):
    """Ruling on Review Focus #4: on Postgres a DB-level error aborts the transaction, so a
    customer-by-customer retry would fail for everyone and the run would quietly do nothing. A
    DBAPIError is raised for Celery to log, and the next daily run retries. The per-customer path
    is never entered, and no customer is reported as a fault of its own."""
    first, _ = customer_with_deliveries(db, (-7,), first_name="First")
    second, _ = customer_with_deliveries(db, (-7,), first_name="Second")
    fault = OperationalError("SELECT 1", {}, Exception("boom"))
    batches = []

    def assemble_or_fail(clusters, *, today):
        batches.append(tuple(clusters))
        raise fault

    monkeypatch.setattr(facts_module, "_assemble", assemble_or_fail)
    errors = []

    with pytest.raises(OperationalError) as raised:
        load_customers(today=day(0), errors=errors)

    assert raised.value is fault
    assert batches == [tuple(sorted((f"u:{first.id}", f"u:{second.id}")))]  # the batch, and nothing after it
    assert errors == []


def test_a_database_fault_inside_the_per_customer_retry_is_raised_too(db, monkeypatch):
    """The same rule once the retry has started (a Python fault sent the batch there): the
    transaction is aborted, so the next customers could only fail, and the fault is raised."""
    first, _ = customer_with_deliveries(db, (-7,), first_name="First")
    second, _ = customer_with_deliveries(db, (-7,), first_name="Second")
    fault = OperationalError("SELECT 1", {}, Exception("boom"))
    batches = []

    def assemble_or_fail(clusters, *, today):
        batches.append(tuple(clusters))
        if len(clusters) > 1:
            raise RuntimeError("deleted outlet user")
        raise fault

    monkeypatch.setattr(facts_module, "_assemble", assemble_or_fail)
    errors = []

    with pytest.raises(OperationalError):
        load_customers(today=day(0), errors=errors)

    keys = tuple(sorted((f"u:{first.id}", f"u:{second.id}")))  # clusters run in key order
    assert batches == [keys, keys[:1]]  # the whole batch, then stopped at the first retry
    assert errors == []


def test_a_single_customers_fault_is_recorded_once_and_never_retried(db, monkeypatch, caplog):
    """A batch of one customer IS that customer: its fault is logged once with its key and
    reported, with no second assembly and no batch-level warning before it."""
    only, _ = customer_with_deliveries(db, (-7,), first_name="Only")
    fault = RuntimeError("deleted outlet user")
    batches = []

    def assemble_or_fail(clusters, *, today):
        batches.append(tuple(clusters))
        raise fault

    monkeypatch.setattr(facts_module, "_assemble", assemble_or_fail)
    errors = []

    facts_module.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=facts_module.logger.name):
            assert load_customers(today=day(0), errors=errors) == []
    finally:
        facts_module.logger.removeHandler(caplog.handler)

    key = f"u:{only.id}"
    assert batches == [(key,)]
    assert errors == [(key, fault)]
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("ERROR", f"reorder_reminders: facts for customer {key} failed and were skipped: deleted outlet user"),
    ]
    with pytest.raises(RuntimeError) as raised:
        load_customer_for_user(only.id, today=day(0))
    assert raised.value is fault
    assert batches == [(key,), (key,)]  # one assembly per call


def test_an_active_episodes_anchor_owner_is_loaded_after_the_anchor_stopped_counting(db):
    """A lapse whose anchor was returned still meets its customer, now with no delivery at all, so
    the run can close it. Without an ACTIVE episode the same history seeds nobody (E3)."""
    kept = make_customer(db, first_name="Kept")
    kept_anchor = delivered_order(db, kept, at(-30))
    make_episode(db, kept, anchor=kept_anchor)
    set_status(db, kept_anchor, OrderStatus.RETURNED)
    closed = make_customer(db, first_name="Closed")
    closed_anchor = delivered_order(db, closed, at(-30))
    make_episode(db, closed, anchor=closed_anchor, status="closed")
    set_status(db, closed_anchor, OrderStatus.RETURNED)
    gone = make_customer(db, first_name="Gone")
    set_status(db, delivered_order(db, gone, at(-30)), OrderStatus.RETURNED)

    customers = _by_key(day(12))

    assert list(customers) == [f"u:{kept.id}"]
    kept_facts = customers[f"u:{kept.id}"]
    assert (kept_facts.member_ids, kept_facts.own_deliveries, kept_facts.supply) == ((kept.id,), (), None)


def test_an_account_whose_only_outlet_is_lost_is_still_on_the_outlet_path(db, sales_agent_user):
    """E5 counts an outlet in ANY stage (D2, D16, D21): a lost shop keeps its account out of the
    customer reminders, though it has nothing dormant to show."""
    shop, _ = customer_with_deliveries(db, (-7,), first_name="Shop")
    outlet(db, shop, stage="lost", agent=sales_agent_user)

    facts = _by_key(day(0))[f"u:{shop.id}"]

    assert (facts.has_any_outlet, facts.dormant_outlets) == (True, ())
    assert OutletService.account_has_any_outlet([shop.id]) is True


def test_a_linked_siblings_order_to_the_shared_place_is_one_own_live_order(db):
    """The order reaches the cluster twice, by its owner and by its address: it is one fact, and
    the customer's own. The coworker at the place sees the same order as someone else's."""
    ali, ali_work = make_customer(db, telegram=True), make_customer(db, first_name="AliWork")
    link(db, ali, ali_work)
    bob = make_customer(db, first_name="Bob")
    office = place(db, ali_work, bob)
    delivered_order(db, ali, at(-7))
    order = live_order(db, ali_work, at(-1), address=office[ali_work.id])

    ali_facts = load_customer_for_user(ali.id, today=day(0))
    bob_facts = load_customer_for_user(bob.id, today=day(0))

    expected = LiveOrderFacts(
        order_id=order.id,
        user_id=ali_work.id,
        delivery_address_id=office[ali_work.id].id,
        created_on=day(-1),
        delivery_date=None,
        awaiting_new_date=False,
        is_own=True,
    )
    assert ali_facts.live_orders == (expected,)
    assert bob_facts.live_orders == (replace(expected, is_own=False),)
