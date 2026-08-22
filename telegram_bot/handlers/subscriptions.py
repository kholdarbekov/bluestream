"""
Subscription management handlers with complete implementation
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from eligibility import main_menu_for
from i18n import i18n
from keyboards import SubscriptionKeyboards, MenuKeyboards, ProductKeyboards
from api_client import api_client
from utils import user_middleware, get_auth_token
from shared.constants import SUBSCRIPTION_STATUS_ICONS
# `BaseHandler._ack` is why every ack below goes through `self._ack`: answering
# a callback is cosmetic and must never abort the step. Every handler here works
# inside one `try` that returns ConversationHandler.END, so a late "query is too
# old" used to end the customer's subscription flow AFTER the change had already
# been sent to the backend.
from handlers.base import BaseHandler


# Conversation states for subscription creation
(SELECT_PRODUCTS, SELECT_QUANTITY, SELECT_FREQUENCY, SELECT_ADDRESS,
 SELECT_START_DATE, SELECT_PAYMENT, CONFIRM_SUBSCRIPTION) = range(7)

# Conversation states for item management
(ITEM_ACTION, ITEM_SELECT_PRODUCT, ITEM_SELECT_QUANTITY) = range(7, 10)


class SubscriptionHandlers(BaseHandler):
    """Subscription-related handlers with full functionality"""

    async def subscriptions_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user subscriptions"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_subscriptions(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                subscriptions = response.data.get("data", {}).get('items', [])

            subs_text = f"{i18n.get('telegram.subscription.title', language)}\n\n"
            if subscriptions:
                active_count = len([s for s in subscriptions if s.get('status') == 'active'])
                paused_count = len([s for s in subscriptions if s.get('status') == 'paused'])
                subs_text += f"✅ {i18n.get('telegram.subscription.active', language)}: {active_count}\n"
                if paused_count > 0:
                    subs_text += f"⏸️ {i18n.get('telegram.subscription.paused', language)}: {paused_count}\n"
            else:
                subs_text += i18n.get('telegram.subscription.no_subscriptions', language)

            keyboard = SubscriptionKeyboards.subscription_list(subscriptions, language)

            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query, text=subs_text, reply_markup=keyboard
                )
                await self._ack(update.callback_query)
            else:
                await update.message.reply_text(text=subs_text, reply_markup=keyboard)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscriptions_menu")

    async def subscription_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show subscription details with real API data"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            subscription_id = int(query.data.split('_')[1])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get subscription details
                response = await client.get_subscription(user_token, subscription_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                subscription = response.data.get('data', {}).get("subscription", {})

                # Get subscription items
                items_response = await client.get_subscription_items(user_token, subscription_id)
                items = items_response.data.get("data", {}).get('items', []) if items_response.success else []

            # Build details text
            status_emoji = SUBSCRIPTION_STATUS_ICONS.get(subscription.get('status'), '❓')
            status_key = f"subscription_status_{subscription.get('status')}"
            frequency_key = f"frequency_{subscription.get('delivery_frequency')}"

            details_text = f"🔄 {i18n.get('telegram.subscription.details_title', language)}\n\n"
            details_text += f"{status_emoji} {i18n.get('telegram.subscription.status', language)}: "
            details_text += f"{i18n.get(status_key, language)}\n"
            details_text += f"📅 {i18n.get('telegram.subscription.frequency', language)}: "
            details_text += f"{i18n.get(frequency_key, language)}\n"

            if subscription.get('next_delivery_date'):
                details_text += f"🚚 {i18n.get('telegram.subscription.next_delivery', language)}: "
                details_text += f"{subscription['next_delivery_date']}\n"

            if subscription.get('next_billing_date'):
                details_text += f"💳 {i18n.get('telegram.subscription.next_billing', language)}: "
                details_text += f"{subscription['next_billing_date']}\n"

            if items:
                details_text += f"\n📦 {i18n.get('telegram.subscription.items', language)}:\n"
                for item in items:
                    product_name = item.get('product', {}).get('name', i18n.get('telegram.common.unknown', language))
                    quantity = item.get('quantity', 1)
                    details_text += f"  • {product_name} x{quantity}\n"

            if subscription.get('billing_amount'):
                details_text += f"\n💰 {i18n.get('telegram.subscription.amount', language)}: "
                details_text += f"{subscription['billing_amount']} {i18n.get('telegram.currency.uzs', language)}\n"

            keyboard = SubscriptionKeyboards.subscription_actions(subscription_id, subscription['status'], language)

            await self._edit_or_replace_callback_message(query, text=details_text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscription_details")

    # ========== SUBSCRIPTION CREATION FLOW ==========

    async def create_subscription_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start subscription creation flow"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Initialize context
            context.user_data['subscription_creation'] = {
                'items': [],
                'frequency': None,
                'address_id': None,
                'start_date': None,
                'payment_method': None
            }

            # Check for templates
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                templates_response = await client.get_subscription_templates(user_token)
                if templates_response.success and templates_response.data.get('templates'):
                    # Show templates option
                    text = i18n.get('telegram.subscription.create_template_or_custom', language)
                    keyboard = SubscriptionKeyboards.subscription_creation_options(language)
                    await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
                    await self._ack(query)
                    return SELECT_PRODUCTS

            # No templates, go straight to product selection
            return await self.select_products(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="create_subscription_start")
            return ConversationHandler.END

    async def select_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select products for subscription"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            products = await self._fetch_and_display_products(
                update, context, language,
                header_key='telegram.subscription.select_products'
            )
            if products is None:
                return ConversationHandler.END

            return SELECT_QUANTITY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_products")
            return ConversationHandler.END

    async def select_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select quantity for chosen product"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            product_id = int(update.callback_query.data.split('_')[2])
            context.user_data['current_product_id'] = product_id

            await self._show_quantity_selector(update, language, 'telegram.subscription.select_quantity')
            return SELECT_FREQUENCY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_quantity")
            return ConversationHandler.END

    async def add_item_with_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add item with selected quantity to subscription and ask for more"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Save item with quantity
            quantity = int(query.data.split('_')[2])
            product_id = context.user_data.get('current_product_id')

            context.user_data['subscription_creation']['items'].append({
                'product_id': product_id,
                'quantity': quantity
            })

            # Show confirmation and options to add more or continue
            items_count = len(context.user_data['subscription_creation']['items'])
            text = f"✅ {i18n.get('telegram.subscription.item_added', language)}\n\n"
            text += f"{i18n.get('telegram.subscription.total_items', language)}: {items_count}\n\n"
            text += i18n.get('telegram.subscription.add_more_or_continue', language)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"➕ {i18n.get('telegram.subscription.add_more_items', language)}",
                    callback_data='sub_add_more_items'
                )],
                [InlineKeyboardButton(
                    f"✅ {i18n.get('telegram.continue', language)}",
                    callback_data='sub_items_done'
                )],
                [InlineKeyboardButton(
                    i18n.get('telegram.cancel', language),
                    callback_data='cancel_subscription_creation'
                )]
            ])

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return SELECT_FREQUENCY  # Reusing this state for the intermediate step

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_with_quantity")
            return ConversationHandler.END

    async def select_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select delivery address"""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)

            # Save frequency - parse from callback data (format: subscription_freq_daily)
            frequency = query.data.split('_')[2]

            # Map frequency to both billing_cycle and delivery_frequency
            # For subscriptions, typically these are the same
            context.user_data['subscription_creation']['billing_cycle'] = frequency
            context.user_data['subscription_creation']['delivery_frequency'] = frequency

            return await self._show_address_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_address")
            return ConversationHandler.END

    async def back_to_address_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Back from the payment-method screen of subscription creation.

        `SubscriptionKeyboards.payment_methods` has always rendered this
        button; nothing claimed it, so a customer part way through subscription
        checkout tapped Back and stayed on the payment screen. The frequency
        they already chose is still in `subscription_creation`, so only the
        address step is re-shown.
        """
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            return await self._show_address_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="back_to_address_selection")
            return ConversationHandler.END

    async def _show_address_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      language: str):
        """Render the address step of subscription creation. Returns the next state.

        Shared by the forward step (`select_address`) and the Back button
        (`back_to_address_selection`) so the screen a customer returns to is
        the screen they left, by construction.
        """
        try:
            query = update.callback_query

            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

                addresses = response.data.get("data", {}).get('addresses', [])

            if not addresses:
                text = i18n.get('telegram.subscription.no_addresses', language)
                keyboard = [[InlineKeyboardButton(
                    i18n.get('telegram.subscription.add_address', language),
                    callback_data='add_address'
                )]]
                await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
                await self._ack(query)
                return ConversationHandler.END

            text = i18n.get('telegram.subscription.select_address', language)
            keyboard = self._build_address_keyboard(addresses, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return SELECT_PAYMENT

        except Exception as e:
            await self._handle_error(update, exc=e, operation="show_address_selection")
            return ConversationHandler.END

    async def select_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select payment method"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Save address
            address_id = int(query.data.split('_')[1])
            context.user_data['subscription_creation']['address_id'] = address_id

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                methods_response = await client.get_payment_methods(user_token, context="subscription")
                if not methods_response.success:
                    await self._handle_api_error(update, methods_response.error, language)
                    return ConversationHandler.END

            available_methods = (methods_response.data or {}).get('data', {}).get('available_methods', [])

            text = i18n.get('telegram.subscription.select_payment', language)
            keyboard = SubscriptionKeyboards.payment_methods(available_methods, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return CONFIRM_SUBSCRIPTION

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_payment")
            return ConversationHandler.END

    async def confirm_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show preview and confirm subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Save payment method
            payment_method = query.data.split('_', 2)[2]
            context.user_data['subscription_creation']['payment_method'] = payment_method

            # Get preview from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                # Prepare preview data - only send fields required by SubscriptionPreviewRequest
                subscription_data = context.user_data['subscription_creation']
                preview_data = {
                    'billing_cycle': subscription_data.get('billing_cycle'),
                    'delivery_frequency': subscription_data.get('delivery_frequency'),
                    'items': subscription_data.get('items', []),
                    'discount_percentage': subscription_data.get('discount_percentage', 0.0)
                }
                response = await client.preview_subscription(user_token, preview_data)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

                preview = response.data.get("data", {}).get('preview', {})

            # Build confirmation message
            text = f"{i18n.get('telegram.subscription.confirm_title', language)}\n\n"
            text += f"📦 {i18n.get('telegram.subscription.items', language)}:\n"
            for item in preview.get('items', []):
                text += f"  • {item.get('product_name')} x{item.get('quantity')}\n"
            text += f"\n📅 {i18n.get('telegram.subscription.frequency', language)}: "
            preview_frequency_key = f"frequency_{preview.get('delivery_frequency')}"
            text += f"{i18n.get(preview_frequency_key, language)}\n"
            text += f"💰 {i18n.get('telegram.subscription.total', language)}: "
            text += f"{preview.get('total_amount')} {i18n.get('telegram.currency.uzs', language)}\n"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.confirm', language),
                    callback_data='confirm_create_subscription'
                ),
                InlineKeyboardButton(
                    i18n.get('telegram.cancel', language),
                    callback_data='cancel_subscription_creation'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

            return CONFIRM_SUBSCRIPTION

        except Exception as e:
            await self._handle_error(update, exc=e, operation="confirm_subscription")
            return ConversationHandler.END

    async def create_subscription_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Actually create the subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                # Prepare subscription data according to CreateSubscriptionRequest schema
                context_data = context.user_data['subscription_creation']

                # Generate subscription name from first product or use default
                subscription_name = f"Subscription - {context_data.get('billing_cycle', 'weekly').title()}"

                subscription_data = {
                    'name': subscription_name,
                    'billing_cycle': context_data.get('billing_cycle'),
                    'delivery_frequency': context_data.get('delivery_frequency'),
                    'delivery_address_id': context_data.get('address_id'),  # Map address_id to delivery_address_id
                    'payment_method': context_data.get('payment_method'),
                    'items': context_data.get('items', []),
                    'auto_renew': True,
                    'delivery_time_slot_id': context_data.get('delivery_time_slot_id'),  # Optional: user preference
                    'discount_percentage': context_data.get('discount_percentage', 0.0)
                }

                # Add optional fields if present
                if context_data.get('start_date'):
                    subscription_data['start_date'] = context_data['start_date']
                if context_data.get('description'):
                    subscription_data['description'] = context_data['description']

                response = await client.create_subscription(user_token, subscription_data)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

                subscription = response.data.get('subscription', {})

            text = f"✅ {i18n.get('telegram.subscription.created_success', language)}\n\n"
            text += f"🆔 {i18n.get('telegram.subscription.id', language)}: {subscription.get('id')}\n"
            text += f"🚚 {i18n.get('telegram.subscription.next_delivery', language)}: "
            text += f"{subscription.get('next_delivery_date')}"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.subscription.view', language),
                    callback_data=f'subscription_{subscription.get("id")}'
                ),
                InlineKeyboardButton(
                    i18n.get('telegram.back_to_menu', language),
                    callback_data='menu_main'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query, i18n.get('telegram.subscription.created_success', language))

            # Clear context
            if 'subscription_creation' in context.user_data:
                del context.user_data['subscription_creation']

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="create_subscription_confirmed")
            return ConversationHandler.END

    async def add_more_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Add More Items' button during subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Go back to product selection
            return await self.select_products(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_more_items")
            return ConversationHandler.END

    async def items_selection_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Done' button after selecting items"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Check if at least one item was added
            items = context.user_data.get('subscription_creation', {}).get('items', [])
            if not items:
                await self._ack(query, i18n.get('telegram.subscription.select_at_least_one_item', language), show_alert=True)
                return SELECT_QUANTITY

            # Proceed to frequency selection
            text = i18n.get('telegram.subscription.select_frequency', language)
            keyboard = SubscriptionKeyboards.subscription_frequency(language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return SELECT_ADDRESS

        except Exception as e:
            await self._handle_error(update, exc=e, operation="items_selection_done")
            return ConversationHandler.END

    async def cancel_subscription_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Clear context
            if 'subscription_creation' in context.user_data:
                del context.user_data['subscription_creation']

            text = i18n.get('telegram.subscription.creation_cancelled', language)
            keyboard = await main_menu_for(update.effective_user.id, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_subscription_creation")
            return ConversationHandler.END

    # ========== SUBSCRIPTION ACTIONS ==========

    async def subscription_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle subscription actions (pause/resume/cancel/edit)"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            action_data = query.data.split('_')
            action = action_data[0]  # pause, resume, cancel, edit
            sub_id = int(action_data[2])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                if action == 'pause':
                    response = await client.pause_subscription(user_token, sub_id)
                    success_msg = i18n.get('telegram.subscription.paused_success', language)

                elif action == 'resume':
                    response = await client.resume_subscription(user_token, sub_id)
                    success_msg = i18n.get('telegram.subscription.resumed_success', language)

                elif action == 'cancel':
                    # Cancel immediately when user clicks cancel button
                    cancel_data = {'immediate': True}
                    response = await client.cancel_subscription(user_token, sub_id, cancel_data)
                    success_msg = i18n.get('telegram.subscription.cancelled_success', language)

                else:
                    await self._ack(query, i18n.get('telegram.unknown_action', language))
                    return

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            await self._ack(query, success_msg)

            # Return to subscriptions menu
            await self.subscriptions_menu(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscription_actions")

    async def skip_delivery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Skip next delivery"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[2])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.skip_next_delivery(user_token, sub_id)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            text = i18n.get('telegram.subscription.skip_success', language)
            await self._ack(query, text)

            # Show updated subscription details
            await self.subscription_details(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="skip_delivery")

    async def view_billing_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View subscription billing history"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[3])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_billing_history(user_token, sub_id)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                history = response.data.get('billing_history', [])

            text = f"{i18n.get('telegram.subscription.billing_history', language)}\n\n"

            if not history:
                text += i18n.get('telegram.subscription.no_billing_history', language)
            else:
                for record in history[:10]:  # Show last 10
                    status_emoji = '✅' if record.get('status') == 'completed' else '❌'
                    text += f"{status_emoji} {record.get('billing_date')} - "
                    text += f"{record.get('amount')} {i18n.get('telegram.currency.uzs', language)}\n"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.back', language),
                    callback_data=f'subscription_{sub_id}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="view_billing_history")

    # ========== ITEM MANAGEMENT ==========

    async def manage_subscription_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show item management menu for subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[2])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get subscription items
                response = await client.get_subscription_items(user_token, sub_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                items = response.data.get("data", {}).get('items', [])

            text = f"{i18n.get('telegram.subscription.manage_items', language)}\n\n"

            if items:
                text += f"{i18n.get('telegram.subscription.current_items', language)}:\n"
                for item in items:
                    product_name = item.get('product', {}).get('name', i18n.get('telegram.common.unknown', language))
                    quantity = item.get('quantity', 1)
                    price = item.get('unit_price', 0)
                    text += f"  • {product_name} x{quantity} - {price * quantity} {i18n.get('telegram.currency.uzs', language)}\n"
            else:
                text += i18n.get('telegram.subscription.no_items', language)

            keyboard = SubscriptionKeyboards.item_management_menu(sub_id, items, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="manage_subscription_items")

    async def add_item_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding item to subscription"""
        try:
            context.user_data['editing_subscription_id'] = int(
                update.callback_query.data.split('_')[2]
            )
            return await self.add_item_back_to_products(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_start")
            return ConversationHandler.END

    async def add_item_back_to_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the add-an-item product list — the first step, and the one the
        quantity screen's Back button returns to.

        That Back used to emit `back_to_product_selection` with nothing in this
        conversation claiming it, so it fell through to the group-0
        `^back_to_product_` handler and died inside `int('selection')`.
        """
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            sub_id = context.user_data.get('editing_subscription_id')

            products = await self._fetch_and_display_products(
                update, context, language,
                header_key='telegram.subscription.select_product_to_add',
                back_callback=f'manage_items_{sub_id}'
            )
            if products is None:
                return ConversationHandler.END

            return ITEM_SELECT_PRODUCT

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_back_to_products")
            return ConversationHandler.END

    async def add_item_select_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select quantity for new item"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            product_id = int(update.callback_query.data.split('_')[2])
            context.user_data['adding_product_id'] = product_id

            await self._show_quantity_selector(update, language, 'telegram.subscription.select_quantity_for_item')
            return ITEM_SELECT_QUANTITY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_select_quantity")
            return ConversationHandler.END

    async def add_item_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and add item to subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            quantity = int(query.data.split('_')[2])
            sub_id = context.user_data.get('editing_subscription_id')
            product_id = context.user_data.get('adding_product_id')

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                item_data = {
                    'product_id': product_id,
                    'quantity': quantity
                }

                response = await client.add_subscription_item(user_token, sub_id, item_data)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

            text = f"✅ {i18n.get('telegram.subscription.item_added_successfully', language)}"
            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.subscription.back_to_items', language),
                    callback_data=f'manage_items_{sub_id}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

            # Clear context
            if 'editing_subscription_id' in context.user_data:
                del context.user_data['editing_subscription_id']
            if 'adding_product_id' in context.user_data:
                del context.user_data['adding_product_id']

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_confirm")
            return ConversationHandler.END

    async def update_item_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Update quantity for existing item"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)

            # Parse: update_item_{sub_id}_{item_id}
            parts = update.callback_query.data.split('_')
            sub_id = int(parts[2])
            item_id = int(parts[3])

            context.user_data['editing_subscription_id'] = sub_id
            context.user_data['editing_item_id'] = item_id

            # This flow never showed a product list — the customer picked an
            # existing item off the item-management menu, so that is where Back
            # belongs. The shared default (`back_to_product_selection`) would
            # offer a step that does not exist here.
            await self._show_quantity_selector(
                update, language, 'telegram.subscription.select_new_quantity',
                back_callback=f'manage_items_{sub_id}',
            )
            return ITEM_SELECT_QUANTITY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_item_quantity")
            return ConversationHandler.END

    async def update_item_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm item quantity update"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            quantity = int(query.data.split('_')[2])
            sub_id = context.user_data.get('editing_subscription_id')
            item_id = context.user_data.get('editing_item_id')

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                item_data = {'quantity': quantity}
                response = await client.update_subscription_item(user_token, sub_id, item_id, item_data)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

            text = f"✅ {i18n.get('telegram.subscription.item_updated_successfully', language)}"
            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.subscription.back_to_items', language),
                    callback_data=f'manage_items_{sub_id}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

            # Clear context
            if 'editing_subscription_id' in context.user_data:
                del context.user_data['editing_subscription_id']
            if 'editing_item_id' in context.user_data:
                del context.user_data['editing_item_id']

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_item_confirm")
            return ConversationHandler.END

    async def remove_item_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and remove item from subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Parse: remove_item_{sub_id}_{item_id}
            parts = query.data.split('_')
            sub_id = int(parts[2])
            item_id = int(parts[3])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.remove_subscription_item(user_token, sub_id, item_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            text = f"✅ {i18n.get('telegram.subscription.item_removed_successfully', language)}"
            await self._ack(query, text)

            # Return to item management
            context.user_data['callback_query_data'] = f'manage_items_{sub_id}'
            await self.manage_subscription_items(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="remove_item_confirm")

    # ========== SUBSCRIPTION EDITING ==========

    async def edit_subscription_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show subscription edit options"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[2])

            text = i18n.get('telegram.subscription.edit_menu', language)
            keyboard = SubscriptionKeyboards.edit_subscription_menu(sub_id, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_subscription_menu")

    async def change_frequency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Change subscription frequency"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[2])
            context.user_data['editing_subscription_id'] = sub_id

            text = i18n.get('telegram.subscription.select_new_frequency', language)
            keyboard = SubscriptionKeyboards.subscription_frequency(language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_frequency")

    async def update_frequency_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm frequency update"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            frequency = query.data.split('_')[1]
            sub_id = context.user_data.get('editing_subscription_id')

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                update_data = {'frequency': frequency}
                response = await client.update_subscription(user_token, sub_id, update_data)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            text = f"✅ {i18n.get('telegram.subscription.frequency_updated_successfully', language)}"
            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.subscription.view', language),
                    callback_data=f'subscription_{sub_id}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

            # Clear context
            if 'editing_subscription_id' in context.user_data:
                del context.user_data['editing_subscription_id']

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_frequency_confirm")

    async def change_payment_method_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show payment method selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[3])
            context.user_data['editing_subscription_id'] = sub_id

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                methods_response = await client.get_payment_methods(user_token, context="subscription")
                if not methods_response.success:
                    await self._handle_api_error(update, methods_response.error, language)
                    return

            available_methods = (methods_response.data or {}).get('data', {}).get('available_methods', [])

            text = i18n.get('telegram.subscription.select_new_payment_method', language)
            # Reached from the subscription's edit menu, not from the creation
            # flow's address step, so Back goes back there.
            keyboard = SubscriptionKeyboards.payment_methods(
                available_methods, language, back_callback=f'edit_sub_{sub_id}'
            )

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_payment_method_menu")

    async def change_payment_method_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm payment method change"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            payment_method = query.data.split('_', 2)[2]
            sub_id = context.user_data.get('editing_subscription_id')

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                payment_data = {'payment_method': payment_method}
                response = await client.change_payment_method(user_token, sub_id, payment_data)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            text = f"✅ {i18n.get('telegram.subscription.payment_method_updated_successfully', language)}"
            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.subscription.view', language),
                    callback_data=f'subscription_{sub_id}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

            # Clear context
            if 'editing_subscription_id' in context.user_data:
                del context.user_data['editing_subscription_id']

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_payment_method_confirm")

    # ========== STATISTICS AND LOGS ==========

    async def view_subscription_statistics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View subscription statistics"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_subscription_statistics(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                stats = response.data.get('statistics', {})

            text = f"📊 {i18n.get('telegram.subscription.statistics', language)}\n\n"
            text += f"📦 {i18n.get('telegram.subscription.total_deliveries', language)}: {stats.get('total_deliveries', 0)}\n"
            text += f"💰 {i18n.get('telegram.subscription.total_spent', language)}: {stats.get('total_spent', 0)} {i18n.get('telegram.currency.uzs', language)}\n"
            text += f"💵 {i18n.get('telegram.subscription.average_order', language)}: {stats.get('average_order_value', 0)} {i18n.get('telegram.currency.uzs', language)}\n"
            text += f"💚 {i18n.get('telegram.subscription.total_savings', language)}: {stats.get('total_savings', 0)} {i18n.get('telegram.currency.uzs', language)}\n"

            if stats.get('most_ordered_product'):
                text += f"\n⭐ {i18n.get('telegram.subscription.favorite_product', language)}: {stats['most_ordered_product']}\n"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.back', language),
                    callback_data='menu_subscriptions'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="view_subscription_statistics")

    async def view_subscription_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """View subscription activity logs"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[2])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_subscription_logs(user_token, sub_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                logs = response.data.get('data', {}).get('items', [])

            text = f"📋 {i18n.get('telegram.subscription.activity_logs', language)}\n\n"

            if not logs:
                text += i18n.get('telegram.subscription.no_activity_logs', language)
            else:
                for log in logs[:10]:  # Show last 10
                    action_icon = {
                        'created': '✨',
                        'updated': '✏️',
                        'paused': '⏸️',
                        'resumed': '▶️',
                        'cancelled': '❌',
                        'item_added': '➕',
                        'item_removed': '➖',
                        'delivery_skipped': '⏭️'
                    }.get(log.get('action'), '📝')

                    text += f"{action_icon} {log.get('details')} - {log.get('created_at')}\n"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.back', language),
                    callback_data=f'subscription_{sub_id}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="view_subscription_logs")

    async def retry_failed_billing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Retry failed billing for subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = int(query.data.split('_')[3])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.retry_billing(user_token, sub_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

            text = f"✅ {i18n.get('telegram.subscription.billing_retry_initiated', language)}"
            await self._ack(query, text, show_alert=True)

            # Refresh subscription details
            await self.subscription_details(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="retry_failed_billing")

    # ========== HELPER METHODS ==========

    async def _fetch_and_display_products(self, update, context, language, header_key, back_callback=None):
        """Shared helper: fetch products from API and display selection keyboard.

        Returns list of products on success, or None on failure (error already shown).
        """
        query = update.callback_query

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return None

            response = await client.get_products(user_token, {'per_page': 20})
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return None

            products = response.data.get('data', {}).get('items', [])

        # Build text
        products_text = i18n.get(header_key, language) + "\n\n"
        for idx, product in enumerate(products[:10], 1):
            price = product.get('base_price', product.get('pricing', {}).get('base_price', 0))
            products_text += f"{idx}. {product['name']} - {price} UZS\n"

        # Build keyboard
        buttons = []
        for product in products[:10]:
            buttons.append([InlineKeyboardButton(
                f"➕ {product['name']}",
                callback_data=f"sub_product_{product['id']}"
            )])

        if back_callback:
            buttons.append([InlineKeyboardButton(
                i18n.get('telegram.back', language),
                callback_data=back_callback
            )])
        else:
            buttons.append([InlineKeyboardButton(
                i18n.get('telegram.cancel', language),
                callback_data='cancel_subscription_creation'
            )])

        keyboard = InlineKeyboardMarkup(buttons)

        if query:
            await self._edit_or_replace_callback_message(query, text=products_text, reply_markup=keyboard)
            await self._ack(query)
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=products_text,
                reply_markup=keyboard
            )

        return products

    async def _show_quantity_selector(self, update, language, text_key, back_callback=None):
        """Shared helper: display quantity selection keyboard.

        `back_callback` names the screen this one was reached from; the builder
        default (`back_to_product_selection`) is correct for the two flows that
        came from a product list.
        """
        query = update.callback_query
        text = i18n.get(text_key, language)
        keyboard = SubscriptionKeyboards.quantity_selector(
            language, back_callback=back_callback
        )
        await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
        await self._ack(query)

    def _build_address_keyboard(self, addresses, language):
        """Build keyboard for address selection"""
        keyboard = []
        address_fallback = i18n.get('telegram.common.address', language)
        for addr in addresses:
            button_text = f"{addr.get('address_line1') or address_fallback} - {addr.get('city', '')}"
            if addr.get('is_default'):
                button_text = f"⭐ {button_text}"
            keyboard.append([InlineKeyboardButton(
                button_text,
                callback_data=f'addr_{addr.get("id")}'
            )])

        keyboard.append([InlineKeyboardButton(
            i18n.get('telegram.subscription.add_new_address', language),
            callback_data='add_address'
        )])

        return InlineKeyboardMarkup(keyboard)



# Global handler instance
subscription_handlers = SubscriptionHandlers()

subscription_handlers.SELECT_PRODUCTS = SELECT_PRODUCTS
subscription_handlers.SELECT_QUANTITY = SELECT_QUANTITY
subscription_handlers.SELECT_FREQUENCY = SELECT_FREQUENCY
subscription_handlers.SELECT_ADDRESS = SELECT_ADDRESS
subscription_handlers.SELECT_START_DATE = SELECT_START_DATE
subscription_handlers.SELECT_PAYMENT = SELECT_PAYMENT
subscription_handlers.CONFIRM_SUBSCRIPTION = CONFIRM_SUBSCRIPTION
subscription_handlers.ITEM_ACTION = ITEM_ACTION
subscription_handlers.ITEM_SELECT_PRODUCT = ITEM_SELECT_PRODUCT
subscription_handlers.ITEM_SELECT_QUANTITY = ITEM_SELECT_QUANTITY
