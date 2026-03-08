"""Unit tests for contract-aware product serialization pricing."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from business_app.serializers.product_serializers import serialize_product


@pytest.mark.unit
def test_serialize_product_uses_contract_price_for_entity_user(sample_user, sample_product, db):
    sample_user.user_type = "entity"
    db.session.add(sample_user)
    db.session.commit()

    corporate_service = MagicMock()
    corporate_service.resolve_contract_pricing_for_user_product.return_value = {
        "unit_price": Decimal("12345.00"),
        "contract": None,
        "contract_price_row": None,
    }

    with patch(
        "business_app.utils.service_factory.get_corporate_contract_service",
        return_value=corporate_service,
    ):
        payload = serialize_product(sample_product, language="en", user=sample_user, quantity=1)

    assert payload["pricing"]["current_price"] == 12345.0
    assert payload["pricing"]["total_price"] == 12345.0
    corporate_service.resolve_contract_pricing_for_user_product.assert_called_once_with(
        user_id=sample_user.id,
        product_id=sample_product.id,
        fallback_price=Decimal("15000.0"),
    )
