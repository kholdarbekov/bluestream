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

    # The key `_with_display_price` stamps the SERVER's client-scoped quote onto
    # each catalogue product under. Kept distinct from `price` so the two can
    # never be confused: `price` is what the keyboard renders, this is where it
    # came from.
    CLIENT_QUOTE_KEY = 'client_unit_price'

    @staticmethod
    def _display_unit_price(product: dict) -> float:
        """🔴 THE ONE PLACE A PRODUCT PRICE IS READ IN THE OPERATOR FLOW.

        ``serialize_product`` publishes price NESTED under ``pricing``
        (``business_app/serializers/product_serializers.py:366-377``); there is
        no top-level ``price`` key on the payload at all. Three screens each
        read one independently — the detail line, the cart line and
        ``OperatorKeyboards.product_list`` — and every one of them read
        ``.get('price', 0)``, which on that payload can only ever return **0**.
        Operators quoted 0 UZS down the phone while
        ``StaffService.create_phone_order`` charged the real price. Three reads
        is how one of them ended up on a key that does not exist, so resolve
        here and let everything downstream read the result.

        The candidate order mirrors the customer bot's established SSOT
        (``telegram_bot/handlers/products.py:_get_effective_unit_price`` and
        ``telegram_bot/keyboards.py:get_product_display_price``) so the two bots
        cannot drift apart on payload shape. ``price`` is last, for an already
        normalised dict (see :meth:`_with_display_price`) and for any legacy
        payload that really does carry one.

        ``client_unit_price`` is FIRST and outranks everything, because it is the
        only candidate priced for the CUSTOMER. ``/api/v1/products/`` prices for
        the CALLER (``business_app/api/products.py:100-111``), and here the
        caller is the OPERATOR: a corporate-contract client was quoted the
        generic 45 000 and charged the contract 27 000, and a VIP/tiered operator
        leaked their own discount into the quote. The client-scoped figure comes
        from ``POST /staff/operator/users/<id>/order-estimate``, which replays
        the very loop ``StaffService.create_phone_order`` charges from
        (:meth:`_client_unit_prices`). The catalogue candidates below survive
        only as the 0-UZS guard they were written as — they must never be what a
        screen quotes when a client is known.
        """
        pricing = (product or {}).get('pricing') or {}
        for candidate in (
            (product or {}).get(CreateOrderHandler.CLIENT_QUOTE_KEY),
            pricing.get('current_price'),
            (product or {}).get('current_price'),
            pricing.get('base_price'),
            (product or {}).get('base_price'),
            (product or {}).get('price'),
        ):
            if candidate is None:
                continue
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
        return 0.0

    @classmethod
    def _with_display_price(cls, products: list, client_unit_prices: dict = None) -> list:
        """Stamp the client-scoped unit price onto each product as ``price``.

        ``OperatorKeyboards.product_list`` renders ``product.get('price', 0)``
        into every button label. Normalising here means the keyboard, the detail
        screen and the cart all render the SAME resolved number without three
        modules each re-deriving it from the nested payload.

        ``client_unit_prices`` maps ``product_id -> unit_price`` and comes off
        the order-estimate endpoint, i.e. off the loop that will CHARGE this
        client. It is stamped under :attr:`CLIENT_QUOTE_KEY`, which
        :meth:`_display_unit_price` reads before any catalogue field, so a
        contract client's screen states the contract price.
        """
        quotes = client_unit_prices or {}
        stamped = []
        for product in (products or []):
            row = {**(product or {})}
            quoted = quotes.get(row.get('id'))
            if quoted is not None:
                row[cls.CLIENT_QUOTE_KEY] = quoted
            row['price'] = cls._display_unit_price(row)
            stamped.append(row)
        return stamped

    async def _client_unit_prices(self, token: str, client_id, products: list):
        """Ask the server what THIS CLIENT pays per unit, for the whole catalogue.

        Returns ``(prices_by_product_id, error_response_or_None)``. On failure it
        returns NO prices rather than the catalogue's own — falling back to
        operator-scoped money is the defect this endpoint exists to remove, and a
        plausible wrong number read down the phone with confidence is worse than
        an error the operator can see.
        """
        product_ids = [p.get('id') for p in (products or []) if (p or {}).get('id') is not None]
        if not product_ids or not client_id:
            return {}, None

        # Quantity is irrelevant to the unit price (`Product.calculate_price`
        # ignores it, and contract rows are per-unit), so 1 is the honest
        # per-unit question to ask for a button label.
        async with api_client as client:
            response = await client.get_operator_order_estimate(
                token, client_id,
                [{'product_id': pid, 'quantity': 1} for pid in product_ids],
            )

        if not response.success:
            return {}, response

        lines = (response.data or {}).get('items') or []
        return {line.get('product_id'): line.get('unit_price') for line in lines}, None

    async def _quote_cart(self, context, token):
        """Re-price the CART server-side and store the quote for the screens.

        Every money figure the operator reads out — each line total and the
        subtotal — comes from this one response, so there is nothing left on
        this screen for a second expression to disagree with. Returns the failed
        ``APIResponse`` when the quote could not be obtained, in which case
        NOTHING is stored: a stale quote beside a changed basket is the same
        defect wearing a different hat.
        """
        order_data = context.user_data.get('new_order', {})
        items = order_data.get('items') or []
        if not items:
            order_data.pop('estimate', None)
            return None

        async with api_client as client:
            response = await client.get_operator_order_estimate(
                token,
                order_data.get('client_id'),
                [{'product_id': i['product_id'], 'quantity': i['quantity']} for i in items],
            )

        if not response.success:
            return response

        order_data['estimate'] = response.data or {}
        return None

    @staticmethod
    def _cod_restriction_notice(restrictions: dict, language: str) -> str:
        """Copy for a blocked COD order — naming the arm that actually fired.

        The cap has two arms (spec 5.5): the customer's own linked cluster is at
        the limit (``restriction_scope == 'person'``), or the grouped WORKPLACE
        this order ships to is (``'place'`` — a COWORKER's unpaid orders). The
        operator reads this text out to the customer on the phone, so telling
        someone with a clean personal record "you have unpaid orders" is simply
        false. Branch on the discriminator, exactly as the customer bot does
        (telegram_bot/handlers/orders.py).

        Only a COUNT crosses over — never a coworker's name or phone (spec 7).
        Payloads without a scope (legacy, or no delivery address supplied) keep
        today's copy, so unlinked + ungrouped customers are unaffected.
        """
        if (restrictions or {}).get('restriction_scope') == 'place':
            return i18n.get(
                'staff.operator.cod_restricted_place',
                language,
                place_active_cod_debt_count=(restrictions.get('place_active_cod_debt_count') or 0),
            )
        return i18n.get('staff.operator.cod_restricted', language)

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
                        f"➕ {i18n.get('staff.operator.add_address', language)}",
                        callback_data=f"staff_op_add_addr_{client_id}"
                    )],
                    [InlineKeyboardButton(
                        f"⬅️ {i18n.get('staff.back', language)}",
                        callback_data="staff_back_to_main"
                    )]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
                # SELECT_ADDRESS, not None. This is an ENTRY POINT, and PTB's
                # `_update_state` IGNORES a None return — so the conversation
                # was never entered on this branch and `staff_op_addr_<id>`,
                # registered only inside SELECT_ADDRESS, had no handler. The
                # operator added the client's first address mid-call, was shown
                # the picker `confirm_address` re-renders, tapped it, and got
                # nothing at all. The order genuinely IS at the address step
                # here — there just are not any addresses yet.
                return SELECT_ADDRESS

            text = i18n.get('staff.operator.select_address', language)
            keyboard = OperatorKeyboards.address_list(language, addresses, client_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return SELECT_ADDRESS

        except Exception as e:
            logger.error(f"Error starting order for client: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_operator
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

    @require_auth
    @require_operator
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

            # Resolve the price ONCE, here, before anything renders it — and
            # resolve it FOR THE CLIENT, not for the operator holding the token.
            client_prices, quote_error = await self._client_unit_prices(
                token, context.user_data.get('new_order', {}).get('client_id'), products,
            )
            if quote_error is not None:
                await self._handle_api_response_error(update, quote_error, language)
                return
            products = self._with_display_price(products, client_prices)

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

    @require_auth
    @require_operator
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
                f"📦 <b>{escape_html(product.get('name', ''))}</b>\n"
                f"💰 {format_currency(self._display_unit_price(product), language=language)}\n\n"
                f"{i18n.get('staff.operator.select_quantity', language)}"
            )

            keyboard = OperatorKeyboards.quantity_selection(language, product_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            return SELECT_QUANTITY

        except Exception as e:
            logger.error(f"Error selecting product: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_operator
    async def select_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quantity selection - add to cart"""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

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
                'price': self._display_unit_price(product),
            })
            context.user_data['new_order'] = order_data

            # Re-price the whole basket server-side. The basket and its quote
            # move together or not at all — on failure the line just added is
            # rolled back, so the screen never states a total for a basket the
            # server has not priced.
            quote_error = await self._quote_cart(context, token)
            if quote_error is not None:
                # `_quote_cart` stores nothing on failure, so popping the new
                # line leaves the previous basket beside its own valid quote.
                order_data['items'].pop()
                await self._handle_api_response_error(update, quote_error, language)
                return SELECT_PRODUCTS

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

    @require_auth
    @require_operator
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
        # The destination address is already chosen at this point (SELECT_ADDRESS
        # precedes SELECT_PRODUCTS), so pass it: the backend then evaluates the
        # COD cap's PLACE arm too and the restriction copy below can name the
        # workplace that actually caused a block instead of blaming the customer.
        async with api_client as client:
            response = await client.get_operator_payment_methods(
                token, client_id,
                delivery_address_id=order_data.get('delivery_address_id'),
            )

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

        default_method = next(
            (m.get('method') for m in available_methods if m.get('is_default')),
            None,
        )
        if default_method:
            context.user_data['new_order']['payment_method'] = default_method

        text = self._format_cart_summary(context, language)
        if restrictions.get('cod_restricted'):
            text += f"\n\n⚠️ {self._cod_restriction_notice(restrictions, language)}"
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

    @require_auth
    @require_operator
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

    @require_auth
    @require_operator
    async def receive_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive delivery notes"""
        language = await self._get_language(update, context)
        notes = update.message.text.strip()
        context.user_data['new_order']['delivery_notes'] = notes

        await self._show_order_summary(update, context, language)
        return CONFIRM_ORDER

    @require_auth
    @require_operator
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
        text += f"\n💳 {payment_label}"

        restrictions = order_data.get('payment_restrictions') or {}
        if restrictions.get('cod_restricted'):
            text += f"\n⚠️ {self._cod_restriction_notice(restrictions, language)}"

        notes = order_data.get('delivery_notes')
        if notes:
            text += f"\n💬 {escape_html(notes)}"

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

    @require_auth
    @require_operator
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
                f"✅ {i18n.get('staff.operator.order_created', language, order_number=order_number)}",
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
        """Format current cart as text summary.

        🔴 THIS SCREEN COMPUTES NO MONEY. Every line total and the subtotal are
        read straight off the server's quote (``_quote_cart`` ->
        ``POST /staff/operator/users/<id>/order-estimate`` ->
        ``StaffService.price_phone_order``) — the same call
        ``create_phone_order`` prices the real order with. It used to multiply
        ``price * qty`` and accumulate its own subtotal from an
        OPERATOR-scoped catalogue price; that is how 45 000 got read down the
        phone for an order that charged 27 000. Do not reintroduce arithmetic
        here: if a figure is missing from the quote, the fix is to publish it
        from the endpoint, not to derive it on the screen.
        """
        order_data = context.user_data.get('new_order', {})
        items = order_data.get('items', [])

        if not items:
            return f"🛒 {i18n.get('staff.operator.cart_empty', language)}"

        quoted_lines = (order_data.get('estimate') or {}).get('items') or []
        if len(quoted_lines) != len(items):
            # The basket moved without its quote. Refuse to state a number
            # rather than invent one; every cart mutation re-quotes or rolls
            # back, so this is a bug report, not a routine branch.
            logger.error(
                "Operator cart has %s lines but the server quote has %s — refusing to state money",
                len(items), len(quoted_lines),
            )
            return f"🛒 {i18n.get('staff.error_occurred', language)}"

        lines = [f"🛒 <b>{i18n.get('staff.operator.cart', language)}</b>\n"]

        for item, quoted in zip(items, quoted_lines):
            name = escape_html(item.get('name') or quoted.get('product_name') or '')
            qty = quoted.get('quantity', 1)
            lines.append(
                f"  • {name} x{qty} — "
                f"{format_currency(quoted.get('total_price'), language=language)}"
            )

        lines.append(
            f"\n💰 {i18n.get('staff.operator.subtotal', language)}: "
            f"{format_currency((order_data.get('estimate') or {}).get('subtotal'), language=language)}"
        )

        return '\n'.join(lines)

    @require_auth
    @require_operator
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
