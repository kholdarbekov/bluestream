"""A future-dated order must be invisible to drivers until its release morning.

Driven through HTTP where a human would drive it, because the bug this feature
prevents is a *visibility* bug: a service-level assertion that a Delivery row is
absent would still pass if the pool endpoint offered the order anyway.

Fixtures/helpers here (``driver_on_shift``, ``_confirm_scheduled_order``) are
the shared foundation Tasks 7 and 9 append to — keep their shape stable.
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from flask_jwt_extended import create_access_token

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.utils.password_security import hash_password
from shared.business_config import MAX_SCHEDULE_HORIZON_DAYS
from shared.enums import OrderStatus, UserRole

TZ = ZoneInfo("Asia/Tashkent")


@pytest.fixture
def driver_on_shift(app, db):
    """An active, rostered driver so the release gate resolves from a real
    roster row and the driver can authenticate against the pool endpoint.

    Depends on the ``db`` fixture (not just ``app``) so
    ``db.create_all()`` has actually run before this fixture writes rows --
    fixtures that only depend on ``app`` never trigger table creation.
    """
    with app.app_context():
        user = User(email="rel-driver@example.com", phone="+998900001111", first_name="Rel",
                    last_name="Driver", role=UserRole.DELIVERY_DRIVER, status="active",
                    password_hash=hash_password("TestPassword123!"))
        db.session.add(user)
        db.session.flush()
        dp = DeliveryPerson(user_id=user.id, full_name="Rel Driver", phone=user.phone,
                            working_hours_start="08:00", working_hours_end="20:00",
                            is_active=True, is_available=True)
        db.session.add(dp)
        db.session.commit()
        return user.id


def _confirm_scheduled_order(app, user_id, target_date, window_start=None, window_end=None):
    """A real (not mocked) `DeliveryService.create_delivery()` call requires a
    delivery address -- unlike the order_schedule_service unit tests, which
    patch `create_delivery` out entirely, this file drives the real release
    path end to end, so the order needs a real, in-range address."""
    with app.app_context():
        address = UserAddress(
            user_id=user_id,
            title="Home",
            full_address="Amir Temur 1",
            street_address="Amir Temur 1",
            city="Tashkent",
            latitude=41.31,
            longitude=69.28,
            is_default=True,
        )
        db.session.add(address)
        db.session.flush()

        order = Order(user_id=user_id, status=OrderStatus.CONFIRMED, total_amount=50000,
                      delivery_date=target_date, delivery_window_start=window_start,
                      delivery_window_end=window_end, order_source="admin",
                      delivery_address_id=address.id)
        db.session.add(order)
        db.session.commit()
        return order.id


@pytest.mark.parametrize(
    "window_start,window_end",
    [
        (None, None),               # anytime
        (time(12, 0), time(18, 0)), # between
        (None, time(10, 0)),        # until
        (time(19, 0), None),        # after
    ],
)
def test_scheduled_order_is_invisible_then_released(app, client, driver_on_shift, window_start, window_end):
    tomorrow = date.today() + timedelta(days=1)
    order_id = _confirm_scheduled_order(app, driver_on_shift, tomorrow, window_start, window_end)

    # --- before release: no delivery row, absent from the pool, nothing broadcast
    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_id).first() is None

        token = create_access_token(identity=str(driver_on_shift),
                                    additional_claims={"role": "delivery_driver"})
    resp = client.get("/api/v1/staff/delivery/pool", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert order_id not in [i["order_id"] for i in resp.get_json()["data"]["items"]]

    # --- run the sweep before the morning: still nothing
    from business_app.tasks.order_tasks import release_due_scheduled_orders

    before = datetime.combine(tomorrow, time(6, 0), tzinfo=TZ).astimezone(timezone.utc)
    with app.app_context(), \
         patch("business_app.services.order_schedule_service.get_utc_now", return_value=before), \
         patch("business_app.tasks.delivery_tasks.evaluate_pool_insertion_suggestions_task.delay") as fanout:
        release_due_scheduled_orders()
        assert Delivery.query.filter_by(order_id=order_id).first() is None
        fanout.assert_not_called()

    # --- run it after the morning: delivery exists and the fan-out fired
    after = datetime.combine(tomorrow, time(8, 30), tzinfo=TZ).astimezone(timezone.utc)
    with app.app_context(), \
         patch("business_app.services.order_schedule_service.get_utc_now", return_value=after):
        result = release_due_scheduled_orders()
        assert result["released"] == 1
        assert Delivery.query.filter_by(order_id=order_id).first() is not None

    resp = client.get("/api/v1/staff/delivery/pool", headers={"Authorization": f"Bearer {token}"})
    assert order_id in [i["order_id"] for i in resp.get_json()["data"]["items"]]


def test_sweep_catches_up_on_a_missed_day(app, driver_on_shift):
    """`delivery_date <= today`, not `== today`: a worker outage must not strand
    yesterday's orders forever."""
    from business_app.tasks.order_tasks import release_due_scheduled_orders

    yesterday = date.today() - timedelta(days=1)
    order_id = _confirm_scheduled_order(app, driver_on_shift, yesterday)
    with app.app_context():
        assert release_due_scheduled_orders()["released"] == 1
        assert Delivery.query.filter_by(order_id=order_id).first() is not None


