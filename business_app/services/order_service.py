"""
Order service for the Water Business Platform
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Dict, Any, Optional, Tuple
from flask import current_app
from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import joinedload

from business_app.utils.service_logging import (
    log_service_call, log_business_event, log_database_query
)

logger = logging.getLogger(__name__)

from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.user import User, UserAddress
from business_app.models.delivery import Delivery
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError, ForbiddenError
from business_app.utils.constants import OrderStatus, PaymentStatus, DeliveryStatus, PaymentMethod, SubscriptionFrequency, UserRole
from business_app.models.order import OrderStatusHistory
from business_app.utils.helpers import calculate_delivery_fee
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from business_app import db


class OrderService:
    """Service for managing orders"""

    def __init__(self, inventory_service=None):
        self.min_order_amount = current_app.config.get('MIN_ORDER_AMOUNT', 10000)
        self.max_order_items = current_app.config.get('MAX_ORDER_ITEMS', 50)
        self._inventory_service = inventory_service

    @property
    def inventory_service(self):
        """Lazy-initialise inventory service if not injected via constructor."""
        if self._inventory_service is None:
            from business_app.services.inventory_service import get_inventory_service
            self._inventory_service = get_inventory_service()
        return self._inventory_service
    
    @log_service_call(operation_type='order', track_performance=True)
    @log_business_event(event_type='created', entity_type='order')
    def create_order(self, user_id: int, order_data: Dict[str, Any]) -> Order:
        """
        Create a new order
        
        Args:
            user_id: ID of the user placing the order
            order_data: Order information including items, delivery address, etc.
        
        Returns:
            Created Order object
        
        Raises:
            ValidationError: If order data is invalid
            NotFoundError: If user or products not found
        """
        # Validate order data
        self._validate_order_data(order_data)
        
        # Get user
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        # Require phone number for placing orders
        if not user.phone:
            raise ValidationError("Phone number is required to place an order. Please update your profile.")
        
        # Validate and calculate order items
        items_data = order_data['items']
        order_items, subtotal = self._process_order_items(items_data, user_id=user_id)
        
        # Calculate delivery fee
        delivery_address = order_data['delivery_address']
        delivery_fee = self._calculate_delivery_fee(delivery_address, subtotal)
        
        # Calculate total
        total_amount = subtotal + delivery_fee
        
        # Check minimum order amount
        if total_amount < self.min_order_amount:
            raise ValidationError(f"Minimum order amount is {self.min_order_amount}")
        
        # Map payment method string to enum if provided
        payment_method = None
        payment_method_str = order_data.get('payment_method')
        if payment_method_str:
            from business_app.utils.constants import PaymentMethod
            payment_method_map = {
                'cash': PaymentMethod.CASH,
                'payme': PaymentMethod.PAYME,
                'click': PaymentMethod.CLICK,
                'card': PaymentMethod.CARD,
                'loyalty_points': PaymentMethod.LOYALTY_POINTS,
                'business_account': PaymentMethod.BUSINESS_ACCOUNT,
            }
            payment_method = payment_method_map.get(payment_method_str)
            if payment_method == PaymentMethod.BUSINESS_ACCOUNT:
                from business_app.services.corporate_contract_service import CorporateContractService
                CorporateContractService().validate_business_account_order(
                    user=user,
                    order_items=order_items,
                )

        # Create order
        order_source = order_data.get('order_source', 'web')
        order = Order(
            user_id=user_id,
            status=OrderStatus.PENDING,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total_amount=total_amount,
            delivery_address_id=delivery_address['delivery_address_id'],
            payment_method=payment_method,
            delivery_date=order_data.get('delivery_date'),
            delivery_time_slot=order_data.get('delivery_time_slot'),
            delivery_notes=order_data.get('delivery_notes'),
            is_urgent=bool(order_data.get('is_urgent', False)),
            loyalty_points_used=int(order_data.get('loyalty_points_used') or 0),
            order_source=order_source
        )
        
        db.session.add(order)
        db.session.flush()  # Get order ID
        
        # Add order items
        for item_data in order_items:
            order_item = OrderItem(
                order_id=order.id,
                product_id=item_data['product_id'],
                contract_id=item_data.get('contract_id'),
                contract_product_price_id=item_data.get('contract_product_price_id'),
                quantity=item_data['quantity'],
                unit_price=item_data['unit_price'],  # Use current price at order time
                total_price=item_data['total_price']
            )
            db.session.add(order_item)
        
        db.session.commit()
        logger.info(f"CREATE ORDER: Order has been inserted successfully")
        
        # Reserve inventory for this order
        try:
            reservation_result = self.inventory_service.reserve_inventory(
                order_id=order.id,
                items=items_data,
                user_id=user_id
            )
            logger.info(f"CREATE ORDER: reservation_result: {reservation_result}")
            
            if not reservation_result['success']:
                # If reservation fails, cancel the order
                db.session.delete(order)
                db.session.commit()
                raise ValidationError(f"Inventory reservation failed: {reservation_result['reason']}")
            
            logger.info(f"CREATE ORDER: AUDIT LOGGER starting")
            # Log successful reservation
            audit_logger.log_event(
                event_type=AuditEventType.ORDER_CREATED,
                action="inventory_reserved_for_order",
                severity=AuditSeverity.MEDIUM,
                resource_type="order",
                resource_id=str(order.id),
                description=f"Inventory reserved for order {order.order_number}",
                additional_data={
                    'order_id': order.id,
                    'order_number': order.order_number,
                    'reservation_expires_at': reservation_result.get('expires_at'),
                    'items_count': len(items_data)
                }
            )
            logger.info(f"CREATE ORDER: FINISHED")

            # Reserve prepayment bottle units for corporate contracts.
            from business_app.services.corporate_contract_service import CorporateContractService
            CorporateContractService().reserve_for_order(order.id)
            db.session.commit()
            
        except ValidationError:
            db.session.delete(order)
            db.session.commit()
            raise
        except Exception as e:
            # If reservation fails, cancel the order
            logger.error(f"Failed to reserve inventory for order {order.id}: {e}")
            db.session.delete(order)
            db.session.commit()
            raise ValidationError(f"Failed to reserve inventory: {str(e)}")
        
        # Send order confirmation notification
        # self._send_order_notification(order, 'order_created')
        
        # Schedule automatic confirmation if enabled
        # self._schedule_auto_confirmation(order.id)
        
        return order
    
    def get_order(self, order_id: int, user_id: int = None) -> Order:
        """Get order by ID"""
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.payment),
            joinedload(Order.delivery_address),
            joinedload(Order.delivery)
        ).filter_by(id=order_id)
        
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        order = query.first()
        if not order:
            raise NotFoundError("Order not found")
        
        return order
    
    # implement order timeline retrieval
    def get_order_timeline(self, order_id: int) -> List[Dict[str, Any]]:
        """
        Get order status history as a timeline.
        
        Returns a list of timeline entries in chronological order,
        starting with order creation and including all status changes.
        """
        # First, get the order itself for the creation timestamp
        order = Order.query.get(order_id)
        if not order:
            return []
        
        timeline = []
        
        # Add order creation as the first entry
        timeline.append({
            'status': 'created',
            'timestamp': order.created_at.isoformat() if order.created_at else None,
            'notes': None,
            'reason': None,
            'is_current': order.status == OrderStatus.PENDING
        })
        
        # Get status history from database
        history = OrderStatusHistory.query.filter_by(order_id=order_id)\
            .order_by(OrderStatusHistory.changed_at.asc()).all()
        
        for i, entry in enumerate(history):
            is_last = (i == len(history) - 1)
            timeline.append({
                'status': entry.new_status.value,
                'timestamp': entry.changed_at.isoformat() if entry.changed_at else None,
                'notes': entry.notes,
                'reason': entry.reason,
                'is_current': is_last  # Mark the last entry as current
            })
        
        return timeline


    def get_user_orders(self, user_id: int, status: OrderStatus = None,
                       page: int = 1, per_page: int = 20) -> Dict[str, Any]:
        """Get user's orders with pagination"""
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.payment),
            joinedload(Order.delivery_address)
        ).filter_by(user_id=user_id)
        
        if status:
            query = query.filter_by(status=status)
        
        query = query.order_by(Order.created_at.desc())
        
        pagination = query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return {
            'orders': [self._serialize_order(order) for order in pagination.items],
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page,
            'per_page': per_page,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev
        }

    def get_user_orders_paginated(
        self,
        user_id: int,
        page: int,
        per_page: int,
        status: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get paginated user orders with optional status/date filters."""
        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.payment),
            joinedload(Order.delivery_address),
            joinedload(Order.delivery),
        ).filter_by(user_id=user_id)

        if status:
            try:
                query = query.filter(Order.status == OrderStatus(status))
            except ValueError as exc:
                raise ValidationError("Invalid status value") from exc

        if start_date:
            query = query.filter(Order.created_at >= start_date)
        if end_date:
            query = query.filter(Order.created_at <= end_date)

        pagination = query.order_by(Order.created_at.desc()).paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )
        return {
            'items': pagination.items,
            'total': pagination.total,
            'page': page,
            'per_page': per_page,
        }

    def get_order_details_for_user(self, order_id: int, user_id: int) -> Dict[str, Any]:
        """Return full order details for a user-owned order."""
        order = self.get_order(order_id, user_id=user_id)
        return {
            'order': order,
            'delivery': order.delivery,
            'timeline': self.get_order_timeline(order_id),
        }

    def get_user_and_address_for_order(
        self,
        user_id: int,
        delivery_address_id: Optional[int],
    ) -> Tuple[User, Optional[UserAddress]]:
        """Resolve and validate user and optional delivery address ownership."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        if delivery_address_id is None:
            return user, None

        address = UserAddress.query.filter_by(
            id=delivery_address_id,
            user_id=user_id,
        ).first()
        if not address:
            raise ValidationError("Invalid delivery address")
        return user, address

    def get_user_or_raise(self, user_id: int) -> User:
        """Return user by id or raise not-found error."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")
        return user

    def validate_user_emergency_order_access(
        self,
        user_id: int,
        daily_limit: int = 3,
    ) -> Dict[str, Any]:
        """Validate emergency-order permissions and per-day rate limit."""
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")

        role_value = user.role.value if hasattr(user.role, 'value') else user.role
        staff_roles = {UserRole.ADMIN.value, UserRole.MANAGER.value, UserRole.OPERATOR.value}
        if not user.is_premium and role_value not in staff_roles:
            raise ForbiddenError("Emergency orders require premium or staff access")

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = Order.query.filter(
            Order.user_id == user_id,
            Order.is_urgent.is_(True),
            Order.created_at >= today_start,
        ).count()
        if today_count >= daily_limit:
            raise ConflictError("Emergency order daily limit exceeded")

        return {'user': user, 'today_count': today_count}

    def get_user_order_statistics(self, user_id: int, period: str = 'year') -> Dict[str, Any]:
        """Get aggregated order statistics for a user."""
        now = datetime.now(timezone.utc)
        start_date: Optional[datetime]
        if period == 'month':
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'quarter':
            quarter_start_month = ((now.month - 1) // 3) * 3 + 1
            start_date = now.replace(
                month=quarter_start_month,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        elif period == 'year':
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == 'all':
            start_date = None
        else:
            raise ValidationError("Invalid period value")

        query = Order.query.filter_by(user_id=user_id)
        if start_date:
            query = query.filter(Order.created_at >= start_date)

        total_orders, total_spent = query.with_entities(
            func.count(Order.id),
            func.coalesce(func.sum(Order.total_amount), 0),
        ).first()
        total_spent_value = float(total_spent or 0)

        status_rows = query.with_entities(
            Order.status,
            func.count(Order.id),
        ).group_by(Order.status).all()
        status_counts = {}
        for status_value, count in status_rows:
            key = status_value.value if hasattr(status_value, 'value') else str(status_value)
            status_counts[key] = count
        for enum_status in OrderStatus:
            status_counts.setdefault(enum_status.value, 0)

        top_products_query = db.session.query(
            Product.name,
            func.sum(OrderItem.quantity).label('total_qty'),
        ).join(OrderItem, OrderItem.product_id == Product.id).join(
            Order, Order.id == OrderItem.order_id,
        ).filter(Order.user_id == user_id)
        if start_date:
            top_products_query = top_products_query.filter(Order.created_at >= start_date)
        top_products_rows = top_products_query.group_by(
            Product.id, Product.name,
        ).order_by(desc('total_qty')).limit(5).all()
        top_products = [{'name': name, 'quantity': int(qty)} for name, qty in top_products_rows]

        monthly_spending: Dict[str, float] = {}
        monthly_samples = db.session.query(
            Order.created_at,
            Order.total_amount,
        ).filter(Order.user_id == user_id).all()
        for created_at, total_amount in monthly_samples:
            if not created_at:
                continue
            month_key = created_at.strftime('%Y-%m')
            monthly_spending[month_key] = monthly_spending.get(month_key, 0.0) + float(total_amount or 0)

        # Keep only most recent 12 months to stabilize payload size.
        monthly_spending = dict(sorted(monthly_spending.items(), reverse=True)[:12])

        return {
            'period': period,
            'statistics': {
                'total_orders': int(total_orders or 0),
                'total_spent': total_spent_value,
                'average_order_value': round(total_spent_value / total_orders, 2) if total_orders else 0,
                'orders_by_status': status_counts,
                'top_products': top_products,
                'monthly_spending_trend': monthly_spending,
            },
        }

    def submit_order_feedback_for_user(
        self,
        order_id: int,
        user_id: int,
        rating: int,
        comment: Optional[str] = None,
    ) -> Order:
        """Persist feedback for a delivered user order."""
        order = self.get_order(order_id, user_id=user_id)
        if order.status != OrderStatus.DELIVERED:
            raise ConflictError("Feedback can be submitted only for delivered orders")
        if not order.delivery:
            raise ValidationError("Order has no delivery record")

        order.delivery.customer_rating = rating
        order.delivery.customer_feedback = comment
        db.session.commit()
        return order

    def repeat_order_for_user(self, order_id: int, user_id: int) -> Order:
        """Create a new order from an existing user-owned order."""
        original_order = self.get_order(order_id, user_id=user_id)
        if not original_order.order_items:
            raise ValidationError("Original order has no items")
        if not original_order.delivery_address:
            raise ValidationError("Original order has no delivery address")

        order_data = {
            'items': [
                {'product_id': item.product_id, 'quantity': item.quantity}
                for item in original_order.order_items
            ],
            'delivery_address': {
                'delivery_address_id': original_order.delivery_address.id,
                'street': original_order.delivery_address.street_address,
                'latitude': original_order.delivery_address.latitude,
                'longitude': original_order.delivery_address.longitude,
            },
            'delivery_notes': original_order.delivery_notes,
            'payment_method': (
                original_order.payment_method.value
                if hasattr(original_order.payment_method, 'value')
                else original_order.payment_method
            ),
            'order_source': 'web',
        }
        return self.create_order(user_id, order_data)

    def get_order_tracking_for_user(self, order_id: int, user_id: int) -> Dict[str, Any]:
        """Get tracking payload for a user-owned order."""
        order = self.get_order(order_id, user_id=user_id)
        timeline = self.get_order_timeline(order_id)

        time_remaining = None
        if order.delivery and order.delivery.estimated_delivery_time:
            estimated_time = order.delivery.estimated_delivery_time
            if estimated_time.tzinfo is None:
                estimated_time = estimated_time.replace(tzinfo=timezone.utc)
            remaining = estimated_time - datetime.now(timezone.utc)
            if remaining.total_seconds() > 0:
                total_minutes = int(remaining.total_seconds() // 60)
                time_remaining = {
                    'hours': total_minutes // 60,
                    'minutes': total_minutes % 60,
                    'total_minutes': total_minutes,
                }

        return {
            'order': order,
            'delivery': order.delivery,
            'timeline': timeline,
            'estimated_time_remaining': time_remaining,
        }

    def perform_bulk_action(self, action: str, order_ids: List[int], actor_user_id: int) -> List[Dict[str, Any]]:
        """Perform bulk actions for admin users on selected orders."""
        actor = User.query.get(actor_user_id)
        if not actor or not actor.is_admin:
            raise ForbiddenError("Admin access required")

        valid_actions = {'confirm', 'cancel', 'mark_priority', 'assign_delivery'}
        if action not in valid_actions:
            raise ValidationError("Invalid action")

        results: List[Dict[str, Any]] = []
        for order_id in order_ids:
            order = Order.query.get(order_id)
            if not order:
                results.append({'order_id': order_id, 'success': False, 'error': 'Order not found'})
                continue

            try:
                if action == 'confirm':
                    self.update_order_status(order.id, OrderStatus.CONFIRMED, updated_by=actor_user_id)
                elif action == 'cancel':
                    self.cancel_order(order.id, reason='Bulk cancellation', actor_user_id=actor_user_id)
                elif action == 'mark_priority':
                    order.is_urgent = True
                    order.updated_at = datetime.now(timezone.utc)
                    db.session.commit()
                elif action == 'assign_delivery':
                    if not order.delivery:
                        from business_app.services.delivery_service import DeliveryService
                        DeliveryService().create_delivery(order.id)

                results.append({'order_id': order_id, 'success': True})
            except Exception as exc:
                db.session.rollback()
                results.append({'order_id': order_id, 'success': False, 'error': str(exc)})

        return results

    def export_orders(
        self,
        format_type: str,
        filters: Dict[str, Any],
        start_date: Optional[str],
        end_date: Optional[str],
        user_id: int,
    ) -> Dict[str, Any]:
        """Build order export file and return metadata."""
        if format_type not in {'csv', 'excel'}:
            raise ValidationError("Invalid format")

        query = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.delivery_address),
        )
        if filters.get('user_id') is not None:
            query = query.filter(Order.user_id == filters['user_id'])
        if filters.get('status'):
            try:
                query = query.filter(Order.status == OrderStatus(filters['status']))
            except ValueError as exc:
                raise ValidationError("Invalid status value") from exc

        if start_date:
            try:
                query = query.filter(Order.created_at >= datetime.fromisoformat(start_date))
            except ValueError as exc:
                raise ValidationError("Invalid start_date format") from exc
        if end_date:
            try:
                query = query.filter(Order.created_at <= datetime.fromisoformat(end_date))
            except ValueError as exc:
                raise ValidationError("Invalid end_date format") from exc

        orders = query.order_by(Order.created_at.desc()).all()

        import csv
        import os
        import uuid

        extension = 'xlsx' if format_type == 'excel' else 'csv'
        filename = f"orders_export_{user_id}_{uuid.uuid4().hex[:8]}.{extension}"
        filepath = os.path.join('/tmp', filename)

        with open(filepath, 'w', newline='', encoding='utf-8') as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(['order_id', 'order_number', 'status', 'total_amount', 'created_at'])
            for order in orders:
                status_value = order.status.value if hasattr(order.status, 'value') else order.status
                writer.writerow([
                    order.id,
                    order.order_number,
                    status_value,
                    float(order.total_amount),
                    order.created_at.isoformat() if order.created_at else '',
                ])

        file_size = os.path.getsize(filepath)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return {
            'download_url': f'/tmp/{filename}',
            'file_size': file_size,
            'expires_at': expires_at,
        }

    def create_subscription_order(
        self,
        subscription_data: Dict[str, Any],
        items_data: List[Dict[str, Any]],
    ) -> Any:
        """Create recurring subscription via subscription service."""
        frequency_value = subscription_data.get('frequency')
        try:
            frequency = SubscriptionFrequency(frequency_value)
        except ValueError as exc:
            raise ValidationError("Invalid subscription frequency") from exc

        payment_method_raw = subscription_data.get('payment_method') or PaymentMethod.CASH.value
        try:
            payment_method = PaymentMethod(payment_method_raw)
        except ValueError as exc:
            raise ValidationError("Invalid payment method") from exc

        from business_app.utils.service_factory import get_subscription_service

        payload = {
            'user_id': subscription_data['user_id'],
            'name': 'Recurring water delivery',
            'description': subscription_data.get('delivery_notes') or '',
            'billing_cycle': frequency.value,
            'delivery_frequency': frequency.value,
            'delivery_address_id': subscription_data.get('delivery_address_id'),
            'payment_method': payment_method,
            'auto_payment': bool(subscription_data.get('auto_pay', True)),
            'auto_renew': True,
            'start_date': datetime.fromisoformat(subscription_data['start_date']) if subscription_data.get('start_date') else datetime.now(timezone.utc),
        }

        return get_subscription_service().create_subscription(payload, items_data)

    def create_scheduled_order(self, order_data: Dict[str, Any], items_data: List[Dict[str, Any]]) -> Order:
        """Create an order scheduled for future processing."""
        user_id = order_data['user_id']
        _, address = self.get_user_and_address_for_order(user_id, order_data.get('delivery_address_id'))
        if not address:
            raise ValidationError("Delivery address is required")

        scheduled_date = order_data.get('scheduled_date')
        if isinstance(scheduled_date, str):
            scheduled_date = datetime.fromisoformat(scheduled_date)
        if scheduled_date and scheduled_date.tzinfo is None:
            scheduled_date = scheduled_date.replace(tzinfo=timezone.utc)
        if scheduled_date and scheduled_date <= datetime.now(timezone.utc):
            raise ValidationError("Scheduled date must be in the future")

        create_payload = {
            'items': items_data,
            'delivery_address': {
                'delivery_address_id': address.id,
                'street': address.street_address,
                'latitude': address.latitude,
                'longitude': address.longitude,
            },
            'delivery_date': order_data.get('delivery_date') or (scheduled_date.date() if scheduled_date else None),
            'delivery_time_slot': order_data.get('delivery_time_slot'),
            'delivery_notes': order_data.get('delivery_notes'),
            'payment_method': order_data.get('payment_method'),
            'order_source': order_data.get('order_source', 'web'),
            'is_urgent': bool(order_data.get('is_urgent', False)),
        }
        return self.create_order(user_id, create_payload)
    
    def update_order_status(self, order_id: int, new_status: OrderStatus, 
                           updated_by: int = None, notes: str = None) -> Order:
        """Update order status"""
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")
        
        # Connect to DB status being potentially a string
        current_status = order.status
        if isinstance(current_status, str):
            try:
                # Try to convert string to Enum
                current_status = OrderStatus(current_status)
            except ValueError:
                # If invalid string, keep as is (validation will likely fail)
                pass

        # Validate status transition
        if not self._is_valid_status_transition(current_status, new_status):
            current_val = current_status.value if hasattr(current_status, 'value') else str(current_status)
            new_val = new_status.value if hasattr(new_status, 'value') else str(new_status)
            raise ValidationError(f"Cannot change status from {current_val} to {new_val}")
        
        # Update order
        old_status = current_status
        order.status = new_status
        order.updated_at = datetime.now(timezone.utc)
        
        if updated_by:
            order.updated_by = updated_by
        
        # Update status-specific fields
        self._update_status_fields(order, new_status)
        
        # Create status history
        self._create_status_history(order_id, old_status, new_status, updated_by, notes)
        
        db.session.commit()
        
        # Send notification
        self._send_order_notification(order, f'status_changed_{new_status.value}')
        
        # Handle status-specific actions
        self._handle_status_change_actions(order, new_status)
        
        return order
    
    @log_service_call(operation_type='order', track_performance=True)
    @log_business_event(event_type='cancelled', entity_type='order')
    def cancel_order(
        self,
        order_id: int,
        user_id: int = None,
        reason: str = None,
        *,
        actor_user_id: int = None,
        process_payment_refund: bool = True,
    ) -> Order:
        """Cancel an order"""
        from business_app.utils.constants import PaymentMethod
        
        order = self.get_order(order_id, user_id)
        
        # Ensure status is Enum for logic checks
        current_status = order.status
        if isinstance(current_status, str):
            try:
                current_status = OrderStatus(current_status)
            except ValueError:
                pass
        
        delivery_status = None
        if order.delivery and getattr(order.delivery, 'status', None) is not None:
            delivery_status = order.delivery.status
            if isinstance(delivery_status, str):
                try:
                    delivery_status = DeliveryStatus(delivery_status)
                except ValueError:
                    pass

        if delivery_status in [
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.ARRIVED,
        ]:
            raise ConflictError("Order cannot be cancelled while delivery is in transit")

        actor_id = actor_user_id if actor_user_id is not None else user_id

        # Check if order can be cancelled
        if current_status in [OrderStatus.DELIVERED, OrderStatus.CANCELLED]:
            raise ConflictError("Order cannot be cancelled")
        
        if current_status == OrderStatus.OUT_FOR_DELIVERY:
            raise ConflictError("Order is out for delivery and cannot be cancelled")
        
        # Determine if stock was already deducted from the database
        # For non-cash orders: stock is deducted on CONFIRMED
        # For cash orders: stock is deducted on DELIVERED (which can't be cancelled anyway)
        is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
        stock_was_deducted = not is_cash_order and current_status in [
            OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY
        ]
        
        if stock_was_deducted:
            # Restore stock quantities for confirmed orders
            self._restore_stock_for_order(order, reason)
        else:
            # Just release Redis reservations for pending orders
            try:
                release_result = self.inventory_service.release_reservations(order_id)
                if release_result['success']:
                    logger.info(f"Released inventory reservations for cancelled order {order_id}")
                else:
                    logger.warning(f"Failed to release inventory reservations for order {order_id}: {release_result.get('reason')}")
            except Exception as e:
                logger.error(f"Error releasing inventory reservations for order {order_id}: {e}")
        
        # Cancel order
        order = self.update_order_status(order_id, OrderStatus.CANCELLED, actor_id, reason)

        # Release reserved corporate prepayment units (if any).
        from business_app.services.corporate_contract_service import CorporateContractService
        CorporateContractService().release_for_order(
            order_id=order.id,
            reason=reason,
            actor_user_id=actor_id,
        )
        db.session.commit()
        
        # Handle refund if payment was made
        if process_payment_refund and order.payment and order.payment.status == PaymentStatus.COMPLETED:
            from .payment_service import PaymentService
            payment_service = PaymentService()
            payment_service.process_refund(order.payment.id, order.total_amount, reason)
        
        # Cancel delivery if it is still awaiting route execution
        if order.delivery and delivery_status in [
            DeliveryStatus.SCHEDULED,
            DeliveryStatus.PENDING,
            DeliveryStatus.ASSIGNED,
        ]:
            from .delivery_service import DeliveryService
            delivery_service = DeliveryService()
            delivery_service.cancel_delivery(order.delivery.id, reason)
        
        return order
    
    def _restore_stock_for_order(self, order: Order, reason: str = None):
        """Restore stock quantities for a cancelled order that had stock deducted"""
        from business_app.services.inventory_service import InventoryOperationType
        
        inventory_service = self.inventory_service
        cancellation_reason = reason or "Order cancelled"
        
        for item in order.order_items:
            try:
                result = inventory_service.adjust_inventory(
                    product_id=item.product_id,
                    quantity_change=item.quantity,  # Positive to restore stock
                    operation_type=InventoryOperationType.RETURN_RESTOCK,
                    reason=f"Stock restored for cancelled order {order.order_number}: {cancellation_reason}",
                    user_id=order.user_id
                )
                
                if result['success']:
                    logger.info(
                        f"Restored stock for product {item.product_id}: "
                        f"+{item.quantity} units (order {order.order_number})"
                    )
                else:
                    logger.error(
                        f"Failed to restore stock for product {item.product_id}: "
                        f"{result.get('reason')}"
                    )
            except Exception as e:
                logger.error(
                    f"Error restoring stock for product {item.product_id} "
                    f"(order {order.order_number}): {e}"
                )

    
    def get_order_summary(self, user_id: int = None, 
                         start_date: datetime = None, 
                         end_date: datetime = None) -> Dict[str, Any]:
        """Get order summary statistics"""
        query = Order.query
        
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        if start_date:
            query = query.filter(Order.created_at >= start_date)
        
        if end_date:
            query = query.filter(Order.created_at <= end_date)
        
        orders = query.all()
        
        summary = {
            'total_orders': len(orders),
            'total_amount': sum(order.total_amount for order in orders),
            'status_breakdown': {},
            'average_order_value': 0,
            'most_ordered_products': self._get_most_ordered_products(orders)
        }
        
        # Status breakdown
        for status in OrderStatus:
            count = len([o for o in orders if o.status == status])
            summary['status_breakdown'][status.value] = count
        
        # Average order value
        if orders:
            summary['average_order_value'] = summary['total_amount'] / len(orders)
        
        return summary
    
    def reorder(self, order_id: int, user_id: int) -> Order:
        """Create a new order based on a previous order"""
        original_order = self.get_order(order_id, user_id)
        
        # Prepare new order data
        order_data = {
            'items': [
                {
                    'product_id': item.product_id,
                    'quantity': item.quantity
                }
                for item in original_order.order_items
            ],
            'delivery_address': {
                'street': original_order.delivery_address.street_address if original_order.delivery_address else None,
                'city': original_order.delivery_address.city if original_order.delivery_address else None,
                'latitude': original_order.delivery_address.latitude if original_order.delivery_address else None,
                'longitude': original_order.delivery_address.longitude if original_order.delivery_address else None
            },
            'delivery_instructions': original_order.delivery_notes
        }
        
        return self.create_order(user_id, order_data)
    
    def apply_discount(self, order_id: int, discount_code: str = None, 
                      discount_amount: int = None) -> Order:
        """Apply discount to order"""
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")
        
        if order.status != OrderStatus.PENDING:
            raise ConflictError("Cannot apply discount to confirmed order")
        
        if discount_code:
            # Validate discount code
            from .loyalty_service import LoyaltyService
            loyalty_service = LoyaltyService()
            discount_amount = loyalty_service.validate_discount_code(discount_code, order.user_id)
        
        if discount_amount and discount_amount > 0:
            order.discount_amount = min(discount_amount, order.subtotal)
            order.total_amount = order.subtotal + order.delivery_fee - order.discount_amount
            order.discount_code = discount_code
            
            db.session.commit()
        
        return order
    
    # Private methods
    def _validate_order_data(self, order_data: Dict[str, Any]):
        """Validate order data"""
        required_fields = ['items', 'delivery_address']
        
        for field in required_fields:
            if field not in order_data:
                raise ValidationError(f"Missing required field: {field}")
        
        # Validate items
        items = order_data['items']
        if not items or len(items) == 0:
            raise ValidationError("Order must contain at least one item")
        
        if len(items) > self.max_order_items:
            raise ValidationError(f"Order cannot contain more than {self.max_order_items} items")
        
        # Validate delivery address
        address = order_data['delivery_address']
        required_address_fields = ['street', 'latitude', 'longitude']
        
        for field in required_address_fields:
            if field not in address:
                raise ValidationError(f"Missing required address field: {field}")
    
    def _process_order_items(
        self,
        items_data: List[Dict[str, Any]],
        user_id: int,
        order_id: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Decimal]:
        """Process and validate order items with comprehensive inventory checks"""
        processed_items = []
        subtotal = Decimal("0.00")
        
        # Validate basic item structure
        for item in items_data:
            if 'product_id' not in item or 'quantity' not in item:
                raise ValidationError("Each item must have product_id and quantity")
            
            quantity = int(item['quantity'])
            if quantity <= 0:
                raise ValidationError("Quantity must be positive")
            
            if quantity > 100:  # Reasonable limit to prevent abuse
                raise ValidationError(f"Maximum quantity per item is 100")
        
        # Perform comprehensive inventory availability check
        availability_results = self.inventory_service.check_multiple_products_availability(
            items_data, exclude_order_id=order_id
        )
        
        # Check for any unavailable items
        unavailable_items = []
        for result in availability_results:
            if not result.is_available:
                product = Product.query.get(result.product_id)
                product_name = product.name if product else f"Product {result.product_id}"
                unavailable_items.append(f"{product_name}: {result.reason}")
        
        if unavailable_items:
            raise ValidationError(f"Inventory check failed: {'; '.join(unavailable_items)}")
        
        # Process items and calculate pricing
        from business_app.services.corporate_contract_service import CorporateContractService
        corporate_service = CorporateContractService()
        for item in items_data:
            product: Product = Product.query.get(item['product_id'])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")
            
            if not product.is_active:
                raise ValidationError(f"Product {product.name} is not available")
            
            quantity = int(item['quantity'])
            
            fallback_price = Decimal(str(product.calculate_price(quantity=quantity)))
            resolution = corporate_service.resolve_contract_pricing_for_user_product(
                user_id=user_id,
                product_id=product.id,
                fallback_price=fallback_price,
            )
            unit_price = Decimal(str(resolution['unit_price']))
            total_price = Decimal(str(unit_price)) * Decimal(str(quantity))
            
            processed_items.append({
                'product_id': product.id,
                'contract_id': resolution['contract'].id if resolution['contract'] else None,
                'contract_product_price_id': (
                    resolution['contract_price_row'].id if resolution['contract_price_row'] else None
                ),
                'quantity': quantity,
                'unit_price': unit_price,
                'total_price': total_price,
            })
            
            subtotal += total_price
        
        # Log inventory check for audit
        audit_logger.log_event(
            event_type=AuditEventType.ORDER_CREATED,
            action="order_inventory_validated",
            severity=AuditSeverity.MEDIUM,
            resource_type="order_inventory",
            description=f"Inventory validated for {len(processed_items)} items",
            additional_data={
                'items_count': len(processed_items),
                'total_quantity': sum(item['quantity'] for item in processed_items),
                'subtotal': float(subtotal)
            }
        )
        
        return processed_items, subtotal
    
    def _calculate_delivery_fee(self, delivery_address: Dict[str, Any], subtotal: int) -> int:
        """Calculate delivery fee via DeliveryService (single source of truth)"""
        from business_app.services.delivery_service import DeliveryService
        delivery_service = DeliveryService()
        latitude = delivery_address.get('latitude', 0)
        longitude = delivery_address.get('longitude', 0)
        return delivery_service.calculate_delivery_fee(latitude, longitude, subtotal)
    
    def _is_valid_status_transition(self, current_status: OrderStatus, new_status: OrderStatus) -> bool:
        """Check if status transition is valid"""
        valid_transitions = {
            OrderStatus.PENDING: [OrderStatus.CONFIRMED, OrderStatus.CANCELLED],
            OrderStatus.CONFIRMED: [OrderStatus.PREPARING, OrderStatus.DELIVERED, OrderStatus.CANCELLED],
            OrderStatus.PREPARING: [OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED],
            OrderStatus.OUT_FOR_DELIVERY: [OrderStatus.DELIVERED, OrderStatus.RETURNED],
            OrderStatus.DELIVERED: [],
            OrderStatus.CANCELLED: [],
            OrderStatus.RETURNED: []
        }
        
        return new_status in valid_transitions.get(current_status, [])
    
    def _update_status_fields(self, order: Order, new_status: OrderStatus):
        """Update status-specific fields"""
        now = datetime.now(timezone.utc)
        
        if new_status == OrderStatus.CONFIRMED:
            order.confirmed_at = now
        elif new_status == OrderStatus.PREPARING:
            order.preparing_at = now
        elif new_status == OrderStatus.OUT_FOR_DELIVERY:
            order.out_for_delivery_at = now
        elif new_status == OrderStatus.DELIVERED:
            order.delivered_at = now
        elif new_status == OrderStatus.CANCELLED:
            order.cancelled_at = now
    
    def _create_status_history(self, order_id: int, old_status: OrderStatus, 
                              new_status: OrderStatus, updated_by: int = None, notes: str = None):
        """Create order status history record"""
        history = OrderStatusHistory(
            order_id=order_id,
            old_status=old_status,
            new_status=new_status,
            changed_by=updated_by,
            notes=notes,
            changed_at=datetime.now(timezone.utc)
        )
        
        db.session.add(history)
    
    def _send_order_notification(self, order: Order, notification_type: str):
        """Send order notification"""
        from ..tasks.notification_tasks import send_order_notification_task
        send_order_notification_task.delay(order.id, notification_type)
    
    def _schedule_auto_confirmation(self, order_id: int):
        """Schedule automatic order confirmation"""
        from ..tasks.order_tasks import auto_confirm_order_task
        # Confirm order automatically after 10 minutes if not manually confirmed
        auto_confirm_order_task.apply_async(args=[order_id], countdown=600)
    
    def _handle_status_change_actions(self, order: Order, new_status: OrderStatus):
        """Handle actions when order status changes"""
        from business_app.utils.constants import PaymentMethod
        
        if new_status == OrderStatus.CONFIRMED:
            # For non-cash orders, confirm inventory reservations - reduce actual stock
            # Cash on delivery orders will have stock deducted on DELIVERED status
            is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
            
            if not is_cash_order:
                self._confirm_inventory_for_order(order)
            else:
                logger.info(f"Skipping inventory confirmation for cash order {order.id} - will deduct on delivery")
            
            # Create delivery record
            from .delivery_service import DeliveryService
            delivery_service = DeliveryService()
            delivery_service.create_delivery(order.id)
            
            # Award loyalty points only for non-cash orders (cash orders get points on delivery)
            if not is_cash_order:
                self._process_loyalty_points_for_order(order)
        
        elif new_status == OrderStatus.DELIVERED:
            # Mark delivery as completed (sync_order_status=False to prevent circular callback)
            if order.delivery:
                from .delivery_service import DeliveryService
                delivery_service = DeliveryService()
                delivery_status = order.delivery.status.value if hasattr(order.delivery.status, 'value') else order.delivery.status
                if delivery_status != DeliveryStatus.DELIVERED.value:
                    delivery_service.complete_delivery(order.delivery.id, sync_order_status=False)
            
            # For cash orders, confirm inventory and award loyalty points on delivery
            is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
            if is_cash_order:
                self._confirm_inventory_for_order(order)
                # Process loyalty points for cash orders
                self._process_loyalty_points_for_order(order)

            # --- LOYALTY OVERHAUL TRIGGERS ---
            # Triggers that must happen only on successful delivery
            try:
                from .loyalty_service import LoyaltyService
                loyalty_service = LoyaltyService()
                
                # Check/Update Streak
                loyalty_service.update_streak(order.user_id)
                
                # Check for Surprise Reward
                loyalty_service.check_surprise_reward(order.user_id)
                
            except Exception as e:
                logger.error(f"Failed to process loyalty triggers for delivered order {order.id}: {e}")

            # Consume reserved corporate prepayment units on successful delivery.
            from business_app.services.corporate_contract_service import CorporateContractService
            CorporateContractService().consume_for_order(
                order_id=order.id,
                delivery_id=order.delivery.id if order.delivery else None,
            )
            db.session.commit()
    
    def _process_loyalty_points_for_order(self, order: Order):
        """
        Process loyalty points for an order:
        Award points based on the loyalty-eligible amount.
        """
        from .loyalty_service import LoyaltyService
        from business_app.services.corporate_contract_service import CorporateContractService
        from business_app.utils.constants import LoyaltyActionType
        
        try:
            loyalty_service = LoyaltyService()
            eligible_amount = CorporateContractService().get_loyalty_eligible_amount_for_order(order)
            if eligible_amount <= 0:
                order.loyalty_points_earned = 0
                logger.info(f"Skipping loyalty points for order {order.order_number}: no eligible amount")
                return

            # Contract-linked orders use the eligible line-item subtotal as the points basis.
            points_earned = loyalty_service.calculate_points_for_purchase(order.user_id, int(eligible_amount))
            if points_earned > 0:
                loyalty_service.award_points(
                    order.user_id, 
                    points_earned, 
                    f"Order #{order.order_number}",
                    LoyaltyActionType.PURCHASE,
                    order.id
                )
                # Update order with earned points for reference
                order.loyalty_points_earned = points_earned
                logger.info(f"Awarded {points_earned} points for order {order.order_number}")
            else:
                order.loyalty_points_earned = 0
                    
        except Exception as e:
            # Don't fail the order if loyalty processing fails
            logger.error(f"Failed to process loyalty points for order {order.order_number}: {e}")
    
    def _confirm_inventory_for_order(self, order: Order):
        """Confirm inventory reservations and reduce stock for an order"""
        try:
            confirmation_result = self.inventory_service.confirm_reservations(order.id)
            if confirmation_result['success']:
                logger.info(f"Confirmed inventory reservations for order {order.id}")
                
                # Log inventory confirmation
                audit_logger.log_event(
                    event_type=AuditEventType.ORDER_UPDATED,
                    action="inventory_confirmed_for_order",
                    severity=AuditSeverity.HIGH,
                    resource_type="order",
                    resource_id=str(order.id),
                    description=f"Inventory confirmed and stock reduced for order {order.order_number}",
                    additional_data={
                        'order_id': order.id,
                        'order_number': order.order_number,
                        'confirmed_items': confirmation_result.get('confirmed_items', [])
                    }
                )
            else:
                logger.error(f"Failed to confirm inventory for order {order.id}: {confirmation_result.get('reason')}")
                raise ValidationError(f"Inventory confirmation failed: {confirmation_result.get('reason')}")
                
        except Exception as e:
            logger.error(f"Error confirming inventory for order {order.id}: {e}")
            raise ValidationError(f"Failed to confirm inventory: {str(e)}")

    
    def _get_most_ordered_products(self, orders: List[Order]) -> List[Dict[str, Any]]:
        """Get most ordered products from order list"""
        product_counts = {}
        
        for order in orders:
            for item in order.order_items:
                if item.product_id not in product_counts:
                    product_counts[item.product_id] = {
                        'product_id': item.product_id,
                        'product_name': item.product.name if item.product else 'Unknown',
                        'total_quantity': 0,
                        'total_orders': 0
                    }
                
                product_counts[item.product_id]['total_quantity'] += item.quantity
                product_counts[item.product_id]['total_orders'] += 1
        
        # Sort by total quantity
        sorted_products = sorted(
            product_counts.values(),
            key=lambda x: x['total_quantity'],
            reverse=True
        )
        
        return sorted_products[:10]  # Top 10
    
    def _serialize_order(self, order: Order) -> Dict[str, Any]:
        """Serialize order to dictionary"""
        return {
            'id': order.id,
            'order_number': order.order_number,
            'status': order.status.value,
            'subtotal': order.subtotal,
            'delivery_fee': order.delivery_fee,
            'discount_amount': order.discount_amount or 0,
            'total_amount': order.total_amount,
            'created_at': order.created_at.isoformat(),
            'confirmed_at': order.confirmed_at.isoformat() if order.confirmed_at else None,
            'delivered_at': order.delivered_at.isoformat() if order.delivered_at else None,
            'delivery_address': {
                'street': order.delivery_address.street_address if order.delivery_address else None,
                'city': order.delivery_address.city if order.delivery_address else None,
                'latitude': order.delivery_address.latitude if order.delivery_address else None,
                'longitude': order.delivery_address.longitude if order.delivery_address else None
            },
            'items': [
                {
                    'id': item.id,
                    'product_id': item.product_id,
                    'product_name': item.product.name if item.product else 'Unknown',
                    'quantity': item.quantity,
                    'unit_price': item.unit_price,
                    'total_price': item.total_price
                }
                for item in order.order_items
            ],
            'payment': {
                'status': order.payment.status.value if order.payment else 'pending',
                'method': order.payment.payment_method.value if order.payment else None
            } if order.payment else None,
            'delivery': {
                'status': order.delivery.status.value if order.delivery else 'pending',
                'tracking_code': order.delivery.tracking_code if order.delivery else None,
                'estimated_delivery_time': order.delivery.estimated_delivery_time.isoformat() 
                    if order.delivery and order.delivery.estimated_delivery_time else None
            } if order.delivery else None
        }
