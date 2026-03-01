"""Admin bulk action service."""

from datetime import datetime, UTC
from typing import Any, Dict, List

from flask import current_app

from business_app import db
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order
from business_app.models.product import Product
from business_app.models.review import Review
from business_app.models.subscription import Subscription
from business_app.models.user import User
from business_app.utils.constants import DeliveryStatus, OrderStatus, SubscriptionStatus, UserRole, UserStatus


class AdminBulkActionService:
    """Service for admin bulk actions."""

    VALID_ACTIONS = {
        'user': ['activate', 'deactivate', 'suspend', 'delete', 'send_email', 'assign_role'],
        'order': ['cancel', 'confirm', 'process', 'mark_delivered'],
        'product': ['activate', 'deactivate', 'delete', 'update_stock', 'update_price'],
        'review': ['approve', 'reject', 'delete', 'feature'],
        'subscription': ['pause', 'resume', 'cancel'],
        'delivery': ['assign_driver', 'mark_in_transit', 'mark_delivered']
    }

    @staticmethod
    def get_valid_actions() -> Dict[str, List[str]]:
        return AdminBulkActionService.VALID_ACTIONS

    @staticmethod
    def is_valid_action(target_type: str, action: str) -> bool:
        valid = AdminBulkActionService.VALID_ACTIONS
        return target_type in valid and action in valid[target_type]

    @staticmethod
    def perform(action, target_type, target_ids, parameters, reason, admin_id):
        """Dispatch and perform bulk action for target type."""
        parameters = parameters or {}

        if target_type == 'user':
            return AdminBulkActionService._bulk_action_users(action, target_ids, parameters, reason, admin_id)
        if target_type == 'order':
            return AdminBulkActionService._bulk_action_orders(action, target_ids, parameters, reason, admin_id)
        if target_type == 'product':
            return AdminBulkActionService._bulk_action_products(action, target_ids, parameters, reason, admin_id)
        if target_type == 'review':
            return AdminBulkActionService._bulk_action_reviews(action, target_ids, parameters, reason, admin_id)
        if target_type == 'subscription':
            return AdminBulkActionService._bulk_action_subscriptions(action, target_ids, parameters, reason, admin_id)
        if target_type == 'delivery':
            return AdminBulkActionService._bulk_action_deliveries(action, target_ids, parameters, reason, admin_id)

        return {
            'success_count': 0,
            'failed_count': len(target_ids),
            'errors': [{'target_type': target_type, 'error': 'Invalid target type'}],
            'total_errors': 1,
        }

    @staticmethod
    def _bulk_action_users(action, user_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for user_id in user_ids:
            try:
                user = User.query.get(user_id)
                if not user:
                    failed_count += 1
                    errors.append({'user_id': user_id, 'error': 'User not found'})
                    continue

                if action == 'activate':
                    user.status = UserStatus.ACTIVE
                    user.is_active = True
                elif action == 'deactivate':
                    user.status = UserStatus.INACTIVE
                    user.is_active = False
                elif action == 'suspend':
                    user.status = UserStatus.SUSPENDED
                    user.is_active = False
                elif action == 'delete':
                    user.status = UserStatus.DELETED
                    user.is_active = False
                elif action == 'assign_role':
                    new_role = parameters.get('role')
                    valid_roles = [
                        UserRole.CUSTOMER.value,
                        UserRole.ADMIN.value,
                        UserRole.MANAGER.value,
                        UserRole.OPERATOR.value,
                    ]
                    if new_role in valid_roles:
                        old_role = user.role
                        user.role = UserRole(new_role)
                        try:
                            from business_app.services.token_service import TokenService
                            token_service = TokenService()
                            token_service.revoke_user_tokens(user_id)
                            current_app.logger.info(
                                "Invalidated tokens for user %s after role change: %s -> %s",
                                user_id,
                                old_role,
                                new_role,
                            )
                        except Exception as token_err:
                            current_app.logger.warning(
                                "Failed to invalidate tokens for user %s: %s",
                                user_id,
                                token_err,
                            )
                    else:
                        failed_count += 1
                        errors.append({'user_id': user_id, 'error': 'Invalid role'})
                        continue
                elif action == 'send_email':
                    pass

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({'user_id': user_id, 'error': str(e)})

        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'errors': errors[:10],
            'total_errors': len(errors)
        }

    @staticmethod
    def _bulk_action_orders(action, order_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for order_id in order_ids:
            try:
                order = Order.query.get(order_id)
                if not order:
                    failed_count += 1
                    errors.append({'order_id': order_id, 'error': 'Order not found'})
                    continue

                if action == 'cancel':
                    if order.status in [OrderStatus.PENDING, OrderStatus.CONFIRMED]:
                        from business_app.services.order_service import OrderService
                        OrderService().cancel_order(
                            order.id,
                            reason=reason,
                            actor_user_id=admin_id,
                        )
                    else:
                        failed_count += 1
                        errors.append({'order_id': order_id, 'error': f'Cannot cancel order with status {order.status}'})
                        continue

                elif action == 'confirm':
                    if order.status == OrderStatus.PENDING:
                        order.status = OrderStatus.CONFIRMED
                        order.confirmed_at = datetime.now(UTC)
                    else:
                        failed_count += 1
                        errors.append({'order_id': order_id, 'error': f'Cannot confirm order with status {order.status}'})
                        continue

                elif action == 'process':
                    if order.status == OrderStatus.CONFIRMED:
                        order.status = OrderStatus.PREPARING
                    else:
                        failed_count += 1
                        errors.append({'order_id': order_id, 'error': f'Cannot process order with status {order.status}'})
                        continue

                elif action == 'mark_delivered':
                    if order.status in [OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY]:
                        order.status = OrderStatus.DELIVERED
                        order.delivered_at = datetime.now(UTC)
                    else:
                        failed_count += 1
                        errors.append({'order_id': order_id, 'error': f'Cannot mark as delivered with status {order.status}'})
                        continue

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({'order_id': order_id, 'error': str(e)})

        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'errors': errors[:10],
            'total_errors': len(errors)
        }

    @staticmethod
    def _bulk_action_products(action, product_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for product_id in product_ids:
            try:
                product = Product.query.get(product_id)
                if not product:
                    failed_count += 1
                    errors.append({'product_id': product_id, 'error': 'Product not found'})
                    continue

                if action == 'activate':
                    product.is_active = True
                elif action == 'deactivate':
                    product.is_active = False
                elif action == 'delete':
                    product.is_active = False
                    product.deleted_at = datetime.now(UTC)
                elif action == 'update_stock':
                    stock_adjustment = parameters.get('stock_adjustment', 0)
                    new_stock = parameters.get('new_stock')

                    if new_stock is not None:
                        product.stock_quantity = new_stock
                    else:
                        product.stock_quantity = (product.stock_quantity or 0) + stock_adjustment

                elif action == 'update_price':
                    new_price = parameters.get('new_price')
                    price_adjustment = parameters.get('price_adjustment', 0)

                    if new_price is not None:
                        product.price = new_price
                    elif price_adjustment != 0:
                        product.price = product.price + price_adjustment

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({'product_id': product_id, 'error': str(e)})

        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'errors': errors[:10],
            'total_errors': len(errors)
        }

    @staticmethod
    def _bulk_action_reviews(action, review_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for review_id in review_ids:
            try:
                review = Review.query.get(review_id)
                if not review:
                    failed_count += 1
                    errors.append({'review_id': review_id, 'error': 'Review not found'})
                    continue

                if action == 'approve':
                    review.is_approved = True
                    review.moderator_notes = reason
                elif action == 'reject':
                    review.is_approved = False
                    review.moderator_notes = reason
                elif action == 'delete':
                    db.session.delete(review)
                elif action == 'feature':
                    review.is_featured = True
                    review.is_approved = True

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({'review_id': review_id, 'error': str(e)})

        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'errors': errors[:10],
            'total_errors': len(errors)
        }

    @staticmethod
    def _bulk_action_subscriptions(action, subscription_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for subscription_id in subscription_ids:
            try:
                subscription = Subscription.query.get(subscription_id)
                if not subscription:
                    failed_count += 1
                    errors.append({'subscription_id': subscription_id, 'error': 'Subscription not found'})
                    continue

                if action == 'pause':
                    if subscription.status == SubscriptionStatus.ACTIVE:
                        subscription.status = SubscriptionStatus.PAUSED
                        subscription.paused_at = datetime.now(UTC)
                        subscription.pause_reason = reason
                    else:
                        failed_count += 1
                        errors.append({'subscription_id': subscription_id, 'error': 'Can only pause active subscriptions'})
                        continue

                elif action == 'resume':
                    if subscription.status == SubscriptionStatus.PAUSED:
                        subscription.status = SubscriptionStatus.ACTIVE
                        subscription.paused_at = None
                        subscription.pause_reason = None
                    else:
                        failed_count += 1
                        errors.append({'subscription_id': subscription_id, 'error': 'Can only resume paused subscriptions'})
                        continue

                elif action == 'cancel':
                    subscription.status = SubscriptionStatus.CANCELLED
                    subscription.end_date = datetime.now(UTC)

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({'subscription_id': subscription_id, 'error': str(e)})

        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'errors': errors[:10],
            'total_errors': len(errors)
        }

    @staticmethod
    def _bulk_action_deliveries(action, delivery_ids, parameters, reason, admin_id):
        success_count = 0
        failed_count = 0
        errors = []

        for delivery_id in delivery_ids:
            try:
                delivery = Delivery.query.get(delivery_id)
                if not delivery:
                    failed_count += 1
                    errors.append({'delivery_id': delivery_id, 'error': 'Delivery not found'})
                    continue

                if action == 'assign_driver':
                    driver_id = parameters.get('driver_id')
                    if not driver_id:
                        failed_count += 1
                        errors.append({'delivery_id': delivery_id, 'error': 'driver_id required'})
                        continue

                    driver = DeliveryPerson.query.get(driver_id)
                    if not driver:
                        failed_count += 1
                        errors.append({'delivery_id': delivery_id, 'error': 'Driver not found'})
                        continue

                    delivery.delivery_person_id = driver_id
                    delivery.status = DeliveryStatus.ASSIGNED

                elif action == 'mark_in_transit':
                    delivery.status = DeliveryStatus.IN_TRANSIT
                    delivery.picked_up_at = datetime.now(UTC)

                elif action == 'mark_delivered':
                    delivery.status = DeliveryStatus.DELIVERED
                    delivery.delivered_at = datetime.now(UTC)

                db.session.commit()
                success_count += 1

            except Exception as e:
                db.session.rollback()
                failed_count += 1
                errors.append({'delivery_id': delivery_id, 'error': str(e)})

        return {
            'success_count': success_count,
            'failed_count': failed_count,
            'errors': errors[:10],
            'total_errors': len(errors)
        }
