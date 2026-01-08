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

from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment, PaymentTransaction, CreditCard
from business_app.models.user import User
from business_app.utils.exceptions import PaymentError, ValidationError, NotFoundError
from business_app.utils.constants import OrderStatus, PaymentStatus, PaymentMethod
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
            redis_url = current_app.config.get('REDIS_URL', 'redis://localhost:6379/0')
            # Use a specific DB for webhook/verification tracking (db 3)
            # Parse the URL and replace the db number
            if '/0' in redis_url:
                redis_url = redis_url.replace('/0', '/3')
            elif redis_url.endswith(':6379'):
                redis_url = redis_url + '/3'
            self.redis_client = redis.from_url(redis_url, decode_responses=True)
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


    def _payme_request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send request to Payme Subscribe API
        
        Args:
            method: JSON-RPC method name
            params: Parameters for the method
            
        Returns:
            Dict: JSON-RPC response
        """
        try:
             # Subscribe API uses X-Auth headers
             print(f"Payme: method: {method}, url: {self.payme_endpoint}")
             print(f"params: {params}")
             headers = {
                 'X-Auth': f"{self.payme_merchant_id}:{self.payme_secret_key}" if method.startswith('receipts.') else self.payme_merchant_id,
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

    # ============================================================================
    # PAYME SUBSCRIBE API METHODS (cards.create, cards.verify, receipts.pay, etc.)
    # ============================================================================

    def _extract_payme_error_message(self, error: Dict) -> str:
        """
        Extract human-readable error message from Payme error response.

        Payme errors have format:
        {
            "code": -31001,
            "message": {
                "uz": "...",
                "ru": "...",
                "en": "..."
            }
        }
        or sometimes just a string message.
        """
        message = error.get('message', 'Unknown error')

        if isinstance(message, dict):
            # Return English message, fallback to Russian, then Uzbek
            return message.get('en') or message.get('ru') or message.get('uz') or str(message)

        return str(message)

    def create_card_token_with_verification(self, card_number: str, expiry: str,
                                            save: bool = True) -> Dict[str, Any]:
        """
        Create card token via Payme cards.create and request verification code.

        This is the main entry point for tokenizing a new card. It:
        1. Calls Payme cards.create to get a token
        2. If card needs verification (verify: false), auto-requests SMS code

        Args:
            card_number: 16-digit card number (no spaces)
            expiry: Expiry in MMYY format (e.g., "0325" for March 2025)
            save: True for permanent token (recurring), False for one-time

        Returns:
            Dict containing:
            - token: Card token from Payme
            - masked_number: e.g., "860006******6311"
            - expire: e.g., "03/25"
            - recurrent: Whether card supports recurring payments
            - needs_verification: True if SMS verification required
            - masked_phone: Phone number for SMS (if verification needed)
            - wait_seconds: Seconds until code expires (if verification needed)
            - verification_sent: True if SMS was sent

        Raises:
            PaymentError: If card creation fails
            ValidationError: If card data is invalid
        """
        # Validate card number format
        clean_number = card_number.replace(' ', '').replace('-', '')
        if not clean_number.isdigit() or len(clean_number) != 16:
            raise ValidationError("Card number must be 16 digits")

        # Validate expiry format (MMYY)
        clean_expiry = expiry.replace('/', '')
        if not clean_expiry.isdigit() or len(clean_expiry) != 4:
            raise ValidationError("Expiry must be in MMYY format")

        month = int(clean_expiry[:2])
        if month < 1 or month > 12:
            raise ValidationError("Invalid expiry month")

        # Step 1: Create token via Payme
        create_response = self._payme_request('cards.create', {
            'card': {
                'number': clean_number,
                'expire': clean_expiry
            },
            'save': save
        })

        if 'error' in create_response:
            error_msg = self._extract_payme_error_message(create_response['error'])
            current_app.logger.error(f"Payme cards.create failed: {error_msg}")
            raise PaymentError(f"Card tokenization failed: {error_msg}")

        card_data = create_response.get('result', {}).get('card', {})
        token = card_data.get('token')

        if not token:
            raise PaymentError("Payme did not return a card token")

        result = {
            'token': token,
            'masked_number': card_data.get('number', ''),
            'expire': card_data.get('expire', ''),
            'recurrent': card_data.get('recurrent', False),
            'needs_verification': not card_data.get('verify', False)
        }

        # Step 2: If verification needed, request SMS code automatically
        # if result['needs_verification']:
        #     try:
        #         verify_result = self.request_card_verification_code(token)
        #         result.update({
        #             'masked_phone': verify_result['phone'],
        #             'wait_seconds': verify_result['wait'] // 1000,  # Convert ms to seconds
        #             'verification_sent': verify_result['sent']
        #         })
        #     except PaymentError as e:
        #         # Log but don't fail - user can manually request code
        #         current_app.logger.warning(f"Auto-verification request failed: {e}")
        #         result['verification_sent'] = False
        #         result['masked_phone'] = None
        #         result['wait_seconds'] = 60  # Default

        # Audit log
        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="card_tokenized",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            description=f"Card tokenized via Payme: {result['masked_number']}",
            additional_data={
                'masked_number': result['masked_number'],
                'needs_verification': result['needs_verification'],
                'recurrent': result['recurrent']
            }
        )

        return result

    def request_card_verification_code(self, token: str) -> Dict[str, Any]:
        """
        Request SMS verification code for a card token via Payme cards.get_verify_code.

        Args:
            token: Card token from cards.create

        Returns:
            Dict containing:
            - sent: True if SMS was sent successfully
            - phone: Masked phone number (e.g., "99890*****31")
            - wait: Validity period in milliseconds

        Raises:
            PaymentError: If request fails
        """
        if not token:
            raise ValidationError("Card token is required")

        response = self._payme_request('cards.get_verify_code', {
            'token': token
        })

        if 'error' in response:
            error_msg = self._extract_payme_error_message(response['error'])
            current_app.logger.error(f"Payme cards.get_verify_code failed: {error_msg}")
            raise PaymentError(f"Failed to send verification code: {error_msg}")

        result = response.get('result', {})

        if not result.get('sent'):
            raise PaymentError("Verification code was not sent by Payme")

        # Reset verification attempts when a new code is sent
        self.reset_verification_attempts(token)

        current_app.logger.info(f"Verification code sent to {result.get('phone', 'unknown')}")

        return {
            'sent': result.get('sent', False),
            'phone': result.get('phone', ''),
            'wait': result.get('wait', 60000)  # Default 60 seconds
        }

    # Maximum verification attempts per token before requiring a new code
    MAX_VERIFICATION_ATTEMPTS = 3
    # TTL for verification attempt tracking in Redis (10 minutes)
    VERIFICATION_ATTEMPTS_TTL = 600

    def _get_verification_attempts_key(self, token: str) -> str:
        """Generate Redis key for tracking verification attempts."""
        # Use a hash of the token to avoid storing full tokens in Redis keys
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        return f"payme:verify_attempts:{token_hash}"

    def get_verification_attempts_remaining(self, token: str) -> int:
        """
        Get remaining verification attempts for a token.

        Args:
            token: Card token

        Returns:
            int: Number of attempts remaining (0-3)
        """
        if not self.redis_client:
            # Fallback if Redis not available - allow attempts but log warning
            current_app.logger.warning("Redis not available for verification attempt tracking")
            return self.MAX_VERIFICATION_ATTEMPTS

        try:
            key = self._get_verification_attempts_key(token)
            attempts = self.redis_client.get(key)
            if attempts is None:
                return self.MAX_VERIFICATION_ATTEMPTS
            return max(0, self.MAX_VERIFICATION_ATTEMPTS - int(attempts))
        except Exception as e:
            current_app.logger.error(f"Error getting verification attempts: {e}")
            return self.MAX_VERIFICATION_ATTEMPTS

    def increment_verification_attempts(self, token: str) -> int:
        """
        Increment failed verification attempts for a token.

        Args:
            token: Card token

        Returns:
            int: Number of attempts remaining after increment
        """
        if not self.redis_client:
            current_app.logger.warning("Redis not available for verification attempt tracking")
            return self.MAX_VERIFICATION_ATTEMPTS - 1

        try:
            key = self._get_verification_attempts_key(token)
            # Increment and set TTL
            pipe = self.redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.VERIFICATION_ATTEMPTS_TTL)
            results = pipe.execute()
            attempts = results[0]  # Result of incr
            return max(0, self.MAX_VERIFICATION_ATTEMPTS - int(attempts))
        except Exception as e:
            current_app.logger.error(f"Error incrementing verification attempts: {e}")
            return self.MAX_VERIFICATION_ATTEMPTS - 1

    def reset_verification_attempts(self, token: str) -> None:
        """
        Reset verification attempts for a token (call after successful verify or new code request).

        Args:
            token: Card token
        """
        if not self.redis_client:
            return

        try:
            key = self._get_verification_attempts_key(token)
            self.redis_client.delete(key)
        except Exception as e:
            current_app.logger.error(f"Error resetting verification attempts: {e}")

    def verify_card(self, token: str, code: str) -> Dict[str, Any]:
        """
        Verify card with SMS code via Payme cards.verify.

        Args:
            token: Card token from cards.create
            code: Verification code from SMS (4-8 alphanumeric characters)

        Returns:
            Dict containing:
            - verified: True if verification successful
            - card: Card data from Payme (masked_number, expire, token, recurrent)

        Raises:
            ValidationError: If code format is invalid or wrong code entered
            PaymentError: If verification fails for other reasons
        """
        # Validate code format
        if not token:
            raise ValidationError("Card token is required")

        code = str(code).strip().upper()  # Normalize - Payme codes can be alphanumeric
        if not code or len(code) < 4 or len(code) > 8:
            raise ValidationError("Verification code must be 4-8 characters")

        if not code.isalnum():
            raise ValidationError("Verification code must be alphanumeric")

        # Check if max attempts exceeded before making API call
        attempts_remaining = self.get_verification_attempts_remaining(token)
        if attempts_remaining <= 0:
            raise ValidationError("Too many failed attempts. Please request a new code.")

        response = self._payme_request('cards.verify', {
            'token': token,
            'code': code
        })

        if 'error' in response:
            error_code = response['error'].get('code')
            error_msg = self._extract_payme_error_message(response['error'])

            # -31103 is wrong verification code
            if error_code == -31103:
                # Increment attempts on wrong code
                attempts_remaining = self.increment_verification_attempts(token)
                current_app.logger.warning(f"Invalid verification code entered. Attempts remaining: {attempts_remaining}")
                raise ValidationError("Invalid verification code")

            # -31104 is expired code
            if error_code == -31104:
                current_app.logger.warning(f"Verification code expired for token")
                raise ValidationError("Verification code has expired. Please request a new code.")

            current_app.logger.error(f"Payme cards.verify failed: {error_msg}")
            raise PaymentError(f"Card verification failed: {error_msg}")

        card_result = response.get('result', {}).get('card', {})

        if not card_result.get('verify'):
            raise PaymentError("Card verification was not confirmed by Payme")

        # Reset attempts on successful verification
        self.reset_verification_attempts(token)

        current_app.logger.info(f"Card verified successfully: {card_result.get('number', 'unknown')}")

        # Audit log
        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_VERIFICATION_CODE_VERIFED,
            action="card_verified",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            description=f"Card verified via SMS: {card_result.get('number', '')}",
            additional_data={
                'masked_number': card_result.get('number'),
                'recurrent': card_result.get('recurrent', False)
            }
        )

        return {
            'verified': True,
            'card': {
                'masked_number': card_result.get('number', ''),
                'expire': card_result.get('expire', ''),
                'token': card_result.get('token', token),
                'recurrent': card_result.get('recurrent', False)
            }
        }

    def create_payme_receipt(self, order: Order, description: str = None) -> Dict[str, Any]:
        """
        Create a Payme receipt with full fiscal details via receipts.create.

        This creates an invoice that can then be paid with receipts.pay.

        Args:
            order: Order object with items
            description: Optional payment description

        Returns:
            Dict containing:
            - receipt_id: Payme receipt ID (_id)
            - state: Receipt state (should be 0 = created)
            - amount: Amount in tiyin
            - create_time: Creation timestamp

        Raises:
            PaymentError: If receipt creation fails
        """
        if not order:
            raise ValidationError("Order is required")

        if not order.order_items or len(order.order_items) == 0:
            raise ValidationError("Order must have at least one item")

        # Calculate total in tiyin (1 UZS = 100 tiyin)
        amount_tiyin = int(float(order.total_amount) * 100)

        # Build items array for fiscal receipt
        items = []
        for item in order.order_items:
            item_data = {
                'title': item.product.name if item.product else f"Product #{item.product_id}",
                'price': int(float(item.unit_price) * 100),  # Convert to tiyin
                'count': item.quantity,
                'code': getattr(item.product, 'ikpu_code', None) or '02201001001000000',  # Default IKPU for water
                'vat_percent': 0,  # Standard VAT in Uzbekistan
            }

            # Add package code if available
            if hasattr(item.product, 'package_code') and item.product.package_code:
                item_data['package_code'] = item.product.package_code

            items.append(item_data)

        # Build receipt params
        params = {
            'amount': amount_tiyin,
            'account': {
                'charge_id': str(order.id)
            },
            'description': description or f"Water delivery order #{order.order_number}"
        }

        # Add detail object for fiscal compliance
        detail = {
            'receipt_type': 0,  # 0 = sale
            'items': items
        }

        # Add shipping/delivery fee if applicable
        delivery_fee = getattr(order, 'delivery_fee', None) or getattr(order, 'shipping_fee', None)
        if delivery_fee and float(delivery_fee) > 0:
            detail['shipping'] = {
                'title': 'Delivery Fee',
                'price': int(float(delivery_fee) * 100)
            }

        params['detail'] = detail

        # Create receipt via Payme
        response = self._payme_request('receipts.create', params)

        if 'error' in response:
            error_msg = self._extract_payme_error_message(response['error'])
            current_app.logger.error(f"Payme receipts.create failed: {error_msg}")
            raise PaymentError(f"Failed to create receipt: {error_msg}")

        receipt = response.get('result', {}).get('receipt', {})

        if not receipt.get('_id'):
            raise PaymentError("Payme did not return a receipt ID")

        current_app.logger.info(f"Payme receipt created: {receipt['_id']} for order {order.id}")

        return {
            'receipt_id': receipt['_id'],
            'state': receipt.get('state', 0),
            'amount': receipt.get('amount', amount_tiyin),
            'create_time': receipt.get('create_time')
        }

    def pay_payme_receipt(self, receipt_id: str, token: str,
                          payer: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute payment on a Payme receipt via receipts.pay.

        Args:
            receipt_id: Receipt ID from receipts.create
            token: Verified card token (must have verify=true)
            payer: Optional payer information for fraud prevention

        Returns:
            Dict containing:
            - success: True if payment successful
            - receipt_id: Receipt ID
            - state: Receipt state (4 = paid successfully)
            - pay_time: Payment timestamp
            - amount: Amount paid
            - card: Card info (masked number, expire)

        Raises:
            PaymentError: If payment fails
        """
        if not receipt_id:
            raise ValidationError("Receipt ID is required")

        if not token:
            raise ValidationError("Card token is required")

        params = {
            'id': receipt_id,
            'token': token
        }

        # Add payer info for fraud prevention (optional but recommended)
        if payer:
            payer_info = {}
            if payer.get('phone'):
                payer_info['phone'] = payer['phone']
            if payer.get('email'):
                payer_info['email'] = payer['email']
            if payer.get('name'):
                payer_info['name'] = payer['name']
            if payer.get('ip'):
                payer_info['ip'] = payer['ip']

            if payer_info:
                params['payer'] = payer_info

        # Execute payment
        response = self._payme_request('receipts.pay', params)

        if 'error' in response:
            error_msg = self._extract_payme_error_message(response['error'])
            current_app.logger.error(f"Payme receipts.pay failed: {error_msg}")
            raise PaymentError(f"Payment failed: {error_msg}")

        receipt = response.get('result', {}).get('receipt', {})
        state = receipt.get('state')

        # Check receipt state - must be 4 for successful payment
        if state != 4:
            state_messages = {
                0: 'Receipt awaiting payment',
                1: 'Transaction verification in progress',
                2: 'Funds deducted, processing',
                3: 'Transaction closing in progress',
                5: 'Receipt archived',
                6: 'Payment on hold - contact Payme support',
                20: 'Payment paused for manual review',
                21: 'Payment queued for cancellation',
                50: 'Payment cancelled'
            }
            msg = state_messages.get(state, f'Unexpected receipt state: {state}')
            current_app.logger.error(f"Payme payment not completed. State: {state} - {msg}")
            raise PaymentError(f"Payment not completed: {msg}")

        current_app.logger.info(f"Payme payment successful. Receipt: {receipt_id}, State: {state}")

        return {
            'success': True,
            'receipt_id': receipt.get('_id', receipt_id),
            'state': state,
            'pay_time': receipt.get('pay_time'),
            'amount': receipt.get('amount'),
            'card': receipt.get('card', {})
        }

    def _detect_card_brand(self, masked_number: str) -> str:
        """
        Detect card brand from masked card number.

        Args:
            masked_number: Masked card number (e.g., "860006******6311")

        Returns:
            str: Card brand (uzcard, humo, visa, mastercard, unknown)
        """
        if not masked_number:
            return 'unknown'

        # Extract first 6 digits (BIN)
        clean = masked_number.replace('*', '').replace(' ', '')
        if len(clean) < 4:
            return 'unknown'

        prefix = clean[:4]

        # Uzbek cards
        if masked_number.startswith('8600'):
            return 'uzcard'
        if masked_number.startswith('9860'):
            return 'humo'

        # International cards (less common in Uzbekistan)
        if prefix.startswith('4'):
            return 'visa'
        if prefix.startswith('5') or prefix.startswith('2'):
            return 'mastercard'

        return 'unknown'

    def _save_or_update_verified_card(self, user_id: int, token: str,
                                       card_metadata: Dict[str, Any]) -> CreditCard:
        """
        Save a verified card to database or update existing card's verification status.

        Args:
            user_id: User ID
            token: Verified card token
            card_metadata: Dict with masked_number, expire, cardholder_name, recurrent

        Returns:
            CreditCard: The saved or updated card object
        """
        # Check if card with this token already exists
        existing_card = CreditCard.query.filter_by(
            user_id=user_id,
            card_token=token
        ).first()

        if existing_card:
            # Update existing card
            existing_card.is_verified = True
            existing_card.last_used_at = datetime.now(timezone.utc)
            existing_card.usage_count = (existing_card.usage_count or 0) + 1
            existing_card.payme_recurrent = card_metadata.get('recurrent', False)
            return existing_card

        # Parse expiry from "MM/YY" format
        expire = card_metadata.get('expire', '')
        try:
            if '/' in expire:
                parts = expire.split('/')
                expiry_month = int(parts[0])
                expiry_year = int('20' + parts[1]) if len(parts[1]) == 2 else int(parts[1])
            else:
                # MMYY format
                expiry_month = int(expire[:2]) if len(expire) >= 2 else 1
                expiry_year = int('20' + expire[2:4]) if len(expire) >= 4 else 2099
        except (ValueError, IndexError):
            expiry_month = 1
            expiry_year = 2099

        # Extract last 4 digits from masked number
        masked_number = card_metadata.get('masked_number', '')
        # Remove asterisks and get last 4
        clean_digits = masked_number.replace('*', '').replace(' ', '')
        last_four = clean_digits[-4:] if len(clean_digits) >= 4 else '0000'

        # Detect card brand
        card_brand = self._detect_card_brand(masked_number)

        # Create new card
        card = CreditCard(
            user_id=user_id,
            card_token=token,
            card_brand=card_brand,
            last_four_digits=last_four,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            cardholder_name=card_metadata.get('cardholder_name', 'Card Holder'),
            is_verified=True,
            is_active=True,
            is_default=False,  # Don't auto-set as default
            provider='payme',
            payme_recurrent=card_metadata.get('recurrent', True),
            last_used_at=datetime.now(timezone.utc),
            usage_count=1
        )

        db.session.add(card)

        # Audit log
        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="verified_card_saved",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            description=f"Verified card saved: {masked_number}",
            additional_data={
                'card_brand': card_brand,
                'last_four': last_four,
                'recurrent': card_metadata.get('recurrent', True),
                'user_id': user_id
            }
        )

        return card

    def process_payme_payment_full(self, order_id: int, card_token: str,
                                   user_id: int, save_card: bool = True,
                                   card_metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Complete Payme payment flow: create receipt and pay.

        This is the main method to call after card verification is complete.
        It orchestrates the full payment process.

        Args:
            order_id: Order ID to pay for
            card_token: Verified card token (must have passed cards.verify)
            user_id: User ID for security verification
            save_card: Whether to save card for future use
            card_metadata: Card details for saving (masked_number, expire, cardholder_name)

        Returns:
            Dict containing:
            - success: True if payment successful
            - payment_id: Our payment record ID
            - order_id: Order ID
            - receipt_id: Payme receipt ID
            - redirect_url: URL to redirect user after success

        Raises:
            NotFoundError: If order not found
            ValidationError: If order/user mismatch or already paid
            PaymentError: If payment fails
        """
        # Load and validate order
        order: Order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")

        if order.user_id != int(user_id):
            raise ValidationError("Order does not belong to this user")

        if hasattr(order, 'is_paid') and order.is_paid:
            raise ValidationError("Order is already paid")

        # Create payment record
        payment: Payment = Payment(
            order_id=order_id,
            user_id=user_id,
            amount=order.total_amount,
            currency='UZS',
            payment_method=PaymentMethod.PAYME,
            status=PaymentStatus.PENDING,
            description=f'Payment for order #{order.order_number}'
        )
        db.session.add(payment)
        db.session.flush()  # Get payment ID

        receipt_id = None

        try:
            # Step 1: Create receipt
            current_app.logger.info(f"Creating Payme receipt for order {order_id}")
            receipt_result = self.create_payme_receipt(
                order,
                description=f'Water delivery order #{order.order_number}'
            )
            receipt_id = receipt_result['receipt_id']

            # Log receipt creation
            self._create_transaction(payment, 'receipt_created', {
                'receipt_id': receipt_id,
                'amount': receipt_result['amount'],
                'state': receipt_result['state']
            })

            # Step 2: Pay receipt
            current_app.logger.info(f"Paying Payme receipt {receipt_id}")
            user = User.query.get(user_id)
            payer_info = {
                'phone': user.phone if user else None,
                'email': user.email if user else None,
                'name': getattr(user, 'full_name', None) if user else None,
            }
            # Try to get IP from request context
            try:
                payer_info['ip'] = request.remote_addr
            except RuntimeError:
                pass  # No request context

            pay_result = self.pay_payme_receipt(receipt_id, card_token, payer_info)

            # Step 3: Update payment record
            payment.status = PaymentStatus.COMPLETED
            payment.paid_at = datetime.now(timezone.utc)
            payment.provider_transaction_id = receipt_id
            payment.provider_data = {
                'receipt_id': receipt_id,
                'state': pay_result['state'],
                'pay_time': pay_result['pay_time'],
                'card_last_four': pay_result['card'].get('number', '')[-4:] if pay_result.get('card') else None
            }

            # Log successful payment
            self._create_transaction(payment, 'payment_completed', {
                'receipt_id': receipt_id,
                'pay_time': pay_result['pay_time'],
                'state': pay_result['state']
            })

            # Step 4: Update order status
            self._handle_successful_payment(payment)

            # Step 5: Save or update card if requested
            if save_card and card_metadata:
                self._save_or_update_verified_card(user_id, card_token, card_metadata)

            db.session.commit()

            # Audit log
            audit_logger.log_event(
                event_type=AuditEventType.PAYMENT_PROCESSED,
                action="payme_payment_completed",
                severity=AuditSeverity.MEDIUM,
                resource_type="payment",
                resource_id=str(payment.id),
                description=f"Payme payment completed for order {order_id}",
                additional_data={
                    'order_id': order_id,
                    'amount': float(order.total_amount),
                    'receipt_id': receipt_id,
                    'user_id': user_id
                }
            )

            return {
                'success': True,
                'payment_id': payment.id,
                'order_id': order_id,
                'receipt_id': receipt_id,
                'amount': float(order.total_amount),
                'redirect_url': f'/my-orders?order_id={order_id}&payment=success'
            }

        except Exception as e:
            # Rollback payment status on failure
            current_app.logger.error(f"Payme payment failed for order {order_id}: {e}")
            payment.status = PaymentStatus.FAILED
            payment.failure_reason = str(e)

            if receipt_id:
                payment.provider_transaction_id = receipt_id

            db.session.commit()

            # Audit log
            audit_logger.log_event(
                event_type=AuditEventType.PAYMENT_FAILED,
                action="payme_payment_failed",
                severity=AuditSeverity.HIGH,
                resource_type="payment",
                resource_id=str(payment.id),
                description=f"Payme payment failed for order {order_id}: {str(e)}",
                additional_data={
                    'order_id': order_id,
                    'error': str(e),
                    'receipt_id': receipt_id,
                    'user_id': user_id
                }
            )

            raise

    # ============================================================================
    # END OF PAYME SUBSCRIBE API METHODS
    # ============================================================================

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
                    event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
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
                    event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
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
                event_type=AuditEventType.WEBHOOK_RECEIVED,
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
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
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
            'receipt_id': transaction_id,
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
        # In this simplistic model, we look up by Payme's ID (which we stored as provider_transaction_id)
        # However, typically Payme expects us to use our own ID or tracking.
        # Based on Payme docs: 'id' is their transaction ID.

        transaction = PaymentTransaction.query.filter_by(provider_transaction_id=transaction_id).first()
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
        
        transaction = PaymentTransaction.query.filter_by(provider_transaction_id=transaction_id).first()
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
        transaction = PaymentTransaction.query.filter_by(provider_transaction_id=transaction_id).first()
        
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
            provider_transaction_id=data.get('receipt_id'),
            provider_response=data
        )
        
        db.session.add(transaction)
        return transaction
    
    def _handle_successful_payment(self, payment: Payment):
        """Handle successful payment"""
        # Update order status
        order = payment.order
        # Handle both Enum and string status values
        status_value = order.status.value if hasattr(order.status, 'value') else order.status
        if status_value == 'pending':
            from .order_service import OrderService
            order_service = OrderService()
            order_service.update_order_status(order.id, OrderStatus.CONFIRMED)
        
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

            # Card token is required for secure card storage
            card_token = card_data.get('card_token')
            if not card_token:
                raise ValidationError("Secure card token is required. Please use client-side tokenization.")

            card_number = card_data.get('card_number', '')
            is_tokenized = CardValidator._is_masked_card_number(card_number)

            # Comprehensive card validation (handles both full and tokenized cards)
            validation_result = CardValidator.validate_complete_card(card_data)
            if not validation_result.is_valid:
                error_details = ', '.join(validation_result.errors)
                current_app.logger.warning(f"Card validation failed: {error_details}")
                raise ValidationError(get_translation('error.validation.card_invalid'))

            # Extract last 4 digits
            if is_tokenized:
                last_four_digits = CardValidator._extract_last_four_from_masked(card_number)
            else:
                cleaned_number = CardValidator._clean_card_number(card_number)
                last_four_digits = cleaned_number[-4:] if len(cleaned_number) >= 4 else '0000'

                # Additional security validations only for full card numbers
                if not CardSecurityValidator.validate_no_sequential_numbers(cleaned_number):
                    raise ValidationError(get_translation('error.validation.card_invalid'))

                if not CardSecurityValidator.validate_not_test_card(cleaned_number):
                    raise ValidationError(get_translation('error.validation.test_card_not_allowed'))

            # Generate card fingerprint to detect duplicates
            if is_tokenized:
                fingerprint = CardValidator.generate_tokenized_card_fingerprint(
                    card_token,
                    last_four_digits,
                    card_data.get('expiry_month'),
                    card_data.get('expiry_year')
                )
            else:
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

            # Determine card brand (from validation or default to unknown for tokenized)
            card_brand = validation_result.card_brand or 'unknown'

            # Create card record
            credit_card = CreditCard(
                user_id=user_id,
                card_token=card_token,
                card_brand=card_brand,
                last_four_digits=last_four_digits,
                expiry_month=card_data.get('expiry_month'),
                expiry_year=card_data.get('expiry_year'),
                cardholder_name=card_data.get('cardholder_name', '').strip(),
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
                event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                action="credit_card_saved",
                severity=AuditSeverity.MEDIUM,
                resource_type="credit_card",
                resource_id=str(credit_card.id),
                description=f"Credit card saved for user {user_id}",
                additional_data={
                    'card_brand': card_brand,
                    'last_four_digits': last_four_digits,
                    'is_default': credit_card.is_default,
                    'fingerprint': fingerprint[:8],
                    'is_tokenized': is_tokenized,
                    'user_id': user_id
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
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="credit_card_set_as_default",
            severity=AuditSeverity.LOW,
            resource_type="credit_card",
            resource_id=str(card.id),
            description=f"Card ending in {card.last_four_digits} set as default",
            additional_data={
                'card_brand': card.card_brand,
                'last_four_digits': card.last_four_digits,
                'user_id': user_id
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
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="credit_card_deleted",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            resource_id=str(card.id),
            description=f"Credit card deleted for user {user_id}",
            additional_data={
                'card_brand': card.card_brand,
                'last_four_digits': card.last_four_digits,
                'user_id': user_id
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
            
            # Get the original transaction from payment
            transaction_id = payment.provider_transaction_id
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