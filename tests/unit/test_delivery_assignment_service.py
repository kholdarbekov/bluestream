import pytest
from decimal import Decimal
from datetime import datetime, UTC, timedelta

from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder
from business_app.services.delivery_assignment_service import (
    DeliveryAssignmentService,
    AssignmentResult,
)
from shared.enums import AssignmentSource, DeliveryStatus, DriverBottleSessionStatus, UserRole, UserType
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.password_security import hash_password


def _driver(db, phone):
    from business_app.models.user import User
    u = User(phone=phone, first_name="D", last_name="R", password_hash=hash_password("TestPassword123!"),
             role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF, is_verified=True, created_at=datetime.now(UTC))
    db.session.add(u); db.session.flush()
    db.session.add(DeliveryPerson(user_id=u.id, full_name="D R", phone=phone, is_active=True, is_available=True,
                                  working_hours_start="00:00", working_hours_end="23:59"))
    db.session.flush()
    return u


def _open_session(db, driver, loaded=20):
    s = DriverBottleSession(driver_user_id=driver.id, bottles_loaded=loaded, status=DriverBottleSessionStatus.OPEN)
    db.session.add(s); db.session.flush()
    return s


def _scheduled_delivery(db, order):
    d = Delivery(order_id=order.id, status=DeliveryStatus.SCHEDULED,
                 scheduled_date=datetime.now(UTC) + timedelta(hours=1), scheduled_time_slot="09:00-12:00")
    db.session.add(d); db.session.flush()
    return d


@pytest.mark.unit
def test_assign_driver_resolves_by_user_id_and_sets_fields(db, app, sample_user, sample_product):
    """assign_driver sets delivery_person_id to the user_id, flips to ASSIGNED, writes history."""
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500001")
    _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _scheduled_delivery(db, order)
    db.session.commit()

    with app.test_request_context():
        res = DeliveryAssignmentService.assign_driver(
            delivery.id, driver_user_id=driver.id, actor_id=driver.id, source=AssignmentSource.AUTO)

    assert isinstance(res, AssignmentResult) and res.changed is True
    refreshed = Delivery.query.get(delivery.id)
    assert refreshed.delivery_person_id == driver.id
    assert refreshed.status == DeliveryStatus.ASSIGNED
    # binding created onto the driver's open session
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first().session_id is not None


@pytest.mark.unit
def test_assign_driver_idempotent_same_driver(db, app, sample_user, sample_product):
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500002")
    _open_session(db, driver)
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = _scheduled_delivery(db, order)
    db.session.commit()
    with app.test_request_context():
        DeliveryAssignmentService.assign_driver(delivery.id, driver_user_id=driver.id, actor_id=driver.id, source=AssignmentSource.AUTO)
        res2 = DeliveryAssignmentService.assign_driver(delivery.id, driver_user_id=driver.id, actor_id=driver.id, source=AssignmentSource.AUTO)
    assert res2.changed is False


@pytest.mark.unit
def test_assign_driver_rejects_in_progress_unless_allowed(db, app, sample_user, sample_product):
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500003")
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = _scheduled_delivery(db, order)
    delivery.status = DeliveryStatus.IN_TRANSIT
    delivery.delivery_person_id = _driver(db, "+998901500099").id
    db.session.commit()
    with app.test_request_context():
        with pytest.raises(ValidationError) as exc:
            DeliveryAssignmentService.assign_driver(delivery.id, driver_user_id=driver.id, actor_id=1, source=AssignmentSource.ADMIN_BULK)
        assert exc.value.error_code == "STAFF_DELIVERY_NOT_CLAIMABLE"


@pytest.mark.unit
def test_assign_driver_unknown_driver_raises(db, app, sample_user, sample_product):
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=1)
    delivery = _scheduled_delivery(db, order)
    db.session.commit()
    with app.test_request_context():
        with pytest.raises(NotFoundError):
            DeliveryAssignmentService.assign_driver(delivery.id, driver_user_id=99999, actor_id=1, source=AssignmentSource.AUTO)


@pytest.mark.unit
def test_assign_driver_require_session_raises_when_no_session_strict(db, app, sample_user, sample_product):
    """Bot self-accept (require_session=True) demands an open session for bottle orders."""
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500004")  # NO open session
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _scheduled_delivery(db, order)
    db.session.commit()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                DeliveryAssignmentService.assign_driver(delivery.id, driver_user_id=driver.id, actor_id=driver.id,
                                                        source=AssignmentSource.BOT_SELF_ACCEPT, require_session=True)
            assert exc.value.error_code == "BOTTLE_SESSION_REQUIRED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False


@pytest.mark.unit
def test_assign_driver_no_session_defers_when_not_required(db, app, sample_user, sample_product):
    """Auto/admin paths (require_session=False) succeed with no session and no binding — A backstops later."""
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500005")
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    delivery = _scheduled_delivery(db, order)
    db.session.commit()
    with app.test_request_context():
        res = DeliveryAssignmentService.assign_driver(delivery.id, driver_user_id=driver.id, actor_id=1, source=AssignmentSource.AUTO)
    assert res.changed is True
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.unit
def test_assign_driver_over_capacity_defers_when_not_required(db, app, sample_user, sample_product):
    """Auto-assign with a session that cannot cover the load must succeed without binding.

    Driver has a session with only 1 bottle loaded; order requires 5.
    require_session=False (source=AUTO) → assignment succeeds (changed=True),
    no DriverBottleSessionOrder binding is created, and no exception is raised.
    The progress-time guard will enforce capacity later.
    """
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500006")
    _open_session(db, driver, loaded=1)  # session exists but has only 1 bottle
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=5)  # needs 5
    delivery = _scheduled_delivery(db, order)
    db.session.commit()
    with app.test_request_context():
        res = DeliveryAssignmentService.assign_driver(
            delivery.id, driver_user_id=driver.id, actor_id=1, source=AssignmentSource.AUTO
        )
    assert res.changed is True
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).first() is None


@pytest.mark.unit
def test_assign_driver_over_capacity_raises_when_require_session(db, app, sample_user, sample_product):
    """Bot self-accept with a session that cannot cover the load must raise BOTTLE_SESSION_CAPACITY_EXCEEDED.

    Driver has a session with only 1 bottle loaded; order requires 5.
    require_session=True (source=BOT_SELF_ACCEPT) → raises ValidationError
    with error_code BOTTLE_SESSION_CAPACITY_EXCEEDED.
    """
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    driver = _driver(db, "+998901500007")
    _open_session(db, driver, loaded=1)  # session exists but has only 1 bottle
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=5)  # needs 5
    delivery = _scheduled_delivery(db, order)
    db.session.commit()
    with app.test_request_context():
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError) as exc:
                DeliveryAssignmentService.assign_driver(
                    delivery.id, driver_user_id=driver.id, actor_id=driver.id,
                    source=AssignmentSource.BOT_SELF_ACCEPT, require_session=True
                )
            assert exc.value.error_code == "BOTTLE_SESSION_CAPACITY_EXCEEDED"
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
