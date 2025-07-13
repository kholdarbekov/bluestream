import os
import asyncio
import logging
import aiohttp
import hmac
import hashlib
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Any
import json
import uuid
from decimal import Decimal
from enum import Enum

import asyncpg
import redis.asyncio as redis
from geopy.distance import geodesic
from geopy.geocoders import Nominatim
import stripe
from twilio.rest import Client as TwilioClient
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

logger = logging.getLogger(__name__)

# Data classes and enums
class DeliveryStatus(Enum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"

class DeliverySlot:
    def __init__(self, slot_id: str, start_time: datetime, end_time: datetime, available: bool = True):
        self.slot_id = slot_id
        self.start_time = start_time
        self.end_time = end_time
        self.available = available

class DeliveryRoute:
    def __init__(self, route_id: str, driver_id: str, orders: List[str], estimated_duration: int, total_distance: float):
        self.route_id = route_id
        self.driver_id = driver_id
        self.orders = orders
        self.estimated_duration = estimated_duration
        self.total_distance = total_distance

class NotificationService:
    """Handle all notification services"""
    
    def __init__(self, redis_client, db_pool):
        self.redis_client = redis_client
        self.db_pool = db_pool
        self.twilio_client = TwilioClient(
            username=os.getenv('TWILIO_ACCOUNT_SID'),
            password=os.getenv('TWILIO_AUTH_TOKEN')
        )
        self.sendgrid_client = SendGridAPIClient(api_key=os.getenv('SENDGRID_API_KEY'))
        self.geolocator = Nominatim(user_agent="aquapure_water_bot")
    
    async def send_sms(self, phone: str, message: str) -> bool:
        """Send SMS notification"""
        try:
            message = self.twilio_client.messages.create(
                body=message,
                from_=os.getenv('TWILIO_PHONE_NUMBER'),
                to=phone
            )
            logger.info(f"SMS sent successfully: {message.sid}")
            return True
        except Exception as e:
            logger.error(f"Failed to send SMS: {e}")
            return False
    
    async def send_email(self, email: str, subject: str, content: str) -> bool:
        """Send email notification"""
        try:
            message = Mail(
                from_email=os.getenv('FROM_EMAIL'),
                to_emails=email,
                subject=subject,
                html_content=content
            )
            response = self.sendgrid_client.send(message)
            logger.info(f"Email sent successfully: {response.status_code}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False
    
    async def send_order_notification(self, user_id: str, order_data: Dict, notification_type: str):
        """Send order-related notifications"""
        try:
            async with self.db_pool.acquire() as conn:
                user = await conn.fetchrow(
                    "SELECT * FROM users WHERE id = $1", user_id
                )
                preferences = await conn.fetchrow(
                    "SELECT * FROM user_preferences WHERE user_id = $1", user_id
                )
            
            if not user:
                return
            
            # Get notification templates
            templates = await self.get_notification_templates(notification_type, user['language_code'])
            
            # Send notifications based on user preferences
            if preferences and preferences['notification_sms'] and user['phone']:
                await self.send_sms(user['phone'], templates['sms'])
            
            if preferences and preferences['notification_email'] and user['email']:
                await self.send_email(user['email'], templates['email_subject'], templates['email_content'])
            
            # Store notification in database
            await self.store_notification(user_id, notification_type, templates['title'], templates['message'])
            
        except Exception as e:
            logger.error(f"Error sending order notification: {e}")
    
    async def get_notification_templates(self, notification_type: str, language: str) -> Dict:
        """Get notification templates"""
        templates = {
            'order_confirmed': {
                'en': {
                    'title': 'Order Confirmed',
                    'message': 'Your water order has been confirmed and is being prepared.',
                    'sms': 'AquaPure: Your order has been confirmed! We\'ll notify you when it\'s ready for delivery.',
                    'email_subject': 'Order Confirmed - AquaPure Water',
                    'email_content': '<h2>Order Confirmed</h2><p>Thank you for your order! We\'re preparing your water for delivery.</p>'
                },
                'uz': {
                    'title': 'Buyurtma tasdiqlandi',
                    'message': 'Sizning suv buyurtmangiz tasdiqlandi va tayyorlanmoqda.',
                    'sms': 'AquaPure: Buyurtmangiz tasdiqlandi! Yetkazib berish uchun tayyor bo\'lganda xabar beramiz.',
                    'email_subject': 'Buyurtma tasdiqlandi - AquaPure Water',
                    'email_content': '<h2>Buyurtma tasdiqlandi</h2><p>Buyurtmangiz uchun rahmat! Suvingizni yetkazib berish uchun tayyorlayapmiz.</p>'
                }
            },
            'out_for_delivery': {
                'en': {
                    'title': 'Out for Delivery',
                    'message': 'Your water order is out for delivery and will arrive soon.',
                    'sms': 'AquaPure: Your order is out for delivery! Expected arrival in 30-60 minutes.',
                    'email_subject': 'Out for Delivery - AquaPure Water',
                    'email_content': '<h2>Out for Delivery</h2><p>Your water order is on the way! Please be available for delivery.</p>'
                }
            }
        }
        
        return templates.get(notification_type, {}).get(language, templates.get(notification_type, {}).get('en', {}))
    
    async def store_notification(self, user_id: str, notification_type: str, title: str, message: str):
        """Store notification in database"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO notifications (user_id, type, title, message, created_at)
                       VALUES ($1, $2, $3, $4, CURRENT_TIMESTAMP)""",
                    user_id, notification_type, title, message
                )
        except Exception as e:
            logger.error(f"Error storing notification: {e}")

class PaymentService:
    """Handle payment processing"""
    
    def __init__(self, redis_client, db_pool):
        self.redis_client = redis_client
        self.db_pool = db_pool
        stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
    
    async def create_payment_intent(self, amount: Decimal, currency: str = 'uzs', metadata: Dict = None) -> Dict:
        """Create Stripe payment intent"""
        try:
            intent = stripe.PaymentIntent.create(
                amount=int(amount * 100),  # Convert to cents
                currency=currency,
                metadata=metadata or {}
            )
            return {
                'client_secret': intent.client_secret,
                'payment_intent_id': intent.id
            }
        except Exception as e:
            logger.error(f"Error creating payment intent: {e}")
            raise
    
    async def process_loyalty_payment(self, user_id: str, amount: Decimal) -> bool:
        """Process loyalty points payment"""
        try:
            async with self.db_pool.acquire() as conn:
                user = await conn.fetchrow(
                    "SELECT loyalty_points FROM users WHERE id = $1", user_id
                )
                
                if not user or user['loyalty_points'] < amount:
                    return False
                
                # Deduct loyalty points
                await conn.execute(
                    "UPDATE users SET loyalty_points = loyalty_points - $1 WHERE id = $2",
                    int(amount), user_id
                )
                
                return True
        except Exception as e:
            logger.error(f"Error processing loyalty payment: {e}")
            return False
    
    async def add_loyalty_points(self, user_id: str, points: int, reason: str = 'order_reward'):
        """Add loyalty points to user account"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET loyalty_points = loyalty_points + $1 WHERE id = $2",
                    points, user_id
                )
                
                # Log loyalty transaction
                await conn.execute(
                    """INSERT INTO loyalty_transactions (user_id, points, transaction_type, reason, created_at)
                       VALUES ($1, $2, 'credit', $3, CURRENT_TIMESTAMP)""",
                    user_id, points, reason
                )
        except Exception as e:
            logger.error(f"Error adding loyalty points: {e}")

class DeliveryService:
    """Handle delivery operations"""
    
    def __init__(self, redis_client, db_pool):
        self.redis_client = redis_client
        self.db_pool = db_pool
        self.geolocator = Nominatim(user_agent="aquapure_delivery")
        self.maps_api_key = os.getenv('GOOGLE_MAPS_API_KEY')
        self.time_slots = self._generate_time_slots()
    
    async def calculate_delivery_fee(self, user_location: Dict, warehouse_location: Dict) -> Decimal:
        """Calculate delivery fee based on distance"""
        try:
            distance = geodesic(
                (user_location['latitude'], user_location['longitude']),
                (warehouse_location['latitude'], warehouse_location['longitude'])
            ).kilometers
            
            # Base fee + distance-based fee
            base_fee = Decimal('5000')  # 5000 UZS base fee
            distance_fee = Decimal(str(distance * 500))  # 500 UZS per km
            
            return base_fee + distance_fee
        except Exception as e:
            logger.error(f"Error calculating delivery fee: {e}")
            return Decimal('10000')  # Default fee
    
    async def schedule_delivery(self, order_id: str, delivery_date: date, time_slot: str) -> bool:
        """Schedule delivery for order"""
        try:
            async with self.db_pool.acquire() as conn:
                # Find available delivery person
                delivery_person = await conn.fetchrow(
                    """SELECT u.id FROM users u
                       WHERE u.role = 'delivery'
                       AND u.id NOT IN (
                           SELECT delivery_person_id FROM deliveries
                           WHERE scheduled_date = $1 AND scheduled_time = $2
                       )
                       LIMIT 1""",
                    delivery_date, time_slot
                )
                
                if not delivery_person:
                    logger.warning(f"No available delivery person for {delivery_date} {time_slot}")
                    return False
                
                # Create delivery record
                await conn.execute(
                    """INSERT INTO deliveries (order_id, delivery_person_id, scheduled_date, 
                       scheduled_time, status, created_at)
                       VALUES ($1, $2, $3, $4, 'scheduled', CURRENT_TIMESTAMP)""",
                    order_id, delivery_person['id'], delivery_date, time_slot
                )
                
                # Update order status
                await conn.execute(
                    "UPDATE orders SET delivery_status = 'scheduled' WHERE id = $1",
                    order_id
                )
                
                logger.info(f"Delivery scheduled for order {order_id} on {delivery_date} at {time_slot}")
                return True
                
        except Exception as e:
            logger.error(f"Error scheduling delivery: {e}")
            return False
    
    def _generate_time_slots(self) -> List[DeliverySlot]:
        """Generate available delivery time slots"""
        slots = []
        base_date = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        
        for day in range(7):  # Next 7 days
            current_date = base_date + timedelta(days=day)
            for hour in range(9, 18):  # 9 AM to 6 PM
                start_time = current_date.replace(hour=hour)
                end_time = start_time + timedelta(hours=2)
                slot_id = f"{current_date.strftime('%Y%m%d')}_{hour:02d}"
                
                slots.append(DeliverySlot(
                    slot_id=slot_id,
                    start_time=start_time,
                    end_time=end_time,
                    available=True
                ))
        
        return slots
    
    async def get_available_slots(self, location: str) -> List[DeliverySlot]:
        """Get available delivery slots for a location"""
        # Filter slots based on location and existing bookings
        async with self.db_pool.acquire() as conn:
            booked_slots = await conn.fetch(
                "SELECT delivery_slot FROM orders WHERE delivery_status != 'delivered' AND delivery_status != 'cancelled'"
            )
            
            booked_slot_ids = {slot['delivery_slot'] for slot in booked_slots}
            
            available_slots = [
                slot for slot in self.time_slots 
                if slot.slot_id not in booked_slot_ids and slot.start_time > datetime.now()
            ]
            
            return available_slots[:20]  # Return next 20 available slots
    

    
    async def optimize_route(self, driver_id: str, order_ids: List[str]) -> DeliveryRoute:
        """Optimize delivery route for multiple orders"""
        try:
            async with self.db_pool.acquire() as conn:
                # Get order locations
                orders = await conn.fetch(
                    "SELECT order_id, delivery_address FROM orders WHERE order_id = ANY($1)",
                    order_ids
                )
                
                # Use Google Maps API for route optimization
                waypoints = [order['delivery_address'] for order in orders]
                optimized_order = await self._optimize_waypoints(waypoints)
                
                route = DeliveryRoute(
                    route_id=f"route_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    driver_id=driver_id,
                    orders=[order_ids[i] for i in optimized_order],
                    estimated_duration=len(order_ids) * 15,  # 15 minutes per delivery
                    total_distance=0.0  # Calculate from API response
                )
                
                return route
        except Exception as e:
            logger.error(f"Route optimization failed: {e}")
            return DeliveryRoute("", driver_id, order_ids, 0, 0.0)
    
    async def _optimize_waypoints(self, waypoints: List[str]) -> List[int]:
        """Optimize waypoint order using Google Maps API"""
        try:
            async with aiohttp.ClientSession() as session:
                origin = waypoints[0]
                destination = waypoints[-1]
                waypoint_str = "|".join(waypoints[1:-1])
                
                url = f"https://maps.googleapis.com/maps/api/directions/json"
                params = {
                    "origin": origin,
                    "destination": destination,
                    "waypoints": f"optimize:true|{waypoint_str}",
                    "key": self.maps_api_key
                }
                
                async with session.get(url, params=params) as response:
                    data = await response.json()
                    if data.get("status") == "OK":
                        waypoint_order = data["routes"][0]["waypoint_order"]
                        return [0] + [i + 1 for i in waypoint_order] + [len(waypoints) - 1]
                    
                return list(range(len(waypoints)))
        except Exception as e:
            logger.error(f"Waypoint optimization failed: {e}")
            return list(range(len(waypoints)))
    
    async def update_delivery_status(self, order_id: str, status: DeliveryStatus, notes: str = ""):
        """Update delivery status"""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET delivery_status = $1 WHERE order_id = $2",
                status.value, order_id
            )
            
            await conn.execute(
                """INSERT INTO delivery_events (order_id, event_type, event_time, description)
                   VALUES ($1, $2, $3, $4)""",
                order_id, status.value, datetime.now(), notes
            )
    
    async def get_delivery_tracking(self, order_id: str) -> Dict:
        """Get delivery tracking information"""
        async with self.db_pool.acquire() as conn:
            order = await conn.fetchrow(
                "SELECT * FROM orders WHERE order_id = $1",
                order_id
            )
            
            events = await conn.fetch(
                "SELECT * FROM delivery_events WHERE order_id = $1 ORDER BY event_time",
                order_id
            )
            
            return {
                "order_id": order_id,
                "status": order['delivery_status'],
                "address": order['delivery_address'],
                "slot": order['delivery_slot'],
                "events": [
                    {
                        "type": event['event_type'],
                        "time": event['event_time'],
                        "description": event['description']
                    }
                    for event in events
                ]
            }

class AnalyticsService:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
    
    async def get_customer_analytics(self, user_id: int) -> Dict:
        """Get customer behavior analytics"""
        async with self.db_pool.acquire() as conn:
            # Order history
            orders = await conn.fetch(
                "SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at DESC",
                user_id
            )
            
            # Calculate metrics
            total_orders = len(orders)
            total_spent = sum(order['total_amount'] for order in orders)
            avg_order_value = total_spent / total_orders if total_orders > 0 else 0
            
            # Favorite products
            product_counts = {}
            for order in orders:
                items = order.get('items', [])
                for item in items:
                    product_id = item.get('product_id')
                    if product_id:
                        product_counts[product_id] = product_counts.get(product_id, 0) + 1
            
            favorite_products = sorted(product_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            
            return {
                "total_orders": total_orders,
                "total_spent": total_spent,
                "avg_order_value": avg_order_value,
                "favorite_products": favorite_products,
                "last_order_date": orders[0]['created_at'] if orders else None
            }
    
    async def get_business_analytics(self) -> Dict:
        """Get business analytics overview"""
        async with self.db_pool.acquire() as conn:
            # Daily revenue
            daily_revenue = await conn.fetchrow(
                "SELECT SUM(total_amount) as revenue FROM orders WHERE DATE(created_at) = CURRENT_DATE"
            )
            
            # Monthly revenue
            monthly_revenue = await conn.fetchrow(
                "SELECT SUM(total_amount) as revenue FROM orders WHERE DATE_PART('month', created_at) = DATE_PART('month', CURRENT_DATE)"
            )
            
            # Top products
            top_products = await conn.fetch(
                """SELECT product_id, COUNT(*) as order_count 
                   FROM orders, jsonb_array_elements(items) as item 
                   WHERE item->>'product_id' IS NOT NULL 
                   GROUP BY product_id 
                   ORDER BY order_count DESC 
                   LIMIT 10"""
            )
            
            # Customer retention
            active_customers = await conn.fetchrow(
                "SELECT COUNT(DISTINCT user_id) as count FROM orders WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'"
            )
            
            return {
                "daily_revenue": daily_revenue['revenue'] or 0,
                "monthly_revenue": monthly_revenue['revenue'] or 0,
                "top_products": [
                    {"product_id": p['product_id'], "count": p['order_count']} 
                    for p in top_products
                ],
                "active_customers": active_customers['count']
            }

class SecurityService:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self.secret_key = "your_secret_key"
    
    def generate_secure_token(self, user_id: int) -> str:
        """Generate secure token for user authentication"""
        timestamp = str(int(datetime.now().timestamp()))
        data = f"{user_id}:{timestamp}"
        signature = hmac.new(
            self.secret_key.encode(),
            data.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"{data}:{signature}"
    
    def verify_token(self, token: str) -> Optional[int]:
        """Verify and extract user ID from token"""
        try:
            parts = token.split(':')
            if len(parts) != 3:
                return None
            
            user_id, timestamp, signature = parts
            data = f"{user_id}:{timestamp}"
            expected_signature = hmac.new(
                self.secret_key.encode(),
                data.encode(),
                hashlib.sha256
            ).hexdigest()
            
            if signature != expected_signature:
                return None
            
            # Check if token is not too old (24 hours)
            if int(timestamp) < int(datetime.now().timestamp()) - 86400:
                return None
            
            return int(user_id)
        except Exception:
            return None
    
    async def log_security_event(self, event_type: str, user_id: int, details: str):
        """Log security events"""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO security_logs (event_type, user_id, event_time, details)
                   VALUES ($1, $2, $3, $4)""",
                event_type, user_id, datetime.now(), details
            )
    
    async def check_rate_limit(self, user_id: int, action: str, limit: int = 10) -> bool:
        """Check if user has exceeded rate limit"""
        async with self.db_pool.acquire() as conn:
            count = await conn.fetchrow(
                """SELECT COUNT(*) as count FROM user_actions 
                   WHERE user_id = $1 AND action = $2 
                   AND created_at >= CURRENT_TIMESTAMP - INTERVAL '1 hour'""",
                user_id, action
            )
            
            return count['count'] < limit