def test_sweep_skips_a_still_pending_order(app, driver_on_shift):
    from business_app.tasks.order_tasks import release_due_scheduled_orders

    with app.app_context():
        order = Order(user_id=driver_on_shift, status=OrderStatus.PENDING, total_amount=50000,
                      delivery_date=date.today(), order_source="web")
        db.session.add(order)
        db.session.commit()
        order_id = order.id
        assert release_due_scheduled_orders()["released"] == 0
        assert Delivery.query.filter_by(order_id=order_id).first() is None


def test_sweep_ignores_an_order_cancelled_while_awaiting_release(app, driver_on_shift):
    """No delivery row exists, so cancelling has no delivery cascade to run and
    the sweep's status filter must drop the order on its delivery day."""
    from business_app.tasks.order_tasks import release_due_scheduled_orders

    order_id = _confirm_scheduled_order(app, driver_on_shift, date.today())
    with app.app_context():
        order = Order.query.get(order_id)
        order.status = OrderStatus.CANCELLED
        db.session.commit()

        assert release_due_scheduled_orders()["released"] == 0
        assert Delivery.query.filter_by(order_id=order_id).first() is None


def _midday_on(target_date):
    """Noon Tashkent on `target_date`, in UTC.

    Every test that seeds `delivery_date=<today>` and expects the sweep to
    RELEASE it must freeze the clock here. Against the real clock those tests
    pass only once wall-clock time is past `driver_on_shift`'s hardcoded 08:00
    shift start, so they went red every day between 05:00 and 08:00 Tashkent
    (the test container runs UTC; adding `TZ` to it widens the window to
    00:00-08:00). This is the fix, not moving the fixture's shift earlier --
    a shift time chosen to dodge a clock is a fixture that no longer describes
    a real roster.

    Both halves come from `datetime.now(TZ)` rather than `date.today()` so the
    seeded date, the sweep's `local_today`, and the release instant all agree
    no matter what TZ the container happens to carry.
    """
    return datetime.combine(target_date, time(12, 0), tzinfo=TZ).astimezone(timezone.utc)


def test_sweep_is_idempotent(app, driver_on_shift):
    from business_app.tasks.order_tasks import release_due_scheduled_orders

    today = _local_today()
    order_id = _confirm_scheduled_order(app, driver_on_shift, today)
    with app.app_context(), patch(
        "business_app.services.order_schedule_service.get_utc_now", return_value=_midday_on(today)
    ):
        assert release_due_scheduled_orders()["released"] == 1
        assert release_due_scheduled_orders()["released"] == 0
        assert Delivery.query.filter_by(order_id=order_id).count() == 1


