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

from business_app.models.payment import Payment, PaymentMethod, CreditCard
from business_app.models.order import Order
from business_app.models.user import User
from business_app.models.subscription import Subscription
from business_app.utils.service_factory import get_payment_service, get_notification_service
from business_app.utils.helpers import get_current_language
from business_app.serializers.payment_serializers import (
    serialize_payment, serialize_payment_list, serialize_credit_card,
    get_available_payment_methods, PaymentSchema, CreditCardSchema,
    CreatePaymentRequest, ProcessPaymentRequest, RefundPaymentRequest
)
from business_app.utils.decorators import validate_json, rate_limit
from business_app.utils.constants import PaymentStatus, PaymentMethodType
from business_app.utils.validation_helpers import (
    validate_list_request_params, FilterValidator, PaginationHelper,
    StatusValidator, RequestDataValidator
)
from business_app.utils.error_handlers import handle_api_exception, create_success_response
from business_app.utils.exceptions import ValidationError, NotFoundError
from business_app.utils.csrf_protection import csrf_required
from business_app.tasks.payment_tasks import process_payment_verification, handle_payment_webhook
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
            return jsonify({'error': 'User not found'}), 404
        
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
        
        return jsonify({
            'available_methods': available_methods,
            'saved_cards': [
                serialize_credit_card(card) for card in saved_cards
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get payment methods error: {e}")
        return jsonify({'error': 'Failed to get payment methods'}), 500


@payments_bp.route('/create', methods=['POST'])
@jwt_required()
@csrf_required
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
            return jsonify({'error': 'Order not found'}), 404
        
        if order.is_paid:
            return jsonify({'error': 'Order is already paid'}), 400
        
        # Create payment
        payment_data = {
            'order_id': order_id,
            'user_id': current_user_id,
            'amount': order.total_amount,
            'currency': 'UZS',
            'payment_method': PaymentMethodType(payment_method),
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
        
        payment = get_payment_service().create_payment(payment_data)
        
        # For cash payments, mark as pending
        if payment_method == 'cash':
            payment.status = PaymentStatus.PENDING
            db.session.commit()
            
            return jsonify({
                'payment': serialize_payment(payment),
                'message': 'Cash payment created. Pay on delivery.'
            }), 201
        
        # For card payments, get payment link
        payment_link = get_payment_service().get_payment_link(payment)
        
        return jsonify({
            'payment': serialize_payment(payment),
            'payment_link': payment_link
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Create payment error: {e}")
        return jsonify({'error': 'Failed to create payment'}), 500


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
            return jsonify({'error': 'Subscription not found'}), 404
        
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
        
        return jsonify({
            'payment': serialize_payment(payment),
            'payment_link': payment_link
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Create subscription payment error: {e}")
        return jsonify({'error': 'Failed to create subscription payment'}), 500


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
            return jsonify({'error': 'Payment not found'}), 404
        
        # Update payment status from provider if pending
        if payment.status == PaymentStatus.PENDING:
            get_payment_service().update_payment_status(payment)
        
        return jsonify({
            'payment': serialize_payment(payment)
        })
        
    except Exception as e:
        current_app.logger.error(f"Get payment status error: {e}")
        return jsonify({'error': 'Failed to get payment status'}), 500


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
    
    return create_success_response(
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
            return jsonify({'error': 'Payment not found'}), 404
        
        if payment.status != PaymentStatus.PENDING:
            return jsonify({'error': 'Only pending payments can be cancelled'}), 400
        
        # Cancel payment
        success = get_payment_service().cancel_payment(payment)
        
        if success:
            return jsonify({
                'message': 'Payment cancelled successfully',
                'payment': serialize_payment(payment)
            })
        else:
            return jsonify({'error': 'Failed to cancel payment'}), 500
        
    except Exception as e:
        current_app.logger.error(f"Cancel payment error: {e}")
        return jsonify({'error': 'Failed to cancel payment'}), 500


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
        
        return jsonify({
            'cards': [serialize_credit_card(card) for card in cards]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get saved cards error: {e}")
        return jsonify({'error': 'Failed to get saved cards'}), 500


@payments_bp.route('/cards', methods=['POST'])
@jwt_required()
@csrf_required
@validate_json(['card_number', 'expiry_month', 'expiry_year', 'cardholder_name'])
@rate_limit(5, 60)  # Limit card saves to 5 per minute per user
def save_card():
    """Save a new credit card with comprehensive validation"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()
        
        # Comprehensive input validation
        card_number = data.get('card_number', '').strip()
        expiry_month = data.get('expiry_month')
        expiry_year = data.get('expiry_year')
        cardholder_name = data.get('cardholder_name', '').strip()
        cvv = data.get('cvv', '').strip()  # Optional for validation
        is_default = data.get('is_default', False)
        
        # Validate required fields are not empty
        if not card_number:
            return jsonify({'error': 'Card number is required'}), 400
        if not cardholder_name:
            return jsonify({'error': 'Cardholder name is required'}), 400
        
        # Validate expiry month and year data types
        try:
            expiry_month = int(expiry_month)
            expiry_year = int(expiry_year)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid expiry month or year format'}), 400
        
        # Validate boolean type for is_default
        if not isinstance(is_default, bool):
            return jsonify({'error': 'is_default must be a boolean'}), 400
        
        # Build card data for validation and saving
        card_data = {
            'user_id': current_user_id,
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
        
        return jsonify({
            'message': 'Card saved successfully',
            'card': serialize_credit_card(card)
        }), 201
        
    except ValidationError as e:
        # Handle validation errors from our card validation
        current_app.logger.warning(f"Card validation failed for user {current_user_id}: {e}")
        return jsonify({'error': str(e)}), 400
    except ValueError as e:
        # Handle other value errors
        current_app.logger.warning(f"Invalid card data for user {current_user_id}: {e}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        # Handle unexpected errors
        current_app.logger.error(f"Unexpected error saving card for user {current_user_id}: {e}")
        return jsonify({'error': 'Failed to save card. Please try again.'}), 500


@payments_bp.route('/cards/<int:card_id>', methods=['DELETE'])
@jwt_required()
@csrf_required
def delete_card(card_id):
    """Delete a saved credit card"""
    try:
        current_user_id = get_jwt_identity()
        
        # Use payment service to delete card with proper validation
        payment_service = get_payment_service()
        success = payment_service.delete_card(card_id, current_user_id)
        
        if success:
            return jsonify({'message': 'Card deleted successfully'})
        else:
            return jsonify({'error': 'Failed to delete card'}), 500
        
    except NotFoundError as e:
        return jsonify({'error': str(e)}), 404
    except ValidationError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Delete card error: {e}")
        return jsonify({'error': 'Failed to delete card'}), 500


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
        
        return jsonify({
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
        return jsonify({'error': 'Failed to get payment statistics'}), 500


@payments_bp.route('/webhook/<provider>', methods=['POST'])
@rate_limit(100, 60)  # 100 webhook requests per minute
def payment_webhook(provider):
    """Handle payment webhooks from providers with replay protection"""
    try:
        # Comprehensive webhook validation with replay protection
        payment_service = get_payment_service()
        if not payment_service.validate_webhook_signature(provider, request):
            return jsonify({'error': 'Invalid signature or replay detected'}), 401
        
        # Extract webhook data based on provider
        if provider.lower() == 'payme':
            webhook_data = request.get_json() or {}
        elif provider.lower() == 'click':
            webhook_data = dict(request.form) if request.form else request.get_json() or {}
        else:
            current_app.logger.error(f"Unsupported webhook provider: {provider}")
            return jsonify({'error': 'Unsupported provider'}), 400
        
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
        if provider.lower() == 'payme':
            return jsonify({'jsonrpc': '2.0', 'result': {'status': 'received'}}), 200
        elif provider.lower() == 'click':
            return jsonify({'error': 0, 'error_note': 'Success'}), 200
        else:
            return jsonify({'status': 'received'}), 200
        
    except Exception as e:
        current_app.logger.error(f"Payment webhook error for {provider}: {e}")
        
        # Log security incident
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
        audit_logger.log_event(
            event_type=AuditEventType.SECURITY_EVENT,
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
            return jsonify({'jsonrpc': '2.0', 'error': {'code': -32000, 'message': 'Server error'}}), 500
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
            return jsonify({'error': 'Payment not found'}), 404
        
        # Trigger payment verification
        process_payment_verification.delay(payment_id)
        
        return jsonify({
            'message': 'Payment verification initiated',
            'payment_id': payment_id
        })
        
    except Exception as e:
        current_app.logger.error(f"Verify payment error: {e}")
        return jsonify({'error': 'Failed to verify payment'}), 500


@payments_bp.route('/refund', methods=['POST'])
@jwt_required()
@csrf_required
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
            return jsonify({'error': 'Payment not found'}), 404
        
        if payment.status != PaymentStatus.COMPLETED:
            return jsonify({'error': 'Only completed payments can be refunded'}), 400
        
        # Request refund
        refund = get_payment_service().request_refund(payment, reason)
        
        return jsonify({
            'message': 'Refund request submitted',
            'refund_id': refund.id,
            'status': refund.status.value
        }), 201
        
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        current_app.logger.error(f"Request refund error: {e}")
        return jsonify({'error': 'Failed to request refund'}), 500


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
        
        return jsonify({
            'base_currency': 'UZS',
            'rates': rates
        })
        
    except Exception as e:
        current_app.logger.error(f"Get exchange rates error: {e}")
        return jsonify({'error': 'Failed to get exchange rates'}), 500