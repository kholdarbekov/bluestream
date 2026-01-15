"""
Order service for the Water Business Platform
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from flask import current_app
from sqlalchemy import and_, or_

from business_app.utils.service_logging import (
    log_service_call, log_business_event, log_database_query
)

logger = logging.getLogger(__name__)

from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.user import User
from business_app.models.delivery import Delivery
from business_app.utils.exceptions import ValidationError, NotFoundError, ConflictError
from business_app.utils.constants import OrderStatus, PaymentStatus, DeliveryStatus
from business_app.models.order import OrderStatusHistory
from business_app.utils.helpers import calculate_delivery_fee, calculate_loyalty_points
# Note: inventory_service imported lazily to avoid circular imports
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from business_app import db


class OrderService:
    """Service for managing orders"""
    
    def __init__(self):
        self.min_order_amount = current_app.config.get('MIN_ORDER_AMOUNT', 10000)
        self.max_order_items = current_app.config.get('MAX_ORDER_ITEMS', 50)
    
    def _get_inventory_service(self):
        """Get inventory service with lazy loading to avoid circular imports"""
        from business_app.services.inventory_service import get_inventory_service
        return get_inventory_service()
    
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
        order_items, subtotal = self._process_order_items(items_data)
        
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
            }
            payment_method = payment_method_map.get(payment_method_str)

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
            delivery_notes=order_data.get('delivery_notes'),
            order_source=order_source
        )
        
        db.session.add(order)
        db.session.flush()  # Get order ID
        
        # Add order items
        for item_data in order_items:
            order_item = OrderItem(
                order_id=order.id,
                product_id=item_data['product_id'],
                quantity=item_data['quantity'],
                unit_price=item_data['unit_price'],  # Use current price at order time
                total_price=item_data['total_price']
            )
            db.session.add(order_item)
        
        db.session.commit()
        logger.info(f"CREATE ORDER: Order has been inserted successfully")
        
        # Reserve inventory for this order
        try:
            reservation_result = self._get_inventory_service().reserve_inventory(
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
        query = Order.query.filter_by(id=order_id)
        
        if user_id:
            query = query.filter_by(user_id=user_id)
        
        order = query.first()
        if not order:
            raise NotFoundError("Order not found")
        
        return order
    
    # implement order timeline retrieval
    def get_order_timeline(self, order_id):
        return []


    def get_user_orders(self, user_id: int, status: OrderStatus = None, 
                       page: int = 1, per_page: int = 20) -> Dict[str, Any]:
        """Get user's orders with pagination"""
        query = Order.query.filter_by(user_id=user_id)
        
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
    
    def update_order_status(self, order_id: int, new_status: OrderStatus, 
                           updated_by: int = None, notes: str = None) -> Order:
        """Update order status"""
        order = Order.query.get(order_id)
        if not order:
            raise NotFoundError("Order not found")
        
        # Validate status transition
        if not self._is_valid_status_transition(order.status, new_status):
            raise ValidationError(f"Cannot change status from {order.status.value} to {new_status.value}")
        
        # Update order
        old_status = order.status
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
    def cancel_order(self, order_id: int, user_id: int = None, reason: str = None) -> Order:
        """Cancel an order"""
        from business_app.utils.constants import PaymentMethod
        
        order = self.get_order(order_id, user_id)
        
        # Check if order can be cancelled
        if order.status in [OrderStatus.DELIVERED, OrderStatus.CANCELLED]:
            raise ConflictError("Order cannot be cancelled")
        
        if order.status == OrderStatus.OUT_FOR_DELIVERY:
            raise ConflictError("Order is out for delivery and cannot be cancelled")
        
        # Determine if stock was already deducted from the database
        # For non-cash orders: stock is deducted on CONFIRMED
        # For cash orders: stock is deducted on DELIVERED (which can't be cancelled anyway)
        is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
        stock_was_deducted = not is_cash_order and order.status in [
            OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.OUT_FOR_DELIVERY
        ]
        
        if stock_was_deducted:
            # Restore stock quantities for confirmed orders
            self._restore_stock_for_order(order, reason)
        else:
            # Just release Redis reservations for pending orders
            try:
                release_result = self._get_inventory_service().release_reservations(order_id)
                if release_result['success']:
                    logger.info(f"Released inventory reservations for cancelled order {order_id}")
                else:
                    logger.warning(f"Failed to release inventory reservations for order {order_id}: {release_result.get('reason')}")
            except Exception as e:
                logger.error(f"Error releasing inventory reservations for order {order_id}: {e}")
        
        # Cancel order
        order = self.update_order_status(order_id, OrderStatus.CANCELLED, user_id, reason)
        
        # Handle refund if payment was made
        if order.payment and order.payment.status == PaymentStatus.COMPLETED:
            from .payment_service import PaymentService
            payment_service = PaymentService()
            payment_service.process_refund(order.payment.id, order.total_amount, reason)
        
        # Cancel delivery if assigned
        if order.delivery:
            from .delivery_service import DeliveryService
            delivery_service = DeliveryService()
            delivery_service.cancel_delivery(order.delivery.id, reason)
        
        return order
    
    def _restore_stock_for_order(self, order: Order, reason: str = None):
        """Restore stock quantities for a cancelled order that had stock deducted"""
        from business_app.services.inventory_service import InventoryOperationType
        
        inventory_service = self._get_inventory_service()
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
    
    def _process_order_items(self, items_data: List[Dict[str, Any]], order_id: Optional[int] = None) -> Tuple[List[Dict[str, Any]], int]:
        """Process and validate order items with comprehensive inventory checks"""
        processed_items = []
        subtotal = 0
        
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
        availability_results = self._get_inventory_service().check_multiple_products_availability(
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
        for item in items_data:
            product: Product = Product.query.get(item['product_id'])
            if not product:
                raise NotFoundError(f"Product {item['product_id']} not found")
            
            if not product.is_active:
                raise ValidationError(f"Product {product.name} is not available")
            
            quantity = int(item['quantity'])
            
            # Get current price (may include user-specific pricing in the future)
            unit_price = product.calculate_price(quantity=quantity)
            total_price = unit_price * quantity
            
            processed_items.append({
                'product_id': product.id,
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
        """Calculate delivery fee"""
        # Use delivery service to calculate fee
        from .delivery_service import DeliveryService
        delivery_service = DeliveryService()
        
        return delivery_service.calculate_delivery_fee(
            delivery_address['latitude'],
            delivery_address['longitude'],
            subtotal
        )
    
    def _is_valid_status_transition(self, current_status: OrderStatus, new_status: OrderStatus) -> bool:
        """Check if status transition is valid"""
        valid_transitions = {
            OrderStatus.PENDING: [OrderStatus.CONFIRMED, OrderStatus.CANCELLED],
            OrderStatus.CONFIRMED: [OrderStatus.PREPARING, OrderStatus.CANCELLED],
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
                points = calculate_loyalty_points(order.total_amount)
                if points > 0:
                    from .loyalty_service import LoyaltyService
                    loyalty_service = LoyaltyService()
                    loyalty_service.award_points(order.user_id, points, f"Order #{order.order_number}")
        
        elif new_status == OrderStatus.DELIVERED:
            # Mark delivery as completed
            if order.delivery:
                from .delivery_service import DeliveryService
                delivery_service = DeliveryService()
                delivery_service.complete_delivery(order.delivery.id)
            
            # For cash orders, confirm inventory and award loyalty points on delivery
            is_cash_order = order.payment_method == PaymentMethod.CASH if order.payment_method else False
            if is_cash_order:
                self._confirm_inventory_for_order(order)
                
                # Award loyalty points for cash orders
                points = calculate_loyalty_points(order.total_amount)
                if points > 0:
                    from .loyalty_service import LoyaltyService
                    loyalty_service = LoyaltyService()
                    loyalty_service.award_points(order.user_id, points, f"Order #{order.order_number}")
    
    def _confirm_inventory_for_order(self, order: Order):
        """Confirm inventory reservations and reduce stock for an order"""
        try:
            confirmation_result = self._get_inventory_service().confirm_reservations(order.id)
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
                'method': order.payment.method.value if order.payment else None
            } if order.payment else None,
            'delivery': {
                'status': order.delivery.status.value if order.delivery else 'pending',
                'tracking_code': order.delivery.tracking_code if order.delivery else None,
                'estimated_delivery_time': order.delivery.estimated_delivery_time.isoformat() 
                    if order.delivery and order.delivery.estimated_delivery_time else None
            } if order.delivery else None
        }