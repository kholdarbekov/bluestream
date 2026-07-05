import pytest
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers.subscription_serializers import (
    AdminCreateSubscriptionRequest,
    AdminUpdateSubscriptionRequest,
    AdminAddSubscriptionItemRequest,
    AdminUpdateSubscriptionItemRequest,
)


@pytest.mark.unit
class TestAdminSubscriptionSerializers:
    def test_create_request_requires_user_id_and_items(self):
        model = AdminCreateSubscriptionRequest(
            user_id=7,
            name="Weekly Water",
            billing_cycle="monthly",
            delivery_frequency="weekly",
            delivery_address_id=3,
            payment_method="cash",
            items=[{"product_id": 2, "quantity": 4}],
        )
        assert model.user_id == 7
        assert model.auto_renew is True  # default
        assert model.discount_percentage == 0.0  # default
        assert model.items == [{"product_id": 2, "quantity": 4}]

    def test_create_request_rejects_missing_user_id(self):
        with pytest.raises(PydanticValidationError):
            AdminCreateSubscriptionRequest(
                name="X yz",
                billing_cycle="monthly",
                delivery_frequency="weekly",
                delivery_address_id=3,
                payment_method="cash",
                items=[{"product_id": 2, "quantity": 4}],
            )

    def test_create_request_rejects_empty_items(self):
        with pytest.raises(PydanticValidationError):
            AdminCreateSubscriptionRequest(
                user_id=7,
                name="X yz",
                billing_cycle="monthly",
                delivery_frequency="weekly",
                delivery_address_id=3,
                payment_method="cash",
                items=[],
            )

    def test_update_request_override_flags_default_false(self):
        model = AdminUpdateSubscriptionRequest(name="New name")
        assert model.override_edit_any_status is False
        assert model.override_manual_billing_amount is False
        assert model.override_manual_billing_dates is False
        # exclude_none keeps only the provided field + the (non-None) flags
        dumped = model.model_dump(
            exclude_none=True,
            exclude={
                "override_edit_any_status",
                "override_manual_billing_amount",
                "override_manual_billing_dates",
            },
        )
        assert dumped == {"name": "New name"}

    def test_update_request_carries_override_values(self):
        model = AdminUpdateSubscriptionRequest(
            billing_amount=12345.0,
            next_billing_date="2026-08-01T09:00:00+00:00",
            override_manual_billing_amount=True,
            override_manual_billing_dates=True,
        )
        assert model.billing_amount == 12345.0
        assert model.override_manual_billing_amount is True
        assert model.next_billing_date is not None

    def test_add_item_request_validates_quantity(self):
        with pytest.raises(PydanticValidationError):
            AdminAddSubscriptionItemRequest(product_id=2, quantity=0)

    def test_update_item_request_ok(self):
        model = AdminUpdateSubscriptionItemRequest(quantity=3, special_instructions="cold")
        assert model.quantity == 3