def test_batch_isolation_one_failure_does_not_abort_others_or_undo_earlier_successes(app, driver_on_shift):
    """Three due orders, the middle one fails: the per-order `try/except` +
    `db.session.rollback()` in `release_due_scheduled_orders` is what
    guarantees a bad order neither aborts the rest of the batch nor discards
    a `Delivery` already committed for an earlier order in the same loop.
    Nothing else in this file pins that -- a later regression (hoisting the
    commit to batch level, or a savepoint that rolls back too broadly) would
    ship green without this test.

    The failure is injected in the real path: `DeliveryService.create_delivery`
    is autospec-patched to raise only for the middle order's id and otherwise
    delegate to the real method, so the other two orders go through
    `create_delivery` for real (address validation, distance calc, the actual
    `Delivery` row) rather than being faked out.
    """
    from business_app.services.delivery_service import DeliveryService
    from business_app.tasks.order_tasks import release_due_scheduled_orders

    today = _local_today()
    order_ids = [_confirm_scheduled_order(app, driver_on_shift, today) for _ in range(3)]
    fail_id = order_ids[1]
    real_create_delivery = DeliveryService.create_delivery

    def _flaky_create_delivery(self, order_id, *args, **kwargs):
        if order_id == fail_id:
            raise RuntimeError("simulated create_delivery failure")
        return real_create_delivery(self, order_id, *args, **kwargs)

    # Frozen for the same reason as test_sweep_is_idempotent: see _midday_on.
    with app.app_context(), \
         patch("business_app.services.order_schedule_service.get_utc_now", return_value=_midday_on(today)), \
         patch.object(DeliveryService, "create_delivery", autospec=True, side_effect=_flaky_create_delivery):
        result = release_due_scheduled_orders()

    assert result["released"] == 2
    assert result["failed"] == 1

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order_ids[0]).first() is not None
        assert Delivery.query.filter_by(order_id=fail_id).first() is None
        assert Delivery.query.filter_by(order_id=order_ids[2]).first() is not None


# ---------------------------------------------------------------------------
# Accepting a schedule: the two create paths a human can actually drive.
#
# Fixture note: the plan's brief named a `sample_address` fixture that does not
# exist — the real one in tests/conftest.py is `user_address` (owned by
# `sample_user`, inside the Tashkent polygon). Quantities are 2, not 1: one
# 15 000 UZS bottle sits under the 20 000 MIN_ORDER_AMOUNT floor and
# `create_order` would reject the basket before any schedule was read.
# ---------------------------------------------------------------------------


def _local_today():
    """The endpoint validates against TASHKENT-local today, so the test asks the
    same clock explicitly rather than relying on the container's TZ env
    happening to be Asia/Tashkent."""
    return datetime.now(TZ).date()


def _admin_headers(app, admin_user):
    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}"}


def test_admin_can_create_a_scheduled_order_with_a_deadline(app, client, admin_user, sample_product, user_address):
    tomorrow = (_local_today() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user),
        json={
            "user_id": user_address.user_id,
            "delivery_address_id": user_address.id,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "payment_method": "cash",
            "delivery_date": tomorrow,
            "delivery_window_start": None,
            "delivery_window_end": "10:00",
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order = resp.get_json()["data"]["order"]
    assert order["delivery_date"] == tomorrow
    assert order["delivery_window"] == {"start": None, "end": "10:00", "kind": "until", "label": "until 10:00"}

    with app.app_context():
        assert Delivery.query.filter_by(order_id=order["id"]).first() is None
        # Persisted as real Time columns, not as a re-parsed string: the whole
        # point of the schema change is that nothing downstream has to split a
        # "HH:MM-HH:MM" label apart again.
        persisted = Order.query.get(order["id"])
        assert persisted.delivery_date.isoformat() == tomorrow
        assert persisted.delivery_window_start is None
        assert persisted.delivery_window_end == time(10, 0)


def test_admin_create_rejects_a_date_beyond_the_horizon(app, client, admin_user, sample_product, user_address):
    # +2, not +1, and the real constant rather than a hardcoded 16: the test and
    # the endpoint read the clock at two different instants, so a request that
    # straddles Tashkent midnight sees the horizon move a day between the two
    # reads. At exactly horizon+1 that lands ON the boundary, and the check is a
    # strict `>`, so the order is ACCEPTED -- a 400-becomes-201 flake that cost a
    # full-suite run at 00:0x Tashkent on 2026-08-20. Sitting two days clear of
    # the boundary is immune to the drift while still testing the same rule.
    too_far = (_local_today() + timedelta(days=MAX_SCHEDULE_HORIZON_DAYS + 2)).isoformat()
    resp = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user),
        json={
            "user_id": user_address.user_id,
            "delivery_address_id": user_address.id,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "payment_method": "cash",
            "delivery_date": too_far,
        },
    )
    assert resp.status_code == 400
    assert str(MAX_SCHEDULE_HORIZON_DAYS) in resp.get_data(as_text=True)


