"""Business-rule unit tests for InventoryService reservation behavior."""

from datetime import datetime, timezone
import fnmatch

import pytest

from business_app.services.inventory_service import InventoryService


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.expiry = {}

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.expiry[key] = ttl
        return True

    def hset(self, key, mapping):
        self.hashes[key] = dict(mapping)
        return True

    def expire(self, key, ttl):
        self.expiry[key] = ttl
        return True

    def keys(self, pattern):
        all_keys = list(self.values.keys()) + list(self.hashes.keys())
        return [key for key in all_keys if fnmatch.fnmatch(key, pattern)]

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                deleted += 1
            if key in self.hashes:
                del self.hashes[key]
                deleted += 1
            if key in self.expiry:
                del self.expiry[key]
        return deleted

    def get(self, key):
        return self.values.get(key)


@pytest.fixture
def inventory_service(app):
    with app.app_context():
        return InventoryService()


@pytest.mark.unit
@pytest.mark.inventory
class TestInventoryServiceBusinessRules:
    def test_check_multiple_products_availability_aggregates_same_product_quantities(
        self,
        inventory_service,
        sample_product,
    ):
        result = inventory_service.check_multiple_products_availability(
            items=[
                {"product_id": sample_product.id, "quantity": 2},
                {"product_id": sample_product.id, "quantity": 3},
            ]
        )

        assert len(result) == 1
        assert result[0].requested_quantity == 5
        assert result[0].is_available is True

    def test_check_product_availability_blocks_min_stock_breach(
        self,
        inventory_service,
        sample_product,
        db,
    ):
        sample_product.stock_quantity = 20
        sample_product.min_stock_level = 10
        db.session.commit()

        result = inventory_service.check_product_availability(sample_product.id, requested_quantity=15)

        assert result.is_available is False
        assert "minimum stock level" in result.reason

    def test_check_product_availability_reads_reservations_after_lazy_redis_init(
        self,
        inventory_service,
        sample_product,
        db,
        monkeypatch,
    ):
        fake_redis = FakeRedis()
        reservation_key = f"inventory_reservation:999:{sample_product.id}"
        fake_redis.values[reservation_key] = "3"
        inventory_service.redis_client = None
        monkeypatch.setattr(inventory_service, "_get_redis_client", lambda: fake_redis)

        sample_product.stock_quantity = 7
        db.session.commit()

        result = inventory_service.check_product_availability(sample_product.id, requested_quantity=5)

        assert result.available_quantity == 4
        assert result.reserved_quantity == 3
        assert result.is_available is False

    def test_reserve_inventory_success_writes_reservation_keys(self, inventory_service, sample_product, monkeypatch):
        fake_redis = FakeRedis()
        monkeypatch.setattr(inventory_service, "_get_redis_client", lambda: fake_redis)

        response = inventory_service.reserve_inventory(
            order_id=101,
            items=[{"product_id": sample_product.id, "quantity": 2}],
            user_id=1,
            ttl=600,
        )

        assert response["success"] is True
        assert len(response["reservations"]) == 1
        reservation_key = f"inventory_reservation:101:{sample_product.id}"
        details_key = f"reservation_details:101:{sample_product.id}"
        assert fake_redis.values[reservation_key] == "2"
        assert fake_redis.hashes[details_key]["order_id"] == 101

    def test_reserve_inventory_returns_failure_for_unavailable_items(self, inventory_service, sample_product, monkeypatch):
        unavailable = inventory_service.check_product_availability(
            sample_product.id,
            requested_quantity=sample_product.stock_quantity + 10,
        )
        monkeypatch.setattr(inventory_service, "check_multiple_products_availability", lambda *_a, **_k: [unavailable])

        response = inventory_service.reserve_inventory(
            order_id=202,
            items=[{"product_id": sample_product.id, "quantity": sample_product.stock_quantity + 10}],
            user_id=1,
            ttl=600,
        )

        assert response["success"] is False
        assert response["reason"] == "Insufficient inventory"
        assert "Product" in response["details"][0]

    def test_reserve_inventory_rolls_back_when_redis_write_fails(self, inventory_service, sample_product, monkeypatch):
        class BrokenRedis(FakeRedis):
            def setex(self, key, ttl, value):
                raise RuntimeError("redis down")

        broken_redis = BrokenRedis()
        monkeypatch.setattr(inventory_service, "_get_redis_client", lambda: broken_redis)

        release_calls = []
        monkeypatch.setattr(
            inventory_service,
            "release_reservations",
            lambda order_id: release_calls.append(order_id) or {"success": True},
        )

        response = inventory_service.reserve_inventory(
            order_id=303,
            items=[{"product_id": sample_product.id, "quantity": 1}],
            user_id=1,
        )

        assert response["success"] is False
        assert "Reservation failed" in response["reason"]
        assert release_calls == [303]

    def test_release_reservations_returns_failure_without_redis(self, inventory_service):
        inventory_service.redis_client = None
        result = inventory_service.release_reservations(order_id=404)
        assert result == {"success": False, "reason": "Reservation system not available"}

    def test_release_reservations_deletes_matching_keys(self, inventory_service):
        fake_redis = FakeRedis()
        fake_redis.values["inventory_reservation:505:1"] = "3"
        fake_redis.values["inventory_reservation:505:2"] = "5"
        fake_redis.hashes["reservation_details:505:1"] = {"product_id": 1}
        fake_redis.hashes["reservation_details:505:2"] = {"product_id": 2}
        inventory_service.redis_client = fake_redis

        result = inventory_service.release_reservations(order_id=505)

        assert result["success"] is True
        assert result["released_items"] == 2
        assert fake_redis.keys("inventory_reservation:505:*") == []
        assert fake_redis.keys("reservation_details:505:*") == []
