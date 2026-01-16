"""
Order management handlers
"""
import logging
from typing import Dict, Any, List
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import OrderKeyboards, MenuKeyboards, ProfileKeyboards, PaymentKeyboards
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, format_price, MessageBuilder, get_auth_token

logger = logging.getLogger('handlers')


class OrderHandlers:
    """Order-related handlers"""
    
    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)
    
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's orders"""
        try:
            user = await user_middleware(update)
            if not user:
                return
            
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Get user token
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Get user orders
                response = await client.get_user_orders(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                orders = response.data.get('data', {}).get('orders', [])
            
            if not orders:
                no_orders_text = i18n.get('telegram.orders.no_orders', language)
                keyboard = MenuKeyboards.main_menu(language)
                
                if update.callback_query:
                    try:
                        await update.callback_query.edit_message_text(
                            text=no_orders_text,
                            reply_markup=keyboard
                        )
                    except Exception as edit_error:
                        # Handle "message is not modified" error silently
                        if "message is not modified" in str(edit_error).lower():
                            logger.debug(f"Message not modified for user {user_id} - content is the same")
                        else:
                            raise edit_error
                    await update.callback_query.answer()
                else:
                    await update.message.reply_text(
                        text=no_orders_text,
                        reply_markup=keyboard
                    )
                return
            
            # Show orders list
            orders_text = i18n.get('telegram.orders.your_orders', language, count=len(orders)) + "\n\n"
            keyboard = OrderKeyboards.order_list(orders, language)
            
            if update.callback_query:
                try:
                    await update.callback_query.edit_message_text(
                        text=orders_text,
                        reply_markup=keyboard
                    )
                except Exception as edit_error:
                    # Handle "message is not modified" error silently
                    if "message is not modified" in str(edit_error).lower():
                        logger.debug(f"Message not modified for user {user_id} - content is the same")
                    else:
                        raise edit_error
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=orders_text,
                    reply_markup=keyboard
                )
            
            logger.info(f"Orders menu shown to user {user_id}")
            
        except Exception as e:
            logger.error(f"Error in orders menu: {e}")
            await self._handle_error(update)
    
    async def order_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show order details"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract order ID
            order_id = int(query.data.split('_')[1])
            
            # Get order details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_order(user_token, order_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                order = response.data['data']['order']
                delivery = response.data['data']['delivery']
            
            # Format order details
            details_text = MessageBuilder.build_order_summary(order, language)
            logger.info(f"order_details handler: details_text: {details_text}")
            
            # Add order items if available
            if order.get('order_items'):
                details_text += f"\n\n📋 {i18n.get('telegram.orders.items_header', language)}:\n"
                for item in order['order_items']:
                    details_text += f"• {item.get('product_name', 'Unknown')} x{item.get('quantity', 1)}\n"
                    details_text += f"  💰 {format_price(item.get('total_price', 0))} UZS\n"
            
            # Add delivery info if available
            if order.get('delivery_address'):
                details_text += f"\n📍 {i18n.get('telegram.orders.delivery_info', language)}:\n{order['delivery_address']}"
            
            keyboard = OrderKeyboards.order_details(order_id, order.get('status', ''), language)
            
            await query.edit_message_text(
                text=details_text,
                reply_markup=keyboard
            )
            await query.answer()
            
            logger.info(f"Order {order_id} details shown to user {user_id}")
            
        except Exception as e:
            logger.error(f"Error in order details: {e}")
            await self._handle_error(update)
    
    async def track_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show order tracking information with visual timeline"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract order ID from callback data (format: track_order_123)
            order_id = int(query.data.split('_')[2])
            
            # Get order tracking details from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.track_order(user_token, order_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                tracking_data = response.data.get('data', {})
                order = tracking_data.get('order', {})
                delivery = tracking_data.get('delivery', {})
                timeline = tracking_data.get('timeline', [])
                time_remaining = tracking_data.get('estimated_time_remaining', {})
            
            # Status icons mapping
            status_icons = {
                'created': '📝',
                'pending': '🕐',
                'confirmed': '✅',
                'preparing': '👨‍🍳',
                'out_for_delivery': '🚚',
                'delivered': '📦',
                'cancelled': '❌',
                'returned': '↩️'
            }
            
            # Status labels mapping
            status_labels = {
                'created': i18n.get('telegram.orders.status_created', language) or 'Order Placed',
                'pending': i18n.get('telegram.orders.status_pending', language) or 'Pending Confirmation',
                'confirmed': i18n.get('telegram.orders.status_confirmed', language) or 'Order Confirmed',
                'preparing': i18n.get('telegram.orders.status_preparing', language) or 'Being Prepared',
                'out_for_delivery': i18n.get('telegram.orders.status_out_for_delivery', language) or 'Out for Delivery',
                'delivered': i18n.get('telegram.orders.status_delivered', language) or 'Delivered',
                'cancelled': i18n.get('telegram.orders.status_cancelled', language) or 'Cancelled',
                'returned': i18n.get('telegram.orders.status_returned', language) or 'Returned'
            }
            
            # Build tracking message header
            tracking_text = f"📍 *{i18n.get('telegram.orders.tracking_title', language) or 'Order Tracking'}*\n\n"
            tracking_text += f"🔢 Order #{order.get('order_number', order_id)}\n\n"
            
            # Build visual timeline
            tracking_text += f"━━━ {i18n.get('telegram.orders.timeline', language) or 'Timeline'} ━━━\n"
            
            if timeline:
                for entry in timeline:
                    status = entry.get('status', 'unknown')
                    timestamp = entry.get('timestamp', '')
                    is_current = entry.get('is_current', False)
                    notes = entry.get('notes', '')
                    
                    # Format timestamp for display
                    formatted_time = ''
                    if timestamp:
                        try:
                            from datetime import datetime
                            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            formatted_time = dt.strftime('%d %b %H:%M')
                        except:
                            formatted_time = timestamp[:16] if len(timestamp) > 16 else timestamp
                    
                    icon = status_icons.get(status, '📋')
                    label = status_labels.get(status, status.replace('_', ' ').title())
                    
                    # Mark current status
                    if is_current:
                        tracking_text += f"🔵 {formatted_time} - {label} ← Current\n"
                    else:
                        tracking_text += f"✅ {formatted_time} - {label}\n"
                    
                    # Add notes if present (escape markdown special chars)
                    if notes:
                        # Escape markdown special characters
                        safe_notes = str(notes).replace('*', '').replace('_', '').replace('`', '')
                        tracking_text += f"   ({safe_notes})\n"
            else:
                # Fallback if no timeline data
                current_status = order.get('status', 'pending')
                icon = status_icons.get(current_status, '📋')
                label = status_labels.get(current_status, current_status)
                tracking_text += f"{icon} {label}\n"
            
            tracking_text += "\n"
            
            # Add estimated time remaining
            if time_remaining and time_remaining.get('total_minutes'):
                mins = time_remaining.get('total_minutes', 0)
                hours = time_remaining.get('hours', 0)
                if hours > 0:
                    tracking_text += f"⏰ {i18n.get('telegram.orders.estimated_remaining', language) or 'Estimated'}: {hours}h {mins % 60}m\n"
                else:
                    tracking_text += f"⏰ {i18n.get('telegram.orders.estimated_remaining', language) or 'Estimated'}: {mins} min\n"
            
            # Add delivery info if available
            if delivery:
                if delivery.get('driver_name'):
                    tracking_text += f"\n🚗 {i18n.get('telegram.orders.driver', language) or 'Driver'}: {delivery['driver_name']}\n"
                if delivery.get('driver_phone'):
                    tracking_text += f"📞 {delivery['driver_phone']}\n"
            
            # Create back button (use order_tracking keyboard for tracking view)
            keyboard = OrderKeyboards.order_tracking(order_id, language)
            
            await query.edit_message_text(
                text=tracking_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            await query.answer()
            
            logger.info(f"Order {order_id} tracking with timeline shown to user {user_id}")
            
        except Exception as e:
            logger.error(f"Error in track_order: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            await self._handle_error(update)
    
    async def checkout_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle checkout process"""
        try:
            user = await user_middleware(update)
            if not user:
                return
            
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Get user's addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                addresses = response.data.get('data', {}).get('addresses', [])
            
            if not addresses:
                # No addresses, prompt to add one
                add_address_text = i18n.get('telegram.orders.no_address_prompt', language)
                keyboard = ProfileKeyboards.location_request(language)
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text=add_address_text,
                        reply_markup=MenuKeyboards.back_button(language)
                    )
                    await update.callback_query.answer()
                else:
                    await update.message.reply_text(
                        text=add_address_text,
                        reply_markup=MenuKeyboards.back_button(language)
                    )
                
                # Set state for address input
                await self.user_repo.update_user_state(user_id, {'awaiting_input': 'address_location'})
                return
            
            # Show address selection
            address_text = i18n.get('telegram.orders.select_address', language)
            keyboard = OrderKeyboards.delivery_addresses(addresses, language)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=address_text,
                    reply_markup=keyboard
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=address_text,
                    reply_markup=keyboard
                )
            
        except Exception as e:
            logger.error(f"Error in checkout handler: {e}")
            await self._handle_error(update)
    
    async def address_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract address ID
            address_id = int(query.data.split('_')[1])
            
            # Store selected address
            context.user_data['selected_address_id'] = address_id
            
            # Show payment methods
            payment_methods = [
                {'type': 'cash', 'name': i18n.get('telegram.payment_cash', language)},
                {'type': 'card', 'name': i18n.get('telegram.payment_card', language)},
                {'type': 'payme', 'name': i18n.get('telegram.payment_payme', language)},
                {'type': 'click', 'name': i18n.get('telegram.payment_click', language)},
            ]

            payment_text = i18n.get('telegram.orders.select_payment', language)
            keyboard = OrderKeyboards.payment_methods(payment_methods, language)
            
            await query.edit_message_text(
                text=payment_text,
                reply_markup=keyboard
            )
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error in address handler: {e}")
            await self._handle_error(update)
    
    async def payment_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle payment method selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract payment method
            payment_method = query.data.split('_')[1]
            
            # Store payment method
            context.user_data['selected_payment_method'] = payment_method
            
            # Show order confirmation
            await self._show_order_confirmation(update, context)
            
        except Exception as e:
            logger.error(f"Error in payment handler: {e}")
            await self._handle_error(update)
    
    async def confirm_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle order confirmation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get stored order data
            address_id = context.user_data.get('selected_address_id')
            payment_method = context.user_data.get('selected_payment_method')

            if not address_id or not payment_method:
                await query.answer(i18n.get('telegram.orders.missing_info', language))
                return

            # Create order
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get cart items from API
                response = await client.get_cart(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                cart = response.data['data']['cart']
                if not cart or not cart.get('cart_items'):
                    await self._handle_api_error(update, i18n.get('telegram.orders.cart_empty', language), language)
                    return

                order_data = {
                    'delivery_address_id': address_id,
                    'payment_method': payment_method,
                    'source': 'telegram',
                    'items': [
                        {
                            'product_id': item['product']['id'],
                            'quantity': item['quantity'],
                        } for item in cart['cart_items']
                    ]
                }

                response = await client.create_order(user_token, order_data)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                order = response.data['data']['order']

            # Handle different payment methods
            if payment_method == 'payme':
                # Payme payment - send invoice via Telegram Payments API
                # Don't clear cart yet - wait for successful payment
                from handlers.payments import payment_handlers

                # Store order data for payment flow
                context.user_data['pending_order_id'] = order['id']
                context.user_data['pending_order_amount'] = order['total_amount']

                # Build order data with items for invoice
                order_for_payment = {
                    'id': order['id'],
                    'order_number': order.get('order_number', str(order['id'])),
                    'total_amount': order['total_amount'],
                    'order_items': order.get('order_items', [])
                }

                # Send Payme invoice
                invoice_sent = await payment_handlers.send_payme_invoice(
                    update, context, order_for_payment
                )

                if not invoice_sent:
                    # Invoice failed - show error with options
                    error_text = i18n.get('telegram.payment.failed_message', language) or \
                        "Failed to create payment. Please try again or choose a different method."

                    keyboard = PaymentKeyboards.payment_failed(order['id'], language)

                    await query.edit_message_text(
                        text=f"❌ {error_text}",
                        reply_markup=keyboard
                    )

                # Don't clear context data - needed for payment flow
                logger.info(f"Payme invoice sent for order {order['id']} to user {user_id}")
                return

            elif payment_method == 'click':
                # Click payment - similar flow (to be implemented)
                # For now, treat like cash
                pass

            # Cash or other payment methods - process immediately
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    # Clear user's cart
                    await client.clear_cart(user_token)

            # Show success message
            success_text = i18n.get('telegram.orders.placed_success', language) + "\n\n"
            success_text += MessageBuilder.build_order_summary(order, language)

            if payment_method == 'cash':
                success_text += "\n\n" + (
                    i18n.get('telegram.orders.cash_note', language) or
                    "Please have the exact amount ready for the driver."
                )

            keyboard = MenuKeyboards.main_menu(language)

            await query.edit_message_text(
                text=success_text,
                reply_markup=keyboard
            )
            await query.answer(i18n.get('telegram.orders.placed_success', language))

            # Clear order data
            context.user_data.clear()

            logger.info(f"Order created successfully for user {user_id} with payment method: {payment_method}")

        except Exception as e:
            logger.error(f"Error confirming order: {e}")
            await self._handle_error(update)
    
    async def _show_order_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show order confirmation screen"""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)
        
        # Build confirmation message
        confirmation_text = i18n.get('telegram.orders.confirmation_title', language) + "\n\n"
        
        # Get cart items from API by api_client.get_cart and show them
        cart_total_amount = 0
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return
            
            response = await client.get_cart(user_token)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return
            
            cart = response.data['data']['cart']
            if not cart:
                await self._handle_api_error(update, i18n.get('telegram.orders.cart_empty', language), language)
                return
            confirmation_text += f"🛒 {i18n.get('telegram.orders.items_header', language)}:\n"
            for item in cart.get('cart_items', []):
                confirmation_text += f"• {item.get('product', {}).get('name', 'Unknown')} x{item.get('quantity', 1)}\n"
                item_subtotal_price = item.get('product', {}).get('current_price', 0) * item.get('quantity', 1)
                cart_total_amount += item_subtotal_price
                confirmation_text += f"  💰 {format_price(item_subtotal_price)} UZS\n\n"
        
        # Add address info
        address_id = context.user_data.get('selected_address_id')
        if address_id:
            confirmation_text += f"📍 {i18n.get('telegram.delivery_address', language)}: Selected address #{address_id}\n\n"

        # Add payment method
        payment_method = context.user_data.get('selected_payment_method')
        if payment_method:
            confirmation_text += f"💳 {i18n.get('telegram.orders.payment_info', language)}: {payment_method.title()}\n\n"

        # Add total amount
        confirmation_text += f"💰 {i18n.get('telegram.total', language)}: {format_price(cart_total_amount)} UZS\n"
        confirmation_text += f"🚚 {i18n.get('telegram.orders.delivery_fee', language)}: Free\n"
        confirmation_text += "────────────────\n"
        confirmation_text += f"💳 {i18n.get('telegram.orders.grand_total', language)}: {format_price(cart_total_amount)} UZS"
        
        keyboard = OrderKeyboards.order_confirmation(language)
        
        await update.callback_query.edit_message_text(
            text=confirmation_text,
            reply_markup=keyboard
        )
        await update.callback_query.answer()
    
    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error"""
        error_msg = i18n.get('telegram.error.auth_failed', language)

        if update.callback_query:
            await update.callback_query.edit_message_text(error_msg)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(error_msg)
    
    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Handle API error"""
        error_msg = f"❌ {error}"
        
        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)
    
    async def _handle_error(self, update: Update):
        """Handle general error"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('telegram.error_occurred', language)
        except:
            error_msg = i18n.get('telegram.error_occurred', 'en')

        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)


# Global handler instance
order_handlers = OrderHandlers()