"""
Credit card validation utilities for secure card processing
"""
import re
import hashlib
from datetime import datetime, UTC
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass

@dataclass
class CardValidationResult:
    """Result of card validation"""
    is_valid: bool
    errors: List[str]
    card_brand: Optional[str] = None
    card_type: Optional[str] = None
    
    def add_error(self, error: str):
        """Add validation error"""
        self.errors.append(error)
        self.is_valid = False


class CardValidator:
    """Comprehensive credit card validation"""
    
    # Card brand patterns (IIN ranges)
    CARD_PATTERNS = {
        'visa': [
            r'^4[0-9]{12}(?:[0-9]{3})?$',  # 13 or 16 digits starting with 4
        ],
        'mastercard': [
            r'^5[1-5][0-9]{14}$',          # 16 digits starting with 51-55
            r'^2[2-7][0-9]{14}$',          # 16 digits starting with 22-27 (new range)
        ],
        'amex': [
            r'^3[47][0-9]{13}$',           # 15 digits starting with 34 or 37
        ],
        'discover': [
            r'^6(?:011|5[0-9]{2})[0-9]{12}$',  # 16 digits starting with 6011 or 65
        ],
        'diners': [
            r'^3[0689][0-9]{11}$',         # 14 digits starting with 30, 36, 38, or 39
        ],
        'jcb': [
            r'^(?:2131|1800|35\d{3})\d{11}$',  # Various JCB patterns
        ],
        'uzcard': [
            r'^8600[0-9]{12}$',            # 16 digits starting with 8600
        ],
        'humo': [
            r'^9860[0-9]{12}$',            # 16 digits starting with 9860
        ]
    }
    
    # Supported card brands for local market
    SUPPORTED_BRANDS = ['visa', 'mastercard', 'uzcard', 'humo']
    
    @classmethod
    def validate_card_number(cls, card_number: str) -> CardValidationResult:
        """
        Validate credit card number using multiple checks
        """
        result = CardValidationResult(is_valid=True, errors=[])
        
        # Clean input
        cleaned_number = cls._clean_card_number(card_number)
        
        # Basic format validation
        if not cleaned_number:
            result.add_error("Card number is required")
            return result
        
        if not cleaned_number.isdigit():
            result.add_error("Card number must contain only digits")
            return result
        
        # Length validation
        if len(cleaned_number) < 13 or len(cleaned_number) > 19:
            result.add_error("Card number must be between 13 and 19 digits")
            return result
        
        # Luhn algorithm validation
        if not cls._luhn_check(cleaned_number):
            result.add_error("Invalid card number (fails checksum)")
            return result
        
        # Brand detection and validation
        brand = cls._detect_card_brand(cleaned_number)
        if not brand:
            result.add_error("Unsupported card brand")
            return result
        
        if brand not in cls.SUPPORTED_BRANDS:
            result.add_error(f"Card brand '{brand}' is not supported")
            return result
        
        result.card_brand = brand
        result.card_type = cls._get_card_type(brand)
        
        return result
    
    @classmethod
    def validate_expiry_date(cls, month: int, year: int) -> CardValidationResult:
        """
        Validate card expiry date
        """
        result = CardValidationResult(is_valid=True, errors=[])
        
        # Month validation
        if not isinstance(month, int) or month < 1 or month > 12:
            result.add_error("Expiry month must be between 1 and 12")
            return result
        
        # Year validation
        current_year = datetime.now(UTC).year
        if not isinstance(year, int) or year < current_year:
            result.add_error("Card has expired")
            return result
        
        if year > current_year + 20:
            result.add_error("Expiry year is too far in the future")
            return result
        
        # Check if card has expired this year
        if year == current_year:
            current_month = datetime.now(UTC).month
            if month < current_month:
                result.add_error("Card has expired")
                return result
        
        return result
    
    @classmethod
    def validate_cvv(cls, cvv: str, card_brand: str) -> CardValidationResult:
        """
        Validate CVV/CVC code
        """
        result = CardValidationResult(is_valid=True, errors=[])
        
        if not cvv or not cvv.isdigit():
            result.add_error("CVV must contain only digits")
            return result
        
        # CVV length validation based on card brand
        expected_length = 4 if card_brand == 'amex' else 3
        
        if len(cvv) != expected_length:
            result.add_error(f"CVV must be {expected_length} digits for {card_brand}")
            return result
        
        return result
    
    @classmethod
    def validate_cardholder_name(cls, name: str) -> CardValidationResult:
        """
        Validate cardholder name
        """
        result = CardValidationResult(is_valid=True, errors=[])
        
        if not name or not name.strip():
            result.add_error("Cardholder name is required")
            return result
        
        cleaned_name = name.strip()
        
        # Length validation
        if len(cleaned_name) < 2:
            result.add_error("Cardholder name is too short")
            return result
        
        if len(cleaned_name) > 100:
            result.add_error("Cardholder name is too long")
            return result
        
        # Character validation (allow letters, spaces, hyphens, apostrophes)
        if not re.match(r"^[a-zA-Z\s\-'\.]+$", cleaned_name):
            result.add_error("Cardholder name contains invalid characters")
            return result
        
        # Must contain at least one letter
        if not re.search(r'[a-zA-Z]', cleaned_name):
            result.add_error("Cardholder name must contain letters")
            return result
        
        return result
    
    @classmethod
    def validate_complete_card(cls, card_data: Dict) -> CardValidationResult:
        """
        Validate complete card information
        """
        result = CardValidationResult(is_valid=True, errors=[])
        
        # Validate card number
        number_result = cls.validate_card_number(card_data.get('card_number', ''))
        if not number_result.is_valid:
            result.errors.extend(number_result.errors)
            result.is_valid = False
        else:
            result.card_brand = number_result.card_brand
            result.card_type = number_result.card_type
        
        # Validate expiry date
        expiry_result = cls.validate_expiry_date(
            card_data.get('expiry_month'), 
            card_data.get('expiry_year')
        )
        if not expiry_result.is_valid:
            result.errors.extend(expiry_result.errors)
            result.is_valid = False
        
        # Validate CVV if provided
        cvv = card_data.get('cvv')
        if cvv:
            cvv_result = cls.validate_cvv(cvv, result.card_brand or 'visa')
            if not cvv_result.is_valid:
                result.errors.extend(cvv_result.errors)
                result.is_valid = False
        
        # Validate cardholder name
        name_result = cls.validate_cardholder_name(card_data.get('cardholder_name', ''))
        if not name_result.is_valid:
            result.errors.extend(name_result.errors)
            result.is_valid = False
        
        return result
    
    @classmethod
    def generate_card_fingerprint(cls, card_number: str, expiry_month: int, expiry_year: int) -> str:
        """
        Generate unique fingerprint for card to detect duplicates
        """
        cleaned_number = cls._clean_card_number(card_number)
        fingerprint_data = f"{cleaned_number[:6]}{cleaned_number[-4:]}{expiry_month:02d}{expiry_year}"
        return hashlib.sha256(fingerprint_data.encode()).hexdigest()[:32]
    
    @classmethod
    def mask_card_number(cls, card_number: str) -> str:
        """
        Mask card number for display (show only last 4 digits)
        """
        cleaned = cls._clean_card_number(card_number)
        if len(cleaned) < 4:
            return "****"
        return "*" * (len(cleaned) - 4) + cleaned[-4:]
    
    @classmethod
    def get_last_four_digits(cls, card_number: str) -> str:
        """
        Extract last four digits of card number
        """
        cleaned = cls._clean_card_number(card_number)
        return cleaned[-4:] if len(cleaned) >= 4 else cleaned
    
    @classmethod
    def _clean_card_number(cls, card_number: str) -> str:
        """
        Clean card number by removing spaces, dashes, and other non-digits
        """
        if not card_number:
            return ""
        return re.sub(r'[^0-9]', '', str(card_number))
    
    @classmethod
    def _luhn_check(cls, card_number: str) -> bool:
        """
        Validate card number using Luhn algorithm (mod 10 check)
        """
        def luhn_checksum(card_num):
            def digits_of(n):
                return [int(d) for d in str(n)]
            
            digits = digits_of(card_num)
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(digits_of(d * 2))
            return checksum % 10
        
        return luhn_checksum(card_number) == 0
    
    @classmethod
    def _detect_card_brand(cls, card_number: str) -> Optional[str]:
        """
        Detect card brand based on number patterns
        """
        for brand, patterns in cls.CARD_PATTERNS.items():
            for pattern in patterns:
                if re.match(pattern, card_number):
                    return brand
        return None
    
    @classmethod
    def _get_card_type(cls, brand: str) -> str:
        """
        Get card type (credit/debit) based on brand
        Note: This is simplified - in reality, you'd need BIN database lookup
        """
        type_mapping = {
            'visa': 'credit',
            'mastercard': 'credit',
            'amex': 'credit',
            'discover': 'credit',
            'diners': 'credit',
            'jcb': 'credit',
            'uzcard': 'debit',  # Local cards are typically debit
            'humo': 'debit'
        }
        return type_mapping.get(brand, 'credit')


