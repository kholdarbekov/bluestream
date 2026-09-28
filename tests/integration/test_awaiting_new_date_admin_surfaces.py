"""The admin surfaces that flag an order awaiting a new date (F8), and the predicate parity (§8 item 11).

A failed delivery never changes its order's status (F1 of
docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md). Staff can only
find such an order through what the backend publishes:
- every `GET /admin/orders` row and the `GET /admin/orders/<id>` detail carry
  `awaiting_new_date`, from `order_schedule_fields`, the one helper both payloads share;
- `GET /admin/orders?delivery_failed=true`, the Orders page's "Delivery failed" toggle,
  selects by `OrderScheduleService.awaiting_new_date_filter()`, the predicate's SQL twin;
- every dispatch snapshot order entry carries `awaiting_new_date`, for the map's "needs a
  new date" pin.

The operator bot's failed list (`GET /staff/delivery/failed`) reads the same SQL twin. The
parity test drives both lists over one fixture: every delivery x order status pair, plus
each order status with no delivery row. The truth is the ruling (`AWAITING`), never
recomputed from the code under test.

Most FAILED rows here are built by hand. One test lets the driver write them instead,
through the staff bot's own taps, once per failure reason (spec §8 item 2, the admin half),
so the three admin surfaces read whatever the failed branch really leaves behind.

Admin routes get a role-claim token, the operator route an operator token and the driver
route the driver's token. Each request carries the query or body its client sends. Faked:
Celery publishing only. The driver-failure test records the failure's two publishes with
signature-enforcing spies.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import UserAddress
from business_app.services.dispatch_service import DispatchService
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.tasks import notification_tasks, staff_tasks
from business_app.utils.state_validators import DELIVERY_DRIVERLESS_STATES
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from shared.staff_constants import FAILED_DELIVERY_REASONS
from tests.integration.test_delivery_failed_alert import _driver_fails, _stop
from tests.integration.test_staff_delivery_ownership_guard import _bearer, _driver
from tests.unit.test_awaiting_new_date_predicate import AWAITING, build_status_matrix
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/admin/orders"
ORDER_DETAIL = "/api/v1/admin/orders/{}"
FAILED_LIST = "/api/v1/staff/delivery/failed"
DISPATCH_SNAPSHOT = "/api/v1/admin/dispatch/snapshot"
DRIVER_STATUS = "/api/v1/staff/delivery/{}/status"

# What the Orders page sends on first load (admin_ui/src/pages/Orders.js, `adminService.getOrders`).
# The empty search and status go along as empty strings.
ORDERS_PAGE_QUERY = {"page": 1, "per_page": 20, "search": "", "status": ""}
# The same, with the "Delivery failed" toggle on.
DELIVERY_FAILED_QUERY = {**ORDERS_PAGE_QUERY, "delivery_failed": "true"}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _get(client, path, headers, query=None):
    response = client.get(path, query_string=query, headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def _order(db, customer, driver, number, *, order_status, delivery_status, due):
    """An order due on `due` at a geocoded Tashkent address, with its delivery in `delivery_status`.

    `delivery_status=None` means no delivery row yet. A driver sits on every row the
    Postgres CHECK lets carry one. A FAILED row carries what the failed branch writes: the
    reason and one attempt, with the driver kept.
    """
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address=f"{number} Chilonzor 9, Tashkent",
        street_address="Chilonzor 9",
        city="Tashkent",
        latitude=41.29,
        longitude=69.21,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=order_status,
        subtotal=Decimal("45000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("45000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        delivery_date=due,
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
                scheduled_date=datetime.combine(due, time(9, 0), tzinfo=ZoneInfo(DISPLAY_TIMEZONE)),
                scheduled_time_slot="09:00-12:00",
                failed_delivery_reason="customer_unavailable" if failed else None,
                delivery_attempts=1 if failed else 0,
            )
        )
    db.session.commit()
    return order


def _one_waiting_beside_two_live_orders(db, customer, driver, due):
    """One order whose delivery failed, beside two live orders that do not wait.

    One has a driver on it and the other has no delivery row yet. These are the two a
    join-less filter would pick up.
    """
    return {
        "waiting": _order(
            db,
            customer,
            driver,
            "ORD-ANDA-WAIT",
            order_status=OrderStatus.OUT_FOR_DELIVERY,
            delivery_status=DeliveryStatus.FAILED,
            due=due,
        ),
        "with_driver": _order(
            db,
            customer,
            driver,
            "ORD-ANDA-DRIVER",
            order_status=OrderStatus.CONFIRMED,
            delivery_status=DeliveryStatus.ASSIGNED,
            due=due,
        ),
        "no_row": _order(
            db, customer, driver, "ORD-ANDA-NOROW", order_status=OrderStatus.CONFIRMED, delivery_status=None, due=due
        ),
    }


# --------------------------------------------------------------------------- #
# The "Delivery failed" filter and the operator list: one set
# --------------------------------------------------------------------------- #


def test_the_delivery_failed_filter_and_the_operator_list_pick_exactly_the_orders_awaiting_a_new_date(
    client, db, admin_claim_headers, operator_auth_headers, sample_user, delivery_driver
):
    """Both lists read the SQL twin, so over one fixture they pick the same orders: the
    ones the ruling names.

    The negative half sits in the same fixture:
    - live orders whose delivery is anything but FAILED;
    - live orders with no row;
    - FAILED deliveries on dead orders.

    `total` is the whole set: at two a page it still says four.
    """
    matrix = build_status_matrix(db, sample_user, delivery_driver)
    expected = {matrix[pair].id for pair in AWAITING}

    admin = _get(client, ORDERS, admin_claim_headers, DELIVERY_FAILED_QUERY)

    assert admin["meta"]["total"] == len(AWAITING)
    rows = admin["data"]["items"]
    assert {row["id"] for row in rows} == expected
    assert {row["id"] for row in rows} == {
        order.id for order in matrix.values() if OrderScheduleService.awaiting_new_date(order)
    }
    assert {row["awaiting_new_date"] for row in rows} == {True}

    operator = _get(client, FAILED_LIST, operator_auth_headers)["data"]

    assert {item["order_id"] for item in operator["items"]} == expected
    assert operator["total"] == len(AWAITING)

    first_page = _get(client, ORDERS, admin_claim_headers, {**DELIVERY_FAILED_QUERY, "per_page": 2})

    assert len(first_page["data"]["items"]) == 2
    assert (first_page["meta"]["total"], first_page["meta"]["pages"]) == (len(AWAITING), 2)
    assert {row["id"] for row in first_page["data"]["items"]} <= expected


def test_the_toggle_narrows_within_the_status_filter(client, db, admin_claim_headers, sample_user, delivery_driver):
    """An admin can pick a status and turn the toggle on together.

    `filter_by(status=...)` binds to the last joined entity, so the delivery join must
    come after it. Joined first, the status would be read off the delivery. Each status
    returns only its own waiting order, and a dead status returns none.
    """
    matrix = build_status_matrix(db, sample_user, delivery_driver)

    for order_status in OrderStatus:
        body = _get(client, ORDERS, admin_claim_headers, {**DELIVERY_FAILED_QUERY, "status": order_status.value})

        expected = {matrix[pair].id for pair in AWAITING if pair[1] == order_status}
        assert body["meta"]["total"] == len(expected), order_status.value
        assert {row["id"] for row in body["data"]["items"]} == expected, order_status.value


def test_while_one_order_waits_neither_list_picks_a_live_order_that_does_not(
    client, db, admin_claim_headers, operator_auth_headers, sample_user, delivery_driver
):
    """§8 item 11's negative case. The SQL twin is a bare clause.

    Without its explicit join, `deliveries` enters FROM unjoined, and the one FAILED
    delivery pairs with every live order. The order a driver has and the order with no
    row yet would come back with it, and `total` would count them. Only the waiting order
    is picked, and its row and detail say so while the other two say not.
    """
    orders = _one_waiting_beside_two_live_orders(db, sample_user, delivery_driver, DispatchService.today())
    waiting_id = orders["waiting"].id
    flags = {order.id: name == "waiting" for name, order in orders.items()}

    admin = _get(client, ORDERS, admin_claim_headers, DELIVERY_FAILED_QUERY)
    operator = _get(client, FAILED_LIST, operator_auth_headers)["data"]

    assert (admin["meta"]["total"], [row["id"] for row in admin["data"]["items"]]) == (1, [waiting_id])
    assert (operator["total"], [item["order_id"] for item in operator["items"]]) == (1, [waiting_id])

    rows = _get(client, ORDERS, admin_claim_headers, ORDERS_PAGE_QUERY)["data"]["items"]
    assert {row["id"]: row["awaiting_new_date"] for row in rows} == flags
    details = {
        order_id: _get(client, ORDER_DETAIL.format(order_id), admin_claim_headers)["data"]["order"]["awaiting_new_date"]
        for order_id in flags
    }
    assert details == flags


def test_the_toggle_narrows_within_the_search_and_the_date_range(
    client, db, admin_claim_headers, sample_user, delivery_driver
):
    """The Orders page sends the search box, the date range and the toggle together
    (admin_ui/src/pages/Orders.js).

    - The search joins `users` ahead of the delivery join.
    - The range filters `Order.created_at` by the day strings the RangePicker sends.

    "ORD-ANDA" matches all three order numbers, and a range around the day they were made
    holds all three, so each narrowing must still come down to the waiting order, counted
    once. A range that ends before that day holds none. The toggle also takes the other
    spellings `fiscalization_failed` takes.
    """
    orders = _one_waiting_beside_two_live_orders(db, sample_user, delivery_driver, DispatchService.today())
    waiting_id = orders["waiting"].id
    made_on = orders["waiting"].created_at.date()
    around = {
        "start_date": (made_on - timedelta(days=1)).isoformat(),
        "end_date": (made_on + timedelta(days=1)).isoformat(),
    }
    before = {
        "start_date": (made_on - timedelta(days=2)).isoformat(),
        "end_date": (made_on - timedelta(days=1)).isoformat(),
    }

    unfiltered = _get(client, ORDERS, admin_claim_headers, {**ORDERS_PAGE_QUERY, "search": "ORD-ANDA", **around})
    assert unfiltered["meta"]["total"] == 3

    for query in (
        {**DELIVERY_FAILED_QUERY, "search": "ORD-ANDA"},
        {**DELIVERY_FAILED_QUERY, **around},
        {**DELIVERY_FAILED_QUERY, "search": "ORD-ANDA", **around},
        {**DELIVERY_FAILED_QUERY, "delivery_failed": "1"},
        {**DELIVERY_FAILED_QUERY, "delivery_failed": "Yes"},
        {**DELIVERY_FAILED_QUERY, "delivery_failed": "TRUE"},
    ):
        body = _get(client, ORDERS, admin_claim_headers, query)
        assert (body["meta"]["total"], [row["id"] for row in body["data"]["items"]]) == (1, [waiting_id]), query

    empty = _get(client, ORDERS, admin_claim_headers, {**DELIVERY_FAILED_QUERY, **before})
    assert (empty["meta"]["total"], empty["data"]["items"]) == (0, [])


# --------------------------------------------------------------------------- #
# A driver's real failure reaches every admin order surface (§8 item 2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reason", FAILED_DELIVERY_REASONS)
def test_a_driver_failure_with_any_reason_is_flagged_on_the_detail_and_the_list_and_found_by_the_filter(
    app, client, db, admin_claim_headers, sample_user, monkeypatch, reason
):
    """Spec §8 backend item 2, the admin half. Task 10 pins the other half: the order status
    stays and one alert is queued.

    The driver's own taps write the failure, through the route and body the staff bot
    sends, so the detail, the list row and the filter read whatever the failed branch
    really leaves behind. The two spies record the failure's post-commit publishes, which
    shows the branch ran and committed. `_stop` puts an earlier cancelled order first, so
    the order id and the delivery id differ, and a join on the wrong id would find nothing.
    """
    driver = _driver(db, phone="+998901610001", first_name="Oybek")
    delivery = _stop(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-F8-DOOR")
    delivery_id, order_id = delivery.id, delivery.order_id
    picked_up = client.put(
        DRIVER_STATUS.format(delivery_id), json={"status": "picked_up"}, headers=_bearer(app, driver)
    )
    assert picked_up.status_code == 200, picked_up.get_data(as_text=True)
    # Recorded from here on, so they hold the failure's publishes only.
    alerts = _task_spy(monkeypatch, staff_tasks.notify_delivery_failed, "delay")
    customer_updates = _task_spy(monkeypatch, notification_tasks.send_delivery_update_task, "delay")

    _driver_fails(app, client, driver, delivery_id, reason)

    failed = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id, new_status=DeliveryStatus.FAILED).one()
    assert (failed.reason, alerts, customer_updates) == (
        reason,
        [((delivery_id, driver.id), {})],
        [((failed.id,), {})],
    )

    detail = _get(client, ORDER_DETAIL.format(order_id), admin_claim_headers)["data"]["order"]
    assert (detail["status"], detail["awaiting_new_date"]) == ("out_for_delivery", True)

    rows = _get(client, ORDERS, admin_claim_headers, ORDERS_PAGE_QUERY)["data"]["items"]
    assert {row["order_number"]: row["awaiting_new_date"] for row in rows} == {
        "ORD-F8-DOOR": True,
        "ORD-F8-DOOR-EARLIER": False,
    }

    waiting = _get(client, ORDERS, admin_claim_headers, DELIVERY_FAILED_QUERY)
    assert (waiting["meta"]["total"], [row["id"] for row in waiting["data"]["items"]]) == (1, [order_id])


# --------------------------------------------------------------------------- #
# The flag on every row the admin sees
# --------------------------------------------------------------------------- #


def test_every_admin_list_row_says_whether_it_awaits_a_new_date(
    client, db, admin_claim_headers, sample_user, delivery_driver
):
    """F8. The Orders page's Alerts tag reads `awaiting_new_date` off the row.

    The check covers every pair on one page: the backend's cap of 100 holds the whole
    fixture.
    """
    matrix = build_status_matrix(db, sample_user, delivery_driver)

    rows = _get(client, ORDERS, admin_claim_headers, {**ORDERS_PAGE_QUERY, "per_page": 100})["data"]["items"]

    assert {row["id"]: row["awaiting_new_date"] for row in rows} == {
        order.id: pair in AWAITING for pair, order in matrix.items()
    }


def test_each_dispatch_order_says_whether_it_needs_a_new_date(
    client, db, admin_claim_headers, sample_user, delivery_driver
):
    """F8: the dispatch map's "needs a new date" pin, for the day the map asks about
    (admin_ui/src/pages/Dispatch.js sends `date`).

    A failed row keeps its driver, so `driver_id` alone would draw it as that driver's
    live stop. The entry carries the backend's answer, and the map reads that instead of
    working it out.
    """
    today = DispatchService.today()
    orders = _one_waiting_beside_two_live_orders(db, sample_user, delivery_driver, today)

    snapshot = _get(client, DISPATCH_SNAPSHOT, admin_claim_headers, {"date": today.isoformat()})["data"]

    entries = {entry["order_id"]: entry for entry in snapshot["orders"]}
    waiting = entries[orders["waiting"].id]
    assert (waiting["awaiting_new_date"], waiting["delivery_status"], waiting["driver_id"]) == (
        True,
        "failed",
        delivery_driver.id,
    )
    assert {name: entries[order.id]["awaiting_new_date"] for name, order in orders.items()} == {
        "waiting": True,
        "with_driver": False,
        "no_row": False,
    }
