"""One rule for "what does a compact order-items summary show".

Before this module the rule was expressed independently in several places and
they disagreed on the only number that matters — how many lines a summary
shows before it collapses the rest into "+N more" (3 here, 5 there, unlimited
elsewhere). A dispatcher reading a stop card and an admin reading the same
order on the Orders page were being shown different truncations of the same
list, and neither number was written down anywhere as the intended one.

These tests pin the single rule and the shapes derived from it, so a future
caller that wants a summary reaches for the SSOT instead of writing an eighth
copy of the truncation.
"""

from decimal import Decimal

import pytest

from business_app.models.order import OrderItem
from business_app.models.product import Product
from business_app.serializers.order_serializers import (
    ORDER_ITEMS_SUMMARY_LIMIT,
    format_order_items_summary,
    summarize_order_items,
)


def add_item(db, order, product, *, quantity, unit_price=Decimal("15000.00")):
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


class TestTruncationRule:
    def test_shows_every_item_when_under_the_limit(self, db, sample_order, sample_product):
        add_item(db, sample_order, sample_product, quantity=2)
        db.session.commit()

        summary = summarize_order_items(sample_order)

        assert [i["product_name"] for i in summary["items"]] == ["Pure Water 19L"]
        assert summary["items"][0]["quantity"] == 2
        assert summary["total_count"] == 1
        assert summary["hidden_count"] == 0

    def test_collapses_the_tail_past_the_limit(self, db, sample_order, sample_category, sample_product):
        add_item(db, sample_order, sample_product, quantity=1)
        for n in range(2, 7):
            add_item(db, sample_order, make_product(db, sample_category, f"Product {n}"), quantity=n)
        db.session.commit()

        summary = summarize_order_items(sample_order)

        assert len(summary["items"]) == ORDER_ITEMS_SUMMARY_LIMIT
        assert summary["total_count"] == 6
        assert summary["hidden_count"] == 6 - ORDER_ITEMS_SUMMARY_LIMIT

    def test_limit_is_three(self):
        # The number the divergent copies disagreed on. Pinned so that changing
        # it is a deliberate edit to this assertion, not a silent drift in one
        # caller.
        assert ORDER_ITEMS_SUMMARY_LIMIT == 3

    def test_empty_order_summarizes_to_nothing(self, db, sample_order):
        summary = summarize_order_items(sample_order)

        assert summary == {"items": [], "total_count": 0, "hidden_count": 0}

    def test_missing_order_summarizes_to_nothing(self):
        # Dispatch stops can carry a null order (a delivery whose order row is
        # gone); the summary must degrade rather than raise inside a read model.
        assert summarize_order_items(None) == {"items": [], "total_count": 0, "hidden_count": 0}


class TestItemShape:
    def test_flags_a_free_reward_line(self, db, sample_order, sample_category):
        add_item(db, sample_order, make_product(db, sample_category, "Free Cup"),
                 quantity=1, unit_price=Decimal("0.00"))
        db.session.commit()

        summary = summarize_order_items(sample_order)

        assert summary["items"][0]["is_reward"] is True

    def test_falls_back_when_the_product_row_is_gone(self, db, sample_order, sample_product):
        item = add_item(db, sample_order, sample_product, quantity=1)
        db.session.commit()
        product_id = sample_product.id
        # Delete the product row itself rather than assigning `item.product =
        # None`, which the ORM would translate into nulling the NOT NULL
        # `product_id` column — i.e. a test that fails on its own setup instead
        # of on the behaviour. `Product` has no reverse relationship to
        # `OrderItem` (see models/order.py:219), so this leaves the item's
        # `product_id` intact and its `product` unresolvable: exactly the
        # dangling-reference shape the fallback exists for.
        db.session.delete(sample_product)
        db.session.commit()
        db.session.expire(item)

        summary = summarize_order_items(sample_order)

        assert summary["items"][0]["product_name"] == f"Product #{product_id}"
        assert summary["items"][0]["product_id"] == product_id

    def test_omits_prices_by_default(self, db, sample_order, sample_product):
        add_item(db, sample_order, sample_product, quantity=2)
        db.session.commit()

        item = summarize_order_items(sample_order)["items"][0]

        assert "unit_price" not in item
        assert "total_price" not in item

    def test_includes_prices_on_request(self, db, sample_order, sample_product):
        add_item(db, sample_order, sample_product, quantity=2)
        db.session.commit()

        item = summarize_order_items(sample_order, with_prices=True)["items"][0]

        assert item["unit_price"] == 15000.0
        assert item["total_price"] == 30000.0


class TestStringForm:
    def test_renders_the_compact_string_used_by_delivery_rows(self, db, sample_order, sample_product):
        add_item(db, sample_order, sample_product, quantity=2)
        db.session.commit()

        assert format_order_items_summary(sample_order) == "Pure Water 19L x2"

    def test_appends_the_hidden_count(self, db, sample_order, sample_category, sample_product):
        add_item(db, sample_order, sample_product, quantity=1)
        for n in range(2, 6):
            add_item(db, sample_order, make_product(db, sample_category, f"Product {n}"), quantity=1)
        db.session.commit()

        rendered = format_order_items_summary(sample_order)

        assert rendered.endswith(f" +{5 - ORDER_ITEMS_SUMMARY_LIMIT} more")

    @pytest.mark.parametrize("order", [None, "empty"])
    def test_renders_empty_string_when_there_is_nothing_to_show(self, db, sample_order, order):
        subject = None if order is None else sample_order

        assert format_order_items_summary(subject) == ""
