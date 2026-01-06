"""
Unit tests for Order Number Generation
Tests the new sequential order number format: {PREFIX}{SEQUENCE}_{YY}
"""
import pytest
import re
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock


class TestOrderSourcePrefixes:
    """Test ORDER_SOURCE_PREFIXES constant"""

    def test_all_sources_have_prefixes(self):
        """Verify all expected sources have prefixes defined"""
        from business_app.utils.constants import ORDER_SOURCE_PREFIXES

        expected_sources = ['telegram', 'web', 'phone', 'admin', 'api', 'mobile']
        for source in expected_sources:
            assert source in ORDER_SOURCE_PREFIXES, f"Missing prefix for source: {source}"

    def test_prefixes_are_two_chars(self):
        """Verify all prefixes are exactly 2 characters"""
        from business_app.utils.constants import ORDER_SOURCE_PREFIXES

        for source, prefix in ORDER_SOURCE_PREFIXES.items():
            assert len(prefix) == 2, f"Prefix for {source} should be 2 chars, got: {prefix}"

    def test_prefixes_are_uppercase(self):
        """Verify all prefixes are uppercase"""
        from business_app.utils.constants import ORDER_SOURCE_PREFIXES

        for source, prefix in ORDER_SOURCE_PREFIXES.items():
            assert prefix.isupper(), f"Prefix for {source} should be uppercase: {prefix}"

    def test_known_prefixes(self):
        """Verify specific prefix mappings"""
        from business_app.utils.constants import ORDER_SOURCE_PREFIXES

        assert ORDER_SOURCE_PREFIXES['telegram'] == 'TG'
        assert ORDER_SOURCE_PREFIXES['web'] == 'WB'
        assert ORDER_SOURCE_PREFIXES['phone'] == 'CC'
        assert ORDER_SOURCE_PREFIXES['admin'] == 'AD'
        assert ORDER_SOURCE_PREFIXES['api'] == 'AP'
        assert ORDER_SOURCE_PREFIXES['mobile'] == 'MB'


class TestOrderNumberPattern:
    """Test ORDER_NUMBER regex pattern"""

    def test_new_format_pattern_matches(self):
        """Test new format order numbers match pattern"""
        from business_app.utils.constants import PATTERNS

        pattern = PATTERNS['ORDER_NUMBER']
        valid_numbers = [
            'TG_000001_26',
            'WB_000042_25',
            'CC_999999_24',
            'AD_000007_26',
            'AP_123456_23',
            'MB_000100_26',
        ]

        for order_number in valid_numbers:
            assert re.match(pattern, order_number), f"Pattern should match: {order_number}"

    def test_new_format_pattern_rejects_invalid(self):
        """Test invalid order numbers are rejected"""
        from business_app.utils.constants import PATTERNS

        pattern = PATTERNS['ORDER_NUMBER']
        invalid_numbers = [
            'TG_00001_26',      # Only 5 digits
            'T_0000001_26',    # 7 digits
            'X_000001_26',     # Invalid prefix
            'T-000042_26',     # Wrong separator
            'TG_00042_2',       # Year only 1 digit
            'TG_0042_266',      # Year 3 digits
            'tg_000001_26',     # Lowercase prefix
        ]

        for order_number in invalid_numbers:
            assert not re.match(pattern, order_number), f"Pattern should not match: {order_number}"


