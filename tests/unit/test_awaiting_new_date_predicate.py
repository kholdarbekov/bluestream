"""An order awaiting a new date: the F1 predicate, its SQL twin, and what reads them.

A failed delivery never changes its order's status (F1 of
docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md), so the state
is derived: the delivery is FAILED and the order is still live.
`OrderScheduleService.awaiting_new_date` states it once and `awaiting_new_date_filter`
states it in SQL. The customer display, the forward-move guard, the re-dispatch guard and
the operator's failed list each read one of the two.

Every helper is pinned over the whole matrix: every delivery status x every order status,
plus every order status with no delivery row yet. The truth is the ruling, spelled out in
`AWAITING`, never recomputed from the code under test.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.services.staff_service import StaffService
from business_app.utils.exceptions import ValidationError
from business_app.utils.query_optimization import get_orders_with_details
from business_app.utils.state_validators import DELIVERY_DRIVERLESS_STATES
from shared.constants import ORDER_DISPLAY_AWAITING_NEW_DATE, ORDER_STATUS_ICONS
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

# F1 as the owner ruled it: a FAILED delivery on a live order. Spelled out, so a drift in
# either twin shows against the ruling rather than against the other twin.
AWAITING = {
    (DeliveryStatus.FAILED, OrderStatus.PENDING),
    (DeliveryStatus.FAILED, OrderStatus.CONFIRMED),
    (DeliveryStatus.FAILED, OrderStatus.PREPARING),
    (DeliveryStatus.FAILED, OrderStatus.OUT_FOR_DELIVERY),
}

# Every delivery status x every order status, plus each order status with no delivery row.
PAIRS = [
    (delivery_status, order_status)
    for delivery_status in [None, *DeliveryStatus]
    for order_status in OrderStatus
]
DELIVERY_PAIRS = [pair for pair in PAIRS if pair[0] is not None]

# `redispatch_block_code` keeps its codes (R18): any other delivery status names the
# delivery, and a FAILED row names its order once the order has left the active lifecycle.
REDISPATCH_CODE_FOR_A_FAILED_ROW = {
    OrderStatus.PENDING: None,
    OrderStatus.CONFIRMED: None,
    OrderStatus.PREPARING: None,
    OrderStatus.OUT_FOR_DELIVERY: None,
    OrderStatus.DELIVERED: "ORDER_NOT_RESCHEDULABLE",
    OrderStatus.CANCELLED: "ORDER_NOT_RESCHEDULABLE",
    OrderStatus.RETURNED: "ORDER_NOT_RESCHEDULABLE",
}

SCHEDULED_AT = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc)
FIRST_FAILURE = datetime(2026, 9, 20, 6, 15, tzinfo=timezone.utc)
REDATED = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
SECOND_FAILURE = datetime(2026, 9, 22, 7, 45, tzinfo=timezone.utc)
UNASKED_FAILURE = datetime(2026, 9, 23, 5, 30, tzinfo=timezone.utc)


def _pair_id(pair):
    delivery_status, order_status = pair
    return f"{delivery_status.value if delivery_status else 'no-row'}-{order_status.value}"


def _in_memory(pair):
    """The pair as unsaved rows: the predicate reads two attributes and needs no table."""
    delivery_status, order_status = pair
    order = Order(order_number=f"MEM-{_pair_id(pair)}", user_id=1, status=order_status, total_amount=1000)
    if delivery_status is not None:
        order.delivery = Delivery(status=delivery_status, scheduled_date=SCHEDULED_AT, scheduled_time_slot="anytime")
    return order


def build_status_matrix(db, customer, driver):
    """One saved order per pair in `PAIRS`, keyed by the pair.

    A driver sits on every row the Postgres CHECK lets carry one. A FAILED row carries
    what the failed branch writes: the reason, one attempt, and the driver kept.
    """
    orders = {}
    for delivery_status, order_status in PAIRS:
        order = Order(
            user_id=customer.id,
            order_number=f"AND-{_pair_id((delivery_status, order_status))}",
            status=order_status,
            subtotal=Decimal("15000.00"),
            delivery_fee=Decimal("0.00"),
            total_amount=Decimal("15000.00"),
            payment_method=PaymentMethod.CASH,
        )
        db.session.add(order)
        db.session.flush()
        if delivery_status is not None:
            failed = delivery_status == DeliveryStatus.FAILED
            db.session.add(
                Delivery(
                    order_id=order.id,
                    status=delivery_status,
                    delivery_person_id=None if delivery_status in DELIVERY_DRIVERLESS_STATES else driver.id,
                    scheduled_date=SCHEDULED_AT,
                    scheduled_time_slot="09:00-12:00",
                    failed_delivery_reason="customer_unavailable" if failed else None,
                    delivery_attempts=1 if failed else 0,
                )
            )
        orders[(delivery_status, order_status)] = order
    db.session.commit()
    return orders


def _naive_utc(value):
    """SQLite hands a timestamp back without its zone; compare both as UTC wall time."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


