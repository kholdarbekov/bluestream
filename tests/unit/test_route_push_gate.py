"""The single push gate (spec §5.2): sounded ⟺ head_changed AND NOT
driver_initiated. Table-driven over trigger × change-shape, asserting the
EXACT webhook payload — not merely that a webhook was called (spec §11)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.tasks.delivery_tasks import optimize_driver_route_task
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="gate-driver@example.com",
        phone="+998900000071",
        password_hash="x",
        first_name="Gate",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
        telegram_id="777000071",
    )
    db.session.add(user)
    db.session.commit()
    person = DeliveryPerson(
        user_id=user.id,
        full_name="Gate Driver",
        phone="+998900000071",
        current_location_lat=41.3000,
        current_location_lng=69.2500,
        last_location_update=datetime.now(UTC),
        is_active=True,
        is_available=True,
    )
    db.session.add(person)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="gate-cust@example.com",
        phone="+998900000072",
        password_hash="x",
        first_name="C",
        last_name="",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_delivery(db, customer_id, driver_id, order_no, lat, lng):
    addr = UserAddress(
        user_id=customer_id,
        title="Stop",
        full_address=f"Stop {order_no}",
        street_address=f"Stop {order_no}",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer_id,
        order_number=order_no,
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("10000"),
        total_amount=Decimal("10000"),
        delivery_address_id=addr.id,
        delivery_date=datetime.now(UTC) + timedelta(hours=2),
        delivery_time_slot="09:00-12:00",
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _haversine_matrix(self, points, traffic=True, use_cache=True):
    from business_app.utils.helpers import calculate_distance

    matrix = {}
    for i, pi in enumerate(points):
        for j, pj in enumerate(points):
            km = 0.0 if i == j else calculate_distance(pi[0], pi[1], pj[0], pj[1])
            matrix[(i, j)] = {"distance_km": km, "duration_minutes": km * 2.4}
    return matrix, "haversine"


@pytest.fixture(autouse=True)
def _matrix(monkeypatch):
    monkeypatch.setattr(
        "business_app.services.maps_service.MapsService.get_distance_matrix",
        _haversine_matrix,
    )


def _run_task_capturing_webhook(app, driver_id, trigger):
    with app.app_context():
        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            result = optimize_driver_route_task.run(driver_id, trigger=trigger)
    calls = [c for c in hook.call_args_list if c.args[0] == "/internal/route-updated"]
    payloads = [c.args[1] for c in calls]
    return result, payloads


@pytest.mark.unit
@pytest.mark.delivery
class TestPushGate:
    def test_driver_initiated_head_change_is_silent(self, app, db, driver, customer):
        """First solve after 'accept': head appears, but the driver caused it."""
        _make_delivery(db, customer.id, driver.id, "ORD-G-1", 41.30, 69.26)
        result, payloads = _run_task_capturing_webhook(app, driver.id, "accept")
        assert result["optimized"] is True
        assert result["sounded"] is False
        assert len(payloads) == 1
        p = payloads[0]
        assert p["sound"] is False
        assert p["head_changed"] is True
        assert p["driver_initiated"] is True
        assert p["trigger"] == "accept"
        assert p["driver_id"] == driver.id
        assert p["telegram_id"] == 777000071

    def test_dispatch_head_change_is_sounded(self, app, db, driver, customer):
        """New order lands as the next stop with trigger='auto' (pool/dispatch):
        exactly one sounded payload (spec §11: 'a dispatch reorder that
        changes the head sends exactly one sounded message').

        This is the money case carried from Task 5's review: the only
        prior coverage that head_changed=True *persists* was the
        first-ever solve (prev_order=[]), which is trivially true and
        would also pass under a wrong rule. Here we do a SECOND normal
        solve where a stop lands strictly nearer the driver than the
        current head and genuinely displaces it -- precisely the state
        the gate must sound on -- and verify the verdict against a
        freshly re-queried DB row, not just the in-memory object the
        service handed back."""
        far = _make_delivery(db, customer.id, driver.id, "ORD-G-2", 41.30, 69.33)
        _run_task_capturing_webhook(app, driver.id, "accept")  # establish route
        _make_delivery(db, customer.id, driver.id, "ORD-G-3", 41.3005, 69.2505)
        result, payloads = _run_task_capturing_webhook(app, driver.id, "auto")
        assert result["sounded"] is True
        assert len(payloads) == 1
        p = payloads[0]
        assert p["sound"] is True
        assert p["head_changed"] is True
        assert p["set_changed"] is True
        assert p["driver_initiated"] is False

        with app.app_context():
            persisted = (
                DeliveryRoute.query.filter_by(delivery_person_id=driver.id)
                .order_by(DeliveryRoute.created_at.desc())
                .first()
            )
            assert (persisted.extra_data or {})["materiality"]["head_changed"] is True

    def test_mid_tail_insertion_is_silent(self, app, db, driver, customer):
        _make_delivery(db, customer.id, driver.id, "ORD-G-4", 41.30, 69.26)
        _make_delivery(db, customer.id, driver.id, "ORD-G-5", 41.30, 69.33)
        _run_task_capturing_webhook(app, driver.id, "accept")
        _make_delivery(db, customer.id, driver.id, "ORD-G-6", 41.30, 69.29)  # mid-tail
        result, payloads = _run_task_capturing_webhook(app, driver.id, "auto")
        assert result["sounded"] is False
        assert payloads[0]["sound"] is False
        assert payloads[0]["head_changed"] is False
        assert payloads[0]["set_changed"] is True

    def test_delivery_trigger_is_always_silent(self, app, db, driver, customer):
        """(head_unchanged, driver_initiated) cell of the gate's truth table.

        Two deliveries so the head genuinely does NOT move between the
        establishing 'accept' solve and this 'delivery' solve (with a single
        delivery, completing it would empty the set entirely and this would
        collapse into the no-active-deliveries case instead). Without the
        `head_changed is False` assertion this test would still pass if the
        head HAD changed -- it would just be re-proving the (True, True)
        cell via `sound is False` alone, not the fourth row."""
        _make_delivery(db, customer.id, driver.id, "ORD-G-7", 41.30, 69.26)
        _make_delivery(db, customer.id, driver.id, "ORD-G-8", 41.30, 69.33)
        _run_task_capturing_webhook(app, driver.id, "accept")
        result, payloads = _run_task_capturing_webhook(app, driver.id, "delivery")
        assert result["sounded"] is False
        assert payloads[0]["sound"] is False
        assert payloads[0]["head_changed"] is False
        assert payloads[0]["driver_initiated"] is True

    def test_no_webhook_when_no_active_deliveries(self, app, db, driver):
        result, payloads = _run_task_capturing_webhook(app, driver.id, "auto")
        assert result == {"optimized": False, "reason": "no_active_deliveries"}
        assert payloads == []

    def test_stale_verdict_not_resurrected_when_active_set_empties_after_a_solve(
        self, app, db, driver, customer
    ):
        """Solve once (head_changed=True is published), THEN the active set
        empties entirely (the one delivery gets delivered) -- a later
        optimize_for_driver call must return None and the task must bail out
        BEFORE the gate, not resurrect the old materiality verdict onto a
        route that no longer has an active head at all.

        Structurally this can't fail today (the task returns early on `route
        is None`, before computing `sound`/pushing a webhook), but only
        `test_no_webhook_when_no_active_deliveries` existed and it only
        covers the empty-from-the-start variant. This locks the
        solve-then-empty shape against a future refactor (e.g. "just
        re-query the route instead of trusting optimize_for_driver's
        return") that could start publishing a materiality dict left over
        from the prior, now-irrelevant solve.
        """
        delivery = _make_delivery(db, customer.id, driver.id, "ORD-G-9", 41.30, 69.26)
        result, payloads = _run_task_capturing_webhook(app, driver.id, "accept")
        assert result["optimized"] is True
        assert payloads[0]["head_changed"] is True

        with app.app_context():
            live = Delivery.query.get(delivery.id)
            live.status = DeliveryStatus.DELIVERED
            db.session.commit()

        result2, payloads2 = _run_task_capturing_webhook(app, driver.id, "auto")
        assert result2 == {"optimized": False, "reason": "no_active_deliveries"}
        assert payloads2 == []


@pytest.mark.unit
@pytest.mark.delivery
class TestDriverRequestedOptimizationEndpointDoesNotSound:
    """Carried from Task 5's review: POST
    /api/v1/delivery/driver/route-optimization enqueued the task with no
    `trigger`, defaulting to 'auto' -> driver_initiated=False -> the gate
    would sound for an optimization the driver explicitly requested. Fixed
    by passing trigger='manual' at the call site (business_app/api/delivery.py).
    """

    def test_endpoint_enqueues_with_manual_trigger(self, app, client, db, driver):
        with app.app_context():
            token = create_access_token(identity=str(driver.id))
        with patch(
            "business_app.tasks.delivery_tasks.optimize_driver_route_task.delay"
        ) as mock_delay:
            response = client.post(
                "/api/v1/delivery/driver/route-optimization",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        # get_jwt_identity() returns the string identity set at token
        # creation; the endpoint passes it through unconverted.
        mock_delay.assert_called_once_with(str(driver.id), trigger="manual")

    def test_manual_trigger_stays_silent_even_on_first_solve(self, app, db, driver, customer):
        """End-to-end proof using the exact args the endpoint now enqueues:
        even on the very first solve for this driver (head_changed=True,
        since there's no previous order), a driver-requested 'manual'
        optimization must stay silent."""
        _make_delivery(db, customer.id, driver.id, "ORD-G-MANUAL", 41.30, 69.26)
        result, payloads = _run_task_capturing_webhook(app, driver.id, "manual")
        assert result["sounded"] is False
        assert len(payloads) == 1
        assert payloads[0]["sound"] is False
        assert payloads[0]["head_changed"] is True
        assert payloads[0]["driver_initiated"] is True
        assert payloads[0]["trigger"] == "manual"
