"""Committed-stop definition (spec §4.1): among IN_TRANSIT/ARRIVED deliveries,
the one whose latest transition INTO its current status is most recent by
DeliveryStatusHistory.changed_at. None when no such delivery exists."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.route_optimization_service import RouteOptimizationService
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


@pytest.fixture
def driver(db):
    user = User(
        email="commit-driver@example.com",
        phone="+998900000031",
        password_hash="x",
        first_name="Commit",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def customer(db):
    user = User(
        email="commit-cust@example.com",
        phone="+998900000032",
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


def _make_delivery(db, customer_id, driver_id, order_no, status):
    addr = UserAddress(
        user_id=customer_id,
        title="Stop",
        full_address=f"Stop {order_no}",
        street_address=f"Stop {order_no}",
        latitude=41.31,
        longitude=69.27,
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
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _add_history(db, delivery, old, new, changed_at):
    db.session.add(
        DeliveryStatusHistory(
            delivery_id=delivery.id,
            old_status=old,
            new_status=new,
            changed_by=delivery.delivery_person_id,
            changed_at=changed_at,
        )
    )
    db.session.commit()


@pytest.mark.unit
@pytest.mark.delivery
class TestGetCommittedStop:
    def test_none_when_nothing_started(self, app, db, driver, customer):
        _make_delivery(db, customer.id, driver.id, "ORD-CS-1", DeliveryStatus.ASSIGNED)
        _make_delivery(db, customer.id, driver.id, "ORD-CS-2", DeliveryStatus.PICKED_UP)
        with app.app_context():
            assert RouteOptimizationService().get_committed_stop(driver.id) is None

    def test_single_in_transit_is_committed(self, app, db, driver, customer):
        d = _make_delivery(db, customer.id, driver.id, "ORD-CS-3", DeliveryStatus.IN_TRANSIT)
        now = datetime.now(UTC)
        _add_history(db, d, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now)
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed is not None and committed.id == d.id

    def test_most_recently_started_wins_across_statuses(self, app, db, driver, customer):
        """Two simultaneously-active started stops (reachable after a
        diversion, spec §12.7): recency of the transition decides."""
        now = datetime.now(UTC)
        d_old = _make_delivery(db, customer.id, driver.id, "ORD-CS-4", DeliveryStatus.ARRIVED)
        _add_history(db, d_old, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now - timedelta(minutes=30))
        _add_history(db, d_old, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED, now - timedelta(minutes=10))
        d_new = _make_delivery(db, customer.id, driver.id, "ORD-CS-5", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_new, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now - timedelta(minutes=2))
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed.id == d_new.id

    def test_stale_history_of_current_status_only(self, app, db, driver, customer):
        """A delivery's OLD transition into IN_TRANSIT must not outrank
        another's newer one, even if the first also has a very recent
        transition into a NON-current status recorded (only rows where
        new_status == the delivery's CURRENT status count)."""
        now = datetime.now(UTC)
        d_a = _make_delivery(db, customer.id, driver.id, "ORD-CS-6", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_a, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now - timedelta(minutes=20))
        d_b = _make_delivery(db, customer.id, driver.id, "ORD-CS-7", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_b, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now - timedelta(minutes=5))
        # Noise row on d_a with a newer timestamp but a non-current new_status.
        _add_history(db, d_a, DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP, now - timedelta(minutes=1))
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed.id == d_b.id

    def test_other_drivers_deliveries_ignored(self, app, db, driver, customer):
        other = User(
            email="commit-other@example.com",
            phone="+998900000033",
            password_hash="x",
            first_name="O",
            last_name="",
            user_type=UserType.STAFF,
            role=UserRole.DELIVERY_DRIVER,
            is_verified=True,
        )
        db.session.add(other)
        db.session.commit()
        d_other = _make_delivery(db, customer.id, other.id, "ORD-CS-8", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_other, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, datetime.now(UTC))
        with app.app_context():
            assert RouteOptimizationService().get_committed_stop(driver.id) is None

    def test_fallback_without_history_rows(self, app, db, driver, customer):
        """Defensive: a delivery moved to IN_TRANSIT outside the status
        services has no history row; it must still anchor the route rather
        than silently reverting to GPS-anchored behaviour."""
        d = _make_delivery(db, customer.id, driver.id, "ORD-CS-9", DeliveryStatus.IN_TRANSIT)
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed is not None and committed.id == d.id

    def test_arrived_only_delivery_is_committed(self, app, db, driver, customer):
        """ARRIVED alone, with no IN_TRANSIT delivery in play, must be
        sufficient on its own. Guards against COMMITTED_STATUSES being
        narrowed to (IN_TRANSIT,) — the other 5 tests in this file all still
        pass under that narrower filter, so this is the one that would catch
        it."""
        d = _make_delivery(db, customer.id, driver.id, "ORD-CS-10", DeliveryStatus.ARRIVED)
        now = datetime.now(UTC)
        _add_history(db, d, DeliveryStatus.IN_TRANSIT, DeliveryStatus.ARRIVED, now)
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed is not None and committed.id == d.id

    def test_tiebreak_on_identical_changed_at_is_deterministic(self, app, db, driver, customer):
        """Two committed deliveries with the exact same changed_at must
        resolve deterministically (higher delivery_id wins), not whatever
        order the DB engine happens to return grouped rows in."""
        now = datetime.now(UTC)
        d_low = _make_delivery(db, customer.id, driver.id, "ORD-CS-11", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_low, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now)
        d_high = _make_delivery(db, customer.id, driver.id, "ORD-CS-12", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_high, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now)
        assert d_high.id > d_low.id
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed.id == d_high.id

    def test_committed_delivery_within_max_age_is_returned(self, app, db, driver, customer):
        max_age_hours = app.config.get("COMMITTED_STOP_MAX_AGE_HOURS", 12)
        d = _make_delivery(db, customer.id, driver.id, "ORD-CS-13", DeliveryStatus.IN_TRANSIT)
        changed_at = datetime.now(UTC) - timedelta(hours=max_age_hours) + timedelta(minutes=5)
        _add_history(db, d, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, changed_at)
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed is not None and committed.id == d.id

    def test_committed_delivery_past_max_age_is_ignored(self, app, db, driver, customer):
        """A delivery that entered IN_TRANSIT/ARRIVED more than
        COMMITTED_STOP_MAX_AGE_HOURS ago is still an active delivery, but it
        must stop anchoring the route — else a never-completed delivery pins
        itself to position 0 forever (spec amendment, prod finding: driver
        1's delivery 56 has been IN_TRANSIT for a month)."""
        max_age_hours = app.config.get("COMMITTED_STOP_MAX_AGE_HOURS", 12)
        d = _make_delivery(db, customer.id, driver.id, "ORD-CS-14", DeliveryStatus.IN_TRANSIT)
        changed_at = datetime.now(UTC) - timedelta(hours=max_age_hours) - timedelta(minutes=5)
        _add_history(db, d, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, changed_at)
        with app.app_context():
            assert RouteOptimizationService().get_committed_stop(driver.id) is None

    def test_fresh_wins_over_stale_when_both_present(self, app, db, driver, customer):
        max_age_hours = app.config.get("COMMITTED_STOP_MAX_AGE_HOURS", 12)
        now = datetime.now(UTC)
        d_stale = _make_delivery(db, customer.id, driver.id, "ORD-CS-15", DeliveryStatus.IN_TRANSIT)
        _add_history(
            db,
            d_stale,
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            now - timedelta(hours=max_age_hours, minutes=30),
        )
        d_fresh = _make_delivery(db, customer.id, driver.id, "ORD-CS-16", DeliveryStatus.IN_TRANSIT)
        _add_history(db, d_fresh, DeliveryStatus.PICKED_UP, DeliveryStatus.IN_TRANSIT, now - timedelta(minutes=5))
        with app.app_context():
            committed = RouteOptimizationService().get_committed_stop(driver.id)
            assert committed.id == d_fresh.id
