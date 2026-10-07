"""`order_timing.delivered_instants_by_order`: the ONE delivered instant (spec 2026-09-28 §4.3.2, I-1, S-7).

`orders` has no delivered column. The DELIVERED row in `order_status_history` is the instant, and a
re-delivered order carries two such rows, so the answer is the LATEST. Sales-agent pay (the credit
month, the bonus window, the pipeline) and the outlet stage job (`ReplenishmentService.
delivered_instants`) both read this one function.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from business_app.models.order import Order, OrderStatusHistory
from business_app.models.sales import Outlet
from business_app.services.sales import replenishment_service
from business_app.services.sales.replenishment_service import ReplenishmentService
from business_app.utils.order_timing import DELIVERED_INSTANTS_CHUNK, delivered_instants_by_order
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
