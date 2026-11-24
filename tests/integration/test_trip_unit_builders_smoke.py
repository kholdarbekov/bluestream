"""The trip-unit test builders themselves run: every driver fixture and every builder."""

import pytest

from business_app.models.delivery import DeliveryPerson
from shared.enums import DeliveryStatus
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    bind,
    big,
    co_driver,
    delivery_for,
    driver,
    member,
    order_with_lines,
    owner,
    receiver,
    sender,
    small,
    trip,
    trip_customer,
)

pytestmark = pytest.mark.integration


def test_driver_fixtures_name_the_delivery_person(db, driver, owner, co_driver, sender, receiver):
    names = {"Dilshod": driver, "Olim": owner, "Kamol": co_driver, "Aziz": sender, "Bobur": receiver}
    for first_name, user in names.items():
        profile = db.session.query(DeliveryPerson).filter_by(user_id=user.id).one()
        assert profile.full_name == first_name
        assert user.first_name == first_name


def test_trip_bind_member_and_delivery_builders_chain(db, driver, co_driver, big, small):
    customer = trip_customer(db)
    order = order_with_lines(db, customer, [(big, 2), (small, 1)])
    session = trip(db, driver, bottles=2, units={small.id: 1})
    link = bind(db, session, order, driver)
    membership = member(db, session, co_driver)
    delivery = delivery_for(db, order, driver, DeliveryStatus.ASSIGNED)

    assert link.session_id == session.id and link.order_id == order.id
    assert membership.member_driver_id == co_driver.id
    assert delivery.order_id == order.id and delivery.delivery_person_id == driver.id