def test_admin_create_rejects_an_unparseable_window(app, client, admin_user, sample_product, user_address):
    """A malformed time is a 400, never a 500: `parse_window_time` raises and
    the endpoint has to own that, otherwise the generic `except Exception`
    below it turns a typo into `Failed to create order`."""
    resp = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user),
        json={
            "user_id": user_address.user_id,
            "delivery_address_id": user_address.id,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "payment_method": "cash",
            "delivery_date": (_local_today() + timedelta(days=1)).isoformat(),
            "delivery_window_end": "25:99",
        },
    )
    assert resp.status_code == 400
    assert "Invalid delivery schedule" in resp.get_data(as_text=True)
    with app.app_context():
        assert Order.query.filter_by(user_id=user_address.user_id).count() == 0


def test_admin_create_without_a_date_still_gets_a_delivery(app, client, admin_user, sample_product, user_address):
    """The no-schedule path must behave exactly as it does today."""
    resp = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user),
        json={
            "user_id": user_address.user_id,
            "delivery_address_id": user_address.id,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "payment_method": "cash",
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order_id = resp.get_json()["data"]["order"]["id"]
    with app.app_context():
        from business_app.services.order_service import OrderService

        OrderService().update_order_status(order_id, OrderStatus.CONFIRMED)
        assert Delivery.query.filter_by(order_id=order_id).first() is not None


def test_public_create_order_persists_the_window(app, db, client, sample_user, sample_product, user_address):
    """The web checkout posts here. If this path drops the window, the feature
    looks like it works and stores nothing."""
    from datetime import datetime as _datetime

    sample_user.phone_verified_at = _datetime.now(timezone.utc)
    db.session.commit()

    tomorrow = (_local_today() + timedelta(days=1)).isoformat()
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    resp = client.post(
        "/api/v1/orders/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "delivery_date": tomorrow,
            "delivery_window_start": "19:00",
            "delivery_window_end": None,
            "source": "web",
        },
    )
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    with app.app_context():
        order = Order.query.get(resp.get_json()["data"]["order"]["id"])
        assert order.delivery_date.isoformat() == tomorrow
        assert order.delivery_window_start == time(19, 0)
        assert order.delivery_window_end is None


def test_public_create_order_rejects_a_past_date(app, db, client, sample_user, sample_product, user_address):
    """The public path must be governed by the SAME validator as the admin one —
    a customer-facing checkout that accepted yesterday would strand the order
    behind a release instant that has already gone by."""
    from datetime import datetime as _datetime

    sample_user.phone_verified_at = _datetime.now(timezone.utc)
    db.session.commit()

    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    resp = client.post(
        "/api/v1/orders/",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "delivery_date": (_local_today() - timedelta(days=1)).isoformat(),
            "source": "web",
        },
    )
    assert resp.status_code == 400
    assert "past" in resp.get_data(as_text=True)
    with app.app_context():
        assert Order.query.filter_by(user_id=sample_user.id).count() == 0


