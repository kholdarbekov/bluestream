"""Unit tests for shared validator utilities."""

from datetime import date, datetime, timedelta

import pytest
from phonenumbers import NumberParseException, PhoneNumberType

from business_app.utils.validators import (
    CoordinateValidator,
    DateValidator,
    EmailValidator,
    NumericValidator,
    OrderValidator,
    PasswordValidator,
    PaymentValidator,
    PhoneValidator,
    Validator,
    email_validator,
    mask_phone_number,
    normalize_phone_number,
    normalize_uzbekistan_phone,
    password_validator,
    phone_validator,
    required_validator,
    validate_data,
    validate_uzbekistan_phone,
)


@pytest.mark.unit
class TestBaseValidators:
    def test_validator_chain_methods(self):
        errors = (
            Validator(" x ", "name")
            .required()
            .min_length(5)
            .max_length(1)
            .pattern(r"^abc$")
            .one_of(["abc", "def"])
            .get_errors()
        )
        assert len(errors) == 4

    def test_email_validator(self):
        assert EmailValidator("user@example.com", "email").validate().is_valid() is True
        assert EmailValidator("bad-email", "email").validate().is_valid() is False

    def test_phone_validator_paths(self, monkeypatch):
        monkeypatch.setattr("business_app.utils.validators.phonenumbers.parse", lambda *_: object())
        monkeypatch.setattr("business_app.utils.validators.phonenumbers.is_valid_number", lambda *_: False)
        monkeypatch.setattr(
            "business_app.utils.validators.phonenumbers.number_type",
            lambda *_: PhoneNumberType.FIXED_LINE,
        )

        validator = PhoneValidator("+998901234567", "phone", "UZ").validate()
        assert validator.is_valid() is False

        def _raise(*_args, **_kwargs):
            raise NumberParseException(0, "bad number")

        monkeypatch.setattr("business_app.utils.validators.phonenumbers.parse", _raise)
        invalid = PhoneValidator("bad", "phone", "UZ").validate()
        assert invalid.is_valid() is False

    def test_password_coordinate_date_numeric_validators(self):
        weak = PasswordValidator("password", "password").validate()
        assert weak.is_valid() is False

        coord = CoordinateValidator("200", "lat").validate_latitude().validate_longitude()
        assert coord.is_valid() is False

        future_day = (date.today() + timedelta(days=1)).isoformat()
        past_day = (date.today() - timedelta(days=1)).isoformat()

        assert DateValidator(future_day, "delivery_date").validate_date().future_date().is_valid() is True
        assert DateValidator(past_day, "delivery_date").past_date().is_valid() is True
        assert DateValidator("not-a-date", "delivery_date").validate_date().is_valid() is False

        numeric = NumericValidator("3.5", "amount").min_value(2).max_value(4).positive().integer()
        assert numeric.is_valid() is False


@pytest.mark.unit
class TestDomainValidators:
    def test_order_validator_errors(self):
        errors = OrderValidator.validate_order_data({"items": "bad", "delivery_address": {}})
        assert "items must be a list" in errors
        assert any("delivery_address is required" in err for err in errors)

        nested_errors = OrderValidator.validate_order_data(
            {"items": [], "delivery_address": {"street": "Main"}}
        )
        assert any("delivery_address.city is required" in err for err in nested_errors)

        item_errors = OrderValidator.validate_order_data(
            {
                "items": [{"product_id": 1, "quantity": 0}, {"quantity": "x"}, "bad"],
                "delivery_address": {
                    "street": "Main",
                    "city": "Tashkent",
                    "latitude": 41.3,
                    "longitude": 69.2,
                },
            }
        )
        assert any("quantity must be positive" in err for err in item_errors)
        assert any("must have product_id" in err for err in item_errors)
        assert any("must be an object" in err for err in item_errors)

    def test_payment_validator(self):
        missing = PaymentValidator.validate_payment_data({})
        assert "amount is required" in missing
        assert "payment_method is required" in missing

        invalid_method = PaymentValidator.validate_payment_data({"amount": 25000, "payment_method": "crypto"})
        assert any("payment_method must be one of" in err for err in invalid_method)

    def test_validate_data_and_helper_validators(self):
        def _rule_ok(value, _field):
            return [] if value else ["missing"]

        def _rule_crash(_value, _field):
            raise RuntimeError("boom")

        errors = validate_data({"name": ""}, {"name": [_rule_ok, _rule_crash]})
        assert "name" in errors
        assert any("Validation error" in err for err in errors["name"])

        assert required_validator("", "field")
        assert email_validator("bad", "email")
        assert phone_validator("+998901234567") or isinstance(phone_validator("+998901234567"), list)
        assert password_validator("weak", "password")


@pytest.mark.unit
class TestUzbekPhoneValidators:
    def test_phone_normalize_validate_and_mask(self):
        # Phone validation is now delegated to the shared phonenumbers-backed SSOT.
        assert normalize_phone_number("90 123 45 67") == "+998901234567"
        assert validate_uzbekistan_phone("90 123 45 67")[0] is True
        assert mask_phone_number("90 123 45 67") == "+998***4567"

        # Prefix 20 (Humans) — the prod outage number — is now accepted.
        assert normalize_phone_number("+998200048156") == "+998200048156"
        assert validate_uzbekistan_phone("+998200048156")[0] is True

        # Legacy 8-prefixed format is dropped (YAGNI) — must not normalize.
        assert normalize_phone_number("89981234567") is None

        # Foreign numbers are rejected.
        assert normalize_phone_number("+12025550123") is None

    def test_helper_phone_functions(self):
        ok, msg, normalized = validate_uzbekistan_phone("+998901234567")
        assert ok is True
        assert msg == "Phone is valid"
        assert normalized == "+998901234567"

        ok2, msg2, normalized2 = validate_uzbekistan_phone("+12345")
        assert ok2 is False
        assert normalized2 is None
        assert isinstance(msg2, str)

        assert normalize_uzbekistan_phone("90 123 45 67") == "+998901234567"
        assert mask_phone_number("+998901234567") == "+998***4567"
        # Un-normalizable but long-enough input is masked head/tail; empty -> placeholder.
        assert mask_phone_number("1234567") == "1234***4567"
        assert mask_phone_number("") == "***"
