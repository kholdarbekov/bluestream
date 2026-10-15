"""serialize_product before a payment method is chosen (spec 2026-10-09 D5, §4.4)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
)
from business_app.serializers.product_serializers import serialize_product
from shared.enums import EntitySubtype


@pytest.mark.unit
def test_serialize_product_states_the_catalogue_and_flags_the_contract_price(sample_user, sample_product, db):
    sample_user.user_type = "entity"
    sample_user.entity_subtype = EntitySubtype.WORKPLACE
    contract = CorporateContract(
        user_id=sample_user.id,
        contract_number="C-SERIALIZER-1",
        name="Serializer contract",
        status=CorporateContractStatus.ACTIVE,
        start_date=datetime.now(UTC) - timedelta(days=1),
        currency="UZS",
        is_active=True,
    )
    db.session.add(contract)
    db.session.flush()
    db.session.add(
        CorporateContractProductPrice(
            contract_id=contract.id,
            product_id=sample_product.id,
            business_account_unit_price=Decimal("12345.00"),
            cash_unit_price=Decimal("13000.00"),
            is_active=True,
        )
    )
    db.session.commit()

    payload = serialize_product(sample_product, language="en", user=sample_user, quantity=1)

    assert payload["pricing"]["current_price"] == 15000.0
    assert payload["pricing"]["contract_price_at_checkout"] is True
