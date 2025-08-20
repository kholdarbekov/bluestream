"""
Unit tests for Payment Service - Critical Business Logic
Tests payment processing, validation, and security
"""
import pytest
from decimal import Decimal
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, UTC

from business_app.services.payment_service import PaymentService
from business_app.models.payment import Payment
from business_app.models.order import Order
from business_app.utils.constants import PaymentStatus, PaymentMethod
from business_app.utils.exceptions import PaymentError, ValidationError


@pytest.fixture
def payment_service():
    """Create PaymentService instance"""
    return PaymentService()


@pytest.mark.critical
@pytest.mark.payment
class TestPaymentProcessing:
    """Test critical payment processing logic"""
    
    def test_create_payment_valid_data(self, payment_service, sample_order, db):
        """Test creating payment with valid data"""
        payment_data = {
            'order_id': sample_order.id,
            'payment_method': 'card',
            'amount': Decimal('18000.00'),
            'currency': 'UZS'
        }
        
        with patch.object(payment_service, '_generate_payment_id', return_value='PAY123'):
            payment = payment_service.create_payment(payment_data)
            
            assert payment.order_id == sample_order.id
            assert payment.amount == Decimal('18000.00')
            assert payment.currency == 'UZS'
            assert payment.status == PaymentStatus.PENDING
            assert payment.payment_id == 'PAY123'
    
    def test_create_payment_invalid_amount(self, payment_service, sample_order):
        """Test payment creation with invalid amount"""
        payment_data = {
            'order_id': sample_order.id,
            'payment_method': 'card',
            'amount': Decimal('-100.00'),  # Negative amount
            'currency': 'UZS'
        }
        
        with pytest.raises(ValidationError, match="Amount must be positive"):
            payment_service.create_payment(payment_data)
    
    def test_create_payment_minimum_amount(self, payment_service, sample_order):
        """Test payment creation below minimum amount"""
        payment_data = {
            'order_id': sample_order.id,
            'payment_method': 'card',
            'amount': Decimal('5000.00'),  # Below minimum
            'currency': 'UZS'
        }
        
        with pytest.raises(ValidationError, match="Amount below minimum"):
            payment_service.create_payment(payment_data)
    
    def test_process_card_payment_success(self, payment_service, sample_payment, db):
        """Test successful card payment processing"""
        with patch.object(payment_service, '_process_card_payment') as mock_process:
            mock_process.return_value = {
                'success': True,
                'transaction_id': 'TXN123',
                'gateway_response': {'status': 'approved'}
            }
            
            result = payment_service.process_payment(sample_payment.id)
            
            assert result['success'] is True
            assert result['transaction_id'] == 'TXN123'
            
            # Verify payment status updated
            db.session.refresh(sample_payment)
            assert sample_payment.status == PaymentStatus.COMPLETED
    
    def test_process_payment_gateway_failure(self, payment_service, sample_payment, db):
        """Test payment processing with gateway failure"""
        with patch.object(payment_service, '_process_card_payment') as mock_process:
            mock_process.return_value = {
                'success': False,
                'error': 'Insufficient funds',
                'gateway_response': {'status': 'declined'}
            }
            
            result = payment_service.process_payment(sample_payment.id)
            
            assert result['success'] is False
            assert 'Insufficient funds' in result['error']
            
            # Verify payment status updated
            db.session.refresh(sample_payment)
            assert sample_payment.status == PaymentStatus.FAILED
    
    def test_process_payment_network_timeout(self, payment_service, sample_payment, db):
        """Test payment processing with network timeout"""
        with patch.object(payment_service, '_process_card_payment') as mock_process:
            mock_process.side_effect = TimeoutError("Gateway timeout")
            
            result = payment_service.process_payment(sample_payment.id)
            
            assert result['success'] is False
            assert 'timeout' in result['error'].lower()
            
            # Payment should remain in pending state for retry
            db.session.refresh(sample_payment)
            assert sample_payment.status == PaymentStatus.PENDING


@pytest.mark.critical
@pytest.mark.payment
class TestPaymentValidation:
    """Test payment validation logic"""
    
    def test_validate_payment_amount_precision(self, payment_service):
        """Test payment amount precision validation"""
        # Valid precision (2 decimal places)
        assert payment_service._validate_amount(Decimal('100.50')) is True
        
        # Invalid precision (3 decimal places)
        with pytest.raises(ValidationError, match="Invalid amount precision"):
            payment_service._validate_amount(Decimal('100.123'))
    
    def test_validate_currency_support(self, payment_service):
        """Test currency validation"""
        # Supported currency
        assert payment_service._validate_currency('UZS') is True
        
        # Unsupported currency
        with pytest.raises(ValidationError, match="Unsupported currency"):
            payment_service._validate_currency('EUR')
    
    def test_validate_payment_method(self, payment_service):
        """Test payment method validation"""
        # Valid payment methods
        valid_methods = ['card', 'payme', 'click', 'cash']
        for method in valid_methods:
            assert payment_service._validate_payment_method(method) is True
        
        # Invalid payment method
        with pytest.raises(ValidationError, match="Invalid payment method"):
            payment_service._validate_payment_method('bitcoin')
    
    def test_validate_card_token_format(self, payment_service):
        """Test card token format validation"""
        # Valid token format
        valid_token = 'card_1234567890abcdef'
        assert payment_service._validate_card_token(valid_token) is True
        
        # Invalid token format
        with pytest.raises(ValidationError, match="Invalid card token format"):
            payment_service._validate_card_token('invalid_token')


