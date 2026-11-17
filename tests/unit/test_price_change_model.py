"""The price-change model: the dates it derives and the vocabularies it publishes (spec 2026-11-16 section 5.1)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from business_app.models.order import OrderItem
from business_app.models.price_change import (
    COURTESY_SOURCES,
    PRICE_CHANGE_NOTICE_KINDS,
    PRICE_CHANGE_STATUSES,
    CourtesySource,
    PriceChange,
    PriceChangeNoticeKind,
    PriceChangeStatus,
)


def test_the_spec_example_timeline():
    """THE one literal pin of the dates; every other test compares with the properties."""
    pc = PriceChange(announce_on=date(2027, 1, 4), change_on=date(2027, 2, 1), reminder_lead_days=7, courtesy_days=14)
    assert pc.effective_at == datetime(2027, 1, 31, 19, 0, tzinfo=UTC)
    assert pc.reminder_on == date(2027, 1, 25)
    assert pc.courtesy_last_day == date(2027, 2, 14)
    assert pc.courtesy_ends_at == datetime(2027, 2, 14, 19, 0, tzinfo=UTC)
    assert pc.price_valid_until == date(2027, 1, 31)


def test_vocabularies_are_the_enums():
    assert PRICE_CHANGE_STATUSES == tuple(member.value for member in PriceChangeStatus)
    assert COURTESY_SOURCES == tuple(member.value for member in CourtesySource)
    assert PRICE_CHANGE_NOTICE_KINDS == tuple(member.value for member in PriceChangeNoticeKind)


def test_order_item_publishes_its_courtesy():
    item = OrderItem(
        quantity=4,
        unit_price=Decimal("20000.00"),
        total_price=Decimal("76000.00"),
        courtesy_quantity=2,
        courtesy_unit_price=Decimal("18000.00"),
    )
    published = item.to_dict()
    assert published["courtesy_quantity"] == 2
    assert published["courtesy_unit_price"] == Decimal("18000.00")
