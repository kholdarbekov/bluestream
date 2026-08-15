"""What a dispatcher needs to read off a stop without opening the order.

Two things this pins:

1. The identity fields on `routes[].stops[]`. They were emitted but asserted
   nowhere — only the `orders[]` layer was covered — so a rename or a dropped
   join on the route path would have shipped green. The stop cards render
   exactly these.

2. The order items, on BOTH builders. `pool[]` is a filtered view over the
   same dicts `orders[]` is built from, and route stops are built separately;
   adding items to one and not the other is how the same order ends up
   showing its contents on one panel and not the other.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.models.order import OrderItem
from business_app.models.product import Product
from business_app.models.user import UserAddress
from business_app.serializers.order_serializers import ORDER_ITEMS_SUMMARY_LIMIT
from business_app.services.dispatch_service import DispatchService
from shared.enums import DeliveryStatus, OrderStatus


def make_order(db, sample_user, sample_order, *, lat=41.31, lng=69.25, number="ORD-ITEMS-1"):
    address = UserAddress(
        user_id=sample_user.id,
        full_address="Chilonzor 12",
        city="Tashkent",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(address)
    db.session.flush()
    order = sample_order.__class__(
        user_id=sample_user.id,
        order_number=number,
        total_amount=sample_order.total_amount,
        status=OrderStatus.CONFIRMED,
        payment_method=sample_order.payment_method,
        delivery_address_id=address.id,
        delivery_date=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()
    return order


def add_item(db, order, product, *, quantity):
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * quantity,
        )
    )
    db.session.flush()


def make_product(db, sample_category, name):
    product = Product(
        name=name, category_id=sample_category.id, size="19L",
        base_price=Decimal("15000.00"), is_active=True,
    )
    db.session.add(product)
    db.session.flush()
    return product


def put_on_route(db, order, driver):
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    db.session.add(
        DeliveryRoute(
            name="r",
            delivery_person_id=driver.id,
            route_date=datetime.now(timezone.utc),
            optimized_order=[delivery.id],
            start_location_lat=41.30,
            start_location_lng=69.24,
        )
    )
    db.session.flush()
    return delivery


def pool_delivery(db, order):
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=None,
        status=DeliveryStatus.PENDING,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


class TestRouteStopIdentity:
    def test_stop_carries_the_fields_its_card_renders(
        self, db, sample_user, sample_order, sample_product, delivery_driver
    ):
        order = make_order(db, sample_user, sample_order, number="TG_000381_26")
        put_on_route(db, order, delivery_driver)
        db.session.commit()

        stop = DispatchService.get_snapshot(date.today())["routes"][0]["stops"][0]

        assert stop["order_number"] == "TG_000381_26"
        assert stop["customer_name"] == sample_user.full_name
        assert stop["address_label"] == "Chilonzor 12"


class TestItemsOnRouteStops:
    def test_stop_carries_its_order_items(
        self, db, sample_user, sample_order, sample_product, delivery_driver
    ):
        order = make_order(db, sample_user, sample_order)
        add_item(db, order, sample_product, quantity=2)
        put_on_route(db, order, delivery_driver)
        db.session.commit()

        stop = DispatchService.get_snapshot(date.today())["routes"][0]["stops"][0]

        assert stop["items"] == [
            {
                "product_id": sample_product.id,
                "product_name": "Pure Water 19L",
                "quantity": 2,
                "is_reward": False,
            }
        ]
        assert stop["items_total_count"] == 1
        assert stop["items_hidden_count"] == 0

    def test_stop_truncates_long_orders_at_the_shared_limit(
        self, db, sample_user, sample_order, sample_category, sample_product, delivery_driver
    ):
        order = make_order(db, sample_user, sample_order)
        add_item(db, order, sample_product, quantity=1)
        for n in range(2, 7):
            add_item(db, order, make_product(db, sample_category, f"Product {n}"), quantity=n)
        put_on_route(db, order, delivery_driver)
        db.session.commit()

        stop = DispatchService.get_snapshot(date.today())["routes"][0]["stops"][0]

        assert len(stop["items"]) == ORDER_ITEMS_SUMMARY_LIMIT
        assert stop["items_total_count"] == 6
        assert stop["items_hidden_count"] == 6 - ORDER_ITEMS_SUMMARY_LIMIT

    def test_stop_with_no_items_reports_an_empty_list(
        self, db, sample_user, sample_order, sample_product, delivery_driver
    ):
        # Never `None`: the card iterates this, and a null would make an
        # itemless order render as a broken row instead of a quiet one.
        order = make_order(db, sample_user, sample_order)
        put_on_route(db, order, delivery_driver)
        db.session.commit()

        stop = DispatchService.get_snapshot(date.today())["routes"][0]["stops"][0]

        assert stop["items"] == []
        assert stop["items_total_count"] == 0


class TestItemsOnPoolAndOrders:
    def test_pool_row_carries_its_order_items(self, db, sample_user, sample_order, sample_product):
        order = make_order(db, sample_user, sample_order)
        add_item(db, order, sample_product, quantity=3)
        pool_delivery(db, order)
        db.session.commit()

        snapshot = DispatchService.get_snapshot(date.today())

        assert len(snapshot["pool"]) == 1
        assert snapshot["pool"][0]["items"][0]["quantity"] == 3
        assert snapshot["pool"][0]["items"][0]["product_name"] == "Pure Water 19L"

    def test_order_layer_carries_its_order_items(self, db, sample_user, sample_order, sample_product):
        order = make_order(db, sample_user, sample_order)
        add_item(db, order, sample_product, quantity=4)
        db.session.commit()

        entry = next(
            o for o in DispatchService.get_snapshot(date.today())["orders"]
            if o["order_number"] == "ORD-ITEMS-1"
        )

        assert entry["items"][0]["quantity"] == 4
        assert entry["items_total_count"] == 1


class TestItemsDoNotCostAQueryPerOrder:
    def test_items_are_eager_loaded(
        self, db, sample_user, sample_order, sample_category, sample_product, count_queries
    ):
        # The snapshot polls every 30s. Lazy-loading items and products would
        # add two round-trips PER ORDER on that timer — the exact N+1 the
        # existing budget test exists to prevent, just on a new relationship.
        for i in range(10):
            order = make_order(db, sample_user, sample_order, lat=41.31 + i / 1000, number=f"ORD-N1-{i}")
            add_item(db, order, sample_product, quantity=1)
            add_item(db, order, make_product(db, sample_category, f"Extra {i}"), quantity=2)
        db.session.commit()

        with count_queries() as counter:
            snapshot = DispatchService.get_snapshot(date.today())

        assert all(len(o["items"]) == 2 for o in snapshot["orders"])
        # 3 base queries (orders, drivers, routes) + 2 for the items/products
        # collections. A per-order load would put this at 20+.
        assert counter.count <= 8
