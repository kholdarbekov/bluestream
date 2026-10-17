"""Address groups are one unit of supply (D18; spec §15.2), on SQLite and on Postgres.

Each scenario runs twice: on the SQLite suite and on a migrated Postgres, where the partial
unique index and the RESTRICT foreign keys are real.
"""

from datetime import timezone

from business_app.models.reorder_reminder import ReorderReminderEpisode, ReorderReminderEvent
from shared.enums import OrderStatus
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    NO_ADDRESS,
    WEEKLY,
    at,
    day,
    delivered_order,
    lapses,
    leave_place,
    live_order,
    make_customer,
    only_lapse,
    place,
    run_engine,
    set_status,
    store_fixture,
)


def _office(store, *names):
    people = [make_customer(store, first_name=name, telegram=True) for name in names]
    return people, place(store, *people)


def test_a_coworkers_delivery_to_the_shared_place_restarts_my_clock(store):
    (alice, bob), office = _office(store, "Alice", "Bob")
    bob_orders = [delivered_order(store, bob, at(n), address=office[bob.id]) for n in (-21, -14, -7, -3)]
    alice_order = delivered_order(store, alice, at(0), address=office[alice.id])

    run_engine(8)
    assert lapses(bob_orders[-1]) == []  # his own rhythm says day 8, but the office was supplied on day 0
    run_engine(11)

    bob_row = only_lapse(bob_orders[-1])
    assert (bob_row.supply_order_id, bob_row.due_on, bob_row.status) == (alice_order.id, day(11), "open")


def test_a_coworkers_order_on_its_way_to_the_place_pauses_me(store):
    (alice, bob), office = _office(store, "Alice", "Bob")
    alice_orders = [delivered_order(store, alice, at(n), address=office[alice.id]) for n in WEEKLY]
    live_order(store, bob, at(10), status=OrderStatus.CONFIRMED, address=office[bob.id])

    run_engine(11)

    assert lapses(alice_orders[-1]) == []


def test_a_coworkers_stuck_order_neither_pauses_nor_escalates_me(store):
    (carol, dave), office = _office(store, "Carol", "Dave")
    carol_orders = [delivered_order(store, carol, at(n), address=office[carol.id]) for n in WEEKLY]
    live_order(store, dave, at(-10), address=office[dave.id])  # 21 days old on day 11, no date: stuck

    run_engine(11)

    row = only_lapse(carol_orders[-1])
    assert (row.status, row.escalation_flag) == ("open", None)


def test_a_coworkers_delivery_after_my_reminder_closes_my_lapse_as_place_resupplied(store):
    (alice, bob), office = _office(store, "Alice", "Bob")
    alice_orders = [delivered_order(store, alice, at(n), address=office[alice.id]) for n in WEEKLY]
    run_engine(11)
    bob_order = delivered_order(store, bob, at(12), address=office[bob.id])

    summary = run_engine(13)

    row = only_lapse(alice_orders[-1])
    assert (row.status, row.close_reason, summary.closed) == ("closed", "place_resupplied", {"place_resupplied": 1})
    closed = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="closed").one()
    assert (closed.detail, closed.payload) == (
        "place_resupplied",
        {"order_id": bob_order.id, "delivered_at": at(12).astimezone(timezone.utc).isoformat()},
    )


def test_two_members_sharing_one_supply_anchor_get_two_rows(store):
    (alice, bob), office = _office(store, "Alice", "Bob")
    alice_orders = [delivered_order(store, alice, at(n), address=office[alice.id]) for n in WEEKLY]
    bob_orders = [delivered_order(store, bob, at(n), address=office[bob.id]) for n in (-22, -15, -8, -1)]

    run_engine(11)

    alice_row, bob_row = only_lapse(alice_orders[-1]), only_lapse(bob_orders[-1])
    assert alice_row.supply_order_id == bob_row.supply_order_id == alice_orders[-1].id
    assert alice_row.id != bob_row.id and (alice_row.user_id, bob_row.user_id) == (alice.id, bob.id)


