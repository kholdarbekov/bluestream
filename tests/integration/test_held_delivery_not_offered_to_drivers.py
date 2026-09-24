"""A delivery waiting for its new day is not offered to drivers, and cannot be handed to one.

Spec: docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md (R3, R22,
section 3.3 "Status guards on the unguarded driver readers"). A `rescheduled`
delivery has no driver and is NOT claimable. The status-allowlist readers (pool
list, auto-assign, the 10-min re-enqueue) already skip it. This file drives the
ones that never looked at the status -- the diversion evaluator, the new-order
broadcast, the pool card's single-item lookup -- and the admin hand-assign paths
that ran `assign_driver(allow_in_progress=True)` and so skipped the claimable
guard.

Every read and write goes through the route the bot or the admin UI really calls.
Only outbound pushes are patched: the staff-bot webhook and Celery `.delay`.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.tasks.delivery_tasks import evaluate_pool_insertion_suggestions_task
from business_app.tasks.staff_tasks import notify_staff_new_order
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserStatus, UserType

pytestmark = pytest.mark.integration

POOL_DRIVER_TELEGRAM_ID = "700700700"


def _make_driver(db, *, phone, telegram_id, name):
    """A rostered, notifiable driver: on shift all day, so no clock is involved."""
    user = User(
        email=f"{telegram_id}@drivers.example.com",
        phone=phone,
        first_name=name,
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        status=UserStatus.ACTIVE,
        telegram_id=telegram_id,
        is_verified=True,
        password_hash=hash_password("DriverPassword123!"),
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name=f"{name} Driver",
            phone=phone,
            working_hours_start="00:00",
            working_hours_end="23:59",
            is_active=True,
            is_available=True,
            notifications_muted=False,
        )
    )
    db.session.commit()
    return user


@pytest.fixture
def pool_driver(db):
    """The driver whose phone shows the pool and the broadcast."""
    return _make_driver(db, phone="+998900002201", telegram_id=POOL_DRIVER_TELEGRAM_ID, name="Pool")


@pytest.fixture
def other_driver(db):
    return _make_driver(db, phone="+998900002202", telegram_id="700700701", name="Other")


def _delivery(db, customer, order_number, *, status, driver_id=None, created_by_staff_id=None):
    """`created_by_staff_id` only matters on Postgres, where an `admin`-source order
    without one violates `ck_orders_staff_creator_for_staff_source`."""
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address=f"Amir Temur {order_number}",
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=order_number,
        status=OrderStatus.CONFIRMED,
        total_amount=50000,
        order_source="admin",
        created_by_staff_id=created_by_staff_id,
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="anytime",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _route(db, driver_id):
    """Today's (empty) route row, so a wrongful assignment would show up spliced into it."""
    route = DeliveryRoute(
        name="today",
        delivery_person_id=driver_id,
        start_location_lat=41.30,
        start_location_lng=69.24,
        route_date=datetime.now(timezone.utc),
        optimized_order=[],
    )
    db.session.add(route)
    db.session.commit()
    return route


def _state(db, delivery_id):
    """What a refusal must leave exactly as it found: status, owner, history rows."""
    db.session.expire_all()
    delivery = db.session.get(Delivery, delivery_id)
    history = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count()
    return delivery.status, delivery.delivery_person_id, history


