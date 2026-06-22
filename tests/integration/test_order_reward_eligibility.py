"""Integration test: ineligible entity users cannot apply a loyalty reward.

An ENTITY user with no corporate contract is not loyalty-eligible.
Passing a reward_id in order_data must raise ValidationError with a message
containing "not available" — before any reward lookup or DB writes.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward
from business_app.models.user import UserAddress
from business_app.services.order_service import OrderService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserType


@pytest.fixture
def loyalty_program_for_eligibility(db):
    program = LoyaltyProgram(name="Default Eligibility", is_active=True, is_default=True)
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def some_reward(db, loyalty_program_for_eligibility):
    """A valid reward — should never be reached for the ineligible user."""
    reward = LoyaltyReward(
        program_id=loyalty_program_for_eligibility.id,
        name="100 off",
        reward_type="discount",
        discount_type="fixed",
        discount_value=Decimal("100.00"),
        points_cost=50,
        is_active=True,
    )
    db.session.add(reward)
    db.session.commit()
    return reward


@pytest.fixture
def ineligible_entity_user(db):
    """An ENTITY user with no corporate contract — not loyalty-eligible."""
    from datetime import datetime, UTC
    user = __import__("business_app.models.user", fromlist=["User"]).User(
        email="entity_noloyalty@example.com",
        phone="+998901111222",
        password_hash=hash_password("Pass1234!"),
        first_name="Entity",
        last_name="Corp",
        user_type=UserType.ENTITY,
        role=UserRole.CUSTOMER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def entity_address(db, ineligible_entity_user):
    address = UserAddress(
        user_id=ineligible_entity_user.id,
        title="Office",
        full_address="Office Street 5",
        street_address="Office Street 5",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        is_default=True,
    )
    db.session.add(address)
    db.session.commit()
    return address


@pytest.mark.integration
@pytest.mark.order
def test_ineligible_entity_user_cannot_apply_reward(
    app,
    db,
    sample_product,
    mock_inventory_service,
    ineligible_entity_user,
    entity_address,
    some_reward,
    loyalty_program_for_eligibility,
):
    """An entity user without a corporate contract is not loyalty-eligible.
    Passing a reward_id must raise ValidationError(match='not available')
    before any reward lookup or inventory/DB work.
    """
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

    order_data = {
        "items": [{"product_id": sample_product.id, "quantity": 2}],
        "delivery_address": {
            "delivery_address_id": entity_address.id,
            "street": entity_address.street_address,
            "latitude": entity_address.latitude,
            "longitude": entity_address.longitude,
        },
        "payment_method": "click",
        "reward_id": some_reward.id,
    }

    service = OrderService(inventory_service=mock_inventory_service)

    with patch(
        "business_app.services.corporate_contract_service.CorporateContractService.reserve_for_order",
        return_value=None,
    ), patch(
        "business_app.services.payment_service.PaymentService.initialize_order_payment",
        return_value=None,
    ):
        with pytest.raises(ValidationError, match="not available"):
            service.create_order(ineligible_entity_user.id, order_data)
