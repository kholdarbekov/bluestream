"""Integration tests for redeeming a loyalty reward at order-creation time.

Exercises OrderService.create_order with a ``reward_id`` in order_data so the
reward (a discount or free product) is applied atomically inside the order
transaction via LoyaltyService.apply_reward_to_order(commit=False).

Heavy cross-system steps that are not under test are isolated: inventory
reservation uses the mock_inventory_service fixture, and the payment-row /
corporate-prepayment steps are patched out. The loyalty path (points
deduction, RewardRedemption row, order total recompute) runs for real.
"""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.loyalty import (
    LoyaltyPoints,
    LoyaltyProgram,
    LoyaltyReward,
    LoyaltyTransaction,
    RewardRedemption,
)
from business_app.models.user import UserAddress
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.order_service import OrderService
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.exceptions import ValidationError


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(name="Default", is_active=True, is_default=True)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def points_account(db, sample_user, loyalty_program):
    """A loyalty account for sample_user backed by a real 1000-pt EARNED lot."""
    account = LoyaltyPoints(
        user_id=sample_user.id,
        program_id=loyalty_program.id,
        total_earned=1000,
        current_balance=1000,
    )
    db.session.add(account)
    db.session.flush()

    lot = LoyaltyTransaction(
        user_id=sample_user.id,
        transaction_type=LoyaltyTransactionType.EARNED,
        points=1000,
        remaining_points=1000,
        description="seed",
    )
    lot.expires_at = datetime(2999, 1, 1, tzinfo=timezone.utc)
    db.session.add(lot)
    db.session.commit()
    return account


@pytest.fixture
def discount_reward(db, loyalty_program):
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="500 off",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("500.00"),
        points_cost=100,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def delivery_address(db, sample_user):
    address = UserAddress(
        user_id=sample_user.id,
        title="Home",
        full_address="Home Street 1",
        street_address="Home Street 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _order_data(product, address, **extra):
    data = {
        "items": [{"product_id": product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": address.id,
            "street": address.street_address,
            "latitude": address.latitude,
            "longitude": address.longitude,
        },
        "payment_method": "click",
    }
    data.update(extra)
    return data


@pytest.mark.integration
@pytest.mark.order
def test_create_order_applies_discount_reward(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
    loyalty_program,
    points_account,
    discount_reward,
    delivery_address,
    monkeypatch,
):
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(
            sample_user.id,
            _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
        )

    db.session.refresh(order)

    assert order.loyalty_discount == Decimal("500.00")
    expected_total = order.subtotal - Decimal("500.00") + order.delivery_fee
    assert order.total_amount == expected_total

    assert (
        RewardRedemption.query.filter_by(order_id=order.id, status="applied").count() == 1
    )
    # 1000 seeded - 100 points_cost
    assert LoyaltyService().get_available_points(sample_user.id) == 900


@pytest.mark.integration
@pytest.mark.order
def test_cancel_order_reverses_reward_redemption(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
    loyalty_program,
    points_account,
    discount_reward,
    delivery_address,
    monkeypatch,
):
    """Cancelling an order that carries an applied reward refunds the spent
    points, flips the redemption to ``cancelled`` and decrements reward usage."""
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}
    mock_inventory_service.release_reservations.return_value = {"success": True}

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(
            sample_user.id,
            _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
        )

    assert RewardRedemption.query.filter_by(order_id=order.id, status="applied").count() == 1
    assert LoyaltyService().get_available_points(sample_user.id) == 900
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 1

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.release_for_order",
        return_value=None,
    ):
        cancelled = service.cancel_order(
            order.id, user_id=sample_user.id, reason="Customer request"
        )

    from shared.enums import OrderStatus

    assert cancelled.status == OrderStatus.CANCELLED
    redemption = RewardRedemption.query.filter_by(order_id=order.id).first()
    assert redemption.status == "cancelled"
    # Spent points refunded back to the full seeded balance.
    assert LoyaltyService().get_available_points(sample_user.id) == 1000
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 0