def _driver_headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id), additional_claims={"role": "delivery_driver"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# --------------------------------------------------------------------------- #
# The pool, as the driver's bot reads it
# --------------------------------------------------------------------------- #


def test_a_held_delivery_is_not_listed_in_the_pool(client, app, db, sample_user, pool_driver):
    held = _delivery(db, sample_user, "HLD-LIST-1", status=DeliveryStatus.RESCHEDULED)
    open_row = _delivery(db, sample_user, "HLD-LIST-2", status=DeliveryStatus.SCHEDULED)
    held_id, open_id = held.id, open_row.id

    resp = client.get("/api/v1/staff/delivery/pool", headers=_driver_headers(app, pool_driver))

    assert resp.status_code == 200, resp.get_data(as_text=True)
    items = resp.get_json()["data"]["items"]
    assert [item["delivery_id"] for item in items] == [open_id]
    assert held_id not in [item["delivery_id"] for item in items]
    assert items[0]["is_claimable"] is True


@pytest.mark.parametrize(
    "status,owned,claimable",
    [
        (DeliveryStatus.SCHEDULED, False, True),
        (DeliveryStatus.PENDING, False, True),
        (DeliveryStatus.RESCHEDULED, False, False),
        (DeliveryStatus.ASSIGNED, True, False),
    ],
    ids=["scheduled", "pending", "rescheduled", "taken"],
)
def test_the_pool_card_lookup_publishes_whether_the_delivery_can_still_be_claimed(
    client, app, db, sample_user, pool_driver, other_driver, status, owned, claimable
):
    """`GET /staff/delivery/pool?delivery_id=` is exactly what the bot's "View details"
    tap sends (orders_pool.view_order_details, include_assigned=true). The bot hides
    Accept and Mark-preparing on `is_claimable: false`, so the answer must be the
    backend's, and a driverless RESCHEDULED row must say False."""
    delivery = _delivery(
        db, sample_user, "HLD-CARD", status=status, driver_id=other_driver.id if owned else None
    )
    delivery_id = delivery.id

    resp = client.get(
        f"/api/v1/staff/delivery/pool?delivery_id={delivery_id}&include_assigned=true&page=1&per_page=1",
        headers=_driver_headers(app, pool_driver),
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    (item,) = resp.get_json()["data"]["items"]
    assert item["delivery_id"] == delivery_id
    assert item["delivery_status"] == status.value
    assert item["is_claimable"] is claimable


def test_accepting_a_held_delivery_is_refused_and_leaves_it_held(client, app, db, sample_user, pool_driver):
    held = _delivery(db, sample_user, "HLD-ACCEPT", status=DeliveryStatus.RESCHEDULED)
    held_id = held.id
    before = _state(db, held_id)

    resp = client.post(f"/api/v1/staff/delivery/accept/{held_id}", headers=_driver_headers(app, pool_driver))

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"
    assert _state(db, held_id) == before == (DeliveryStatus.RESCHEDULED, None, 0)


# --------------------------------------------------------------------------- #
# Hand-assignment from the admin side (R22)
# --------------------------------------------------------------------------- #


def test_dispatch_cannot_hand_a_held_delivery_to_a_driver(
    client, db, sample_user, admin_auth_headers, pool_driver
):
    """The Dispatch pool panel's Assign runs `assign_driver(allow_in_progress=True)`,
    which skipped the claimable guard. A held delivery is released by its date and
    never by hand. To release it early, reschedule it to today."""
    held = _delivery(db, sample_user, "HLD-DISPATCH", status=DeliveryStatus.RESCHEDULED)
    route = _route(db, pool_driver.id)
    held_id, route_id, driver_id = held.id, route.id, pool_driver.id
    before = _state(db, held_id)

    with patch("business_app.services.route_edit_service.notify_route_updated") as pushed, patch(
        "business_app.services.route_edit_service.notify_staff_order_reassigned"
    ) as reassigned:
        resp = client.post(
            f"/api/v1/admin/dispatch/stops/{held_id}/assign",
            json={"driver_id": driver_id},
            headers=admin_auth_headers,
        )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"
    assert _state(db, held_id) == before == (DeliveryStatus.RESCHEDULED, None, 0)
    assert db.session.get(DeliveryRoute, route_id).optimized_order == []
    pushed.assert_not_called()
    reassigned.delay.assert_not_called()


@pytest.mark.parametrize(
    "method,path,body_key",
    [
        ("post", "/api/v1/admin/staff/delivery/assign/{id}", "delivery_person_id"),
        ("put", "/api/v1/admin/staff/delivery/reassign/{id}", "new_delivery_person_id"),
    ],
    ids=["assign", "reassign"],
)
def test_the_delivery_page_cannot_hand_a_held_delivery_to_a_driver(
    client, db, sample_user, admin_claim_headers, pool_driver, method, path, body_key
):
    """The Delivery page's Assign modal posts to `assign` for a driverless row and to
    `reassign` when its (possibly stale) row still showed a driver. `reassign` is the
    `allow_in_progress=True` path. Both must refuse a held row and say why in
    `data.error_code`, not only in prose."""
    held = _delivery(db, sample_user, "HLD-PAGE", status=DeliveryStatus.RESCHEDULED)
    route = _route(db, pool_driver.id)
    held_id, route_id, driver_id = held.id, route.id, pool_driver.id
    before = _state(db, held_id)

    with patch("business_app.tasks.staff_tasks.notify_staff_order_assigned.delay") as assigned, patch(
        "business_app.tasks.staff_tasks.notify_staff_order_reassigned.delay"
    ) as reassigned:
        resp = getattr(client, method)(
            path.format(id=held_id), json={body_key: driver_id}, headers=admin_claim_headers
        )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"
    assert _state(db, held_id) == before == (DeliveryStatus.RESCHEDULED, None, 0)
    assert db.session.get(DeliveryRoute, route_id).optimized_order == []
    assigned.assert_not_called()
    reassigned.assert_not_called()


# --------------------------------------------------------------------------- #
# The broadcast pipeline, read when it runs, not when it was queued
# --------------------------------------------------------------------------- #


def test_an_evaluator_queued_before_the_reschedule_offers_nothing(app, db, sample_user, pool_driver):
    """The evaluator is queued post-commit when a delivery lands in the pool, and it is
    the single broadcast fan-out. If an admin moves the order to a later day before it
    runs, it must neither push a diversion offer nor queue the new-order broadcast."""
    held = _delivery(db, sample_user, "HLD-EVAL", status=DeliveryStatus.RESCHEDULED)

    with patch("business_app.utils.bot_webhook.notify_pool_insertion_suggestion") as offer, patch(
        "business_app.tasks.staff_tasks.notify_staff_new_order.delay"
    ) as broadcast:
        result = evaluate_pool_insertion_suggestions_task.run(held.id)

    assert result == {"suggested": False, "reason": "not_claimable"}
    offer.assert_not_called()
    broadcast.assert_not_called()


@pytest.mark.parametrize(
    "status,owned",
    [
        (DeliveryStatus.RESCHEDULED, False),
        (DeliveryStatus.ASSIGNED, True),
        (DeliveryStatus.CANCELLED, False),
    ],
    ids=["rescheduled", "taken", "cancelled"],
)
def test_a_broadcast_for_a_delivery_that_is_no_longer_claimable_sends_nothing(
    app, db, sample_user, pool_driver, other_driver, status, owned
):
    delivery = _delivery(
        db, sample_user, "HLD-BCAST-X", status=status, driver_id=other_driver.id if owned else None
    )
    order_id = delivery.order_id

    with patch("business_app.tasks.staff_tasks._send_staff_webhook", return_value=True) as webhook:
        notify_staff_new_order.run(order_id=order_id)

    webhook.assert_not_called()


def test_a_broadcast_for_an_order_with_no_delivery_row_sends_nothing(app, db, sample_user, pool_driver):
    """No Delivery row means the order was never released to drivers. There is
    nothing to accept, whatever the caller thought."""
    order = Order(user_id=sample_user.id, order_number="HLD-NOROW", status=OrderStatus.CONFIRMED,
                  total_amount=50000, order_source="admin")
    db.session.add(order)
    db.session.commit()

    with patch("business_app.tasks.staff_tasks._send_staff_webhook", return_value=True) as webhook:
        notify_staff_new_order.run(order_id=order.id)

    webhook.assert_not_called()


@pytest.mark.parametrize("status", [DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING], ids=lambda s: s.value)
def test_a_broadcast_for_a_claimable_delivery_still_reaches_the_drivers(app, db, sample_user, pool_driver, status):
    delivery = _delivery(db, sample_user, "HLD-BCAST-OK", status=status)
    order_id, delivery_id = delivery.order_id, delivery.id

    with patch("business_app.tasks.staff_tasks._send_staff_webhook", return_value=True) as webhook:
        notify_staff_new_order.run(order_id=order_id)

    webhook.assert_called_once()
    endpoint, payload = webhook.call_args.args
    assert endpoint == "/internal/new-order"
    assert payload["order_id"] == order_id
    assert payload["delivery_person_telegram_ids"] == [POOL_DRIVER_TELEGRAM_ID]
    assert payload["order_info"]["delivery_id"] == delivery_id
    assert payload["order_info"]["order_number"] == "HLD-BCAST-OK"


# --------------------------------------------------------------------------- #
# The same refusal where the driverless CHECK is real
# --------------------------------------------------------------------------- #


def test_on_postgres_a_dispatch_assign_of_a_held_delivery_is_a_400_not_a_check_violation(pg_app, pg_db):
    """SQLite has no `ck_deliveries_no_driver_for_pool_status`. On Postgres (Task 1
    widened it to `rescheduled`), an unguarded `assign_driver` writes a driver onto
    the held row and the flush dies on that CHECK. The dispatcher then gets a 500
    instead of the reason."""
    admin = User(email="pg-admin@example.com", phone="+998909300001", password_hash="x",
                 first_name="Pg", last_name="Admin", user_type=UserType.STAFF, role=UserRole.ADMIN,
                 status=UserStatus.ACTIVE, is_verified=True)
    customer = User(email="pg-cust@example.com", phone="+998909300002", password_hash="x",
                    first_name="Pg", last_name="Customer", user_type=UserType.INDIVIDUAL,
                    role=UserRole.CUSTOMER, is_verified=True)
    pg_db.session.add_all([admin, customer])
    pg_db.session.commit()
    driver = _make_driver(pg_db, phone="+998909300003", telegram_id="700700799", name="Pg")
    held = _delivery(
        pg_db, customer, "HLD-PG-1", status=DeliveryStatus.RESCHEDULED, created_by_staff_id=admin.id
    )
    held_id, driver_id = held.id, driver.id
    token = create_access_token(identity=str(admin.id), additional_claims={"role": "admin"})

    with patch("business_app.services.route_edit_service.notify_route_updated"), patch(
        "business_app.services.route_edit_service.notify_staff_order_reassigned"
    ):
        resp = pg_app.test_client().post(
            f"/api/v1/admin/dispatch/stops/{held_id}/assign",
            json={"driver_id": driver_id},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["error_code"] == "STAFF_DELIVERY_NOT_CLAIMABLE"
    assert _state(pg_db, held_id) == (DeliveryStatus.RESCHEDULED, None, 0)
