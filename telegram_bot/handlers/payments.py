"""
Telegram payment-link handler.

Implements redirect-based external payment links for the configured PSP.
"""
import logging
from typing import Dict, Any

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from i18n import i18n
from api_client import api_client
from utils import get_auth_token, format_price
from keyboards import PaymentKeyboards
from handlers.base import BaseHandler

logger = logging.getLogger('handlers.payments')


class PaymentHandlers(BaseHandler):
    """Handle redirect-based PSP payment links in Telegram."""

    def __init__(self):
        super().__init__()
        logger.info("PaymentHandlers initialized (external payment link mode)")

    # =========================================================================
    # CORE PAYMENT METHODS
    # =========================================================================

    async def send_payment_link(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order_data: Dict[str, Any],
        payment_method: str = 'click',
    ) -> bool:
        """
        Send external payment link to user via Redirect Method.
        
        Differs from native invoice: sends a message with an inline button
        that redirects to the configured PSP checkout page.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            # Extract order details
            order_id = order_data.get('id')
            order_number = order_data.get('order_number', str(order_id))
            total_amount = order_data.get('total_amount', 0)
            
            # 1. Authenticate with Backend
            async with api_client as client:
                token = await get_auth_token(update, context, client)
                if not token:
                    logger.error("Failed to get auth token for payment-link generation")
                    await self._send_error_message(
                        update, context, 
                        i18n.get('telegram.auth.login_required', language)
                    )
                    return False
                    
                # 2. Request Payment Link
                # We use the generic 'POST /payments/create' endpoint via api_client
                # Use dynamic bot username
                bot = context.bot
                bot_username = bot.username or (await bot.get_me()).username
                return_url = f"https://t.me/{bot_username}"

                result = await client.create_payment(token, {
                    'order_id': order_id,
                    'payment_method': payment_method,
                    'return_url': return_url
                })
                
                if not result.success:
                    logger.error(f"Failed to create {payment_method} link: {result.error}")
                    await self._send_error_message(
                        update, context, 
                        i18n.get('telegram.payment.create_link_failed_with_error', language, error=result.error)
                    )
                    return False
                
                # Let's inspect result structure safely
                response_body = result.data or {}
                if 'data' in response_body:
                    response_data = response_body['data']
                else:
                    response_data = response_body

                payment_link_data = response_data.get('payment_link', {})
                # It accepts dict (from payment_service) or string? 
                # payment_service returns dict.
                if isinstance(payment_link_data, dict):
                    payment_url = payment_link_data.get('payment_url')
                else:
                    payment_url = str(payment_link_data)
                
                if not payment_url:
                     logger.error(f"No payment_url in response: {result.data}")
                     await self._send_error_message(
                         update,
                         context,
                         i18n.get('telegram.payment.invalid_link_received', language)
                     )
                     return False
                     
            # 3. Send Message with Button
            msg_text = i18n.get(
                'telegram.payment.pay_message',
                language,
                order_number=order_number,
                amount=format_price(total_amount)
            )

            keyboard = PaymentKeyboards.payment_link(payment_url, language)

            # Edit the existing confirmation message instead of sending a new one
            query = update.callback_query
            if query:
                sent_message = await query.edit_message_text(
                    text=msg_text,
                    reply_markup=keyboard
                )
            else:
                sent_message = await update.effective_message.reply_text(
                    text=msg_text,
                    reply_markup=keyboard
                )

            # Store payment message_id in Redis for webhook to edit on success
            message_id = None
            if query:
                if hasattr(sent_message, 'message_id'):
                    message_id = sent_message.message_id
                else:
                    message_id = query.message.message_id
            elif hasattr(sent_message, 'message_id'):
                message_id = sent_message.message_id

            if message_id and order_id:
                try:
                    from token_manager import token_manager
                    if token_manager and token_manager.redis:
                        redis_key = f"bot:payment_msg:{order_id}"
                        await token_manager.redis.setex(redis_key, 3600, str(message_id))
                except Exception as redis_err:
                    logger.warning(f"Failed to store payment message_id in Redis: {redis_err}")
            
            logger.info(f"{payment_method} link sent for order {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error sending payment link: {e}", exc_info=True)
            language = await i18n.get_user_language(update.effective_user.id)
            await self._send_error_message(
                update, context, 
                i18n.get('telegram.payment.failed_message', language)
            )
            return False

    async def send_payme_invoice(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order_data: Dict[str, Any],
        payment_method: str = 'click',
    ) -> bool:
        """Backward-compatible wrapper for old call sites."""
        return await self.send_payment_link(
            update,
            context,
            order_data,
            payment_method=payment_method,
        )

    # =========================================================================
    # ERROR HANDLING & RECOVERY
    # =========================================================================

    async def retry_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle payment retry request.
        Re-fetches order and sends new invoice.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data
            order_id = int(query.data.split('_')[-1])

            await query.answer()

            # Fetch order details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await query.edit_message_text(
                        i18n.get('telegram.error.auth_failed', language)
                    )
                    return

                response = await client.get_order(user_token, order_id)
                if not response.success:
                    await query.edit_message_text(
                        i18n.get('telegram.payment.error_order_not_found', language)
                    )
                    return

                order = response.data.get('data', {}).get('order', {})

            # Check if order can still be paid
            if order.get('is_paid'):
                await query.edit_message_text(
                    i18n.get('telegram.payment.error_already_paid', language)
                )
                return

            payment_info = order.get('payment_info') or {}
            provider_method = payment_info.get('payment_provider') or order.get('payment_method') or 'click'
            if provider_method == 'card':
                provider_method = 'click'

            # Send new payment link
            await self.send_payment_link(update, context, order, payment_method=provider_method)

            logger.info(f"Payment retry initiated for order {order_id} by user {user_id}")

        except Exception as e:
            logger.error(f"Error in payment retry: {e}", exc_info=True)
            await self._handle_error(update, context)

    async def switch_payment_method(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle request to switch payment method.
        Returns user to payment method selection.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data
            order_id = int(query.data.split('_')[-1])

            await query.answer()

            # Store order ID in context for payment method selection
            context.user_data['pending_order_id'] = order_id

            # Show payment method selection
            from handlers.orders import order_handlers

            payment_methods = [
                {'type': 'cash', 'name': i18n.get('telegram.payment_cash', language)},
                {'type': 'card', 'name': i18n.get('telegram.payment_card', language)},
            ]

            from keyboards import OrderKeyboards

            payment_text = i18n.get('telegram.orders.select_payment', language)
            keyboard = OrderKeyboards.payment_methods(payment_methods, language)

            await query.edit_message_text(
                text=payment_text,
                reply_markup=keyboard
            )

            logger.info(f"Payment method switch initiated for order {order_id} by user {user_id}")

        except Exception as e:
            logger.error(f"Error in switch payment method: {e}", exc_info=True)
            await self._handle_error(update, context)

    async def cancel_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle payment cancellation.
        Shows options to retry, switch method, or cancel order.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data
            order_id = int(query.data.split('_')[-1])

            await query.answer()

            # Show cancellation options
            cancelled_text = i18n.get('telegram.payment.cancelled_message', language)

            keyboard = PaymentKeyboards.payment_failed(order_id, language)

            await query.edit_message_text(
                text=f"❌ {cancelled_text}",
                reply_markup=keyboard
            )

            logger.info(f"Payment cancelled for order {order_id} by user {user_id}")

        except Exception as e:
            logger.error(f"Error in cancel payment: {e}", exc_info=True)
            await self._handle_error(update, context)




# Global handler instance
payment_handlers = PaymentHandlers()