@pytest.fixture
def free_product(db, sample_category):
    from business_app.models.product import Product, ProductSizeEnum

    p = Product(
        name="Free Bottle",
        base_price=Decimal("8000.00"),
        category_id=sample_category.id,
        size=ProductSizeEnum.SIZE_19L,
        is_active=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def free_product_reward(db, loyalty_program, free_product):
    reward = LoyaltyReward(
        program_id=loyalty_program.id,
        name="2 free bottles",
        reward_type="free_product",
        points_cost=100,
        free_product_id=free_product.id,
        free_product_quantity=2,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.mark.integration
@pytest.mark.order
def test_create_order_free_product_reserves_configured_quantity(
    app,
    db,
    sample_user,
    sample_product,
    sample_category,
    mock_inventory_service,
    loyalty_program,
    points_account,
    free_product,
    free_product_reward,
    delivery_address,
    monkeypatch,
):
    """A free-product reward with quantity=2 must reserve 2 units of the free
    product (not 1), so inventory protection matches what checkout consumes."""
    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        ),
        SimpleNamespace(
            product_id=free_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        ),
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    service = OrderService(inventory_service=mock_inventory_service)
    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        order = service.create_order(
            sample_user.id,
            _order_data(sample_product, delivery_address, reward_id=free_product_reward.id),
        )

    # The reservation list must carry the free product at the configured quantity (2).
    items = mock_inventory_service.reserve_inventory.call_args.kwargs["items"]
    free_entries = [it for it in items if it["product_id"] == free_product.id]
    assert len(free_entries) == 1 and free_entries[0]["quantity"] == 2

    db.session.refresh(order)
    free_items = [i for i in order.order_items if i.product_id == free_product.id]
    assert len(free_items) == 1 and free_items[0].quantity == 2


@pytest.mark.integration
@pytest.mark.order
def test_create_order_rejects_reward_plus_promo(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
    loyalty_program,
    points_account,
    discount_reward,
    delivery_address,
):
    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]

    service = OrderService(inventory_service=mock_inventory_service)

    with pytest.raises(ValidationError, match="cannot be used"):
        service.create_order(
            sample_user.id,
            _order_data(
                sample_product,
                delivery_address,
                reward_id=discount_reward.id,
                promo_code="SAVE10",
            ),
        )


@pytest.mark.integration
@pytest.mark.order
def test_create_order_reward_failure_rolls_back_order(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
    loyalty_program,
    points_account,
    discount_reward,
    delivery_address,
    monkeypatch,
):
    """If apply_reward_to_order raises, no Order row must be persisted."""
    from business_app.models.order import Order

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    mock_inventory_service.reserve_inventory.return_value = {"success": True, "expires_at": None}

    def _boom(self, *a, **k):
        raise ValidationError("Insufficient points")

    monkeypatch.setattr(LoyaltyService, "apply_reward_to_order", _boom)

    before = Order.query.filter_by(user_id=sample_user.id).count()

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        with pytest.raises(ValidationError):
            service.create_order(
                sample_user.id,
                _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
            )

    assert Order.query.filter_by(user_id=sample_user.id).count() == before


@pytest.mark.integration
@pytest.mark.order
def test_create_order_rolls_back_reward_when_inventory_fails(
    app,
    db,
    sample_user,
    sample_product,
    mock_inventory_service,
    loyalty_program,
    points_account,
    discount_reward,
    delivery_address,
    monkeypatch,
):
    """Reward applied (real path), then inventory reservation fails -> everything
    rolls back: no Order, no RewardRedemption, points restored, usage counter at 0."""
    from business_app.models.order import Order

    monkeypatch.setattr(LoyaltyService, "_send_points_notification", lambda *a, **k: None)

    mock_inventory_service.check_multiple_products_availability.return_value = [
        SimpleNamespace(
            product_id=sample_product.id,
            requested_quantity=2,
            available_quantity=100,
            reserved_quantity=0,
            is_available=True,
            reason="Available",
        )
    ]
    # Reward block runs for real; inventory reservation reports failure afterwards.
    mock_inventory_service.reserve_inventory.return_value = {"success": False, "reason": "out of stock"}

    before_orders = Order.query.filter_by(user_id=sample_user.id).count()

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        with pytest.raises(ValidationError):
            service.create_order(
                sample_user.id,
                _order_data(sample_product, delivery_address, reward_id=discount_reward.id),
            )

    assert Order.query.filter_by(user_id=sample_user.id).count() == before_orders
    assert RewardRedemption.query.filter_by(user_id=sample_user.id).count() == 0
    # Points restored to the full seeded balance (1000) after rollback.
    assert LoyaltyService().get_available_points(sample_user.id) == 1000
    db.session.refresh(discount_reward)
    assert discount_reward.redemptions_used == 0
