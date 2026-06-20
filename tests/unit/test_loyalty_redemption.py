from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTransaction,
    RewardRedemption,
)
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductSizeEnum
from business_app.services.loyalty_service import LoyaltyService
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.exceptions import ValidationError


@pytest.fixture
def program(db):
    p = LoyaltyProgram(name="Default", is_active=True, is_default=True, uzs_per_point=250)
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.fixture
def discount_reward(db, program):
    r = LoyaltyReward(
        program_id=program.id, name="500 off", reward_type="discount", points_cost=100,
        discount_type="fixed", discount_value=Decimal("500.00"), is_active=True,
        max_uses_per_user=1, redemptions_used=0,
    )
    _db.session.add(r)
    _db.session.commit()
    return r


@pytest.mark.unit
def test_reward_redemption_persists_and_serializes(db, sample_user, discount_reward):
    rr = RewardRedemption(
        reward_id=discount_reward.id, user_id=sample_user.id, order_id=None,
        reward_type="discount", points_spent=100, discount_amount=Decimal("500.00"),
        code="RWDTEST01", status="applied",
    )
    _db.session.add(rr)
    _db.session.commit()

    fetched = RewardRedemption.query.filter_by(code="RWDTEST01").first()
    assert fetched is not None
    payload = fetched.to_dict()
    assert payload["reward_id"] == discount_reward.id
    assert payload["points_spent"] == 100
    assert payload["status"] == "applied"
    assert payload["discount_amount"] == 500.0


@pytest.fixture
def service(app):
    with app.app_context():
        return LoyaltyService()


def _account_with_points(db, user_id, program_id, points):
    """A loyalty account backed by a real earn lot (so available points == points)."""
    acc = LoyaltyPoints(user_id=user_id, program_id=program_id, total_earned=points, current_balance=points)
    _db.session.add(acc)
    _db.session.flush()
    lot = LoyaltyTransaction(
        user_id=user_id, transaction_type=LoyaltyTransactionType.EARNED, points=points,
        remaining_points=points, description="seed",
    )
    lot.expires_at = datetime(2999, 1, 1, tzinfo=timezone.utc)
    _db.session.add(lot)
    _db.session.commit()
    return acc


def _order(db, user_id, subtotal):
    from shared.enums import OrderStatus
    # order_number is globally unique; suffix with the current order count so a
    # test that creates several orders for the same user/subtotal never collides.
    seq = Order.query.count()
    o = Order(
        user_id=user_id, order_number=f"ORD-{user_id}-{int(subtotal)}-{seq}", status=OrderStatus.PENDING,
        subtotal=Decimal(str(subtotal)), delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(str(subtotal)),
    )
    _db.session.add(o)
    _db.session.commit()
    return o


def test_apply_fixed_discount_reward(service, db, sample_user, program, discount_reward, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)

    service.apply_reward_to_order(order, discount_reward.id, commit=True)
    db.session.refresh(order)

    assert order.loyalty_discount == Decimal("500.00")
    assert order.total_amount == Decimal("9500.00")
    rr = RewardRedemption.query.filter_by(order_id=order.id).first()
    assert rr.status == "applied" and rr.points_spent == 100 and rr.reward_type == "discount"
    assert service.get_available_points(sample_user.id) == 900
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 1


def test_percentage_discount_capped_at_subtotal(service, db, sample_user, program, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    reward = LoyaltyReward(program_id=program.id, name="10%", reward_type="discount", points_cost=50,
                           discount_type="percentage", discount_value=Decimal("10"), is_active=True,
                           max_uses_per_user=5, redemptions_used=0)
    _db.session.add(reward); _db.session.commit()
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)

    service.apply_reward_to_order(order, reward.id, commit=True)
    db.session.refresh(order)
    assert order.loyalty_discount == Decimal("1000.00")  # 10% of 10000
    assert order.total_amount == Decimal("9000.00")


def test_apply_rejects_insufficient_points(service, db, sample_user, program, discount_reward):
    _account_with_points(db, sample_user.id, program.id, 50)  # reward costs 100
    order = _order(db, sample_user.id, 10000)
    with pytest.raises(ValidationError, match="Insufficient points"):
        service.apply_reward_to_order(order, discount_reward.id, commit=True)


def test_apply_rejects_below_min_order_value(service, db, sample_user, program):
    reward = LoyaltyReward(program_id=program.id, name="big", reward_type="discount", points_cost=10,
                           discount_type="fixed", discount_value=Decimal("100"), is_active=True,
                           min_order_value=Decimal("20000.00"), max_uses_per_user=5, redemptions_used=0)
    _db.session.add(reward); _db.session.commit()
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)  # below 20000
    with pytest.raises(ValidationError):
        service.apply_reward_to_order(order, reward.id, commit=True)


