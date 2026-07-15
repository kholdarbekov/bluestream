from datetime import datetime
from decimal import Decimal

from business_app.serializers.admin_serializers import CustomerMapPinSchema


def test_customer_map_pin_schema_camelcase_and_money_float():
    pin = {
        "address_id": 7,
        "user_id": 42,
        "full_name": "Alisher Karimov",
        "phone": "+998901234567",
        "user_type": "individual",
        "entity_subtype": None,
        "lat": 41.31,
        "lng": 69.28,
        "is_default": True,
        "address_label": "Chilonzor 12",
        "address_index": 1,
        "address_count": 2,
        "last_order_date": datetime(2026, 7, 1, 9, 0, 0),
        "order_count": 5,
        "bottle_balance": Decimal("3.00"),
        "outstanding_debt": Decimal("12000.00"),
        "active_cod_debt_count": 1,
        "cod_restricted": False,
    }
    dumped = CustomerMapPinSchema.model_validate(pin).model_dump(by_alias=True)

    assert dumped["addressId"] == 7
    assert dumped["userId"] == 42
    assert dumped["fullName"] == "Alisher Karimov"
    assert dumped["bottleBalance"] == 3.0          # MoneyFloat -> float
    assert isinstance(dumped["bottleBalance"], float)
    assert dumped["outstandingDebt"] == 12000.0
    assert dumped["addressCount"] == 2
    assert dumped["codRestricted"] is False


def test_customer_map_pin_schema_nullable_fields():
    pin = {
        "address_id": 1, "user_id": 1, "full_name": "No Phone",
        "phone": None, "user_type": "entity", "entity_subtype": "grocery_store",
        "lat": 41.3, "lng": 69.2, "is_default": False, "address_label": "",
        "address_index": 1, "address_count": 1, "last_order_date": None,
        "order_count": 0, "bottle_balance": Decimal("0"), "outstanding_debt": Decimal("0"),
        "active_cod_debt_count": 0, "cod_restricted": False,
    }
    dumped = CustomerMapPinSchema.model_validate(pin).model_dump(by_alias=True)
    assert dumped["phone"] is None
    assert dumped["lastOrderDate"] is None
    assert dumped["entitySubtype"] == "grocery_store"
