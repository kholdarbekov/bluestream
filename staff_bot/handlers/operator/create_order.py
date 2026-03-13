"""
Create Order Handler for Staff Bot
Allows operators to create orders on behalf of clients via conversation flow.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.keyboards.operator import OperatorKeyboards
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils.formatters import format_currency, escape_html
from staff_bot.utils.search import detect_search_type
from staff_bot.permissions import require_auth, require_operator
from staff_bot.i18n import i18n

logger = logging.getLogger(__name__)

# Conversation states
SELECT_CLIENT, SELECT_ADDRESS, SELECT_PRODUCTS, SELECT_QUANTITY, \
    SELECT_PAYMENT, ENTER_NOTES, CONFIRM_ORDER = range(30, 37)


class CreateOrderHandler(BaseHandler):
    """Handle order creation flow for operators"""

    @require_auth
    @require_operator
    async def start_create_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start order creation - search for client first"""
        language = await self._get_language(update, context)

        # Clear previous order data
        context.user_data.pop('new_order', None)
        context.user_data['new_order'] = {'items': []}

        text = i18n.get('staff.operator.order_enter_phone', language)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        else:
            await update.message.reply_text(text, parse_mode='HTML')

        return SELECT_CLIENT

    @require_auth
    @require_operator
    async def start_order_for_client(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start order creation for a specific client (from user_found keyboard)"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_op_order_{user_id}
            client_id = int(query.data.split('_')[-1])
            context.user_data['new_order'] = {'client_id': client_id, 'items': []}

            # Fetch client addresses
            async with api_client as client:
                response = await client.get_user_addresses(token, client_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            addresses = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not addresses:
                text = i18n.get('staff.operator.no_addresses', language)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"\u2795 {i18n.get('staff.operator.add_address', language)}",
                        callback_data=f"staff_op_add_addr_{client_id}"
                    )],
                    [InlineKeyboardButton(
                        f"\u2b05\ufe0f {i18n.get('staff.back', language)}",
                        callback_data="staff_back_to_main"
                    )]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                return

            text = i18n.get('staff.operator.select_address', language)
            keyboard = OperatorKeyboards.address_list(language, addresses, client_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return SELECT_ADDRESS

        except Exception as e:
            logger.error(f"Error starting order for client: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    async def receive_client_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Search for client by phone/name"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        query_text = update.message.text.strip()

        try:
            search_type = detect_search_type(query_text)
            async with api_client as client:
                response = await client.search_clients(
                    token, query_text, search_type=search_type
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return SELECT_CLIENT

            clients = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not clients:
                text = i18n.get(
                    'staff.operator.no_results',
                    language,
                    query=escape_html(query_text),
                )
                keyboard = OperatorKeyboards.user_not_found(language)
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
                return ConversationHandler.END

            # Show client results
            for client_user in clients[:5]:
                from staff_bot.utils.formatters import format_user_card
                card = format_user_card(client_user, language)
                keyboard = OperatorKeyboards.user_found(language, client_user['id'])
                await update.message.reply_text(
                    card, reply_markup=keyboard, parse_mode='HTML'
                )

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error searching for client: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    async def select_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle address selection"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_op_addr_{address_id}
            address_id = int(query.data.split('_')[-1])
            context.user_data['new_order']['delivery_address_id'] = address_id

            # Fetch products
            async with api_client as client:
                response = await client.get_products(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            products = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not products:
                await query.edit_message_text(
                    i18n.get('staff.operator.no_products', language),
                    reply_markup=CommonKeyboards.back_button(language)
                )
                return

            # Store products for reference
            context.user_data['available_products'] = {
                str(p.get('id')): p for p in products
            }

            text = self._format_cart_summary(context, language)
            text += f"\n\n{i18n.get('staff.operator.select_products', language)}"

            keyboard = OperatorKeyboards.product_list(language, products)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return SELECT_PRODUCTS

        except Exception as e:
            logger.error(f"Error selecting address: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    async def select_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle product selection - show quantity picker"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_op_product_{product_id}
            product_id = int(query.data.split('_')[-1])
            products = context.user_data.get('available_products', {})
            product = products.get(str(product_id))

            if not product:
                await query.answer(i18n.get('staff.error_occurred', language), show_alert=True)
                return

            context.user_data['selecting_product_id'] = product_id

            text = (
                f"\U0001f4e6 <b>{escape_html(product.get('name', ''))}</b>\n"
                f"\U0001f4b0 {format_currency(product.get('price', 0), language=language)}\n\n"
                f"{i18n.get('staff.operator.select_quantity', language)}"
            )

            keyboard = OperatorKeyboards.quantity_selection(language, product_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return SELECT_QUANTITY

        except Exception as e:
            logger.error(f"Error selecting product: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    async def select_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quantity selection - add to cart"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_op_qty_{product_id}_{qty}
            parts = query.data.split('_')
            product_id = int(parts[3])
            quantity = int(parts[4])

            products = context.user_data.get('available_products', {})
            product = products.get(str(product_id))
            if not product:
                return

            # Add to cart
            order_data = context.user_data.get('new_order', {'items': []})
            order_data['items'].append({
                'product_id': product_id,
                'quantity': quantity,
                'name': product.get('name', ''),
                'price': product.get('price', 0),
            })
            context.user_data['new_order'] = order_data

            # Show updated cart + product list
            text = self._format_cart_summary(context, language)
            text += f"\n\n{i18n.get('staff.operator.add_more_or_done', language)}"

            # Re-fetch product list
            all_products = list(products.values())
            keyboard = OperatorKeyboards.product_list(language, all_products)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return SELECT_PRODUCTS

        except Exception as e:
            logger.error(f"Error selecting quantity: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    async def products_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Done selecting products - move to payment selection"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        order_data = context.user_data.get('new_order', {})
        items = order_data.get('items', [])

        if not items:
            await query.answer(
                i18n.get('staff.operator.no_items_selected', language),
                show_alert=True
            )
            return

        client_id = order_data.get('client_id')
        async with api_client as client:
            response = await client.get_operator_payment_methods(token, client_id)

        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return SELECT_PRODUCTS

        payment_payload = response.data or {}
        available_methods = payment_payload.get('available_methods', [])
        restrictions = payment_payload.get('payment_restrictions', {})
        context.user_data['new_order']['payment_restrictions'] = restrictions
        context.user_data['new_order']['available_payment_methods'] = [
            method.get('method') for method in available_methods if method.get('method')
        ]

        text = self._format_cart_summary(context, language)
        if restrictions.get('cod_restricted'):
            text += (
                f"\n\n\u26a0\ufe0f "
                f"{i18n.get('staff.operator.cod_restricted', language)}"
            )
        text += f"\n\n{i18n.get('staff.operator.select_payment', language)}"

        if not available_methods:
            await query.edit_message_text(
                text,
                reply_markup=CommonKeyboards.back_button(language, "staff_back_to_main"),
                parse_mode='HTML',
            )
            return SELECT_PRODUCTS

        keyboard = OperatorKeyboards.payment_methods(language, available_methods)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return SELECT_PAYMENT

    async def select_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle payment method selection"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_op_pay_{method}
            prefix = "staff_op_pay_"
            if not query.data.startswith(prefix):
                await query.answer(i18n.get('staff.error_occurred', language), show_alert=True)
                return SELECT_PAYMENT

            method = query.data[len(prefix):]
            allowed_methods = set(context.user_data.get('new_order', {}).get('available_payment_methods') or [])
            if allowed_methods and method not in allowed_methods:
                await query.answer(
                    i18n.get('staff.operator.payment_unavailable', language),
                    show_alert=True,
                )
                return SELECT_PAYMENT
            context.user_data['new_order']['payment_method'] = method

            # Ask for delivery notes
            text = i18n.get('staff.operator.enter_notes', language)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    i18n.get('staff.operator.skip_notes', language),
                    callback_data="staff_op_skip_notes"
                )
            ]])

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return ENTER_NOTES

        except Exception as e:
            logger.error(f"Error selecting payment: {e}", exc_info=True)
            await self._handle_error(update, context)

    async def receive_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive delivery notes"""
        language = await self._get_language(update, context)
        notes = update.message.text.strip()
        context.user_data['new_order']['delivery_notes'] = notes

        await self._show_order_summary(update, context, language)
        return CONFIRM_ORDER

    async def skip_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Skip delivery notes"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        context.user_data['new_order']['delivery_notes'] = None
        await self._show_order_summary(update, context, language)
        return CONFIRM_ORDER

    async def _show_order_summary(self, update, context, language):
        """Show order summary for confirmation"""
        order_data = context.user_data.get('new_order', {})
        text = self._format_cart_summary(context, language)

        payment = order_data.get('payment_method', 'cash')
        payment_label = i18n.get(f'staff.operator.payment_{payment}', language)
        text += f"\n\U0001f4b3 {payment_label}"

        restrictions = order_data.get('payment_restrictions') or {}
        if restrictions.get('cod_restricted'):
            text += f"\n\u26a0\ufe0f {i18n.get('staff.operator.cod_restricted', language)}"

        notes = order_data.get('delivery_notes')
        if notes:
            text += f"\n\U0001f4ac {escape_html(notes)}"

        text += f"\n\n{i18n.get('staff.operator.confirm_order_prompt', language)}"

        keyboard = OperatorKeyboards.order_confirm(language)

        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

    async def confirm_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and create the order"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            order_data = context.user_data.get('new_order', {})

            # Build API request
            api_items = [
                {'product_id': item['product_id'], 'quantity': item['quantity']}
                for item in order_data.get('items', [])
            ]

            request_data = {
                'client_id': order_data.get('client_id'),
                'items': api_items,
                'delivery_address_id': order_data.get('delivery_address_id'),
                'payment_method': order_data.get('payment_method', 'cash'),
                'delivery_notes': order_data.get('delivery_notes'),
            }

            async with api_client as client:
                response = await client.create_order_for_client(token, request_data)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return CONFIRM_ORDER

            result = response.data or {}
            order_number = result.get('order_number') or i18n.get('staff.common.not_available', language)

            await query.edit_message_text(
                f"\u2705 {i18n.get('staff.operator.order_created', language, order_number=order_number)}",
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML'
            )

        except Exception as e:
            logger.error(f"Error creating order: {e}", exc_info=True)
            await self._handle_error(update, context)
            return CONFIRM_ORDER

        context.user_data.pop('new_order', None)
        context.user_data.pop('available_products', None)
        return ConversationHandler.END

    def _format_cart_summary(self, context, language):
        """Format current cart as text summary"""
        order_data = context.user_data.get('new_order', {})
        items = order_data.get('items', [])

        if not items:
            return f"\U0001f6d2 {i18n.get('staff.operator.cart_empty', language)}"

        lines = [f"\U0001f6d2 <b>{i18n.get('staff.operator.cart', language)}</b>\n"]
        subtotal = 0

        for item in items:
            name = escape_html(item.get('name', ''))
            qty = item.get('quantity', 1)
            price = item.get('price', 0)
            item_total = price * qty
            subtotal += item_total
            lines.append(f"  \u2022 {name} x{qty} \u2014 {format_currency(item_total, language=language)}")

        lines.append(
            f"\n\U0001f4b0 {i18n.get('staff.operator.subtotal', language)}: "
            f"{format_currency(subtotal, language=language)}"
        )

        return '\n'.join(lines)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel order creation"""
        context.user_data.pop('new_order', None)
        context.user_data.pop('available_products', None)
        language = await self._get_language(update, context)

        text = i18n.get('staff.cancelled', language)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, reply_markup=CommonKeyboards.back_button(language)
            )
        else:
            await update.message.reply_text(
                text, reply_markup=CommonKeyboards.back_button(language)
            )
        return ConversationHandler.END
