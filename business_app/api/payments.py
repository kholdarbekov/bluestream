"""
Payments API endpoints for the Water Business Platform
This file should be placed in business_app/api/payments.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func
from datetime import datetime, timedelta
try:
    from datetime import UTC
except ImportError:
    # For Python < 3.11
    from datetime import timezone
    UTC = timezone.utc

from business_app.models.payment import Payment, CreditCard
from business_app.models.order import Order
from business_app.models.user import User
from business_app.models.subscription import Subscription
from business_app.utils.service_factory import get_payment_service, get_notification_service
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from business_app.serializers.payment_serializers import (
    serialize_payment, serialize_payment_list, serialize_credit_card,
    get_available_payment_methods, PaymentSchema, CreditCardSchema,
    CreatePaymentRequest, ProcessPaymentRequest, RefundPaymentRequest
)
from business_app.utils.decorators import validate_json, rate_limit
from business_app.utils.constants import PaymeErrors, PaymentStatus, PaymentMethodType, PaymentMethod
from business_app.utils.validation_helpers import (
    validate_list_request_params, FilterValidator, PaginationHelper,
    StatusValidator, RequestDataValidator
)
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.tasks.payment_tasks import process_payment_verification, handle_payment_webhook
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, forbidden_response,
    conflict_response, internal_error_response
)
from business_app import db

payments_bp = Blueprint('payments', __name__)




@payments_bp.route('/methods', methods=['GET'])
@jwt_required()
def get_payment_methods():
    """Get available payment methods"""
    try:
        current_user_id = get_jwt_identity()

        user = User.query.get(current_user_id)
        if not user:
            return not_found_response(message='User not found')

        # Get user's saved payment methods
        saved_cards = CreditCard.query.filter_by(
            user_id=current_user_id,
            is_active=True
        ).all()

        # Available payment providers
        available_methods = [
            {
                'method': 'payme',
                'name': 'Payme',
                'icon_url': '/static/images/payment/payme.png',
                'description': 'Pay with Payme wallet or card',
                'is_active': True,
                'supported_currencies': ['UZS']
            },
            {
                'method': 'click',
                'name': 'Click',
                'icon_url': '/static/images/payment/click.png',
                'description': 'Pay with Click wallet or card',
                'is_active': True,
                'supported_currencies': ['UZS']
            },
            {
                'method': 'uzcard',
                'name': 'UzCard',
                'icon_url': '/static/images/payment/uzcard.png',
                'description': 'Pay with UzCard',
                'is_active': True,
                'supported_currencies': ['UZS']
            },
            {
                'method': 'humo',
                'name': 'Humo',
                'icon_url': '/static/images/payment/humo.png',
                'description': 'Pay with Humo card',
                'is_active': True,
                'supported_currencies': ['UZS']
            },
            {
                'method': 'cash',
                'name': 'Cash on Delivery',
                'icon_url': '/static/images/payment/cash.png',
                'description': 'Pay with cash when order is delivered',
                'is_active': True,
                'supported_currencies': ['UZS']
            }
        ]

        return success_response(data={
            'available_methods': available_methods,
            'saved_cards': [
                serialize_credit_card(card) for card in saved_cards
            ]
        })

    except Exception as e:
        current_app.logger.error(f"Get payment methods error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.get_methods_failed'))


@payments_bp.route('/create', methods=['POST'])
@jwt_required()
@validate_json(['order_id', 'payment_method'])
def create_payment():
    """Create a new payment for an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        order_id = data.get('order_id')
        payment_method = data.get('payment_method')

        # Validate order
        order = Order.query.filter_by(
            id=order_id,
            user_id=current_user_id
        ).first()

        if not order:
            return not_found_response(message='Order not found')

        if order.is_paid:
            return error_response(message=get_translation('api.payments.error.already_paid'))

        # Check for existing pending payment for this order with same payment method
        # This prevents duplicate payments when user retries payment
        existing_payment = Payment.query.filter(
            Payment.order_id == order_id,
            Payment.user_id == current_user_id,
            Payment.status == PaymentStatus.PENDING
        ).first()

        if existing_payment:
            current_app.logger.info(
                f"Reusing existing pending payment {existing_payment.id} for order {order_id}"
            )
            # For cash payments, just return the existing payment
            if payment_method == 'cash':
                return created_response(
                    data={
                        'payment': serialize_payment(existing_payment),
                        'message': 'Cash payment created. Pay on delivery.'
                    }
                )
            # For card payments, generate a fresh payment link for the existing payment
            payment_link = get_payment_service().create_payment_link(existing_payment.id)
            return created_response(
                data={
                    'payment': serialize_payment(existing_payment),
                    'payment_link': payment_link
                }
            )

        # Create new payment
        payment_data = {
            'order_id': order_id,
            'user_id': current_user_id,
            'amount': order.total_amount,
            'currency': 'UZS',
            'payment_method': PaymentMethod(payment_method),
            'description': f'Payment for order #{order.order_number}',
            'return_url': data.get('return_url'),
            'cancel_url': data.get('cancel_url'),
            'metadata': {
                'order_number': order.order_number,
                'customer_phone': order.user.phone
            }
        }

        # Use saved card if specified
        saved_card_id = data.get('saved_card_id')
        if saved_card_id:
            card = CreditCard.query.filter_by(
                id=saved_card_id,
                user_id=current_user_id,
                is_active=True
            ).first()
            if card:
                payment_data['saved_card_id'] = saved_card_id

        payment = get_payment_service().create_payment(**payment_data)

        # For cash payments, mark as pending
        if payment_method == 'cash':
            payment.status = PaymentStatus.PENDING
            db.session.commit()

            return created_response(
                data={
                    'payment': serialize_payment(payment),
                    'message': 'Cash payment created. Pay on delivery.'
                }
            )

        # For card payments, get payment link
        payment_link = get_payment_service().create_payment_link(payment.id)

        return created_response(
            data={
                'payment': serialize_payment(payment),
                'payment_link': payment_link
            }
        )

    except ValueError as e:
        return error_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Create payment error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.create_failed'))


