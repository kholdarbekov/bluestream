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


# --- The legacy domestic trunk form, "8 90 123 45 67" (owner decision 2026-11-14, unit "phone") -------------------
# Older customers still type the trunk "8" before the 9-digit number. It is accepted only as a fallback, after the
# number as typed was refused, so no input that had an answer before gets a different one.


@pytest.mark.parametrize("raw, expected", [
    ("8 90 123 45 67", "+998901234567"),
    ("8(90)1234567", "+998901234567"),
    ("8 (90) 123-45-67", "+998901234567"),
    ("8-90-123-45-67", "+998901234567"),
    ("8901234567", "+998901234567"),
    ("8 88 123 45 67", "+998881234567"),
    ("8 20 004 81 56", "+998200048156"),
])
def test_the_domestic_trunk_form_normalizes_to_e164(raw, expected):
    assert normalize_phone_number(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    # Every form accepted before keeps its exact answer.
    ("+998901234567", "+998901234567"),
    (" +998901234567 ", "+998901234567"),
    ("+998 90 123 45 67", "+998901234567"),
    ("+998 (90) 123-45-67", "+998901234567"),
    ("998901234567", "+998901234567"),
    ("998 90 123 45 67", "+998901234567"),
    ("00998901234567", "+998901234567"),
    ("00 998 90 123 45 67", "+998901234567"),
    ("901234567", "+998901234567"),
    ("90 123 45 67", "+998901234567"),
    ("(90) 123-45-67", "+998901234567"),
    ("90-123-45-67", "+998901234567"),
    ("881234567", "+998881234567"),     # a bare national number that itself starts with 8
    ("88 123 45 67", "+998881234567"),
])
def test_every_previously_accepted_form_keeps_its_answer(raw, expected):
    assert normalize_phone_number(raw) == expected


@pytest.mark.parametrize("raw", [
    "8 71 123 45 67",          # a Tashkent landline in the trunk form: no SMS, never a sign-in number
    "8 (71) 200-00-00",
    "71 123 45 67",            # the same landline bare, refused before and after
    "8 00 123 45 67",          # no such UZ range
    "8 90 123 45 6",           # one digit short
    "8 90 123 45 678",         # one digit long
    "88 90 123 45 67",         # two trunk digits
    "89012345678",             # a Russian mobile in its own trunk form
    "+7 901 234 56 78",
    "+8 90 123 45 67",         # a "+" means an international number, never the trunk form
    "8 10 998 90 123 45 67",   # the old international dialling prefix is not a form we read
    "+12025550123",
])
def test_the_trunk_fallback_admits_nothing_foreign_or_landline(raw):
    assert normalize_phone_number(raw) is None


@pytest.mark.parametrize("prefix", [f"{n:02d}" for n in range(100)])
def test_the_trunk_form_accepts_exactly_what_the_bare_number_does(prefix):
    """For every operator prefix, "8" + the 9-digit number answers what the 9-digit number answers on its own: the
    fallback widens nothing beyond the bare national form the normaliser already accepted."""
    bare = f"{prefix}1234567"
    assert normalize_phone_number(f"8{bare}") == normalize_phone_number(bare)
    assert normalize_phone_number(f"8 ({prefix}) 123-45-67") == normalize_phone_number(bare)
