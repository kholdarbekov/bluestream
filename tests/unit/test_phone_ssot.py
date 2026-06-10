import pytest
from shared.validators import (
    normalize_phone_number,
    validate_phone_number,
    validate_uzbekistan_phone,
    mask_phone_number,
)


@pytest.mark.parametrize("raw", [
    "+998200048156",   # Humans prefix 20 — the prod outage number
    "998200048156",    # Telegram contact form (no +)
    "200048156",       # bare national
    "+998 20 004 81 56",
])
def test_prefix_20_normalizes_to_e164(raw):
    assert normalize_phone_number(raw) == "+998200048156"


@pytest.mark.parametrize("raw", [
    "+998901234567", "998901234567", "901234567", "90 123 45 67",
])
def test_standard_mobile_normalizes(raw):
    assert normalize_phone_number(raw) == "+998901234567"


@pytest.mark.parametrize("raw", ["", None, "12345", "+12025550123", "+99899"])
def test_invalid_returns_none(raw):
    assert normalize_phone_number(raw) is None


def test_validate_uzbekistan_phone_tuple_shape():
    ok, msg, norm = validate_uzbekistan_phone("+998200048156")
    assert ok is True and norm == "+998200048156"
    ok, msg, norm = validate_uzbekistan_phone("nonsense")
    assert ok is False and norm is None


def test_validate_phone_number_boolean():
    assert validate_phone_number("+998200048156") is True
    assert validate_phone_number("nonsense") is False


def test_mask_phone_number():
    assert mask_phone_number("+998200048156") == "+998***8156"
    assert mask_phone_number("") == "***"