class CardSecurityValidator:
    """Additional security validations for card processing"""
    
    @classmethod
    def validate_no_sequential_numbers(cls, card_number: str) -> bool:
        """
        Check for obviously fake sequential numbers (e.g., 1234567890123456)
        """
        cleaned = CardValidator._clean_card_number(card_number)
        
        # Check for sequential ascending
        sequential_asc = ''.join(str(i % 10) for i in range(len(cleaned)))
        if cleaned == sequential_asc:
            return False
        
        # Check for sequential descending
        sequential_desc = ''.join(str((9 - i) % 10) for i in range(len(cleaned)))
        if cleaned == sequential_desc:
            return False
        
        # Check for repeated patterns
        if len(set(cleaned)) <= 2:  # Too few unique digits
            return False
        
        return True
    
    @classmethod
    def validate_not_test_card(cls, card_number: str) -> bool:
        """
        Check if card number is a known test card number
        """
        cleaned = CardValidator._clean_card_number(card_number)
        
        # Common test card numbers
        test_numbers = {
            '4111111111111111',  # Visa test
            '4000000000000002',  # Visa test
            '5555555555554444',  # Mastercard test
            '5105105105105100',  # Mastercard test
            '378282246310005',   # Amex test
            '371449635398431',   # Amex test
        }
        
        return cleaned not in test_numbers
    
    @classmethod
    def validate_bin_country(cls, card_number: str, allowed_countries: List[str] = None) -> bool:
        """
        Validate BIN (Bank Identification Number) country
        Note: This is simplified - in production, use a BIN database service
        """
        if not allowed_countries:
            allowed_countries = ['UZ', 'RU', 'KZ']  # Default for regional market
        
        cleaned = CardValidator._clean_card_number(card_number)
        bin_number = cleaned[:6]
        
        # Simplified country detection based on known BINs
        uzbekistan_bins = ['860000', '986000']  # UzCard, Humo
        russia_bins = ['427600', '548673']      # Common Russian banks
        
        # For local cards, always allow
        if any(bin_number.startswith(uz_bin[:4]) for uz_bin in uzbekistan_bins):
            return True
        
        # For international cards, would need proper BIN database lookup
        # This is a simplified implementation
        return True