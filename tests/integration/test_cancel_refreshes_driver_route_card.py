"""Every order cancel refreshes, silently, the pinned route card of the driver who held the stop.

Owner, 2026-10-22: when an order is cancelled, the driver's route card is refreshed with no sound
and no alert. Every cancel path reaches one choke point, the delivery cascade in
`OrderService._cancel_delivery_for_cancelled_order`, which queues the refresh; an after-commit
listener (`business_app/utils/route_card_refresh_dispatch.py`) publishes it once the cancel has
committed, and never when it rolls back.

These drive the admin routes the way admin_ui calls them: the Orders page's Cancel
(`adminService.updateOrderStatus`: `{status, notes, reason}`, `notes` left undefined by the row
menu and so absent from the JSON) and `adminService.performBulkAction`'s four keys. The customer
route is in test_customer_cancel_order.py, whose `staff_told` spy is reused here: it records every
Celery publish and staff-bot post, each bound against its real signature.
"""

from datetime import UTC, datetime

import pytest

from business_app.models.delivery import Delivery
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.services.route_optimization_service import RouteOptimizationService
from business_app.services.sales.agent_order_approval_service import AgentOrderApprovalService
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod
from tests.integration.test_customer_cancel_order import (  # noqa: F401 - `customer_told`, `staff_told` are fixtures
    BULK_ACTIONS_URL,
    CANCEL_URL,
    DS,
    OS,
    PS,
    Case,
    _card_refresh,
    _headers,
    _seed,
    customer_told,
    staff_told,
)

ADMIN_STATUS_URL = "/api/v1/admin/orders/{order_id}/status"
REASON = "Customer changed their mind"

# Every delivery status a driver holds a stop in, with the order status it sits under. The
# admin's cancel is allowed from all of them, and is silent from all of them too: the owner's
# rule is "no sound or alert when an order is cancelled", picked up or not.
HELD = [
    (DS.ASSIGNED, OS.CONFIRMED),
    (DS.PICKED_UP, OS.OUT_FOR_DELIVERY),
    (DS.IN_TRANSIT, OS.OUT_FOR_DELIVERY),
    (DS.ARRIVED, OS.OUT_FOR_DELIVERY),
]


def _orders_page_cancel(client, headers, order_id):
    """The Orders page's Cancel, confirmed in CloseOrderDialog: `{status, notes, reason}` with the
    row menu's `notes` undefined, which JSON drops."""
    return client.put(
        ADMIN_STATUS_URL.format(order_id=order_id),
        json={"status": "cancelled", "reason": REASON},
        headers=headers,
    )


@pytest.mark.integration
@pytest.mark.parametrize("held", HELD, ids=[delivery_status.value for delivery_status, _ in HELD])
def test_an_admin_cancel_refreshes_the_holding_drivers_card_silently(
    client, db, sample_user, delivery_driver, admin_claim_headers, customer_told, staff_told, held
):
    delivery_status, order_status = held
    case = Case(f"admin-{delivery_status.value}", order_status, False, PS.PENDING, delivery_status, None)
    order, delivery = _seed(db, sample_user, delivery_driver, case)
    order_id, delivery_id = order.id, delivery.id

    response = _orders_page_cancel(client, admin_claim_headers, order_id)

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    assert db.session.get(Order, order_id).status is OrderStatus.CANCELLED
    assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.CANCELLED
    # One silent refresh for the driver who held it, and nothing else: no "order cancelled"
    # message, no re-solve (whose gate could sound on a head cancel).
    assert staff_told.everything() == [_card_refresh(delivery_driver.id, delivery_id)]


