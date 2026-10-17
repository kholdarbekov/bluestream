"""`order_timing.delivered_instants_by_order`: the ONE delivered instant (spec 2026-09-28 §4.3.2, I-1, S-7).

`orders` has no delivered column. The DELIVERED row in `order_status_history` is the instant, and a
re-delivered order carries two such rows, so the answer is the LATEST. Sales-agent pay (the credit
month, the bonus window, the pipeline) and the outlet stage job (`ReplenishmentService.
delivered_instants`) both read this one function.
"""

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderStatusHistory
from business_app.models.sales import Outlet
from business_app.services.sales import replenishment_service
from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.utils import order_timing
from business_app.utils.order_timing import (
    DELIVERED_INSTANTS_CHUNK,
    delivered_instants_by_order,
    delivered_order_instants_select,
)
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus

pytestmark = pytest.mark.unit

UTC = timezone.utc
FIRST = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
SECOND = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)
THIRD = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
RETURNED_AT = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def _order(db, customer, number, *, status=OrderStatus.DELIVERED, order_id=None):
    order = Order(
        id=order_id,
        user_id=customer.id,
        order_number=number,
        status=status,
        subtotal=Decimal("40000.00"),
        total_amount=Decimal("40000.00"),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _history(db, order, old_status, new_status, changed_at):
    db.session.add(
        OrderStatusHistory(order_id=order.id, old_status=old_status, new_status=new_status, changed_at=changed_at)
    )
    db.session.commit()


def test_the_latest_delivered_row_is_the_instant(db, sample_user):
    """A re-delivered order answers with its second DELIVERED row. A returned order still has the
    instant it was delivered: whether it is STILL delivered is the caller's question (pay's
    `credit_still_earned`, the stage job's status filter). An order never delivered is absent,
    not None."""
    redelivered = _order(db, sample_user, "SA_000901_26")
    _history(db, redelivered, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, FIRST)
    _history(db, redelivered, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, SECOND)
    returned = _order(db, sample_user, "SA_000902_26", status=OrderStatus.RETURNED)
    _history(db, returned, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, THIRD)
    _history(db, returned, OrderStatus.DELIVERED, OrderStatus.RETURNED, RETURNED_AT)
    on_the_way = _order(db, sample_user, "SA_000903_26", status=OrderStatus.OUT_FOR_DELIVERY)
    _history(db, on_the_way, OrderStatus.CONFIRMED, OrderStatus.OUT_FOR_DELIVERY, FIRST)

    instants = delivered_instants_by_order([redelivered.id, returned.id, on_the_way.id])

    assert instants == {redelivered.id: SECOND, returned.id: THIRD}
    # SQLite hands the column back naive; the primitive answers in aware UTC either way.
    assert all(instant.utcoffset() == timedelta(0) for instant in instants.values())


def test_an_empty_list_asks_the_database_nothing(db, count_queries):
    with count_queries() as counter:
        assert delivered_instants_by_order([]) == {}

    assert counter.count == 0, counter.statements


def test_a_long_list_is_read_in_chunks_of_500(db, sample_user, count_queries):
    """A sync over a busy agent's lookback can pass thousands of ids. 1,400 ids are three chunks
    (1-500, 501-1000, 1001-1400) with one delivered order in each, so a chunk that was read and
    dropped would show. A repeated id is asked once."""
    assert DELIVERED_INSTANTS_CHUNK == 500
    placed = {}
    for order_id, delivered_at in ((1, FIRST), (700, SECOND), (1400, THIRD)):
        order = _order(db, sample_user, f"SA_{order_id:06d}_26", order_id=order_id)
        _history(db, order, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, delivered_at)
        placed[order_id] = delivered_at

    with count_queries() as counter:
        instants = delivered_instants_by_order(list(range(1, 1401)) + [700])

    assert instants == placed
    history_reads = [statement for statement in counter.statements if "order_status_history" in statement]
    assert len(history_reads) == 3, history_reads


def test_the_outlet_stage_job_asks_the_primitive(db, sample_user, second_sample_user, monkeypatch):
    """`ReplenishmentService.delivered_instants` keeps its own question (this outlet's orders that
    are still DELIVERED) and takes the instants from the primitive, never from a second
    `max(changed_at)`. The fake answers instants no history row holds, so a copy of the rule
    would answer with different ones (here: none at all)."""
    outlet = Outlet(name="Bahor market", outlet_type="grocery_store", stage="active", user_id=sample_user.id)
    db.session.add(outlet)
    db.session.commit()
    first = _order(db, sample_user, "SA_000911_26")
    second = _order(db, sample_user, "SA_000912_26")
    third = _order(db, sample_user, "SA_000913_26")
    _order(db, sample_user, "SA_000914_26", status=OrderStatus.OUT_FOR_DELIVERY)  # not landed yet
    _order(db, second_sample_user, "SA_000915_26")  # another customer's shop
    asked = []

    def _instants(order_ids):
        asked.append(sorted(order_ids))
        return {first.id: FIRST, second.id: THIRD, third.id: SECOND}

    monkeypatch.setattr(replenishment_service, "delivered_instants_by_order", _instants)

    assert ReplenishmentService.delivered_instants(outlet, limit=2) == [THIRD, SECOND]
    assert asked == [sorted([first.id, second.id, third.id])]


def test_the_outlet_reads_its_whole_history_when_no_limit_is_given(db, sample_user, monkeypatch):
    """§9.3: the stage job asks for FULL history, because the usual gap needs six delivery DAYS and
    a same-day burst makes six orders fewer days. Newest first, every landed order."""
    outlet = Outlet(name="Bahor market", outlet_type="grocery_store", stage="active", user_id=sample_user.id)
    db.session.add(outlet)
    db.session.commit()
    first = _order(db, sample_user, "SA_000916_26")
    second = _order(db, sample_user, "SA_000917_26")
    third = _order(db, sample_user, "SA_000918_26")
    monkeypatch.setattr(
        replenishment_service,
        "delivered_instants_by_order",
        lambda order_ids: {first.id: FIRST, second.id: SECOND, third.id: THIRD},
    )

    assert ReplenishmentService.delivered_instants(outlet) == [THIRD, SECOND, FIRST]


def _select_rows(db, *criteria):
    statement = delivered_order_instants_select()
    for criterion in criteria:
        statement = statement.where(criterion)
    return [
        (row.order_id, row.user_id, row.delivery_address_id, ensure_utc(row.delivered_at))
        for row in db.session.execute(statement.order_by(Order.id)).all()
    ]


def test_the_select_lists_each_still_delivered_order_once_with_its_latest_instant(
    db, sample_user, user_address
):
    """One row per order whose CURRENT status is DELIVERED, dated by its latest DELIVERED row and
    carrying the address it went to. A returned order is no longer a delivery; a legacy delivered
    order with no DELIVERED history row has no instant and is left out, never dated NULL (Review
    Focus 2); an order on its way is not delivered yet."""
    redelivered = _order(db, sample_user, "SA_000921_26")
    redelivered.delivery_address_id = user_address.id
    db.session.commit()
    _history(db, redelivered, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, FIRST)
    _history(db, redelivered, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, SECOND)
    returned = _order(db, sample_user, "SA_000922_26", status=OrderStatus.RETURNED)
    _history(db, returned, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, THIRD)
    _order(db, sample_user, "SA_000923_26")  # legacy: DELIVERED, no history row
    on_the_way = _order(db, sample_user, "SA_000924_26", status=OrderStatus.OUT_FOR_DELIVERY)
    _history(db, on_the_way, OrderStatus.CONFIRMED, OrderStatus.OUT_FOR_DELIVERY, FIRST)

    assert _select_rows(db, Order.user_id == sample_user.id) == [
        (redelivered.id, sample_user.id, user_address.id, SECOND)
    ]


def test_the_select_and_the_per_order_reader_answer_with_the_same_instant(db, sample_user, second_sample_user):
    """Two public readers, one rule: for every still-delivered order they agree to the second."""
    ids = []
    for customer, number, instants in (
        (sample_user, "SA_000931_26", (FIRST,)),
        (sample_user, "SA_000932_26", (FIRST, THIRD)),
        (second_sample_user, "SA_000933_26", (SECOND,)),
    ):
        order = _order(db, customer, number)
        for instant in instants:
            _history(db, order, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, instant)
        ids.append(order.id)

    from_select = {order_id: delivered_at for order_id, _user, _address, delivered_at in _select_rows(db)}

    assert from_select == delivered_instants_by_order(ids)
    assert from_select == {ids[0]: FIRST, ids[1]: THIRD, ids[2]: SECOND}


def test_the_latest_delivered_rule_is_spelled_once():
    """§4: both readers are built from ONE building block. A second `max(changed_at)` is a second
    rule that can drift."""
    source = inspect.getsource(order_timing)

    assert source.count("func.max(OrderStatusHistory.changed_at)") == 1
    assert source.count("OrderStatusHistory.new_status == OrderStatus.DELIVERED") == 1
