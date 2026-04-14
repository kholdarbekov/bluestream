"""
Orders API endpoints
This file should be placed in business_app/api/orders.py
"""
from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from datetime import datetime, UTC, timedelta

from business_app.utils.service_factory import (
    get_order_service, get_delivery_service,
    get_notification_service, get_analytics_service, get_cart_service, get_payment_service
)
from business_app.utils.translations import get_translation
from business_app.serializers.order_serializers import (
    serialize_order, serialize_order_delivery, serialize_delivery_slot,
    serialize_order_payment, CreateOrderRequest, OrderFeedbackRequest, CartEstimateRequest
)
from business_app.utils.decorators import validate_json, rate_limit, require_verification
from business_app.utils.constants import OrderStatus, NotificationType, PaymentMethod
from business_app.utils.validation_helpers import validate_list_request_params
from business_app.utils.error_handlers import handle_api_exception
from business_app.utils.exceptions import (
    ValidationError, NotFoundError, ForbiddenError, ConflictError, TaxCommitteeUnavailableError
)
from business_app.utils.api_responses import (
    success_response, error_response, created_response,
    not_found_response, validation_error_response, forbidden_response,
    internal_error_response
)
from business_app.tasks.delivery_tasks import auto_assign_delivery_task
from business_app import db
from pydantic import ValidationError as PydanticValidationError

orders_bp = Blueprint('orders', __name__)


def _rollback_session():
    db.session.rollback()


