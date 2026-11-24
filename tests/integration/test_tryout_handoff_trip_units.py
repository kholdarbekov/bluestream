"""POST /api/v1/staff/tryout-tasks/<id>/complete-handoff -- a try-out hand-off counts one-way units (10 L) on the trip.

Driven through the route the bot calls (`StaffAPIClient.complete_tryout_handoff`, an empty body). The try-out is set up
through `TryoutService.create_tryout` (setup only); the trip comes from the shared trip builders.
"""

from decimal import Decimal

import pytest

from business_app.models.bottle import DriverBottleSession, DriverBottleSessionUnit
from business_app.models.tryout import TryoutBottleLedger
from business_app.services.tryout_service import TryoutService
from shared.enums import TryoutTaskType
from tests.integration.trip_unit_builders import (  # noqa: F401 -- fixtures
    big,
    driver,
    small,
    staff_headers,
    trip,
    trip_customer,
)

pytestmark = pytest.mark.integration

HANDOFF = "/api/v1/staff/tryout-tasks/{}/complete-handoff"


def _handoff_task_id(db, driver, lines) -> int:
    admin = trip_customer(db)  # any user id: the audit actor of the create
    tryout = TryoutService.create_tryout(
        {
            "trial_contact": {"first_name": "Trial", "phone": "+998901400002", "preferred_language": "uz"},
            "address": {
                "full_address": "3 Trip Street, Tashkent",
                "city": "Tashkent",
                "latitude": 41.3113,
                "longitude": 69.2799,
                "is_default": True,
            },
            "items": [{"product_id": product.id, "quantity": quantity} for product, quantity in lines],
            "assigned_driver_user_id": driver.id,
        },
        admin.id,
        source="admin",
    )
    (task_id,) = [t.id for t in tryout.tasks if t.task_type is TryoutTaskType.HANDOFF]
    return task_id


def test_a_ten_litre_only_handoff_tallies_the_actors_line(client, app, db, driver, small):
    """Trip 10 L 12; the hand-off of 2 x 10 L puts 2 on the line and no bottle on the trip or the ledger."""
    session = trip(db, driver, units={small.id: 12})
    task_id = _handoff_task_id(db, driver, [(small, 2)])

    with app.app_context():
        headers = staff_headers(driver)
    response = client.post(HANDOFF.format(task_id), json={}, headers=headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    line = DriverBottleSessionUnit.query.filter_by(session_id=session.id, product_id=small.id).one()
    assert (line.loaded, line.delivered) == (12, 2)
    assert db.session.get(DriverBottleSession, session.id).bottles_delivered == 0
    assert TryoutBottleLedger.query.count() == 0


def test_a_mixed_handoff_tallies_the_bottles_and_the_units(client, app, db, driver, big, small):
    """1 x 18.9 L returnable and 3 x 10 L: 1 bottle on the trip, 3 on the 10 L line, one ledger row for the bottle."""
    session = trip(db, driver, bottles=5, units={small.id: 12})
    task_id = _handoff_task_id(db, driver, [(big, 1), (small, 3)])

    with app.app_context():
        headers = staff_headers(driver)
    response = client.post(HANDOFF.format(task_id), json={}, headers=headers)

    assert response.status_code == 200, response.get_data(as_text=True)
    db.session.expire_all()
    line = DriverBottleSessionUnit.query.filter_by(session_id=session.id, product_id=small.id).one()
    assert line.delivered == 3
    assert db.session.get(DriverBottleSession, session.id).bottles_delivered == 1
    assert [e.units for e in TryoutBottleLedger.query.all()] == [Decimal("1.00")]
