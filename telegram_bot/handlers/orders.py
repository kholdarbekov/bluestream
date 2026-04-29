"""
Order management handlers
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List
from telegram import Update, constants
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from i18n import i18n
from keyboards import OrderKeyboards, MenuKeyboards, PaymentKeyboards
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, format_price, MessageBuilder, get_auth_token
from shared.constants import ORDER_STATUS_ICONS, DEFAULT_STATUS_ICON, DISPLAY_TIMEZONE
from handlers.base import BaseHandler

logger = logging.getLogger('handlers')


class OrderHandlers(BaseHandler):
    """Order-related handlers"""

    @staticmethod
    def _build_checkout_payment_methods(available_methods: List[Dict[str, Any]], language: str) -> List[Dict[str, str]]:
        method_codes = {
            str(method.get('method'))
            for method in (available_methods or [])
            if method.get('is_active', True)
        }
        payment_methods: List[Dict[str, str]] = []
        if 'cash' in method_codes:
            payment_methods.append({
                'type': 'cash',
                'name': i18n.get('telegram.payment_cash', language),
            })
        if any(code != 'cash' for code in method_codes):
            payment_methods.append({
                'type': 'card',
                'name': i18n.get('telegram.payment_card', language),
            })
        return payment_methods

    @staticmethod
    def _cod_restriction_notice(restrictions: Dict[str, Any]) -> str:
        active_debt_count = restrictions.get('active_cod_debt_count') or 0
        if active_debt_count:
            return (
                f"Cash on delivery is unavailable because you already have "
                f"{active_debt_count} outstanding COD debts. Please choose a card payment method."
            )
        return "Cash on delivery is temporarily unavailable. Please choose a card payment method."

    @staticmethod
    def _build_cod_prepayment_brief(cart: Dict[str, Any], order_total: float) -> str:
        """Build a short COD prepayment summary for post-order success message."""
        cod_prepayment = (cart or {}).get('cod_prepayment') or {}
        available_balance = float(cod_prepayment.get('available_balance') or 0)
        if available_balance <= 0:
            return ""

        normalized_order_total = float(order_total or 0)
        potential_applied = float(
            cod_prepayment.get('potential_applied_amount')
            or min(available_balance, normalized_order_total)
        )
        potential_applied = max(0.0, min(potential_applied, normalized_order_total))
        if potential_applied <= 0:
            return ""

        payable_after = max(0.0, normalized_order_total - potential_applied)
        return (
            f"\n🔁 COD prepaid used: {format_price(potential_applied)} UZS."
            f" Pay on delivery: {format_price(payable_after)} UZS."
        )

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
            unknown_text = i18n.get('telegram.common.unknown', language)

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
                    details_text += f"• {item.get('product_name', unknown_text)} x{item.get('quantity', 1)}\n"
                    details_text += f"  💰 {format_price(item.get('total_price', 0))} UZS\n"

            details_text = escape_markdown(details_text, version=2)

            # Add delivery info if available
            if order.get('delivery_address'):
                # Make order delivery address title bold.
                details_text += f"\n{i18n.get('telegram.orders.delivery_info', language)}:\n*{escape_markdown(order['delivery_address'].get('title', unknown_text), version=2)}* \- {escape_markdown(order['delivery_address'].get('full_address', ''), version=2)}"

            keyboard = OrderKeyboards.order_details(order_id, order.get('status', ''), language)

            logger.info(f"order_details handler: details_text after escaping: {details_text}")
            await query.edit_message_text(
                text=details_text,
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.MARKDOWN_V2
            )
            await query.answer()

            logger.info(f"Order {order_id} details shown to user {user_id}")

        except Exception as e:
            logger.error(f"Error in order details: {e}")
            await self._handle_error(update)

    async def cancel_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle order cancellation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data (format: cancel_order_123)
            order_id = int(query.data.split('_')[2])

            # Confirm cancellation
            if 'confirm' not in query.data:
                # Ask for confirmation first
                confirm_keyboard = MenuKeyboards.yes_no_buttons(
                    language,
                    yes_callback='cancel_order_confirm_yes',
                    no_callback='cancel_order_confirm_no'
                )

                # Context needs to know which order we are cancelling
                context.user_data['cancelling_order_id'] = order_id

                await query.edit_message_text(
                    text=i18n.get('telegram.orders.cancel_confirm', language),
                    reply_markup=confirm_keyboard
                )
                await query.answer()
                return

        except Exception as e:
            logger.error(f"Error in cancel_order: {e}")
            await self._handle_error(update)

    async def cancel_order_confirm_yes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process confirmed cancellation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Check if this is for order cancellation
            order_id = context.user_data.get('cancelling_order_id')
            if not order_id:
                # Not for us, or expired
                await query.answer()
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.cancel_order(user_token, order_id)

                if response.success:
                    await query.answer(i18n.get('telegram.orders.cancel_success', language))
                    # Clear context
                    context.user_data.pop('cancelling_order_id', None)
                    # Redirect to orders list
                    await self.orders_menu(update, context)
                else:
                    await self._handle_api_error(update, response.error, language)

        except Exception as e:
            logger.error(f"Error processing cancellation: {e}")
            await self._handle_error(update)

    async def cancel_order_confirm_no(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process cancelled cancellation (User clicked No)"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Check if this is for order cancellation
            order_id = context.user_data.get('cancelling_order_id')
            if not order_id:
                await query.answer()
                return

            # Clear context
            context.user_data.pop('cancelling_order_id', None)

            # Return to order details
            # Hack: modify query.data to be what order_details expects
            query.data = f"order_{order_id}"
            await self.order_details(update, context)

        except Exception as e:
            logger.error(f"Error denying cancellation: {e}")
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

            status_icons = ORDER_STATUS_ICONS

            # Status labels mapping
            status_labels = {
                'created': i18n.get('telegram.orders.status_created', language),
                'pending': i18n.get('telegram.orders.status_pending', language),
                'confirmed': i18n.get('telegram.orders.status_confirmed', language),
                'preparing': i18n.get('telegram.orders.status_preparing', language),
                'out_for_delivery': i18n.get('telegram.orders.status_out_for_delivery', language),
                'delivered': i18n.get('telegram.orders.status_delivered', language),
                'cancelled': i18n.get('telegram.orders.status_cancelled', language),
                'returned': i18n.get('telegram.orders.status_returned', language)
            }

            # Build tracking message header
            tracking_text = f"📍 *{i18n.get('telegram.orders.tracking_title', language)}*\n\n"
            tracking_text += f"🔢 {i18n.get('telegram.order.number', language, order.get('order_number', order_id))}\n\n"

            # Build visual timeline
            tracking_text += f"━━━ {i18n.get('telegram.orders.timeline', language)} ━━━\n"

            if timeline:
                for entry in timeline:
                    status = entry.get('status', 'unknown')
                    timestamp = entry.get('timestamp', '')
                    is_current = entry.get('is_current', False)
                    notes = entry.get('notes', '')

                    # Format timestamp for display (convert UTC → display timezone)
                    formatted_time = ''
                    if timestamp:
                        try:
                            from datetime import datetime
                            from zoneinfo import ZoneInfo
                            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                            dt_local = dt.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
                            formatted_time = dt_local.strftime('%d %b %H:%M')
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Failed to parse order timestamp '{timestamp}': {e}")
                            formatted_time = timestamp[:16] if len(timestamp) > 16 else timestamp

                    icon = status_icons.get(status, '📋')
                    label = status_labels.get(status, status.replace('_', ' ').title())

                    # Mark current status
                    if is_current:
                        tracking_text += f"🔵 {formatted_time} - {label} ← {i18n.get('telegram.orders.current_status', language)}\n"
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
                    tracking_text += f"⏰ {i18n.get('telegram.orders.estimated_remaining', language)}: {hours}h {mins % 60}m\n"
                else:
                    tracking_text += f"⏰ {i18n.get('telegram.orders.estimated_remaining', language)}: {mins}m\n"

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

            # Store addresses for display in order confirmation
            context.user_data['checkout_addresses'] = {
                addr['id']: {'title': addr.get('title', ''), 'full_address': addr.get('full_address', '')}
                for addr in addresses
            }

            if not addresses:
                # No addresses, prompt to add one
                add_address_text = i18n.get('telegram.orders.no_address_prompt', language)
                keyboard = OrderKeyboards.delivery_addresses([], language)
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text=add_address_text,
                        reply_markup=keyboard
                    )
                    await update.callback_query.answer()
                else:
                    await update.message.reply_text(
                        text=add_address_text,
                        reply_markup=keyboard
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

            # Store selected address and its display info
            context.user_data['selected_address_id'] = address_id
            address_map = context.user_data.get('checkout_addresses', {})
            address_info = address_map.get(address_id, {})
            context.user_data['selected_address_title'] = address_info.get('title', '')
            context.user_data['selected_address_full'] = address_info.get('full_address', '')

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_payment_methods(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            payment_payload = response.data.get('data', {})
            payment_methods = self._build_checkout_payment_methods(
                payment_payload.get('available_methods', []),
                language,
            )
            if not payment_methods:
                await self._handle_api_error(
                    update,
                    "No payment methods are available right now. Please try again later.",
                    language,
                )
                return

            payment_text = i18n.get('telegram.orders.select_payment', language)
            restrictions = payment_payload.get('payment_restrictions') or {}
            if restrictions.get('cod_restricted'):
                payment_text += "\n\n" + self._cod_restriction_notice(restrictions)
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

    async def cancel_checkout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel checkout from order confirmation screen."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Clear only checkout-related selections and keep unrelated context.
            context.user_data.pop('selected_address_id', None)
            context.user_data.pop('selected_address_title', None)
            context.user_data.pop('selected_address_full', None)
            context.user_data.pop('selected_payment_method', None)
            context.user_data.pop('checkout_addresses', None)

            await query.edit_message_text(
                text=i18n.get('telegram.action_cancelled', language),
                reply_markup=MenuKeyboards.main_menu(language),
            )
            await query.answer(i18n.get('telegram.action_cancelled_short', language))

            logger.info(f"Checkout cancelled by user {user_id}")
        except Exception as e:
            logger.error(f"Error cancelling checkout: {e}")
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
                    # Tax Committee unavailable — show dedicated error with options
                    if response.status_code == 503:
                        error_text = i18n.get('telegram.orders.asl_belgisi_error_message', language)
                        keyboard = OrderKeyboards.asl_belgisi_error(language)
                        await query.edit_message_text(text=error_text, reply_markup=keyboard)
                        return
                    await self._handle_api_error(update, response.error, language)
                    return

                order = response.data['data']['order']
                response_payload = response.data.get('data', {}) or {}

            # For card/click: show "preparing" message and wait until payment_ready_at
            # so the Tax Committee utilisation is at least PRE_PAYMENT_UTILISATION_WAIT_SECONDS
            # before the user sees the payment link.
            #
            # When the proactive marking-code pool covers the order the backend
            # returns payment_ready_at == now → remaining ≤ 0 → no preparing
            # message is shown and the link is delivered immediately via the
            # original edit-in-place path (no need for the notification UX
            # because there was nothing to wait for).
            needed_wait = False
            if payment_method in ['card', 'click']:
                payment_ready_at_str = response_payload.get('payment_ready_at')
                if payment_ready_at_str:
                    payment_ready_at = datetime.fromisoformat(payment_ready_at_str.replace('Z', '+00:00'))
                    remaining = (payment_ready_at - datetime.now(timezone.utc)).total_seconds()
                    logger.info(f"Payment for order: {order.get('order_number')} will be ready at {payment_ready_at_str}, waiting for {remaining:.2f} seconds before showing payment link to user {user_id}")
                    if remaining > 0:
                        needed_wait = True
                        preparing_text = i18n.get('telegram.orders.preparing_payment_message', language).format(
                            order_number=order.get('order_number', str(order['id']))
                        )
                        await query.edit_message_text(text=preparing_text)
                        await asyncio.sleep(remaining)

            # Handle different payment methods
            if payment_method in ['payme', 'card', 'click']:
                provider_method = 'payme' if payment_method == 'payme' else 'click'
                # Redirect payment flow
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

                # Send external payment link.
                # Only use the "new message + notification" UX when we actually
                # made the user wait — i.e. the slow path. When the proactive
                # pool covered the order, fall back to edit-in-place because
                # there's no preparing message to replace.
                invoice_sent = await payment_handlers.send_payment_link(
                    update,
                    context,
                    order_for_payment,
                    payment_method=provider_method,
                    send_as_new_message=(needed_wait and provider_method == 'click'),
                )

                if not invoice_sent:
                    # Invoice failed - show error with options
                    error_text = i18n.get('telegram.payment.failed_message', language)

                    keyboard = PaymentKeyboards.payment_failed(order['id'], language)

                    await query.edit_message_text(
                        text=f"❌ {error_text}",
                        reply_markup=keyboard
                    )

                # Don't clear context data - needed for payment flow
                logger.info(f"{provider_method} payment link sent for order {order['id']} to user {user_id}")
                return

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
                success_text += "\n\n" + i18n.get('telegram.orders.cash_note', language)
                success_text += self._build_cod_prepayment_brief(
                    cart=cart,
                    order_total=order.get('total_amount', 0)
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

    async def select_payment_cash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Switch payment method to cash and retry order confirmation.

        Triggered when the user taps "Pay with cash" on the Asl belgisi error screen.
        """
        context.user_data['selected_payment_method'] = 'cash'
        await self.confirm_order(update, context)

    async def _show_order_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show order confirmation screen"""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)
        unknown_text = i18n.get('telegram.common.unknown', language)

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
            confirmation_text += f"{i18n.get('telegram.orders.items_header', language)}:\n"
            for item in cart.get('cart_items', []):
                confirmation_text += f"• {item.get('product', {}).get('name', unknown_text)} x{item.get('quantity', 1)}\n"
                item_subtotal_price = item.get('product', {}).get('current_price', 0) * item.get('quantity', 1)
                cart_total_amount += item_subtotal_price
                confirmation_text += f"  💰 {format_price(item_subtotal_price)} UZS\n\n"

        # Add address info
        address_id = context.user_data.get('selected_address_id')
        if address_id:
            address_title = context.user_data.get('selected_address_title', '')
            address_full = context.user_data.get('selected_address_full', '')
            display_address = address_title if address_title else address_full
            confirmation_text += f"{i18n.get('telegram.delivery_address', language)}: {display_address}\n\n"

        # Add payment method
        payment_method = context.user_data.get('selected_payment_method')
        if payment_method:
            payment_method_labels = {
                'cash': i18n.get('telegram.payment_cash', language),
                'card': i18n.get('telegram.payment_card', language),
                'payme': i18n.get('telegram.payment_payme', language),
            }
            payment_method_label = payment_method_labels.get(
                payment_method,
                i18n.get('telegram.common.unknown', language)
            )
            confirmation_text += f"{i18n.get('telegram.orders.payment_info', language)}: {payment_method_label}\n\n"

        # Add total amount
        confirmation_text += f"💰 {i18n.get('telegram.total', language)}: {format_price(cart_total_amount)} UZS\n"
        confirmation_text += f"🚚 {i18n.get('telegram.orders.delivery_fee', language, amount=0)}\n"
        confirmation_text += "────────────────\n"
        confirmation_text += f"💳 {i18n.get('telegram.orders.grand_total', language, amount=format_price(cart_total_amount))}"

        if payment_method == 'cash':
            cod_prepayment = cart.get('cod_prepayment') or {}
            available_balance = float(cod_prepayment.get('available_balance') or 0)
            if available_balance > 0:
                potential_applied = float(
                    cod_prepayment.get('potential_applied_amount')
                    or min(available_balance, float(cart_total_amount))
                )
                payable_after = float(
                    cod_prepayment.get('estimated_payable_after_prepayment')
                    or max(0.0, float(cart_total_amount) - potential_applied)
                )
                confirmation_text += "\n\n"
                confirmation_text += f"💳 COD prepaid balance: {format_price(available_balance)} UZS\n"
                confirmation_text += f"🔁 Auto-applied on this COD order: {format_price(potential_applied)} UZS\n"
                confirmation_text += f"🧾 Estimated COD payable after prepaid: {format_price(payable_after)} UZS"

        keyboard = OrderKeyboards.order_confirmation(language)

        await update.callback_query.edit_message_text(
            text=confirmation_text,
            reply_markup=keyboard
        )
        await update.callback_query.answer()

    async def back_to_order_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle back button from payment screen to order confirmation"""
        try:
            await self._show_order_confirmation(update, context)
        except Exception as e:
            logger.error(f"Error in back_to_order_confirm: {e}")
            await self._handle_error(update)


# Global handler instance
order_handlers = OrderHandlers()