@pytest.mark.critical
@pytest.mark.payment
class TestPaymentSecurity:
    """Test payment security features"""
    
    def test_payment_signature_validation(self, payment_service):
        """Test payment signature validation"""
        payment_data = {
            'amount': '18000.00',
            'currency': 'UZS',
            'order_id': '123'
        }
        
        # Valid signature
        valid_signature = payment_service._generate_payment_signature(payment_data)
        assert payment_service._validate_payment_signature(payment_data, valid_signature) is True
        
        # Invalid signature
        invalid_signature = 'invalid_signature'
        assert payment_service._validate_payment_signature(payment_data, invalid_signature) is False
    
    def test_payment_idempotency(self, payment_service, sample_order, db):
        """Test payment idempotency to prevent duplicate charges"""
        payment_data = {
            'order_id': sample_order.id,
            'payment_method': 'card',
            'amount': Decimal('18000.00'),
            'currency': 'UZS',
            'idempotency_key': 'unique_key_123'
        }
        
        # First payment creation should succeed
        with patch.object(payment_service, '_generate_payment_id', return_value='PAY123'):
            payment1 = payment_service.create_payment(payment_data)
            assert payment1 is not None
        
        # Second payment with same idempotency key should return existing payment
        payment2 = payment_service.create_payment(payment_data)
        assert payment2.id == payment1.id
    
    def test_sensitive_data_masking(self, payment_service):
        """Test that sensitive payment data is properly masked in logs"""
        card_data = {
            'card_number': '4111111111111111',
            'cvv': '123',
            'expiry_month': '12',
            'expiry_year': '2025'
        }
        
        masked_data = payment_service._mask_sensitive_data(card_data)
        
        assert masked_data['card_number'] == '4111****1111'
        assert masked_data['cvv'] == '***'
        assert masked_data['expiry_month'] == '12'  # Not sensitive
        assert masked_data['expiry_year'] == '2025'  # Not sensitive


@pytest.mark.critical
@pytest.mark.payment
class TestRefundProcessing:
    """Test refund processing logic"""
    
    def test_full_refund_success(self, payment_service, sample_payment, db):
        """Test successful full refund"""
        # Set payment as completed first
        sample_payment.status = PaymentStatus.COMPLETED
        sample_payment.transaction_id = 'TXN123'
        db.session.commit()
        
        with patch.object(payment_service, '_process_refund') as mock_refund:
            mock_refund.return_value = {
                'success': True,
                'refund_id': 'REF123',
                'refund_amount': sample_payment.amount
            }
            
            result = payment_service.refund_payment(
                sample_payment.id, 
                sample_payment.amount, 
                'Customer requested refund'
            )
            
            assert result['success'] is True
            assert result['refund_amount'] == sample_payment.amount
            
            # Verify payment status updated
            db.session.refresh(sample_payment)
            assert sample_payment.status == PaymentStatus.REFUNDED
    
    def test_partial_refund_success(self, payment_service, sample_payment, db):
        """Test successful partial refund"""
        sample_payment.status = PaymentStatus.COMPLETED
        sample_payment.transaction_id = 'TXN123'
        db.session.commit()
        
        refund_amount = Decimal('5000.00')  # Partial refund
        
        with patch.object(payment_service, '_process_refund') as mock_refund:
            mock_refund.return_value = {
                'success': True,
                'refund_id': 'REF123',
                'refund_amount': refund_amount
            }
            
            result = payment_service.refund_payment(
                sample_payment.id, 
                refund_amount, 
                'Partial refund requested'
            )
            
            assert result['success'] is True
            assert result['refund_amount'] == refund_amount
            
            # Payment should remain completed for partial refunds
            db.session.refresh(sample_payment)
            assert sample_payment.status == PaymentStatus.COMPLETED
            assert sample_payment.refund_amount == refund_amount
    
    def test_refund_invalid_amount(self, payment_service, sample_payment, db):
        """Test refund with invalid amount"""
        sample_payment.status = PaymentStatus.COMPLETED
        db.session.commit()
        
        # Refund amount exceeding payment amount
        excessive_amount = sample_payment.amount + Decimal('1000.00')
        
        with pytest.raises(ValidationError, match="Refund amount exceeds payment amount"):
            payment_service.refund_payment(sample_payment.id, excessive_amount, 'Invalid refund')
    
    def test_refund_pending_payment(self, payment_service, sample_payment):
        """Test refund of pending payment"""
        # Payment is still pending
        assert sample_payment.status == PaymentStatus.PENDING
        
        with pytest.raises(PaymentError, match="Cannot refund pending payment"):
            payment_service.refund_payment(sample_payment.id, Decimal('1000.00'), 'Invalid refund')