def test_an_address_leaving_the_place_moves_the_pair_backwards_and_a_new_lapse_can_open(store):
    (carol, dave), office = _office(store, "Carol", "Dave")
    carol_orders = [delivered_order(store, carol, at(n)) for n in (-24, -17, -10, -3)]  # her home address
    dave_order = delivered_order(store, dave, at(0), address=office[dave.id])
    run_engine(11)
    assert only_lapse(carol_orders[-1]).supply_order_id == dave_order.id

    leave_place(store, office[carol.id])
    run_engine(12)

    closed, reopened = lapses(carol_orders[-1])
    assert (closed.status, closed.close_reason) == ("closed", "superseded")
    assert (reopened.status, reopened.supply_order_id, reopened.due_on) == ("open", carol_orders[-1].id, day(8))


def test_a_supply_that_moved_back_to_another_place_order_names_that_order_when_it_closes(store):
    """Fix round 1 M7: Carol's address leaves the office, so her supply falls back to the depot's older
    delivery while her own anchor stays put. The `closed` event names the order that moved: Erin's."""
    carol, dave, erin = (make_customer(store, first_name=name, telegram=True) for name in ("Carol", "Dave", "Erin"))
    office = place(store, carol, dave, label="office")
    depot = place(store, carol, erin, label="depot")
    carol_orders = [delivered_order(store, carol, at(n)) for n in (-24, -17, -10, -3)]  # her home address
    erin_order = delivered_order(store, erin, at(-1), address=depot[erin.id])
    dave_order = delivered_order(store, dave, at(0), address=office[dave.id])
    run_engine(11)
    assert only_lapse(carol_orders[-1]).supply_order_id == dave_order.id

    leave_place(store, office[carol.id])
    run_engine(12)

    closed, reopened = lapses(carol_orders[-1])
    event = ReorderReminderEvent.query.filter_by(episode_id=closed.id, kind="closed").one()
    assert (closed.close_reason, event.payload) == (
        "superseded",
        {"order_id": erin_order.id, "delivered_at": at(-1).astimezone(timezone.utc).isoformat()},
    )
    assert (reopened.status, reopened.supply_order_id, reopened.due_on) == ("open", erin_order.id, day(10))


def test_a_lapse_whose_own_delivery_stopped_counting_closes_superseded_naming_no_order(store):
    """Fix round 2 (§6 Close, M7): Alice's only delivery is returned, so she has no own anchor left and
    her supply falls back to Bob's older office delivery. The pair moved because her own delivery
    vanished, so the `closed` event names no order: there is no superseding one."""
    (alice, bob), office = _office(store, "Alice", "Bob")
    bob_orders = [delivered_order(store, bob, at(n), address=office[bob.id]) for n in (-65, -35, -5)]  # due day 46
    alice_order = delivered_order(store, alice, at(0), address=office[alice.id])  # preset: due day 31
    run_engine(31)
    assert (only_lapse(alice_order).status, only_lapse(alice_order).supply_order_id) == ("open", alice_order.id)
    assert lapses(bob_orders[-1]) == []

    set_status(store, alice_order, OrderStatus.RETURNED)
    summary = run_engine(32)

    row = only_lapse(alice_order)
    closed = ReorderReminderEvent.query.filter_by(episode_id=row.id, kind="closed").one()
    assert (row.status, row.close_reason, closed.detail, closed.payload, summary.closed) == (
        "closed", "superseded", "superseded", None, {"superseded": 1},
    )


def test_a_member_who_never_ordered_is_never_reminded(store):
    (alice, eve), office = _office(store, "Alice", "Eve")
    [delivered_order(store, alice, at(n), address=office[alice.id]) for n in WEEKLY]

    run_engine(11)
    run_engine(46)

    assert ReorderReminderEpisode.query.filter_by(user_id=eve.id).count() == 0


def test_an_order_with_no_delivery_address_counts_only_for_its_orderer(db):
    """SQLite only: Postgres refuses a delivered order without an address
    (`ck_orders_address_required_after_pending`); legacy rows from before that check exist."""
    (alice, bob), office = _office(db, "Alice", "Bob")
    bob_orders = [delivered_order(db, bob, at(n), address=office[bob.id]) for n in (-24, -17, -10, -3)]
    delivered_order(db, alice, at(0), address=NO_ADDRESS)

    run_engine(8)

    assert only_lapse(bob_orders[-1]).supply_order_id == bob_orders[-1].id
