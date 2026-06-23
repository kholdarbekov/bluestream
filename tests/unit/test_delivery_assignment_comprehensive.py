"""Comprehensive behavioural tests for delivery driver auto-assignment.

These complement (do NOT duplicate) the existing focused regressions in
``tests/unit/test_delivery_driver_assignment.py``,
``tests/unit/test_auto_assign_delivery_task.py`` and
``tests/unit/test_notify_driver_assignment_task.py``.

Prod incident recap (all silently passed the SQLite suite):

* ``DeliveryService.assign_delivery_driver`` wrote the PHANTOM attribute
  ``delivery.driver_id`` (the real mapped column is ``delivery_person_id``).
  The write was dropped, the row committed with ``status='assigned'`` and
  ``delivery_person_id=NULL`` -> ``ck_deliveries_person_required_after_assigned``
  CheckViolation on Postgres.
* The driver was validated via the drift-prone singular ``User.role`` instead
  of the canonical ``DeliveryPerson`` profile, so legitimate drivers raised
  ``NotFoundError('Driver not found')``.
* ``notify_driver_assignment_task`` referenced phantom columns
  (``delivery.driver`` / ``delivery.driver_id`` / ``delivery.tracking_code`` /
  ``delivery.delivery_address_street``).

These tests assert the ACTUAL PERSISTED COLUMN VALUES after each operation so a
regression to a phantom attribute is caught even on SQLite (which ignores the
migration-only CHECK constraint).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.delivery_service import DeliveryService
from business_app.tasks import notification_tasks
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from business_app.tasks.notification_tasks import notify_driver_assignment_task
from business_app.utils.exceptions import NotFoundError, ValidationError
from shared.enums import DeliveryStatus, OrderStatus, UserRole, UserType


_run_notify = notify_driver_assignment_task.run.__func__


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_user(db, *, role, email, phone, user_type=UserType.STAFF):
    user = User(
        email=email,
        phone=phone,
        password_hash="x",
        first_name="Drv",
        last_name="R",
        user_type=user_type,
        role=role,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_driver(
    db,
    *,
    role=UserRole.DELIVERY_DRIVER,
    email,
    phone,
    is_active=True,
    is_available=True,
    create_profile=True,
):
    """A staff user who (optionally) has an active DeliveryPerson profile."""
    user = _make_user(db, role=role, email=email, phone=phone)
    if create_profile:
        person = DeliveryPerson(
            user_id=user.id,
            full_name="Drv R",
            phone=phone,
            is_active=is_active,
            is_available=is_available,
            # 24h coverage so is_working_now is True regardless of wall clock.
            working_hours_start="00:00",
            working_hours_end="23:59",
        )
        db.session.add(person)
        db.session.commit()
    return user


_ORDER_SEQ = {"n": 0}


def _make_scheduled_delivery(db, *, lat=41.3, lng=69.25, status=DeliveryStatus.SCHEDULED):
    _ORDER_SEQ["n"] += 1
    n = _ORDER_SEQ["n"]
    customer = User(
        email=f"cust-comp-{n}@example.com",
        phone=f"+99890700{n:04d}",
        password_hash="x",
        first_name="C",
        last_name="U",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(customer)
    db.session.commit()
    addr = UserAddress(
        user_id=customer.id,
        title="h",
        full_address="12 Main St",
        street_address="12 Main St",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(addr)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=f"ORD-COMP-{n}",
        status=OrderStatus.CONFIRMED,
        subtotal=Decimal("0"),
        total_amount=Decimal("0"),
        delivery_address_id=addr.id,
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        status=status,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


# ---------------------------------------------------------------------------
# assign_delivery_driver — service behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestAssignDeliveryDriverService:
    def test_assigns_when_role_is_operator_drift(self, app, db):
        """role drift: an OPERATOR who drives must still be assignable because
        identity is the DeliveryPerson profile, not User.role."""
        with app.app_context():
            driver = _make_driver(
                db, role=UserRole.OPERATOR, email="op1@x.com", phone="+998901111201"
            )
            delivery = _make_scheduled_delivery(db)

            DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id == driver.id
            assert refreshed.status == DeliveryStatus.ASSIGNED

    def test_assigns_when_role_is_admin_drift(self, app, db):
        """ADMIN role drift variant — same canonical DeliveryPerson identity."""
        with app.app_context():
            driver = _make_driver(
                db, role=UserRole.ADMIN, email="adm1@x.com", phone="+998901111202"
            )
            delivery = _make_scheduled_delivery(db)

            DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id == driver.id
            assert refreshed.status == DeliveryStatus.ASSIGNED

    def test_phantom_driver_id_column_never_set(self, app, db):
        """The legacy phantom write target must NOT leak back: there is no
        mapped ``driver_id`` column, only ``delivery_person_id`` carries the
        driver. Guards against re-introducing the silent no-op write."""
        with app.app_context():
            driver = _make_driver(db, email="ph1@x.com", phone="+998901111203")
            delivery = _make_scheduled_delivery(db)

            DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            # The real column is populated...
            assert refreshed.delivery_person_id == driver.id
            # ...and the phantom attribute is not a mapped column.
            assert "driver_id" not in Delivery.__table__.columns

    def test_raises_not_found_when_no_delivery_person_profile(self, app, db):
        """A user with the right role but NO DeliveryPerson profile is not a
        valid driver — assignment must raise, never leave a NULL person."""
        with app.app_context():
            # role looks right, but no profile created.
            user = _make_user(
                db, role=UserRole.DELIVERY_DRIVER, email="np1@x.com", phone="+998901111204"
            )
            delivery = _make_scheduled_delivery(db)

            with pytest.raises(NotFoundError, match="Driver not found"):
                DeliveryService().assign_delivery_driver(delivery.id, user.id)

            refreshed = Delivery.query.get(delivery.id)
            # Row must remain in the pool, untouched.
            assert refreshed.delivery_person_id is None
            assert refreshed.status == DeliveryStatus.SCHEDULED

    def test_raises_not_found_when_profile_inactive(self, app, db):
        """An inactive DeliveryPerson profile is not assignable (filter_by
        is_active=True). Status must NOT flip to ASSIGNED."""
        with app.app_context():
            driver = _make_driver(
                db, email="inact1@x.com", phone="+998901111205", is_active=False
            )
            delivery = _make_scheduled_delivery(db)

            with pytest.raises(NotFoundError, match="Driver not found"):
                DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id is None
            assert refreshed.status == DeliveryStatus.SCHEDULED

    def test_raises_not_found_when_delivery_unknown(self, app, db):
        with app.app_context():
            driver = _make_driver(db, email="unk1@x.com", phone="+998901111206")
            with pytest.raises(NotFoundError, match="Delivery not found"):
                DeliveryService().assign_delivery_driver(999999, driver.id)

    def test_records_assigned_at_in_route_data(self, app, db):
        """route_data must carry an ISO ``assigned_at`` timestamp post-assign."""
        with app.app_context():
            driver = _make_driver(db, email="ra1@x.com", phone="+998901111207")
            delivery = _make_scheduled_delivery(db)

            before = datetime.now(UTC)
            DeliveryService().assign_delivery_driver(delivery.id, driver.id)
            after = datetime.now(UTC)

            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.route_data is not None
            assert "assigned_at" in refreshed.route_data
            stamped = datetime.fromisoformat(refreshed.route_data["assigned_at"])
            assert before <= stamped <= after

    def test_assigned_at_preserves_existing_route_data(self, app, db):
        """Stamping assigned_at must merge, not clobber, prior route_data."""
        with app.app_context():
            driver = _make_driver(db, email="ra2@x.com", phone="+998901111208")
            delivery = _make_scheduled_delivery(db)
            delivery.route_data = {"prior": "keep-me"}
            db.session.commit()

            DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.route_data["prior"] == "keep-me"
            assert "assigned_at" in refreshed.route_data

    def test_assign_rejects_reassigning_an_already_assigned_delivery(self, app, db):
        """Since assignment was unified under DeliveryAssignmentService.assign_driver,
        the auto/single-assign path (assign_delivery_driver) only claims a pool
        delivery — it must NOT silently steal an already-ASSIGNED delivery from
        another driver. Reassignment is the admin reassign path's job
        (AdminDeliveryService.reassign_delivery, allow_in_progress=True)."""
        with app.app_context():
            driver_a = _make_driver(db, email="rA@x.com", phone="+998901111209")
            driver_b = _make_driver(db, email="rB@x.com", phone="+998901111210")
            delivery = _make_scheduled_delivery(db)

            svc = DeliveryService()
            svc.assign_delivery_driver(delivery.id, driver_a.id)
            assert Delivery.query.get(delivery.id).delivery_person_id == driver_a.id

            with pytest.raises(ValidationError) as exc:
                svc.assign_delivery_driver(delivery.id, driver_b.id)
            assert exc.value.error_code == "STAFF_DELIVERY_NOT_CLAIMABLE"

            # The original driver keeps the delivery.
            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id == driver_a.id

    def test_assignment_enqueues_driver_notification(self, app, db):
        """The post-commit _notify_driver hook enqueues the driver-assignment
        notification task addressed to this delivery."""
        with app.app_context():
            driver = _make_driver(db, email="nq1@x.com", phone="+998901111211")
            delivery = _make_scheduled_delivery(db)

            with patch.object(
                notification_tasks.notify_driver_assignment_task, "delay"
            ) as notify_delay:
                DeliveryService().assign_delivery_driver(delivery.id, driver.id)

            notify_delay.assert_called_once_with(delivery.id)


# ---------------------------------------------------------------------------
# auto_assign_delivery_task — end-to-end with real DB
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestAutoAssignDeliveryTaskEndToEnd:
    def test_assigns_real_candidate_no_check_violation(self, app, db):
        """The whole path: a SCHEDULED delivery + one active DeliveryPerson ->
        delivery becomes ASSIGNED with delivery_person_id set, and the result
        dict reports success. This is the exact prod path that CheckViolated."""
        with app.app_context():
            driver = _make_driver(db, email="aa1@x.com", phone="+998901112201")
            delivery = _make_scheduled_delivery(db)

            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError(
                "must not retry when a driver is available"
            )

            result = auto_assign_delivery_task.run.__func__(mock_self, delivery.id)

            assert result["success"] is True
            assert result["driver_id"] == driver.id

            refreshed = Delivery.query.get(delivery.id)
            # The mapped FK is populated BEFORE status flips -> no CheckViolation.
            assert refreshed.delivery_person_id == driver.id
            assert refreshed.status == DeliveryStatus.ASSIGNED

    def test_candidate_with_drifted_role_still_assigns(self, app, db):
        """A candidate whose User.role is NOT delivery_driver but who has an
        active DeliveryPerson profile must still be auto-assigned."""
        with app.app_context():
            driver = _make_driver(
                db, role=UserRole.OPERATOR, email="aa2@x.com", phone="+998901112202"
            )
            delivery = _make_scheduled_delivery(db)

            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("should not retry")

            result = auto_assign_delivery_task.run.__func__(mock_self, delivery.id)

            assert result["success"] is True
            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id == driver.id
            assert refreshed.status == DeliveryStatus.ASSIGNED

    def test_no_candidates_retries(self, app, db):
        """Zero active DeliveryPerson candidates -> the no-driver retry path.
        We script self.retry to raise (as a real worker does) and assert the
        delivery is left untouched in the pool."""
        from celery.exceptions import Retry

        with app.app_context():
            delivery = _make_scheduled_delivery(db)  # no drivers seeded

            mock_self = MagicMock()
            mock_self.retry.side_effect = Retry("Retry in 900s")

            with pytest.raises(Retry):
                auto_assign_delivery_task.run.__func__(mock_self, delivery.id)

            mock_self.retry.assert_called_once_with(countdown=900)
            refreshed = Delivery.query.get(delivery.id)
            assert refreshed.delivery_person_id is None
            assert refreshed.status == DeliveryStatus.SCHEDULED

    def test_inactive_only_candidate_is_no_available_driver(self, app, db):
        """An inactive DeliveryPerson is filtered out of candidates, so an
        otherwise-empty pool falls to the no-driver retry path."""
        from celery.exceptions import Retry

        with app.app_context():
            _make_driver(
                db, email="aa3@x.com", phone="+998901112203", is_active=False
            )
            delivery = _make_scheduled_delivery(db)

            mock_self = MagicMock()
            mock_self.retry.side_effect = Retry("Retry in 900s")

            with pytest.raises(Retry):
                auto_assign_delivery_task.run.__func__(mock_self, delivery.id)

            mock_self.retry.assert_called_once_with(countdown=900)
            assert Delivery.query.get(delivery.id).delivery_person_id is None

    def test_already_assigned_delivery_is_skipped(self, app, db):
        """A delivery that is no longer SCHEDULED short-circuits with a
        no-longer-scheduled result and is not re-assigned."""
        with app.app_context():
            driver = _make_driver(db, email="aa4@x.com", phone="+998901112204")
            delivery = _make_scheduled_delivery(db, status=DeliveryStatus.SCHEDULED)
            # Pre-assign it.
            delivery.delivery_person_id = driver.id
            delivery.status = DeliveryStatus.ASSIGNED
            db.session.commit()

            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("should not retry")

            result = auto_assign_delivery_task.run.__func__(mock_self, delivery.id)

            assert result["success"] is False
            assert result["error"] == "Delivery no longer scheduled"

    def test_unknown_delivery_returns_not_found(self, app, db):
        with app.app_context():
            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("should not retry")

            result = auto_assign_delivery_task.run.__func__(mock_self, 987654)

            assert result == {"success": False, "error": "Delivery not found"}


# ---------------------------------------------------------------------------
# notify_driver_assignment_task — real columns
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.delivery
class TestNotifyDriverAssignmentTask:
    def _assigned_delivery(self, db, *, phone, email):
        driver = _make_driver(db, email=email, phone=phone)
        delivery = _make_scheduled_delivery(db)
        delivery.delivery_person_id = driver.id
        delivery.status = DeliveryStatus.ASSIGNED
        db.session.commit()
        return driver, delivery

    def test_notifies_with_real_columns(self, app, db):
        """Addressed to delivery_person_id, with tracking_number as
        tracking_code and the order's street_address — all real columns."""
        with app.app_context():
            driver, delivery = self._assigned_delivery(
                db, phone="+998901113201", email="nd1@x.com"
            )
            delivery_id, driver_id = delivery.id, driver.id
            tracking_number = delivery.tracking_number

            fake = MagicMock()
            fake.send_notification.return_value = {"success": True}
            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("must not retry when healthy")

            with patch.object(notification_tasks, "NotificationService", return_value=fake):
                result = _run_notify(mock_self, delivery_id)

            assert result == {"success": True}
            assert fake.send_notification.call_args.args[0] == driver_id
            template = fake.send_notification.call_args.args[3]
            assert template["tracking_code"] == tracking_number
            assert template["delivery_address"] == "12 Main St"

    def test_early_return_when_no_delivery_person(self, app, db):
        """An unassigned delivery (no delivery_person) -> early return failure,
        no NotificationService call, no crash."""
        with app.app_context():
            delivery = _make_scheduled_delivery(db)  # no person assigned
            delivery_id = delivery.id

            fake = MagicMock()
            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("must not retry")

            with patch.object(notification_tasks, "NotificationService", return_value=fake):
                result = _run_notify(mock_self, delivery_id)

            assert result == {"success": False, "error": "Delivery or driver not found"}
            fake.send_notification.assert_not_called()

    def test_early_return_when_delivery_missing(self, app, db):
        with app.app_context():
            fake = MagicMock()
            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("must not retry")

            with patch.object(notification_tasks, "NotificationService", return_value=fake):
                result = _run_notify(mock_self, 765432)

            assert result == {"success": False, "error": "Delivery or driver not found"}
            fake.send_notification.assert_not_called()

    def test_template_carries_order_number_and_estimated_time(self, app, db):
        """Sanity that the rest of the template uses real columns too."""
        with app.app_context():
            driver, delivery = self._assigned_delivery(
                db, phone="+998901113202", email="nd2@x.com"
            )
            delivery.estimated_delivery_time = datetime.now(UTC) + timedelta(hours=2)
            order_number = delivery.order.order_number
            db.session.commit()
            delivery_id = delivery.id

            fake = MagicMock()
            fake.send_notification.return_value = {"success": True}
            mock_self = MagicMock()
            mock_self.retry.side_effect = AssertionError("must not retry")

            with patch.object(notification_tasks, "NotificationService", return_value=fake):
                _run_notify(mock_self, delivery_id)

            template = fake.send_notification.call_args.args[3]
            assert template["order_number"] == order_number
            assert template["estimated_delivery_time"] is not None
