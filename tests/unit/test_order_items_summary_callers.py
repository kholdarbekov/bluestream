"""The callers of the items-summary rule must all read it from one place.

These two surfaces used to each carry their own copy of "which items does a
compact summary show": the admin order serializer truncated at 5, the delivery
row builder at 3. Same order, two different "+N more" counts, depending on
which admin screen you happened to open.

The point of these tests is not that 3 is a better number than 5 — it is that
there is exactly one number, and that both callers move when it moves.
"""

from decimal import Decimal

from business_app.models.order import OrderItem
from business_app.models.product import Product
from business_app.serializers.admin_serializers import serialize_order_admin
from business_app.serializers.order_serializers import ORDER_ITEMS_SUMMARY_LIMIT
from business_app.services.admin_delivery_service import AdminDeliveryService


def add_item(db, order, product, *, quantity=1, unit_price=Decimal("15000.00")):
    item = OrderItem(
        order_id=order.id,
        product_id=product.id,
        quantity=quantity,
        unit_price=unit_price,
        total_price=unit_price * quantity,
    )
    db.session.add(item)
    db.session.flush()
    return item


def make_product(db, sample_category, name):
    product = Product(
        name=name,
        category_id=sample_category.id,
        size="19L",
        base_price=Decimal("15000.00"),
        is_active=True,
    )
    db.session.add(product)
    db.session.flush()
    return product


def stock_order(db, order, sample_category, sample_product, count):
    add_item(db, order, sample_product, quantity=1)
    for n in range(2, count + 1):
        add_item(db, order, make_product(db, sample_category, f"Product {n}"), quantity=n)
    db.session.commit()


class TestAdminOrderSerializer:
    def test_truncates_at_the_shared_limit(self, db, sample_order, sample_category, sample_product):
        stock_order(db, sample_order, sample_category, sample_product, count=6)

        data = serialize_order_admin(sample_order)

        assert len(data["items_summary"]) == ORDER_ITEMS_SUMMARY_LIMIT

    def test_still_reports_the_true_total(self, db, sample_order, sample_category, sample_product):
        # The truncation is a display budget, not a count. An admin looking at
        # a 6-line order must not be told it has 3 lines.
        stock_order(db, sample_order, sample_category, sample_product, count=6)

        data = serialize_order_admin(sample_order)

        assert data["item_count"] == 6

    def test_keeps_the_money_fields_the_orders_page_renders(self, db, sample_order, sample_product):
        add_item(db, sample_order, sample_product, quantity=2)
        db.session.commit()

        item = serialize_order_admin(sample_order)["items_summary"][0]

        assert item["unit_price"] == 15000.0
        assert item["total_price"] == 30000.0
        assert item["product_name"] == "Pure Water 19L"
        assert item["quantity"] == 2
        assert item["is_reward"] is False

    def test_empty_order_serializes_to_an_empty_summary(self, db, sample_order):
        data = serialize_order_admin(sample_order)

        assert data["items_summary"] == []
        assert data["item_count"] == 0


class TestDeliveryRowSummary:
    def test_truncates_at_the_shared_limit(self, db, sample_order, sample_category, sample_product):
        stock_order(db, sample_order, sample_category, sample_product, count=6)

        rendered = AdminDeliveryService._build_items_summary(sample_order)

        assert rendered.endswith(f" +{6 - ORDER_ITEMS_SUMMARY_LIMIT} more")
        assert rendered.count(" x") == ORDER_ITEMS_SUMMARY_LIMIT

    def test_renders_the_same_compact_string_as_before(self, db, sample_order, sample_product):
        add_item(db, sample_order, sample_product, quantity=2)
        db.session.commit()

        assert AdminDeliveryService._build_items_summary(sample_order) == "Pure Water 19L x2"

    def test_missing_order_renders_empty(self):
        assert AdminDeliveryService._build_items_summary(None) == ""
