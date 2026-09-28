"""F18: an order whose delivery failed cannot be moved forward by any writer.

While its delivery is `failed` and the order is still active, the order is waiting for a
new date (`OrderScheduleService.awaiting_new_date`). Moving it to `preparing` or
`out_for_delivery` would queue the customer "your order is being prepared" / "out for
delivery" while every customer surface says "Awaiting a new delivery date" and nobody is
coming. One guard decides it, `OrderScheduleService.assert_can_advance`. Each of the three
writers is driven here through the route its client calls:

* the admin Update Status modal: `PUT /api/v1/admin/orders/<id>/status`, with the body
  `adminService.updateOrderStatus` sends (`{status, notes}`);
* the operator's "Mark preparing" (both staff-bot buttons): `PUT /api/v1/staff/orders/<id>/preparing`,
  no body;
* bulk `process`: `POST /api/v1/admin/bulk-actions`, with the body
  `adminService.performBulkAction` builds (`{action, target_type, target_ids, parameters}`).

A refused move writes nothing, and on the two routes that notify it queues nothing. A move
on an order whose delivery did not fail still goes through, so the guard does not over-refuse.

Mocked: only the Celery publish of `send_order_notification_task`, through a recorder bound
to the task's own signature. The refusal text is read from the guard itself, never restated.

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md (F18, §3.2).
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.user import UserAddress
from business_app.services.order_schedule_service import OrderScheduleService
from business_app.tasks.notification_tasks import send_order_notification_task
from business_app.utils.exceptions import ValidationError
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

ADMIN_STATUS = "/api/v1/admin/orders/{}/status"
OPERATOR_PREPARING = "/api/v1/staff/orders/{}/preparing"
BULK_ACTIONS = "/api/v1/admin/bulk-actions"
# What an admin types into the Update Status modal's notes box.
ADMIN_NOTE = "Loaded onto the van"


@pytest.fixture
def notices(monkeypatch):
    """Every `send_order_notification_task.delay`, as `(args, kwargs)`, bound against the task.

    It is the only publish a forward move makes (`status_changed_<status>`), and it goes out
    only after the write commits.
    """
    return _task_spy(monkeypatch, send_order_notification_task, "delay")


def _order(db, customer, *, number, order_status, delivery_status, driver=None):
    """An order at `order_status` with its delivery at `delivery_status`.

    A `failed` row is what the staff failure branch leaves: it keeps its driver and carries
    a reason and one attempt. A `scheduled` row is a normal pool row with no driver.
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
    )
    db.session.add(order)
    db.session.flush()
    failed = delivery_status == DeliveryStatus.FAILED
    db.session.add(
        Delivery(
            order_id=order.id,
            delivery_person_id=driver.id if driver else None,
            status=delivery_status,
            failed_delivery_reason="customer_unavailable" if failed else None,
            delivery_attempts=1 if failed else 0,
            scheduled_date=datetime.now(timezone.utc),
            scheduled_time_slot="09:00-11:00",
        )
    )
    db.session.commit()
    return order.id


def _refusal(db, order_id):
    """The one rule's answer for this order.

    It proves the fixture really awaits a new date, and it gives the text the routes must
    carry, taken from the guard rather than copied into this test.
    """
    order = db.session.get(Order, order_id)
    assert OrderScheduleService.awaiting_new_date(order) is True
    with pytest.raises(ValidationError) as refused:
        OrderScheduleService.assert_can_advance(order)
    assert refused.value.error_code == "ORDER_AWAITING_NEW_DATE"
    return refused.value.message


def _snapshot(db, order_id):
    """Everything a refused forward move must leave exactly as it was.

    `updated_at` (TimestampMixin, `onupdate`) on both rows proves neither row was rewritten.
    """
    db.session.expire_all()
    order = db.session.get(Order, order_id)
    delivery = order.delivery
    return (
        order.status,
        order.updated_at,
        delivery.status,
        delivery.delivery_person_id,
        delivery.delivery_attempts,
        delivery.updated_at,
    )


def _history_count(order_id):
    return OrderStatusHistory.query.filter_by(order_id=order_id).count()


