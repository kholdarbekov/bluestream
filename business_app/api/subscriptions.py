"""
Subscriptions API endpoints
This file should be placed in business_app/api/subscriptions.py
"""
from flask import Blueprint, request, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from business_app.utils.service_factory import (
    get_subscription_service, get_notification_service
)
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from business_app.serializers.subscription_serializers import (
    serialize_subscription, serialize_subscription_item, serialize_subscription_log,
    SubscriptionSchema, SubscriptionItemSchema, CreateSubscriptionRequest, UpdateSubscriptionRequest,
    PauseSubscriptionRequest, CancelSubscriptionRequest, AddSubscriptionItemRequest,
    UpdateSubscriptionItemRequest, SubscriptionPreviewRequest, SubscriptionPreviewResponse,
    ChangePaymentMethodRequest, SkipDeliveryRequest
)
from business_app.utils.constants import NotificationType
from business_app.utils.exceptions import NotFoundError, ValidationError, ConflictError
from business_app.utils.pydantic_helpers import (
    validate_json_with_model, serialize_database_model, serialize_response
)
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, conflict_response, internal_error_response
)
from business_app.tasks.subscription_tasks import process_subscription_billing
from business_app import db

subscriptions_bp = Blueprint('subscriptions', __name__)




@subscriptions_bp.route('/', methods=['GET'])
@jwt_required()
def get_subscriptions():
    """Get user's subscriptions with filtering and pagination"""
    try:
        current_user_id = get_jwt_identity()

        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 50)
        status = request.args.get('status')
        billing_cycle = request.args.get('billing_cycle')

        pagination = get_subscription_service().get_user_subscriptions_paginated(
            user_id=current_user_id,
            page=page,
            per_page=per_page,
            status=status,
            billing_cycle=billing_cycle,
        )

        return paginated_response(
            items=[serialize_subscription(sub) for sub in pagination.items],
            page=page,
            per_page=per_page,
            total=pagination.total,
            additional_meta={'subscriptions_key': 'items'}
        )

    except ValidationError as e:
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Get subscriptions error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_failed'))


