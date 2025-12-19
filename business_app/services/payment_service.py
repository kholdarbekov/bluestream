"""
Payment service for the Water Business Platform
Supports Payme, Click, Cash, and Loyalty Points payments
"""
import hashlib
import hmac
import json
import random
import uuid
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from flask import current_app, request
import requests
import redis

from business_app.models.order import Order
from business_app.models.payment import Payment, PaymentTransaction, CreditCard
from business_app.models.user import User
from business_app.utils.exceptions import PaymentError, ValidationError, NotFoundError
from business_app.utils.constants import PaymentStatus, PaymentMethod
from business_app.utils.helpers import generate_random_string
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from business_app.utils.card_validation import CardValidator, CardSecurityValidator
from business_app.utils.translations import get_translation
from business_app import db


class PaymentService:
    """Service for handling payment processing"""
    
    def __init__(self):
        # Payme configuration
        self.payme_merchant_id = current_app.config.get('PAYME_MERCHANT_ID')
        self.payme_secret_key = current_app.config.get('PAYME_SECRET_KEY')
        self.payme_endpoint = current_app.config.get('PAYME_ENDPOINT_URL')
        self.payme_test_mode = current_app.config.get('PAYME_TEST_MODE', True)
        
        # Click configuration
        self.click_merchant_id = current_app.config.get('CLICK_MERCHANT_ID')
        self.click_service_id = current_app.config.get('CLICK_SERVICE_ID')
        self.click_secret_key = current_app.config.get('CLICK_SECRET_KEY')
        self.click_endpoint = current_app.config.get('CLICK_ENDPOINT_URL')
        self.click_test_mode = current_app.config.get('CLICK_TEST_MODE', True)
        
        # Webhook replay protection configuration
        self.webhook_tolerance_seconds = current_app.config.get('WEBHOOK_TIMESTAMP_TOLERANCE', 300)  # 5 minutes
        self.webhook_nonce_ttl = current_app.config.get('WEBHOOK_NONCE_TTL', 3600)  # 1 hour
        
        # Initialize Redis for nonce tracking
        try:
            self.redis_client = redis.Redis(
                host=current_app.config.get('REDIS_HOST', 'localhost'),
                port=current_app.config.get('REDIS_PORT', 6379),
                db=current_app.config.get('REDIS_WEBHOOK_DB', 3),
                decode_responses=True
            )
        except Exception as e:
            current_app.logger.warning(f"Redis not available for webhook nonce tracking: {e}")
            self.redis_client = None
    
    def create_payment(self, order_id: int, payment_method: PaymentMethod,
                      amount: int = None, **kwargs) -> Payment:
        """
        Create payment record

        Args:
            order_id: Order ID
            payment_method: Payment method
            amount: Payment amount (defaults to order total)
            **kwargs: Additional payment data

        Returns:
            Payment object

        Raises:
            NotFoundError: If order not found
            ValidationError: If payment data invalid
        """
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError(get_translation('error.not_found'))
        
        # Use order total if amount not specified
        if amount is None:
            amount = order.total_amount
        
        # Validate amount
        if amount <= 0:
            raise ValidationError(get_translation('error.validation.invalid_amount'))

        if amount > order.total_amount:
            raise ValidationError(get_translation('error.validation.amount_exceeds_total'))
        
        # Create payment record
        payment = Payment(
            order_id=order_id,
            user_id=order.user_id,
            payment_method=payment_method,
            amount=amount,
            status=PaymentStatus.PENDING,
            currency='UZS',
            provider_data=kwargs
        )
        
        db.session.add(payment)
        db.session.commit()
        
        return payment
    
    def create_payment_link(self, payment_id: int) -> Dict[str, str]:
        """
        Create payment link for external payment gateways

        Args:
            payment_id: Payment ID

        Returns:
            Dictionary with payment URL and other details
        """
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation('error.not_found'))
        
        if payment.method == PaymentMethod.PAYME:
            return self._create_payme_link(payment)
        elif payment.method == PaymentMethod.CLICK:
            return self._create_click_link(payment)
        else:
            raise PaymentError(get_translation('error.payment.unsupported_method'))
    
    def process_cash_payment(self, payment_id: int, collected_by: int = None) -> Payment:
        """Process cash payment"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation('error.not_found'))

        if payment.method != PaymentMethod.CASH:
            raise ValidationError(get_translation('error.payment.invalid_method'))
        
        # Update payment status
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = datetime.now(timezone.utc)
        payment.collected_by = collected_by
        
        # Create transaction record
        self._create_transaction(payment, 'payment_completed', {
            'collected_by': collected_by,
            'collection_method': 'cash_on_delivery'
        })
        
        db.session.commit()
        
        # Update order status
        self._handle_successful_payment(payment)
        
        return payment
    
    def process_loyalty_points_payment(self, payment_id: int, points_used: int) -> Payment:
        """Process payment using loyalty points"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation('error.not_found'))

        if payment.method != PaymentMethod.LOYALTY_POINTS:
            raise ValidationError(get_translation('error.payment.invalid_method'))

        # Check user points balance
        from .loyalty_service import LoyaltyService
        loyalty_service = LoyaltyService()

        user_points = loyalty_service.get_user_points(payment.user_id)
        if user_points < points_used:
            raise ValidationError(get_translation('api.loyalty.insufficient_points'))

        # Calculate payment amount from points
        from ..utils.helpers import calculate_discount_from_points
        payment_amount = calculate_discount_from_points(points_used)

        if payment_amount < payment.amount:
            raise ValidationError(get_translation('api.loyalty.insufficient_points'))

        # Deduct points
        loyalty_service.deduct_points(
            payment.user_id,
            points_used,
            f"Payment for order #{payment.order.order_number}"
        )

        # Update payment
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = datetime.now(timezone.utc)
        payment.provider_data['points_used'] = points_used

        self._create_transaction(payment, 'payment_completed', {
            'points_used': points_used,
            'points_value': payment_amount
        })

        db.session.commit()

        self._handle_successful_payment(payment)

        return payment

    def process_card_payment(self, order_id: int, card_id: int, user_id: int,
                            amount: Optional[int] = None) -> Payment:
        """
        Process payment using a stored credit card

        Args:
            order_id: Order ID to pay for
            card_id: Saved card ID
            user_id: User ID (for security verification)
            amount: Payment amount (optional, defaults to order total)

        Returns:
            Payment object

        Raises:
            NotFoundError: If order or card not found
            ValidationError: If card is invalid or payment fails
            PaymentError: If payment processing fails
        """
        # Validate order exists
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError(get_translation('error.not_found'))

        # Verify order belongs to user
        if order.user_id != user_id:
            raise ValidationError(get_translation('error.forbidden'))

        # Use order total if amount not specified
        if amount is None:
            amount = order.total_amount

        # Validate card
        is_valid, error_message = self.validate_card_for_payment(card_id, user_id, amount)
        if not is_valid:
            raise ValidationError(error_message)

        # Get card details
        card = self.get_card_by_id(card_id, user_id)

        # Determine payment provider based on card brand
        if card.provider == 'payme':
            payment_method = PaymentMethod.PAYME
        elif card.provider == 'click':
            payment_method = PaymentMethod.CLICK
        else:
            # Default to Payme for international cards
            payment_method = PaymentMethod.PAYME

        # Create payment record
        payment = self.create_payment(
            order_id=order_id,
            payment_method=payment_method,
            amount=amount,
            card_id=card_id,
            card_token=card.card_token,
            card_brand=card.card_brand,
            last_four_digits=card.last_four_digits
        )

        try:
            # Process payment through gateway
            if payment_method == PaymentMethod.PAYME:
                result = self._process_payme_card_payment(payment, card)
            elif payment_method == PaymentMethod.CLICK:
                result = self._process_click_card_payment(payment, card)
            else:
                raise PaymentError(get_translation('error.payment.unsupported_method'))

            # Update payment status based on gateway response
            if result.get('success'):
                payment.status = PaymentStatus.COMPLETED
                payment.paid_at = datetime.now(timezone.utc)
                payment.gateway_reference = result.get('transaction_id')
                payment.gateway_response = result

                # Mark card as verified on first successful payment
                if not card.is_verified:
                    card.is_verified = True

                self._create_transaction(payment, 'payment_completed', result)
                db.session.commit()

                self._handle_successful_payment(payment)

                # Log successful payment
                audit_logger.log_event(
                    event_type=AuditEventType.DATA_MODIFICATION,
                    action="card_payment_successful",
                    severity=AuditSeverity.MEDIUM,
                    resource_type="payment",
                    resource_id=str(payment.id),
                    user_id=user_id,
                    description=f"Card payment successful for order {order_id}",
                    additional_data={
                        'order_id': order_id,
                        'amount': amount,
                        'card_last_four': card.last_four_digits,
                        'payment_method': payment_method.value
                    }
                )
            else:
                payment.status = PaymentStatus.FAILED
                payment.gateway_response = result
                self._create_transaction(payment, 'payment_failed', result)
                db.session.commit()

                # Log failed payment
                audit_logger.log_event(
                    event_type=AuditEventType.SECURITY_EVENT,
                    action="card_payment_failed",
                    severity=AuditSeverity.MEDIUM,
                    resource_type="payment",
                    resource_id=str(payment.id),
                    user_id=user_id,
                    description=f"Card payment failed for order {order_id}",
                    additional_data={
                        'order_id': order_id,
                        'amount': amount,
                        'card_last_four': card.last_four_digits,
                        'error': result.get('error_message')
                    }
                )

                raise PaymentError(result.get('error_message', get_translation('api.payments.failed')))

            return payment

        except Exception as e:
            current_app.logger.error(f"Card payment processing error for order {order_id}: {e}")
            payment.status = PaymentStatus.FAILED
            db.session.commit()
            raise PaymentError(get_translation('api.payments.failed'))

    def _process_payme_card_payment(self, payment: Payment, card: CreditCard) -> Dict[str, Any]:
        """
        Process card payment through Payme gateway
        (Simplified implementation - integrate with actual Payme card API)

        Args:
            payment: Payment object
            card: CreditCard object

        Returns:
            Dict with success status and transaction details
        """
        try:
            # Payme Subscribe API: Charge saved card
            # Step 1: Create receipt (invoice)
            amount_tiyin = int(payment.amount * 100)
            create_receipt = self._payme_request('receipts.create', {
                'amount': amount_tiyin,
                'account': {
                    'order_id': str(payment.order_id)
                }
            })
            
            if 'error' in create_receipt:
                 raise PaymentError(f"Failed to create Payme receipt: {create_receipt['error'].get('message')}")
                 
            receipt_id = create_receipt['result']['receipt']['_id']
            
            # Step 2: Pay receipt with card token (Verify)
            pay_receipt = self._payme_request('receipts.pay', {
                'id': receipt_id,
                'token': card.card_token
            })
            
            if 'error' in pay_receipt:
                raise PaymentError(f"Failed to process card payment: {pay_receipt['error'].get('message')}")

            # Return success
            return {
                'success': True,
                'transaction_id': receipt_id,
                'amount': payment.amount,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'provider_response': pay_receipt['result']
            }

        except Exception as e:
            current_app.logger.error(f"Payme card payment error: {e}")
            return {
                'success': False,
                'error_message': str(e)
            }

    def _payme_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send request to Payme Subscribe API
        
        Args:
            method: JSON-RPC method name (e.g., 'receipts.create')
            params: Parameters for the method
            
        Returns:
            Dict: JSON-RPC response
        """
        try:
             # Subscribe API uses X-Auth headers
             print(f"Payme: method: {method}, url: {self.payme_endpoint}, {self.payme_merchant_id}:{self.payme_secret_key}")
             print(f"payme_secret_key: {self.payme_secret_key}")
             print(f"payme_merchant_id: {self.payme_merchant_id}")
             print(f"params: {params}")
             headers = {
                 'X-Auth': f"{self.payme_merchant_id}:{self.payme_secret_key}",
                 'Content-Type': 'application/json'
             }
             
             payload = {
                 'method': method,
                 'params': params,
                 'id': random.randint(1, 1000000),
                 'jsonrpc': '2.0'
             }
             
             response = requests.post(
                 self.payme_endpoint, # Typically ends with /api for Subscribe API
                 json=payload,
                 headers=headers,
                 timeout=30
             )
             
             return response.json()
        except Exception as e:
             current_app.logger.error(f"Payme API request error ({method}): {e}")
             return {'error': {'code': -1, 'message': str(e)}}

    def _process_click_card_payment(self, payment: Payment, card: CreditCard) -> Dict[str, Any]:
        """
        Process card payment through Click gateway
        (Simplified implementation - integrate with actual Click card API)

        Args:
            payment: Payment object
            card: CreditCard object

        Returns:
            Dict with success status and transaction details
        """
        try:
            # In production, integrate with Click's card payment API
            # This is a simplified placeholder
            current_app.logger.info(f"Processing Click card payment for payment {payment.id}")

            # Simulate payment processing
            # In real implementation, call Click API with card token
            if self.click_test_mode:
                # Test mode - always succeed for non-test cards
                return {
                    'success': True,
                    'transaction_id': f"click_{int(datetime.now(timezone.utc).timestamp())}",
                    'amount': payment.amount,
                    'card_token': card.card_token,
                    'timestamp': datetime.now(timezone.utc).isoformat()
                }
            else:
                # Production mode - actual API call would go here
                raise NotImplementedError("Click card payment API integration required")

        except Exception as e:
            current_app.logger.error(f"Click card payment error: {e}")
            return {
                'success': False,
                'error_message': str(e)
            }
    
    def handle_payme_webhook(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme webhook"""
        try:
            # Verify webhook signature
            if not self._verify_payme_signature(webhook_data):
                raise PaymentError(get_translation('error.payment.invalid_signature'))
            
            method = webhook_data.get('method')
            params = webhook_data.get('params', {})
            
            if method == 'CheckPerformTransaction':
                return self._payme_check_perform_transaction(params)
            elif method == 'CreateTransaction':
                return self._payme_create_transaction(params)
            elif method == 'PerformTransaction':
                return self._payme_perform_transaction(params)
            elif method == 'CancelTransaction':
                return self._payme_cancel_transaction(params)
            elif method == 'CheckTransaction':
                return self._payme_check_transaction(params)
            else:
                raise PaymentError(get_translation('error.payment.unknown_method'))
                
        except Exception as e:
            current_app.logger.error(f"Payme webhook error: {e}")
            return {'error': {'code': -32400, 'message': 'Bad Request'}}
    
    def handle_click_webhook(self, webhook_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Click webhook"""
        try:
            # Verify webhook signature
            if not self._verify_click_signature(webhook_data):
                raise PaymentError(get_translation('error.payment.invalid_signature'))
            
            action = webhook_data.get('action')
            
            if action == 'prepare':
                return self._click_prepare(webhook_data)
            elif action == 'complete':
                return self._click_complete(webhook_data)
            else:
                raise PaymentError(get_translation('error.payment.unknown_action'))
                
        except Exception as e:
            current_app.logger.error(f"Click webhook error: {e}")
            return {'error': -1, 'error_note': 'Bad Request'}
    
    def process_refund(self, payment_id: int, amount: int, reason: str = None) -> bool:
        """Process payment refund"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation('error.not_found'))

        if payment.status != PaymentStatus.COMPLETED:
            raise ValidationError(get_translation('error.payment.cannot_refund'))

        if amount > payment.amount:
            raise ValidationError(get_translation('error.validation.amount_exceeds_total'))
        
        # Process refund based on payment method
        if payment.method in [PaymentMethod.PAYME, PaymentMethod.CLICK]:
            success = self._process_gateway_refund(payment, amount, reason)
        elif payment.method == PaymentMethod.LOYALTY_POINTS:
            success = self._process_points_refund(payment, amount, reason)
        else:
            # Cash refund - manual process
            success = True
        
        if success:
            # Update payment status
            if amount == payment.amount:
                payment.status = PaymentStatus.REFUNDED
            else:
                payment.status = PaymentStatus.PARTIALLY_REFUNDED
            
            payment.refunded_amount = (payment.refunded_amount or 0) + amount
            payment.refunded_at = datetime.now(timezone.utc)
            
            # Create transaction record
            self._create_transaction(payment, 'refund_processed', {
                'refund_amount': amount,
                'reason': reason
            })
            
            db.session.commit()
        
        return success
    
    def validate_webhook_signature(self, provider: str, request) -> bool:
        """
        Validate webhook signature and implement replay protection
        
        Args:
            provider: Payment provider (payme, click)
            request: Flask request object
        
        Returns:
            bool: True if valid and not replayed, False otherwise
        """
        try:
            # Step 1: Basic signature validation based on provider
            if provider.lower() == 'payme':
                signature_valid = self._validate_payme_webhook_signature(request)
            elif provider.lower() == 'click':
                signature_valid = self._validate_click_webhook_signature(request)
            else:
                current_app.logger.error(f"Unknown webhook provider: {provider}")
                return False
            
            if not signature_valid:
                audit_logger.log_event(
                    event_type=AuditEventType.SECURITY_EVENT,
                    action="webhook_signature_validation_failed",
                    severity=AuditSeverity.HIGH,
                    resource_type="payment_webhook",
                    description=f"Invalid webhook signature from {provider}",
                    additional_data={
                        'provider': provider,
                        'remote_addr': request.remote_addr,
                        'user_agent': request.headers.get('User-Agent'),
                        'content_length': request.headers.get('Content-Length')
                    }
                )
                return False
            
            # Step 2: Replay protection validation
            if not self._validate_webhook_replay_protection(provider, request):
                audit_logger.log_event(
                    event_type=AuditEventType.SECURITY_EVENT,
                    action="webhook_replay_attack_detected",
                    severity=AuditSeverity.CRITICAL,
                    resource_type="payment_webhook",
                    description=f"Webhook replay attack detected from {provider}",
                    additional_data={
                        'provider': provider,
                        'remote_addr': request.remote_addr,
                        'user_agent': request.headers.get('User-Agent'),
                        'timestamp': request.headers.get('X-Timestamp'),
                        'nonce': request.headers.get('X-Nonce')
                    }
                )
                return False
            
            # Step 3: Log successful validation
            audit_logger.log_event(
                event_type=AuditEventType.DATA_ACCESS,
                action="webhook_validation_successful",
                severity=AuditSeverity.LOW,
                resource_type="payment_webhook",
                description=f"Webhook validation successful for {provider}",
                additional_data={
                    'provider': provider,
                    'remote_addr': request.remote_addr
                }
            )
            
            return True
            
        except Exception as e:
            current_app.logger.error(f"Webhook validation error for {provider}: {e}")
            audit_logger.log_event(
                event_type=AuditEventType.SECURITY_EVENT,
                action="webhook_validation_error",
                severity=AuditSeverity.HIGH,
                resource_type="payment_webhook",
                description=f"Webhook validation error for {provider}: {str(e)}",
                additional_data={
                    'provider': provider,
                    'error': str(e)
                }
            )
            return False
    
    def _validate_webhook_replay_protection(self, provider: str, request) -> bool:
        """
        Validate webhook replay protection using timestamp and nonce
        
        Args:
            provider: Payment provider
            request: Flask request object
        
        Returns:
            bool: True if not a replay, False if replay detected
        """
        try:
            # Extract timestamp and nonce from headers or body
            timestamp = None
            nonce = None
            
            if provider.lower() == 'payme':
                # For Payme, we'll use request time and generate nonce from request data
                body = request.get_json() or {}
                timestamp = body.get('timestamp') or request.headers.get('X-Timestamp')
                nonce = body.get('nonce') or request.headers.get('X-Nonce')
                
                # If no timestamp/nonce provided, generate from request data for basic protection
                if not timestamp:
                    timestamp = str(int(time.time()))
                if not nonce:
                    # Generate deterministic nonce from request content
                    content_hash = hashlib.sha256(
                        json.dumps(body, sort_keys=True).encode('utf-8')
                    ).hexdigest()[:16]
                    nonce = f"payme_{content_hash}_{timestamp}"
                    
            elif provider.lower() == 'click':
                # For Click, extract from form data or headers
                timestamp = request.form.get('timestamp') or request.headers.get('X-Timestamp')
                nonce = request.form.get('nonce') or request.headers.get('X-Nonce')
                
                # If no timestamp/nonce provided, generate from request data
                if not timestamp:
                    timestamp = str(int(time.time()))
                if not nonce:
                    # Generate deterministic nonce from form data
                    form_data = dict(request.form)
                    form_data.pop('timestamp', None)
                    form_data.pop('nonce', None)
                    content_hash = hashlib.sha256(
                        json.dumps(form_data, sort_keys=True).encode('utf-8')
                    ).hexdigest()[:16]
                    nonce = f"click_{content_hash}_{timestamp}"
            
            # Validate timestamp (not too old, not from future)
            try:
                webhook_time = int(timestamp)
                current_time = int(time.time())
                time_diff = abs(current_time - webhook_time)
                
                if time_diff > self.webhook_tolerance_seconds:
                    current_app.logger.warning(
                        f"Webhook timestamp too old or too new: {time_diff}s difference (tolerance: {self.webhook_tolerance_seconds}s)"
                    )
                    return False
                    
            except (ValueError, TypeError):
                current_app.logger.warning(f"Invalid webhook timestamp format: {timestamp}")
                return False
            
            # Check nonce for replay protection (if Redis available)
            if self.redis_client and nonce:
                nonce_key = f"webhook_nonce:{provider}:{nonce}"
                
                # Check if nonce already exists
                if self.redis_client.exists(nonce_key):
                    current_app.logger.warning(f"Webhook nonce replay detected: {nonce}")
                    return False
                
                # Store nonce with TTL
                self.redis_client.setex(nonce_key, self.webhook_nonce_ttl, "1")
            
            return True
            
        except Exception as e:
            current_app.logger.error(f"Replay protection validation error: {e}")
            # In case of error, be conservative and reject
            return False
    
    def _validate_payme_webhook_signature(self, request) -> bool:
        """
        Validate Payme webhook signature with enhanced security
        """
        try:
            # Use the existing method but with additional validation
            data = request.get_json() or {}
            
            # Basic signature validation using existing method
            basic_validation = self._verify_payme_signature(data)
            if not basic_validation:
                return False
            
            # Additional IP validation (if configured)
            allowed_ips = current_app.config.get('PAYME_WEBHOOK_IPS', [])
            if allowed_ips and request.remote_addr not in allowed_ips:
                current_app.logger.warning(f"Webhook from unauthorized IP: {request.remote_addr}")
                return False
            
            # Validate request content-type
            if request.content_type != 'application/json':
                current_app.logger.warning(f"Invalid content-type for Payme webhook: {request.content_type}")
                return False
            
            return True
            
        except Exception as e:
            current_app.logger.error(f"Payme webhook signature validation error: {e}")
            return False
    
    def _validate_click_webhook_signature(self, request) -> bool:
        """
        Validate Click webhook signature with enhanced security
        """
        try:
            # Use the existing method but with additional validation
            data = dict(request.form) if request.form else request.get_json() or {}
            
            # Basic signature validation using existing method
            basic_validation = self._verify_click_signature(data)
            if not basic_validation:
                return False
            
            # Additional IP validation (if configured)
            allowed_ips = current_app.config.get('CLICK_WEBHOOK_IPS', [])
            if allowed_ips and request.remote_addr not in allowed_ips:
                current_app.logger.warning(f"Webhook from unauthorized IP: {request.remote_addr}")
                return False
            
            # Validate request content-type
            valid_content_types = ['application/x-www-form-urlencoded', 'application/json']
            if request.content_type not in valid_content_types:
                current_app.logger.warning(f"Invalid content-type for Click webhook: {request.content_type}")
                return False
            
            return True
            
        except Exception as e:
            current_app.logger.error(f"Click webhook signature validation error: {e}")
            return False
    
    def get_payment_status(self, payment_id: int) -> Dict[str, Any]:
        """Get payment status and details"""
        payment = Payment.query.get(payment_id)
        if not payment:
            raise NotFoundError(get_translation('error.not_found'))
        
        return {
            'id': payment.id,
            'status': payment.status.value,
            'method': payment.method.value,
            'amount': payment.amount,
            'currency': payment.currency,
            'payment_id': payment.payment_id,
            'created_at': payment.created_at.isoformat(),
            'paid_at': payment.paid_at.isoformat() if payment.paid_at else None,
            'gateway_reference': payment.gateway_reference,
            'gateway_response': payment.gateway_response,
            'refunded_amount': payment.refunded_amount or 0,
            'refunded_at': payment.refunded_at.isoformat() if payment.refunded_at else None,
            'transactions': [
                {
                    'id': tx.id,
                    'type': tx.transaction_type,
                    'amount': tx.amount,
                    'status': tx.status,
                    'created_at': tx.created_at.isoformat(),
                    'gateway_response': tx.gateway_response
                }
                for tx in payment.transactions
            ]
        }
    
    # Private methods for Payme integration
        """Create Payme payment link"""
        import base64
        
        params = f"m={self.payme_merchant_id};ac.order_id={payment.order_id};a={int(payment.amount * 100)}"
        encoded_params = base64.b64encode(params.encode('utf-8')).decode('utf-8')
        payment_url = f"{self.payme_endpoint}/{encoded_params}"
        
        return {
            'payment_url': payment_url,
            'reference': payment.payment_id,
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        }
    
    def _verify_payme_signature(self, data: Dict[str, Any]) -> bool:
        """Verify Payme webhook signature"""
        if not self.payme_secret_key:
            current_app.logger.error("Payme secret key not configured")
            return False
        
        # Extract headers for signature verification
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Basic '):
            current_app.logger.warning("Missing or invalid Authorization header for Payme webhook")
            return False
        
        try:
            import base64
            # Decode the basic auth header
            encoded_credentials = auth_header[6:]  # Remove "Basic "
            decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8')
            username, password = decoded_credentials.split(':', 1)
            
            # Verify credentials against merchant ID and secret key
            expected_username = self.payme_merchant_id
            expected_password = self.payme_secret_key
            
            if username != expected_username or password != expected_password:
                current_app.logger.warning(f"Invalid Payme webhook credentials. Username: {username}")
                return False
            
            return True
            
        except Exception as e:
            current_app.logger.error(f"Failed to verify Payme signature: {e}")
            return False
    
    def _payme_check_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CheckPerformTransaction"""
        account = params.get('account', {})
        order_id = account.get('order_id')
        amount = params.get('amount')
        
        if not order_id:
            return {'error': {'code': -31001, 'message': 'Order not found'}}
        
        order = Order.query.get(order_id)
        if not order or order.total_amount * 100 != amount:  # Payme uses tiyin
            return {'error': {'code': -31001, 'message': 'Invalid order'}}
        
        return {'result': {'allow': True}}
    
    def _payme_create_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CreateTransaction"""
        account = params.get('account', {})
        order_id = account.get('order_id')
        amount = params.get('amount')
        transaction_id = params.get('id')
        
        # Find or create payment
        payment = Payment.query.filter_by(
            order_id=order_id,
            method=PaymentMethod.PAYME
        ).first()
        
        if not payment:
            payment = self.create_payment(order_id, PaymentMethod.PAYME, amount // 100)
        
        # Create transaction record
        transaction = self._create_transaction(payment, 'created', {
            'gateway_transaction_id': transaction_id,
            'amount': amount
        })
        
        return {
            'result': {
                'create_time': int(transaction.created_at.timestamp() * 1000),
                'transaction': str(transaction.id),
                'state': 1  # State 1: Transaction created
            }
        }

    def _payme_perform_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme PerformTransaction"""
        transaction_id = params.get('id')
        
        # Check if transaction exists
        # In this simplistic model, we look up by Payme's ID (which we stored as gateway_transaction_id)
        # However, typically Payme expects us to use our own ID or tracking.
        # Based on Payme docs: 'id' is their transaction ID.
        
        transaction = PaymentTransaction.query.filter_by(gateway_transaction_id=transaction_id).first()
        if not transaction:
            return {'error': {'code': -31003, 'message': 'Transaction not found'}}
            
        payment = transaction.payment
        
        if payment.status == PaymentStatus.COMPLETED:
             return {
                'result': {
                    'transaction': str(transaction.id),
                    'perform_time': int(transaction.processed_at.timestamp() * 1000) if transaction.processed_at else int(datetime.now(timezone.utc).timestamp() * 1000),
                    'state': 2 # State 2: Transaction completed
                }
            }
            
        if payment.status == PaymentStatus.CANCELLED:
             return {'error': {'code': -31008, 'message': 'Transaction cancelled'}}

        # Mark as paid
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = datetime.now(timezone.utc)
        
        transaction.status = 'completed'
        transaction.processed_at = payment.paid_at
        
        db.session.commit()
        
        self._handle_successful_payment(payment)
        
        return {
            'result': {
                'transaction': str(transaction.id),
                'perform_time': int(transaction.processed_at.timestamp() * 1000),
                'state': 2
            }
        }

    def _payme_cancel_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CancelTransaction"""
        transaction_id = params.get('id')
        reason = params.get('reason')
        
        transaction = PaymentTransaction.query.filter_by(gateway_transaction_id=transaction_id).first()
        if not transaction:
            return {'error': {'code': -31003, 'message': 'Transaction not found'}}
            
        payment = transaction.payment
        
        if payment.status == PaymentStatus.COMPLETED:
             # Check if reversible
             if not self.process_refund(payment.id, payment.amount, f"Payme Cancel: {reason}"):
                 return {'error': {'code': -31007, 'message': 'Could not cancel transaction'}}
             return {
                 'result': {
                     'transaction': str(transaction.id),
                     'cancel_time': int(datetime.now(timezone.utc).timestamp() * 1000),
                     'state': -2
                 }
             }

        payment.status = PaymentStatus.CANCELLED
        transaction.status = 'cancelled'
        db.session.commit()
        
        return {
            'result': {
                'transaction': str(transaction.id),
                'cancel_time': int(datetime.now(timezone.utc).timestamp() * 1000),
                'state': -1
            }
        }

    def _payme_check_transaction(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Payme CheckTransaction"""
        transaction_id = params.get('id')
        transaction = PaymentTransaction.query.filter_by(gateway_transaction_id=transaction_id).first()
        
        if not transaction:
             return {'error': {'code': -31003, 'message': 'Transaction not found'}}
             
        payment = transaction.payment
        
        # Determine state
        state = 0
        if payment.status == PaymentStatus.PENDING:
            state = 1
        elif payment.status == PaymentStatus.COMPLETED:
            state = 2
        elif payment.status == PaymentStatus.CANCELLED:
            state = -1
        elif payment.status == PaymentStatus.REFUNDED:
            state = -2
            
        return {
            'result': {
                'create_time': int(transaction.created_at.timestamp() * 1000),
                'perform_time': int(transaction.processed_at.timestamp() * 1000) if transaction.processed_at else 0,
                'cancel_time': 0, # Should store cancel time if cancelled
                'transaction': str(transaction.id),
                'state': state,
                'reason': None
            }
        }
    
    # Private methods for Click integration
    def _create_click_link(self, payment: Payment) -> Dict[str, str]:
        """Create Click payment link"""
        base_url = current_app.config.get('COMPANY_WEBSITE', 'http://localhost:5000')
        params = {
            'service_id': self.click_service_id,
            'merchant_id': self.click_merchant_id,
            'amount': payment.amount,
            'transaction_param': payment.payment_id,
            'return_url': f"{base_url}/payment/success?order_id={payment.order_id}",
            'cancel_url': f"{base_url}/payment/cancel?order_id={payment.order_id}"
        }

        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        payment_url = f"{self.click_endpoint}?{query_string}"

        return {
            'payment_url': payment_url,
            'reference': payment.payment_id,
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        }
    
    def _verify_click_signature(self, data: Dict[str, Any]) -> bool:
        """Verify Click webhook signature"""
        if not self.click_secret_key:
            current_app.logger.error("Click secret key not configured")
            return False
        
        try:
            # Extract signature verification parameters
            click_trans_id = data.get('click_trans_id', '')
            service_id = data.get('service_id', '')
            merchant_trans_id = data.get('merchant_trans_id', '')
            amount = data.get('amount', '')
            action = data.get('action', '')
            sign_time = data.get('sign_time', '')
            sign_string = data.get('sign_string', '')
            
            # Build signature string according to Click specification
            # Format: click_trans_id + service_id + secret_key + merchant_trans_id + amount + action + sign_time
            signature_data = f"{click_trans_id}{service_id}{self.click_secret_key}{merchant_trans_id}{amount}{action}{sign_time}"
            
            # Calculate MD5 hash
            expected_signature = hashlib.md5(signature_data.encode('utf-8')).hexdigest()
            
            if sign_string != expected_signature:
                current_app.logger.warning(f"Invalid Click webhook signature. Expected: {expected_signature}, Got: {sign_string}")
                return False
            
            return True
            
        except Exception as e:
            current_app.logger.error(f"Failed to verify Click signature: {e}")
            return False
    
    def _click_prepare(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Click prepare request"""
        transaction_param = data.get('merchant_trans_id')
        amount = data.get('amount')
        
        payment = Payment.query.filter_by(payment_id=transaction_param).first()
        if not payment or payment.amount != amount:
            return {'error': -5, 'error_note': 'Transaction not found'}
        
        return {'click_trans_id': data.get('click_trans_id'), 'merchant_trans_id': transaction_param, 'merchant_prepare_id': payment.id, 'error': 0, 'error_note': 'Success'}
    
    def _click_complete(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle Click complete request"""
        payment_id = data.get('merchant_prepare_id')
        
        payment = Payment.query.get(payment_id)
        if not payment:
            return {'error': -6, 'error_note': 'Transaction not found'}
        
        # Update payment status
        payment.status = PaymentStatus.COMPLETED
        payment.paid_at = datetime.now(timezone.utc)
        payment.gateway_reference = data.get('click_trans_id')
        payment.gateway_response = data
        
        self._create_transaction(payment, 'completed', data)
        db.session.commit()
        
        self._handle_successful_payment(payment)
        
        return {'click_trans_id': data.get('click_trans_id'), 'merchant_trans_id': data.get('merchant_trans_id'), 'merchant_confirm_id': payment.id, 'error': 0, 'error_note': 'Success'}
    
    # Helper methods
    def _generate_payment_id(self) -> str:
        """Generate unique payment reference"""
        return f"PAY_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{generate_random_string(6).upper()}"
    
    def _create_transaction(self, payment: Payment, transaction_type: str, 
                          data: Dict[str, Any]) -> PaymentTransaction:
        """Create payment transaction record"""
        transaction = PaymentTransaction(
            payment_id=payment.id,
            transaction_type=transaction_type,
            amount=data.get('amount', payment.amount),
            status='completed' if transaction_type == 'completed' else 'pending',
            gateway_transaction_id=data.get('gateway_transaction_id'),
            gateway_response=data
        )
        
        db.session.add(transaction)
        return transaction
    
    def _handle_successful_payment(self, payment: Payment):
        """Handle successful payment"""
        # Update order status
        order = payment.order
        if order.status.value == 'pending':
            from .order_service import OrderService
            order_service = OrderService()
            order_service.update_order_status(order.id, 'confirmed')
        
        # Send notification
        from ..tasks.notification_tasks import send_payment_confirmation_task
        send_payment_confirmation_task.delay(payment.id)
    
    def _process_gateway_refund(self, payment: Payment, amount: int, reason: str) -> bool:
        """Process refund through payment gateway"""
        try:
            if payment.payment_method == PaymentMethod.PAYME:
                return self._process_payme_refund(payment, amount, reason)
            elif payment.payment_method == PaymentMethod.CLICK:
                return self._process_click_refund(payment, amount, reason)
            else:
                current_app.logger.error(f"Unsupported payment method for refund: {payment.payment_method}")
                return False
        except Exception as e:
            current_app.logger.error(f"Gateway refund failed for payment {payment.id}: {e}")
            return False
    
    def _process_points_refund(self, payment: Payment, amount: int, reason: str) -> bool:
        """Process loyalty points refund"""
        from .loyalty_service import LoyaltyService
        loyalty_service = LoyaltyService()
        
        # Calculate points to refund
        from ..utils.helpers import calculate_loyalty_points
        points_to_refund = calculate_loyalty_points(amount)
        
        loyalty_service.award_points(
            payment.user_id,
            points_to_refund,
            f"Refund for order #{payment.order.order_number}"
        )
        
        return True
    
    def save_card(self, card_data: Dict[str, Any]) -> CreditCard:
        """
        Save a credit card with comprehensive validation and security
        
        Args:
            card_data: Dictionary containing card information
                - user_id: User ID
                - card_number: Credit card number
                - expiry_month: Expiry month (1-12)
                - expiry_year: Expiry year (YYYY)
                - cardholder_name: Cardholder name
                - cvv: CVV code (optional, for validation only)
                - is_default: Whether this should be the default card
        
        Returns:
            CreditCard: Saved card object
        
        Raises:
            ValidationError: If card validation fails
            NotFoundError: If user not found
        """
        try:
            # Validate user exists
            user_id = card_data.get('user_id')
            user = User.query.get(user_id)
            if not user:
                raise NotFoundError(get_translation('error.not_found'))
            
            # Comprehensive card validation
            validation_result = CardValidator.validate_complete_card(card_data)
            if not validation_result.is_valid:
                raise ValidationError(get_translation('error.validation.card_invalid'))
            
            # Additional security validations
            card_number = card_data.get('card_number')
            cleaned_number = CardValidator._clean_card_number(card_number)
            
            # Check for obviously fake or test cards
            if not CardSecurityValidator.validate_no_sequential_numbers(cleaned_number):
                raise ValidationError(get_translation('error.validation.card_invalid'))

            if not CardSecurityValidator.validate_not_test_card(cleaned_number):
                raise ValidationError(get_translation('error.validation.test_card_not_allowed'))
            
            # Generate card fingerprint to detect duplicates
            fingerprint = CardValidator.generate_card_fingerprint(
                cleaned_number, 
                card_data.get('expiry_month'),
                card_data.get('expiry_year')
            )
            
            # Check for duplicate cards
            existing_card = CreditCard.query.filter_by(
                user_id=user_id,
                fingerprint=fingerprint,
                is_active=True
            ).first()
            
            if existing_card:
                raise ValidationError(get_translation('error.validation.card_already_saved'))
            
            # In production, card_token should be passed from frontend (generated by Payme SDK)
            # If card_token is provided, we use it directly
            card_token = card_data.get('card_token')
            if not card_token:
                # If no token provided (legacy/unsafe flow), we strictly reject or fail
                # For this implementation, we require 'card_token'
                raise ValidationError("Secure card token is required. Please use client-side tokenization.")

            # Optional: Verify token with Payme Subscribe API
            # verify_result = self._payme_request('cards.check', {'token': card_token})
            # if not verify_result.get('success'):
            #     raise ValidationError("Invalid card token")
            
            # Extract card details from data (passed for display purposes only)
            masked_number = card_data.get('card_number', '****0000')[-8:]
            last_four_digits = masked_number[-4:] if len(masked_number) >= 4 else '0000'
            
            # Create card record
            credit_card = CreditCard(
                user_id=user_id,
                card_token=card_token,
                card_brand=validation_result.card_brand,  # Detected or passed from frontend
                last_four_digits=last_four_digits,
                expiry_month=card_data.get('expiry_month'),
                expiry_year=card_data.get('expiry_year'),
                cardholder_name=card_data.get('cardholder_name').strip(),
                is_default=card_data.get('is_default', False),
                provider='payme',  # Defaulting to Payme for tokenized cards
                fingerprint=fingerprint,
                is_verified=False  # Will be verified on first successful payment
            )
            
            # If this is set as default, unset other default cards
            if credit_card.is_default:
                CreditCard.query.filter_by(
                    user_id=user_id,
                    is_default=True,
                    is_active=True
                ).update({'is_default': False})
            
            db.session.add(credit_card)
            db.session.commit()
            
            # Log card save event for audit
            audit_logger.log_event(
                event_type=AuditEventType.DATA_CREATION,
                action="credit_card_saved",
                severity=AuditSeverity.MEDIUM,
                resource_type="credit_card",
                resource_id=str(credit_card.id),
                user_id=user_id,
                description=f"Credit card saved for user {user_id}",
                additional_data={
                    'card_brand': validation_result.card_brand,
                    'last_four_digits': last_four_digits,
                    'is_default': credit_card.is_default,
                    'fingerprint': fingerprint[:8]
                }
            )
            
            current_app.logger.info(f"Credit card saved successfully for user {user_id}")
            return credit_card
            
        except (ValidationError, NotFoundError):
            # Re-raise these exceptions as they are expected
            raise
        except Exception as e:
            current_app.logger.error(f"Unexpected error saving card for user {user_id}: {e}")
            db.session.rollback()
            raise PaymentError(get_translation('error.payment.card_save_failed'))
    
    def get_user_cards(self, user_id: int, include_expired: bool = False) -> List[CreditCard]:
        """
        Get all active credit cards for a user

        Args:
            user_id: User ID
            include_expired: Whether to include expired cards (default: False)

        Returns:
            List of CreditCard objects ordered by default status then creation date
        """
        query = CreditCard.query.filter_by(
            user_id=user_id,
            is_active=True
        )

        # Filter out expired cards if requested
        if not include_expired:
            current_date = datetime.now(timezone.utc)
            query = query.filter(
                db.or_(
                    CreditCard.expiry_year > current_date.year,
                    db.and_(
                        CreditCard.expiry_year == current_date.year,
                        CreditCard.expiry_month >= current_date.month
                    )
                )
            )

        return query.order_by(
            CreditCard.is_default.desc(),
            CreditCard.created_at.desc()
        ).all()

    def get_default_card(self, user_id: int) -> Optional[CreditCard]:
        """
        Get user's default payment card

        Args:
            user_id: User ID

        Returns:
            CreditCard object or None if no default card
        """
        current_date = datetime.now(timezone.utc)

        # First try to get non-expired default card
        card = CreditCard.query.filter_by(
            user_id=user_id,
            is_default=True,
            is_active=True
        ).filter(
            db.or_(
                CreditCard.expiry_year > current_date.year,
                db.and_(
                    CreditCard.expiry_year == current_date.year,
                    CreditCard.expiry_month >= current_date.month
                )
            )
        ).first()

        # If no valid default card, try to get any non-expired card
        if not card:
            card = CreditCard.query.filter_by(
                user_id=user_id,
                is_active=True
            ).filter(
                db.or_(
                    CreditCard.expiry_year > current_date.year,
                    db.and_(
                        CreditCard.expiry_year == current_date.year,
                        CreditCard.expiry_month >= current_date.month
                    )
                )
            ).order_by(CreditCard.created_at.desc()).first()

        return card

    def get_card_by_id(self, card_id: int, user_id: int) -> Optional[CreditCard]:
        """
        Get specific card by ID with user ownership verification

        Args:
            card_id: Card ID
            user_id: User ID (for ownership verification)

        Returns:
            CreditCard object or None if not found

        Raises:
            NotFoundError: If card not found or doesn't belong to user
        """
        card = CreditCard.query.filter_by(
            id=card_id,
            user_id=user_id,
            is_active=True
        ).first()

        if not card:
            raise NotFoundError(get_translation('error.not_found'))

        return card

    def validate_card_for_payment(self, card_id: int, user_id: int, amount: int) -> tuple[bool, Optional[str]]:
        """
        Validate that a card can be used for payment

        Args:
            card_id: Card ID
            user_id: User ID
            amount: Payment amount in smallest currency unit

        Returns:
            Tuple of (is_valid, error_message)
            - (True, None) if valid
            - (False, "error message") if invalid
        """
        try:
            # Check card exists and belongs to user
            card = CreditCard.query.filter_by(
                id=card_id,
                user_id=user_id,
                is_active=True
            ).first()

            if not card:
                return False, get_translation('error.not_found')

            # Check if card is expired
            current_date = datetime.now(timezone.utc)
            is_expired = (
                card.expiry_year < current_date.year or
                (card.expiry_year == current_date.year and card.expiry_month < current_date.month)
            )

            if is_expired:
                return False, get_translation('error.validation.card_expired')

            # Check if card is verified (has been used successfully at least once)
            if not card.is_verified and current_app.config.get('REQUIRE_CARD_VERIFICATION', False):
                return False, get_translation('error.validation.card_not_verified')

            # Validate amount is positive
            if amount <= 0:
                return False, get_translation('error.validation.invalid_amount')

            # Check if provider is available for this card brand
            if not self._is_provider_available(card.provider):
                return False, get_translation('error.payment.provider_unavailable')

            return True, None

        except Exception as e:
            current_app.logger.error(f"Card validation error: {e}")
            return False, get_translation('error.validation.card_invalid')

    def _is_provider_available(self, provider: str) -> bool:
        """
        Check if payment provider is available

        Args:
            provider: Provider name (payme, click, uzcard, humo)

        Returns:
            bool: True if provider is configured and available
        """
        if provider == 'payme':
            return bool(self.payme_merchant_id and self.payme_secret_key)
        elif provider == 'click':
            return bool(self.click_merchant_id and self.click_secret_key)
        elif provider in ['uzcard', 'humo']:
            # These typically go through Payme or Click
            return bool(self.payme_merchant_id or self.click_merchant_id)
        else:
            return False
    
    def set_default_card(self, card_id: int, user_id: int) -> CreditCard:
        """
        Set a card as the default payment method

        Args:
            card_id: Card ID
            user_id: User ID (for security)

        Returns:
            CreditCard: Updated card object

        Raises:
            NotFoundError: If card not found
            ValidationError: If card is expired
        """
        card = CreditCard.query.filter_by(
            id=card_id,
            user_id=user_id,
            is_active=True
        ).first()

        if not card:
            raise NotFoundError(get_translation('error.not_found'))

        # Check if card is expired
        current_date = datetime.now(timezone.utc)
        is_expired = (
            card.expiry_year < current_date.year or
            (card.expiry_year == current_date.year and card.expiry_month < current_date.month)
        )

        if is_expired:
            raise ValidationError(get_translation('error.validation.card_expired'))

        # Unset current default card
        CreditCard.query.filter_by(
            user_id=user_id,
            is_default=True,
            is_active=True
        ).update({'is_default': False})

        # Set new default card
        card.is_default = True
        db.session.commit()

        # Log event
        audit_logger.log_event(
            event_type=AuditEventType.DATA_MODIFICATION,
            action="credit_card_set_as_default",
            severity=AuditSeverity.LOW,
            resource_type="credit_card",
            resource_id=str(card.id),
            user_id=user_id,
            description=f"Card ending in {card.last_four_digits} set as default",
            additional_data={
                'card_brand': card.card_brand,
                'last_four_digits': card.last_four_digits
            }
        )

        return card

    def delete_card(self, card_id: int, user_id: int) -> bool:
        """
        Delete (deactivate) a credit card

        Args:
            card_id: Card ID
            user_id: User ID (for security)

        Returns:
            bool: True if successful

        Raises:
            NotFoundError: If card not found
            ValidationError: If card cannot be deleted
        """
        card = CreditCard.query.filter_by(
            id=card_id,
            user_id=user_id,
            is_active=True
        ).first()

        if not card:
            raise NotFoundError(get_translation('error.not_found'))

        # Prevent deletion of default card if it's the only card
        if card.is_default:
            other_cards_count = CreditCard.query.filter_by(
                user_id=user_id,
                is_active=True
            ).filter(CreditCard.id != card_id).count()

            if other_cards_count == 0:
                raise ValidationError(get_translation('error.validation.cannot_delete_last_card'))

            # If deleting default card, make another card default
            if other_cards_count > 0:
                next_card = CreditCard.query.filter_by(
                    user_id=user_id,
                    is_active=True
                ).filter(CreditCard.id != card_id).first()
                next_card.is_default = True

        # Call Payme verify API if needed to remove token invalidation
        # For now, we assume removal is successful and just deactivate locally
        # Ideally: self._payme_request('cards.remove', {'token': card.card_token})

        # Soft delete
        card.is_active = False
        card.deleted_at = datetime.now(timezone.utc)

        db.session.commit()

        # Log card deletion for audit
        audit_logger.log_event(
            event_type=AuditEventType.DATA_DELETION,
            action="credit_card_deleted",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            resource_id=str(card.id),
            user_id=user_id,
            description=f"Credit card deleted for user {user_id}",
            additional_data={
                'card_brand': card.card_brand,
                'last_four_digits': card.last_four_digits
            }
        )

        return True
    
    def _tokenize_card(self, card_number: str, card_brand: str) -> str:
        """
        Tokenize card number (simplified implementation)
        In production, this should integrate with actual payment processor tokenization
        
        Args:
            card_number: Clean card number
            card_brand: Card brand
        
        Returns:
            str: Card token
        """
        return card_token
    
    def create_card_token(self, number: str, expire: str, save: bool = False) -> Dict[str, Any]:
        """
        Create card token via Payme API
        
        Args:
            number: Card number (16 digits)
            expire: Expiration date (MMYY or similar)
            save: Whether to save the card
            
        Returns:
            Dict containing token and card metadata
        """
        # Ensure expire format is valid for Payme (usually starts with MM...)
        # Payme expects simple string, often checking len. 
        # Typically Payme docs example: "0399".
        
        # Call Payme cards.create
        response = self._payme_request('cards.create', {
            'card': {
                'number': number,
                'expire': expire
            },
            'save': save
        })
        
        if 'error' in response:
            raise PaymentError(f"Payme tokenization failed: {response['error'].get('message')}, response: {response}")
            
        result = response.get('result', {}).get('card', {})
        if not result.get('token'):
             raise PaymentError("Payme did not return a token")
             
        return result

    def _get_provider_for_brand(self, card_brand: str) -> str:
        """
        Get payment provider for card brand
        
        Args:
            card_brand: Card brand
        
        Returns:
            str: Provider name
        """
        provider_mapping = {
            'uzcard': 'uzcard',
            'humo': 'humo',
            'visa': 'payme',  # Can also be click
            'mastercard': 'payme'  # Can also be click
        }
        
        return provider_mapping.get(card_brand, 'payme')
    
    def _process_payme_refund(self, payment: Payment, amount: int, reason: str) -> bool:
        """Process refund through Payme gateway"""
        try:
            if not self.payme_secret_key or not self.payme_merchant_id:
                current_app.logger.error("Payme credentials not configured for refund")
                return False
            
            # Get the original transaction from payment metadata
            transaction_id = payment.provider_data.get('gateway_transaction_id')
            if not transaction_id:
                current_app.logger.error(f"No Payme transaction ID found for payment {payment.id}")
                return False
            
            # Prepare refund request to Payme
            refund_payload = {
                "method": "CancelTransaction",
                "params": {
                    "id": transaction_id,
                    "reason": reason or "Customer request"
                }
            }
            
            # Make refund request to Payme
            response = requests.post(
                self.payme_endpoint,
                json=refund_payload,
                auth=(self.payme_merchant_id, self.payme_secret_key),
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result:
                    current_app.logger.info(f"Payme refund successful for payment {payment.id}")
                    return True
                elif 'error' in result:
                    current_app.logger.error(f"Payme refund failed for payment {payment.id}: {result['error']}")
                    return False
            else:
                current_app.logger.error(f"Payme refund API error for payment {payment.id}: HTTP {response.status_code}")
                return False
                
        except requests.RequestException as e:
            current_app.logger.error(f"Payme refund network error for payment {payment.id}: {e}")
            return False
        except Exception as e:
            current_app.logger.error(f"Payme refund unexpected error for payment {payment.id}: {e}")
            return False
    
    def _process_click_refund(self, payment: Payment, amount: int, reason: str) -> bool:
        """Process refund through Click gateway"""
        try:
            if not self.click_secret_key or not self.click_service_id:
                current_app.logger.error("Click credentials not configured for refund")
                return False
            
            # Get the original transaction from payment metadata
            click_trans_id = payment.provider_data.get('click_trans_id')
            if not click_trans_id:
                current_app.logger.error(f"No Click transaction ID found for payment {payment.id}")
                return False
            
            # Prepare refund request to Click
            refund_data = {
                'service_id': self.click_service_id,
                'click_trans_id': click_trans_id,
                'merchant_trans_id': payment.payment_id,
                'amount': amount,
                'action': 'refund',
                'sign_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # Generate signature for refund request
            signature_data = f"{click_trans_id}{self.click_service_id}{self.click_secret_key}{payment.payment_id}{amount}refund{refund_data['sign_time']}"
            refund_data['sign_string'] = hashlib.md5(signature_data.encode('utf-8')).hexdigest()
            
            # Make refund request to Click
            response = requests.post(
                f"{self.click_endpoint}/refund",
                data=refund_data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('error', 0) == 0:
                    current_app.logger.info(f"Click refund successful for payment {payment.id}")
                    return True
                else:
                    current_app.logger.error(f"Click refund failed for payment {payment.id}: {result.get('error_note', 'Unknown error')}")
                    return False
            else:
                current_app.logger.error(f"Click refund API error for payment {payment.id}: HTTP {response.status_code}")
                return False
                
        except requests.RequestException as e:
            current_app.logger.error(f"Click refund network error for payment {payment.id}: {e}")
            return False
        except Exception as e:
            current_app.logger.error(f"Click refund unexpected error for payment {payment.id}: {e}")
            return False