# --------------------------------------------------------------------------- #
# The predicate and what reads it, over the whole matrix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pair", PAIRS, ids=_pair_id)
def test_only_a_failed_delivery_on_a_live_order_awaits_a_new_date(app, pair):
    assert OrderScheduleService.awaiting_new_date(_in_memory(pair)) is (pair in AWAITING)


@pytest.mark.parametrize("pair", PAIRS, ids=_pair_id)
def test_the_customer_sees_the_wait_or_the_orders_own_status(app, pair):
    """F15. Every customer surface publishes this, and the bot looks its icon up by it,
    so every answer must have an icon."""
    display = OrderScheduleService.customer_display_status(_in_memory(pair))

    assert display == (ORDER_DISPLAY_AWAITING_NEW_DATE if pair in AWAITING else pair[1].value)
    assert display in ORDER_STATUS_ICONS


@pytest.mark.parametrize("pair", PAIRS, ids=_pair_id)
def test_only_an_order_awaiting_a_new_date_is_refused_a_forward_move(app, pair):
    """F18: the one guard that operator "Mark preparing", the admin status route and bulk
    `process` call before they write."""
    order = _in_memory(pair)

    if pair in AWAITING:
        with pytest.raises(ValidationError) as refused:
            OrderScheduleService.assert_can_advance(order)
        assert refused.value.error_code == "ORDER_AWAITING_NEW_DATE"
    else:
        assert OrderScheduleService.assert_can_advance(order) is None


@pytest.mark.parametrize("pair", DELIVERY_PAIRS, ids=_pair_id)
def test_redispatch_block_code_keeps_its_codes_and_asks_the_predicate(app, pair):
    delivery_status, order_status = pair
    order = _in_memory(pair)

    code = StaffService.redispatch_block_code(order.delivery)

    expected = (
        REDISPATCH_CODE_FOR_A_FAILED_ROW[order_status]
        if delivery_status == DeliveryStatus.FAILED
        else "STAFF_DELIVERY_NOT_REDISPATCHABLE"
    )
    assert code == expected
    assert (code is None) is OrderScheduleService.awaiting_new_date(order)


def test_redispatch_block_code_asks_the_predicate_for_the_awaiting_half(app, monkeypatch):
    """R18 keeps no copy of F1: the awaiting half is `awaiting_new_date`'s answer. A
    predicate that says yes for a cancelled order is obeyed, where any rule of the
    re-dispatch's own would refuse it."""
    order = _in_memory((DeliveryStatus.FAILED, OrderStatus.CANCELLED))
    asked = []

    def _awaiting(cls, asked_order):
        asked.append(asked_order)
        return True

    monkeypatch.setattr(OrderScheduleService, "awaiting_new_date", classmethod(_awaiting))

    assert StaffService.redispatch_block_code(order.delivery) is None
    assert asked == [order]


def test_the_awaiting_display_status_is_the_literal_every_client_keys_on():
    """The bots, the web pages and the translation keys spell it out, and it must never
    collide with a real order status."""
    assert ORDER_DISPLAY_AWAITING_NEW_DATE == "awaiting_new_date"
    assert ORDER_DISPLAY_AWAITING_NEW_DATE not in {status.value for status in OrderStatus}
    assert ORDER_STATUS_ICONS[ORDER_DISPLAY_AWAITING_NEW_DATE] == "⏳"


# --------------------------------------------------------------------------- #
# The SQL twin, and the operator list built on it
# --------------------------------------------------------------------------- #


def test_the_sql_twin_selects_exactly_the_orders_the_predicate_does(db, sample_user, delivery_driver):
    """Joined explicitly, as the clause requires. The negative half is in the same run:
    live orders whose delivery is anything but FAILED, or that have no row yet, sit
    beside the FAILED ones and must not be picked."""
    matrix = build_status_matrix(db, sample_user, delivery_driver)

    selected = {
        order.id
        for order in Order.query.join(Delivery, Delivery.order_id == Order.id)
        .filter(OrderScheduleService.awaiting_new_date_filter())
        .all()
    }

    assert selected == {matrix[pair].id for pair in AWAITING}
    assert selected == {order.id for order in matrix.values() if OrderScheduleService.awaiting_new_date(order)}


def test_the_predicate_costs_no_query_on_the_admin_list_load(db, sample_user, delivery_driver, count_queries):
    """The admin list loads its page through `get_orders_with_details`, which eager-loads
    the delivery, so asking every row issues no statement (spec §3.1)."""
    matrix = build_status_matrix(db, sample_user, delivery_driver)
    loaded = get_orders_with_details(Order.query).all()

    with count_queries() as counter:
        answers = {
            order.id: (
                OrderScheduleService.awaiting_new_date(order),
                OrderScheduleService.customer_display_status(order),
            )
            for order in loaded
        }

    assert counter.count == 0, counter.statements
    awaiting_ids = {order_id for order_id, (awaiting, _display) in answers.items() if awaiting}
    assert awaiting_ids == {matrix[pair].id for pair in AWAITING}


def test_the_operator_list_is_exactly_what_the_redispatch_accepts(db, sample_user, delivery_driver):
    matrix = build_status_matrix(db, sample_user, delivery_driver)
    deliveries = [order.delivery for order in matrix.values() if order.delivery is not None]

    listed = {delivery.id for delivery in StaffService.get_failed_deliveries()}

    assert listed == {matrix[pair].delivery.id for pair in AWAITING}
    assert listed == {delivery.id for delivery in deliveries if StaffService.redispatch_block_code(delivery) is None}