@pytest.mark.integration
def test_a_bulk_cancel_refreshes_once_per_held_order_and_never_for_a_driverless_one(
    client, db, sample_user, delivery_driver, admin_claim_headers, customer_told, staff_told
):
    """Each bulk item commits on its own, so two orders the same driver held give two
    publishes, one per stop. A delivery still in the pool belongs to no driver: nothing."""
    first, first_delivery = _seed(
        db, sample_user, delivery_driver, Case("bulk-held-1", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None)
    )
    second, second_delivery = _seed(
        db, sample_user, delivery_driver, Case("bulk-held-2", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None)
    )
    pooled, pooled_delivery = _seed(
        db, sample_user, delivery_driver, Case("bulk-pooled", OS.CONFIRMED, False, PS.PENDING, DS.PENDING, None)
    )
    assert pooled_delivery.delivery_person_id is None
    ids = [first.id, second.id, pooled.id]
    deliveries = [first_delivery.id, second_delivery.id, pooled_delivery.id]

    response = client.post(
        BULK_ACTIONS_URL,
        json={"action": "cancel", "target_type": "order", "target_ids": ids, "parameters": {}},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["results"] == {
        "success_count": 3,
        "failed_count": 0,
        "errors": [],
        "total_errors": 0,
    }
    db.session.expire_all()
    assert [db.session.get(Delivery, delivery_id).status for delivery_id in deliveries] == [
        DeliveryStatus.CANCELLED
    ] * 3
    assert staff_told.everything() == [
        _card_refresh(delivery_driver.id, deliveries[0]),
        _card_refresh(delivery_driver.id, deliveries[1]),
    ]


@pytest.mark.integration
def test_a_cancel_refreshes_the_card_even_when_the_driver_has_no_route_row_today(
    app, client, db, sample_user, delivery_driver, customer_told, staff_told
):
    """The card lists the driver's active deliveries whether or not a route was ever solved for
    today, so the refresh cannot wait on there being a route row to drop the stop from."""
    order, delivery = _seed(
        db, sample_user, delivery_driver, Case("no-route-row", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None)
    )
    assert RouteOptimizationService().current_route(delivery_driver.id) is None

    response = client.post(CANCEL_URL.format(order_id=order.id), headers=_headers(app, sample_user))

    assert response.status_code == 200, response.get_json()
    assert RouteOptimizationService().current_route(delivery_driver.id) is None
    assert staff_told.everything() == [_card_refresh(delivery_driver.id, delivery.id)]


@pytest.mark.integration
def test_a_cancel_that_rolls_back_after_queueing_the_refresh_publishes_nothing(
    app, client, db, sample_user, delivery_driver, customer_told, staff_told, monkeypatch
):
    """The refresh is queued by the delivery cascade and published only by the commit that
    persists the cancel. Here the step after the cascade, closing a pending agent-order hold,
    fails: the cancel rolls back, the queued refresh is discarded with it, and a later commit
    on the same session publishes nothing either.

    A cash order. A Click order's cancel writes a fiscalization audit row through a second
    session, and on the test's in-memory SQLite (one shared connection) that session's commit
    would persist the half-done cancel before the failure, which Postgres never does."""
    from business_app.utils import route_card_refresh_dispatch

    order, delivery = _seed(
        db, sample_user, delivery_driver, Case("rolled-back", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None)
    )
    order.payment_method = PaymentMethod.CASH
    Payment.query.filter_by(order_id=order.id).one().payment_method = PaymentMethod.CASH
    db.session.commit()
    order_id, delivery_id = order.id, delivery.id

    queued = []
    real_queue = route_card_refresh_dispatch.queue_route_card_refresh

    def queue_route_card_refresh(driver_id: int, delivery_id: int) -> None:
        queued.append((driver_id, delivery_id))
        real_queue(driver_id, delivery_id)

    def close_on_cancel(order, *, actor_id):
        raise RuntimeError("approval store unavailable")

    monkeypatch.setattr(route_card_refresh_dispatch, "queue_route_card_refresh", queue_route_card_refresh)
    monkeypatch.setattr(AgentOrderApprovalService, "close_on_cancel", staticmethod(close_on_cancel))

    response = client.post(CANCEL_URL.format(order_id=order_id), headers=_headers(app, sample_user))

    assert response.status_code == 500, response.get_json()
    assert queued == [(delivery_driver.id, delivery_id)], "the cascade ran, so the failure came after the queue"
    db.session.expire_all()
    assert db.session.get(Order, order_id).status is OrderStatus.CONFIRMED
    assert db.session.get(Delivery, delivery_id).status is DeliveryStatus.ASSIGNED
    db.session.commit()
    assert staff_told.everything() == []


@pytest.mark.integration
def test_an_admin_cancel_decides_on_the_delivery_as_the_database_has_it(
    client, db, sample_user, delivery_driver, admin_claim_headers, customer_told, staff_told
):
    """The admin's cancel locks only the Order before the cascade. A driver's accept that commits
    after this request first read the delivery (still in the pool, no driver) must not be missed:
    the cascade re-reads the delivery locked, so the driver who now holds it gets the silent card
    refresh, the stop leaves that driver's route, and their active count is resynced.

    The stale read is built as in test_customer_cancel_order.py: the accept is committed in the
    database, then the session's copy is put back to what the request had read first."""
    from sqlalchemy import update
    from sqlalchemy.orm.attributes import set_committed_value

    from business_app.models.delivery import DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
    from tests.integration.test_customer_cancel_order import _on_duty

    _on_duty(db, delivery_driver)
    order, delivery = _seed(
        db, sample_user, delivery_driver, Case("stale-accept", OS.CONFIRMED, False, PS.PENDING, DS.PENDING, None)
    )
    order_id, delivery_id = order.id, delivery.id
    assert order.delivery is delivery and delivery.delivery_person_id is None

    # The driver's accept, committed behind the request's back: the stop is theirs, on their route.
    db.session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id)
        .values(status=DeliveryStatus.ASSIGNED, delivery_person_id=delivery_driver.id)
    )
    DeliveryPerson.query.filter_by(user_id=delivery_driver.id).one().current_active_deliveries = 1
    route = DeliveryRoute(
        name="Today",
        delivery_person_id=delivery_driver.id,
        start_location_lat=41.3111,
        start_location_lng=69.2797,
        route_date=datetime.now(UTC),
        optimized_order=[delivery_id],
    )
    db.session.add(route)
    db.session.commit()
    set_committed_value(delivery, "status", DeliveryStatus.PENDING)
    set_committed_value(delivery, "delivery_person_id", None)
    assert db.session.get(Delivery, delivery_id).delivery_person_id is None, "the stale read is in place"
    staff_told.forget()

    response = _orders_page_cancel(client, admin_claim_headers, order_id)

    assert response.status_code == 200, response.get_json()
    assert staff_told.everything() == [_card_refresh(delivery_driver.id, delivery_id)]
    db.session.expire_all()
    landed = db.session.get(Delivery, delivery_id)
    assert (landed.status, landed.delivery_person_id) == (DeliveryStatus.CANCELLED, delivery_driver.id)
    last = (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).order_by(DeliveryStatusHistory.id.desc()).first()
    )
    assert (last.old_status, last.new_status) == (DeliveryStatus.ASSIGNED, DeliveryStatus.CANCELLED)
    assert RouteOptimizationService().current_route(delivery_driver.id).optimized_order == []
    assert DeliveryPerson.query.filter_by(user_id=delivery_driver.id).one().current_active_deliveries == 0


