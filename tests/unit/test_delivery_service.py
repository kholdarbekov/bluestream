"""
Unit tests for DeliveryService aligned with current implementation.
"""

from datetime import datetime, UTC

import pytest

from business_app.services.delivery_service import DeliveryService
from business_app.utils.constants import DeliveryType
from business_app.utils.exceptions import NotFoundError
from shared.constants import TASHKENT_COORDINATES


@pytest.fixture
def delivery_service(app):
    with app.app_context():
        return DeliveryService()


@pytest.mark.unit
@pytest.mark.delivery
class TestDeliveryService:
    def test_calculate_delivery_fee_free_over_threshold(self, delivery_service):
        fee = delivery_service.calculate_delivery_fee(41.2995, 69.2401, 60000)
        assert fee == 0

    def test_get_available_time_slots_returns_list(self, delivery_service, monkeypatch):
        monkeypatch.setattr(delivery_service, "_check_slot_capacity", lambda *_: True)

        slots = delivery_service.get_available_time_slots(datetime.now(UTC).date(), DeliveryType.STANDARD)

        assert isinstance(slots, list)
        assert len(slots) > 0

    def test_create_delivery_raises_when_order_missing(self, delivery_service, db):
        with pytest.raises(NotFoundError, match="Order not found"):
            delivery_service.create_delivery(order_id=999999)

    def test_store_coordinates_do_not_track_warehouse_config(self, app, monkeypatch):
        """The warehouse (route optimization's last-resort start anchor,
        WAREHOUSE_LATITUDE/LONGITUDE) sits at the southern edge of
        TASHKENT_POLYGON coverage — ~48.8km from the farthest coverage
        vertex, against DELIVERY_RADIUS_KM=50 (~1.2km of margin). Centring
        the delivery-fee/range circle there would put northern-Tashkent
        addresses on the edge of rejection. The store/delivery-range origin
        must stay independent of the warehouse config, even though the two
        happen to share a default value today."""
        with app.app_context():
            monkeypatch.setitem(app.config, "WAREHOUSE_LATITUDE", 41.0111)
            monkeypatch.setitem(app.config, "WAREHOUSE_LONGITUDE", 69.5222)

            service = DeliveryService()

            assert service.store_latitude == TASHKENT_COORDINATES["latitude"]
            assert service.store_longitude == TASHKENT_COORDINATES["longitude"]
