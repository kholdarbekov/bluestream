"""Spec §5.1: DeliveryPerson carries the uncertainty radius of its stored fix,
so a coarse reading can be refused instead of silently re-sorting the route."""

import pytest

from business_app.models.delivery import DeliveryPerson


@pytest.mark.unit
def test_delivery_person_has_location_accuracy_column():
    column = DeliveryPerson.__table__.columns.get("location_accuracy_m")
    assert column is not None, "DeliveryPerson.location_accuracy_m is missing"
    assert column.nullable is True, (
        "must be nullable — not every Telegram client reports horizontal_accuracy"
    )