def test_apply_enforces_max_uses_per_user(service, db, sample_user, program, discount_reward, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    _account_with_points(db, sample_user.id, program.id, 1000)
    o1 = _order(db, sample_user.id, 10000)
    service.apply_reward_to_order(o1, discount_reward.id, commit=True)  # max_uses_per_user=1
    o2 = _order(db, sample_user.id, 10000)
    with pytest.raises(ValidationError, match="limit"):
        service.apply_reward_to_order(o2, discount_reward.id, commit=True)


def test_apply_free_product_injects_zero_priced_item(service, db, sample_user, sample_category, program, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    product = Product(name="Free Bottle", base_price=Decimal("8000.00"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add(product); _db.session.commit()
    reward = LoyaltyReward(program_id=program.id, name="free bottle", reward_type="free_product",
                           points_cost=200, free_product_id=product.id, is_active=True,
                           max_uses_per_user=5, redemptions_used=0)
    _db.session.add(reward); _db.session.commit()
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)

    service.apply_reward_to_order(order, reward.id, commit=True)
    db.session.refresh(order)
    free_items = [i for i in order.order_items if i.product_id == product.id and i.unit_price == Decimal("0.00")]
    assert len(free_items) == 1 and free_items[0].quantity == 1
    assert order.total_amount == Decimal("10000.00")  # free item adds 0


def test_apply_rejects_duplicate_reward_on_same_order(service, db, sample_user, program, discount_reward, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)
    service.apply_reward_to_order(order, discount_reward.id, commit=True)
    with pytest.raises(ValidationError, match="already been applied"):
        service.apply_reward_to_order(order, discount_reward.id, commit=True)


def test_cancel_redemption_refunds_points_and_flips_status(service, db, sample_user, program, discount_reward, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)
    service.apply_reward_to_order(order, discount_reward.id, commit=True)
    assert service.get_available_points(sample_user.id) == 900

    service.cancel_redemption_for_order(order.id, commit=True)

    rr = RewardRedemption.query.filter_by(order_id=order.id).first()
    assert rr.status == "cancelled"
    assert service.get_available_points(sample_user.id) == 1000  # refunded
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 0

    acc = LoyaltyPoints.query.filter_by(user_id=sample_user.id).first()
    db.session.refresh(acc)
    assert acc.total_redeemed == 0   # un-redeemed
    assert acc.total_earned == 1000  # NOT inflated by the refund


def test_calculate_total_preserves_reward_discount(db, sample_user):
    from shared.enums import OrderStatus
    o = Order(user_id=sample_user.id, order_number="ORD-CT-1", status=OrderStatus.PENDING,
              subtotal=Decimal("0.00"), delivery_fee=Decimal("0.00"),
              discount_amount=Decimal("0.00"), loyalty_discount=Decimal("500.00"),
              total_amount=Decimal("0.00"))
    o.order_items = [OrderItem(product_id=1, quantity=1, unit_price=Decimal("10000.00"), total_price=Decimal("10000.00"))]
    o.calculate_total()
    assert o.loyalty_discount == Decimal("500.00")
    assert o.total_amount == Decimal("9500.00")


@pytest.mark.unit
def test_reward_to_dict_includes_free_product_quantity(db, program):
    r = LoyaltyReward(
        program_id=program.id, name="2 bottles", reward_type="free_product",
        points_cost=200, free_product_id=1, free_product_quantity=2, is_active=True,
    )
    _db.session.add(r); _db.session.commit()
    assert r.to_dict()["free_product_quantity"] == 2


@pytest.mark.unit
def test_reward_to_dict_defaults_free_product_quantity_to_one(db, program):
    r = LoyaltyReward(
        program_id=program.id, name="default qty", reward_type="free_product",
        points_cost=200, free_product_id=1, is_active=True,
    )
    _db.session.add(r); _db.session.commit()
    _db.session.refresh(r)
    assert r.to_dict()["free_product_quantity"] == 1


def test_apply_free_product_uses_configured_quantity(service, db, sample_user, sample_category, program, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    product = Product(name="Free Bottle", base_price=Decimal("8000.00"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add(product); _db.session.commit()
    reward = LoyaltyReward(program_id=program.id, name="2 bottles", reward_type="free_product",
                           points_cost=200, free_product_id=product.id, free_product_quantity=2,
                           is_active=True, max_uses_per_user=5, redemptions_used=0)
    _db.session.add(reward); _db.session.commit()
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)

    service.apply_reward_to_order(order, reward.id, commit=True)
    db.session.refresh(order)
    free_items = [i for i in order.order_items if i.product_id == product.id and i.unit_price == Decimal("0.00")]
    assert len(free_items) == 1 and free_items[0].quantity == 2


def test_apply_free_product_rejects_inactive_product(service, db, sample_user, sample_category, program):
    product = Product(name="Gone", base_price=Decimal("8000.00"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=False)
    _db.session.add(product); _db.session.commit()
    reward = LoyaltyReward(program_id=program.id, name="bad", reward_type="free_product",
                           points_cost=10, free_product_id=product.id, is_active=True,
                           max_uses_per_user=5, redemptions_used=0)
    _db.session.add(reward); _db.session.commit()
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)
    with pytest.raises(ValidationError, match="not available"):
        service.apply_reward_to_order(order, reward.id, commit=True)


def test_apply_free_product_marks_item_as_reward(service, db, sample_user, sample_category, program, monkeypatch):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    product = Product(name="Free Bottle", base_price=Decimal("8000.00"), category_id=sample_category.id,
                      size=ProductSizeEnum.SIZE_19L, is_active=True)
    _db.session.add(product); _db.session.commit()
    reward = LoyaltyReward(program_id=program.id, name="free bottle", reward_type="free_product",
                           points_cost=200, free_product_id=product.id, is_active=True,
                           max_uses_per_user=5, redemptions_used=0)
    _db.session.add(reward); _db.session.commit()
    _account_with_points(db, sample_user.id, program.id, 1000)
    order = _order(db, sample_user.id, 10000)

    service.apply_reward_to_order(order, reward.id, commit=True)
    db.session.refresh(order)

    free_items = [i for i in order.order_items if i.product_id == product.id]
    assert len(free_items) == 1
    assert free_items[0].is_reward_item is True