@subscriptions_bp.route('/<int:subscription_id>', methods=['GET'])
@jwt_required()
def get_subscription(subscription_id):
    """Get specific subscription details"""
    try:
        current_user_id = get_jwt_identity()
        details = get_subscription_service().get_subscription_details_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )
        subscription = details['subscription']
        recent_orders = details['recent_orders']
        billing_info = details['billing_info']

        return success_response(
            data={
                'subscription': serialize_subscription(subscription, include_items=True),
                'recent_orders': [
                    {
                        'id': order.id,
                        'order_number': order.order_number,
                        'status': order.status.value,
                        'total_amount': order.total_amount,
                        'created_at': order.created_at.isoformat() if order.created_at else None
                    }
                    for order in recent_orders
                ],
                'billing_info': billing_info
            }
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except Exception as e:
        current_app.logger.error(f"Get subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_failed'))


@subscriptions_bp.route('/', methods=['POST'])
@jwt_required()
@validate_json_with_model(CreateSubscriptionRequest)
def create_subscription():
    """Create a new subscription"""
    try:
        current_user_id = get_jwt_identity()

        # Get validated data from the decorator
        validated_data = request.validated_json
        subscription = get_subscription_service().create_subscription_for_user(
            user_id=current_user_id,
            validated_data=validated_data,
        )
        current_app.logger.info(f"Create subscription: SUCCESSFULL get_subscription_service().create_subscription, subscription: {subscription}")
        # Send confirmation notification
        get_notification_service().send_notification(
            current_user_id,
            NotificationType.SUBSCRIPTION_CREATED,
            template_data={
                'subscription_name': subscription.get('name'),
                'billing_amount': subscription.get('billing_amount'),
                'billing_cycle': subscription.get('billing_cycle'),
                'next_billing_date': subscription.get('next_billing_date')
            }
        )

        current_app.logger.info(f"Create subscription: SUCCESSFULL send_notification")

        # Use Pydantic schema for response
        # subscription_response = serialize_database_model(subscription, SubscriptionSchema)
        # current_app.logger.info(f"Create subscription: SUCCESSFULL serialize_database_model(subscription), subscription_response: {subscription_response}")
        # if subscription.subscription_items:
        #     subscription_response['subscription_items'] = [
        #         serialize_database_model(item, SubscriptionItemSchema)
        #         for item in subscription.subscription_items
        #     ]

        #     current_app.logger.info(f"Create subscription: SUCCESSFULL serialize_database_model(subscription_items), subscription_response['subscription_items']: {subscription_response['subscription_items']}")

        return created_response(
            data={'subscription': subscription},
            message=get_translation('api.subscriptions.created')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        current_app.logger.warning(f"Create subscription validation error: {e}")
        return error_response(get_translation(e.message), status_code=400)
    except ValueError as e:
        db.session.rollback()
        current_app.logger.warning(f"Create subscription validation error: {e}")
        return error_response(get_translation('api.subscriptions.error.validation_failed'), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.create_failed'))


@subscriptions_bp.route('/<int:subscription_id>', methods=['PUT'])
@jwt_required()
@validate_json_with_model(UpdateSubscriptionRequest)
def update_subscription(subscription_id):
    """Update subscription details"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        subscription = get_subscription_service().update_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            update_data=validated_data.model_dump(exclude_none=True),
        )

        # Use Pydantic schema for response
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)

        return success_response(
            data={'subscription': subscription_response},
            message=get_translation('api.subscriptions.updated')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.update_failed'))


@subscriptions_bp.route('/<int:subscription_id>/pause', methods=['POST'])
@jwt_required()
@validate_json_with_model(PauseSubscriptionRequest)
def pause_subscription(subscription_id):
    """Pause a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        reason = validated_data.reason or get_translation('api.subscriptions.default_reason_customer_request')
        resume_date = validated_data.resume_date
        subscription = get_subscription_service().pause_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            reason=reason,
            resume_date=resume_date,
        )

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            'subscription_paused',
            template_data={
                'subscription_name': subscription.get_translated('name', language),
                'pause_reason': reason,
                'resume_date': (
                    resume_date.strftime('%Y-%m-%d')
                    if resume_date else get_translation('api.subscriptions.manual_resume_required')
                )
            }
        )

        # Schedule automatic resume if date specified
        if resume_date:
            from business_app.tasks.subscription_tasks import resume_subscription_task
            resume_subscription_task.apply_async(
                args=[subscription_id],
                eta=resume_date
            )

        # Use Pydantic schema for response
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)

        return success_response(
            data={'subscription': subscription_response},
            message=get_translation('api.subscriptions.paused')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Pause subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.pause_failed'))


@subscriptions_bp.route('/<int:subscription_id>/resume', methods=['POST'])
@jwt_required()
def resume_subscription(subscription_id):
    """Resume a paused subscription"""
    try:
        current_user_id = get_jwt_identity()
        subscription = get_subscription_service().resume_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            'subscription_resumed',
            template_data={
                'subscription_name': subscription.get_translated('name', language),
                'next_billing_date': subscription.next_billing_date.strftime('%Y-%m-%d')
            }
        )

        return success_response(
            data={
                'subscription': serialize_subscription(subscription)
            },
            message=get_translation('api.subscriptions.resumed')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Resume subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.resume_failed'))


@subscriptions_bp.route('/<int:subscription_id>/cancel', methods=['POST'])
@jwt_required()
@validate_json_with_model(CancelSubscriptionRequest)
def cancel_subscription(subscription_id):
    """Cancel a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json

        reason = validated_data.reason or get_translation('api.subscriptions.default_reason_customer_request')
        immediate = validated_data.immediate
        subscription = get_subscription_service().cancel_subscription_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            reason=reason,
            immediate=immediate,
        )

        # Send notification
        notification_type = NotificationType.SUBSCRIPTION_CANCELLED if immediate else NotificationType.SUBSCRIPTION_CANCELLATION_SCHEDULED
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            notification_type,
            template_data={
                'subscription_name': subscription.get_translated('name', language),
                'cancellation_reason': reason,
                'effective_date': (
                    subscription.end_date.strftime('%Y-%m-%d')
                    if subscription.end_date else get_translation('api.subscriptions.immediate')
                )
            }
        )

        # Use Pydantic schema for response
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)
        current_app.logger.info(f"Cancel subscription: subscription_response: {subscription_response}, subscription.to_dict(): {subscription.to_dict()}")

        message = (
            get_translation('api.subscriptions.cancelled')
            if immediate
            else get_translation('api.subscriptions.cancellation_scheduled')
        )
        return success_response(
            data={'subscription': subscription.to_dict()},
            message=message
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Cancel subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.cancel_failed'))


@subscriptions_bp.route('/<int:subscription_id>/items', methods=['GET'])
@jwt_required()
def get_subscription_items(subscription_id):
    """Get subscription items"""
    try:
        current_user_id = get_jwt_identity()
        items = get_subscription_service().get_subscription_items_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )

        return success_response(
            data={
                'items': [
                    serialize_subscription_item(item) for item in items
                ]
            }
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except Exception as e:
        current_app.logger.error(f"Get subscription items error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_items_failed'))


@subscriptions_bp.route('/<int:subscription_id>/items', methods=['POST'])
@jwt_required()
@validate_json_with_model(AddSubscriptionItemRequest)
def add_subscription_item(subscription_id):
    """Add item to subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        language = get_current_language()
        item_result = get_subscription_service().add_subscription_item_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            product_id=validated_data.product_id,
            quantity=validated_data.quantity,
            special_instructions=validated_data.special_instructions,
            language=language,
        )
        item = item_result['item']
        billing_amount = item_result['billing_amount']

        # Use Pydantic schema for response
        # item_response = serialize_database_model(item, SubscriptionItemSchema)
        item_response = item.to_dict()
        current_app.logger.info(f"Add subscription item SUCCESS: item_response: {item_response}")

        return created_response(
            data={
                'item': item_response,
                'new_billing_amount': float(billing_amount)
            },
            message=get_translation('api.subscriptions.item_added')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except ConflictError as e:
        db.session.rollback()
        return conflict_response(get_translation(e.message))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Add subscription item error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.add_item_failed'))


@subscriptions_bp.route('/<int:subscription_id>/items/<int:item_id>', methods=['PUT'])
@jwt_required()
@validate_json_with_model(UpdateSubscriptionItemRequest)
def update_subscription_item(subscription_id, item_id):
    """Update subscription item quantity"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        update_result = get_subscription_service().update_subscription_item_for_user(
            subscription_id=subscription_id,
            item_id=item_id,
            user_id=current_user_id,
            quantity=validated_data.quantity,
            special_instructions=validated_data.special_instructions,
        )
        item = update_result['item']
        billing_amount = update_result['billing_amount']

        # Use Pydantic schema for response
        item_response = serialize_database_model(item, SubscriptionItemSchema)

        return success_response(
            data={
                'item': item_response,
                'new_billing_amount': float(billing_amount)
            },
            message=get_translation('api.subscriptions.item_updated')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update subscription item error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.update_item_failed'))


@subscriptions_bp.route('/<int:subscription_id>/items/<int:item_id>', methods=['DELETE'])
@jwt_required()
def remove_subscription_item(subscription_id, item_id):
    """Remove item from subscription"""
    try:
        current_user_id = get_jwt_identity()
        subscription = get_subscription_service().remove_subscription_item_for_user(
            subscription_id=subscription_id,
            item_id=item_id,
            user_id=current_user_id,
        )

        return success_response(
            data={
                'new_billing_amount': subscription.billing_amount
            },
            message=get_translation('api.subscriptions.item_removed')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Remove subscription item error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.remove_item_failed'))


@subscriptions_bp.route('/<int:subscription_id>/billing-history', methods=['GET'])
@jwt_required()
def get_billing_history(subscription_id):
    """Get subscription billing history"""
    try:
        current_user_id = get_jwt_identity()
        billing_data = get_subscription_service().get_subscription_billing_history_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )

        return success_response(
            data=billing_data
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except Exception as e:
        current_app.logger.error(f"Get billing history error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_billing_history_failed'))


@subscriptions_bp.route('/<int:subscription_id>/logs', methods=['GET'])
@jwt_required()
def get_subscription_logs(subscription_id):
    """Get subscription activity logs"""
    try:
        current_user_id = get_jwt_identity()
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 50)

        pagination = get_subscription_service().get_subscription_logs_paginated_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            page=page,
            per_page=per_page,
        )

        return paginated_response(
            items=[serialize_subscription_log(log) for log in pagination.items],
            page=page,
            per_page=per_page,
            total=pagination.total
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except Exception as e:
        current_app.logger.error(f"Get subscription logs error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_logs_failed'))


@subscriptions_bp.route('/templates', methods=['GET'])
def get_subscription_templates():
    """Get subscription templates/presets"""
    try:
        # Predefined subscription templates
        templates = [
            {
                'id': 'basic_weekly',
                'name': get_translation('api.subscriptions.templates.basic_weekly.name'),
                'description': get_translation('api.subscriptions.templates.basic_weekly.description'),
                'billing_cycle': 'weekly',
                'delivery_frequency': 'weekly',
                'discount_percentage': 5.0,
                'suggested_items': [
                    {'product_id': 1, 'quantity': 4},  # 4x 19L bottles
                ],
                'estimated_monthly_cost': 45000
            },
            {
                'id': 'family_monthly',
                'name': get_translation('api.subscriptions.templates.family_monthly.name'),
                'description': get_translation('api.subscriptions.templates.family_monthly.description'),
                'billing_cycle': 'monthly',
                'delivery_frequency': 'monthly',
                'discount_percentage': 10.0,
                'suggested_items': [
                    {'product_id': 1, 'quantity': 16},  # 16x 19L bottles
                    {'product_id': 2, 'quantity': 8},   # 8x 5L bottles
                ],
                'estimated_monthly_cost': 160000
            },
            {
                'id': 'office_daily',
                'name': get_translation('api.subscriptions.templates.office_daily.name'),
                'description': get_translation('api.subscriptions.templates.office_daily.description'),
                'billing_cycle': 'monthly',
                'delivery_frequency': 'daily',
                'discount_percentage': 15.0,
                'suggested_items': [
                    {'product_id': 1, 'quantity': 2},   # 2x 19L bottles daily
                    {'product_id': 3, 'quantity': 4},   # 4x 1L bottles daily
                ],
                'estimated_monthly_cost': 280000
            }
        ]

        return success_response(data={'templates': templates})

    except Exception as e:
        current_app.logger.error(f"Get subscription templates error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_templates_failed'))


@subscriptions_bp.route('/preview', methods=['POST'])
@jwt_required()
@validate_json_with_model(SubscriptionPreviewRequest)
def preview_subscription():
    """Preview subscription cost and details before creation"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json

        # Calculate subscription preview
        preview = get_subscription_service().calculate_subscription_preview(
            user_id=current_user_id,
            billing_cycle=validated_data.billing_cycle,
            delivery_frequency=validated_data.delivery_frequency,
            items=validated_data.items,
            discount_percentage=validated_data.discount_percentage
        )

        # Serialize the preview response using the response schema
        preview_response = serialize_response(preview, SubscriptionPreviewResponse)

        return success_response(
            data={'preview': preview_response},
            message=get_translation('api.subscriptions.preview_calculated')
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except ValueError as e:
        current_app.logger.warning(f"Preview subscription validation error: {e}")
        return error_response(get_translation('api.subscriptions.error.validation_failed'), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Preview subscription error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.preview_failed'))


@subscriptions_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_subscription_statistics():
    """Get user's subscription statistics"""
    try:
        current_user_id = get_jwt_identity()
        language = get_current_language()
        statistics = get_subscription_service().get_subscription_statistics_for_user(
            user_id=current_user_id,
            language=language,
        )

        return success_response(
            data={
                'statistics': statistics
            }
        )

    except Exception as e:
        current_app.logger.error(f"Get subscription statistics error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.get_statistics_failed'))


@subscriptions_bp.route('/<int:subscription_id>/skip-next-delivery', methods=['POST'])
@jwt_required()
@validate_json_with_model(SkipDeliveryRequest)
def skip_next_delivery(subscription_id):
    """Skip the next delivery for a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        reason = validated_data.reason or get_translation('api.subscriptions.default_reason_customer_request')
        skip_result = get_subscription_service().skip_next_delivery_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            reason=reason,
        )
        subscription = skip_result['subscription']
        current_next_delivery = skip_result['original_delivery_date']
        new_next_delivery = skip_result['new_next_delivery_date']

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            'delivery_skipped',
            template_data={
                'subscription_name': subscription.get_translated('name', language),
                'skipped_date': current_next_delivery.strftime('%Y-%m-%d'),
                'next_delivery_date': new_next_delivery.strftime('%Y-%m-%d'),
                'reason': reason
            }
        )

        return success_response(
            data={
                'original_delivery_date': current_next_delivery.isoformat(),
                'new_next_delivery_date': new_next_delivery.isoformat()
            },
            message=get_translation('api.subscriptions.next_delivery_skipped')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Skip next delivery error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.skip_failed'))


@subscriptions_bp.route('/<int:subscription_id>/change-payment-method', methods=['POST'])
@jwt_required()
@validate_json_with_model(ChangePaymentMethodRequest)
def change_payment_method(subscription_id):
    """Change payment method for subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        payment_change = get_subscription_service().change_payment_method_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
            payment_method=validated_data.payment_method,
        )
        subscription = payment_change['subscription']
        old_payment_method = payment_change['old_payment_method']
        new_payment_method = payment_change['new_payment_method']

        # Send notification
        language = get_current_language()
        get_notification_service().send_notification(
            current_user_id,
            'payment_method_changed',
            template_data={
                'subscription_name': subscription.get_translated('name', language),
                'old_method': old_payment_method.value,
                'new_method': new_payment_method.value
            }
        )

        subscription_response = serialize_database_model(subscription, SubscriptionSchema)

        return success_response(
            data={'subscription': subscription_response},
            message=get_translation('api.subscriptions.payment_method_updated')
        )

    except NotFoundError as e:
        db.session.rollback()
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        db.session.rollback()
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Change payment method error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.change_payment_failed'))


@subscriptions_bp.route('/<int:subscription_id>/retry-billing', methods=['POST'])
@jwt_required()
def retry_billing(subscription_id):
    """Retry failed billing for subscription"""
    try:
        current_user_id = get_jwt_identity()
        get_subscription_service().validate_retry_billing_for_user(
            subscription_id=subscription_id,
            user_id=current_user_id,
        )

        # Process billing retry asynchronously
        process_subscription_billing.delay(subscription_id, retry=True)

        return success_response(
            message=get_translation('api.subscriptions.billing_retry_initiated')
        )

    except NotFoundError as e:
        return not_found_response(get_translation(e.message))
    except ValidationError as e:
        return error_response(get_translation(e.message), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Retry billing error: {e}")
        return internal_error_response(get_translation('api.subscriptions.error.retry_billing_failed'))