def test_admin_order_response_publishes_awaiting_release_and_release_at(
    app, client, admin_user, sample_product, user_address
):
    """`serialize_order_admin` must publish the release-gate state next to the
    window, so the operator screen can show "held until <time>" without a
    second round trip.

    A freshly admin-created cash order stays PENDING (instant-COD-confirm only
    fires for a customer with a delivered-order history, which this fixture
    user has none of) -- `is_awaiting_release` correctly reports False for a
    PENDING order (it isn't release-gated, it's simply unconfirmed), so this
    confirms it via the real status-update endpoint first, exactly like an
    operator would.
    """
    tomorrow = (_local_today() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/api/v1/admin/orders",
        headers=_admin_headers(app, admin_user),
        json={
            "user_id": user_address.user_id,
            "delivery_address_id": user_address.id,
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "payment_method": "cash",
            "delivery_date": tomorrow,
            "delivery_window_end": "10:00",
        },
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    order_id = resp.get_json()["data"]["order"]["id"]

    resp = client.put(
        f"/api/v1/admin/orders/{order_id}/status",
        headers=_admin_headers(app, admin_user),
        json={"status": "confirmed"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    order = resp.get_json()["data"]["order"]
    assert order["awaiting_release"] is True
    assert order["release_at"] is not None

    with app.app_context():
        from business_app.services.order_schedule_service import OrderScheduleService

        persisted = Order.query.get(order_id)
        assert order["release_at"] == OrderScheduleService.release_at(persisted).isoformat()
        assert Delivery.query.filter_by(order_id=order_id).first() is None


# ---------------------------------------------------------------------------
# Task 9: publish the window on every order payload + order the pool by
# deadline.
#
# Neither order below carries a `delivery_date` (unlike `_confirm_scheduled_
# order`'s other callers): a "deadline vs anytime" pool-ordering test has no
# business depending on the release gate at all, and giving one a `date.
# today()` here would make release depend on wall-clock time-of-day versus
# each rostered driver's shift start (`driver_on_shift` opens at 08:00
# Tashkent) -- exactly the local-vs-UTC-day trap called out for this task.
# A dateless order always releases immediately (`release_at` is None), so
# `ensure_delivery_if_due` creates the Delivery unconditionally here.
# ---------------------------------------------------------------------------


def test_pool_orders_deadline_first(app, client, driver_on_shift):
    """A 'until 10:00' order must outrank an older anytime order in the pool."""
    from business_app.services.order_schedule_service import OrderScheduleService

    older_id = _confirm_scheduled_order(app, driver_on_shift, None)
    with app.app_context():
        OrderScheduleService.ensure_delivery_if_due(Order.query.get(older_id))

    urgent_id = _confirm_scheduled_order(app, driver_on_shift, None, window_end=time(10, 0))
    with app.app_context():
        OrderScheduleService.ensure_delivery_if_due(Order.query.get(urgent_id))
        token = create_access_token(identity=str(driver_on_shift), additional_claims={"role": "delivery_driver"})

    resp = client.get("/api/v1/staff/delivery/pool", headers={"Authorization": f"Bearer {token}"})
    ids = [i["order_id"] for i in resp.get_json()["data"]["items"]]
    assert ids.index(urgent_id) < ids.index(older_id)


def test_pool_item_publishes_the_window(app, client, driver_on_shift):
    from business_app.services.order_schedule_service import OrderScheduleService

    order_id = _confirm_scheduled_order(app, driver_on_shift, None, window_start=time(19, 0))
    with app.app_context():
        OrderScheduleService.ensure_delivery_if_due(Order.query.get(order_id))
        token = create_access_token(identity=str(driver_on_shift), additional_claims={"role": "delivery_driver"})

    resp = client.get("/api/v1/staff/delivery/pool", headers={"Authorization": f"Bearer {token}"})
    item = next(i for i in resp.get_json()["data"]["items"] if i["order_id"] == order_id)
    assert item["delivery_window"] == {"start": "19:00", "end": None, "kind": "after", "label": "after 19:00"}