class AdminService:
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
        self.admin_users = set()  # Admin user IDs
    
    async def is_admin(self, user_id: int) -> bool:
        """Check if user is admin"""
        async with self.db_pool.acquire() as conn:
            admin = await conn.fetchrow(
                "SELECT is_admin FROM users WHERE telegram_id = $1",
                user_id
            )
            return admin and admin['is_admin']
    
    async def get_pending_orders(self) -> List[Dict]:
        """Get all pending orders for admin management"""
        async with self.db_pool.acquire() as conn:
            orders = await conn.fetch(
                """SELECT o.*, u.username, u.phone 
                   FROM orders o 
                   JOIN users u ON o.user_id = u.telegram_id 
                   WHERE o.status = 'pending' 
                   ORDER BY o.created_at DESC"""
            )
            
            return [dict(order) for order in orders]
    
    async def update_order_status(self, order_id: str, new_status: str, admin_id: int) -> bool:
        """Update order status (admin only)"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE orders SET status = $1 WHERE order_id = $2",
                    new_status, order_id
                )
                
                await conn.execute(
                    """INSERT INTO admin_actions (admin_id, action, order_id, timestamp)
                       VALUES ($1, $2, $3, $4)""",
                    admin_id, f"status_change_{new_status}", order_id, datetime.now()
                )
                
                return True
        except Exception as e:
            logger.error(f"Order status update failed: {e}")
            return False
    
    async def get_system_stats(self) -> Dict:
        """Get system statistics for admin dashboard"""
        async with self.db_pool.acquire() as conn:
            stats = {}
            
            # Total users
            stats['total_users'] = await conn.fetchval("SELECT COUNT(*) FROM users")
            
            # Today's orders
            stats['today_orders'] = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE DATE(created_at) = CURRENT_DATE"
            )
            
            # Pending orders
            stats['pending_orders'] = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE status = 'pending'"
            )
            
            # Today's revenue
            stats['today_revenue'] = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE DATE(created_at) = CURRENT_DATE"
            )
            
            return stats

class OrderService:
    """Handle order operations"""
    
    def __init__(self, db_pool: asyncpg.Pool, redis_client):
        self.db_pool = db_pool
        self.redis_client = redis_client
    
    async def create_order(self, user_id: str, items: List[Dict], delivery_address: str, 
                          payment_method: str = 'cash') -> Dict:
        """Create a new order"""
        try:
            async with self.db_pool.acquire() as conn:
                # Calculate total amount
                total_amount = sum(item['price'] * item['quantity'] for item in items)
                
                # Generate order ID
                order_id = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8]}"
                
                # Create order
                order = await conn.fetchrow(
                    """INSERT INTO orders (order_id, user_id, items, total_amount, 
                       delivery_address, payment_method, status, created_at)
                       VALUES ($1, $2, $3, $4, $5, $6, 'pending', CURRENT_TIMESTAMP)
                       RETURNING *""",
                    order_id, user_id, json.dumps(items), total_amount, 
                    delivery_address, payment_method
                )
                
                return dict(order)
        except Exception as e:
            logger.error(f"Error creating order: {e}")
            raise
    
    async def get_user_orders(self, user_id: str, limit: int = 10) -> List[Dict]:
        """Get user's order history"""
        async with self.db_pool.acquire() as conn:
            orders = await conn.fetch(
                """SELECT * FROM orders WHERE user_id = $1 
                   ORDER BY created_at DESC LIMIT $2""",
                user_id, limit
            )
            return [dict(order) for order in orders]
    
    async def get_order_details(self, order_id: str) -> Optional[Dict]:
        """Get detailed order information"""
        async with self.db_pool.acquire() as conn:
            order = await conn.fetchrow(
                "SELECT * FROM orders WHERE order_id = $1",
                order_id
            )
            return dict(order) if order else None