# --------------------------------------------------------------------------- #
# 1. Admin Update Status modal: PUT /admin/orders/<id>/status
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "order_status, requested",
    [
        # The driver failed from `assigned`, before pickup: the order is still confirmed.
        (OrderStatus.CONFIRMED, "preparing"),
        # An operator marked it preparing, then the driver failed.
        (OrderStatus.PREPARING, "out_for_delivery"),
    ],
)
def test_admin_update_status_refuses_a_forward_move_while_awaiting_a_new_date(
    app, client, db, admin_claim_headers, sample_user, delivery_driver, notices, order_status, requested
):
    order_id = _order(
        db,
        sample_user,
        number=f"ORD-F18-ADM-{requested}",
        order_status=order_status,
        delivery_status=DeliveryStatus.FAILED,
        driver=delivery_driver,
    )
    refusal = _refusal(db, order_id)
    before = _snapshot(db, order_id)

    response = client.put(
        ADMIN_STATUS.format(order_id),
        json={"status": requested, "notes": ADMIN_NOTE},
        headers=admin_claim_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert body.get("data") == {"error_code": "ORDER_AWAITING_NEW_DATE"}
    assert body["errors"] == [refusal]
    assert _snapshot(db, order_id) == before
    assert _history_count(order_id) == 0
    assert notices == []


@pytest.mark.parametrize(
    "order_status, requested",
    [
        (OrderStatus.CONFIRMED, OrderStatus.PREPARING),
        (OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY),
    ],
)
def test_admin_update_status_still_moves_an_order_whose_delivery_did_not_fail(
    app, client, db, admin_claim_headers, sample_user, notices, order_status, requested
):
    order_id = _order(
        db,
        sample_user,
        number=f"ORD-F18-ADM-OK-{requested.value}",
        order_status=order_status,
        delivery_status=DeliveryStatus.SCHEDULED,
    )

    response = client.put(
        ADMIN_STATUS.format(order_id),
        json={"status": requested.value, "notes": ADMIN_NOTE},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["order"]["status"] == requested.value
    db.session.expire_all()
    assert db.session.get(Order, order_id).status == requested
    history = OrderStatusHistory.query.filter_by(order_id=order_id).one()
    assert (history.old_status, history.new_status, history.notes) == (order_status, requested, ADMIN_NOTE)
    assert notices == [((order_id, f"status_changed_{requested.value}"), {})]


def test_admin_update_status_forwards_the_code_of_any_refusal(app, client, db, admin_claim_headers, sample_user, notices):
    """The route used to answer every refusal with its bare sentence. The modal (Task 17)
    and the reason rule (Task 8) branch on `data.error_code`, so it is forwarded for every
    ValidationError, not only for F18's."""
    order_id = _order(
        db,
        sample_user,
        number="ORD-F18-ADM-CODE",
        order_status=OrderStatus.PENDING,
        delivery_status=DeliveryStatus.SCHEDULED,
    )

    response = client.put(
        ADMIN_STATUS.format(order_id),
        json={"status": "preparing", "notes": ADMIN_NOTE},
        headers=admin_claim_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json().get("data") == {"error_code": "ORDER_STATUS_TRANSITION_INVALID"}
    db.session.expire_all()
    assert db.session.get(Order, order_id).status == OrderStatus.PENDING
    assert notices == []


# --------------------------------------------------------------------------- #
# 2. Operator "Mark preparing": PUT /staff/orders/<id>/preparing
# --------------------------------------------------------------------------- #


def test_operator_mark_preparing_refuses_an_order_awaiting_a_new_date(
    app, client, db, operator_auth_headers, sample_user, delivery_driver, notices
):
    order_id = _order(
        db,
        sample_user,
        number="ORD-F18-OP",
        order_status=OrderStatus.CONFIRMED,
        delivery_status=DeliveryStatus.FAILED,
        driver=delivery_driver,
    )
    refusal = _refusal(db, order_id)
    before = _snapshot(db, order_id)

    # The staff bot's `mark_order_preparing` sends no body.
    response = client.put(OPERATOR_PREPARING.format(order_id), headers=operator_auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert (body["error_code"], body["message"]) == ("ORDER_AWAITING_NEW_DATE", refusal)
    assert _snapshot(db, order_id) == before
    assert notices == []


def test_operator_mark_preparing_still_moves_a_confirmed_order_in_the_pool(
    app, client, db, operator_auth_headers, sample_user, notices
):
    order_id = _order(
        db,
        sample_user,
        number="ORD-F18-OP-OK",
        order_status=OrderStatus.CONFIRMED,
        delivery_status=DeliveryStatus.SCHEDULED,
    )

    response = client.put(OPERATOR_PREPARING.format(order_id), headers=operator_auth_headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["status"] == "preparing"
    db.session.expire_all()
    assert db.session.get(Order, order_id).status == OrderStatus.PREPARING
    assert notices == [((order_id, "status_changed_preparing"), {})]


# --------------------------------------------------------------------------- #
# 3. Bulk `process`: POST /admin/bulk-actions
# --------------------------------------------------------------------------- #


def test_bulk_process_counts_an_order_awaiting_a_new_date_as_failed(
    app, client, db, admin_claim_headers, sample_user, delivery_driver
):
    # The `process` arm writes `order.status` itself and never queues a notice, even for
    # the order it does move. The proof here is the per-item result and the untouched rows.
    awaiting_id = _order(
        db,
        sample_user,
        number="ORD-F18-BULK-WAIT",
        order_status=OrderStatus.CONFIRMED,
        delivery_status=DeliveryStatus.FAILED,
        driver=delivery_driver,
    )
    ready_id = _order(
        db,
        sample_user,
        number="ORD-F18-BULK-OK",
        order_status=OrderStatus.CONFIRMED,
        delivery_status=DeliveryStatus.SCHEDULED,
    )
    refusal = _refusal(db, awaiting_id)
    before = _snapshot(db, awaiting_id)

    # `adminService.performBulkAction` sends exactly these four keys; no `reason`.
    response = client.post(
        BULK_ACTIONS,
        json={
            "action": "process",
            "target_type": "order",
            "target_ids": [awaiting_id, ready_id],
            "parameters": {},
        },
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["results"] == {
        "success_count": 1,
        "failed_count": 1,
        "errors": [{"order_id": awaiting_id, "error": refusal}],
        "total_errors": 1,
    }
    assert _snapshot(db, awaiting_id) == before
    assert db.session.get(Order, ready_id).status == OrderStatus.PREPARING