def test_the_count_is_the_whole_set_not_the_page(db, sample_user, delivery_driver):
    """F9: the list says "showing 2 of 4", so the count never reads the capped page."""
    build_status_matrix(db, sample_user, delivery_driver)

    assert len(StaffService.get_failed_deliveries(limit=2)) == 2
    assert StaffService.count_failed_deliveries() == len(AWAITING) == 4


def test_the_operator_list_and_its_count_read_the_sql_twin(db, sample_user, delivery_driver, monkeypatch):
    """Neither keeps a filter of its own (F1: one SQL twin). A twin that picks one
    delivered row, which the ruling never would, is exactly what both answer with."""
    matrix = build_status_matrix(db, sample_user, delivery_driver)
    delivered_id = matrix[(DeliveryStatus.DELIVERED, OrderStatus.DELIVERED)].delivery.id

    def _twin(cls):
        return Delivery.id == delivered_id

    monkeypatch.setattr(OrderScheduleService, "awaiting_new_date_filter", classmethod(_twin))

    assert [delivery.id for delivery in StaffService.get_failed_deliveries()] == [delivered_id]
    assert StaffService.count_failed_deliveries() == 1


def test_the_operator_list_loads_the_driver_and_the_order_with_its_rows(
    db, sample_user, delivery_driver, count_queries
):
    """A card names the order and the driver who attempted the door (the failed branch
    keeps `delivery_person_id`). Both come with the page, not one query per card."""
    matrix = build_status_matrix(db, sample_user, delivery_driver)
    expected_numbers = {matrix[pair].order_number for pair in AWAITING}
    # An empty identity map, as a request starts with: nothing is loaded already.
    db.session.expunge_all()

    deliveries = StaffService.get_failed_deliveries()
    with count_queries() as counter:
        cards = {(delivery.order.order_number, delivery.delivery_person.full_name) for delivery in deliveries}

    assert counter.count == 0, counter.statements
    assert cards == {(number, "Delivery Driver") for number in expected_numbers}


# --------------------------------------------------------------------------- #
# When it failed
# --------------------------------------------------------------------------- #


def _delivery(db, customer, number, status):
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(order_id=order.id, status=status, scheduled_date=SCHEDULED_AT, scheduled_time_slot="anytime")
    db.session.add(delivery)
    db.session.flush()
    return delivery


def _history(db, delivery, old_status, new_status, changed_at):
    db.session.add(
        DeliveryStatusHistory(
            delivery_id=delivery.id, old_status=old_status, new_status=new_status, changed_at=changed_at
        )
    )


def test_failed_at_is_the_latest_failure_of_each_delivery_asked_for(db, sample_user, count_queries):
    """Re-dated and failed again: the second failure. Failed once and re-dated since: that
    failure, not the re-date. Never failed, or not asked for: absent, so the caller picks
    its own fallback. One grouped query for the whole page."""
    failed_twice = _delivery(db, sample_user, "ORD-FAT-TWICE", DeliveryStatus.FAILED)
    _history(db, failed_twice, DeliveryStatus.ARRIVED, DeliveryStatus.FAILED, FIRST_FAILURE)
    _history(db, failed_twice, DeliveryStatus.FAILED, DeliveryStatus.SCHEDULED, REDATED)
    _history(db, failed_twice, DeliveryStatus.ARRIVED, DeliveryStatus.FAILED, SECOND_FAILURE)

    redated = _delivery(db, sample_user, "ORD-FAT-REDATED", DeliveryStatus.SCHEDULED)
    _history(db, redated, DeliveryStatus.ARRIVED, DeliveryStatus.FAILED, FIRST_FAILURE)
    _history(db, redated, DeliveryStatus.FAILED, DeliveryStatus.SCHEDULED, REDATED)

    never_failed = _delivery(db, sample_user, "ORD-FAT-NEVER", DeliveryStatus.PENDING)
    _history(db, never_failed, DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING, REDATED)

    not_asked = _delivery(db, sample_user, "ORD-FAT-UNASKED", DeliveryStatus.FAILED)
    _history(db, not_asked, DeliveryStatus.ARRIVED, DeliveryStatus.FAILED, UNASKED_FAILURE)
    db.session.commit()
    asked = [failed_twice.id, redated.id, never_failed.id]

    with count_queries() as counter:
        failed_at = OrderScheduleService.failed_at_by_delivery(asked)

    assert counter.count == 1, counter.statements
    assert {delivery_id: _naive_utc(at) for delivery_id, at in failed_at.items()} == {
        failed_twice.id: _naive_utc(SECOND_FAILURE),
        redated.id: _naive_utc(FIRST_FAILURE),
    }


def test_failed_at_asks_nothing_for_an_empty_page(db, count_queries):
    with count_queries() as counter:
        assert OrderScheduleService.failed_at_by_delivery([]) == {}

    assert counter.count == 0, counter.statements
