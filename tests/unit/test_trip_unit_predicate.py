"""The one-way trip unit rule: the Python predicate, its SQL twin, and a trip line's own figures."""

import pytest

from business_app.models.bottle import DriverBottleSessionUnit
from business_app.models.product import Product


@pytest.mark.parametrize(
    "tracks,per_unit,counted,expected",
    [
        (False, 0, True, True),
        (True, 1, True, False),
        (True, 0, True, True),
        (False, 1, True, True),
        (False, 0, False, False),
    ],
)
def test_the_predicate_and_its_sql_twin_agree(db, sample_category, tracks, per_unit, counted, expected):
    p = Product(
        name="P",
        category_id=sample_category.id,
        size="10L",  # size is NOT NULL
        base_price=1000,
        tracks_returnable_bottles=tracks,
        returnable_bottles_per_unit=per_unit,
        counted_on_bottle_trip=counted,
    )
    db.session.add(p)
    db.session.commit()
    assert p.is_trip_one_way_unit is expected
    assert (Product.query.filter(Product.id == p.id, Product.trip_one_way_unit_clause()).count() == 1) is expected


def test_a_new_product_is_not_counted_on_the_trip_by_default(db, sample_category):
    p = Product(name="D", category_id=sample_category.id, size="5L", base_price=1000)
    db.session.add(p)
    db.session.commit()
    assert p.counted_on_bottle_trip is False
    assert p.is_trip_one_way_unit is False


def test_a_line_publishes_on_board_transferable_and_its_own_discrepancy():
    line = DriverBottleSessionUnit(loaded=12, transferred_in=3, delivered=10, transferred_out=6, returned=0)
    assert (line.on_board, line.transferable) == (-1, 0)
    line.compute_discrepancy()
    assert line.discrepancy == -1


def test_a_line_with_units_left_is_transferable_and_a_return_lowers_the_discrepancy():
    line = DriverBottleSessionUnit(loaded=12, transferred_in=0, delivered=5, transferred_out=0, returned=3)
    assert (line.on_board, line.transferable) == (7, 7)
    line.compute_discrepancy()
    assert line.discrepancy == 4
