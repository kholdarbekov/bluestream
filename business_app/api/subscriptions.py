"""
Subscriptions API endpoints
This file should be placed in business_app/api/subscriptions.py
"""
from flask import Blueprint, request, jsonify, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, desc, func
from datetime import datetime, UTC, timedelta

from business_app.models.subscription import Subscription, SubscriptionItem, SubscriptionLog
from business_app.models.product import Product
from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.utils.service_factory import (
    get_subscription_service, get_payment_service, get_notification_service
)
from business_app.serializers.subscription_serializers import (
    serialize_subscription, serialize_subscription_item, serialize_subscription_billing_info,
    serialize_subscription_statistics, serialize_subscription_preview, serialize_subscription_log,
    SubscriptionSchema, SubscriptionItemSchema, CreateSubscriptionRequest, UpdateSubscriptionRequest,
    PauseSubscriptionRequest, CancelSubscriptionRequest, AddSubscriptionItemRequest,
    UpdateSubscriptionItemRequest, SubscriptionPreviewRequest, SubscriptionPreviewResponse, 
    ChangePaymentMethodRequest, SkipDeliveryRequest
)
from business_app.utils.constants import SubscriptionStatus, PaymentMethod
from business_app.utils.pydantic_helpers import (
    validate_json_with_model, create_success_response, create_error_response,
    paginated_response, serialize_database_model, serialize_response
)
from business_app.tasks.subscription_tasks import process_subscription_billing, send_subscription_reminder
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
        
        # Build query
        query = Subscription.query.filter_by(user_id=current_user_id)
        
        # Apply filters
        if status:
            try:
                sub_status = SubscriptionStatus(status)
                query = query.filter_by(status=sub_status)
            except ValueError:
                return jsonify({'error': 'Invalid status value'}), 400
        
        if billing_cycle:
            query = query.filter_by(billing_cycle=billing_cycle)
        
        # Order by creation date (newest first)
        query = query.order_by(Subscription.created_at.desc())
        
        # Paginate
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'subscriptions': [
                serialize_subscription(sub) for sub in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get subscriptions error: {e}")
        return jsonify({'error': 'Failed to get subscriptions'}), 500


@subscriptions_bp.route('/<int:subscription_id>', methods=['GET'])
@jwt_required()
def get_subscription(subscription_id):
    """Get specific subscription details"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        # Get subscription orders
        recent_orders = Order.query.filter_by(
            subscription_id=subscription_id
        ).order_by(Order.created_at.desc()).limit(10).all()
        
        # Get upcoming billing info
        billing_info = get_subscription_service().get_billing_info(subscription_id)
        
        return jsonify({
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
        })
        
    except Exception as e:
        current_app.logger.error(f"Get subscription error: {e}")
        return jsonify({'error': 'Failed to get subscription'}), 500


@subscriptions_bp.route('/', methods=['POST'])
@jwt_required()
@validate_json_with_model(CreateSubscriptionRequest)
def create_subscription():
    """Create a new subscription"""
    try:
        current_user_id = get_jwt_identity()
        
        # Get validated data from the decorator
        validated_data = request.validated_json
        
        user = User.query.get(current_user_id)
        if not user:
            return create_error_response('User not found', 404)
        
        # Validate delivery address exists and belongs to user
        delivery_address_id = validated_data.delivery_address_id
        
        address = UserAddress.query.filter_by(
            id=delivery_address_id,
            user_id=current_user_id
        ).first()
        if not address:
            return create_error_response('Invalid delivery address', 404)
        
        # Create subscription using validated data
        subscription_data = {
            'user_id': current_user_id,
            'name': validated_data.name,
            'description': validated_data.description or '',
            'billing_cycle': validated_data.billing_cycle.value,
            'delivery_frequency': validated_data.delivery_frequency.value,
            'delivery_day_of_week': validated_data.delivery_day_of_week,
            'delivery_day_of_month': validated_data.delivery_day_of_month,
            'delivery_time_slot': validated_data.delivery_time_slot,
            'delivery_address_id': delivery_address_id,
            'payment_method': PaymentMethod(validated_data.payment_method),
            'auto_payment': validated_data.auto_payment,
            'auto_renew': validated_data.auto_renew,
            'discount_percentage': validated_data.discount_percentage,
            'start_date': validated_data.start_date or datetime.now(UTC),
            'end_date': validated_data.end_date
        }
        
        subscription = get_subscription_service().create_subscription(subscription_data, validated_data.items)
        
        # Send confirmation notification
        get_notification_service().send_notification(
            user.id,
            'subscription_created',
            template_data={
                'subscription_name': subscription.name,
                'billing_amount': subscription.billing_amount,
                'billing_cycle': subscription.billing_cycle,
                'next_billing_date': subscription.next_billing_date.strftime('%Y-%m-%d')
            }
        )
        
        # Use Pydantic schema for response
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)
        if subscription.subscription_items:
            subscription_response['subscription_items'] = [
                serialize_database_model(item, SubscriptionItemSchema) 
                for item in subscription.subscription_items
            ]
        
        return create_success_response(
            message='Subscription created successfully',
            data={'subscription': subscription_response},
            status_code=201
        )
        
    except ValueError as e:
        db.session.rollback()
        return create_error_response(str(e), 400)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Create subscription error: {e}")
        return create_error_response('Failed to create subscription', 500)


@subscriptions_bp.route('/<int:subscription_id>', methods=['PUT'])
@jwt_required()
@validate_json_with_model(UpdateSubscriptionRequest)
def update_subscription(subscription_id):
    """Update subscription details"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', 404)
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            return create_error_response('Cannot update cancelled subscription', 400)
        
        # Update fields from validated data
        changes = {}
        update_data = validated_data.model_dump(exclude_none=True)
        
        for field, new_value in update_data.items():
            if hasattr(subscription, field):
                old_value = getattr(subscription, field)
                
                # Special validation for delivery address
                if field == 'delivery_address_id':
                    address = UserAddress.query.filter_by(
                        id=new_value,
                        user_id=current_user_id
                    ).first()
                    if not address:
                        return create_error_response('Invalid delivery address', 404)
                
                # Special handling for payment method
                if field == 'payment_method':
                    try:
                        new_value = PaymentMethod(new_value)
                    except ValueError:
                        return create_error_response('Invalid payment method', 400)
                
                setattr(subscription, field, new_value)
                changes[field] = {'old': old_value, 'new': new_value}
        
        subscription.updated_at = datetime.now(UTC)
        
        # Log the changes
        if changes:
            log = SubscriptionLog(
                subscription_id=subscription_id,
                action='updated',
                details=f"Updated fields: {', '.join(changes.keys())}",
                user_id=current_user_id,
                extra_data={'changes': changes}
            )
            db.session.add(log)
        
        db.session.commit()
        
        # Use Pydantic schema for response
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)
        
        return create_success_response(
            message='Subscription updated successfully',
            data={'subscription': subscription_response}
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update subscription error: {e}")
        return create_error_response('Failed to update subscription', 500)


@subscriptions_bp.route('/<int:subscription_id>/pause', methods=['POST'])
@jwt_required()
@validate_json_with_model(PauseSubscriptionRequest)
def pause_subscription(subscription_id):
    """Pause a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', 404)
        
        if subscription.status != SubscriptionStatus.ACTIVE:
            return create_error_response('Only active subscriptions can be paused', 400)
        
        reason = validated_data.reason or 'Customer request'
        resume_date = validated_data.resume_date
        
        # Validate resume date if provided
        if resume_date and resume_date <= datetime.now(UTC):
            return create_error_response('Resume date must be in the future', 400)
        
        # Pause the subscription
        subscription.pause(reason=reason, resume_date=resume_date)
        db.session.commit()
        
        # Send notification
        get_notification_service().send_notification(
            current_user_id,
            'subscription_paused',
            template_data={
                'subscription_name': subscription.name,
                'pause_reason': reason,
                'resume_date': resume_date.strftime('%Y-%m-%d') if resume_date else 'Manual resume required'
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
        
        return create_success_response(
            message='Subscription paused successfully',
            data={'subscription': subscription_response}
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Pause subscription error: {e}")
        return create_error_response('Failed to pause subscription', 500)


@subscriptions_bp.route('/<int:subscription_id>/resume', methods=['POST'])
@jwt_required()
def resume_subscription(subscription_id):
    """Resume a paused subscription"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        if subscription.status != SubscriptionStatus.PAUSED:
            return jsonify({'error': 'Only paused subscriptions can be resumed'}), 400
        
        # Resume the subscription
        subscription.resume()
        db.session.commit()
        
        # Send notification
        get_notification_service().send_notification(
            current_user_id,
            'subscription_resumed',
            template_data={
                'subscription_name': subscription.name,
                'next_billing_date': subscription.next_billing_date.strftime('%Y-%m-%d')
            }
        )
        
        return jsonify({
            'message': 'Subscription resumed successfully',
            'subscription': serialize_subscription(subscription)
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Resume subscription error: {e}")
        return jsonify({'error': 'Failed to resume subscription'}), 500


@subscriptions_bp.route('/<int:subscription_id>/cancel', methods=['POST'])
@jwt_required()
@validate_json_with_model(CancelSubscriptionRequest)
def cancel_subscription(subscription_id):
    """Cancel a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', 404)
        
        if subscription.status == SubscriptionStatus.CANCELLED:
            return create_error_response('Subscription is already cancelled', 400)
        
        reason = validated_data.reason or 'Customer request'
        immediate = validated_data.immediate
        
        # Cancel the subscription
        if immediate:
            subscription.cancel(reason=reason)
        else:
            # Cancel at end of current billing period
            subscription.auto_renew = False
            subscription.end_date = subscription.next_billing_date
            
            log = SubscriptionLog(
                subscription_id=subscription_id,
                action='cancellation_scheduled',
                details=f"Subscription will be cancelled on {subscription.end_date.strftime('%Y-%m-%d')}. Reason: {reason}",
                user_id=current_user_id
            )
            db.session.add(log)
        
        db.session.commit()
        
        # Send notification
        template = 'subscription_cancelled' if immediate else 'subscription_cancellation_scheduled'
        get_notification_service().send_notification(
            current_user_id,
            template,
            template_data={
                'subscription_name': subscription.name,
                'cancellation_reason': reason,
                'effective_date': subscription.end_date.strftime('%Y-%m-%d') if subscription.end_date else 'Immediate'
            }
        )
        
        # Use Pydantic schema for response
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)
        
        message = 'Subscription cancelled successfully' if immediate else 'Subscription cancellation scheduled'
        return create_success_response(
            message=message,
            data={'subscription': subscription_response}
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Cancel subscription error: {e}")
        return create_error_response('Failed to cancel subscription', 500)


@subscriptions_bp.route('/<int:subscription_id>/items', methods=['GET'])
@jwt_required()
def get_subscription_items(subscription_id):
    """Get subscription items"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        return jsonify({
            'items': [
                serialize_subscription_item(item) for item in subscription.subscription_items
            ]
        })
        
    except Exception as e:
        current_app.logger.error(f"Get subscription items error: {e}")
        return jsonify({'error': 'Failed to get subscription items'}), 500


@subscriptions_bp.route('/<int:subscription_id>/items', methods=['POST'])
@jwt_required()
@validate_json_with_model(AddSubscriptionItemRequest)
def add_subscription_item(subscription_id):
    """Add item to subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', 404)
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            return create_error_response('Cannot modify cancelled subscription', 400)
        
        product_id = validated_data.product_id
        quantity = validated_data.quantity
        
        # Validate product
        product = Product.query.filter_by(id=product_id, is_active=True).first()
        if not product:
            return create_error_response('Product not found', 404)
        
        # Check if item already exists
        existing_item = SubscriptionItem.query.filter_by(
            subscription_id=subscription_id,
            product_id=product_id
        ).first()
        
        if existing_item:
            return create_error_response('Product already exists in subscription', 409)
        
        # Add new item
        item = SubscriptionItem(
            subscription_id=subscription_id,
            product_id=product_id,
            quantity=quantity,
            unit_price=product.current_price,
            product_name=product.name,
            product_sku=product.sku,
            special_instructions=validated_data.special_instructions
        )
        item.calculate_total()
        
        db.session.add(item)
        
        # Recalculate subscription billing amount
        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        
        # Log the change
        log = SubscriptionLog(
            subscription_id=subscription_id,
            action='item_added',
            details=f"Added {quantity}x {product.name}",
            user_id=current_user_id
        )
        db.session.add(log)
        
        db.session.commit()
        
        # Use Pydantic schema for response
        item_response = serialize_database_model(item, SubscriptionItemSchema)
        
        return create_success_response(
            message='Item added to subscription successfully',
            data={
                'item': item_response,
                'new_billing_amount': float(subscription.billing_amount)
            },
            status_code=201
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Add subscription item error: {e}")
        return create_error_response('Failed to add item to subscription', 500)


@subscriptions_bp.route('/<int:subscription_id>/items/<int:item_id>', methods=['PUT'])
@jwt_required()
@validate_json_with_model(UpdateSubscriptionItemRequest)
def update_subscription_item(subscription_id, item_id):
    """Update subscription item quantity"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', 404)
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            return create_error_response('Cannot modify cancelled subscription', 400)
        
        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id
        ).first()
        
        if not item:
            return create_error_response('Subscription item not found', 404)
        
        new_quantity = validated_data.quantity
        
        old_quantity = item.quantity
        item.quantity = new_quantity
        
        # Update special instructions if provided
        if validated_data.special_instructions is not None:
            item.special_instructions = validated_data.special_instructions
        
        item.calculate_total()
        
        # Recalculate subscription billing amount
        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        
        # Log the change
        log = SubscriptionLog(
            subscription_id=subscription_id,
            action='item_updated',
            details=f"Updated {item.product_name} quantity from {old_quantity} to {new_quantity}",
            user_id=current_user_id
        )
        db.session.add(log)
        
        db.session.commit()
        
        # Use Pydantic schema for response
        item_response = serialize_database_model(item, SubscriptionItemSchema)
        
        return create_success_response(
            message='Subscription item updated successfully',
            data={
                'item': item_response,
                'new_billing_amount': float(subscription.billing_amount)
            }
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Update subscription item error: {e}")
        return create_error_response('Failed to update subscription item', 500)


@subscriptions_bp.route('/<int:subscription_id>/items/<int:item_id>', methods=['DELETE'])
@jwt_required()
def remove_subscription_item(subscription_id, item_id):
    """Remove item from subscription"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            return jsonify({'error': 'Cannot modify cancelled subscription'}), 400
        
        item = SubscriptionItem.query.filter_by(
            id=item_id,
            subscription_id=subscription_id
        ).first()
        
        if not item:
            return jsonify({'error': 'Subscription item not found'}), 404
        
        # Check if this is the last item
        remaining_items = SubscriptionItem.query.filter_by(
            subscription_id=subscription_id
        ).filter(SubscriptionItem.id != item_id).count()
        
        if remaining_items == 0:
            return jsonify({'error': 'Cannot remove the last item from subscription'}), 400
        
        product_name = item.product_name
        
        db.session.delete(item)
        
        # Recalculate subscription billing amount
        subscription.billing_amount = subscription.get_total_value()
        subscription.updated_at = datetime.now(UTC)
        
        # Log the change
        log = SubscriptionLog(
            subscription_id=subscription_id,
            action='item_removed',
            details=f"Removed {product_name}",
            user_id=current_user_id
        )
        db.session.add(log)
        
        db.session.commit()
        
        return jsonify({
            'message': 'Item removed from subscription successfully',
            'new_billing_amount': subscription.billing_amount
        })
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Remove subscription item error: {e}")
        return jsonify({'error': 'Failed to remove subscription item'}), 500


@subscriptions_bp.route('/<int:subscription_id>/billing-history', methods=['GET'])
@jwt_required()
def get_billing_history(subscription_id):
    """Get subscription billing history"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        # Get payment history
        from business_app.models.payment import Payment
        payments = Payment.query.filter_by(
            subscription_id=subscription_id
        ).order_by(Payment.created_at.desc()).all()
        
        # Get billing summary
        total_paid = sum(p.amount for p in payments if p.status.value == 'completed')
        failed_payments = len([p for p in payments if p.status.value == 'failed'])
        
        return jsonify({
            'billing_history': [
                {
                    'payment_id': p.payment_id,
                    'amount': p.amount,
                    'status': p.status.value,
                    'payment_method': p.payment_method.value,
                    'created_at': p.created_at.isoformat() if p.created_at else None,
                    'failure_reason': p.failure_reason
                }
                for p in payments
            ],
            'summary': {
                'total_paid': total_paid,
                'total_payments': len(payments),
                'failed_payments': failed_payments,
                'next_billing_date': subscription.next_billing_date.isoformat() if subscription.next_billing_date else None,
                'next_billing_amount': subscription.billing_amount
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get billing history error: {e}")
        return jsonify({'error': 'Failed to get billing history'}), 500


@subscriptions_bp.route('/<int:subscription_id>/logs', methods=['GET'])
@jwt_required()
def get_subscription_logs(subscription_id):
    """Get subscription activity logs"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        # Get query parameters
        page = int(request.args.get('page', 1))
        per_page = min(int(request.args.get('per_page', 20)), 50)
        
        # Get logs
        pagination = SubscriptionLog.query.filter_by(
            subscription_id=subscription_id
        ).order_by(SubscriptionLog.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'logs': [
                serialize_subscription_log(log) for log in pagination.items
            ],
            'pagination': {
                'page': page,
                'pages': pagination.pages,
                'per_page': per_page,
                'total': pagination.total,
                'has_next': pagination.has_next,
                'has_prev': pagination.has_prev
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get subscription logs error: {e}")
        return jsonify({'error': 'Failed to get subscription logs'}), 500


@subscriptions_bp.route('/templates', methods=['GET'])
def get_subscription_templates():
    """Get subscription templates/presets"""
    try:
        # Predefined subscription templates
        templates = [
            {
                'id': 'basic_weekly',
                'name': 'Basic Weekly',
                'description': 'Perfect for small families - weekly water delivery',
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
                'name': 'Family Monthly',
                'description': 'Great value for larger families - monthly bulk delivery',
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
                'name': 'Office Daily',
                'description': 'Fresh water for your office every day',
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
        
        return jsonify({'templates': templates})
        
    except Exception as e:
        current_app.logger.error(f"Get subscription templates error: {e}")
        return jsonify({'error': 'Failed to get subscription templates'}), 500


@subscriptions_bp.route('/preview', methods=['POST'])
@jwt_required()
@validate_json_with_model(SubscriptionPreviewRequest)
def preview_subscription():
    """Preview subscription cost and details before creation"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        user = User.query.get(current_user_id)
        if not user:
            return create_error_response('User not found', status_code=404)
        
        # Calculate subscription preview
        preview = get_subscription_service().calculate_subscription_preview(
            user_id=current_user_id,
            billing_cycle=validated_data.billing_cycle.value,
            delivery_frequency=validated_data.delivery_frequency.value,
            items=validated_data.items,
            discount_percentage=validated_data.discount_percentage
        )
        
        # Serialize the preview response using the response schema
        preview_response = serialize_response(preview, SubscriptionPreviewResponse)
        
        return create_success_response(
            message='Subscription preview calculated successfully',
            data={'preview': preview_response}
        )
        
    except ValueError as e:
        return create_error_response(str(e), status_code=400)
    except Exception as e:
        current_app.logger.error(f"Preview subscription error: {e}")
        return create_error_response('Failed to preview subscription', status_code=500)


@subscriptions_bp.route('/statistics', methods=['GET'])
@jwt_required()
def get_subscription_statistics():
    """Get user's subscription statistics"""
    try:
        current_user_id = get_jwt_identity()
        
        # Get all user subscriptions
        subscriptions = Subscription.query.filter_by(user_id=current_user_id).all()
        
        # Calculate statistics
        total_subscriptions = len(subscriptions)
        active_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.ACTIVE])
        paused_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.PAUSED])
        cancelled_subscriptions = len([s for s in subscriptions if s.status == SubscriptionStatus.CANCELLED])
        
        # Calculate total spent on subscriptions
        total_spent = sum(s.total_amount_billed for s in subscriptions)
        
        # Calculate savings from subscriptions
        total_savings = sum(
            s.total_amount_billed * (s.discount_percentage / 100) 
            for s in subscriptions if s.discount_percentage > 0
        )
        
        # Get upcoming deliveries count
        from datetime import date
        upcoming_deliveries = 0
        for subscription in subscriptions:
            if subscription.status == SubscriptionStatus.ACTIVE:
                next_delivery = subscription.calculate_next_delivery_date()
                if next_delivery >= date.today():
                    upcoming_deliveries += 1
        
        # Monthly spending trend
        monthly_spending = {}
        for i in range(12):
            month_start = (datetime.now(UTC).replace(day=1) - timedelta(days=32*i)).replace(day=1)
            month_key = month_start.strftime('%Y-%m')
            
            # Calculate subscription billings for this month
            month_total = 0
            for subscription in subscriptions:
                # This is simplified - in reality you'd query actual payments
                if (subscription.created_at.date() <= month_start.date() and 
                    (not subscription.end_date or subscription.end_date.date() >= month_start.date())):
                    
                    if subscription.billing_cycle == 'monthly':
                        month_total += subscription.billing_amount
                    elif subscription.billing_cycle == 'weekly':
                        month_total += subscription.billing_amount * 4
                    elif subscription.billing_cycle == 'daily':
                        month_total += subscription.billing_amount * 30
            
            monthly_spending[month_key] = month_total
        
        return jsonify({
            'statistics': {
                'total_subscriptions': total_subscriptions,
                'active_subscriptions': active_subscriptions,
                'paused_subscriptions': paused_subscriptions,
                'cancelled_subscriptions': cancelled_subscriptions,
                'total_spent': total_spent,
                'total_savings': total_savings,
                'upcoming_deliveries': upcoming_deliveries,
                'monthly_spending_trend': monthly_spending
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Get subscription statistics error: {e}")
        return jsonify({'error': 'Failed to get subscription statistics'}), 500


@subscriptions_bp.route('/<int:subscription_id>/skip-next-delivery', methods=['POST'])
@jwt_required()
@validate_json_with_model(SkipDeliveryRequest)
def skip_next_delivery(subscription_id):
    """Skip the next delivery for a subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', status_code=404)
        
        if subscription.status != SubscriptionStatus.ACTIVE:
            return create_error_response('Only active subscriptions can skip deliveries', status_code=400)
        
        reason = validated_data.reason or 'Customer request'
        
        # Calculate next delivery date after skip
        current_next_delivery = subscription.calculate_next_delivery_date()
        
        if subscription.delivery_frequency == 'daily':
            new_next_delivery = current_next_delivery + timedelta(days=1)
        elif subscription.delivery_frequency == 'weekly':
            new_next_delivery = current_next_delivery + timedelta(weeks=1)
        elif subscription.delivery_frequency == 'monthly':
            # Add one month
            if current_next_delivery.month == 12:
                new_next_delivery = current_next_delivery.replace(
                    year=current_next_delivery.year + 1, month=1
                )
            else:
                new_next_delivery = current_next_delivery.replace(
                    month=current_next_delivery.month + 1
                )
        else:
            new_next_delivery = current_next_delivery + timedelta(days=7)  # Default to weekly
        
        # Log the skip
        log = SubscriptionLog(
            subscription_id=subscription_id,
            action='delivery_skipped',
            details=f"Skipped delivery scheduled for {current_next_delivery.strftime('%Y-%m-%d')}. Reason: {reason}",
            user_id=current_user_id,
            extra_data={
                'original_delivery_date': current_next_delivery.isoformat(),
                'new_delivery_date': new_next_delivery.isoformat(),
                'reason': reason
            }
        )
        db.session.add(log)
        db.session.commit()
        
        # Send notification
        get_notification_service().send_notification(
            current_user_id,
            'delivery_skipped',
            template_data={
                'subscription_name': subscription.name,
                'skipped_date': current_next_delivery.strftime('%Y-%m-%d'),
                'next_delivery_date': new_next_delivery.strftime('%Y-%m-%d'),
                'reason': reason
            }
        )
        
        return create_success_response(
            message='Next delivery skipped successfully',
            data={
                'original_delivery_date': current_next_delivery.isoformat(),
                'new_next_delivery_date': new_next_delivery.isoformat()
            }
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Skip next delivery error: {e}")
        return create_error_response('Failed to skip delivery', status_code=500)


@subscriptions_bp.route('/<int:subscription_id>/change-payment-method', methods=['POST'])
@jwt_required()
@validate_json_with_model(ChangePaymentMethodRequest)
def change_payment_method(subscription_id):
    """Change payment method for subscription"""
    try:
        current_user_id = get_jwt_identity()
        validated_data = request.validated_json
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return create_error_response('Subscription not found', status_code=404)
        
        if subscription.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAUSED]:
            return create_error_response('Cannot change payment method for cancelled subscription', status_code=400)
        
        try:
            new_payment_method = PaymentMethod(validated_data.payment_method)
        except ValueError:
            return create_error_response('Invalid payment method', status_code=400)
        
        old_payment_method = subscription.payment_method
        subscription.payment_method = new_payment_method
        subscription.updated_at = datetime.now(UTC)
        
        # Log the change
        log = SubscriptionLog(
            subscription_id=subscription_id,
            action='payment_method_changed',
            details=f"Payment method changed from {old_payment_method.value} to {new_payment_method.value}",
            user_id=current_user_id
        )
        db.session.add(log)
        db.session.commit()
        
        # Send notification
        get_notification_service().send_notification(
            current_user_id,
            'payment_method_changed',
            template_data={
                'subscription_name': subscription.name,
                'old_method': old_payment_method.value,
                'new_method': new_payment_method.value
            }
        )
        
        subscription_response = serialize_database_model(subscription, SubscriptionSchema)
        
        return create_success_response(
            message='Payment method updated successfully',
            data={'subscription': subscription_response}
        )
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Change payment method error: {e}")
        return create_error_response('Failed to change payment method', status_code=500)


@subscriptions_bp.route('/<int:subscription_id>/retry-billing', methods=['POST'])
@jwt_required()
def retry_billing(subscription_id):
    """Retry failed billing for subscription"""
    try:
        current_user_id = get_jwt_identity()
        
        subscription = Subscription.query.filter_by(
            id=subscription_id,
            user_id=current_user_id
        ).first()
        
        if not subscription:
            return jsonify({'error': 'Subscription not found'}), 404
        
        if subscription.status != SubscriptionStatus.ACTIVE:
            return jsonify({'error': 'Only active subscriptions can retry billing'}), 400
        
        # Check if there are failed billing attempts
        if subscription.failed_billing_attempts == 0:
            return jsonify({'error': 'No failed billing attempts to retry'}), 400
        
        # Process billing retry asynchronously
        process_subscription_billing.delay(subscription_id, retry=True)
        
        return jsonify({
            'message': 'Billing retry initiated. You will be notified of the result.'
        })
        
    except Exception as e:
        current_app.logger.error(f"Retry billing error: {e}")
        return jsonify({'error': 'Failed to retry billing'}), 500