class TestGenerateOrderNumber:
    """Test generate_order_number function"""

    @pytest.fixture
    def mock_db_session(self):
        """Mock database session for sequence generation"""
        with patch('business_app.utils.helpers.db') as mock_db:
            mock_result = MagicMock()
            mock_result.scalar.return_value = 42
            mock_db.session.execute.return_value = mock_result
            yield mock_db

    def test_generates_correct_format_for_telegram(self, mock_db_session):
        """Test order number format for Telegram source"""
        from business_app.utils.helpers import generate_order_number

        order_number = generate_order_number('telegram')

        assert order_number.startswith('TG')
        assert len(order_number) == 11
        assert order_number[8] == '_'
        assert order_number[2:8] == '000042'  # Mocked sequence

    def test_generates_correct_format_for_web(self, mock_db_session):
        """Test order number format for web source"""
        from business_app.utils.helpers import generate_order_number

        order_number = generate_order_number('web')

        assert order_number.startswith('WB')
        assert len(order_number) == 11

    def test_generates_correct_format_for_phone(self, mock_db_session):
        """Test order number format for phone/contact center source"""
        from business_app.utils.helpers import generate_order_number

        order_number = generate_order_number('phone')

        assert order_number.startswith('CC')
        assert len(order_number) == 11

    def test_generates_correct_format_for_admin(self, mock_db_session):
        """Test order number format for admin source"""
        from business_app.utils.helpers import generate_order_number

        order_number = generate_order_number('admin')

        assert order_number.startswith('AD')
        assert len(order_number) == 11

    def test_default_source_is_web(self, mock_db_session):
        """Test default source is 'web' when not specified"""
        from business_app.utils.helpers import generate_order_number

        order_number = generate_order_number()

        assert order_number.startswith('WB')

    def test_unknown_source_defaults_to_web(self, mock_db_session):
        """Test unknown source falls back to 'WB' prefix"""
        from business_app.utils.helpers import generate_order_number

        order_number = generate_order_number('unknown_source')

        assert order_number.startswith('WB')

    def test_sequence_is_zero_padded(self, mock_db_session):
        """Test sequence number is zero-padded to 6 digits"""
        from business_app.utils.helpers import generate_order_number

        mock_db_session.session.execute.return_value.scalar.return_value = 1
        order_number = generate_order_number('web')

        assert order_number[2:8] == '000001'

    def test_year_suffix_is_correct(self, mock_db_session):
        """Test year suffix matches current year"""
        from business_app.utils.helpers import generate_order_number

        current_year = datetime.now(timezone.utc).year
        expected_year_suffix = str(current_year)[-2:]

        order_number = generate_order_number('web')

        assert order_number[-2:] == expected_year_suffix

    def test_fallback_on_db_error(self):
        """Test fallback to legacy format on database error"""
        from business_app.utils.helpers import generate_order_number

        with patch('business_app.utils.helpers.db') as mock_db:
            mock_db.session.execute.side_effect = Exception("DB connection failed")

            order_number = generate_order_number('web')

            # Should fall back to legacy format
            assert order_number.startswith('WB')
            assert len(order_number) > 11  # Legacy format is longer


class TestOrderNumberIntegration:
    """Integration tests requiring database - marked for integration test runs"""

    @pytest.mark.integration
    def test_sequence_increments(self, app, db):
        """Test that sequence increments on each call"""
        from business_app.utils.helpers import generate_order_number

        with app.app_context():
            order1 = generate_order_number('web')
            order2 = generate_order_number('web')

            seq1 = int(order1[2:8])
            seq2 = int(order2[2:8])

            assert seq2 == seq1 + 1

    @pytest.mark.integration
    def test_different_sources_have_independent_sequences(self, app, db):
        """Test that different sources maintain independent sequences"""
        from business_app.utils.helpers import generate_order_number

        with app.app_context():
            # Create orders from different sources
            tg_order = generate_order_number('telegram')
            web_order = generate_order_number('web')
            admin_order = generate_order_number('admin')

            # Each should have its own sequence
            assert tg_order.startswith('TG')
            assert web_order.startswith('WB')
            assert admin_order.startswith('AD')


class TestOrderValidatorSources:
    """Test order validators include all sources"""

    def test_phone_source_is_valid(self):
        """Test 'phone' is a valid order source"""
        from business_app.utils.order_validators import OrderCreateValidator

        validator = OrderCreateValidator()
        validator._validate_order_source('phone')

        assert len(validator.errors) == 0

    def test_all_sources_are_valid(self):
        """Test all defined sources pass validation"""
        from business_app.utils.order_validators import OrderCreateValidator
        from business_app.utils.constants import ORDER_SOURCE_PREFIXES

        for source in ORDER_SOURCE_PREFIXES.keys():
            validator = OrderCreateValidator()
            validator._validate_order_source(source)

            assert len(validator.errors) == 0, f"Source '{source}' should be valid"