class ProductService:
    """Handle product operations"""
    
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
    
    async def get_available_products(self) -> List[Dict]:
        """Get all available products"""
        async with self.db_pool.acquire() as conn:
            products = await conn.fetch(
                "SELECT * FROM products WHERE is_active = true ORDER BY name"
            )
            return [dict(product) for product in products]
    
    async def get_product_by_id(self, product_id: str) -> Optional[Dict]:
        """Get product by ID"""
        async with self.db_pool.acquire() as conn:
            product = await conn.fetchrow(
                "SELECT * FROM products WHERE id = $1 AND is_active = true",
                product_id
            )
            return dict(product) if product else None
    
    async def update_product_stock(self, product_id: str, quantity: int):
        """Update product stock"""
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE products SET stock_quantity = stock_quantity - $1 WHERE id = $2",
                quantity, product_id
            )

class SubscriptionService:
    """Handle subscription operations"""
    
    def __init__(self, db_pool: asyncpg.Pool):
        self.db_pool = db_pool
    
    async def create_subscription(self, user_id: str, product_id: str, 
                                frequency_days: int, quantity: int) -> Dict:
        """Create a new subscription"""
        try:
            async with self.db_pool.acquire() as conn:
                subscription = await conn.fetchrow(
                    """INSERT INTO subscriptions (user_id, product_id, frequency_days, 
                       quantity, next_delivery_date, status, created_at)
                       VALUES ($1, $2, $3, $4, CURRENT_DATE + INTERVAL '$3 days', 'active', CURRENT_TIMESTAMP)
                       RETURNING *""",
                    user_id, product_id, frequency_days, quantity
                )
                return dict(subscription)
        except Exception as e:
            logger.error(f"Error creating subscription: {e}")
            raise
    
    async def get_user_subscriptions(self, user_id: str) -> List[Dict]:
        """Get user's active subscriptions"""
        async with self.db_pool.acquire() as conn:
            subscriptions = await conn.fetch(
                """SELECT s.*, p.name as product_name, p.price 
                   FROM subscriptions s
                   JOIN products p ON s.product_id = p.id
                   WHERE s.user_id = $1 AND s.status = 'active'
                   ORDER BY s.next_delivery_date""",
                user_id
            )
            return [dict(sub) for sub in subscriptions]
    
    async def cancel_subscription(self, subscription_id: str) -> bool:
        """Cancel a subscription"""
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE subscriptions SET status = 'cancelled' WHERE id = $1",
                    subscription_id
                )
                return True
        except Exception as e:
            logger.error(f"Error cancelling subscription: {e}")
            return False
