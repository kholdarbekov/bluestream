"""TDD test: admin reassign_delivery migrates bottle binding to the new driver's session."""
import pytest
from datetime import datetime, UTC, timedelta
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.bottle import DriverBottleSession, DriverBottleSessionOrder
from business_app.services.admin_delivery_service import AdminDeliveryService
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, UserRole, UserType
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


@pytest.mark.unit
def test_reassign_migrates_binding_to_new_driver_session(db, app, sample_user, sample_product):
    from tests.unit.test_bottle_session_integration import _make_order_with_bottles
    a = _driver(db, "+998901900001"); b = _driver(db, "+998901900002")
    sa = DriverBottleSession(driver_user_id=a.id, bottles_loaded=20, status=DriverBottleSessionStatus.OPEN)
    sb = DriverBottleSession(driver_user_id=b.id, bottles_loaded=20, status=DriverBottleSessionStatus.OPEN)
    db.session.add_all([sa, sb]); db.session.flush()
    order = _make_order_with_bottles(db, sample_user, sample_product, quantity=2)
    d = Delivery(order_id=order.id, status=DeliveryStatus.ASSIGNED, delivery_person_id=a.id,
                 scheduled_date=datetime.now(UTC)+timedelta(hours=1), scheduled_time_slot="09:00-12:00")
    db.session.add(d); db.session.flush()
    BottleTrackingService().bind_order_to_session(sa.id, order.id, accepted_by_driver_id=a.id)
    db.session.commit()

    with app.test_request_context():
        AdminDeliveryService.reassign_delivery(d.id, new_person_id=b.id, actor_id=1)

    binding = DriverBottleSessionOrder.query.filter_by(order_id=order.id).first()
    assert binding.session_id == sb.id   # migrated onto B's session
    assert Delivery.query.get(d.id).delivery_person_id == b.id
