"""
Unit tests for InventoryService aligned with current implementation.
"""

from fnmatch import fnmatch

import pytest

from business_app.services.inventory_service import InventoryService, InventoryOperationType
from business_app.utils.exceptions import NotFoundError


class _FakeRedis:
    """The five operations a reservation round-trip uses, in memory.

    The unit suite cannot assume a live redis, and a MISSING client makes
    `_get_reserved_quantity` answer 0 — which would make the test below pass with or
    without the fix. So the hold has to be really written and really read back.
    """

    def __init__(self):
        self.values = {}
        self.hashes = {}

    def setex(self, key, ttl, value):
        self.values[key] = str(value)

    def get(self, key):
        return self.values.get(key)

    def keys(self, pattern):
        return [key for key in self.values if fnmatch(key, pattern)]

    def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def expire(self, key, ttl):
        return True


@pytest.fixture
def inventory_service(app):
    with app.app_context():
        return InventoryService()


@pytest.mark.unit
@pytest.mark.inventory
class TestInventoryService:
    def test_check_product_availability_success(self, inventory_service, sample_product):
        result = inventory_service.check_product_availability(sample_product.id, 1)

        assert result.is_available is True
        assert result.product_id == sample_product.id

    def test_check_product_availability_missing_product(self, inventory_service, db):
        with pytest.raises(NotFoundError):
            inventory_service.check_product_availability(999999, 1)

    def test_reserve_inventory_returns_failure_when_unavailable(self, inventory_service, sample_product, monkeypatch):
        unavailable = inventory_service.check_product_availability(sample_product.id, sample_product.stock_quantity + 1)
        monkeypatch.setattr(inventory_service, "check_multiple_products_availability", lambda *_args, **_kwargs: [unavailable])

        result = inventory_service.reserve_inventory(order_id=1, items=[{"product_id": sample_product.id, "quantity": 9999}])

        assert result["success"] is False
        assert "Insufficient inventory" in result["reason"]

    def test_a_re_reservation_does_not_count_the_orders_own_hold_against_it(
        self, inventory_service, sample_product, db
    ):
        """M28: re-stamping an order's hold must not read that hold as rival demand.

        The agent-order confirmation window re-reserves the SAME order's items for
        `SALES_CONFIRMATION_TTL_HOURS`
        (`AgentOrderConfirmationService._hold_stock_for_the_question`), and the availability
        read named no order — so the 30-minute hold this order already owns was scored as
        somebody else's demand and the extension was refused, silently, at exactly the tight
        stock the extension exists for. Free stock here is below TWICE the line (3 on the
        shelf, 2 per hold), which is the entire window in which the two readings differ.
        """
        sample_product.stock_quantity = 3
        sample_product.min_stock_level = 0
        db.session.commit()
        inventory_service.redis_client = _FakeRedis()
        items = [{"product_id": sample_product.id, "quantity": 2}]

        first = inventory_service.reserve_inventory(order_id=4242, items=items, ttl=1800)
        assert first["success"] is True

        again = inventory_service.reserve_inventory(order_id=4242, items=items, ttl=3 * 3600)

        assert again["success"] is True, again
        # ...and it is the order's OWN hold that is excluded, not a hole in the check: a
        # DIFFERENT order asking for the same two units is still refused.
        other = inventory_service.reserve_inventory(order_id=4243, items=items, ttl=1800)
        assert other == {
            "success": False,
            "reason": "Insufficient inventory",
            "details": [f"Product {sample_product.id}: Insufficient stock"],
        }

    def test_adjust_inventory_success(self, inventory_service, sample_product, db):
        old_stock = sample_product.stock_quantity

        result = inventory_service.adjust_inventory(
            product_id=sample_product.id,
            quantity_change=5,
            operation_type=InventoryOperationType.MANUAL_ADJUSTMENT,
            reason="test adjustment",
            user_id=None,
        )

        db.session.refresh(sample_product)
        assert result["success"] is True
        assert sample_product.stock_quantity == old_stock + 5

    def test_get_inventory_status(self, inventory_service, sample_product):
        status = inventory_service.get_inventory_status(sample_product.id)

        assert status["product_id"] == sample_product.id
        assert "available_quantity" in status
        assert "is_in_stock" in status