@pytest.mark.critical
@pytest.mark.payment
class TestPaymentGatewayIntegration:
    """Test payment gateway integration logic"""
    
    def test_payme_integration(self, payment_service, sample_payment):
        """Test Payme gateway integration"""
        sample_payment.provider = 'payme'
        
        with patch.object(payment_service, '_call_payme_api') as mock_api:
            mock_api.return_value = {
                'result': {
                    'state': 2,  # Completed state
                    'transaction': '123456789'
                }
            }
            
            result = payment_service._process_payme_payment(sample_payment)
            
            assert result['success'] is True
            assert result['transaction_id'] == '123456789'
    
    def test_click_integration(self, payment_service, sample_payment):
        """Test Click gateway integration"""
        sample_payment.provider = 'click'
        
        with patch.object(payment_service, '_call_click_api') as mock_api:
            mock_api.return_value = {
                'error': 0,
                'error_note': 'Success',
                'click_trans_id': '987654321'
            }
            
            result = payment_service._process_click_payment(sample_payment)
            
            assert result['success'] is True
            assert result['transaction_id'] == '987654321'
    
    def test_gateway_fallback(self, payment_service, sample_payment, db):
        """Test gateway fallback on primary gateway failure"""
        sample_payment.provider = 'payme'
        
        with patch.object(payment_service, '_process_payme_payment') as mock_payme:
            with patch.object(payment_service, '_process_card_payment') as mock_card:
                # Primary gateway fails
                mock_payme.side_effect = Exception("Payme gateway down")
                
                # Fallback gateway succeeds
                mock_card.return_value = {
                    'success': True,
                    'transaction_id': 'CARD123'
                }
                
                result = payment_service.process_payment_with_fallback(sample_payment.id)
                
                assert result['success'] is True
                assert result['transaction_id'] == 'CARD123'
                assert result['used_fallback'] is True


@pytest.mark.critical
@pytest.mark.payment
class TestPaymentCalculations:
    """Test payment amount calculations"""
    
    def test_calculate_payment_fees(self, payment_service):
        """Test payment fee calculations"""
        # Card payment fee (2.5%)
        card_amount = Decimal('10000.00')
        card_fee = payment_service._calculate_payment_fee(card_amount, 'card')
        assert card_fee == Decimal('250.00')
        
        # Payme payment fee (1%)
        payme_fee = payment_service._calculate_payment_fee(card_amount, 'payme')
        assert payme_fee == Decimal('100.00')
        
        # Cash payment (no fee)
        cash_fee = payment_service._calculate_payment_fee(card_amount, 'cash')
        assert cash_fee == Decimal('0.00')
    
    def test_calculate_total_with_fees(self, payment_service):
        """Test total amount calculation including fees"""
        base_amount = Decimal('10000.00')
        
        # Card payment total
        card_total = payment_service._calculate_total_with_fees(base_amount, 'card')
        expected_card_total = base_amount + Decimal('250.00')  # 2.5% fee
        assert card_total == expected_card_total
        
        # Cash payment total
        cash_total = payment_service._calculate_total_with_fees(base_amount, 'cash')
        assert cash_total == base_amount  # No fee
    
    def test_currency_conversion(self, payment_service):
        """Test currency conversion if multiple currencies supported"""
        usd_amount = Decimal('10.00')
        
        with patch.object(payment_service, '_get_exchange_rate', return_value=Decimal('12000.00')):
            uzs_amount = payment_service._convert_currency(usd_amount, 'USD', 'UZS')
            assert uzs_amount == Decimal('120000.00')


@pytest.mark.performance
@pytest.mark.payment
class TestPaymentPerformance:
    """Test payment processing performance"""
    
    def test_payment_processing_time(self, payment_service, sample_payment):
        """Test that payment processing completes within acceptable time"""
        import time
        
        with patch.object(payment_service, '_process_card_payment') as mock_process:
            mock_process.return_value = {'success': True, 'transaction_id': 'TXN123'}
            
            start_time = time.time()
            payment_service.process_payment(sample_payment.id)
            end_time = time.time()
            
            processing_time = end_time - start_time
            assert processing_time < 2.0  # Should complete within 2 seconds
    
    def test_concurrent_payment_processing(self, payment_service, db):
        """Test concurrent payment processing doesn't cause race conditions"""
        import threading
        
        results = []
        
        def process_payment(payment_id):
            try:
                result = payment_service.process_payment(payment_id)
                results.append(result)
            except Exception as e:
                results.append({'error': str(e)})
        
        # Create multiple payments
        payments = []
        for i in range(5):
            payment = Payment(
                order_id=1,
                user_id=1,
                payment_method='card',
                amount=Decimal('1000.00'),
                currency='UZS',
                status=PaymentStatus.PENDING,
                payment_id=f'PAY{i}'
            )
            db.session.add(payment)
            payments.append(payment)
        db.session.commit()
        
        # Process payments concurrently
        threads = []
        for payment in payments:
            thread = threading.Thread(target=process_payment, args=(payment.id,))
            threads.append(thread)
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # Verify all payments processed without conflicts
        assert len(results) == 5
        assert all('error' not in result for result in results)