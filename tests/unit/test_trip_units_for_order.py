"""What an order puts on the trip: returnable 18.9 L bottles and counted one-way units, from one rule."""

from decimal import Decimal

from business_app.models.order import OrderItem
from business_app.models.product import Product
from business_app.services.bottle_tracking_service import BottleTrackingService, TripUnits
from tests.integration.trip_unit_builders import nineteen, order_with_lines, ten_litre


def test_a_mixed_order_puts_three_bottles_and_four_ten_litre_on_the_trip(db, sample_user, sample_category):
    big, small = nineteen(db, sample_category), ten_litre(db, sample_category)
    order = order_with_lines(db, sample_user, [(big, 3), (small, 4)])
    u = BottleTrackingService.trip_units_for_order(order)
    assert (u.bottles, u.units, u.any) == (Decimal("3.00"), {small.id: 4}, True)
    assert u.bottles == BottleTrackingService.calculate_bottles_for_order(order)


def test_an_unticked_ten_litre_order_puts_nothing_on_the_trip(db, sample_user, sample_category):
    small = ten_litre(db, sample_category, counted=False)
    order = order_with_lines(db, sample_user, [(small, 4)])
    u = BottleTrackingService.trip_units_for_order(order)
    assert (u.bottles, u.units, u.any) == (Decimal("0.00"), {}, False)


def test_a_reward_line_of_ten_litre_counts(db, sample_user, sample_category):
    small = ten_litre(db, sample_category)
    order = order_with_lines(db, sample_user, [(small, 1)])
    item = OrderItem.query.filter_by(order_id=order.id).one()
    item.is_reward_item = True
    item.unit_price = Decimal("0.00")
    item.total_price = Decimal("0.00")
    db.session.commit()
    assert BottleTrackingService.trip_units_for_order(order).units == {small.id: 1}


def test_deltas_keep_their_sign(db, sample_category):
    small = ten_litre(db, sample_category)
    u = BottleTrackingService.trip_units_for_lines([(small, -2)])
    assert u.units == {small.id: -2}
    assert u.any is True


def test_opposite_deltas_cancel_and_the_zero_entry_is_dropped(db, sample_category):
    small = ten_litre(db, sample_category)
    u = BottleTrackingService.trip_units_for_lines([(small, 3), (small, -3)])
    assert (u.bottles, u.units, u.any) == (Decimal("0.00"), {}, False)


def test_a_line_without_a_product_is_skipped(db, sample_category):
    small = ten_litre(db, sample_category)
    u = BottleTrackingService.trip_units_for_lines([(None, 3), (small, 2)])
    assert u == TripUnits(Decimal("0.00"), {small.id: 2})


def test_a_fractional_bottle_per_unit_product_is_summed_exactly(db, sample_category):
    half = Product(
        name="Half",
        category_id=sample_category.id,
        size="5L",
        base_price=1000,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=0.5,
    )
    db.session.add(half)
    db.session.commit()
    u = BottleTrackingService.trip_units_for_lines([(half, 3)])
    assert (u.bottles, u.units) == (Decimal("1.50"), {})


def test_counted_trip_products_lists_only_ticked_one_way_products_in_id_order(db, sample_category):
    big = nineteen(db, sample_category)
    ten_off = ten_litre(db, sample_category, counted=False)
    ten_a = ten_litre(db, sample_category)
    ten_b = ten_litre(db, sample_category)
    ten_b.is_active = False  # the setting, not the catalogue, decides
    db.session.commit()
    assert big.id and ten_off.id
    assert [p.id for p in BottleTrackingService.counted_trip_products()] == [ten_a.id, ten_b.id]
