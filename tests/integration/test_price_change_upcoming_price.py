"""The coming-price and direct-edit-lock answers (price-change transition spec §5.2, area A8).

Changes are created through `PriceChangeService().create(...)`; lifecycle columns are stamped directly, the way the
announcement and switch tasks will write them.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from business_app.models.price_change import PriceChangeStatus
from business_app.models.product import Product
from business_app.services.price_change.schedule import PriceChangeService, UpcomingPrice, price_fields_changed
from business_app.utils.local_windows import local_date
from business_app.utils.timezone_utils import get_utc_now
from tests.integration.reorder_reminder_builders import make_customer
from shared.enums import UserRole, UserType

pytestmark = pytest.mark.integration


def _make(db, product):
    admin = make_customer(db, first_name="Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)
    today = local_date()
    return PriceChangeService().create(
        {
            "announce_on": today + timedelta(days=7),
            "change_on": today + timedelta(days=35),
            "items": [{"product_id": product.id, "new_price": 20000, "courtesy_cap": 2}],
        },
        admin.id,
    )


def _other_product(db, product):
    other = Product(
        name="Other 5L", category_id=product.category_id, size="5L", volume=5.0, volume_unit="L",
        base_price=Decimal("5000.00"), stock_quantity=10, is_active=True,
    )
    db.session.add(other)
    db.session.commit()
    return other


def _announce(db, pc):
    pc.status = PriceChangeStatus.ANNOUNCED
    pc.announcement_started_at = get_utc_now()
    db.session.commit()


def _applied(db, pc):
    now = get_utc_now()
    pc.status, pc.announcement_started_at, pc.applied_at = PriceChangeStatus.APPLIED, now, now
    db.session.commit()


def _cancelled(db, pc):
    pc.status, pc.cancelled_at = PriceChangeStatus.CANCELLED, get_utc_now()
    db.session.commit()


def test_no_coming_price_before_the_announcement_starts(db, sample_product):
    _make(db, sample_product)
    assert PriceChangeService().upcoming_price(sample_product.id) is None


def test_the_coming_price_once_announced(db, sample_product):
    pc = _make(db, sample_product)
    _announce(db, pc)
    assert PriceChangeService().upcoming_price(sample_product.id) == UpcomingPrice(
        sample_product.id, Decimal("20000.00"), pc.change_on, pc.price_valid_until
    )


def test_no_coming_price_for_a_product_outside_the_change(db, sample_product):
    pc = _make(db, sample_product)
    _announce(db, pc)
    assert PriceChangeService().upcoming_price(_other_product(db, sample_product).id) is None


@pytest.mark.parametrize("finish", [_applied, _cancelled], ids=["applied", "cancelled"])
def test_no_coming_price_once_applied_or_cancelled(db, sample_product, finish):
    pc = _make(db, sample_product)
    _announce(db, pc)
    finish(db, pc)
    assert PriceChangeService().upcoming_price(sample_product.id) is None


def test_the_direct_edit_lock_follows_the_pending_change(db, sample_product):
    service = PriceChangeService()
    pc = _make(db, sample_product)
    other = _other_product(db, sample_product)

    assert service.blocks_direct_price_edit(sample_product.id) is True
    assert service.pending_for_product(sample_product.id).id == pc.id
    assert service.blocks_direct_price_edit(other.id) is False
    assert service.pending_for_product(other.id) is None

    _announce(db, pc)
    assert service.blocks_direct_price_edit(sample_product.id) is True
    assert service.pending_for_product(sample_product.id).id == pc.id

    _applied(db, pc)
    assert service.blocks_direct_price_edit(sample_product.id) is False
    assert service.pending_for_product(sample_product.id) is None


def test_a_cancelled_change_does_not_lock_the_price(db, sample_product):
    pc = _make(db, sample_product)
    _cancelled(db, pc)
    assert PriceChangeService().blocks_direct_price_edit(sample_product.id) is False


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, False),
        ({"name": "x"}, False),
        ({"price": 15000}, False),
        ({"price": "15000.00"}, False),
        ({"base_price": 15001}, True),
        ({"price": 20000}, True),
        ({"discount_price": None}, False),
        ({"discount_price": 14000}, True),
        ({"price": None}, True),
        ({"price": "abc"}, True),
    ],
)
def test_price_fields_changed(db, sample_product, payload, expected):
    assert price_fields_changed(sample_product, payload) is expected