@payments_bp.route('/subscription', methods=['POST'])
@jwt_required()
@validate_json(['subscription_id', 'payment_method'])
def create_subscription_payment():
    """Create a payment for subscription billing"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        subscription_id = data.get('subscription_id')
        payment_method = data.get('payment_method')

        # Validate subscription
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()

        if not subscription:
            return not_found_response(message='Subscription not found')

        # Create subscription payment
        payment_data = {
            'subscription_id': subscription_id,
            'user_id': current_user_id,
            'amount': subscription.billing_amount,
            'currency': 'UZS',
            'payment_method': PaymentMethodType(payment_method),
            'description': f'Subscription payment for {subscription.get_translated("name", get_current_language())}',
            'is_recurring': True,
            'return_url': data.get('return_url'),
            'cancel_url': data.get('cancel_url'),
            'metadata': {
                'subscription_name': subscription.get_translated('name', get_current_language()),
                'billing_cycle': subscription.billing_cycle
            }
        }

        payment = get_payment_service().create_payment(payment_data)
        payment_link = get_payment_service().get_payment_link(payment)

        return created_response(
            data={
                'payment': serialize_payment(payment),
                'payment_link': payment_link
            }
        )

    except ValueError as e:
        return error_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Create subscription payment error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.subscription_create_failed'))


@payments_bp.route('/<int:payment_id>/status', methods=['GET'])
@jwt_required()
def get_payment_status(payment_id):
    """Get payment status"""
    try:
        current_user_id = get_jwt_identity()

        payment = Payment.query.filter_by(
            id=payment_id,
            user_id=current_user_id
        ).first()

        if not payment:
            return not_found_response(message='Payment not found')

        # Update payment status from provider if pending
        if payment.status == PaymentStatus.PENDING:
            get_payment_service().update_payment_status(payment)

        return success_response(data={'payment': serialize_payment(payment)})

    except Exception as e:
        current_app.logger.error(f"Get payment status error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.get_status_failed'))


@payments_bp.route('/', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_payments():
    """Get user payments with pagination"""
    # Validate request parameters using centralized validation
    params = validate_list_request_params(
        default_per_page=20,
        max_per_page=50,
        allow_status_filter=True,
        status_enum=PaymentStatus,
        allow_date_filter=True,
        allow_future_dates=True
    )

    # Validate payment method filter separately
    payment_method_str = request.args.get('payment_method')
    payment_method = StatusValidator.validate_status_enum(
        payment_method_str, PaymentMethodType, 'payment_method'
    )

    # Build query
    query = Payment.query.filter_by(user_id=params['user_id'])

    # Apply filters using centralized filter builders
    query = FilterValidator.build_status_filter_query(
        query, Payment.status, params.get('status')
    )

    query = FilterValidator.build_status_filter_query(
        query, Payment.payment_method, payment_method
    )

    query = FilterValidator.build_date_filter_query(
        query, Payment.created_at, params.get('start_date'), params.get('end_date')
    )

    # Order by creation date (newest first)
    query = query.order_by(Payment.created_at.desc())

    # Paginate
    pagination = query.paginate(
        page=params['page'], per_page=params['per_page'], error_out=False
    )

    # Build standardized pagination response
    response_data = PaginationHelper.build_pagination_response(
        pagination.items, pagination, serialize_payment
    )

    return success_response(
        data={'payments': response_data['items'], 'pagination': response_data['pagination']},
        message='Payments retrieved successfully'
    )


@payments_bp.route('/<int:payment_id>/cancel', methods=['POST'])
@jwt_required()
def cancel_payment(payment_id):
    """Cancel a pending payment"""
    try:
        current_user_id = get_jwt_identity()

        payment = Payment.query.filter_by(
            id=payment_id,
            user_id=current_user_id
        ).first()

        if not payment:
            return not_found_response(message='Payment not found')

        if payment.status != PaymentStatus.PENDING:
            return error_response(message=get_translation('api.payments.error.only_pending_cancellable'))

        # Cancel payment
        success = get_payment_service().cancel_payment(payment)

        if success:
            return success_response(
                data={
                    'message': 'Payment cancelled successfully',
                    'payment': serialize_payment(payment)
                }
            )
        else:
            return internal_error_response(message=get_translation('api.payments.error.cancel_failed'))

    except Exception as e:
        current_app.logger.error(f"Cancel payment error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.cancel_failed'))


@payments_bp.route('/cards', methods=['GET'])
@jwt_required()
def get_saved_cards():
    """Get user's saved credit cards"""
    try:
        current_user_id = get_jwt_identity()

        cards = CreditCard.query.filter_by(
            user_id=current_user_id,
            is_active=True
        ).order_by(CreditCard.created_at.desc()).all()

        return success_response(data={'cards': [serialize_credit_card(card) for card in cards]})

    except Exception as e:
        current_app.logger.error(f"Get saved cards error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.get_cards_failed'))


@payments_bp.route('/tokenize', methods=['POST'])
@jwt_required()
@validate_json(['card_number', 'expiry'])
@rate_limit(10, 60)
def tokenize_card():
    """
    Tokenize card via Payme and trigger SMS verification if needed.

    This endpoint creates a card token and automatically requests SMS verification
    if the card requires it (most cards do on first use).

    Request body:
        card_number: Card number (16 digits, spaces allowed)
        expiry: Expiry date in MM/YY or MMYY format
        save: (optional) Whether to save for recurring payments, default true

    Response:
        token: Card token for subsequent operations
        masked_number: Masked card number (e.g., "860006******6311")
        expire: Expiry in MM/YY format
        needs_verification: True if SMS verification is required
        masked_phone: Phone number where SMS was sent (if verification needed)
        wait_seconds: Seconds until verification code expires
        verification_sent: True if SMS was successfully sent
    """
    try:
        data = request.get_json()
        card_number = data.get('card_number', '').replace(' ', '')
        expiry = data.get('expiry', '').replace('/', '')  # Expecting MM/YY -> MMYY
        save = data.get('save', True)  # Default to saving card

        if len(expiry) != 4:
            return error_response(message="Invalid expiry format. Use MM/YY")

        payment_service = get_payment_service()
        token_data = payment_service.create_card_token_with_verification(
            card_number, expiry, save=save
        )

        return success_response(data={
            'token': token_data.get('token'),
            'masked_number': token_data.get('masked_number'),
            'expire': token_data.get('expire'),
            'needs_verification': token_data.get('needs_verification', True),
            'masked_phone': token_data.get('masked_phone'),
            'wait_seconds': token_data.get('wait_seconds', 60),
            'verification_sent': token_data.get('verification_sent', False),
            'recurrent': token_data.get('recurrent', False),
            'type': 'payme'
        })

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Tokenization error: {e}")
        return internal_error_response(message=str(e))


@payments_bp.route('/cards', methods=['POST'])
@jwt_required()
@validate_json(['cardholder_name']) # Removed card_number requirement here as we might use token
@rate_limit(5, 60)  # Limit card saves to 5 per minute per user
def save_card():
    """Save a new credit card with comprehensive validation"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        # Comprehensive input validation
        card_number = data.get('card_number', '').strip()
        card_token = data.get('card_token')
        expiry_month = data.get('expiry_month')
        expiry_year = data.get('expiry_year')
        cardholder_name = data.get('cardholder_name', '').strip()
        cvv = data.get('cvv', '').strip()  # Optional for validation
        is_default = data.get('is_default', False)

        # Validate required fields
        # If we have a token, card_number is treated as masked/optional or last 4 digits
        if not card_token and not card_number:
            return error_response(message=get_translation('error.validation.card_number_required'))
            
        if not cardholder_name:
            return error_response(message=get_translation('error.validation.cardholder_name_required'))

        # Validate expiry month and year data types
        try:
            expiry_month = int(expiry_month)
            expiry_year = int(expiry_year)
        except (ValueError, TypeError):
            return error_response(message=get_translation('error.validation.invalid_card_expiry'))

        # Validate boolean type for is_default
        if not isinstance(is_default, bool):
            return error_response(message=get_translation('error.validation.invalid_boolean'))

        # Build card data for validation and saving
        card_data = {
            'user_id': current_user_id,
            'card_token': card_token,
            'card_number': card_number,
            'expiry_month': expiry_month,
            'expiry_year': expiry_year,
            'cardholder_name': cardholder_name,
            'is_default': is_default
        }

        # Add CVV for validation if provided (not stored)
        if cvv:
            card_data['cvv'] = cvv

        # Save card using payment service with comprehensive validation
        payment_service = get_payment_service()
        card = payment_service.save_card(card_data)

        # Log successful card save
        current_app.logger.info(f"Credit card saved successfully for user {current_user_id}")

        return created_response(
            data={
                'message': 'Card saved successfully',
                'card': serialize_credit_card(card)
            }
        )

    except ValidationError as e:
        # Handle validation errors from our card validation
        current_app.logger.warning(f"Card validation failed for user {current_user_id}: {e}")
        return validation_error_response(errors=str(e))
    except ValueError as e:
        # Handle other value errors
        current_app.logger.warning(f"Invalid card data for user {current_user_id}: {e}")
        return error_response(message=str(e))
    except Exception as e:
        # Handle unexpected errors
        current_app.logger.error(f"Unexpected error saving card for user {current_user_id}: {e}")
        return internal_error_response(message=get_translation('api.payments.error.save_card_failed'))


@payments_bp.route('/cards/<int:card_id>', methods=['DELETE'])
@jwt_required()
def delete_card(card_id):
    """Delete a saved credit card"""
    try:
        current_user_id = get_jwt_identity()

        # Use payment service to delete card with proper validation
        payment_service = get_payment_service()
        success = payment_service.delete_card(card_id, current_user_id)

        if success:
            return success_response(data={'message': 'Card deleted successfully'})
        else:
            return internal_error_response(message=get_translation('api.payments.error.delete_card_failed'))

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Delete card error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.delete_card_failed'))


@payments_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_payment_statistics():
    """Get user's payment statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'year')  # month, quarter, year, all

        # Calculate date range
        now = datetime.now(UTC)
        if period == 'month':
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'quarter':
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(month=quarter_start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'year':
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:  # all time
            start_date = None

        # Base query
        query = Payment.query.filter_by(user_id=current_user_id)
        if start_date:
            query = query.filter(Payment.created_at >= start_date)

        payments = query.all()

        # Calculate statistics
        total_payments = len(payments)
        successful_payments = len([p for p in payments if p.status == PaymentStatus.COMPLETED])
        failed_payments = len([p for p in payments if p.status == PaymentStatus.FAILED])
        total_amount = sum(p.amount for p in payments if p.status == PaymentStatus.COMPLETED)

        # Payment methods breakdown
        method_stats = {}
        for method in PaymentMethodType:
            method_payments = [p for p in payments if p.payment_method == method]
            method_stats[method.value] = {
                'count': len(method_payments),
                'total_amount': sum(p.amount for p in method_payments if p.status == PaymentStatus.COMPLETED),
                'success_rate': (len([p for p in method_payments if p.status == PaymentStatus.COMPLETED]) /
                               len(method_payments) * 100) if method_payments else 0
            }

        # Monthly spending trend
        monthly_spending = {}
        for i in range(12):
            month_start = (now.replace(day=1) - timedelta(days=32*i)).replace(day=1)
            month_end = (month_start.replace(month=month_start.month % 12 + 1)
                        if month_start.month < 12
                        else month_start.replace(year=month_start.year + 1, month=1))

            month_payments = [p for p in payments
                            if month_start <= p.created_at < month_end and p.status == PaymentStatus.COMPLETED]
            month_total = sum(p.amount for p in month_payments)

            monthly_spending[month_start.strftime('%Y-%m')] = month_total

        return success_response(data={
            'period': period,
            'statistics': {
                'total_payments': total_payments,
                'successful_payments': successful_payments,
                'failed_payments': failed_payments,
                'success_rate': round((successful_payments / total_payments * 100), 2) if total_payments > 0 else 0,
                'total_amount': total_amount,
                'average_payment': round(total_amount / successful_payments, 2) if successful_payments > 0 else 0,
                'payment_methods': method_stats,
                'monthly_spending_trend': monthly_spending
            }
        })

    except Exception as e:
        current_app.logger.error(f"Get payment statistics error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.get_stats_failed'))


@payments_bp.route('/webhook/<provider>', methods=['POST'])
@rate_limit(100, 60)  # 100 webhook requests per minute
def payment_webhook(provider):
    """Handle payment webhooks from providers with replay protection"""
    # Note: This endpoint must use jsonify() directly for provider-specific response formats
    # as payment providers (Payme, Click) expect specific JSON structures
    try:
        # Comprehensive webhook validation with replay protection
        payment_service = get_payment_service()
        if not payment_service.validate_webhook_signature(provider, request):
            if provider.lower() == 'payme':
                try:
                    json_data = request.get_json(silent=True)
                    request_id = json_data.get('id') if isinstance(json_data, dict) else None
                except Exception:
                    request_id = None
                    
                return jsonify({
                    'jsonrpc': '2.0',
                    'id': request_id,
                    'error': {'code': -32504, 'message': 'Insufficient privileges'}
                }), 200
            if provider.lower() == 'click':
                 return jsonify({'error': -1, 'error_note': 'Sign check failed'}), 200
            
            return jsonify({'error': 'Invalid signature or replay detected'}), 401

        # Extract webhook data based on provider
        if provider.lower() == 'payme':
            webhook_data = request.get_json() or {}
        elif provider.lower() == 'click':
            webhook_data = dict(request.form) if request.form else request.get_json() or {}
        else:
            current_app.logger.error(f"Unsupported webhook provider: {provider}")
            return jsonify({'error': 'Unsupported provider'}), 400

        if provider.lower() == 'payme':
            # Payme REQUIRES synchronous response with JSON-RPC result
            response_data = payment_service.handle_payme_webhook(webhook_data)
            return jsonify(response_data)
        
        # Add metadata for processing
        webhook_metadata = {
            'provider': provider.lower(),
            'webhook_data': webhook_data,
            'headers': dict(request.headers),
            'remote_addr': request.remote_addr,
            'received_at': datetime.now(UTC).isoformat(),
            'content_type': request.content_type
        }

        # Process webhook asynchronously
        handle_payment_webhook.delay(webhook_metadata, provider.lower())

        # Return provider-specific response format
        if provider.lower() == 'click':
            return jsonify({'error': 0, 'error_note': 'Success'}), 200
        else:
            return jsonify({'status': 'received'}), 200

    except Exception as e:
        current_app.logger.error(f"Payment webhook error for {provider}: {e}")

        # Log security incident
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
        audit_logger.log_event(
            event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
            action="webhook_processing_error",
            severity=AuditSeverity.HIGH,
            resource_type="payment_webhook",
            description=f"Webhook processing error for {provider}: {str(e)}",
            additional_data={
                'provider': provider,
                'error': str(e),
                'remote_addr': request.remote_addr
            }
        )

        # Return provider-specific error format
        if provider.lower() == 'payme':
            try:
                json_data = request.get_json(silent=True)
                request_id = json_data.get('id') if isinstance(json_data, dict) else None
            except Exception:
                request_id = None
                
            return jsonify({
                'jsonrpc': '2.0',
                'id': request_id,
                'error': {'code': PaymeErrors.INTERNAL_ERROR, 'message': 'Server error'}
            }), 200

        elif provider.lower() == 'click':
            return jsonify({'error': -1, 'error_note': 'Internal server error'}), 500
        else:
            return jsonify({'error': 'Webhook processing failed'}), 500


@payments_bp.route('/<int:payment_id>/verify', methods=['POST'])
@jwt_required()
def verify_payment(payment_id):
    """Manually verify payment status"""
    try:
        current_user_id = get_jwt_identity()

        payment = Payment.query.filter_by(
            id=payment_id,
            user_id=current_user_id
        ).first()

        if not payment:
            return not_found_response(message='Payment not found')

        # Trigger payment verification
        process_payment_verification.delay(payment_id)

        return success_response(data={
            'message': 'Payment verification initiated',
            'payment_id': payment_id
        })

    except Exception as e:
        current_app.logger.error(f"Verify payment error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.verify_failed'))


@payments_bp.route('/refund', methods=['POST'])
@jwt_required()
@validate_json(['payment_id'])
def request_refund():
    """Request payment refund"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        payment_id = data.get('payment_id')
        reason = data.get('reason', 'Customer request')

        payment = Payment.query.filter_by(
            id=payment_id,
            user_id=current_user_id
        ).first()

        if not payment:
            return not_found_response(message='Payment not found')

        if payment.status != PaymentStatus.COMPLETED:
            return error_response(message=get_translation('api.payments.error.only_completed_refundable'))

        # Request refund
        refund = get_payment_service().request_refund(payment, reason)

        return created_response(
            data={
                'message': 'Refund request submitted',
                'refund_id': refund.id,
                'status': refund.status.value
            }
        )

    except ValueError as e:
        return error_response(message=str(e))
    except Exception as e:
        current_app.logger.error(f"Request refund error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.refund_failed'))


@payments_bp.route('/exchange-rates', methods=['GET'])
def get_exchange_rates():
    """Get current exchange rates (if supporting multiple currencies)"""
    try:
        # For now, only UZS is supported, but this endpoint can be extended
        rates = {
            'UZS': {
                'name': 'Uzbek Som',
                'symbol': 'AC<',
                'rate': 1.0,  # Base currency
                'updated_at': datetime.now(UTC).isoformat()
            }
        }

        return success_response(data={
            'base_currency': 'UZS',
            'rates': rates
        })

    except Exception as e:
        current_app.logger.error(f"Get exchange rates error: {e}")
        return internal_error_response(message=get_translation('api.payments.error.get_rates_failed'))


# =============================================================================
# PAYME SUBSCRIBE API ENDPOINTS (Card verification and payment flow)
# =============================================================================

@payments_bp.route('/cards/create-token', methods=['POST'])
@jwt_required()
@validate_json(['card_number', 'expiry'])
@rate_limit(5, 60)  # 5 requests per minute
def create_card_token():
    """
    Create card token via Payme and trigger SMS verification.

    This is an alias for /tokenize with a more RESTful path.
    Creates a card token and automatically requests SMS verification
    if the card requires it.

    Request body:
        card_number: Card number (16 digits, spaces allowed)
        expiry: Expiry date in MM/YY or MMYY format
        cardholder_name: (optional) Cardholder name
        save: (optional) Whether to save for recurring payments, default true

    Response:
        token: Card token for subsequent operations
        masked_number: Masked card number
        expire: Expiry in MM/YY format
        needs_verification: True if SMS verification is required
        masked_phone: Phone number where SMS was sent
        wait_seconds: Seconds until verification code expires
        verification_sent: True if SMS was successfully sent
    """
    try:
        data = request.get_json()
        card_number = data.get('card_number', '').replace(' ', '').replace('-', '')
        expiry = data.get('expiry', '').replace('/', '')
        save = data.get('save', False)

        if len(expiry) != 4:
            return error_response(message="Invalid expiry format. Use MM/YY or MMYY")

        payment_service = get_payment_service()
        token_data = payment_service.create_card_token_with_verification(
            card_number, expiry, save=save
        )

        return success_response(data={
            'token': token_data.get('token'),
            'masked_number': token_data.get('masked_number'),
            'expire': token_data.get('expire'),
            'needs_verification': token_data.get('needs_verification', True),
            'masked_phone': token_data.get('masked_phone'),
            'wait_seconds': token_data.get('wait_seconds', 60),
            'verification_sent': token_data.get('verification_sent', False),
            'recurrent': token_data.get('recurrent', False)
        })

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Create card token error: {e}")
        return internal_error_response(message=str(e))


@payments_bp.route('/cards/send-verification', methods=['POST'])
@jwt_required()
@validate_json(['token', 'order_id'])
@rate_limit(3, 60)  # 3 requests per minute (prevent SMS spam)
def send_verification_code():
    """
    Send SMS verification code for a card token after order creation.

    This endpoint should be called AFTER:
    1. cards.create (via /cards/create-token) - tokenizes the card
    2. Order creation (via /orders/) - creates order and payment record

    This triggers Payme's cards.get_verify_code to send SMS to cardholder.

    Request body:
        token: Card token from create-token endpoint
        order_id: Order ID to associate with this verification

    Response:
        sent: True if SMS was sent successfully
        masked_phone: Phone number where SMS was sent
        wait_seconds: Seconds until verification code expires
    """
    try:
        data = request.get_json()
        token = data.get('token')
        order_id = data.get('order_id')

        if not token:
            return error_response(message="Card token is required")

        if not order_id:
            return error_response(message="Order ID is required")

        # Verify order exists and belongs to current user
        current_user_id = get_jwt_identity()
        order = Order.query.filter_by(id=order_id, user_id=current_user_id).first()
        if not order:
            return not_found_response(message="Order not found")

        payment_service = get_payment_service()
        result = payment_service.request_card_verification_code(token)

        current_app.logger.info(f"Verification code sent for order {order_id}: {result.get('phone')}")

        return success_response(data={
            'sent': result.get('sent', False),
            'masked_phone': result.get('phone', ''),
            'wait_seconds': result.get('wait', 60000) // 1000  # Convert ms to seconds
        })

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Send verification code error: {e}")
        return internal_error_response(message=str(e))


@payments_bp.route('/cards/resend-code', methods=['POST'])
@jwt_required()
@validate_json(['token'])
@rate_limit(3, 60)  # 3 requests per minute (prevent SMS spam)
def resend_verification_code():
    """
    Resend SMS verification code for a card token.

    Use this when the original code expires or user didn't receive it.
    Rate limited to 3 requests per minute to prevent SMS spam.

    Request body:
        token: Card token from create-token endpoint

    Response:
        sent: True if SMS was sent successfully
        masked_phone: Phone number where SMS was sent
        wait_seconds: Seconds until new code expires
    """
    try:
        data = request.get_json()
        token = data.get('token')

        if not token:
            return error_response(message="Card token is required")

        payment_service = get_payment_service()
        result = payment_service.request_card_verification_code(token)

        return success_response(data={
            'sent': result.get('sent', False),
            'masked_phone': result.get('phone', ''),
            'wait_seconds': result.get('wait', 60000) // 1000  # Convert ms to seconds
        })

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Resend verification code error: {e}")
        return internal_error_response(message=str(e))


@payments_bp.route('/cards/verify', methods=['POST'])
@jwt_required()
@validate_json(['token', 'code'])
@rate_limit(10, 60)  # 10 requests per minute
def verify_card_code():
    """
    Verify card with SMS code.

    After receiving the SMS code, call this endpoint to verify the card.
    Maximum 3 attempts per token - after that, request a new code.

    Request body:
        token: Card token from create-token endpoint
        code: Verification code from SMS (4-8 alphanumeric characters)

    Response (Success):
        verified: True
        card: Object with masked_number, expire

    Response (Wrong Code):
        success: false
        message: "Invalid verification code"
        data.attempts_remaining: Number of attempts left

    Response (Max Attempts):
        success: false
        message: "Too many failed attempts"
        data.request_new_code: true
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        token = data.get('token')
        code = data.get('code')

        if not token:
            return error_response(message="Card token is required")
        if not code:
            return error_response(message="Verification code is required")

        payment_service = get_payment_service()

        try:
            result = payment_service.verify_card(token, code)

            return success_response(data={
                'verified': True,
                'card': result.get('card', {})
            })

        except ValidationError as e:
            error_msg = str(e)

            # Get actual attempts remaining from service (tracked in Redis)
            attempts_remaining = payment_service.get_verification_attempts_remaining(token)

            # Check if user needs to request a new code
            request_new_code = attempts_remaining <= 0 or "request a new code" in error_msg.lower()

            return error_response(
                message=error_msg,
                status_code=400,
                data={
                    'attempts_remaining': attempts_remaining,
                    'request_new_code': request_new_code
                }
            )

    except Exception as e:
        current_app.logger.error(f"Verify card error: {e}")
        return internal_error_response(message=str(e))


@payments_bp.route('/process-card-payment', methods=['POST'])
@jwt_required()
@validate_json(['order_id', 'token'])
@rate_limit(5, 60)  # 5 payment attempts per minute
def process_card_payment():
    """
    Process payment with a verified card token.

    This is the final step in the payment flow. The card must be verified
    (via /cards/verify) before calling this endpoint.

    Request body:
        order_id: Order ID to pay for
        token: Verified card token
        save_card: (optional) Whether to save card for future use, default true
        card_metadata: (optional) Card details for saving
            - masked_number: Masked card number
            - expire: Expiry date
            - cardholder_name: Cardholder name

    Response (Success):
        success: true
        payment_id: Our payment record ID
        order_id: Order ID
        receipt_id: Payme receipt ID
        amount: Amount paid
        redirect_url: URL to redirect user

    Response (Failure):
        success: false
        message: Error description
    """
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        order_id = data.get('order_id')
        token = data.get('token')
        save_card = data.get('save_card', True)
        card_metadata = data.get('card_metadata', {})

        if not order_id:
            return error_response(message="Order ID is required")
        if not token:
            return error_response(message="Card token is required")

        payment_service = get_payment_service()

        result = payment_service.process_payme_payment_full(
            order_id=order_id,
            card_token=token,
            user_id=current_user_id,
            save_card=save_card,
            card_metadata=card_metadata
        )

        # Send notification on successful payment
        try:
            notification_service = get_notification_service()
            notification_service.send_payment_notification(result['payment_id'])
        except Exception as notify_error:
            current_app.logger.warning(f"Failed to send payment notification: {notify_error}")

        return success_response(data={
            'success': True,
            'payment_id': result['payment_id'],
            'order_id': result['order_id'],
            'receipt_id': result['receipt_id'],
            'amount': result.get('amount'),
            'redirect_url': result.get('redirect_url', f'/my-orders?order_id={order_id}&payment=success')
        })

    except NotFoundError as e:
        return not_found_response(message=str(e))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Process card payment error: {e}")
        return error_response(
            message=str(e),
            status_code=500,
            data={
                'order_id': data.get('order_id') if 'data' in dir() else None,
                'redirect_url': f'/checkout?error=payment_failed'
            }
        )