@pytest.mark.integration
def test_the_cascade_locks_the_delivery_only_when_it_will_write_it(
    client, db, sample_user, delivery_driver, admin_claim_headers, customer_told, staff_told, monkeypatch
):
    """The cascade decides on a fresh read first and takes the Delivery lock only for a row it is
    about to cancel. A FAILED delivery (its order awaiting a new date) is final: cancelling the
    order leaves it as it is, so no lock is taken on it, and a reschedule holding it
    (Delivery, then Order) never waits on this cancel (Order, then Delivery). SQLite takes no row
    locks, so the lock is observed at its one helper: the real `_lock_delivery`, recorded."""
    from business_app.services.order_schedule_service import OrderScheduleService

    real_lock = OrderScheduleService._lock_delivery
    locked = []

    def _lock_delivery(order_id: int):
        locked.append(order_id)
        return real_lock(order_id)

    monkeypatch.setattr(OrderScheduleService, "_lock_delivery", staticmethod(_lock_delivery))
    failed_order, failed_delivery = _seed(
        db, sample_user, delivery_driver, Case("lock-failed", OS.CONFIRMED, False, PS.PENDING, DS.FAILED, None)
    )
    held_order, held_delivery = _seed(
        db, sample_user, delivery_driver, Case("lock-held", OS.CONFIRMED, False, PS.PENDING, DS.ASSIGNED, None)
    )
    failed_order_id, failed_delivery_id = failed_order.id, failed_delivery.id
    held_order_id, held_delivery_id = held_order.id, held_delivery.id

    assert _orders_page_cancel(client, admin_claim_headers, failed_order_id).status_code == 200
    assert locked == []
    db.session.expire_all()
    assert db.session.get(Delivery, failed_delivery_id).status is DeliveryStatus.FAILED
    assert staff_told.everything() == []

    assert _orders_page_cancel(client, admin_claim_headers, held_order_id).status_code == 200
    assert locked == [held_order_id]
    db.session.expire_all()
    assert db.session.get(Delivery, held_delivery_id).status is DeliveryStatus.CANCELLED
    assert staff_told.everything() == [_card_refresh(delivery_driver.id, held_delivery_id)]
