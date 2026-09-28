"""An admin cancel or return names its reason, and only admins ever read it (F6, F19).

Spec: docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md §3.4.
Each case is driven through the route the admin UI calls, with the body it sends:
- `PUT /admin/orders/<id>/status`: the Orders page's row-menu Cancel sends
  `{status, reason}` and its Update Status modal `{status, notes, reason}`;
- `PUT /admin/deliveries/<id>`: the Delivery page's Update modal sends
  `{status, notes}`, plus `reason` only when it moves a row to returned.

The reason lands on the history row's `reason` column, never in `notes`, because
the customer bot prints every history note on its Track screen. The customer's
order reads carry no `reason` at all, and `GET /admin/orders/<id>` publishes the
latest one as `closing_reason`.

Celery publishes are already no-ops (tests/conftest.py). The customer status
notice is spied, with its real signature, only where a refusal must queue none.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.payment import Payment
from business_app.services.order_service import ADMIN_REASON_MAX_LENGTH, ADMIN_REASON_REQUIRED_STATUSES
from business_app.tasks.notification_tasks import send_order_notification_task
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, PaymentStatus
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

ORDERS = "/api/v1/admin/orders"
DELIVERIES = "/api/v1/admin/deliveries"
CUSTOMER_ORDERS = "/api/v1/orders"
STATUSES = "/api/v1/orders/statuses"
# Staff text on the delivery row. The Delivery page sends it back with every save.
GATE_CODE = "Gate code 42"

# The bodies the updated admin UI never sends but a pre-deploy tab or a hand-made
# request can: no reason, a blank one, one that is not text, and one the history
# column cannot hold. Each is a coded 400, never a 500 or Python's own error text.
UNUSABLE_REASONS = [
    pytest.param({}, "ADMIN_REASON_REQUIRED", id="missing"),
    pytest.param({"reason": "   "}, "ADMIN_REASON_REQUIRED", id="blank"),
    pytest.param({"reason": 42}, "ADMIN_REASON_REQUIRED", id="not-text"),
    pytest.param({"reason": "x" * (ADMIN_REASON_MAX_LENGTH + 1)}, "ADMIN_REASON_TOO_LONG", id="too-long"),
]


def _order(db, customer, address, number, *, order_status, delivery_status=None, driver_id=None,
           delivery_notes=None):
    """A cash order as the admin screens see it: a PENDING COD payment and, when
    `delivery_status` is given, its delivery row in that status."""
    order = Order(
        user_id=customer.id,
        order_number=number,
        status=order_status,
        subtotal=Decimal("50000.00"),
        delivery_fee=Decimal("0.00"),
        total_amount=Decimal("50000.00"),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CASH,
            amount=order.total_amount,
            currency="UZS",
            status=PaymentStatus.PENDING,
            payment_id=f"admin-reason-{order.id}",
        )
    )
    if delivery_status is not None:
        db.session.add(
            Delivery(
                order_id=order.id,
                status=delivery_status,
                delivery_person_id=driver_id,
                delivery_notes=delivery_notes,
                scheduled_date=datetime.now(timezone.utc),
                scheduled_time_slot="anytime",
            )
        )
    db.session.commit()
    return order.id


def _awaiting_new_date(db, customer, address, driver, number):
    """The state behind orders 803 and 330: the driver marked the delivery failed
    and kept it, and the order is still out for delivery."""
    return _order(
        db, customer, address, number,
        order_status=OrderStatus.OUT_FOR_DELIVERY, delivery_status=DeliveryStatus.FAILED, driver_id=driver.id,
    )


def _closing_reason(client, headers, order_id):
    response = client.get(f"{ORDERS}/{order_id}", headers=headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["data"]["order"]["closing_reason"]


def _assert_customer_never_sees(client, auth_headers, order_id, reason):
    """The customer's two order reads, `GET /orders/<id>` and `/orders/<id>/track`,
    carry the reason nowhere, and no timeline entry has a key to carry it in."""
    for path in (f"{CUSTOMER_ORDERS}/{order_id}", f"{CUSTOMER_ORDERS}/{order_id}/track"):
        response = client.get(path, headers=auth_headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        assert reason not in response.get_data(as_text=True)
        assert [entry for entry in response.get_json()["data"]["timeline"] if "reason" in entry] == []


@pytest.mark.parametrize("target", ["cancelled", "returned"])
@pytest.mark.parametrize("body, error_code", UNUSABLE_REASONS)
def test_an_orders_page_cancel_or_return_without_a_usable_reason_changes_nothing(
    client, db, monkeypatch, admin_claim_headers, sample_user, user_address, delivery_driver,
    target, body, error_code,
):
    """F6. Orders 803 and 330 were cancelled by an admin 1-2 hours after their failed delivery was
    re-dispatched, and nothing recorded why. The route now refuses a cancel or a return without a
    reason, before the cascade runs. The order, its failed delivery and its history stay as they
    were, and the customer is sent nothing. The limit is read from the two history columns, both
    String(100). A longer reason is refused, never cut."""
    assert ADMIN_REASON_MAX_LENGTH == 100
    order_id = _awaiting_new_date(db, sample_user, user_address, delivery_driver, "ORD-AR-REFUSED")
    notices = _task_spy(monkeypatch, send_order_notification_task, "delay")

    response = client.put(
        f"{ORDERS}/{order_id}/status",
        json={"status": target, "notes": "Sorry for the trouble", **body},
        headers=admin_claim_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["data"]["error_code"] == error_code
    db.session.expire_all()
    assert db.session.get(Order, order_id).status == OrderStatus.OUT_FOR_DELIVERY
    delivery = Delivery.query.filter_by(order_id=order_id).one()
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.FAILED, delivery_driver.id)
    assert OrderStatusHistory.query.filter_by(order_id=order_id).count() == 0
    assert notices == []


@pytest.mark.parametrize("target", ["cancelled", "returned"])
def test_an_orders_page_cancel_or_return_keeps_the_reason_off_everything_the_customer_reads(
    client, db, admin_claim_headers, auth_headers, admin_user, sample_user, user_address, delivery_driver, target,
):
    """F6 and F19. The Update Status modal sends both boxes.
    - The note to the customer keeps its routing onto the history row's `notes`, which the
      customer's Track screen prints.
    - The internal reason is stripped and stored whole on `reason`. Neither customer read carries
      it, and admins see it as the order's closing reason."""
    reason = f"Customer was abusive on the phone ({target})"
    order_id = _awaiting_new_date(db, sample_user, user_address, delivery_driver, "ORD-AR-KEPT")

    response = client.put(
        f"{ORDERS}/{order_id}/status",
        json={"status": target, "notes": "Sorry for the trouble", "reason": f"  {reason}  "},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["order"]["status"] == target
    db.session.expire_all()
    (row,) = OrderStatusHistory.query.filter_by(order_id=order_id).all()
    assert (row.old_status, row.new_status, row.changed_by, row.notes, row.reason) == (
        OrderStatus.OUT_FOR_DELIVERY, OrderStatus(target), admin_user.id, "Sorry for the trouble", reason,
    )
    _assert_customer_never_sees(client, auth_headers, order_id, reason)
    assert _closing_reason(client, admin_claim_headers, order_id) == {
        "status": target,
        "reason": reason,
        "changed_by_name": "Admin User",
        "changed_at": row.changed_at.isoformat(),
    }


def test_the_orders_page_reads_which_statuses_need_a_reason_from_the_backend(client, db):
    """F6. `GET /orders/statuses`, the read the Orders page already makes for its transitions,
    publishes the statuses whose PUT must carry a reason. The page asks for the reason, behind a
    confirm, for exactly these, and keeps no copy of the rule."""
    response = client.get(STATUSES)

    assert response.status_code == 200, response.get_data(as_text=True)
    published = response.get_json()["data"]["reason_required_statuses"]
    assert published == sorted(status.value for status in ADMIN_REASON_REQUIRED_STATUSES)
    assert published == ["cancelled", "returned"]


@pytest.mark.parametrize("target", [status.value for status in OrderStatus])
def test_the_status_route_asks_a_reason_for_exactly_the_published_statuses(
    client, db, admin_claim_headers, sample_user, user_address, delivery_driver, target,
):
    """The published list is a promise the write path keeps. A PUT with no reason is refused with
    ADMIN_REASON_REQUIRED for every published status and for no other. Another status may still be
    refused, by its transition, but never for want of a reason."""
    published = client.get(STATUSES).get_json()["data"]["reason_required_statuses"]
    order_id = _awaiting_new_date(db, sample_user, user_address, delivery_driver, f"ORD-AR-{target}")

    response = client.put(
        f"{ORDERS}/{order_id}/status", json={"status": target, "notes": ""}, headers=admin_claim_headers,
    )

    error_code = ((response.get_json() or {}).get("data") or {}).get("error_code")
    assert (error_code == "ADMIN_REASON_REQUIRED") == (target in published), response.get_data(as_text=True)


@pytest.mark.parametrize("body, error_code", UNUSABLE_REASONS)
def test_a_delivery_page_return_without_a_usable_reason_changes_nothing(
    client, db, admin_claim_headers, sample_user, user_address, delivery_driver, body, error_code,
):
    """F6. Before pickup, Returned is the only negative option the Delivery page offers. It used
    to need no confirm and no reason. A move to returned is now refused before its first write:
    the row keeps its driver, the order stays confirmed, and neither side gets a history row."""
    order_id = _order(
        db, sample_user, user_address, "ORD-AR-DP-REFUSED",
        order_status=OrderStatus.CONFIRMED, delivery_status=DeliveryStatus.ASSIGNED,
        driver_id=delivery_driver.id, delivery_notes=GATE_CODE,
    )
    delivery_id = Delivery.query.filter_by(order_id=order_id).one().id

    response = client.put(
        f"{DELIVERIES}/{delivery_id}",
        json={"status": "returned", "notes": GATE_CODE, **body},
        headers=admin_claim_headers,
    )

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["data"]["error_code"] == error_code
    db.session.expire_all()
    delivery = db.session.get(Delivery, delivery_id)
    assert (delivery.status, delivery.delivery_person_id) == (DeliveryStatus.ASSIGNED, delivery_driver.id)
    assert db.session.get(Order, order_id).status == OrderStatus.CONFIRMED
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == 0
    assert OrderStatusHistory.query.filter_by(order_id=order_id).count() == 0


def test_a_delivery_page_return_writes_one_order_history_row_with_the_reason_and_no_note(
    client, db, admin_claim_headers, auth_headers, admin_user, sample_user, user_address, delivery_driver,
):
    """F6. This path writes `order.status = RETURNED` directly and used to leave the order's history
    blank. It now records exactly one row: the order's own pre-status, the admin, the reason, and NO
    note. The delivery's notes are staff text (a gate code here), and the customer bot prints every
    history note, so they are never copied across. The customer's Track timeline gains a `returned`
    entry, the same as an Orders-page Returned produces."""
    reason = "Warehouse ran out of 19L bottles"
    order_id = _order(
        db, sample_user, user_address, "ORD-AR-DP-KEPT",
        order_status=OrderStatus.CONFIRMED, delivery_status=DeliveryStatus.ASSIGNED,
        driver_id=delivery_driver.id, delivery_notes=GATE_CODE,
    )
    delivery_id = Delivery.query.filter_by(order_id=order_id).one().id

    response = client.put(
        f"{DELIVERIES}/{delivery_id}",
        json={"status": "returned", "notes": GATE_CODE, "reason": reason},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    (order_row,) = OrderStatusHistory.query.filter_by(order_id=order_id).all()
    assert (order_row.old_status, order_row.new_status, order_row.changed_by, order_row.notes, order_row.reason) == (
        OrderStatus.CONFIRMED, OrderStatus.RETURNED, admin_user.id, None, reason,
    )
    (delivery_row,) = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).all()
    assert (delivery_row.old_status, delivery_row.new_status, delivery_row.reason) == (
        DeliveryStatus.ASSIGNED, DeliveryStatus.RETURNED, reason,
    )

    track = client.get(f"{CUSTOMER_ORDERS}/{order_id}/track", headers=auth_headers)
    assert track.status_code == 200, track.get_data(as_text=True)
    last = track.get_json()["data"]["timeline"][-1]
    assert (last["status"], last["is_current"], last["notes"]) == ("returned", True, None)
    assert GATE_CODE not in track.get_data(as_text=True)
    _assert_customer_never_sees(client, auth_headers, order_id, reason)
    assert _closing_reason(client, admin_claim_headers, order_id) == {
        "status": "returned",
        "reason": reason,
        "changed_by_name": "Admin User",
        "changed_at": order_row.changed_at.isoformat(),
    }


def test_a_delivery_page_return_of_an_order_already_returned_adds_no_second_order_row(
    client, db, admin_claim_headers, auth_headers, sample_user, user_address, delivery_driver,
):
    """The usual clean-up. An admin returns an order that is out for delivery from the Orders page,
    which leaves its delivery where it is (spec §11), then moves that delivery to returned on the
    Delivery page. The order was already returned, so its history keeps its one Returned row: a
    second would put Returned twice on the customer's Track screen and replace the closing reason
    with this clean-up's. The delivery's own history still records this move and its reason."""
    order_id = _order(
        db, sample_user, user_address, "ORD-AR-TWICE",
        order_status=OrderStatus.OUT_FOR_DELIVERY, delivery_status=DeliveryStatus.IN_TRANSIT,
        driver_id=delivery_driver.id,
    )
    delivery_id = Delivery.query.filter_by(order_id=order_id).one().id
    returned = client.put(
        f"{ORDERS}/{order_id}/status",
        json={"status": "returned", "reason": "Customer moved away"},
        headers=admin_claim_headers,
    )
    assert returned.status_code == 200, returned.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).status == DeliveryStatus.IN_TRANSIT
    closing = _closing_reason(client, admin_claim_headers, order_id)
    assert closing["reason"] == "Customer moved away"

    response = client.put(
        f"{DELIVERIES}/{delivery_id}",
        json={"status": "returned", "notes": GATE_CODE, "reason": "Bottles back at the warehouse"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    (order_row,) = OrderStatusHistory.query.filter_by(order_id=order_id, new_status=OrderStatus.RETURNED).all()
    assert order_row.reason == "Customer moved away"
    assert _closing_reason(client, admin_claim_headers, order_id) == closing
    delivery_row = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).one()
    assert (delivery_row.old_status, delivery_row.new_status, delivery_row.reason) == (
        DeliveryStatus.IN_TRANSIT, DeliveryStatus.RETURNED, "Bottles back at the warehouse",
    )
    track = client.get(f"{CUSTOMER_ORDERS}/{order_id}/track", headers=auth_headers)
    assert track.status_code == 200, track.get_data(as_text=True)
    assert [entry["status"] for entry in track.get_json()["data"]["timeline"]].count("returned") == 1


def test_a_notes_only_save_on_a_returned_delivery_needs_no_reason(
    client, db, admin_claim_headers, sample_user, user_address,
):
    """Review Focus 5. The Delivery page sends the row's current status with every save, so "Edit
    notes" on a returned row arrives as `{status: 'returned', notes}`. That is not a move to
    returned: it needs no reason, saves the note, and writes no history row on either side."""
    order_id = _order(
        db, sample_user, user_address, "ORD-AR-DP-NOTES",
        order_status=OrderStatus.RETURNED, delivery_status=DeliveryStatus.RETURNED,
    )
    delivery_id = Delivery.query.filter_by(order_id=order_id).one().id

    response = client.put(
        f"{DELIVERIES}/{delivery_id}",
        json={"status": "returned", "notes": "Customer called back"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["delivery"]["notes"] == "Customer called back"
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).delivery_notes == "Customer called back"
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == 0
    assert OrderStatusHistory.query.filter_by(order_id=order_id).count() == 0


def test_the_closing_reason_is_the_latest_reasoned_cancel_or_return(
    client, db, admin_claim_headers, sample_user, user_address, delivery_driver,
):
    """F19. The admin order detail shows the latest cancel or return that carries a reason, with who
    did it and when. The steps, all offered by the Update Status modal:
    - Reopening a returned order is not a close, so the return stays on record.
    - Cancelling the reopened order then replaces it."""
    order_id = _awaiting_new_date(db, sample_user, user_address, delivery_driver, "ORD-AR-LATEST")
    assert _closing_reason(client, admin_claim_headers, order_id) is None

    returned = client.put(
        f"{ORDERS}/{order_id}/status",
        json={"status": "returned", "reason": "Refused at the door"},
        headers=admin_claim_headers,
    )
    assert returned.status_code == 200, returned.get_data(as_text=True)
    reopened = client.put(f"{ORDERS}/{order_id}/status", json={"status": "pending"}, headers=admin_claim_headers)
    assert reopened.status_code == 200, reopened.get_data(as_text=True)
    assert _closing_reason(client, admin_claim_headers, order_id)["reason"] == "Refused at the door"

    cancelled = client.put(
        f"{ORDERS}/{order_id}/status",
        json={"status": "cancelled", "reason": "Duplicate of another order"},
        headers=admin_claim_headers,
    )
    assert cancelled.status_code == 200, cancelled.get_data(as_text=True)

    db.session.expire_all()
    latest = OrderStatusHistory.query.filter_by(order_id=order_id, new_status=OrderStatus.CANCELLED).one()
    assert _closing_reason(client, admin_claim_headers, order_id) == {
        "status": "cancelled",
        "reason": "Duplicate of another order",
        "changed_by_name": "Admin User",
        "changed_at": latest.changed_at.isoformat(),
    }


def test_a_customer_cancel_leaves_no_closing_reason(client, db, admin_claim_headers, auth_headers, sample_user,
                                                     user_address):
    """The customer's own cancel sends a `reason` too, but it is the customer's note. `cancel_order`'s
    `reason` keeps its notes meaning and is never routed to the history `reason` column. So nothing
    on the admin detail reads as a staff decision."""
    order_id = _order(db, sample_user, user_address, "ORD-AR-CUSTOMER", order_status=OrderStatus.PENDING)

    response = client.post(
        f"{CUSTOMER_ORDERS}/{order_id}/cancel", json={"reason": "Changed my mind"}, headers=auth_headers
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    (row,) = OrderStatusHistory.query.filter_by(order_id=order_id).all()
    assert (row.new_status, row.notes, row.reason) == (OrderStatus.CANCELLED, "Changed my mind", None)
    assert _closing_reason(client, admin_claim_headers, order_id) is None