@orders_bp.route('/', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_orders():
    """Get user orders with pagination and filtering"""
    params = validate_list_request_params(
        default_per_page=20,
        max_per_page=50,
        allow_status_filter=True,
        status_enum=OrderStatus,
        allow_date_filter=True,
        allow_future_dates=True
    )

    status_value = params.get('status')
    if hasattr(status_value, 'value'):
        status_value = status_value.value

    paginated = get_order_service().get_user_orders_paginated(
        user_id=params['user_id'],
        page=params['page'],
        per_page=params['per_page'],
        status=status_value,
        start_date=params.get('start_date'),
        end_date=params.get('end_date'),
    )

    pages = (paginated['total'] + params['per_page'] - 1) // params['per_page'] if params['per_page'] else 0
    pagination_data = {
        'page': params['page'],
        'pages': pages,
        'per_page': params['per_page'],
        'total': paginated['total'],
        'has_next': params['page'] < pages,
        'has_prev': params['page'] > 1,
    }
    
    return success_response(
        data={
            'orders': [serialize_order(order, include_items=True, include_payment=True) for order in paginated['items']],
            'pagination': pagination_data,
        },
        message=get_translation('api.orders.list_retrieved')
    )


@orders_bp.route('/<int:order_id>', methods=['GET'])
@jwt_required()
def get_order(order_id):
    """Get specific order details"""
    try:
        current_user_id = get_jwt_identity()
        details = get_order_service().get_order_details_for_user(order_id, current_user_id)
        order = details['order']
        delivery_info = serialize_order_delivery(details['delivery']) if details['delivery'] else None
        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()
        payment_timeline = cash_collection_service.get_order_payment_timeline(order.id)

        return success_response(
            data={
                'order': serialize_order(order, include_items=True, include_delivery=True, include_payment=True),
                'delivery': delivery_info,
                'timeline': details['timeline'],
                'payment_timeline': payment_timeline,
            },
            message=get_translation('api.orders.retrieved')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('api.orders.not_found'))
    except Exception as e:
        current_app.logger.error(f"Get order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/emergency', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=3600, per='user')  # 3 emergency orders per hour per user
def create_emergency_order():
    """Create an emergency order with priority delivery"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            request_data = CreateOrderRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        context = get_order_service().validate_user_emergency_order_access(current_user_id)
        user = context['user']
        _, address = get_order_service().get_user_and_address_for_order(
            current_user_id,
            request_data.delivery_address_id,
        )
        if not address:
            return error_response(
                message=get_translation('api.addresses.not_found'),
                status_code=400,
            )

        # Emergency orders have additional fee
        emergency_fee = 10000  # 10,000 UZS emergency fee

        # Create order with emergency flag.
        order_data = {
            'items': request_data.items,
            'delivery_address': {
                'delivery_address_id': address.id,
                'street': address.street_address,
                'latitude': address.latitude,
                'longitude': address.longitude,
            },
            'delivery_notes': request_data.delivery_notes,
            'is_urgent': True,
            'payment_method': request_data.payment_method,
            'order_source': request_data.source,
            'emergency_fee': emergency_fee,
        }

        # Set delivery for within 2 hours
        emergency_delivery_time = datetime.now(UTC) + timedelta(hours=2)
        order_data['delivery_date'] = emergency_delivery_time.date()
        order_data['delivery_time_slot'] = 'emergency'

        order = get_order_service().create_order(current_user_id, order_data)

        # Create priority delivery
        delivery = get_delivery_service().create_emergency_delivery(order)

        # Immediate driver assignment
        auto_assign_delivery_task.apply_async(args=[delivery.id], countdown=30)

        # Notification failures should not fail order creation.
        try:
            get_notification_service().send_notification(
                user.id,
                NotificationType.ORDER_UPDATE,
                template_data={
                    'order_number': order.order_number,
                    'estimated_delivery': emergency_delivery_time.strftime('%H:%M'),
                },
            )
        except Exception as notification_error:
            current_app.logger.error(f"Emergency order notification failed: {notification_error}")

        return created_response(
            data={
                'order': serialize_order(order, include_items=True),
                'emergency_fee': emergency_fee,
                'estimated_delivery_time': emergency_delivery_time.isoformat()
            },
            message=get_translation('api.orders.created')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('error.not_found'))
    except ForbiddenError:
        return forbidden_response(message=get_translation('error.forbidden'))
    except ConflictError:
        return error_response(message=get_translation('error.forbidden'), status_code=429)
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Create emergency order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/quick-reorder', methods=['GET'])
@jwt_required()
def get_quick_reorder_suggestions():
    """Get quick reorder suggestions based on order history"""
    try:
        current_user_id = get_jwt_identity()
        limit = min(int(request.args.get('limit', 5)), 10)
        period_days = int(request.args.get('period_days', 90))

        # Use CartService to get quick reorder suggestions
        cart_service = get_cart_service()
        suggestions = cart_service.get_quick_reorder_suggestions(
            user_id=current_user_id,
            limit=limit,
            period_days=period_days
        )

        return success_response(
            data={'quick_reorder_suggestions': suggestions},
            message=get_translation('success.saved')
        )

    except Exception as e:
        current_app.logger.error(f"Get quick reorder suggestions error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_order_statistics():
    """Get user's order statistics"""
    try:
        current_user_id = get_jwt_identity()
        period = request.args.get('period', 'year')  # month, quarter, year, all
        result = get_order_service().get_user_order_statistics(current_user_id, period=period)
        return success_response(data=result, message=get_translation('success.saved'))

    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get order statistics error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/feedback', methods=['POST'])
@jwt_required()
def submit_order_feedback(order_id):
    """Submit feedback for a completed order"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            feedback_data = OrderFeedbackRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        order = get_order_service().submit_order_feedback_for_user(
            order_id=order_id,
            user_id=current_user_id,
            rating=feedback_data.rating,
            comment=feedback_data.comment,
        )

        # Track feedback for analytics
        get_analytics_service().track_order_feedback(order_id, feedback_data.rating, feedback_data.comment)

        return success_response(message=get_translation('success.saved'))

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except NotFoundError:
        return not_found_response(message=get_translation('api.orders.not_found'))
    except (ValidationError, ConflictError) as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Submit order feedback error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/', methods=['POST'])
@jwt_required()
@require_verification('phone')
def create_order():
    """Create a new order"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            order_request = CreateOrderRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        _, address = get_order_service().get_user_and_address_for_order(
            current_user_id,
            order_request.delivery_address_id,
        )
        if not address:
            return error_response(
                message=get_translation('api.addresses.not_found'),
                status_code=400
            )

        # Create order using service
        order_data = {
            "items": order_request.items,
            "delivery_address": {
                'delivery_address_id': order_request.delivery_address_id,
                "street": address.street_address,
                "longitude": address.longitude,
                "latitude": address.latitude,
            },
            'user_id': current_user_id,
            'delivery_date': order_request.delivery_date,
            'delivery_time_slot': order_request.delivery_time_slot,
            'delivery_notes': order_request.delivery_notes,
            'is_urgent': order_request.is_urgent,
            'payment_method': order_request.payment_method,
            'loyalty_points_used': order_request.loyalty_points_used,
            'promo_code': order_request.promo_code,
            'order_source': order_request.source
        }

        order = get_order_service().create_order(current_user_id, order_data)
        current_app.logger.info(f"CREATE ORDER API: Order created successfully: order={order}")

        # Clear user's cart after successful order creation
        try:
            get_cart_service().clear_cart(current_user_id)
            current_app.logger.info(f"CREATE ORDER API: Cart cleared for user {current_user_id}")
        except Exception as e:
            current_app.logger.error(f"CREATE ORDER API: Failed to clear cart: {e}")

        # Create delivery record if delivery details provided
        if order.delivery_date and order.delivery_time_slot and not getattr(order, 'delivery', None):
            delivery = get_delivery_service().create_delivery(order.id)

            # Auto-assign delivery driver
            auto_assign_delivery_task.delay(delivery.id)

        current_app.logger.info(f"CREATE ORDER API: order_number={order.order_number}, type={type(order.order_number)}, total_amount={order.total_amount}, type={type(order.total_amount)}")

        # Send order confirmation
        # get_notification_service().send_notification(
        #     user.id,
        #     'order_created',
        #     template_data={
        #         'order_number': order.order_number,
        #         'total_amount': float(order.total_amount)
        #     }
        # )
        current_app.logger.info(f"CREATE ORDER API: send_notification finished")

        response_data = {
            'order': serialize_order(order, include_items=True, include_payment=True)
        }

        payment_method_value = order.payment_method.value if hasattr(order.payment_method, 'value') else order.payment_method

        # Pre-utilise marking codes for card/click payments so the Tax Committee
        # utilisation request happens before the user sees (and uses) the payment link.
        pre_utilization_at = None
        if payment_method_value in {'click', 'card'} and getattr(order, 'payment', None):
            from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
            try:
                pre_utilization_at = PaymentFiscalizationService().pre_utilise_marking_codes_for_payment(
                    order.payment
                )
            except TaxCommitteeUnavailableError as e:
                current_app.logger.error(f"CREATE ORDER API: Tax Committee unavailable for order {order.id}: {e}")
                # Cancel the order so inventory and marking codes are released cleanly
                try:
                    get_order_service().cancel_order(order.id, reason='tax_committee_unavailable')
                except Exception:
                    _rollback_session()
                return error_response(
                    message=get_translation('api.orders.tax_committee_unavailable'),
                    status_code=503,
                    data={'error_code': 'ASL_BELGISI_UNAVAILABLE'},
                )
            except ValidationError:
                # Bubble up — caught by outer except ValidationError block
                raise

        if payment_method_value in {'click', 'card', 'payme'} and getattr(order, 'payment', None):
            payment_link = get_payment_service().create_payment_link(order.payment.id)
            response_data['payment_link'] = payment_link
            response_data['payment_url'] = payment_link.get('payment_url') if isinstance(payment_link, dict) else payment_link

        if pre_utilization_at is not None:
            wait_seconds = int(current_app.config.get('PRE_PAYMENT_UTILISATION_WAIT_SECONDS', 45) or 45)
            response_data['pre_utilization_at'] = pre_utilization_at.isoformat()
            response_data['payment_ready_at'] = (pre_utilization_at + timedelta(seconds=wait_seconds)).isoformat()

        if (order.payment_method.value if hasattr(order.payment_method, 'value') else order.payment_method) == 'cash':
            from business_app.services.cash_collection_service import CashCollectionService

            response_data['payment_restrictions'] = CashCollectionService().get_cod_restriction_context(current_user_id)

        return created_response(
            data=response_data,
            message=get_translation('api.orders.created')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('error.not_found'))
    except ValidationError as e:
        _rollback_session()
        return error_response(message=e.message, status_code=400)
    except PydanticValidationError as e:
        _rollback_session()
        return validation_error_response(e.errors())
    except ValueError as e:
        _rollback_session()
        current_app.logger.warning(f"Create order validation error: {e}")
        return error_response(
            message=get_translation('api.orders.error.invalid_request_data'),
            status_code=400
        )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Create order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/retry-payment', methods=['POST'])
@jwt_required()
def retry_order_payment(order_id):
    """Create or refresh a payment link for an existing unpaid order."""
    try:
        current_user_id = get_jwt_identity()
        order = get_order_service().get_order(order_id, current_user_id)

        if getattr(order, 'is_paid', False):
            return error_response(
                message=get_translation('api.payments.error.already_paid'),
                status_code=409,
            )

        payment_method_value = order.payment_method.value if hasattr(order.payment_method, 'value') else order.payment_method
        if payment_method_value not in {'click', 'card', 'payme'}:
            return validation_error_response('Order does not have a retryable online payment method')

        payment_enum = PaymentMethod(payment_method_value)
        payment = get_payment_service().create_payment(
            order_id=order.id,
            payment_method=payment_enum,
            amount=order.total_amount,
            description=f'Payment for order #{order.order_number}',
        )
        payment_link = get_payment_service().create_payment_link(payment.id)

        return success_response(
            data={
                'payment': serialize_order_payment(payment),
                'payment_link': payment_link,
                'payment_url': payment_link.get('payment_url') if isinstance(payment_link, dict) else payment_link,
            },
            message=get_translation('api.payments.initiated'),
        )
    except NotFoundError:
        return not_found_response(message=get_translation('api.orders.not_found'))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Retry order payment error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/cancel', methods=['POST'])
@jwt_required()
def cancel_order(order_id):
    """Cancel an order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json(silent=True) or {}

        order = get_order_service().cancel_order(
            order_id=order_id,
            user_id=current_user_id,
            reason=data.get('reason'),
        )

        # Send cancellation notification
        get_notification_service().send_notification(
            current_user_id,
            NotificationType.ORDER_UPDATE,
            template_data={
                'order_number': order.order_number,
                'cancellation_reason': data.get('reason', 'Customer request')
            }
        )

        return success_response(
            data={'order': serialize_order(order)},
            message=get_translation('api.orders.cancelled')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('api.orders.not_found'))
    except ConflictError:
        return error_response(message=get_translation('api.orders.cannot_cancel'), status_code=400)
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Cancel order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/cart/estimate', methods=['POST'])
@jwt_required()
def estimate_cart():
    """Estimate cart total with discounts and delivery fee"""
    try:
        current_user_id = get_jwt_identity()

        # Validate request with Pydantic
        try:
            cart_request = CartEstimateRequest(**request.get_json())
        except PydanticValidationError as e:
            return validation_error_response(e.errors())

        # Use CartService to calculate cart estimate
        cart_service = get_cart_service()
        estimate = cart_service.calculate_cart_estimate(
            user_id=current_user_id,
            items=[item.dict() for item in cart_request.items],
            delivery_address_id=cart_request.delivery_address_id,
            delivery_date=cart_request.delivery_date,
            delivery_time_slot=cart_request.delivery_time_slot,
            loyalty_points_used=cart_request.loyalty_points_used,
            promo_code=cart_request.promo_code
        )

        return success_response(
            data=estimate,
            message=get_translation('success.saved')
        )

    except PydanticValidationError as e:
        return validation_error_response(e.errors())
    except NotFoundError:
        return not_found_response(message=get_translation('error.not_found'))
    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except ValueError as e:
        current_app.logger.warning(f"Estimate cart validation error: {e}")
        return error_response(
            message=get_translation('api.orders.error.invalid_request_data'),
            status_code=400
        )
    except Exception as e:
        current_app.logger.error(f"Estimate cart error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/delivery-slots', methods=['GET'])
@jwt_required()
def get_delivery_slots():
    """Get available delivery time slots"""
    try:
        delivery_date = request.args.get('delivery_date')

        if not delivery_date:
            return error_response(
                message=get_translation('error.validation.required_field'),
                status_code=400
            )

        try:
            target_date = datetime.fromisoformat(delivery_date).date()
        except ValueError:
            return error_response(
                message=get_translation('error.validation.invalid_date'),
                status_code=400
            )

        # Get available time slots
        slots = get_delivery_service().get_available_time_slots(target_date)

        return success_response(
            data={
                'delivery_date': delivery_date,
                'available_slots': [
                    serialize_delivery_slot(slot, target_date) for slot in slots
                ]
            },
            message=get_translation('success.saved')
        )

    except Exception as e:
        current_app.logger.error(f"Get delivery slots error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/promo-code/validate', methods=['POST'])
@jwt_required()
@validate_json(['promo_code'])
def validate_promo_code():
    """Validate promotional code"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        promo_code = data.get('promo_code').upper()
        cart_total = data.get('cart_total', 0)

        # Use CartService to validate promo code
        cart_service = get_cart_service()
        validation_result = cart_service.validate_promo_code(
            promo_code=promo_code,
            user_id=current_user_id,
            cart_total=cart_total
        )

        if not validation_result['valid']:
            return error_response(
                message=validation_result['message'],
                status_code=400
            )

        return success_response(
            data=validation_result,
            message=get_translation('success.saved')
        )

    except ValidationError as e:
        return validation_error_response(errors=str(e))
    except Exception as e:
        current_app.logger.error(f"Validate promo code error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/repeat/<int:order_id>', methods=['POST'])
@jwt_required()
def repeat_order(order_id):
    """Repeat a previous order"""
    try:
        current_user_id = get_jwt_identity()
        new_order = get_order_service().repeat_order_for_user(order_id, current_user_id)

        return created_response(
            data={'order': serialize_order(new_order, include_items=True)},
            message=get_translation('api.orders.created')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('api.orders.not_found'))
    except ValidationError as e:
        current_app.logger.warning(f"Repeat order validation error: {e.message}")
        return error_response(
            message=e.message,
            status_code=400
        )
    except ValueError as e:
        current_app.logger.warning(f"Repeat order validation error: {e}")
        return error_response(
            message=get_translation('api.orders.error.invalid_request_data'),
            status_code=400
        )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Repeat order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/<int:order_id>/track', methods=['GET'])
@jwt_required()
def track_order(order_id):
    """Track order status and delivery"""
    try:
        current_user_id = get_jwt_identity()
        tracking = get_order_service().get_order_tracking_for_user(order_id, current_user_id)
        order = tracking['order']
        delivery_info = serialize_order_delivery(tracking['delivery']) if tracking['delivery'] else None
        from business_app.services.cash_collection_service import CashCollectionService

        cash_collection_service = CashCollectionService()

        return success_response(
            data={
                'order': {
                    'id': order.id,
                    'order_number': order.order_number,
                    'status': order.status.value,
                    'total_amount': order.total_amount,
                    'created_at': order.created_at.isoformat(),
                    'payment_info': serialize_order_payment(order.payment) if getattr(order, 'payment', None) else None,
                },
                'delivery': delivery_info,
                'timeline': tracking['timeline'],
                'estimated_time_remaining': tracking['estimated_time_remaining'],
                'payment_timeline': cash_collection_service.get_order_payment_timeline(order.id),
            },
            message=get_translation('api.orders.retrieved')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('api.orders.not_found'))
    except Exception as e:
        current_app.logger.error(f"Track order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/bulk-action', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=5, window_seconds=300, per='user')  # 5 bulk actions per 5 minutes per user
def bulk_order_action():
    """Perform bulk action on multiple orders"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        action = data.get('action')
        order_ids = data.get('order_ids')

        if not isinstance(order_ids, list) or len(order_ids) > 100:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        valid_actions = ['confirm', 'cancel', 'mark_priority', 'assign_delivery']
        if action not in valid_actions:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Process bulk action
        results = get_order_service().perform_bulk_action(action, order_ids, current_user_id)

        return success_response(
            data={'results': results},
            message=get_translation('success.updated')
        )

    except ForbiddenError:
        return forbidden_response(message=get_translation('error.forbidden'))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Bulk order action error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/export', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=3, window_seconds=600, per='user')  # 3 exports per 10 minutes per user
def export_orders():
    """Export orders to CSV/Excel"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = get_order_service().get_user_or_raise(current_user_id)

        # Regular users can only export their own orders
        if not user.is_admin:
            filters = {'user_id': current_user_id}
        else:
            filters = data.get('filters', {})

        format_type = data.get('format', 'csv')  # csv, excel
        start_date = data.get('start_date')
        end_date = data.get('end_date')

        if format_type not in ['csv', 'excel']:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Generate export
        export_result = get_order_service().export_orders(
            format_type=format_type,
            filters=filters,
            start_date=start_date,
            end_date=end_date,
            user_id=current_user_id
        )

        return success_response(
            data={
                'download_url': export_result['download_url'],
                'file_size': export_result['file_size'],
                'expires_at': export_result['expires_at'].isoformat()
            },
            message=get_translation('success.saved')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('error.not_found'))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except Exception as e:
        current_app.logger.error(f"Export orders error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/subscription', methods=['POST'])
@jwt_required()
def create_subscription_order():
    """Create a recurring subscription order"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        user = get_order_service().get_user_or_raise(current_user_id)

        items_data = data.get('items', [])
        frequency = data.get('frequency')  # weekly, biweekly, monthly

        if frequency not in ['weekly', 'biweekly', 'monthly']:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

        # Create subscription order
        subscription_data = {
            'user_id': current_user_id,
            'delivery_address_id': data.get('delivery_address_id'),
            'frequency': frequency,
            'start_date': data.get('start_date'),
            'delivery_time_slot': data.get('delivery_time_slot'),
            'delivery_notes': data.get('delivery_notes'),
            'payment_method': data.get('payment_method'),
            'auto_pay': data.get('auto_pay', True)
        }

        subscription = get_order_service().create_subscription_order(subscription_data, items_data)
        subscription_id = subscription.get('id') if isinstance(subscription, dict) else subscription.id
        next_delivery = (
            subscription.get('next_delivery_date')
            if isinstance(subscription, dict)
            else (subscription.next_delivery_date.isoformat() if subscription.next_delivery_date else None)
        )
        status_value = subscription.get('status') if isinstance(subscription, dict) else subscription.status
        frequency_value = (
            subscription.get('delivery_frequency')
            if isinstance(subscription, dict)
            else subscription.delivery_frequency
        )

        # Send confirmation notification
        get_notification_service().send_notification(
            user.id,
            NotificationType.SUBSCRIPTION_CREATED,
            template_data={
                'subscription_id': subscription_id,
                'frequency': frequency,
                'next_delivery': next_delivery
            }
        )

        return created_response(
            data={
                'subscription': {
                    'id': subscription_id,
                    'frequency': frequency_value.value if hasattr(frequency_value, 'value') else frequency_value,
                    'status': status_value.value if hasattr(status_value, 'value') else status_value,
                    'next_delivery_date': next_delivery,
                    'created_at': subscription.get('created_at') if isinstance(subscription, dict) else subscription.created_at.isoformat()
                }
            },
            message=get_translation('api.subscriptions.created')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('error.not_found'))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except ValueError as e:
        current_app.logger.warning(f"Create subscription order validation error: {e}")
        return error_response(
            message=get_translation('api.orders.error.invalid_request_data'),
            status_code=400
        )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Create subscription order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/schedule', methods=['POST'])
@jwt_required()
def schedule_order():
    """Schedule an order for future delivery"""
    try:
        current_user_id = get_jwt_identity()
        data = request.get_json()

        get_order_service().get_user_or_raise(current_user_id)

        scheduled_date = data.get('scheduled_date')
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_date)
            if scheduled_dt.tzinfo is None:
                scheduled_dt = scheduled_dt.replace(tzinfo=UTC)
            
            if scheduled_dt <= datetime.now(UTC):
                return error_response(
                    message=get_translation('error.validation.invalid_date'),
                    status_code=400
                )
        except ValueError:
            return error_response(
                message=get_translation('error.validation.invalid_date'),
                status_code=400
            )

        # Create scheduled order
        order_data = {
            'user_id': current_user_id,
            'delivery_address_id': data.get('delivery_address_id'),
            'delivery_date': scheduled_dt.date(),
            'delivery_time_slot': data.get('delivery_time_slot'),
            'delivery_notes': data.get('delivery_notes'),
            'payment_method': data.get('payment_method'),
            'order_source': data.get('source', 'web'),
            'is_scheduled': True,
            'scheduled_date': scheduled_dt
        }

        items_data = data.get('items', [])
        order = get_order_service().create_scheduled_order(order_data, items_data)

        # Schedule order processing task
        from business_app.tasks.order_tasks import process_scheduled_order_task
        process_scheduled_order_task.apply_async(
            args=[order.id],
            eta=scheduled_dt - timedelta(hours=1)  # Process 1 hour before scheduled time
        )

        return created_response(
            data={
                'order': serialize_order(order),
                'scheduled_for': scheduled_dt.isoformat()
            },
            message=get_translation('api.orders.created')
        )

    except NotFoundError:
        return not_found_response(message=get_translation('error.not_found'))
    except ValidationError as e:
        return error_response(message=e.message, status_code=400)
    except ValueError as e:
        current_app.logger.warning(f"Schedule order validation error: {e}")
        return error_response(
            message=get_translation('api.orders.error.invalid_request_data'),
            status_code=400
        )
    except Exception as e:
        _rollback_session()
        current_app.logger.error(f"Schedule order error: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@orders_bp.route('/statuses', methods=['GET'])
def get_order_statuses():
    """
    Get all available order statuses.
    
    This endpoint provides the single source of truth for order statuses,
    ensuring UI and backend remain synchronized.
    """
    statuses = []
    for status in OrderStatus:
        # Convert enum value to human-readable label
        label = status.value.replace('_', ' ').title()
        statuses.append({
            'value': status.value,
            'label': label
        })
    
    return success_response(
        data={'statuses': statuses},
        message=get_translation('api.orders.statuses_retrieved')
    )


# ------------------------------------------------------------------
# Customer-facing bottle balance endpoints
# ------------------------------------------------------------------

@orders_bp.route('/bottles/my-balances', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_my_bottle_balances():
    """Get the current customer's bottle balances across all addresses."""
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.serializers.bottle_serializers import serialize_bottle_balance

    user_id = get_jwt_identity()
    service = BottleTrackingService()
    balances = service.get_customer_balances(user_id)
    return success_response(
        data=[serialize_bottle_balance(b) for b in balances],
    )


@orders_bp.route('/bottles/my-ledger/<int:address_id>', methods=['GET'])
@jwt_required()
@handle_api_exception
def get_my_bottle_ledger(address_id):
    """Get the current customer's bottle ledger for a specific address."""
    from business_app.services.bottle_tracking_service import BottleTrackingService
    from business_app.serializers.bottle_serializers import serialize_bottle_ledger_entry

    user_id = get_jwt_identity()
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 20, type=int), 50)

    service = BottleTrackingService()
    result = service.get_address_ledger(user_id, address_id, page=page, per_page=per_page)
    return success_response(data={
        'items': [serialize_bottle_ledger_entry(e) for e in result['items']],
        'total': result['total'],
        'page': result['page'],
        'per_page': result['per_page'],
    })
