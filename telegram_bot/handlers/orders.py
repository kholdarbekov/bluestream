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

from eligibility import main_menu_for
from i18n import i18n
from keyboards import OrderKeyboards, MenuKeyboards, PaymentKeyboards
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, format_price, MessageBuilder, get_auth_token
from shared.constants import ORDER_STATUS_ICONS, DEFAULT_STATUS_ICON, DISPLAY_TIMEZONE
from shared.business_config import MIN_ORDER_AMOUNT
from handlers.base import BaseHandler
from handlers.products import product_handlers

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
        if any(code not in ('cash', 'business_account') for code in method_codes):
            payment_methods.append({
                'type': 'card',
                'name': i18n.get('telegram.payment_card', language),
            })
        if 'business_account' in method_codes:
            payment_methods.append({
                'type': 'business_account',
                'name': i18n.get('telegram.payment_business_account', language),
            })
        return payment_methods

    @staticmethod
    def _cod_restriction_notice(restrictions: Dict[str, Any], language: str) -> str:
        active_debt_count = restrictions.get('active_cod_debt_count') or 0
        if active_debt_count:
            return i18n.get(
                'telegram.orders.cod_restricted_has_debts',
                language,
                active_debt_count=active_debt_count,
            )
        return i18n.get('telegram.orders.cod_restricted_unavailable', language)

    @staticmethod
    def _build_cod_prepayment_brief(cart: Dict[str, Any], order_total: float, language: str) -> str:
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
        return i18n.get(
            'telegram.orders.cod_prepayment_applied',
            language,
            potential_applied=format_price(potential_applied),
            payable_after=format_price(payable_after),
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
                keyboard = await main_menu_for(update.effective_user.id, language)

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
            await self._handle_error(update, exc=e, operation="orders_menu")

    async def order_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int | None = None):
        """Show order details.

        ``order_id`` may be passed explicitly (e.g. when re-dispatched from
        another handler); otherwise it is parsed from the callback data. This
        avoids mutating the immutable ``CallbackQuery.data`` to re-route.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            unknown_text = i18n.get('telegram.common.unknown', language)

            # Extract order ID (parse from callback data only when not supplied)
            if order_id is None:
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

            # Add order items if available. A free loyalty-reward line (is_reward)
            # for a purchased product is merged into that product's line as a
            # "+N free 🎁" bonus; a reward product not otherwise purchased is shown
            # as its own "🎁 … free" line — clearer than a duplicate product row.
            if order.get('order_items'):
                details_text += f"\n\n📋 {i18n.get('telegram.orders.items_header', language)}:\n"
                items = order['order_items']
                free_suffix = i18n.get('telegram.loyalty.free_suffix', language)
                free_by_pid = {}
                for it in items:
                    if it.get('is_reward'):
                        pid = it.get('product_id')
                        free_by_pid[pid] = free_by_pid.get(pid, 0) + (it.get('quantity') or 0)
                paid_pids = {it.get('product_id') for it in items if not it.get('is_reward')}
                for it in items:
                    if it.get('is_reward'):
                        continue
                    bonus = free_by_pid.get(it.get('product_id'), 0)
                    suffix = f" (+{bonus} {free_suffix} 🎁)" if bonus else ""
                    details_text += f"• {it.get('product_name', unknown_text)} x{it.get('quantity', 1)}{suffix}\n"
                    details_text += f"  💰 {format_price(it.get('total_price', 0))} UZS\n"
                standalone = {}
                for it in items:
                    if it.get('is_reward') and it.get('product_id') not in paid_pids:
                        pid = it.get('product_id')
                        standalone[pid] = standalone.get(pid, 0) + (it.get('quantity') or 0)
                for pid, qty in standalone.items():
                    name = next((i.get('product_name', unknown_text) for i in items if i.get('product_id') == pid), unknown_text)
                    details_text += f"• 🎁 {name} x{qty} — {free_suffix}\n"

            details_text = escape_markdown(details_text, version=2)

            # Add delivery info if available
            if order.get('delivery_address'):
                # Make order delivery address title bold.
                details_text += f"\n{i18n.get('telegram.orders.delivery_info', language)}:\n*{escape_markdown(order['delivery_address'].get('title', unknown_text), version=2)}* \\- {escape_markdown(order['delivery_address'].get('full_address', ''), version=2)}"

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
            await self._handle_error(update, exc=e, operation="order_details")

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
            await self._handle_error(update, exc=e, operation="cancel_order")

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
            await self._handle_error(update, exc=e, operation="cancel_order_confirm_yes")

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

            # Return to order details (pass the id explicitly — never mutate
            # the immutable CallbackQuery.data).
            await self.order_details(update, context, order_id=order_id)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_order_confirm_no")

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
            await self._handle_error(update, exc=e, operation="track_order")

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
                    await self._edit_or_replace_callback_message(
                        update.callback_query, add_address_text, reply_markup=keyboard,
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

            # Quick Order auto-selected an address (last order's or default).
            # Read it before deciding whether to show a picker.
            quick_order_address_id = context.user_data.get('quick_order_address_id')
            checkout_source = context.user_data.get('checkout_source', 'cart')

            # Quick Order semantics (per UX requirements):
            #   * Use the implicit address (from prior order / default).
            #   * If user has exactly 1 address total, skip confirmation
            #     entirely and go straight to payment.
            #   * If user has multiple addresses, show a confirmation card so
            #     they can verify or change the auto-selected one.
            if checkout_source == 'quick_order' and quick_order_address_id:
                # Pick the auto-selected address (preferring the implicit one
                # if it still exists; otherwise fall back to the first).
                selected_address = next(
                    (a for a in addresses if a.get('id') == quick_order_address_id),
                    addresses[0],
                )
                if len(addresses) == 1:
                    # Single address — no confirmation, go directly to payment.
                    await self._show_payment_picker(update, context, selected_address['id'])
                    return
                # Multiple addresses — show confirmation with the auto-selected
                # one. Back button returns to the products menu (origin of the
                # Quick Order flow), not to the (probably empty) cart view.
                await self._show_address_confirmation(
                    update, context, selected_address, language,
                    back_callback='menu_products',
                )
                return

            # Regular checkout-from-cart flow.
            if len(addresses) == 1:
                await self._show_address_confirmation(
                    update, context, addresses[0], language,
                    back_callback='back_to_cart',
                )
                return

            # Multi-address picker (regular flow).
            address_text = i18n.get('telegram.orders.select_address', language)
            keyboard = OrderKeyboards.delivery_addresses(addresses, language)

            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query, address_text, reply_markup=keyboard,
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=address_text,
                    reply_markup=keyboard
                )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="checkout_handler")

    async def address_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address selection — extract id then defer to _show_payment_picker."""
        try:
            query = update.callback_query
            address_id = int(query.data.split('_')[1])
            await self._show_payment_picker(update, context, address_id)
        except Exception as e:
            await self._handle_error(update, exc=e, operation="address_handler")

    async def _show_address_confirmation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        address: Dict[str, Any],
        language: str,
        *,
        back_callback: str,
    ) -> None:
        """Render the 'Delivering to: …' confirmation step.

        Shown either when the user has only one saved address (single-address
        auto-skip) or when a Quick Order auto-selected one of several
        addresses. `back_callback` controls where Back navigates — for the
        cart-driven flow it's 'back_to_cart'; for Quick Order it's the
        products menu, since that's the screen the user actually came from.
        """
        title = address.get('title') or i18n.get('telegram.address.default_title', language)
        full_address = address.get('full_address') or ''
        label = i18n.get('telegram.checkout.delivering_to', language)
        address_text = f"{label}\n*{title}*"
        if full_address:
            address_text += f"\n{full_address}"
        keyboard = OrderKeyboards.single_address_confirm(
            address, language, back_callback=back_callback,
        )

        if update.callback_query:
            await self._edit_or_replace_callback_message(
                update.callback_query, address_text,
                reply_markup=keyboard, parse_mode='Markdown',
            )
            await update.callback_query.answer()
        else:
            await update.message.reply_text(
                text=address_text, reply_markup=keyboard,
                parse_mode='Markdown',
            )

    async def _show_payment_picker(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        address_id: int,
    ) -> None:
        """Render the payment-method picker for the given address_id.

        Used by both the regular address_handler (user tapped an address) and
        the Quick Order flow (address auto-selected from prior order). Stores
        selected_address_id in context so confirm_order can read it later.
        """
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)

        # Persist the selected address + its display info for downstream steps.
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

        # Pre-select the business-account default (Plan 3): if the API flags a
        # default method for this cart, seed it so the customer sees it chosen.
        # A manual tap still overrides via payment_handler.
        default_method = next(
            (
                str(m.get('method'))
                for m in payment_payload.get('available_methods', [])
                if m.get('is_default')
            ),
            None,
        )
        if default_method:
            context.user_data['selected_payment_method'] = default_method

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
            payment_text += "\n\n" + self._cod_restriction_notice(restrictions, language)
        keyboard = OrderKeyboards.payment_methods(payment_methods, language)

        query = update.callback_query
        if query:
            await self._edit_or_replace_callback_message(
                query, payment_text, reply_markup=keyboard,
            )
            await query.answer()
        else:
            await update.message.reply_text(payment_text, reply_markup=keyboard)

    async def payment_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle payment method selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract payment method
            payment_method = query.data.split('_', 1)[1]

            # Store payment method
            context.user_data['selected_payment_method'] = payment_method

            # Show order confirmation
            await self._show_order_confirmation(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="payment_handler")

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
            context.user_data.pop('checkout_source', None)
            context.user_data.pop('quick_order_address_id', None)
            context.user_data.pop('selected_reward_id', None)
            context.user_data.pop('cart_edit_return', None)

            await query.edit_message_text(
                text=i18n.get('telegram.action_cancelled', language),
                reply_markup=await main_menu_for(update.effective_user.id, language),
            )
            await query.answer(i18n.get('telegram.action_cancelled_short', language))

            logger.info(f"Checkout cancelled by user {user_id}")
        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_checkout")

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

                # Apply a loyalty reward selected via the loyalty rewards menu
                # (Phase 3 apply-at-checkout flow). Backend CreateOrderRequest
                # accepts an optional reward_id and applies it during creation.
                #
                # Consume the selection at read time (pop, not get) so it is
                # cleared once per checkout attempt regardless of outcome. If we
                # only cleared it on the success path, a failure path (503 Tax
                # Committee, generic API error, outer except) would leak the
                # selection into the user's next order and silently re-apply the
                # reward.
                selected_reward_id = context.user_data.pop('selected_reward_id', None)
                if selected_reward_id:
                    order_data['reward_id'] = selected_reward_id

                response = await client.create_order(user_token, order_data)
                if not response.success:
                    # Tax Committee unavailable — show dedicated error with options
                    if response.status_code == 503:
                        # Stash the cancelled order_id so the cash-rescue button
                        # can revive THIS order rather than rebuilding from cart.
                        # Backend returns it under data.cancelled_order_id.
                        try:
                            cancelled_order_id = (response.data or {}).get('data', {}).get('cancelled_order_id')
                        except AttributeError:
                            cancelled_order_id = None
                        if cancelled_order_id:
                            context.user_data['psp_failed_order_id'] = int(cancelled_order_id)
                        error_text = i18n.get('telegram.orders.asl_belgisi_error_message', language)
                        keyboard = OrderKeyboards.asl_belgisi_error(language)
                        await query.edit_message_text(text=error_text, reply_markup=keyboard)
                        await query.answer()
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
                    order_number = order.get('order_number')

                    # Three distinct cases — log each at the right level so the
                    # ops dashboard isn't drowned in INFO noise from the fast
                    # path and so a real clock-skew incident actually surfaces:
                    #
                    #   1. remaining > 0          — slow path, we'll actually wait
                    #   2. -CLOCK_SKEW_WARN_THRESHOLD_SECONDS <= remaining <= 0
                    #                              — fast path (proactive pool
                    #                                covered it), normal jitter
                    #   3. remaining < -threshold — clock skew between backend
                    #                                and bot worth investigating
                    CLOCK_SKEW_WARN_THRESHOLD_SECONDS = 5.0

                    if remaining > 0:
                        needed_wait = True
                        logger.info(
                            "Payment for order %s ready at %s, waiting %.2fs before showing link to user %s",
                            order_number, payment_ready_at_str, remaining, user_id,
                        )
                        preparing_text = i18n.get('telegram.orders.preparing_payment_message', language).format(
                            order_number=order.get('order_number', str(order['id']))
                        )
                        await query.edit_message_text(text=preparing_text)
                        await asyncio.sleep(remaining)
                    elif remaining < -CLOCK_SKEW_WARN_THRESHOLD_SECONDS:
                        # Negative wait of more than a few seconds isn't normal
                        # PSP-coverage timing — it points to backend/bot clock
                        # drift (NTP failure, container time skew, etc). Fire a
                        # WARN so it's visible without blocking the order flow.
                        logger.warning(
                            "Clock skew suspected: payment_ready_at=%s is %.2fs in the past "
                            "for order %s (user %s). Check NTP on backend and bot containers.",
                            payment_ready_at_str, -remaining, order_number, user_id,
                        )
                    else:
                        # Fast path: proactive marking-code pool covered the
                        # order, payment_ready_at ≈ now, no wait needed. DEBUG
                        # so it doesn't drown the log under normal traffic.
                        logger.debug(
                            "Payment for order %s ready immediately (proactive pool path, remaining=%.2fs) for user %s",
                            order_number, remaining, user_id,
                        )

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

                # Don't clear context data - needed for payment flow.
                # But the edit-cart return flag is no longer needed once order is placed.
                context.user_data.pop('cart_edit_return', None)
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
                    order_total=order.get('total_amount', 0),
                    language=language,
                )

            keyboard = await main_menu_for(update.effective_user.id, language)

            await query.edit_message_text(
                text=success_text,
                reply_markup=keyboard
            )
            await query.answer(i18n.get('telegram.orders.placed_success', language))

            # Clear order data
            context.user_data.clear()

            logger.info(f"Order created successfully for user {user_id} with payment method: {payment_method}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="confirm_order")

    async def select_payment_cash(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Rescue flow: clone the PSP-cancelled order as a fresh cash order.

        Triggered when the user taps "Pay with cash" on the Asl belgisi error
        screen. The bot stashed the cancelled order_id on the 503 response;
        here we call the dedicated rescue endpoint which creates a new cash
        order from the cancelled order's items, bypassing the COD active-
        debt cap.

        If the stash is missing (e.g. the user re-entered from a stale
        screen) we fall back to recreating from cart via confirm_order. That
        path still respects the COD cap, but at least the button is not
        silently dead.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            query = update.callback_query

            # Use get() not pop() — if the rescue API call fails we want the
            # stash to survive so the user can tap "try again" without us
            # silently degrading to the COD-blocked fallback path.
            order_id = context.user_data.get('psp_failed_order_id')
            if not order_id:
                context.user_data['selected_payment_method'] = 'cash'
                await self.confirm_order(update, context)
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.retry_order_with_cash(user_token, order_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                order = (response.data or {}).get('data', {}).get('order') or {}

            # Rescue succeeded — clear the stash and any other checkout state.
            for key in (
                'psp_failed_order_id', 'selected_address_id', 'selected_address_title',
                'selected_address_full', 'selected_payment_method', 'checkout_addresses',
                'checkout_source', 'quick_order_address_id', 'selected_reward_id',
            ):
                context.user_data.pop(key, None)

            success_text = i18n.get('telegram.orders.placed_success', language) + "\n\n"
            success_text += MessageBuilder.build_order_summary(order, language)
            success_text += "\n\n" + i18n.get('telegram.orders.cash_note', language)

            keyboard = await main_menu_for(update.effective_user.id, language)
            await self._edit_or_replace_callback_message(
                query, success_text, reply_markup=keyboard,
            )
            await query.answer(i18n.get('telegram.orders.placed_success', language))

            logger.info(f"Order {order_id} rescued to cash for user {user_id}")
        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_payment_cash")

    async def _show_order_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show order confirmation screen"""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)
        unknown_text = i18n.get('telegram.common.unknown', language)

        # Build confirmation message
        confirmation_text = i18n.get('telegram.orders.confirmation_title', language) + "\n\n"

        # Get cart items from API by api_client.get_cart and show them
        cart_total_amount = 0
        min_qty_violations: list = []
        selected_reward = None
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
                product_payload = item.get('product') or {}
                quantity = item.get('quantity', 1)
                confirmation_text += f"• {product_payload.get('name', unknown_text)} x{quantity}\n"
                item_subtotal_price = product_payload.get('current_price', 0) * quantity
                cart_total_amount += item_subtotal_price
                confirmation_text += f"  💰 {format_price(item_subtotal_price)} UZS\n\n"

                # Per-product purchase minimum (mirrors backend rule).
                inventory = product_payload.get('inventory') or {}
                min_qty = int(inventory.get('min_order_quantity', 1) or 1)
                if quantity < min_qty:
                    min_qty_violations.append({
                        'name': product_payload.get('name', unknown_text),
                        'min_qty': min_qty,
                        'remaining': min_qty - quantity,
                    })

            # A loyalty reward chosen from the rewards menu is applied server-side
            # at order creation; fetch it here to preview it on this screen.
            selected_reward_id = context.user_data.get('selected_reward_id')
            if selected_reward_id:
                rewards_resp = await client.get_loyalty_rewards(user_token)
                if rewards_resp.success:
                    _all_rewards = (rewards_resp.data or {}).get('data', {}).get('rewards', [])
                    selected_reward = next(
                        (r for r in _all_rewards if r.get('id') == selected_reward_id), None
                    )

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
                'business_account': i18n.get('telegram.payment_business_account', language),
            }
            payment_method_label = payment_method_labels.get(
                payment_method,
                i18n.get('telegram.common.unknown', language)
            )
            confirmation_text += f"{i18n.get('telegram.orders.payment_info', language)}: {payment_method_label}\n\n"

        # Selected reward preview + its effect on the grand total. The backend
        # (LoyaltyService.apply_reward_to_order) is authoritative; this mirrors the
        # simple discount rule so the confirm screen matches the placed order. A
        # discount only applies once the order meets the reward's min order value.
        reward_discount = 0.0
        if selected_reward:
            reward_name = selected_reward.get('name') or i18n.get('telegram.loyalty.reward_fallback', language)
            reward_type = selected_reward.get('reward_type')
            min_order_value = float(selected_reward.get('min_order_value') or 0)
            reward_label = i18n.get('telegram.loyalty.reward_applied', language)
            if reward_type == 'discount' and cart_total_amount >= min_order_value:
                discount_type = selected_reward.get('discount_type') or 'fixed'
                discount_value = float(selected_reward.get('discount_value') or 0)
                raw = (cart_total_amount * discount_value / 100.0) if discount_type == 'percentage' else discount_value
                reward_discount = min(round(raw, 2), float(cart_total_amount))
                confirmation_text += f"🎁 {reward_label}: {reward_name}\n   −{format_price(reward_discount)} UZS\n\n"
            elif reward_type == 'free_product':
                free_qty = int(selected_reward.get('free_product_quantity') or 1)
                free_suffix = i18n.get('telegram.loyalty.free_suffix', language)
                confirmation_text += f"🎁 {reward_label}: {reward_name} ({free_qty}× {free_suffix})\n\n"
            else:
                confirmation_text += f"🎁 {reward_label}: {reward_name}\n\n"

        # Add total amount
        grand_total_amount = max(0.0, float(cart_total_amount) - reward_discount)
        confirmation_text += f"💰 {i18n.get('telegram.total', language)}: {format_price(cart_total_amount)} UZS\n"
        confirmation_text += f"🚚 {i18n.get('telegram.orders.delivery_fee', language, amount=0)}\n"
        confirmation_text += "────────────────\n"
        confirmation_text += f"💳 {i18n.get('telegram.orders.grand_total', language, amount=format_price(grand_total_amount))}"

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
                confirmation_text += i18n.get(
                    'telegram.orders.cod_prepaid_balance',
                    language,
                    available_balance=format_price(available_balance),
                ) + "\n"
                confirmation_text += i18n.get(
                    'telegram.orders.cod_prepaid_auto_applied',
                    language,
                    potential_applied=format_price(potential_applied),
                ) + "\n"
                confirmation_text += i18n.get(
                    'telegram.orders.cod_estimated_payable',
                    language,
                    payable_after=format_price(payable_after),
                )

        # Block confirm if per-product or order-level minimum isn't met.
        # MIN_ORDER_AMOUNT comes from shared.business_config (same env-driven SSOT
        # the backend reads) so the bot mirrors backend validation up-front.
        amount_met = cart_total_amount >= MIN_ORDER_AMOUNT
        qty_met = not min_qty_violations
        meets_minimum = amount_met and qty_met

        if not meets_minimum:
            confirmation_text += "\n\n────────────────\n"
            if not amount_met:
                remaining_amount = MIN_ORDER_AMOUNT - cart_total_amount
                confirmation_text += "⚠️ " + i18n.get(
                    'telegram.cart_min_order_warning', language,
                    min_amount=format_price(MIN_ORDER_AMOUNT),
                    remaining=format_price(remaining_amount),
                ) + "\n"
            for v in min_qty_violations:
                confirmation_text += "⚠️ " + i18n.get(
                    'telegram.cart_min_qty_warning', language,
                    product_name=v['name'],
                    min_qty=v['min_qty'],
                    remaining=v['remaining'],
                ) + "\n"

        import eligibility
        show_reward = await eligibility.is_loyalty_eligible(update.effective_user.id)
        keyboard = OrderKeyboards.order_confirmation(
            language,
            meets_minimum=meets_minimum,
            has_reward=bool(context.user_data.get('selected_reward_id')),
            show_reward=show_reward,
        )

        await update.callback_query.edit_message_text(
            text=confirmation_text,
            reply_markup=keyboard
        )
        await update.callback_query.answer()

    async def back_to_order_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle back button from payment screen to order confirmation"""
        try:
            # The user is back at the confirmation screen — editing is done.
            # Clear the flag so a subsequent cart tap renders normal (non-edit) mode.
            context.user_data.pop('cart_edit_return', None)
            await self._show_order_confirmation(update, context)
        except Exception as e:
            await self._handle_error(update, exc=e, operation="back_to_order_confirm")

    async def edit_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open the cart in edit mode from the order-confirmation 'Edit' button.

        Repointed from checkout_handler (Deliverable B): instead of restarting
        checkout, render the existing server-cart summary with per-item +/- and
        remove controls. We stash where 'Done' should return to so the cart
        keyboard can route back to the confirmation screen (which re-fetches the
        live cart, so edits are reflected). selected_address_id / payment /
        reward are NOT cleared here, so they survive the round-trip.
        """
        try:
            context.user_data['cart_edit_return'] = 'order_confirm'
            await product_handlers.show_cart(update, context, edit_mode=True)
        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_cart")

    async def back_to_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Back button from the confirmation card to the payment-method picker.

        Re-renders the payment picker for the address already chosen this
        checkout. The selected reward (selected_reward_id) is intentionally left
        untouched so the user keeps it when they return to confirmation.
        """
        try:
            await self._show_payment_picker(
                update, context, context.user_data['selected_address_id']
            )
        except Exception as e:
            await self._handle_error(update, exc=e, operation="back_to_payment")

    async def checkout_choose_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show a picker of loyalty rewards that can be applied to this order.

        Only rewards the user can actually redeem (``can_redeem``) AND that meet
        this order's subtotal vs. the reward's ``min_order_value`` are offered, so
        the backend never rejects the selection at order creation. Tapping one
        routes to checkout_apply_reward; Back returns to the confirmation screen.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                cart_response = await client.get_cart(user_token)
                if not cart_response.success:
                    await self._handle_api_error(update, cart_response.error, language)
                    return
                cart = (cart_response.data or {}).get('data', {}).get('cart') or {}
                subtotal = sum(
                    (item.get('product') or {}).get('current_price', 0) * item.get('quantity', 1)
                    for item in cart.get('cart_items', [])
                )

                rewards_response = await client.get_loyalty_rewards(user_token)
                rewards = (
                    (rewards_response.data or {}).get('data', {}).get('rewards', [])
                    if rewards_response.success else []
                )

            # Keep the FULL catalog: affordable + in-budget rewards become
            # tappable buttons; everything else is listed as a locked text line
            # explaining the shortfall (coins or min order value).
            balance = (
                (rewards_response.data or {}).get('data', {}).get('user_points_balance', 0)
                if rewards_response.success else 0
            )
            points_unit = i18n.get('telegram.loyalty.points_unit', language)

            affordable = []
            text = f"🎁 {i18n.get('telegram.loyalty.choose_reward_title', language)}\n\n"
            text += i18n.get('telegram.loyalty.balance_header', language, points=balance) + "\n\n"

            if not rewards:
                text += i18n.get('telegram.loyalty.no_rewards_for_order', language)
            else:
                for reward in rewards:
                    name = reward.get('name') or i18n.get('telegram.loyalty.reward_fallback', language)
                    cost = reward.get('points_cost', 0)
                    min_order = float(reward.get('min_order_value') or 0)
                    meets_min = min_order <= float(subtotal)
                    if reward.get('can_redeem') and meets_min:
                        affordable.append(reward)
                        text += f"🎁 {name} — {cost} {points_unit}\n"
                    elif not reward.get('can_redeem'):
                        shortfall = reward.get('points_needed') or max(0, cost - balance)
                        lock = i18n.get('telegram.loyalty.lock_need_coins', language, points=shortfall)
                        text += f"🔒 {name} — {cost} {points_unit} ({lock})\n"
                    else:
                        add_amount = int(min_order - float(subtotal))
                        lock = i18n.get('telegram.loyalty.lock_add_order', language, amount=add_amount)
                        text += f"🔒 {name} — {cost} {points_unit} ({lock})\n"

            keyboard = OrderKeyboards.checkout_reward_picker(affordable, language)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()
        except Exception as e:
            await self._handle_error(update, exc=e, operation="checkout_choose_reward")

    async def checkout_apply_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store the chosen reward and re-render the confirmation (with its preview)."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            reward_id = int(query.data.rsplit('_', 1)[1])
            context.user_data['selected_reward_id'] = reward_id
            await self._show_order_confirmation(update, context)
        except Exception as e:
            await self._handle_error(update, exc=e, operation="checkout_apply_reward")

    async def checkout_remove_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear the selected reward and re-render the confirmation screen."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            context.user_data.pop('selected_reward_id', None)
            await query.answer(i18n.get('telegram.loyalty.reward_removed', language))
            await self._show_order_confirmation(update, context)
        except Exception as e:
            await self._handle_error(update, exc=e, operation="checkout_remove_reward")


# Global handler instance
order_handlers = OrderHandlers()
