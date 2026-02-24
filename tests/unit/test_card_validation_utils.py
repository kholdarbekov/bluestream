"""Unit tests for card validation and security helpers."""

from datetime import datetime, UTC

import pytest

from business_app.utils.card_validation import CardSecurityValidator, CardValidator


@pytest.mark.unit
class TestCardValidator:
    def test_validate_card_number_errors(self):
        assert CardValidator.validate_card_number("").is_valid is False
        assert CardValidator.validate_card_number("abcd").is_valid is False
        assert CardValidator.validate_card_number("123456789012").is_valid is False
        assert CardValidator.validate_card_number("4111111111111112").is_valid is False

        unsupported = CardValidator.validate_card_number("6011111111111117")
        assert unsupported.is_valid is False

    def test_validate_card_number_success_supported(self):
        visa = CardValidator.validate_card_number("4111 1111 1111 1111")
        assert visa.is_valid is True
        assert visa.card_brand == "visa"
        assert visa.card_type == "credit"

    def test_validate_expiry_date(self):
        now = datetime.now(UTC)
        assert CardValidator.validate_expiry_date(0, now.year).is_valid is False
        assert CardValidator.validate_expiry_date(12, now.year - 1).is_valid is False
        assert CardValidator.validate_expiry_date(1, now.year + 21).is_valid is False

        valid_current = CardValidator.validate_expiry_date(now.month, now.year)
        assert valid_current.is_valid is True

    def test_validate_cvv_and_cardholder_name(self):
        assert CardValidator.validate_cvv("12", "visa").is_valid is False
        assert CardValidator.validate_cvv("123", "visa").is_valid is True
        assert CardValidator.validate_cvv("1234", "amex").is_valid is True

        assert CardValidator.validate_cardholder_name("").is_valid is False
        assert CardValidator.validate_cardholder_name("1").is_valid is False
        assert CardValidator.validate_cardholder_name("John#Doe").is_valid is False
        assert CardValidator.validate_cardholder_name("John Doe").is_valid is True

    def test_validate_complete_card_and_tokenized(self):
        card = {
            "card_number": "4111111111111111",
            "expiry_month": 12,
            "expiry_year": datetime.now(UTC).year + 1,
            "cvv": "123",
            "cardholder_name": "John Doe",
        }
        full = CardValidator.validate_complete_card(card)
        assert full.is_valid is True
        assert full.card_brand == "visa"

        tokenized = CardValidator.validate_complete_card(
            {
                "card_token": "tok_1234567890abcdef",
                "card_number": "**** **** **** 6478",
                "expiry_month": 12,
                "expiry_year": datetime.now(UTC).year + 1,
                "cardholder_name": "Jane Doe",
            }
        )
        assert tokenized.is_valid is True
        assert tokenized.card_brand == "unknown"

        invalid_tokenized = CardValidator.validate_tokenized_card(
            {
                "card_token": "short",
                "card_number": "**** **** **** 6478",
                "expiry_month": 12,
                "expiry_year": datetime.now(UTC).year + 1,
                "cardholder_name": "Jane Doe",
            }
        )
        assert invalid_tokenized.is_valid is False

    def test_card_helpers(self):
        assert CardValidator._is_masked_card_number("****1111") is True
        assert CardValidator._is_masked_card_number("4111111111111111") is False
        assert CardValidator._extract_last_four_from_masked("**** **** **** 6478") == "6478"
        assert CardValidator._extract_last_four_from_masked("") == ""

        fp1 = CardValidator.generate_card_fingerprint("4111111111111111", 12, 2030)
        fp2 = CardValidator.generate_tokenized_card_fingerprint("tok_1234567890abcdef", "1111", 12, 2030)
        assert len(fp1) == 32
        assert len(fp2) == 32

        assert CardValidator.mask_card_number("4111 1111 1111 1111").endswith("1111")
        assert CardValidator.get_last_four_digits("4111111111111111") == "1111"
        assert CardValidator._clean_card_number("4111-1111-1111-1111") == "4111111111111111"
        assert CardValidator._luhn_check("4111111111111111") is True
        assert CardValidator._detect_card_brand("4111111111111111") == "visa"
        assert CardValidator._get_card_type("humo") == "debit"


@pytest.mark.unit
class TestCardSecurityValidator:
    def test_security_checks(self):
        assert CardSecurityValidator.validate_no_sequential_numbers("0123456789012345") is False
        assert CardSecurityValidator.validate_no_sequential_numbers("4111111111111111") is False
        assert CardSecurityValidator.validate_no_sequential_numbers("4532123412345678") is True

        assert CardSecurityValidator.validate_not_test_card("4111111111111111") is False
        assert CardSecurityValidator.validate_not_test_card("4242424242424242") is True

        assert CardSecurityValidator.validate_bin_country("8600123412341231") is True
        assert CardSecurityValidator.validate_bin_country("4242424242424242", ["UZ"]) is True
