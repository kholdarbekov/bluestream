"""A customer whose delivery failed sees the wait for a new date, not a stale ETA (F15).

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §4.2, §8 item 10.

A failure never changes the order's status (F1). So every customer read publishes
`display_status`, from `OrderScheduleService.customer_display_status`, and the clients only
label it. While the order waits:
- the timeline's current step is the wait, dated as the operator's failed list dates it
  (spec §3.5): the latest FAILED history row, else the delivery's last update;
- the countdown to the failed attempt's ETA goes.

None of it is stored, so a reschedule takes all of it away.

Driven through the routes the bot and the web call, with the customer's own token:
`GET /orders`, `GET /orders/<id>` and `GET /orders/<id>/track`. The re-date is the admin
Reschedule modal's `PATCH /admin/orders/<id>/schedule`, under the shared `local_noon` clocks
(tests/integration/conftest.py).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple, Tuple

import pytest
from sqlalchemy import text

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order, OrderStatusHistory
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod

pytestmark = pytest.mark.integration

ORDERS_URL = "/api/v1/orders/"
ORDER_URL = "/api/v1/orders/{order_id}"
TRACK_URL = "/api/v1/orders/{order_id}/track"
SCHEDULE_URL = "/api/v1/admin/orders/{order_id}/schedule"
REASON = "Mijoz ertaga uyda bo'ladi"

# The literal the bots, the web pages and the translation keys spell out.
AWAITING = "awaiting_new_date"

# Short names keep each shape readable.
OS, DS = OrderStatus, DeliveryStatus


class Shape(NamedTuple):
    name: str
    order_status: OrderStatus
    delivery_status: DeliveryStatus
    # (old, new, hours ago), oldest first: the history production wrote on the way here.
    order_history: Tuple[Tuple[OrderStatus, OrderStatus, int], ...]
    delivery_history: Tuple[Tuple[DeliveryStatus, DeliveryStatus, int], ...]
    # What the customer is shown, and the steps of the timeline they read.
    display_status: str
    steps: Tuple[str, ...]
    # When the current step happened, in hours ago.
    current_step_hours_ago: int


# Confirmed before anything else happened to the order.
CONFIRMED = (OS.PENDING, OS.CONFIRMED, 5)

# The driver accepted, then could not deliver before pickup, so the order is still confirmed.
FAILED_BEFORE_PICKUP = Shape(
    "failed-before-pickup",
    OS.CONFIRMED,
    DS.FAILED,
    (CONFIRMED,),
    ((DS.PENDING, DS.ASSIGNED, 2), (DS.ASSIGNED, DS.FAILED, 1)),
    AWAITING,
    ("created", "confirmed", AWAITING),
    1,
)

# Returned on the Orders page and put back to pending while its delivery stayed failed
# (spec §3.3). A pending order's creation step reads as current, so this is the order that
# could show two current steps. The wait keeps the failure's stamp, which is older than the
# two steps above it.
FAILED_THEN_RETURNED_TO_PENDING = Shape(
    "failed-then-returned-to-pending",
    OS.PENDING,
    DS.FAILED,
    (CONFIRMED, (OS.OUT_FOR_DELIVERY, OS.RETURNED, 3), (OS.RETURNED, OS.PENDING, 2)),
    ((DS.ARRIVED, DS.FAILED, 4),),
    AWAITING,
    ("created", "confirmed", "returned", "pending", AWAITING),
    4,
)

# Failed through the legacy issue writer (removed in Task 3). It set `failed` and stamped
# `updated_at` but wrote no history row (tasks/delivery_tasks.py ~719-723). The wait is dated
# by that last update, which `_seed` stamps at `now`.
FAILED_WITH_NO_HISTORY_ROW = Shape(
    "failed-with-no-history-row",
    OS.OUT_FOR_DELIVERY,
    DS.FAILED,
    (CONFIRMED,),
    (),
    AWAITING,
    ("created", "confirmed", AWAITING),
    0,
)

# A driver has it and nothing failed, so the order's own last step is the current one.
ACCEPTED_BY_A_DRIVER = Shape(
    "accepted-by-a-driver",
    OS.CONFIRMED,
    DS.ASSIGNED,
    (CONFIRMED,),
    ((DS.PENDING, DS.ASSIGNED, 1),),
    "confirmed",
    ("created", "confirmed"),
    5,
)

# Failed, then cancelled on purpose: the order no longer waits for anything.
FAILED_THEN_CANCELLED = Shape(
    "failed-then-cancelled",
    OS.CANCELLED,
    DS.FAILED,
    (CONFIRMED, (OS.OUT_FOR_DELIVERY, OS.CANCELLED, 1)),
    ((DS.ARRIVED, DS.FAILED, 2),),
    "cancelled",
    ("created", "confirmed", "cancelled"),
    1,
)

SHAPES = [
    FAILED_BEFORE_PICKUP,
    FAILED_THEN_RETURNED_TO_PENDING,
    FAILED_WITH_NO_HISTORY_ROW,
    ACCEPTED_BY_A_DRIVER,
    FAILED_THEN_CANCELLED,
]

# Failed, re-dated, then failed again (plan Review Focus 4). The first re-date took the order
# back to confirmed (R6), and the pickup sync wrote no history on the way out again.
FAILED_TWICE = Shape(
    "failed-twice",
    OS.OUT_FOR_DELIVERY,
    DS.FAILED,
    (CONFIRMED, (OS.OUT_FOR_DELIVERY, OS.CONFIRMED, 3)),
    ((DS.ARRIVED, DS.FAILED, 4), (DS.FAILED, DS.SCHEDULED, 3), (DS.ARRIVED, DS.FAILED, 1)),
    AWAITING,
    ("created", "confirmed", "confirmed", AWAITING),
    1,
)


def _seed(db, customer, address, driver, shape, now):
    """One customer order, left the way production leaves it in `shape`.

    The delivery keeps the driver who attempted the door and the ETA stamped for that
    attempt. A failed tap clears neither (spec §1).
    """
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-F15-{shape.name}",
        status=shape.order_status,
        payment_method=PaymentMethod.CASH,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        delivery_address_id=address.id,
        created_at=now - timedelta(hours=6),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=shape.delivery_status,
        delivery_person_id=driver.id,
        scheduled_date=now - timedelta(hours=6),
        scheduled_time_slot="anytime",
        estimated_delivery_time=datetime.now(timezone.utc) + timedelta(hours=2),
        delivery_attempts=sum(1 for _old, new, _ago in shape.delivery_history if new is DS.FAILED),
        failed_delivery_reason="customer_unavailable" if shape.delivery_status is DS.FAILED else None,
        # The row's last update, later than every failure seeded below: a note edit bumps it.
        # A wait dated by it while a FAILED history row exists would show.
        updated_at=now,
    )
    db.session.add(delivery)
    db.session.flush()
    for old, new, hours_ago in shape.order_history:
        db.session.add(
            OrderStatusHistory(
                order_id=order.id, old_status=old, new_status=new, changed_at=now - timedelta(hours=hours_ago)
            )
        )
    for old, new, hours_ago in shape.delivery_history:
        db.session.add(
            DeliveryStatusHistory(
                delivery_id=delivery.id, old_status=old, new_status=new, changed_at=now - timedelta(hours=hours_ago)
            )
        )
    db.session.commit()
    return order.id


def _get(client, url, headers):
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]


def _customer_reads(client, headers, order_id):
    """What the bot and the web read for one order: its list row, its detail, its Track."""
    (listed,) = [row for row in _get(client, ORDERS_URL, headers)["orders"] if row["id"] == order_id]
    detail = _get(client, ORDER_URL.format(order_id=order_id), headers)
    track = _get(client, TRACK_URL.format(order_id=order_id), headers)
    return listed, detail, track


def _one_current_step_last(steps):
    """Every step in order, and only the last one current."""
    return [(step, index == len(steps) - 1) for index, step in enumerate(steps)]


def _steps(timeline):
    return [(entry["status"], entry["is_current"]) for entry in timeline]


def _stamp(entry):
    """An entry's timestamp as aware UTC. SQLite hands it back without its zone; the app writes UTC."""
    stamp = datetime.fromisoformat(entry["timestamp"])
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def test_the_customer_sees_the_wait_until_staff_pick_a_new_date(
    client, db, auth_headers, admin_claim_headers, sample_user, user_address, delivery_driver, local_noon
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    order_id = _seed(db, sample_user, user_address, delivery_driver, FAILED_TWICE, now)

    listed, detail, track = _customer_reads(client, auth_headers, order_id)

    # The order keeps its real status (F1). Every read publishes the wait as what to show.
    for published in (listed, detail["order"], track["order"]):
        assert "error" not in published, f"the degraded fallback answered: {published}"
        assert (published["status"], published["display_status"]) == ("out_for_delivery", AWAITING)
    # The ETA was stamped for the attempt that failed, and nothing is on its way now.
    assert track["estimated_time_remaining"] is None
    # One current step, the wait. It is stamped with the second failure, not the first and
    # not the row's later last update, and it is the whole entry: no `reason`, no staff text.
    for timeline in (detail["timeline"], track["timeline"]):
        assert _steps(timeline) == _one_current_step_last(FAILED_TWICE.steps)
        wait = timeline[-1]
        assert {**wait, "timestamp": _stamp(wait)} == {
            "status": AWAITING,
            "timestamp": now - timedelta(hours=1),
            "notes": None,
            "is_current": True,
        }

    # The admin Orders page re-dates it for tomorrow, with the body the Reschedule modal sends.
    tomorrow = local_noon.date() + timedelta(days=1)
    resp = client.patch(
        SCHEDULE_URL.format(order_id=order_id),
        headers=admin_claim_headers,
        json={
            "delivery_date": tomorrow.isoformat(),
            "delivery_window_start": None,
            "delivery_window_end": None,
            "reason": REASON,
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    listed, detail, track = _customer_reads(client, auth_headers, order_id)

    # Nothing was stored for the wait, so it is gone. The re-date moved the order back to
    # confirmed (R6), and that history row is now the current step.
    for published in (listed, detail["order"], track["order"]):
        assert (published["status"], published["display_status"]) == ("confirmed", "confirmed")
    for timeline in (detail["timeline"], track["timeline"]):
        assert _steps(timeline) == _one_current_step_last(("created", "confirmed", "confirmed", "confirmed"))


@pytest.mark.parametrize("shape", SHAPES, ids=[shape.name for shape in SHAPES])
def test_the_wait_is_the_current_step_only_while_the_order_awaits_a_new_date(
    client, db, auth_headers, sample_user, user_address, delivery_driver, shape
):
    """F1's two halves on the customer's reads.
    - A failed delivery on a live order waits, whatever the order's status.
    - A delivery that did not fail, or a failed one on an order cancelled since, shows the
      order's own last step.

    Either way, exactly one step is current."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    order_id = _seed(db, sample_user, user_address, delivery_driver, shape, now)

    listed, detail, track = _customer_reads(client, auth_headers, order_id)

    for published in (listed, detail["order"], track["order"]):
        assert (published["status"], published["display_status"]) == (
            shape.order_status.value,
            shape.display_status,
        )
    for timeline in (detail["timeline"], track["timeline"]):
        assert _steps(timeline) == _one_current_step_last(shape.steps)
        assert _stamp(timeline[-1]) == now - timedelta(hours=shape.current_step_hours_ago)


@pytest.mark.parametrize(
    "shape, counts_down",
    [(FAILED_BEFORE_PICKUP, False), (ACCEPTED_BY_A_DRIVER, True)],
    ids=["awaiting-a-new-date", "accepted-by-a-driver"],
)
def test_only_the_wait_hides_the_countdown(
    client, db, auth_headers, sample_user, user_address, delivery_driver, shape, counts_down
):
    """Both deliveries carry the same ETA, two hours out. A failed tap leaves it on the row,
    so the Track screen would count down to an attempt that has already ended."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    order_id = _seed(db, sample_user, user_address, delivery_driver, shape, now)

    remaining = _get(client, TRACK_URL.format(order_id=order_id), auth_headers)["estimated_time_remaining"]

    assert (remaining is not None) is counts_down, remaining


@pytest.mark.parametrize(
    "legacy_update, own_status",
    [
        ("UPDATE orders SET discount_amount = NULL WHERE id = :order_id", "out_for_delivery"),
        ("UPDATE orders SET status = NULL WHERE id = :order_id", None),
    ],
    ids=["null-discount-amount", "null-status"],
)
def test_a_row_the_serializer_cannot_finish_still_answers_with_its_own_status(
    client, db, auth_headers, sample_user, user_address, delivery_driver, legacy_update, own_status
):
    """The degraded answer publishes the order's own status and never asks the predicate
    again. The predicate reads a relationship, so a second raise inside the fallback would
    turn one bad row into a 500 for the customer's whole order list.

    Two legacy rows the full serializer cannot finish, both through nullable columns:
    - a NULL `discount_amount`, which it cannot turn into a number;
    - a NULL `status`. The order then has no status of its own, and reading one must not
      raise a second time."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    order_id = _seed(db, sample_user, user_address, delivery_driver, FAILED_TWICE, now)
    db.session.execute(text(legacy_update), {"order_id": order_id})
    db.session.commit()

    (listed,) = [row for row in _get(client, ORDERS_URL, auth_headers)["orders"] if row["id"] == order_id]

    assert "error" in listed, f"the full serializer answered, so this proves nothing about the fallback: {listed}"
    assert listed["display_status"] == own_status
