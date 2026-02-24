"""Business-rule unit tests for DeliveryService constraints and helpers."""

from datetime import UTC, datetime, timedelta

import pytest

from business_app.models.delivery import Delivery
from business_app.models.user import UserAddress
from business_app.services.delivery_service import DeliveryService
from business_app.utils.constants import DeliveryStatus, DeliveryType
from business_app.utils.exceptions import DeliveryError, ValidationError


@pytest.fixture
def delivery_service(app):
    with app.app_context():
        return DeliveryService()


@pytest.fixture
def order_with_address(db, sample_order, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Yunusobod, Tashkent",
        street_address="Yunusobod street 1",
        latitude=41.3111,
        longitude=69.2797,
        is_default=True,
    )
    db.session.add(address)
    db.session.flush()
    sample_order.delivery_address_id = address.id
    sample_order.delivery_date = datetime.now(UTC) + timedelta(days=1)
    db.session.commit()
    return sample_order


@pytest.mark.unit
@pytest.mark.delivery
class TestDeliveryServiceBusinessRules:
    def test_create_delivery_rejects_when_delivery_already_exists(self, delivery_service, order_with_address, db):
        existing = Delivery(
            order_id=order_with_address.id,
            status=DeliveryStatus.SCHEDULED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(existing)
        db.session.commit()

        with pytest.raises(ValidationError, match="Delivery already exists"):
            delivery_service.create_delivery(order_with_address.id)

    def test_create_delivery_rejects_out_of_range_address(self, delivery_service, order_with_address, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.delivery_service.calculate_distance",
            lambda *_args, **_kwargs: delivery_service.max_delivery_distance + 1,
        )

        with pytest.raises(DeliveryError, match="outside our delivery range"):
            delivery_service.create_delivery(order_with_address.id)

    def test_create_delivery_succeeds_for_in_range_address(self, delivery_service, order_with_address, monkeypatch, db):
        monkeypatch.setattr("business_app.services.delivery_service.calculate_distance", lambda *_a, **_k: 5.25)
        monkeypatch.setattr(delivery_service, "_schedule_delivery_assignment", lambda *_a, **_k: None)

        delivery = delivery_service.create_delivery(order_with_address.id, delivery_type=DeliveryType.STANDARD)
        db.session.refresh(delivery)

        assert delivery.order_id == order_with_address.id
        assert delivery.status == DeliveryStatus.SCHEDULED
        assert delivery.distance_km == pytest.approx(5.25, rel=1e-3)

    def test_get_available_time_slots_filters_by_capacity(self, delivery_service, monkeypatch):
        monkeypatch.setattr(
            "business_app.services.delivery_service.get_time_slots",
            lambda: ["09:00-12:00", "14:00-17:00", "18:00-21:00"],
        )
        monkeypatch.setattr(
            delivery_service,
            "_check_slot_capacity",
            lambda _date, slot: slot != "14:00-17:00",
        )

        slots = delivery_service.get_available_time_slots(datetime.now(UTC).date(), DeliveryType.STANDARD)

        assert slots == ["09:00-12:00", "18:00-21:00"]

    def test_get_delivery_zones_returns_normalized_structure(self, delivery_service):
        zones = delivery_service.get_delivery_zones()
        assert zones
        assert all("name" in zone for zone in zones)
        assert all("max_distance_km" in zone for zone in zones)
        assert all("fee" in zone for zone in zones)
        assert all("estimated_time_minutes" in zone for zone in zones)

    def test_update_delivery_status_rejects_invalid_transition(self, delivery_service, order_with_address, db):
        delivery = Delivery(
            order_id=order_with_address.id,
            status=DeliveryStatus.DELIVERED,
            scheduled_date=datetime.now(UTC),
            scheduled_time_slot="09:00-12:00",
        )
        db.session.add(delivery)
        db.session.commit()

        with pytest.raises(ValidationError, match="Cannot change status"):
            delivery_service.update_delivery_status(delivery.id, DeliveryStatus.ASSIGNED)

    def test_calculate_delivery_fee_current_policy_is_free(self, delivery_service):
        assert delivery_service.calculate_delivery_fee(41.30, 69.24, order_total=10000) == 0
        assert delivery_service.calculate_delivery_fee(41.30, 69.24, order_total=100000) == 0
